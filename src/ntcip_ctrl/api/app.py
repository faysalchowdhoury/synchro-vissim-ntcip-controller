"""Progression API.

``POST /v1/bandwidth``  corridor + offsets -> EB/WB band
``POST /v1/optimize``   corridor -> optimal offsets (result cached by request hash)
``POST /v1/utdf/parse`` raw UTDF text -> typed timings
"""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import __version__
from ..core.exceptions import ControllerError, UTDFParseError
from ..corridor.bandwidth import bandwidth, optimize_offsets
from ..timing.models import Corridor
from ..timing.utdf import parse_utdf


class BandwidthRequest(BaseModel):
    corridor: Corridor
    offsets_s: list[float] | None = None


class OptimizeRequest(BaseModel):
    corridor: Corridor
    restarts: int = Field(24, ge=0, le=200)
    step_s: float = Field(1.0, gt=0.0, le=10.0)


class BandResponse(BaseModel):
    eb_s: float
    wb_s: float
    efficiency: float
    offsets_s: list[float]


def create_app(cache_size: int = 256) -> FastAPI:
    app = FastAPI(title="NTCIP Controller / Progression API", version=__version__)
    cache: OrderedDict[str, BandResponse] = OrderedDict()

    @app.exception_handler(ControllerError)
    async def _err(_: Request, exc: ControllerError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"error": type(exc).__name__, "detail": str(exc)})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.post("/v1/bandwidth", response_model=BandResponse)
    async def bw(req: BandwidthRequest) -> BandResponse:
        if req.offsets_s is not None and len(req.offsets_s) != len(req.corridor.intersections):
            raise HTTPException(422, "one offset per intersection required")
        r = bandwidth(req.corridor, req.offsets_s)
        return BandResponse(eb_s=r.eb_s, wb_s=r.wb_s, efficiency=r.efficiency(req.corridor.cycle_s),
                            offsets_s=list(r.offsets_s))

    @app.post("/v1/optimize", response_model=BandResponse)
    async def opt(req: OptimizeRequest) -> BandResponse:
        key = hashlib.sha256(json.dumps(req.model_dump(mode="json"), sort_keys=True).encode()).hexdigest()
        if key in cache:
            cache.move_to_end(key)
            return cache[key]
        r = await run_in_threadpool(optimize_offsets, req.corridor, step_s=req.step_s, restarts=req.restarts)
        resp = BandResponse(eb_s=r.eb_s, wb_s=r.wb_s, efficiency=r.efficiency(req.corridor.cycle_s),
                            offsets_s=list(r.offsets_s))
        cache[key] = resp
        while len(cache) > cache_size:
            cache.popitem(last=False)
        return resp

    @app.post("/v1/utdf/parse")
    async def parse(request: Request) -> dict[str, object]:
        text = (await request.body()).decode("utf-8-sig", errors="replace")
        if "\n" not in text:
            raise UTDFParseError("body must be UTDF text")
        timings, volumes = parse_utdf(text)
        return {"timings": {k: v.model_dump(mode="json") for k, v in timings.items()}, "volumes": volumes}

    return app
