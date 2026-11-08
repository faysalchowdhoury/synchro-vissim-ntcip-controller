from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from ntcip_ctrl.core.exceptions import UTDFParseError
from ntcip_ctrl.corridor.bandwidth import bandwidth, optimize_offsets
from ntcip_ctrl.timing.hcm import lane_group_delay, los
from ntcip_ctrl.timing.models import Corridor
from ntcip_ctrl.timing.utdf import parse_utdf, write_utdf

ROOT = Path(__file__).resolve().parents[1]
UTDF = ROOT / "examples" / "main_street_existing.utdf.csv"


def test_parse_example_utdf() -> None:
    timings, vols = parse_utdf(UTDF)
    assert sorted(timings) == [101, 102, 103, 104]
    t = timings[101]
    assert t.cycle_s == 100 and t.coord_phases == (2, 6)
    assert t.ring1 == [[1, 2], [3, 4]] and t.ring2 == [[5, 6], [7, 8]]
    assert t.phases[2].split_s == 47 and t.phases[2].recall == "max"
    assert t.phases[1].passage_s == 2.5
    assert timings[102].ring1 == [[1, 2], [4]]
    assert vols[101][2] == 1050 and vols[101][1] == 100 and vols[101][4] == 300


def test_utdf_roundtrip(tmp_path: Path) -> None:
    timings, vols = parse_utdf(UTDF)
    out = write_utdf(timings, tmp_path / "rt.csv", vols)
    t2, v2 = parse_utdf(out)
    assert t2 == timings
    assert v2 == vols


def test_utdf_errors(tmp_path: Path) -> None:
    p = tmp_path / "bad.csv"
    p.write_text("[Phases]\nPhases Data\nRECORDNAME,INTID,D1\n")
    with pytest.raises(UTDFParseError):
        parse_utdf(p)
    text = UTDF.read_text().replace("MaxGreen,101", "MaxGreenX,101")
    with pytest.raises(UTDFParseError):
        parse_utdf(text)


def test_bandwidth_two_signal_closed_form() -> None:
    # two identical signals, green 0-41 of C=100; travel time 20 s.
    from .conftest import eight_phase
    from ntcip_ctrl.timing.models import Approach, CorridorIntersection

    ap = {p: Approach(volume_vph=100) for p in range(1, 9)}
    c = Corridor(name="t", speed_mps=10.0, intersections=[
        CorridorIntersection(timing=eight_phase(1), position_m=0, approaches=ap),
        CorridorIntersection(timing=eight_phase(2), position_m=200, approaches=ap)])
    assert bandwidth(c, [0, 20]).eb_s == pytest.approx(41, abs=0.11)      # perfect EB progression
    assert bandwidth(c, [0, 20]).wb_s == pytest.approx(1, abs=0.11)       # WB: 41 - 2*20
    assert bandwidth(c, [0, 0]).eb_s == pytest.approx(21, abs=0.11)       # simultaneous: 41 - 20
    best = optimize_offsets(c, restarts=4)
    assert best.total_s >= bandwidth(c, [0, 0]).total_s - 1e-9
    assert best.total_s == pytest.approx(42, abs=0.3)                    # offset 0 (21 + 21) or 20 (41 + 1)


def test_optimizer_improves_example(corridor: Corridor) -> None:
    before = bandwidth(corridor)
    after = optimize_offsets(corridor, restarts=8)
    assert after.total_s > before.total_s
    assert after.offsets_s[0] == 0.0


def test_hcm_delay_matches_hand_calculation() -> None:
    d = lane_group_delay(600, 1800, 40, 100, arrivals_on_green=None)
    g_c, x = 0.4, 600 / 720
    d1 = 0.5 * 100 * (1 - g_c) ** 2 / (1 - x * g_c)
    d2 = 900 * 0.25 * ((x - 1) + np.sqrt((x - 1) ** 2 + 8 * 0.5 * x / (720 * 0.25)))
    assert d.delay_s == pytest.approx(d1 + d2)
    assert d.delay_s == pytest.approx(37.9, abs=0.1)  # 27.0 uniform + 10.9 incremental
    assert los(d.delay_s) == "D"
    assert lane_group_delay(600, 1800, 40, 100, arrivals_on_green=0.7).delay_s < d.delay_s
