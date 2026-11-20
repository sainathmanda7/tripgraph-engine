"""Milestone 5 verification tests:
- BudgetedProviderProxy equal-budget enforcement
- S2_taxonomy deterministic ontology recovery (0 LLM tokens)
- S2_greedy vs CP-SAT coupled locked-window contrast
- Tool-mediated Phase B hallucination firewall & free-text intent ranking
- Consent-gated LLM outage safety (never silently drops user activities)
- Tier B ChaosDriftMiddleware (ETag drift, 429 outage, conflicting hours)
- Tier C OSM opening_hours parser, OSRM rail guard, and GTFS CSV parser
"""

from __future__ import annotations

from datetime import date, time

import pytest

from tripgraph.grounding.chaos import ChaosConfig, ChaosDriftMiddleware
from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC, FixtureGroundingProvider
from tripgraph.grounding.live_adapters import (
    OsrmDemoAdapter,
    StaticGtfsTimetableAdapter,
    parse_osm_opening_hours,
)
from tripgraph.grounding.provider import (
    BudgetedProviderProxy,
    CandidateSearchIntent,
    ETagDriftError,
    ProviderOutageError,
    RetrievalBudgetExceededError,
)
from tripgraph.repair.phase_b import (
    LLMCallMetrics,
    LLMOutageError,
    SearchIntentLLMOutput,
    SemanticRankLLMOutput,
    run_phase_b_cycle,
)
from tripgraph.repair.taxonomy import (
    expand_candidates_deterministic,
    solve_ir_greedy,
    taxonomy_controls_sha256,
)
from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    EvidenceVerdict,
    HotelBookingContract,
    NodeTier,
    Place,
    TransitMode,
    TripGraph,
    TruthValue,
)
from tripgraph.solver.cpsat_engine import solve_ir
from tripgraph.solver.ir import compile_dirty_ir
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc
from tripgraph.verifier.evidence import resolve_place_availability

D_MON = date(2026, 6, 15)  # Monday in Tokyo


def _tyo_utc(d: date, h: int, m: int = 0) -> int:
    reg = PinnedTzRegistry()
    res = resolve_local_to_utc(d, time(h, m), reg.get("Asia/Tokyo"))
    assert res.utc_epoch is not None
    return res.utc_epoch


def _tokyo_monday_closed_trip() -> tuple[TripGraph, FixtureGroundingProvider]:
    provider = FixtureGroundingProvider()
    stay = CityStay(
        id="s_tyo",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_tyo",
            hotel_name="Roppongi Hotel",
            tz_iana="Asia/Tokyo",
            check_in_date=date(2026, 6, 14),
            check_out_date=date(2026, 6, 17),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    # Mori Art Museum is CLOSED on Monday (D_MON)!
    acts = (
        ActivityNode(
            id="act_shrine", stay_id="s_tyo", city_id="tokyo", place_id="tyo_meiji_shrine",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 8, 30), end_utc=_tyo_utc(D_MON, 10, 0),
            duration_min=90, category="shrine", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="act_art", stay_id="s_tyo", city_id="tokyo", place_id="tyo_mori_art",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 11, 0), end_utc=_tyo_utc(D_MON, 13, 0),
            duration_min=120, category="contemporary_art", locked_by_user=False,
            node_tier=NodeTier.OPTIONAL,
        ),
    )
    graph = TripGraph(
        trip_id="t_mon_closed",
        stays=(stay,),
        activities=acts,
        user_intent_notes="Prefer quiet, stroller-friendly indoor minimalist craft spots; avoid crowded tourist halls.",
    )
    return graph, provider


class _FakePhaseBClient:
    """Deterministic test double that also injects a hallucinated ID to test the firewall."""

    def __init__(self, *, fail_outage: bool = False, include_hallucination: bool = True) -> None:
        self.fail_outage = fail_outage
        self.include_hallucination = include_hallucination

    def generate_search_intent(self, *, target_act, core_groups, user_intent_notes, timeout_sec=1.4):
        if self.fail_outage:
            raise LLMOutageError("Simulated 503 LLM outage")
        return (
            SearchIntentLLMOutput(
                target_categories=("design_museum", "craft_gallery", "art_gallery"),
                preferred_tags=("quiet", "stroller_friendly", "minimalist_craft", "indoor"),
                avoid_tags=("crowded_tourist",),
                rationale="Broaden closed Monday museum to quiet stroller-friendly minimalist craft/design spaces.",
            ),
            LLMCallMetrics(prompt_tokens=120, completion_tokens=45, latency_ms=210.0),
        )

    def rank_candidates(self, *, target_act, candidates, user_intent_notes, max_select=5, timeout_sec=1.4):
        if self.fail_outage:
            raise LLMOutageError("Simulated 503 LLM outage")
        ranked = [p.place_id for p in candidates if "quiet" in p.tags][:3]
        if self.include_hallucination:
            ranked = ["tyo_HALLUCINATED_MUSEUM_999", *ranked]
        return (
            SemanticRankLLMOutput(ranked_place_ids=tuple(ranked[:max_select]), rationale="Ranked quiet craft venues."),
            LLMCallMetrics(prompt_tokens=210, completion_tokens=35, latency_ms=190.0),
        )


def test_budgeted_provider_proxy_enforces_hard_query_and_injection_caps() -> None:
    provider = FixtureGroundingProvider()
    proxy = BudgetedProviderProxy(provider, max_provider_queries=1, max_candidates_fetched=20, max_candidates_to_cpsat=5)
    intent = CandidateSearchIntent(city_id="tokyo", target_categories=("art_gallery", "design_museum"))

    first = proxy.search_candidates(intent)
    assert len(first) >= 2
    assert proxy.telemetry.provider_queries == 1

    with pytest.raises(RetrievalBudgetExceededError, match="max_provider_queries"):
        proxy.search_candidates(intent)

    with pytest.raises(RetrievalBudgetExceededError, match="max_candidates_to_cpsat"):
        proxy.record_cpsat_injection(["a", "b", "c", "d", "e", "f"])


def test_s2_taxonomy_solves_monday_closed_museum_at_zero_llm_tokens() -> None:
    reg = PinnedTzRegistry()
    graph, provider = _tokyo_monday_closed_trip()
    places = provider.all_places()
    dirty_days = frozenset({("s_tyo", D_MON)})

    # Phase A alone hits INFEASIBLE (Mori Art Museum is closed Monday)
    ir_a = compile_dirty_ir(graph, dirty_days, places, provider.transit_minutes, DEFAULT_FIXTURE_NOW_UTC, reg)
    res_a = solve_ir(ir_a)
    assert res_a.certificate == "PROVEN_PHYSICALLY_INFEASIBLE"

    # S2_taxonomy expands deterministically through BudgetedProviderProxy (0 LLM tokens)
    proxy = BudgetedProviderProxy(provider)
    target_act = next(a for a in graph.activities if a.id == "act_art")
    cands = expand_candidates_deterministic(
        target_act, graph, proxy, places, DEFAULT_FIXTURE_NOW_UTC, reg
    )
    assert len(cands) >= 2
    assert proxy.telemetry.provider_queries == 1

    # Inject S2_taxonomy candidates into CP-SAT -> PROVEN_OPTIMAL_IN_SCOPE!
    ir_tax = compile_dirty_ir(
        graph, dirty_days, places, provider.transit_minutes, DEFAULT_FIXTURE_NOW_UTC, reg,
        extra_candidates_by_act={"act_art": cands},
    )
    res_tax = solve_ir(ir_tax)
    assert res_tax.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    assert res_tax.assignments["act_art"].place_id in ("tyo_2121_design", "tyo_national_art", "tyo_mingeikan")
    assert len(taxonomy_controls_sha256()) == 64


def test_s2_greedy_trapped_by_locked_window_while_cpsat_solves_globally() -> None:
    """Classic non-CP-SAT greedy trap:
    act_flex originally at 09:00-11:00 (2h), act_lock pinned at 10:00-11:00 (1h).
    Greedy processes act_flex first in orig-start order -> puts act_flex at 09:00-11:00
    -> collides with locked 10:00-11:00 and fails!
    Exact CP-SAT slides act_flex AFTER act_lock (11:20-13:20) and proves OPTIMAL."""
    reg = PinnedTzRegistry()
    provider = FixtureGroundingProvider()
    places = provider.all_places()
    stay = CityStay(
        id="s_tyo", city_id="tokyo", tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_tyo", hotel_name="Hotel", tz_iana="Asia/Tokyo",
            check_in_date=date(2026, 6, 14), check_out_date=date(2026, 6, 17),
            guarantee_type="CARD_GUARANTEED", access_method="STAFFED_24H",
        ),
    )
    acts = (
        ActivityNode(
            id="act_flex", stay_id="s_tyo", city_id="tokyo", place_id="tyo_teamlab",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 9, 0), end_utc=_tyo_utc(D_MON, 11, 0),
            duration_min=120, category="interactive_art", locked_by_user=False, node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="act_lock", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 10, 0), end_utc=_tyo_utc(D_MON, 11, 0),
            duration_min=60, category="cafe", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
    )
    graph = TripGraph(trip_id="t_greedy_trap", stays=(stay,), activities=acts)
    ir = compile_dirty_ir(
        graph, frozenset({("s_tyo", D_MON)}), places, provider.transit_minutes, DEFAULT_FIXTURE_NOW_UTC, reg
    )

    greedy_res = solve_ir_greedy(ir)
    assert greedy_res.feasible is False
    assert greedy_res.failure_reason == "GREEDY_TRAPPED_AT:act_lock"

    cpsat_res = solve_ir(ir)
    assert cpsat_res.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    assert cpsat_res.day_orders[("s_tyo", D_MON.isoformat())] == ("act_lock", "act_flex")


def test_phase_b_filters_hallucinations_avoids_crowded_venues_and_proves_via_cpsat() -> None:
    reg = PinnedTzRegistry()
    graph, provider = _tokyo_monday_closed_trip()
    places = provider.all_places()
    dirty_days = frozenset({("s_tyo", D_MON)})
    target_act = next(a for a in graph.activities if a.id == "act_art")

    ir_a = compile_dirty_ir(graph, dirty_days, places, provider.transit_minutes, DEFAULT_FIXTURE_NOW_UTC, reg)
    res_a = solve_ir(ir_a)

    proxy = BudgetedProviderProxy(provider)
    pb_res = run_phase_b_cycle(
        graph=graph,
        dirty_days=dirty_days,
        target_act=target_act,
        initial_core=res_a.core_groups,
        proxy=proxy,
        place_registry=places,
        now_utc=DEFAULT_FIXTURE_NOW_UTC,
        tz_registry=reg,
        llm_client=_FakePhaseBClient(include_hallucination=True),
    )

    assert pb_res.status == "SOLVED_BY_PHASE_B"
    assert pb_res.rejected_hallucinated_ids == ("tyo_HALLUCINATED_MUSEUM_999",)
    assert pb_res.llm_calls_made == 2
    assert proxy.telemetry.provider_queries == 1
    assert pb_res.solve_result is not None
    assert pb_res.solve_result.certificate == "PROVEN_OPTIMAL_IN_SCOPE"
    chosen = pb_res.solve_result.assignments["act_art"].place_id
    # Avoided crowded_tourist National Art Center; chose quiet minimalist space!
    assert chosen in ("tyo_2121_design", "tyo_mingeikan")


def test_consent_gated_llm_outage_never_silently_drops_user_activity() -> None:
    reg = PinnedTzRegistry()
    graph, provider = _tokyo_monday_closed_trip()
    places = provider.all_places()
    dirty_days = frozenset({("s_tyo", D_MON)})
    target_act = next(a for a in graph.activities if a.id == "act_art")

    # Default allow_auto_drop_optional=False -> MUST NOT commit drop!
    proxy1 = BudgetedProviderProxy(provider)
    res_default = run_phase_b_cycle(
        graph=graph, dirty_days=dirty_days, target_act=target_act, initial_core=(),
        proxy=proxy1, place_registry=places, now_utc=DEFAULT_FIXTURE_NOW_UTC, tz_registry=reg,
        llm_client=_FakePhaseBClient(fail_outage=True),
        allow_auto_drop_optional=False,
    )
    assert res_default.status == "RETRYABLE_UPSTREAM_LLM_UNAVAILABLE"
    assert res_default.solve_result is None
    assert res_default.suggested_fallback_preview is not None
    assert res_default.suggested_fallback_preview.assignments["act_art"].present is False

    # Explicit opt-in allow_auto_drop_optional=True -> commits DEGRADED_OPTIONAL_DROPPED
    proxy2 = BudgetedProviderProxy(provider)
    res_optin = run_phase_b_cycle(
        graph=graph, dirty_days=dirty_days, target_act=target_act, initial_core=(),
        proxy=proxy2, place_registry=places, now_utc=DEFAULT_FIXTURE_NOW_UTC, tz_registry=reg,
        llm_client=_FakePhaseBClient(fail_outage=True),
        allow_auto_drop_optional=True,
    )
    assert res_optin.status == "DEGRADED_OPTIONAL_DROPPED"
    assert res_optin.solve_result is not None
    assert res_optin.solve_result.assignments["act_art"].present is False


def test_tier_b_chaos_middleware_etag_drift_429_and_supplier_conflict() -> None:
    reg = PinnedTzRegistry()
    base = FixtureGroundingProvider()

    # 1. HTTP 429 injection
    chaos_429 = ChaosDriftMiddleware(base, ChaosConfig(fail_search_with_429=True))
    with pytest.raises(ProviderOutageError, match="429"):
        chaos_429.search_candidates(CandidateSearchIntent(city_id="tokyo", target_categories=("park",)))

    # 2. Optimistic ETag drift between search and commit verification
    chaos_etag = ChaosDriftMiddleware(
        base, ChaosConfig(drift_etag_on_second_read=frozenset({"tyo_2121_design"}))
    )
    proxy = BudgetedProviderProxy(chaos_etag)
    proxy.search_candidates(CandidateSearchIntent(city_id="tokyo", target_categories=("design_museum",)))
    with pytest.raises(ETagDriftError, match="tyo_2121_design"):
        proxy.verify_etags_unchanged()

    # 3. Conflicting peer supplier hours -> resolves to UNKNOWN (CONFLICTING_EVIDENCE_ON_FRONTIER)
    chaos_conf = ChaosDriftMiddleware(
        base, ChaosConfig(inject_conflicting_closed_for=frozenset({"tyo_2121_design"}))
    )
    rec = chaos_conf.get_place("tyo_2121_design")
    assert rec is not None
    avail = resolve_place_availability(
        rec.payload, _tyo_utc(D_MON, 11, 0), _tyo_utc(D_MON, 13, 0), DEFAULT_FIXTURE_NOW_UTC, reg
    )
    assert avail.truth == TruthValue.UNKNOWN
    assert avail.epistemic_code == "CONFLICTING_EVIDENCE_ON_FRONTIER"


def test_tier_c_osm_opening_hours_osrm_rail_guard_and_gtfs_parser() -> None:
    reg = PinnedTzRegistry()

    # 1. Real OSM opening_hours parsing (closed Monday, overnight Fri-Sat, 24/7, unparseable quarantine)
    ev1 = parse_osm_opening_hours(
        "Mo off; Tu-Su 10:00-18:00", place_id="p_osm_1", fetched_at_utc=DEFAULT_FIXTURE_NOW_UTC
    )
    assert ev1.verdict == EvidenceVerdict.OPEN
    assert len(ev1.rules) == 6
    assert all(r.weekday != 0 for r in ev1.rules)

    ev_night = parse_osm_opening_hours(
        "Fr-Sa 18:00-02:00", place_id="p_osm_bar", fetched_at_utc=DEFAULT_FIXTURE_NOW_UTC
    )
    assert ev_night.verdict == EvidenceVerdict.OPEN
    assert all(r.close_day_offset == 1 for r in ev_night.rules)

    ev_bad = parse_osm_opening_hours(
        "sunrise-sunset; PH off", place_id="p_osm_weird", fetched_at_utc=DEFAULT_FIXTURE_NOW_UTC
    )
    assert ev_bad.verdict == EvidenceVerdict.UNPARSEABLE

    # 2. OSRM demo adapter explicitly rejects RAIL
    osrm = OsrmDemoAdapter()
    with pytest.raises(ValueError, match="WALK and DRIVE only"):
        osrm.route_minutes(35.68, 139.76, 34.98, 135.75, TransitMode.RAIL)

    # 3. Real GTFS CSV timetable parsing
    sample_gtfs = """trip_id,arrival_time,departure_time,stop_id,stop_sequence
NOZOMI_1,07:00:00,07:00:00,TYO,1
NOZOMI_1,09:15:00,09:15:00,KYO,2
NOZOMI_2,09:30:00,09:30:00,TYO,1
NOZOMI_2,11:45:00,11:45:00,KYO,2
"""
    gtfs = StaticGtfsTimetableAdapter(stop_times_csv=sample_gtfs, tz_iana="Asia/Tokyo")
    slots = gtfs.get_slots("TYO", "KYO", D_MON, reg)
    assert len(slots) == 2
    assert slots[0].slot_id == "gtfs_NOZOMI_1_2026-06-15"
    assert (slots[0].arrive_utc - slots[0].depart_utc) == 135 * 60
