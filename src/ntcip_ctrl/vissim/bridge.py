"""Software-in-the-loop: drive Vissim signal groups from emulated NEMA controllers.

Each Vissim time step the bridge

1. reads detector presence/impulse for every mapped detector,
2. steps each :class:`NemaController` with those calls and actuations,
3. writes the resulting phase indications to the mapped signal groups
   (``SigState`` = ``GREEN`` / ``AMBER`` / ``RED``),
4. advances Vissim one step (``Simulation.RunSingleStep``).

Mapping comes from YAML::

    network: corridor.inpx
    controllers:
      101: {sc: 1, groups: {2: 2, 6: 6, 4: 4, 8: 8, 1: 1, 5: 5, 3: 3, 7: 7},
            detectors: {2: [21, 22], 6: [61, 62], 4: [41], 8: [81], 1: [11], 5: [51], 3: [31], 7: [71]}}

In Vissim, set the signal controllers to a type whose signal groups accept external
``SigState`` writes and configure the detectors. COM attribute names (``Detection``,
``Impulse``, ``SigState``) are those documented for recent Vissim releases but **this module
has not been run against a licensed Vissim installation**; verify them in the COM help of
your version before use.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from pydantic import Field

from ..core.config import StrictModel, load_model
from ..core.exceptions import VissimAutomationError
from ..core.logging import get_logger
from ..nema.controller import ControllerMode, NemaController
from ..timing.models import IntersectionTiming

log = get_logger(__name__)
_STATE_MAP = {"G": "GREEN", "Y": "AMBER", "R": "RED"}


class ControllerMap(StrictModel):
    sc: int
    groups: dict[int, int]
    detectors: dict[int, list[int]] = Field(default_factory=dict)


class BridgeConfig(StrictModel):
    network: Path
    resolution: int = Field(10, ge=1, le=20)
    period_s: float = Field(3600.0, gt=0.0)
    controllers: dict[int, ControllerMap]


class VissimSignalBridge:
    def __init__(self, config_path: str | Path, timings: dict[int, IntersectionTiming],
                 mode: ControllerMode = "actuated", prog_id: str = "Vissim.Vissim") -> None:
        if sys.platform != "win32":
            raise VissimAutomationError("Vissim COM requires Windows")
        self.cfg = load_model(config_path, BridgeConfig)
        missing = set(self.cfg.controllers) - set(timings)
        if missing:
            raise VissimAutomationError("no timing for mapped controllers", context={"missing": sorted(missing)})
        self.timings = timings
        self.mode = mode
        self.prog_id = prog_id
        self._vissim: Any = None

    def __enter__(self) -> "VissimSignalBridge":
        try:
            import win32com.client as com
        except ImportError as exc:
            raise VissimAutomationError("pip install pywin32") from exc
        try:
            self._vissim = com.Dispatch(self.prog_id)
            self._vissim.LoadNet(str(self.cfg.network.resolve()), False)
        except Exception as exc:  # noqa: BLE001
            raise VissimAutomationError(f"cannot start Vissim / load network: {exc}") from exc
        return self

    def __exit__(self, *exc: object) -> None:
        if self._vissim is not None:
            try:
                self._vissim.Exit()
            except Exception:  # noqa: BLE001
                log.warning("vissim.exit_failed")
            self._vissim = None

    def run(self) -> dict[int, dict[int, float]]:
        """Run the full period; returns green seconds per controller and phase."""
        vis = self._vissim
        if vis is None:
            raise VissimAutomationError("use as a context manager")
        dt = 1.0 / self.cfg.resolution
        vis.Simulation.SetAttValue("SimRes", self.cfg.resolution)
        vis.Simulation.SetAttValue("SimPeriod", self.cfg.period_s)
        ctrls = {iid: NemaController(self.timings[iid], self.mode, dt=dt) for iid in self.cfg.controllers}
        sgs = {iid: {ph: vis.Net.SignalControllers.ItemByKey(m.sc).SGs.ItemByKey(sg) for ph, sg in m.groups.items()}
               for iid, m in self.cfg.controllers.items()}
        dets = {iid: {ph: [vis.Net.Detectors.ItemByKey(d) for d in ds] for ph, ds in m.detectors.items()}
                for iid, m in self.cfg.controllers.items()}
        greens: dict[int, dict[int, float]] = {iid: {} for iid in ctrls}
        for _ in range(int(self.cfg.period_s * self.cfg.resolution)):
            for iid, ctrl in ctrls.items():
                calls, acts = set(), set()
                for ph, objs in dets[iid].items():
                    for d in objs:
                        if d.AttValue("Detection"):
                            calls.add(ph)
                        if d.AttValue("Impulse"):
                            acts.add(ph)
                ctrl.step(calls, acts | calls)
                for ph, sg in sgs[iid].items():
                    state = ctrl.phase_state(ph)
                    sg.SetAttValue("SigState", _STATE_MAP[state])
                    if state == "G":
                        greens[iid][ph] = greens[iid].get(ph, 0.0) + dt
            vis.Simulation.RunSingleStep()
        return greens
