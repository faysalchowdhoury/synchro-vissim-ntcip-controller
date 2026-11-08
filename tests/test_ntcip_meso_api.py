from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from ntcip_ctrl.api.app import create_app
from ntcip_ctrl.core.exceptions import SNMPError
from ntcip_ctrl.corridor.meso import simulate
from ntcip_ctrl.nema.controller import NemaController
from ntcip_ctrl.ntcip.agent import SnmpAgent, SnmpClient
from ntcip_ctrl.ntcip.ber import BAD_VALUE, GET, NO_ERROR, NO_SUCH_NAME, READ_ONLY, Message, dec_oid, enc_oid
from ntcip_ctrl.ntcip.mib import PHASE_ENTRY, STATUS_ENTRY
from ntcip_ctrl.timing.models import Corridor

from .conftest import eight_phase


def test_ber_roundtrip() -> None:
    oid = (1, 3, 6, 1, 4, 1, 1206, 4, 2, 1, 1, 2, 1, 4, 300)
    assert dec_oid(enc_oid(oid)[2:]) == oid
    m = Message(GET, 123456, [(oid, None), (oid, -5), (oid, b"abc"), (oid, 70000)], b"public")
    m2 = Message.decode(m.encode())
    assert m2.varbinds == m.varbinds and m2.request_id == 123456 and m2.community == b"public"
    with pytest.raises(SNMPError):
        Message.decode(b"\x30\x05\x02\x01")


def test_agent_get_set_and_errors() -> None:
    async def scenario() -> None:
        ctrl = NemaController(eight_phase(), "actuated")
        agent = SnmpAgent(ctrl, speedup=50.0)
        host, port = await agent.start("127.0.0.1", 0)
        try:
            ro = SnmpClient(host, port)
            rw = SnmpClient(host, port, community=b"private")
            m = await ro.get(STATUS_ENTRY + (4, 1))
            assert m.error_status == NO_ERROR and m.varbinds[0][1] == (1 << 1) | (1 << 5)  # phases 2 and 6 green
            m = await ro.get(PHASE_ENTRY + (4, 3))
            assert m.varbinds[0][1] == 5
            m = await rw.set(PHASE_ENTRY + (4, 3), 6)
            assert m.error_status == NO_ERROR and ctrl.timing.phases[3].min_green_s == 6
            m = await rw.set(PHASE_ENTRY + (5, 3), 35)  # passage 3.5 s in tenths
            assert ctrl.timing.phases[3].passage_s == 3.5
            m = await rw.set(PHASE_ENTRY + (8, 2), 25)  # 2.5 s yellow violates 3-6 s
            assert m.error_status == BAD_VALUE
            m = await rw.set(STATUS_ENTRY + (4, 1), 0)
            assert m.error_status == READ_ONLY
            m = await ro.get(PHASE_ENTRY + (4, 9))
            assert m.error_status == NO_SUCH_NAME
            nxt = await ro.get_next(PHASE_ENTRY + (4,))
            assert nxt.varbinds[0][0] == PHASE_ENTRY + (4, 1)
            with pytest.raises(SNMPError):  # read community may not SET: agent drops it
                await SnmpClient(host, port, timeout_s=0.1, retries=0).set(PHASE_ENTRY + (4, 3), 7)
            assert agent.requests >= 8
        finally:
            await agent.stop()

    asyncio.run(scenario())


def test_meso_runs_and_offsets_matter(corridor: Corridor) -> None:
    good = simulate(corridor, mode="actuated", offsets=[0, 23, 46, 76], seed=3, duration_s=1500, warmup_s=300)
    assert not good.trips.empty and set(good.trips["direction"]) == {"EB", "WB"}
    assert (good.trips["delay_s"] >= 0).all()
    served = good.movements.set_index(["int_id", "phase"])["served_vph"]
    assert served.loc[(101, 4)] == pytest.approx(300, rel=0.35)
    again = simulate(corridor, mode="actuated", offsets=[0, 23, 46, 76], seed=3, duration_s=1500, warmup_s=300)
    assert good.trips["delay_s"].sum() == again.trips["delay_s"].sum()  # deterministic per seed


def test_delay_optimizer_never_worsens_start(corridor: Corridor) -> None:
    from ntcip_ctrl.corridor.delay_opt import optimize_offsets_delay, total_delay_veh_h

    start = [0.0, 0.0, 0.0, 0.0]
    kw = dict(mode="actuated", seeds=[5], duration_s=700.0, warmup_s=100.0)
    base = total_delay_veh_h(corridor, start, **kw)  # type: ignore[arg-type]
    res = optimize_offsets_delay(corridor, start=start, step_s=25.0, sweeps=1, seeds=[5], duration_s=700.0,
                                 warmup_s=100.0)
    assert res.objective_veh_h <= base + 1e-9
    assert res.evaluations == 1 + 3 * 3
    assert res.offsets_s[0] == 0.0


def test_api(corridor: Corridor) -> None:
    c = TestClient(create_app())
    body = {"corridor": corridor.model_dump(mode="json")}
    r = c.post("/v1/bandwidth", json=body)
    assert r.status_code == 200 and r.json()["eb_s"] >= 0
    r1 = c.post("/v1/optimize", json={**body, "restarts": 4})
    r2 = c.post("/v1/optimize", json={**body, "restarts": 4})
    assert r1.json() == r2.json() and r1.json()["eb_s"] + r1.json()["wb_s"] > r.json()["eb_s"] + r.json()["wb_s"]
    assert c.post("/v1/bandwidth", json={**body, "offsets_s": [0]}).status_code == 422
    text = open(__import__("pathlib").Path(__file__).resolve().parents[1] / "examples" / "main_street_existing.utdf.csv").read()
    assert set(c.post("/v1/utdf/parse", content=text).json()["timings"]) == {"101", "102", "103", "104"}
