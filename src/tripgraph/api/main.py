"""FastAPI service for TripGraph v7 (Zero in-process CP-SAT execution)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from typing import AsyncGenerator

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from tripgraph.grounding.fixtures import FixtureGroundingProvider
from tripgraph.runtime.orchestrator import TripOrchestrator
from tripgraph.runtime.store import FencedTripStore
from tripgraph.runtime.supervisor import SolverProcessSupervisor
from tripgraph.schema.edits import EditCommand
from tripgraph.schema.models import TripGraph, VerificationPolicy


class EditRequestBody(BaseModel):
    model_config = ConfigDict(frozen=True)

    edit: EditCommand
    verification_policy: VerificationPolicy = VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY
    allow_auto_drop_optional: bool = False


def create_app(
    *,
    db_path: str = ":memory:",
    max_solver_workers: int | None = None,
) -> FastAPI:
    if max_solver_workers is None:
        default_workers = 1 if os.getenv("RENDER", "").lower() == "true" else 2
        max_solver_workers = int(os.getenv("TRIPGRAPH_MAX_WORKERS", str(default_workers)))
    store = FencedTripStore(db_path=db_path)
    supervisor = SolverProcessSupervisor(max_workers=max_solver_workers)
    provider = FixtureGroundingProvider()
    orchestrator = TripOrchestrator(
        store=store,
        supervisor=supervisor,
        provider=provider,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        await supervisor.start()
        try:
            yield
        finally:
            await supervisor.shutdown()
            store.close()

    app = FastAPI(
        title="TripGraph v7 — Neuro-Symbolic Itinerary Engine",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.store = store
    app.state.supervisor = supervisor
    app.state.orchestrator = orchestrator

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "engine": "tripgraph-v7"}

    @app.post("/trips", status_code=201)
    async def create_trip(graph: TripGraph) -> dict:
        if store.get_trip(graph.trip_id) is not None:
            raise HTTPException(status_code=409, detail="Trip ID already exists.")
        saved = store.create_trip(graph)
        return {"trip_id": saved.graph.trip_id, "version": saved.version}

    @app.get("/trips/{trip_id}")
    async def get_trip(trip_id: str) -> dict:
        committed = store.get_trip(trip_id)
        if committed is None:
            raise HTTPException(status_code=404, detail="Trip not found.")
        return {
            "trip_id": trip_id,
            "version": committed.version,
            "graph": committed.graph.model_dump(mode="json"),
        }

    @app.post("/trips/{trip_id}/verify")
    async def verify_trip(trip_id: str) -> dict:
        committed = store.get_trip(trip_id)
        if committed is None:
            raise HTTPException(status_code=404, detail="Trip not found.")
        rep = orchestrator.verify_only(committed.graph)
        return {
            "hard_constraints_status": rep.hard_constraints_status.value,
            "verification_certificate": rep.verification_certificate,
            "violations": [
                {"check_id": v.check_id, "code": v.code, "detail": v.detail}
                for v in rep.violations
            ],
            "assumptions_accepted": [
                {"check_id": a.check_id, "code": a.code, "detail": a.detail}
                for a in rep.assumptions_accepted
            ],
        }

    @app.patch("/trips/{trip_id}/edit")
    async def edit_trip(trip_id: str, body: EditRequestBody) -> JSONResponse:
        outcome = await orchestrator.apply_edit(
            trip_id=trip_id,
            edit=body.edit,
            verification_policy=body.verification_policy,
            allow_auto_drop_optional=body.allow_auto_drop_optional,
        )
        return JSONResponse(status_code=outcome.http_status, content=outcome.payload)

    @app.get("/jobs/{job_id}/events")
    async def job_events(job_id: str) -> StreamingResponse:
        job = orchestrator.jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found.")

        async def _stream() -> AsyncGenerator[str, None]:
            sent = 0
            while True:
                while sent < len(job.events):
                    ev = job.events[sent]
                    sent += 1
                    yield f"data: {json.dumps(ev)}\n\n"
                if job.done_event.is_set() and sent >= len(job.events):
                    break
                await asyncio.sleep(0.02)

        return StreamingResponse(_stream(), media_type="text/event-stream")

    return app


app = create_app()
