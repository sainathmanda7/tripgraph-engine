"""Sprint 1 P0 Hardening Regression Tests (TripGraph v7).

Enforces all 5 red-team acceptance criteria + per-policy UNKNOWN commit gate:
1. Fresh-process API import NEVER loads ortools (no direct OR transitive leak).
2. Phase B solves (primary, pool fallback, outage preview) execute strictly via
   SolverProcessSupervisor subprocesses, never in-process.
3. GlobalReplanMode enforces BOTH FreezeSnapshot/LockGuard and independent K3
   verification + VerificationPolicy before every commit.
4. ALL_LOCKED_VERIFY_ONLY enforces LockGuard, enforces K3/VerificationPolicy,
   and emits honest NOT_INVOKED_VERIFY_ONLY certificates (never PROVEN_OPTIMAL).
5. worker_main.py arms prctl(PR_SET_PDEATHSIG) BEFORE importing ortools, fails
   loudly on non-zero return / OSError, and checks expected_ppid after arming.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import date, time
from pathlib import Path
import subprocess
import sys

import pytest

from tripgraph.grounding.chaos import ChaosConfig, ChaosDriftMiddleware
from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC, FixtureGroundingProvider
from tripgraph.repair.phase_b import (
    LLMCallMetrics,
    LLMOutageError,
    SearchIntentLLMOutput,
    SemanticRankLLMOutput,
)
from tripgraph.runtime import worker_main
from tripgraph.runtime.orchestrator import TripOrchestrator
from tripgraph.runtime.store import FencedTripStore
from tripgraph.runtime.supervisor import SolverProcessSupervisor
from tripgraph.schema.edits import MoveActivityEdit, ShiftStartDateEdit, UpdateDietaryEdit
from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    HotelBookingContract,
    NodeTier,
    TripGraph,
    VerificationPolicy,
)
from tripgraph.solver.types import DualSolveResult, SolvedActivityAssignment
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

D_SUN = date(2026, 6, 14)
D_MON = date(2026, 6, 15)


def _tyo_utc(d: date, h: int, m: int = 0) -> int:
    res = resolve_local_to_utc(d, time(h, m), PinnedTzRegistry().get("Asia/Tokyo"))
    assert res.utc_epoch is not None
    return res.utc_epoch


def _tokyo_trip(trip_id: str, *, lock_shrine: bool = True, lock_all: bool = False) -> TripGraph:
    stay = CityStay(
        id="s_tyo",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
        locked_by_user=lock_all,
        hotel=HotelBookingContract(
            hotel_id="h_tyo",
            hotel_name="Roppongi Central",
            tz_iana="Asia/Tokyo",
            check_in_date=D_SUN,
            check_out_date=date(2026, 6, 18),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    acts = (
        ActivityNode(
            id="a_sun_park", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 10, 0), end_utc=_tyo_utc(D_SUN, 12, 0),
            duration_min=120, category="park", locked_by_user=lock_all, node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="a_mon_shrine", stay_id="s_tyo", city_id="tokyo", place_id="tyo_meiji_shrine",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 9, 0), end_utc=_tyo_utc(D_MON, 10, 30),
            duration_min=90, category="shrine", locked_by_user=(lock_shrine or lock_all),
            node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="a_mon_art", stay_id="s_tyo", city_id="tokyo", place_id="tyo_mori_art",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 11, 30), end_utc=_tyo_utc(D_MON, 13, 30),
            duration_min=120, category="contemporary_art", locked_by_user=lock_all,
            node_tier=NodeTier.OPTIONAL,
        ),
    )
    return TripGraph(trip_id=trip_id, stays=(stay,), activities=acts)


def test_fresh_process_api_import_never_loads_ortools_transitively() -> None:
    """AC1: Importing tripgraph.api.main in a fresh Python process must NOT load ortools."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, tripgraph.api.main; "
                "loaded = [m for m in sys.modules if m == 'ortools' or m.startswith('ortools.')]; "
                "assert not loaded, f'ortools leaked into API process: {loaded}'; "
                "print('CLEAN_NO_ORTOOLS')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"STDOUT={proc.stdout} STDERR={proc.stderr}"
    assert "CLEAN_NO_ORTOOLS" in proc.stdout

    src_root = Path(__file__).resolve().parents[1] / "src" / "tripgraph"
    forbidden: list[str] = []
    for sub in ("api", "runtime", "repair"):
        for py_file in (src_root / sub).rglob("*.py"):
            if py_file.name == "worker_main.py":
                continue
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if "ortools" in alias.name or "cpsat_engine" in alias.name:
                            forbidden.append(f"{py_file.name}:{node.lineno}:{alias.name}")
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    if "ortools" in mod or "cpsat_engine" in mod:
                        forbidden.append(f"{py_file.name}:{node.lineno}:{mod}")
    assert forbidden == [], f"Forbidden solver imports outside worker_main: {forbidden}"


class _TestPhaseBClient:
    def __init__(self, *, fail_outage: bool = False, bad_rank_only: bool = False) -> None:
        self.fail_outage = fail_outage
        self.bad_rank_only = bad_rank_only

    def generate_search_intent(self, **kwargs):
        if self.fail_outage:
            raise LLMOutageError("Simulated 503 LLM outage")
        return (
            SearchIntentLLMOutput(target_categories=("design_museum", "contemporary_art", "museum")),
            LLMCallMetrics(prompt_tokens=80, completion_tokens=20, latency_ms=5.0),
        )

    def rank_candidates(self, **kwargs):
        if self.fail_outage:
            raise LLMOutageError("Simulated 503 LLM outage")
        if self.bad_rank_only:
            return (
                SemanticRankLLMOutput(ranked_place_ids=("tyo_HALLUCINATED_ONLY",)),
                LLMCallMetrics(prompt_tokens=90, completion_tokens=15, latency_ms=5.0),
            )
        return (
            SemanticRankLLMOutput(ranked_place_ids=("tyo_2121_design",)),
            LLMCallMetrics(prompt_tokens=90, completion_tokens=15, latency_ms=5.0),
        )


@pytest.mark.asyncio
async def test_phase_b_executes_all_solves_via_supervisor_subprocess() -> None:
    """AC2: Phase B primary, fallback, and outage preview solves all route through supervisor."""
    for label, client, expected_status in (
        ("primary", _TestPhaseBClient(), 200),
        ("pool_fallback", _TestPhaseBClient(bad_rank_only=True), 200),
        ("outage_preview", _TestPhaseBClient(fail_outage=True), 503),
    ):
        store = FencedTripStore(":memory:")
        sup = SolverProcessSupervisor(max_workers=1)
        await sup.start()
        solve_calls = 0
        orig_solve = sup.solve

        async def _counting_solve(*args, **kwargs):
            nonlocal solve_calls
            solve_calls += 1
            return await orig_solve(*args, **kwargs)

        sup.solve = _counting_solve  # type: ignore[method-assign]
        try:
            orch = TripOrchestrator(
                store=store,
                supervisor=sup,
                provider=FixtureGroundingProvider(),
                tz_registry=PinnedTzRegistry(),
                now_utc=DEFAULT_FIXTURE_NOW_UTC,
                llm_client=client,
            )
            store.create_trip(_tokyo_trip(f"t_pb_{label}"))
            out = await orch.apply_edit(
                trip_id=f"t_pb_{label}",
                edit=MoveActivityEdit(
                    activity_id="a_mon_art",
                    target_stay_id="s_tyo",
                    target_local_date=D_MON,
                    target_start_utc=_tyo_utc(D_MON, 12, 0),
                ),
            )
            assert out.http_status == expected_status, f"{label}: got {out}"
            assert solve_calls >= 2, f"{label}: expected >=2 supervisor solves, got {solve_calls}"
        finally:
            await sup.shutdown()
            store.close()


@pytest.mark.asyncio
async def test_global_replan_blocks_locked_mutation_and_invalid_candidate() -> None:
    """AC3: GlobalReplanMode MUST enforce LockGuard and independent K3 verifier before commit."""
    store = FencedTripStore(":memory:")
    sup = SolverProcessSupervisor(max_workers=1)
    await sup.start()
    try:
        orch = TripOrchestrator(
            store=store,
            supervisor=sup,
            provider=FixtureGroundingProvider(),
            tz_registry=PinnedTzRegistry(),
            now_utc=DEFAULT_FIXTURE_NOW_UTC,
        )
        base = _tokyo_trip("t_global_locked", lock_shrine=True)
        store.create_trip(base)

        out = await orch.apply_edit(
            trip_id="t_global_locked",
            edit=ShiftStartDateEdit(delta_days=2),
        )
        assert out.http_status == 202
        job = orch.jobs[out.payload["job_id"]]
        await asyncio.wait_for(job.done_event.wait(), timeout=10.0)

        assert job.status == "FAILED"
        assert job.final_payload is not None
        assert job.final_payload["error"] == "LOCK_GUARD_VIOLATION"
        saved = store.get_trip("t_global_locked")
        assert saved is not None and saved.version == 1
        shrine = next(a for a in saved.graph.activities if a.id == "a_mon_shrine")
        assert shrine.local_date == D_MON
    finally:
        await sup.shutdown()
        store.close()

    store2 = FencedTripStore(":memory:")
    sup2 = SolverProcessSupervisor(max_workers=1)
    await sup2.start()
    try:
        unlocked = TripGraph(
            trip_id="t_global_bad_verify",
            stays=(_tokyo_trip("tmp", lock_shrine=False).stays[0],),
            activities=(
                ActivityNode(
                    id="a_open", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
                    local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 10, 0), end_utc=_tyo_utc(D_SUN, 12, 0),
                    duration_min=120, category="park",
                ),
            ),
        )
        store2.create_trip(unlocked)

        async def _tampered_solve(ir, **kwargs) -> DualSolveResult:
            return DualSolveResult(
                certificate="PROVEN_OPTIMAL_IN_SCOPE",
                assignments={
                    "a_open": SolvedActivityAssignment(
                        act_id="a_open",
                        present=True,
                        place_id="tyo_mori_art",
                        start_utc=_tyo_utc(D_MON, 11, 0),
                        end_utc=_tyo_utc(D_MON, 13, 0),
                        changed=True,
                    )
                },
                day_orders={("s_tyo", D_MON.isoformat()): ("a_open",)},
            )

        sup2.solve = _tampered_solve  # type: ignore[method-assign]
        orch2 = TripOrchestrator(
            store=store2,
            supervisor=sup2,
            provider=FixtureGroundingProvider(),
            tz_registry=PinnedTzRegistry(),
            now_utc=DEFAULT_FIXTURE_NOW_UTC,
        )
        out2 = await orch2.apply_edit(
            trip_id="t_global_bad_verify",
            edit=ShiftStartDateEdit(delta_days=1),
        )
        assert out2.http_status == 202
        job2 = orch2.jobs[out2.payload["job_id"]]
        await asyncio.wait_for(job2.done_event.wait(), timeout=10.0)

        assert job2.status == "FAILED"
        assert job2.final_payload is not None
        assert job2.final_payload["error"] == "INDEPENDENT_VERIFIER_REJECTED_SOLVED_GRAPH"
        saved2 = store2.get_trip("t_global_bad_verify")
        assert saved2 is not None and saved2.version == 1
    finally:
        await sup2.shutdown()
        store2.close()


@pytest.mark.asyncio
async def test_all_locked_verify_only_enforces_lock_guard_policy_gate_and_honest_certificate() -> None:
    """AC3 & AC4: ALL_LOCKED_VERIFY_ONLY blocks locked mutation, enforces UNKNOWN policy, never fakes OPTIMAL."""
    store = FencedTripStore(":memory:")
    sup = SolverProcessSupervisor(max_workers=1)
    await sup.start()
    try:
        orch = TripOrchestrator(
            store=store,
            supervisor=sup,
            provider=FixtureGroundingProvider(),
            tz_registry=PinnedTzRegistry(),
            now_utc=DEFAULT_FIXTURE_NOW_UTC,
        )
        stay = CityStay(
            id="s_tyo", city_id="tokyo", tz_iana="Asia/Tokyo", locked_by_user=True,
            hotel=HotelBookingContract(
                hotel_id="h_tyo", hotel_name="Roppongi Central", tz_iana="Asia/Tokyo",
                check_in_date=D_SUN, check_out_date=date(2026, 6, 17),
                guarantee_type="CARD_GUARANTEED", access_method="STAFFED_24H",
            ),
        )
        all_locked = TripGraph(
            trip_id="t_all_locked",
            stays=(stay,),
            activities=(
                ActivityNode(
                    id="a_lock_1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
                    local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 10, 0), end_utc=_tyo_utc(D_SUN, 12, 0),
                    duration_min=120, category="park", locked_by_user=True, node_tier=NodeTier.CRITICAL,
                ),
            ),
        )
        store.create_trip(all_locked)

        # 1. Trying to move a locked activity MUST be rejected by LockGuard (409)
        bad_out = await orch.apply_edit(
            trip_id="t_all_locked",
            edit=MoveActivityEdit(
                activity_id="a_lock_1",
                target_stay_id="s_tyo",
                target_local_date=D_SUN,
                target_start_utc=_tyo_utc(D_SUN, 13, 0),
            ),
        )
        assert bad_out.http_status == 409
        assert bad_out.payload["status"] == "LOCK_GUARD_VIOLATION"
        assert store.get_trip("t_all_locked").version == 1  # type: ignore[union-attr]

        # 2. Valid non-node-mutating edit commits with honest NOT_INVOKED_VERIFY_ONLY certificate
        ok_out = await orch.apply_edit(
            trip_id="t_all_locked",
            edit=UpdateDietaryEdit(required_dietary_tags=("vegan",)),
            verification_policy=VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY,
        )
        assert ok_out.http_status == 200
        assert ok_out.payload["scope_tier"] == "ALL_LOCKED_VERIFY_ONLY"
        assert ok_out.payload["solver_certificate"] == "NOT_INVOKED_VERIFY_ONLY"
        assert ok_out.payload["solver_certificate"] != "PROVEN_OPTIMAL_IN_SCOPE"
        assert store.get_trip("t_all_locked").version == 2  # type: ignore[union-attr]

        # 3. Conflicting frontier evidence (UNKNOWN) MUST be blocked by policy commit gate
        chaos_provider = ChaosDriftMiddleware(
            FixtureGroundingProvider(),
            ChaosConfig(inject_conflicting_closed_for=frozenset({"tyo_yoyogi_park"})),
        )
        orch_chaos = TripOrchestrator(
            store=store,
            supervisor=sup,
            provider=chaos_provider,
            tz_registry=PinnedTzRegistry(),
            now_utc=DEFAULT_FIXTURE_NOW_UTC,
        )
        unk_out = await orch_chaos.apply_edit(
            trip_id="t_all_locked",
            edit=UpdateDietaryEdit(required_dietary_tags=("vegetarian",)),
            verification_policy=VerificationPolicy.ALLOW_UNVERIFIED_HOLIDAYS,
        )
        assert unk_out.http_status == 409
        assert unk_out.payload["solver_certificate"] == "NOT_INVOKED_VERIFY_ONLY"
        assert "CONFLICTING_EVIDENCE_ON_FRONTIER" in unk_out.payload["disallowed_assumptions"]
        assert store.get_trip("t_all_locked").version == 2  # type: ignore[union-attr]
    finally:
        await sup.shutdown()
        store.close()


def test_worker_pdeathsig_fails_loudly_and_imports_solver_lazily() -> None:
    """AC5: worker_main never imports ortools at top-level and never swallows prctl/ppid failures."""
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, tripgraph.runtime.worker_main; "
                "loaded = [m for m in sys.modules if m == 'ortools' or m.startswith('ortools.')]; "
                "assert not loaded, f'worker_main eagerly imported ortools before prctl: {loaded}'"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, f"STDERR={proc.stderr}"

    class _FailPrctlLibc:
        def prctl(self, *args):
            return -1

    with pytest.raises(RuntimeError, match="prctl.*failed"):
        worker_main._install_linux_pdeathsig(
            expected_ppid=1234, _platform="linux", _libc=_FailPrctlLibc(), _getppid=lambda: 1234
        )

    class _OSErrorLibc:
        def prctl(self, *args):
            raise OSError("libc failure")

    with pytest.raises(OSError, match="libc failure"):
        worker_main._install_linux_pdeathsig(
            expected_ppid=1234, _platform="linux", _libc=_OSErrorLibc(), _getppid=lambda: 1234
        )

    class _OkLibc:
        def prctl(self, *args):
            return 0

    with pytest.raises(RuntimeError, match="Parent process exited"):
        worker_main._install_linux_pdeathsig(
            expected_ppid=1234, _platform="linux", _libc=_OkLibc(), _getppid=lambda: 1
        )

    worker_main._install_linux_pdeathsig(
        expected_ppid=1234, _platform="linux", _libc=_OkLibc(), _getppid=lambda: 1234
    )
