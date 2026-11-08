from __future__ import annotations

from pathlib import Path

import pytest

from ntcip_ctrl.timing.corridor_config import build_corridor
from ntcip_ctrl.timing.models import Corridor, IntersectionTiming, PhaseParams

ROOT = Path(__file__).resolve().parents[1]


def eight_phase(int_id: int = 1, offset: float = 0.0, *, left_split: float = 13, thru_a: float = 47,
                left_b: float = 12, thru_b: float = 28) -> IntersectionTiming:
    def lp(split: float) -> PhaseParams:
        return PhaseParams(split_s=split, min_green_s=5, passage_s=2.5, yellow_s=4, red_clear_s=1)

    def tp(split: float, recall: str = "none") -> PhaseParams:
        return PhaseParams(split_s=split, min_green_s=8, passage_s=3.0, yellow_s=4, red_clear_s=2, recall=recall)  # type: ignore[arg-type]

    return IntersectionTiming(
        int_id=int_id, cycle_s=left_split + thru_a + left_b + thru_b, offset_s=offset,
        phases={1: lp(left_split), 5: lp(left_split), 2: tp(thru_a, "max"), 6: tp(thru_a, "max"),
                3: lp(left_b), 7: lp(left_b), 4: tp(thru_b), 8: tp(thru_b)},
    )


@pytest.fixture
def timing() -> IntersectionTiming:
    return eight_phase()


@pytest.fixture(scope="session")
def corridor() -> Corridor:
    return build_corridor(ROOT / "configs" / "corridor.yaml")
