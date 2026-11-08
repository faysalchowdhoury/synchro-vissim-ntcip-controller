"""Signal-retiming study: existing vs. optimised offsets, fixed-time vs. actuated-coordinated."""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from scipy import stats

from ..core.logging import get_logger
from ..corridor.bandwidth import BandResult, bandwidth, optimize_offsets
from ..corridor.delay_opt import optimize_offsets_delay
from ..corridor.meso import simulate
from ..timing.corridor_config import build_corridor
from ..timing.hcm import lane_group_delay, los
from ..timing.models import Corridor
from ..timing.utdf import write_utdf

log = get_logger(__name__)
SIDE_PHASES = (3, 4, 7, 8)


def _run(payload: dict[str, Any]) -> dict[str, Any]:
    corridor = Corridor.model_validate(payload["corridor"])
    res = simulate(corridor, mode=payload["mode"], offsets=payload["offsets"], seed=payload["seed"],
                   duration_s=payload["duration_s"], warmup_s=payload["warmup_s"])
    tr, mv = res.trips, res.movements
    out: dict[str, Any] = {"scenario": payload["scenario"], "seed": payload["seed"], "runtime_s": res.runtime_s}
    for d in ("EB", "WB"):
        sub = tr[tr["direction"] == d] if not tr.empty else tr
        out[f"{d}_trip_delay_s"] = float(sub["delay_s"].mean()) if len(sub) else math.nan
        out[f"{d}_stops"] = float(sub["stops"].mean()) if len(sub) else math.nan
        out[f"{d}_trips"] = int(len(sub))
    w = mv["served"].to_numpy(float)
    d = mv["mean_delay_s"].fillna(0).to_numpy(float)
    out["network_delay_s"] = float((w * d).sum() / w.sum()) if w.sum() else math.nan
    side = mv[mv["phase"].isin(SIDE_PHASES)]
    out["side_street_delay_s"] = float((side["served"] * side["mean_delay_s"]).sum() / side["served"].sum())
    left = mv[mv["phase"].isin((1, 5))]
    out["arterial_left_delay_s"] = float((left["served"] * left["mean_delay_s"]).sum() / left["served"].sum())
    coord = mv[mv["phase"].isin((2, 6))]
    out["arterial_arrivals_on_green"] = float((coord["served"] * coord["arrivals_on_green"]).sum() / coord["served"].sum())
    out["residual_queue"] = int(mv["residual_queue"].sum())
    out["movements"] = mv.to_dict("records")
    out["controllers"] = res.controllers.to_dict("records")
    return out


def run_study(config: str | Path, out_dir: str | Path, *, seeds: list[int], workers: int = 2,
              duration_s: float = 3900.0, warmup_s: float = 300.0) -> dict[str, Any]:
    corridor = build_corridor(config)
    out = Path(out_dir) / corridor.name
    out.mkdir(parents=True, exist_ok=True)
    existing = [i.timing.offset_s for i in corridor.intersections]
    band_existing = bandwidth(corridor, existing)
    band_opt = optimize_offsets(corridor)
    # delay-optimal offsets: tuned on seeds 1001/1002, evaluated below on the study seeds (out of sample)
    dopt = optimize_offsets_delay(corridor, mode="actuated", start=list(band_opt.offsets_s))
    band_delay = bandwidth(corridor, list(dopt.offsets_s))
    # recommended timing sheet back to Synchro
    rec_timings = {i.timing.int_id: i.timing.model_copy(update={"offset_s": o})
                   for i, o in zip(corridor.intersections, dopt.offsets_s)}
    vols = {i.timing.int_id: {p: a.volume_vph for p, a in i.approaches.items()} for i in corridor.intersections}
    write_utdf(rec_timings, out / "main_street_recommended.utdf.csv", vols)

    scenarios = {
        "existing_actuated": ("actuated", existing),
        "maxband_actuated": ("actuated", list(band_opt.offsets_s)),
        "mindelay_actuated": ("actuated", list(dopt.offsets_s)),
        "mindelay_fixed": ("fixed", list(dopt.offsets_s)),
    }
    payloads = [
        {"corridor": corridor.model_dump(mode="json"), "scenario": name, "mode": mode, "offsets": offs, "seed": s,
         "duration_s": duration_s, "warmup_s": warmup_s}
        for name, (mode, offs) in scenarios.items() for s in seeds
    ]
    workers = max(1, min(workers, os.cpu_count() or 1))
    if workers == 1:
        results = [_run(p) for p in payloads]
    else:
        with ProcessPoolExecutor(workers) as pool:
            results = list(pool.map(_run, payloads))
    per_seed = pd.DataFrame([{k: v for k, v in r.items() if k not in ("movements", "controllers")} for r in results])
    per_seed.to_csv(out / "per_seed_metrics.csv", index=False)
    movements = pd.DataFrame([dict(m, scenario=r["scenario"], seed=r["seed"]) for r in results for m in r["movements"]])
    movements.to_csv(out / "movements.csv", index=False)
    controllers = pd.DataFrame([dict(c, scenario=r["scenario"], seed=r["seed"]) for r in results for c in r["controllers"]])
    controllers.to_csv(out / "controllers.csv", index=False)

    summary = {
        "corridor": corridor.name, "seeds": seeds, "cycle_s": corridor.cycle_s,
        "speed_mps": corridor.speed_mps,
        "bandwidth": {
            "existing": {"offsets": list(band_existing.offsets_s), "eb_s": band_existing.eb_s, "wb_s": band_existing.wb_s,
                         "efficiency": band_existing.efficiency(corridor.cycle_s)},
            "maxband": {"offsets": list(band_opt.offsets_s), "eb_s": band_opt.eb_s, "wb_s": band_opt.wb_s,
                        "efficiency": band_opt.efficiency(corridor.cycle_s)},
            "mindelay": {"offsets": list(dopt.offsets_s), "eb_s": band_delay.eb_s, "wb_s": band_delay.wb_s,
                         "efficiency": band_delay.efficiency(corridor.cycle_s)},
        },
        "delay_optimizer": {"evaluations": dopt.evaluations, "objective_veh_h": dopt.objective_veh_h,
                            "history": [list(h[0]) + [h[1]] for h in dopt.history]},
        "metrics": {},
        "tests": {},
        "hcm_check": hcm_check(corridor, movements, controllers, "mindelay_actuated", list(dopt.offsets_s)),
    }
    metrics = ["EB_trip_delay_s", "WB_trip_delay_s", "EB_stops", "WB_stops", "network_delay_s",
               "side_street_delay_s", "arterial_left_delay_s", "arterial_arrivals_on_green"]
    for sc in scenarios:
        sub = per_seed[per_seed["scenario"] == sc]
        summary["metrics"][sc] = {}
        for m in metrics:
            x = sub[m].to_numpy(float)
            ci = float(stats.t.ppf(0.975, len(x) - 1) * x.std(ddof=1) / math.sqrt(len(x))) if len(x) > 1 else math.nan
            summary["metrics"][sc][m] = {"mean": float(x.mean()), "ci": ci}
        summary["metrics"][sc]["network_los"] = los(summary["metrics"][sc]["network_delay_s"]["mean"])
    base = per_seed[per_seed["scenario"] == "existing_actuated"]
    for sc in scenarios:
        if sc == "existing_actuated":
            continue
        alt = per_seed[per_seed["scenario"] == sc]
        summary["tests"][sc] = {}
        for m in metrics:
            a, b = base[m].to_numpy(float), alt[m].to_numpy(float)
            p = float(stats.ttest_rel(a, b).pvalue) if len(a) == len(b) and len(a) > 1 else math.nan  # common seeds
            summary["tests"][sc][m] = {"pct_change": 100.0 * (b.mean() - a.mean()) / a.mean() if a.mean() else math.nan,
                                       "p_paired": p}
    (out / "summary.json").write_text(json.dumps(summary, indent=2, default=float), encoding="utf-8")
    plot_time_space(corridor, [("Existing (simultaneous)", band_existing), ("Max bandwidth", band_opt),
                               ("Min delay (recommended)", band_delay)], out / "figures" / "time_space.png")
    return summary


def hcm_check(corridor: Corridor, movements: pd.DataFrame, controllers: pd.DataFrame, scenario: str,
              offsets: list[float]) -> list[dict[str, Any]]:
    """HCM 7 lane-group delay (Synchro-style) vs. simulated stop-line delay for the same timing."""
    rows = []
    mv = movements[movements["scenario"] == scenario].groupby(["int_id", "phase"]).mean(numeric_only=True)
    cs = controllers[controllers["scenario"] == scenario].groupby(["int_id", "phase"]).mean(numeric_only=True)
    for it in corridor.intersections:
        iid = it.timing.int_id
        for ph in (2, 6, 4, 8):
            if ph not in it.timing.phases or (iid, ph) not in mv.index:
                continue
            ap = it.approaches[ph]
            g_eff = float(cs.loc[(iid, ph), "avg_green_s"])
            p_green = float(mv.loc[(iid, ph), "arrivals_on_green"])
            vol = float(mv.loc[(iid, ph), "served_vph"])
            hcm = lane_group_delay(vol, ap.sat_flow_vphpl * ap.lanes, g_eff, corridor.cycle_s,
                                   arrivals_on_green=p_green if ph in (2, 6) else None)
            rows.append({"int_id": iid, "phase": ph, "volume_vph": vol, "avg_green_s": g_eff, "v_c": hcm.v_c,
                         "p_on_green": p_green, "hcm_delay_s": hcm.delay_s, "hcm_los": hcm.los,
                         "sim_delay_s": float(mv.loc[(iid, ph), "mean_delay_s"])})
    return rows


def plot_time_space(corridor: Corridor, plans: list[tuple[str, BandResult]], path: Path) -> Path:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    from ..timing.schedule import coord_green_windows

    c = corridor.cycle_s
    x = np.array([i.position_m for i in corridor.intersections])
    fig, axes = plt.subplots(1, len(plans), figsize=(6 * len(plans), 5), sharey=True, squeeze=False)
    axes = axes[0]
    for ax, (title, res) in zip(axes, plans):
        for k, it in enumerate(corridor.intersections):
            for ph, dy, col in ((2, -12, "#2b8a3e"), (6, 12, "#1c7ed6")):
                s, e = coord_green_windows(it.timing, ph)
                for cyc in range(-1, 4):
                    t0 = res.offsets_s[k] + cyc * c + s
                    ax.plot([t0, t0 + (e - s)], [x[k] + dy] * 2, color=col, lw=4, solid_capstyle="butt")
        tau = np.arange(0, c, 0.1)
        for eb, ph_col in ((True, "#2b8a3e"), (False, "#1c7ed6")):
            from ..corridor.bandwidth import _direction_masks

            m = _direction_masks(corridor, np.array(res.offsets_s), eb, tau).all(axis=0)
            if m.any():
                idx = np.flatnonzero(m)
                t_start, t_end = tau[idx[0]], tau[idx[-1]]
                travel = (x - x[0]) / corridor.speed_mps if eb else (x[-1] - x) / corridor.speed_mps
                for tt in (t_start, t_end):
                    ax.plot(tt + c + travel, x, color=ph_col, lw=1, ls="--")
        ax.set_title(f"{title}\nEB band {res.eb_s:.0f} s, WB band {res.wb_s:.0f} s (C = {c:.0f} s)", fontsize=10)
        ax.set_xlabel("time (s)")
        ax.set_xlim(0, 3 * c)
    from matplotlib.lines import Line2D

    axes[0].legend(handles=[Line2D([], [], color="#2b8a3e", lw=4, label="phi2 EB green"),
                            Line2D([], [], color="#1c7ed6", lw=4, label="phi6 WB green"),
                            Line2D([], [], color="k", lw=1, ls="--", label="band edges")],
                   loc="upper left", fontsize=8, framealpha=0.9)
    axes[0].set_ylabel("intersection (distance along arterial)")
    axes[0].set_yticks(x, [f"{i.timing.int_id}" for i in corridor.intersections])
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return path
