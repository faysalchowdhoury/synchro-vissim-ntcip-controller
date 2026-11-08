from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from ntcip_ctrl.nema.controller import NemaController
from ntcip_ctrl.timing.models import BARRIER_A, BARRIER_B, IntersectionTiming, PhaseParams
from ntcip_ctrl.timing.schedule import nominal_schedule

from .conftest import eight_phase

DT = 0.1


def run(ctrl: NemaController, seconds: float, calls_fn=None) -> list[dict[int, str]]:  # type: ignore[no-untyped-def]
    """States *after* each tick; entry i describes time (i + 1) * dt."""
    out = []
    for i in range(int(round(seconds / DT))):
        calls = calls_fn(i * DT) if calls_fn else set()
        ctrl.step(set(calls), set(calls))
        out.append(ctrl.states())
    return out


def greens(trace: list[dict[int, str]], phase: int) -> list[tuple[float, float]]:
    out, start = [], None
    for i, s in enumerate(trace):
        t = (i + 1) * DT
        if s[phase] == "G" and start is None:
            start = t
        elif s[phase] != "G" and start is not None:
            out.append((round(start, 1), round(t, 1)))
            start = None
    return out


def test_nominal_schedule_reference_is_coord_begin_of_green(timing: IntersectionTiming) -> None:
    s = nominal_schedule(timing)
    assert s[2].start == 0 and s[6].start == 0 and s[2].green_end == pytest.approx(41)
    assert s[3].start == pytest.approx(47) and s[4].start == pytest.approx(59) and s[4].green_end == pytest.approx(81)
    assert s[1].start == pytest.approx(87) and s[1].end == pytest.approx(100)


@pytest.mark.parametrize("offset", [0.0, 30.0])
def test_fixed_time_reproduces_nominal_schedule(offset: float) -> None:
    ctrl = NemaController(eight_phase(offset=offset), "fixed", dt=DT)
    trace = run(ctrl, 400)
    g2 = [g for g in greens(trace, 2) if g[0] > 1.0]  # drop intervals truncated at t=0
    assert len(g2) >= 3
    for a, b in g2:
        assert b - a == pytest.approx(41, abs=0.15)
        assert ((a - offset) % 100 + 0.5) % 100 == pytest.approx(0.5, abs=0.15)
    g4 = [g for g in greens(trace, 4) if g[0] > 1.0]
    assert len(g4) >= 3
    for a, b in g4:
        assert b - a == pytest.approx(22, abs=0.15)
        assert (a - offset) % 100 == pytest.approx(59, abs=0.15)


def test_actuated_rests_in_coordinated_phases_without_demand(timing: IntersectionTiming) -> None:
    trace = run(NemaController(timing, "actuated", dt=DT), 300)
    assert all(s[2] == "G" and s[6] == "G" for s in trace)
    assert all(s[p] == "R" for s in trace for p in (1, 3, 4, 5, 7, 8))


def test_side_call_yields_at_force_off_dual_entry_and_early_return(timing: IntersectionTiming) -> None:
    # continuous demand on phase 4 only, from t = 10 s to t = 70 s
    trace = run(NemaController(timing, "actuated", dt=DT), 200, lambda t: {4} if 10 <= t < 70 else set())
    g4 = greens(trace, 4)
    g8 = greens(trace, 8)
    assert g4 and g4[0][0] == pytest.approx(47, abs=0.2)  # coord yields at 41, +6 s clearance
    assert g8 and g8[0][0] == pytest.approx(g4[0][0], abs=0.2)  # dual entry brings phi8 up with phi4
    end4 = g4[0][1]
    assert end4 < 81 - 1  # gapped out after demand stopped, before the 81 s force-off
    g2 = greens(trace, 2)
    second_start = g2[1][0]
    assert second_start == pytest.approx(end4 + 6, abs=0.3)  # early return to coordinated green
    assert second_start < 100


def test_gap_out_after_minimum_green(timing: IntersectionTiming) -> None:
    # a locked call on both side-street lefts placed during coordinated green
    ctrl = NemaController(timing, "actuated", dt=DT)
    trace = run(ctrl, 150, lambda t: {3, 7} if 30 <= t < 31 else set())
    (a, b), = greens(trace, 3)
    assert a == pytest.approx(47, abs=0.2)
    assert b - a == pytest.approx(5.0, abs=0.15)  # min green 5 s; passage already expired -> gap out
    assert ctrl.stats[3].terminations["gap_out"] == 1


def test_barrier_hold_with_dual_entry(timing: IntersectionTiming) -> None:
    # only phi3 is called: ring 2 must still time a barrier-B phase, so dual entry brings up phi8
    # (min green 8 s). phi3 gaps out at 5 s but must hold green until phi8 can cross the barrier.
    trace = run(NemaController(timing, "actuated", dt=DT), 150, lambda t: {3} if 30 <= t < 31 else set())
    (a3, b3), = greens(trace, 3)
    (a8, b8), = greens(trace, 8)
    assert a3 == pytest.approx(a8, abs=0.15)
    assert b3 - a3 == pytest.approx(8.0, abs=0.15) and b8 == pytest.approx(b3, abs=0.15)


def test_force_off_with_continuous_demand(timing: IntersectionTiming) -> None:
    trace = run(NemaController(timing, "actuated", dt=DT), 150, lambda t: {3, 7} if t >= 30 else set())
    a, b = greens(trace, 3)[0]
    assert b == pytest.approx(54, abs=0.2)  # fixed force-off = 47 + 7


def test_late_call_is_skipped_when_window_closed(timing: IntersectionTiming) -> None:
    # call for phi3 arrives at 51 s: only 3 s remain before its 54 s force-off (< 5 s min green)
    trace = run(NemaController(timing, "actuated", dt=DT), 99, lambda t: {3, 4} if t >= 51 else set())
    assert not greens(trace, 3)
    assert greens(trace, 4)


def test_max_out() -> None:
    t = eight_phase()
    p4 = t.phases[4].model_copy(update={"max_green_s": 12.0})
    t = IntersectionTiming.model_validate({**t.model_dump(), "phases": {**t.phases, 4: p4}})
    trace = run(NemaController(t, "actuated", dt=DT), 150, lambda s: {4} if s >= 30 else set())
    a, b = greens(trace, 4)[0]
    assert b - a == pytest.approx(12.0, abs=0.15)


def test_barrier_and_ring_safety_under_random_demand(timing: IntersectionTiming) -> None:
    rng = np.random.default_rng(3)
    calls_at = {round(i * DT, 1): {p for p in range(1, 9) if rng.random() < 0.02} for i in range(30_000)}
    trace = run(NemaController(timing, "actuated", dt=DT), 3000, lambda t: calls_at.get(round(t, 1), set()))
    served = set()
    for s in trace:
        live = {p for p, v in s.items() if v != "R"}
        served |= {p for p, v in s.items() if v == "G"}
        assert not (live & BARRIER_A and live & BARRIER_B), s
        assert len(live & {1, 2, 3, 4}) <= 1 and len(live & {5, 6, 7, 8}) <= 1, s
    assert served == set(range(1, 9))


def test_invalid_timing_rejected() -> None:
    good = eight_phase()
    bad = {**good.model_dump(), "phases": {**good.phases, 1: PhaseParams(split_s=20, min_green_s=5)}}
    with pytest.raises(ValidationError):
        IntersectionTiming.model_validate(bad)
    with pytest.raises(ValidationError):
        PhaseParams(split_s=8, min_green_s=10, yellow_s=4, red_clear_s=1)
