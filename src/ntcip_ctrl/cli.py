"""``ntcip-ctrl`` command line.

parse-utdf   print typed timings from a UTDF file
bandwidth    two-way bandwidth for the corridor's current (or given) offsets
optimize     optimise offsets and write a new UTDF file
simulate     one closed-loop corridor run
study        full retiming study (4 scenarios x N seeds) + report
agent        run an NTCIP 1202 SNMP agent for one intersection
snmp-get     query an agent (GET / GETNEXT walk of the status group)
serve        start the progression API
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .core.exceptions import ControllerError
from .core.logging import configure_logging


def _corridor(a: argparse.Namespace):  # type: ignore[no-untyped-def]
    from .timing.corridor_config import build_corridor

    return build_corridor(a.config, getattr(a, "utdf", None))


def cmd_parse(a: argparse.Namespace) -> int:
    from .timing.utdf import parse_utdf

    t, v = parse_utdf(Path(a.utdf))
    print(json.dumps({"timings": {k: x.model_dump(mode="json") for k, x in t.items()}, "volumes": v}, indent=2))
    return 0


def cmd_bandwidth(a: argparse.Namespace) -> int:
    from .corridor.bandwidth import bandwidth

    c = _corridor(a)
    r = bandwidth(c, a.offsets)
    print(json.dumps({"eb_s": r.eb_s, "wb_s": r.wb_s, "efficiency": r.efficiency(c.cycle_s), "offsets": r.offsets_s}))
    return 0


def cmd_optimize(a: argparse.Namespace) -> int:
    from .corridor.bandwidth import bandwidth, optimize_offsets
    from .timing.utdf import write_utdf

    c = _corridor(a)
    before = bandwidth(c)
    after = optimize_offsets(c, restarts=a.restarts)
    if a.write:
        write_utdf({i.timing.int_id: i.timing.model_copy(update={"offset_s": o})
                    for i, o in zip(c.intersections, after.offsets_s)}, a.write)
    print(json.dumps({"before": vars(before) | {"total": before.total_s}, "after": vars(after) | {"total": after.total_s}},
                     indent=2, default=float))
    return 0


def cmd_simulate(a: argparse.Namespace) -> int:
    from .corridor.meso import simulate

    c = _corridor(a)
    r = simulate(c, mode=a.mode, offsets=a.offsets, seed=a.seed, duration_s=a.duration, warmup_s=a.warmup)
    print(r.movements.to_string(index=False))
    if not r.trips.empty:
        print(r.trips.groupby("direction")[["delay_s", "stops"]].mean().to_string())
    print(f"runtime {r.runtime_s:.1f} s")
    return 0


def cmd_study(a: argparse.Namespace) -> int:
    from .services.report import write_report
    from .services.study import run_study

    summary = run_study(a.config, a.out, seeds=a.seeds, workers=a.workers, duration_s=a.duration, warmup_s=a.warmup)
    md = write_report(summary, Path(a.out) / summary["corridor"])
    print(md.read_text(encoding="utf-8"))
    return 0


def cmd_agent(a: argparse.Namespace) -> int:
    from .nema.controller import NemaController
    from .ntcip.agent import DemandModel, SnmpAgent

    c = _corridor(a)
    it = next(i for i in c.intersections if i.timing.int_id == a.int_id)
    ctrl = NemaController(it.timing, "actuated")

    async def main() -> None:
        agent = SnmpAgent(ctrl, demand=DemandModel({p: ap.volume_vph for p, ap in it.approaches.items()}),
                          speedup=a.speedup)
        host, port = await agent.start(a.host, a.port)
        print(f"NTCIP 1202 agent for intersection {a.int_id} on udp://{host}:{port} (Ctrl+C to stop)")
        try:
            await asyncio.Event().wait()
        finally:
            await agent.stop()

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    return 0


def cmd_snmp_get(a: argparse.Namespace) -> int:
    from .ntcip.agent import SnmpClient
    from .ntcip.mib import STATUS_ENTRY

    async def main() -> None:
        cl = SnmpClient(a.host, a.port)
        for _ in range(a.polls):
            m = await cl.get(STATUS_ENTRY + (4, 1), STATUS_ENTRY + (3, 1))
            g, y = (int(v) for _, v in m.varbinds)  # type: ignore[arg-type]
            print(f"greens={[p for p in range(1, 9) if g >> (p - 1) & 1]} yellows={[p for p in range(1, 9) if y >> (p - 1) & 1]}")
            await asyncio.sleep(a.interval)

    asyncio.run(main())
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from .api.app import create_app

    uvicorn.run(create_app(), host=a.host, port=a.port, log_level="warning")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ntcip-ctrl", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--log-level", default="WARNING")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("parse-utdf")
    s.add_argument("utdf")
    s.set_defaults(func=cmd_parse)

    for name, fn in (("bandwidth", cmd_bandwidth), ("optimize", cmd_optimize), ("simulate", cmd_simulate),
                     ("agent", cmd_agent)):
        s = sub.add_parser(name)
        s.add_argument("--config", default="configs/corridor.yaml")
        s.add_argument("--utdf")
        s.set_defaults(func=fn)
        if name in ("bandwidth", "simulate"):
            s.add_argument("--offsets", type=float, nargs="+")
        if name == "optimize":
            s.add_argument("--restarts", type=int, default=24)
            s.add_argument("--write")
        if name == "simulate":
            s.add_argument("--mode", choices=["fixed", "actuated"], default="actuated")
            s.add_argument("--seed", type=int, default=1)
            s.add_argument("--duration", type=float, default=3900.0)
            s.add_argument("--warmup", type=float, default=300.0)
        if name == "agent":
            s.add_argument("--int-id", type=int, default=101)
            s.add_argument("--host", default="127.0.0.1")
            s.add_argument("--port", type=int, default=1161)
            s.add_argument("--speedup", type=float, default=1.0)

    s = sub.add_parser("study")
    s.add_argument("--config", default="configs/corridor.yaml")
    s.add_argument("--out", default="results")
    s.add_argument("--seeds", type=int, nargs="+", default=[11, 23, 37, 41, 59, 67, 73, 89, 97, 101])
    s.add_argument("--workers", type=int, default=2)
    s.add_argument("--duration", type=float, default=3900.0)
    s.add_argument("--warmup", type=float, default=300.0)
    s.set_defaults(func=cmd_study)

    s = sub.add_parser("snmp-get")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=1161)
    s.add_argument("--polls", type=int, default=10)
    s.add_argument("--interval", type=float, default=0.5)
    s.set_defaults(func=cmd_snmp_get)

    s = sub.add_parser("serve")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    configure_logging(a.log_level, json_output=True)
    try:
        return int(a.func(a))
    except ControllerError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
