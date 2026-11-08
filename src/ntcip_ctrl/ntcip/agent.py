"""Asyncio SNMPv1 agent serving a live emulated controller, plus a client.

The agent runs the controller in real time (one tick per ``dt`` wall-clock seconds,
optionally accelerated) with Poisson detector calls, and answers GET / GETNEXT / SET on
UDP. Requests with the wrong community are silently dropped, as SNMPv1 agents do.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
from dataclasses import dataclass, field

import numpy as np

from ..core.exceptions import SNMPError
from ..core.logging import get_logger
from ..nema.controller import NemaController
from .ber import GET, GET_NEXT, NO_ERROR, NO_SUCH_NAME, RESPONSE, SET, Message, Value
from .mib import MibView

log = get_logger(__name__)


@dataclass
class DemandModel:
    """Per-phase Poisson arrival rates (veh/h) that generate detector calls."""

    vph: dict[int, float]
    seed: int = 7
    rng: np.random.Generator = field(init=False)
    queues: dict[int, int] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.rng = np.random.default_rng(self.seed)

    def tick(self, dt: float, greens: set[int]) -> tuple[set[int], set[int]]:
        act: set[int] = set()
        for p, rate in self.vph.items():
            if self.rng.random() < rate * dt / 3600.0:
                self.queues[p] = self.queues.get(p, 0) + 1
                act.add(p)
            if p in greens and self.queues.get(p, 0) and self.rng.random() < dt / 2.0:
                self.queues[p] -= 1
        calls = {p for p, q in self.queues.items() if q > 0} | act
        return calls, act | calls


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, agent: "SnmpAgent") -> None:
        self.agent = agent
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        reply = self.agent.handle(data)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)


class SnmpAgent:
    def __init__(self, controller: NemaController, *, community: bytes = b"public",
                 write_community: bytes = b"private", demand: DemandModel | None = None,
                 speedup: float = 1.0) -> None:
        self.controller = controller
        self.mib = MibView(controller)
        self.community = community
        self.write_community = write_community
        self.demand = demand
        self.speedup = speedup
        self.requests = 0
        self._transport: asyncio.DatagramTransport | None = None
        self._clock: asyncio.Task[None] | None = None

    def handle(self, data: bytes) -> bytes | None:
        try:
            msg = Message.decode(data)
        except SNMPError as exc:
            log.warning("snmp.malformed", extra={"error": str(exc)})
            return None
        if msg.version != 0:
            return None
        if msg.pdu_type == SET and msg.community != self.write_community:
            return None
        if msg.pdu_type != SET and msg.community not in (self.community, self.write_community):
            return None
        self.requests += 1
        out: list[tuple[tuple[int, ...], Value]] = []
        err, idx = NO_ERROR, 0
        for i, (oid, value) in enumerate(msg.varbinds, start=1):
            if msg.pdu_type == GET:
                e, v = self.mib.get(oid)
                out.append((oid, v))
            elif msg.pdu_type == GET_NEXT:
                nxt, e, v = self.mib.get_next(oid)
                out.append((nxt or oid, v))
            elif msg.pdu_type == SET:
                e = self.mib.set(oid, value)
                out.append((oid, value))
            else:
                e = NO_SUCH_NAME
                out.append((oid, None))
            if e != NO_ERROR and err == NO_ERROR:
                err, idx = e, i
        if err != NO_ERROR:  # SNMPv1: on error, echo the request varbinds
            out = list(msg.varbinds)
        return Message(RESPONSE, msg.request_id, out, msg.community, err, idx).encode()

    async def _run_clock(self) -> None:
        dt = self.controller.dt
        while True:
            calls, acts = self.demand.tick(dt, self.controller.greens()) if self.demand else (set(), set())
            self.controller.step(calls, acts)
            await asyncio.sleep(dt / self.speedup)

    async def start(self, host: str = "127.0.0.1", port: int = 1161) -> tuple[str, int]:
        loop = asyncio.get_running_loop()
        transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(self), local_addr=(host, port))
        self._transport = transport
        self._clock = asyncio.create_task(self._run_clock())
        addr = transport.get_extra_info("sockname")
        log.info("snmp.agent_started", extra={"host": addr[0], "port": addr[1]})
        return addr[0], addr[1]

    async def stop(self) -> None:
        if self._clock:
            self._clock.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._clock
        if self._transport:
            self._transport.close()


class SnmpClient:
    """Async SNMPv1 manager with timeout and retries."""

    _ids = itertools.count(1)

    def __init__(self, host: str, port: int, *, community: bytes = b"public", timeout_s: float = 0.5,
                 retries: int = 2) -> None:
        self.host, self.port = host, port
        self.community = community
        self.timeout_s = timeout_s
        self.retries = retries

    async def _request(self, pdu: int, varbinds: list[tuple[tuple[int, ...], Value]]) -> Message:
        loop = asyncio.get_running_loop()
        rid = next(self._ids) & 0x7FFFFFFF
        payload = Message(pdu, rid, varbinds, self.community).encode()
        for _attempt in range(self.retries + 1):
            fut: asyncio.Future[bytes] = loop.create_future()

            class _P(asyncio.DatagramProtocol):
                def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
                    if not fut.done():
                        fut.set_result(data)

            transport, _ = await loop.create_datagram_endpoint(_P, remote_addr=(self.host, self.port))
            try:
                transport.sendto(payload)
                data = await asyncio.wait_for(fut, self.timeout_s)
                msg = Message.decode(data)
                if msg.request_id == rid:
                    return msg
            except asyncio.TimeoutError:
                continue
            finally:
                transport.close()
        raise SNMPError("no response", context={"host": self.host, "port": self.port, "retries": self.retries})

    async def get(self, *oids: tuple[int, ...]) -> Message:
        return await self._request(GET, [(o, None) for o in oids])

    async def get_next(self, oid: tuple[int, ...]) -> Message:
        return await self._request(GET_NEXT, [(oid, None)])

    async def set(self, oid: tuple[int, ...], value: int) -> Message:
        return await self._request(SET, [(oid, value)])
