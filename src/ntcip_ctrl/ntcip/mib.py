"""NTCIP 1202 object subset mapped onto a live :class:`NemaController`.

OIDs (NTCIP 1202 ``asc`` node = 1.3.6.1.4.1.1206.4.2.1):

==========================  ===========================  =========  ======
object                      OID suffix                   units      access
==========================  ===========================  =========  ======
phaseMinimumGreen.p         .1.2.1.4.p                   s          RW
phasePassage.p              .1.2.1.5.p                   0.1 s      RW
phaseMaximum1.p             .1.2.1.6.p                   s          RW
phaseYellowChange.p         .1.2.1.8.p                   0.1 s      RW
phaseRedClear.p             .1.2.1.9.p                   0.1 s      RW
phaseStatusGroupReds.1      .1.4.1.2.1                   bitmask    RO
phaseStatusGroupYellows.1   .1.4.1.3.1                   bitmask    RO
phaseStatusGroupGreens.1    .1.4.1.4.1                   bitmask    RO
==========================  ===========================  =========  ======

The suffixes follow the published NTCIP 1202 v03 MIB; check a vendor's MIB before
pointing this at real hardware (many controllers add proprietary objects). Status bit
``n-1`` of group 1 is phase ``n``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from pydantic import ValidationError

from ..nema.controller import NemaController
from .ber import BAD_VALUE, NO_ERROR, NO_SUCH_NAME, READ_ONLY, Value

ASC: tuple[int, ...] = (1, 3, 6, 1, 4, 1, 1206, 4, 2, 1)
PHASE_ENTRY = ASC + (1, 2, 1)
STATUS_ENTRY = ASC + (1, 4, 1)

# column -> (PhaseParams field, scale: wire value = field * scale)
PHASE_COLUMNS: dict[int, tuple[str, float]] = {
    4: ("min_green_s", 1.0),
    5: ("passage_s", 10.0),
    6: ("max_green_s", 1.0),
    8: ("yellow_s", 10.0),
    9: ("red_clear_s", 10.0),
}


@dataclass
class MibView:
    controller: NemaController

    def _oids(self) -> list[tuple[int, ...]]:
        out = [PHASE_ENTRY + (col, p) for col in PHASE_COLUMNS for p in sorted(self.controller.timing.phases)]
        out += [STATUS_ENTRY + (col, 1) for col in (2, 3, 4)]
        return sorted(out)

    def _status(self, col: int) -> int:
        want = {2: "R", 3: "Y", 4: "G"}[col]
        mask = 0
        for p, s in self.controller.states().items():
            if s == want:
                mask |= 1 << (p - 1)
        return mask

    def get(self, oid: tuple[int, ...]) -> tuple[int, Value]:
        if oid[: len(PHASE_ENTRY)] == PHASE_ENTRY and len(oid) == len(PHASE_ENTRY) + 2:
            col, p = oid[-2], oid[-1]
            if col in PHASE_COLUMNS and p in self.controller.timing.phases:
                name, scale = PHASE_COLUMNS[col]
                pp = self.controller.timing.phases[p]
                val = pp.max_s if name == "max_green_s" else getattr(pp, name)
                return NO_ERROR, int(round(val * scale))
        if oid[: len(STATUS_ENTRY)] == STATUS_ENTRY and len(oid) == len(STATUS_ENTRY) + 2:
            col, grp = oid[-2], oid[-1]
            if col in (2, 3, 4) and grp == 1:
                return NO_ERROR, self._status(col)
        return NO_SUCH_NAME, None

    def get_next(self, oid: tuple[int, ...]) -> tuple[tuple[int, ...] | None, int, Value]:
        for o in self._oids():
            if o > oid:
                err, v = self.get(o)
                return o, err, v
        return None, NO_SUCH_NAME, None

    def set(self, oid: tuple[int, ...], value: Value) -> int:
        if oid[: len(STATUS_ENTRY)] == STATUS_ENTRY:
            return READ_ONLY
        if not (oid[: len(PHASE_ENTRY)] == PHASE_ENTRY and len(oid) == len(PHASE_ENTRY) + 2):
            return NO_SUCH_NAME
        col, p = oid[-2], oid[-1]
        if col not in PHASE_COLUMNS or p not in self.controller.timing.phases:
            return NO_SUCH_NAME
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return BAD_VALUE
        name, scale = PHASE_COLUMNS[col]
        timing = self.controller.timing
        try:
            new_pp = timing.phases[p].model_copy(update={name: value / scale})
            new_pp = type(new_pp).model_validate(new_pp.model_dump())
            new_timing = type(timing).model_validate({**timing.model_dump(), "phases": {**timing.phases, p: new_pp}})
        except (ValidationError, ValueError):
            return BAD_VALUE
        self.controller.update_timing(new_timing)
        return NO_ERROR


Handler = Callable[[tuple[int, ...]], tuple[int, Value]]
