"""Milestone 6 production reliability tests (TripGraph v7):
1. AST firewall: src/tripgraph/api/ NEVER imports ortools in-process.
2. Event loop stays 100% unblocked while CP-SAT executes in subprocess.
3. Hung worker SIGKILL termination + pool self-healing.
4. Atomic SQLite commit fencing blocks aborted, expired, and stale-version writes.
5. FastAPI PATCH /trips/{id}/edit sync 200 OK + Monday closed-museum recovery.
6. FastAPI >50% dirty edit -> 202 Accepted + live SSE GlobalReplanMode stream.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import date, time
from pathlib import Path
import time as pytime

import httpx
import pytest

from tripgraph.api.main import create_app
from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC, FixtureGroundingProvider
from tripgraph.runtime.store import FencedTripStore, StaleVersionOrAbortedError
from tripgraph.runtime.supervisor import SolverProcessSupervisor, SolverTimeoutError
from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    HotelBookingContract,
    NodeTier,
    TripGraph,
)
from tripgraph.solver.ir import compile_dirty_ir
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

D_SUN = date(2026, 6, 14)
D_MON = date(2026, 6, 15)


def _tyo_utc(d: date, h: int, m: int = 0) -> int:
    reg = PinnedTzRegistry()
    res = resolve_local_to_utc(d, time(h, m), reg.get("Asia/Tokyo"))
    assert res.utc_epoch is not None
    return res.utc_epoch


def _sample_trip(trip_id: str = "trip_api_1") -> TripGraph:
    stay = CityStay(
        id="s_tyo",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_tyo",
            hotel_name="Roppongi Hotel",
            tz_iana="Asia/Tokyo",
            check_in_date=D_SUN,
            check_out_date=date(2026, 6, 17),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    acts = (
        # Day 1 (Sunday): stays frozen during Monday edit
        ActivityNode(
            id="a_sun_1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 10, 0), end_utc=_tyo_utc(D_SUN, 12, 0),
            duration_min=120, category="park", node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="a_sun_2", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
            local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 14, 0), end_utc=_tyo_utc(D_SUN, 15, 0),
            duration_min=60, category="cafe", node_tier=NodeTier.OPTIONAL,
        ),
        # Day 2 (Monday): Mori Art Museum is closed Monday; Meiji Shrine is locked!
        ActivityNode(
            id="a_mon_shrine", stay_id="s_tyo", city_id="tokyo", place_id="tyo_meiji_shrine",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 9, 0), end_utc=_tyo_utc(D_MON, 10, 30),
            duration_min=90, category="shrine", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="a_mon_art", stay_id="s_tyo", city_id="tokyo", place_id="tyo_mori_art",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 11, 30), end_utc=_tyo_utc(D_MON, 13, 30),
            duration_min=120, category="contemporary_art", node_tier=NodeTier.OPTIONAL,
        ),
    )
    return TripGraph(trip_id=trip_id, stays=(stay,), activities=acts)


def test_ast_api_layer_never_imports_ortools_in_process() -> None:
    """v7 Guard: FastAPI handlers must NEVER import ortools or invoke CpSolver directly."""
    api_dir = Path(__file__).resolve().parents[1] / "src" / "tripgraph" / "api"
    bad: list[str] = []
    for py_file in api_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if "ortools" in alias.name:
                        bad.append(f"{py_file}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom):
                if "ortools" in (node.module or ""):
                    bad.append(f"{py_file}:{node.lineno}")
    assert bad == [], f"API layer imported ortools in-process: {bad}"


@pytest.mark.asyncio
async def test_subprocess_supervisor_nonblocking_and_sigkill_self_healing() -> None:
    reg = PinnedTzRegistry()
    provider = FixtureGroundingProvider()
    graph = _sample_trip("t_sup")
    ir = compile_dirty_ir(
        graph,
        frozenset({("s_tyo", D_SUN)}),
        provider.all_places(),
        provider.transit_minutes,
        DEFAULT_FIXTURE_NOW_UTC,
        reg,
    )

    sup = SolverProcessSupervisor(max_workers=1)
    await sup.start()
    try:
        # 1. Non-blocking proof: async heartbeat ticks >= 5 times during a 0.20s subprocess work
        ticks = 0

        async def heartbeat() -> None:
            nonlocal ticks
            for _ in range(10):
                await asyncio.sleep(0.02)
                ticks += 1

        hb_task = asyncio.create_task(heartbeat())
        res_ok = await sup.solve(ir, hard_deadline_sec=2.0, test_hang_sec=0.20)
        await hb_task
        assert res_ok.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
        assert ticks >= 5

        # 2. Hung worker SIGKILL proof: 5s hang killed at 0.15s deadline
        t0 = pytime.perf_counter()
        with pytest.raises(SolverTimeoutError, match="SIGKILLed"):
            await sup.solve(ir, hard_deadline_sec=0.15, test_hang_sec=5.0)
        elapsed = pytime.perf_counter() - t0
        assert elapsed < 0.80  # Must NOT wait for the 5s hang!
        assert sup.kills_count >= 1

        # 3. Pool self-healed: fresh replacement worker immediately solves cleanly
        res_after = await sup.solve(ir, hard_deadline_sec=2.0)
        assert res_after.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    finally:
        await sup.shutdown()


def test_fenced_commit_blocks_aborted_expired_and_stale_version() -> None:
    store = FencedTripStore(":memory:")
    try:
        g = _sample_trip("t_fence")
        store.create_trip(g)
        now = pytime.time()

        # 1. Aborted request MUST be rejected
        store.register_request("req_aborted", "t_fence", now + 10.0)
        store.abort_request("req_aborted")
        with pytest.raises(StaleVersionOrAbortedError):
            store.commit_fenced(
                trip_id="t_fence", base_version=1, request_id="req_aborted", new_graph=g, now_utc=now
            )

        # 2. Expired deadline MUST be rejected
        store.register_request("req_expired", "t_fence", now - 1.0)
        with pytest.raises(StaleVersionOrAbortedError):
            store.commit_fenced(
                trip_id="t_fence", base_version=1, request_id="req_expired", new_graph=g, now_utc=now
            )

        # 3. Valid commit advances version 1 -> 2, then stale version=1 MUST be rejected
        store.register_request("req_ok", "t_fence", now + 10.0)
        store.register_request("req_stale", "t_fence", now + 10.0)
        saved = store.commit_fenced(
            trip_id="t_fence", base_version=1, request_id="req_ok", new_graph=g, now_utc=now
        )
        assert saved.version == 2

        with pytest.raises(StaleVersionOrAbortedError):
            store.commit_fenced(
                trip_id="t_fence", base_version=1, request_id="req_stale", new_graph=g, now_utc=now
            )
    finally:
        store.close()


@pytest.mark.asyncio
async def test_fastapi_sync_edit_recovers_closed_monday_with_zero_blast_radius() -> None:
    app = create_app(db_path=":memory:", max_solver_workers=1)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            trip = _sample_trip("t_http_sync")
            r_create = await client.post("/trips", json=trip.model_dump(mode="json"))
            assert r_create.status_code == 201
            assert r_create.json()["version"] == 1

            # Move a_mon_art on Monday (Mori Art is closed Monday -> S2_taxonomy swaps to open venue!)
            r_edit = await client.patch(
                "/trips/t_http_sync/edit",
                json={
                    "edit": {
                        "edit_type": "move_activity",
                        "activity_id": "a_mon_art",
                        "target_stay_id": "s_tyo",
                        "target_local_date": D_MON.isoformat(),
                        "target_start_utc": _tyo_utc(D_MON, 12, 0),
                    }
                },
            )
            assert r_edit.status_code == 200
            body = r_edit.json()
            assert body["status"] == "COMMITTED"
            assert body["version"] == 2
            assert body["recovery_mode"] == "S2_TAXONOMY_DETERMINISTIC"
            assert body["solver_certificate"] == "PROVEN_OPTIMAL_IN_SCOPE"
            assert body["verification_certificate"] == "VERIFIED_TRUE"
            # Untouched Sunday activities stayed SHA-256 frozen -> 0.0 blast radius!
            assert body["blast_radius"]["semantic"] == 0.0
            assert body["blast_radius"]["temporal"] == 0.0


@pytest.mark.asyncio
async def test_fastapi_over_50_percent_dirty_escalates_to_202_and_sse_completes() -> None:
    app = create_app(db_path=":memory:", max_solver_workers=1)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Create a clean all-open Sunday-only/Tuesday trip so shifting +1 day stays feasible
            stay = CityStay(
                id="s_tyo", city_id="tokyo", tz_iana="Asia/Tokyo",
                hotel=HotelBookingContract(
                    hotel_id="h_tyo", hotel_name="Hotel", tz_iana="Asia/Tokyo",
                    check_in_date=date(2026, 6, 12), check_out_date=date(2026, 6, 15),
                    guarantee_type="CARD_GUARANTEED", access_method="STAFFED_24H",
                ),
            )
            d_fri = date(2026, 6, 12)
            d_sat = date(2026, 6, 13)
            trip = TripGraph(
                trip_id="t_global",
                stays=(stay,),
                activities=(
                    ActivityNode(
                        id="a1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
                        local_date=d_fri, start_utc=_tyo_utc(d_fri, 10, 0), end_utc=_tyo_utc(d_fri, 12, 0),
                        duration_min=120, category="park",
                    ),
                    ActivityNode(
                        id="a2", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
                        local_date=d_sat, start_utc=_tyo_utc(d_sat, 11, 0), end_utc=_tyo_utc(d_sat, 12, 0),
                        duration_min=60, category="cafe",
                    ),
                ),
            )
            await client.post("/trips", json=trip.model_dump(mode="json"))

            # shift_start_date touches 100% of nodes (>50%) -> 202 Accepted!
            r_edit = await client.patch(
                "/trips/t_global/edit",
                json={"edit": {"edit_type": "shift_start_date", "delta_days": 1}},
            )
            assert r_edit.status_code == 202
            data = r_edit.json()
            assert data["mode"] == "GLOBAL_REPLAN_ESCALATED"
            events_url = data["events_url"]

            # Consume SSE stream until background 30s worker finishes
            r_sse = await client.get(events_url)
            assert r_sse.status_code == 200
            assert "completed" in r_sse.text

            # Verify committed version incremented to 2
            r_get = await client.get("/trips/t_global")
            assert r_get.status_code == 200
            assert r_get.json()["version"] == 2
