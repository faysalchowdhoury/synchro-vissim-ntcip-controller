"""Markdown report for the retiming study."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

LABELS = {
    "existing_actuated": "Existing (simultaneous), actuated-coord.",
    "maxband_actuated": "Max-bandwidth offsets, actuated-coord.",
    "mindelay_actuated": "Min-delay offsets, actuated-coord.",
    "mindelay_fixed": "Min-delay offsets, pretimed",
}
ROWS = (
    ("EB_trip_delay_s", "EB arterial delay per trip (s)", "{:.1f}"),
    ("WB_trip_delay_s", "WB arterial delay per trip (s)", "{:.1f}"),
    ("EB_stops", "EB stops per trip", "{:.2f}"),
    ("WB_stops", "WB stops per trip", "{:.2f}"),
    ("arterial_arrivals_on_green", "Arterial arrivals on green (share)", "{:.2f}"),
    ("side_street_delay_s", "Side-street delay (s/veh)", "{:.1f}"),
    ("arterial_left_delay_s", "Arterial left-turn delay (s/veh)", "{:.1f}"),
    ("network_delay_s", "All-movement stop-line delay (s/veh)", "{:.1f}"),
)


def _p(p: float) -> str:
    if p is None or math.isnan(p):
        return "-"
    return "<0.001" if p < 0.001 else f"{p:.3f}"


def write_report(summary: dict[str, Any], out_dir: Path) -> Path:
    sc = list(LABELS)
    bw = summary["bandwidth"]
    c = summary["cycle_s"]
    lines = [f"# Retiming study: `{summary['corridor']}`", ""]
    lines.append(f"Common cycle {c:.0f} s, progression speed {summary['speed_mps'] * 2.23694:.0f} mph, "
                 f"seeds {', '.join(map(str, summary['seeds']))} (common random numbers; paired t-tests vs. "
                 "existing actuated-coordinated operation). 1 h analysis after 5 min warm-up.")
    lines += ["", "## Progression bandwidth", "", "| Plan | Offsets (s) | EB band | WB band | Efficiency |", "|---|---|---|---|---|"]
    names = {"existing": "Existing (simultaneous)", "maxband": "Max bandwidth (MAXBAND-style)",
             "mindelay": "Min total delay (simulation-based)"}
    for k in ("existing", "maxband", "mindelay"):
        b = bw[k]
        lines.append(f"| {names[k]} | {', '.join(f'{o:.0f}' for o in b['offsets'])} | {b['eb_s']:.1f} s | {b['wb_s']:.1f} s | "
                     f"{100 * b['efficiency']:.0f}% |")
    do = summary["delay_optimizer"]
    lines += ["", f"Delay optimiser: {do['evaluations']} closed-loop evaluations on tuning seeds 1001-1002 "
              "(disjoint from the evaluation seeds below)."]
    lines += ["", "## Operations (mean ± 95% CI over seeds)", "",
              "| Metric | " + " | ".join(LABELS[s] for s in sc) + " |", "|" + "---|" * (len(sc) + 1)]
    for key, label, fmt in ROWS:
        cells = []
        for s in sc:
            m = summary["metrics"][s][key]
            cell = f"{fmt.format(m['mean'])} ± {fmt.format(m['ci'])}"
            if s != "existing_actuated":
                t = summary["tests"][s][key]
                cell += f" ({t['pct_change']:+.0f}%, p {_p(t['p_paired'])})"
            cells.append(cell)
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    lines.append("| All-movement LOS (stop-line delay) | " + " | ".join(summary["metrics"][s]["network_los"] for s in sc) + " |")
    lines += ["", "## HCM 7 cross-check (min-delay offsets, actuated-coordinated)", "",
              "HCM delay uses the simulated average green and, for coordinated phases, the simulated share of "
              "arrivals on green in the progression factor. Simulated delay is stop-line queue delay only "
              "(no acceleration/deceleration delay), so it should sit somewhat below HCM.", "",
              "| Int | Phase | v (vph) | avg green (s) | v/c | P on green | HCM delay (LOS) | Sim delay |", "|---|---|---|---|---|---|---|---|"]
    for r in summary["hcm_check"]:
        lines.append(f"| {r['int_id']} | {r['phase']} | {r['volume_vph']:.0f} | {r['avg_green_s']:.1f} | {r['v_c']:.2f} | "
                     f"{r['p_on_green']:.2f} | {r['hcm_delay_s']:.1f} ({r['hcm_los']}) | {r['sim_delay_s']:.1f} |")
    lines += ["", "![Time-space diagram](figures/time_space.png)", ""]
    md = out_dir / "RESULTS.md"
    md.write_text("\n".join(lines), encoding="utf-8")
    return md
