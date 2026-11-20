"""Stratified benchmark scenario generator (TripGraph v7).

Produces:
- Disjoint 18-scenario dev pilot (for power estimation & control tuning)
- 116-scenario held-out evaluation suite:
    * Regime 1  (24 cases): Shiftable temporal cascades (S2_cpsat solves at 0 LLM tokens)
    * Regime 2A (18 cases): Structured category closures (S2_taxonomy vs S3 parity check)
    * Regime 2B (50 cases): Free-text / compositional multi-facet intent (50 cases to
                            ensure >= 35 non-tie pairs at <=30% attrition for >=80% power)
    * Regime 3  (24 cases): Hard-lock / genuinely infeasible conflicts (honest 409 refusal)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time
from typing import Literal

from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC
from tripgraph.schema.edits import (
    EditCommand,
    ExtendStayEdit,
    MoveActivityEdit,
    UpdateDietaryEdit,
)
from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    HotelBookingContract,
    NodeTier,
    TripConstraints,
    TripGraph,
    VerificationPolicy,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

Regime = Literal["R1_SHIFTABLE", "R2A_STRUCTURED_CLOSURE", "R2B_FREETEXT_INTENT", "R3_HARD_LOCK_UNSAT"]

D_SUN = date(2026, 6, 14)
D_MON = date(2026, 6, 15)
D_TUE = date(2026, 6, 16)


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario_id: str
    split: Literal["pilot", "held_out"]
    regime: Regime
    edit_class: str
    graph: TripGraph
    edit: EditCommand
    expected_physically_solvable: bool
    verification_policy: VerificationPolicy = VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY
    # For R2B deterministic structural checks (preferred vs avoided vibe tags)
    target_preferred_tags: tuple[str, ...] = ()
    target_avoid_tags: tuple[str, ...] = ()


def _utc(d: date, h: int, m: int, tz_iana: str = "Asia/Tokyo") -> int:
    reg = PinnedTzRegistry()
    res = resolve_local_to_utc(d, time(h, m), reg.get(tz_iana))
    assert res.utc_epoch is not None
    return res.utc_epoch


def _tokyo_stay() -> CityStay:
    return CityStay(
        id="s_tyo",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
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


def _kyoto_stay() -> CityStay:
    return CityStay(
        id="s_kyo",
        city_id="kyoto",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_kyo",
            hotel_name="Okazaki Inn",
            tz_iana="Asia/Tokyo",
            check_in_date=D_SUN,
            check_out_date=date(2026, 6, 18),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )


def _make_r1_case(idx: int, split: Literal["pilot", "held_out"]) -> BenchmarkScenario:
    """Regime 1: Coupled locked-window & temporal shift scenarios (solvable by CP-SAT, 0 LLM)."""
    # Alternate greedy-trap cases (where flex collides with locked) and smooth shifts
    greedy_trap = (idx % 2 == 0)
    stay = _tokyo_stay()
    frozen_sun = (
        ActivityNode(
            id="f_sun_1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_utc(D_SUN, 10, 0), end_utc=_utc(D_SUN, 12, 0),
            duration_min=120, category="park", node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="f_sun_2", stay_id="s_tyo", city_id="tokyo", place_id="tyo_t_s_tantan",
            local_date=D_SUN, start_utc=_utc(D_SUN, 13, 0), end_utc=_utc(D_SUN, 14, 0),
            duration_min=60, category="food", is_meal=True, node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="f_tue_1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_ain_soph",
            local_date=D_TUE, start_utc=_utc(D_TUE, 12, 0), end_utc=_utc(D_TUE, 13, 30),
            duration_min=90, category="food", is_meal=True, node_tier=NodeTier.ESSENTIAL,
        ),
    )
    if greedy_trap:
        # Flex opens at 09:00 (teamLab, 2h) colliding with locked coffee at 10:00-11:00 -> traps greedy!
        lock_h = 10 + (idx % 3)
        mon_acts = (
            ActivityNode(
                id="a_flex", stay_id="s_tyo", city_id="tokyo", place_id="tyo_teamlab",
                local_date=D_MON, start_utc=_utc(D_MON, 9, 0), end_utc=_utc(D_MON, 11, 0),
                duration_min=120, category="interactive_art", node_tier=NodeTier.ESSENTIAL,
            ),
            ActivityNode(
                id="a_lock", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
                local_date=D_MON, start_utc=_utc(D_MON, lock_h, 0), end_utc=_utc(D_MON, lock_h + 1, 0),
                duration_min=60, category="cafe", locked_by_user=True, node_tier=NodeTier.CRITICAL,
            ),
        )
        edit_start = _utc(D_MON, 9, 0)
    else:
        shift_h = 11 + (idx % 4)
        mon_acts = (
            ActivityNode(
                id="a_flex", stay_id="s_tyo", city_id="tokyo", place_id="tyo_2121_design",
                local_date=D_MON, start_utc=_utc(D_MON, shift_h, 0), end_utc=_utc(D_MON, shift_h + 2, 0),
                duration_min=120, category="design_museum", node_tier=NodeTier.ESSENTIAL,
            ),
            ActivityNode(
                id="a_lock", stay_id="s_tyo", city_id="tokyo", place_id="tyo_meiji_shrine",
                local_date=D_MON, start_utc=_utc(D_MON, 8, 30), end_utc=_utc(D_MON, 9, 30),
                duration_min=60, category="shrine", locked_by_user=True, node_tier=NodeTier.CRITICAL,
            ),
        )
        edit_start = _utc(D_MON, shift_h, 30)

    g = TripGraph(trip_id=f"{split}_r1_{idx:02d}", stays=(stay,), activities=frozen_sun + mon_acts)
    edit = MoveActivityEdit(
        activity_id="a_flex", target_stay_id="s_tyo", target_local_date=D_MON, target_start_utc=edit_start
    )
    return BenchmarkScenario(
        scenario_id=f"{split}_R1_{idx:02d}",
        split=split,
        regime="R1_SHIFTABLE",
        edit_class="move_activity",
        graph=g,
        edit=edit,
        expected_physically_solvable=True,
    )


def _make_r2a_case(idx: int, split: Literal["pilot", "held_out"]) -> BenchmarkScenario:
    """Regime 2A: Structured Monday closures or dietary edits (S2_taxonomy should match S3)."""
    use_kyoto = (idx % 2 == 1)
    stay = _kyoto_stay() if use_kyoto else _tokyo_stay()
    city = "kyoto" if use_kyoto else "tokyo"
    sid = stay.id

    if idx % 3 == 2:
        # Dietary update forcing non-vegan restaurant swap
        meal_pid = "kyo_nishiki" if use_kyoto else "tyo_tsukiji_hall"
        frozen = (
            ActivityNode(
                id="f1", stay_id=sid, city_id=city,
                place_id="kyo_philosopher" if use_kyoto else "tyo_yoyogi_park",
                local_date=D_SUN, start_utc=_utc(D_SUN, 10, 0), end_utc=_utc(D_SUN, 12, 0),
                duration_min=120, category="park",
            ),
            ActivityNode(
                id="f2", stay_id=sid, city_id=city,
                place_id="kyo_ippodo" if use_kyoto else "tyo_koffee_mameya",
                local_date=D_TUE, start_utc=_utc(D_TUE, 14, 0), end_utc=_utc(D_TUE, 15, 0),
                duration_min=60, category="cafe",
            ),
        )
        mon = (
            ActivityNode(
                id="a_meal", stay_id=sid, city_id=city, place_id=meal_pid,
                local_date=D_MON, start_utc=_utc(D_MON, 12, 0), end_utc=_utc(D_MON, 13, 0),
                duration_min=60, category="food", is_meal=True, node_tier=NodeTier.ESSENTIAL,
            ),
        )
        g = TripGraph(trip_id=f"{split}_r2a_{idx:02d}", stays=(stay,), activities=frozen + mon)
        edit: EditCommand = UpdateDietaryEdit(required_dietary_tags=("vegan",))
        e_cls = "update_dietary"
    else:
        # Monday closed museum (Mori Art in Tokyo or KYOCERA in Kyoto)
        closed_pid = "kyo_kyocera_art" if use_kyoto else "tyo_mori_art"
        frozen = (
            ActivityNode(
                id="f1", stay_id=sid, city_id=city,
                place_id="kyo_fushimi" if use_kyoto else "tyo_meiji_shrine",
                local_date=D_SUN, start_utc=_utc(D_SUN, 9, 0), end_utc=_utc(D_SUN, 11, 0),
                duration_min=120, category="shrine",
            ),
            ActivityNode(
                id="f2", stay_id=sid, city_id=city,
                place_id="kyo_philosopher" if use_kyoto else "tyo_yoyogi_park",
                local_date=D_TUE, start_utc=_utc(D_TUE, 10, 0), end_utc=_utc(D_TUE, 12, 0),
                duration_min=120, category="park",
            ),
        )
        mon = (
            ActivityNode(
                id="a_closed", stay_id=sid, city_id=city, place_id=closed_pid,
                local_date=D_MON, start_utc=_utc(D_MON, 11, 0), end_utc=_utc(D_MON, 13, 0),
                duration_min=120, category="contemporary_art", node_tier=NodeTier.ESSENTIAL,
            ),
        )
        g = TripGraph(trip_id=f"{split}_r2a_{idx:02d}", stays=(stay,), activities=frozen + mon)
        edit = MoveActivityEdit(
            activity_id="a_closed", target_stay_id=sid, target_local_date=D_MON,
            target_start_utc=_utc(D_MON, 11, 0),
        )
        e_cls = "move_activity"

    return BenchmarkScenario(
        scenario_id=f"{split}_R2A_{idx:02d}",
        split=split,
        regime="R2A_STRUCTURED_CLOSURE",
        edit_class=e_cls,
        graph=g,
        edit=edit,
        expected_physically_solvable=True,
    )


_R2B_INTENT_PROFILES = (
    (
        "Quiet, stroller-friendly indoor minimalist craft backup after a long morning shrine walk; avoid loud crowded tourist halls.",
        ("quiet", "stroller_friendly", "minimalist_craft", "indoor"),
        ("crowded_tourist", "loud"),
    ),
    (
        "Relaxed indoor architecture and craft experience with low crowds for traveling with a toddler.",
        ("indoor", "quiet", "stroller_friendly"),
        ("crowded_tourist",),
    ),
    (
        "Calm, contemplative Japanese design and folk-craft atmosphere away from mainstream tourist crowds.",
        ("minimalist_craft", "quiet", "indoor"),
        ("crowded_tourist", "loud"),
    ),
)


def _make_r2b_case(idx: int, split: Literal["pilot", "held_out"]) -> BenchmarkScenario:
    """Regime 2B: Free-text compositional multi-facet intent (50 held-out cases)."""
    profile_text, pref_tags, avoid_tags = _R2B_INTENT_PROFILES[idx % len(_R2B_INTENT_PROFILES)]
    use_kyoto = (idx % 3 == 2)
    stay = _kyoto_stay() if use_kyoto else _tokyo_stay()
    city = "kyoto" if use_kyoto else "tokyo"
    sid = stay.id
    closed_pid = "kyo_kyocera_art" if use_kyoto else "tyo_mori_art"
    shrine_pid = "kyo_fushimi" if use_kyoto else "tyo_meiji_shrine"

    frozen = (
        ActivityNode(
            id="f1", stay_id=sid, city_id=city,
            place_id="kyo_philosopher" if use_kyoto else "tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_utc(D_SUN, 10, 0), end_utc=_utc(D_SUN, 12, 0),
            duration_min=120, category="park",
        ),
        ActivityNode(
            id="f2", stay_id=sid, city_id=city,
            place_id="kyo_mumokuteki" if use_kyoto else "tyo_ain_soph",
            local_date=D_TUE, start_utc=_utc(D_TUE, 12, 0), end_utc=_utc(D_TUE, 13, 30),
            duration_min=90, category="food", is_meal=True,
        ),
    )
    start_h = 11 + (idx % 3)
    mon = (
        ActivityNode(
            id="a_shrine_lock", stay_id=sid, city_id=city, place_id=shrine_pid,
            local_date=D_MON, start_utc=_utc(D_MON, 8, 0), end_utc=_utc(D_MON, 9, 30),
            duration_min=90, category="shrine", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="a_art_closed", stay_id=sid, city_id=city, place_id=closed_pid,
            local_date=D_MON, start_utc=_utc(D_MON, start_h, 0), end_utc=_utc(D_MON, start_h + 2, 0),
            duration_min=120, category="contemporary_art", node_tier=NodeTier.ESSENTIAL,
        ),
    )
    g = TripGraph(
        trip_id=f"{split}_r2b_{idx:02d}",
        stays=(stay,),
        activities=frozen + mon,
        user_intent_notes=profile_text,
    )
    edit = MoveActivityEdit(
        activity_id="a_art_closed",
        target_stay_id=sid,
        target_local_date=D_MON,
        target_start_utc=_utc(D_MON, start_h, 0),
    )
    return BenchmarkScenario(
        scenario_id=f"{split}_R2B_{idx:02d}",
        split=split,
        regime="R2B_FREETEXT_INTENT",
        edit_class="move_activity",
        graph=g,
        edit=edit,
        expected_physically_solvable=True,
        target_preferred_tags=pref_tags,
        target_avoid_tags=avoid_tags,
    )


def _make_r3_case(idx: int, split: Literal["pilot", "held_out"]) -> BenchmarkScenario:
    """Regime 3: Hard-lock / genuinely infeasible conflicts (honest 409 refusal)."""
    stay = _tokyo_stay()
    frozen = (
        ActivityNode(
            id="f1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_utc(D_SUN, 10, 0), end_utc=_utc(D_SUN, 12, 0),
            duration_min=120, category="park",
        ),
        ActivityNode(
            id="f2", stay_id="s_tyo", city_id="tokyo", place_id="tyo_ain_soph",
            local_date=D_TUE, start_utc=_utc(D_TUE, 12, 0), end_utc=_utc(D_TUE, 13, 0),
            duration_min=60, category="food", is_meal=True,
        ),
    )
    if idx % 2 == 0:
        # Two locked activities overlapping at the exact same slot -> provably UNSAT!
        mon = (
            ActivityNode(
                id="a_lock1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_2121_design",
                local_date=D_MON, start_utc=_utc(D_MON, 11, 0), end_utc=_utc(D_MON, 13, 0),
                duration_min=120, category="design_museum", locked_by_user=True, node_tier=NodeTier.CRITICAL,
            ),
            ActivityNode(
                id="a_lock2", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
                local_date=D_MON, start_utc=_utc(D_MON, 11, 30), end_utc=_utc(D_MON, 12, 30),
                duration_min=60, category="cafe", locked_by_user=True, node_tier=NodeTier.CRITICAL,
            ),
        )
        constraints = TripConstraints()
    else:
        # Locked closed Monday museum -> cannot substitute or drop a user lock!
        mon = (
            ActivityNode(
                id="a_lock1", stay_id="s_tyo", city_id="tokyo", place_id="tyo_mori_art",
                local_date=D_MON, start_utc=_utc(D_MON, 11, 0), end_utc=_utc(D_MON, 13, 0),
                duration_min=120, category="contemporary_art", locked_by_user=True, node_tier=NodeTier.CRITICAL,
            ),
            ActivityNode(
                id="a_lock2", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
                local_date=D_MON, start_utc=_utc(D_MON, 15, 0), end_utc=_utc(D_MON, 16, 0),
                duration_min=60, category="cafe", locked_by_user=False, node_tier=NodeTier.OPTIONAL,
            ),
        )
        constraints = TripConstraints()

    g = TripGraph(
        trip_id=f"{split}_r3_{idx:02d}",
        stays=(stay,),
        activities=frozen + mon,
        constraints=constraints,
    )
    edit = MoveActivityEdit(
        activity_id="a_lock2", target_stay_id="s_tyo", target_local_date=D_MON,
        target_start_utc=mon[1].start_utc,
    )
    return BenchmarkScenario(
        scenario_id=f"{split}_R3_{idx:02d}",
        split=split,
        regime="R3_HARD_LOCK_UNSAT",
        edit_class="move_activity",
        graph=g,
        edit=edit,
        expected_physically_solvable=False,
    )


def build_pilot_scenarios() -> list[BenchmarkScenario]:
    """18 disjoint dev-pilot scenarios (4 R1, 4 R2A, 6 R2B, 4 R3)."""
    return [
        *(_make_r1_case(i, "pilot") for i in range(4)),
        *(_make_r2a_case(i, "pilot") for i in range(4)),
        *(_make_r2b_case(i, "pilot") for i in range(6)),
        *(_make_r3_case(i, "pilot") for i in range(4)),
    ]


def build_held_out_scenarios() -> list[BenchmarkScenario]:
    """116 held-out scenarios (24 R1, 18 R2A, 50 R2B, 24 R3)."""
    return [
        *(_make_r1_case(i, "held_out") for i in range(24)),
        *(_make_r2a_case(i, "held_out") for i in range(18)),
        *(_make_r2b_case(i, "held_out") for i in range(50)),
        *(_make_r3_case(i, "held_out") for i in range(24)),
    ]
