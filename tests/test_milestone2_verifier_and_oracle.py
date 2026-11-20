"""Milestone 2 verification tests:
1. AST firewall proving eval/reference_oracle/ has ZERO imports from tripgraph.
2. 25/25 gold adversarial bank verified independently by BOTH internal verifier
   AND zero-shared-code reference oracle (100% agreement gate).
3. Two-axis K3 certificate test: HOLIDAY_UNVERIFIED can never emit VERIFIED_TRUE.
"""

from __future__ import annotations

import ast
import json
import sys
from datetime import date, time
from pathlib import Path

import pytest

# Make eval/ importable for testing the isolated oracle
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from eval.reference_oracle.oracle import (  # noqa: E402
    oracle_evaluate_evidence,
    oracle_evaluate_hotel,
    oracle_resolve_local_utc,
    parse_iso_utc,
)
from tripgraph.schema.models import (  # noqa: E402
    ActivityNode,
    AuthorityTier,
    CityStay,
    EvidenceRecord,
    EvidenceSpecificity,
    EvidenceVerdict,
    HolidayPolicy,
    HotelBookingContract,
    OpeningRule,
    Place,
    TripGraph,
    TruthValue,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc  # noqa: E402
from tripgraph.verifier.engine import verify_trip_graph  # noqa: E402
from tripgraph.verifier.evidence import resolve_place_availability  # noqa: E402
from tripgraph.verifier.hotel import evaluate_hotel_arrival  # noqa: E402

GOLD_CASES = json.loads((ROOT / "eval" / "gold_oracle_cases.json").read_text(encoding="utf-8"))


def test_reference_oracle_zero_tripgraph_imports() -> None:
    """v7 Firewall: eval/reference_oracle/ must NEVER import from tripgraph."""
    oracle_dir = ROOT / "eval" / "reference_oracle"
    bad_imports: list[str] = []

    for py_file in oracle_dir.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "tripgraph" or alias.name.startswith("tripgraph."):
                        bad_imports.append(f"{py_file}:{node.lineno} import {alias.name}")
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "tripgraph" or mod.startswith("tripgraph."):
                    bad_imports.append(f"{py_file}:{node.lineno} from {mod}")

    assert bad_imports == [], f"Oracle firewall violated: {bad_imports}"


def test_gold_bank_has_25_cases() -> None:
    assert len(GOLD_CASES) == 25


def _parse_hhmm(s: str) -> time:
    parts = [int(p) for p in s.split(":")]
    return time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def _run_internal_case(case: dict) -> tuple[str | None, str]:
    reg = PinnedTzRegistry()
    kind = case["kind"]

    if kind == "dst_resolve":
        pinned = reg.get(case["tz_iana"])
        res = resolve_local_to_utc(
            date.fromisoformat(case["local_date"]),
            _parse_hhmm(case["local_time"]),
            pinned,
            ambiguous_policy=case["fold_policy"],
            gap_policy=case["gap_policy"],
        )
        return (None if res.utc_epoch is None else str(res.utc_epoch), "DST")

    if kind == "hotel":
        c_dict = dict(case["contract"])
        if c_dict.get("guest_notified_late_at_utc"):
            c_dict["guest_notified_late_at_utc"] = parse_iso_utc(c_dict["guest_notified_late_at_utc"])
        contract = HotelBookingContract.model_validate(c_dict)
        followup = (
            parse_iso_utc(case["followup_checkin_utc"])
            if case.get("followup_checkin_utc")
            else None
        )
        out = evaluate_hotel_arrival(
            contract,
            parse_iso_utc(case["arrival_utc"]),
            reg,
            followup_checkin_utc=followup,
        )
        return (out.truth.value, out.state)

    if kind == "evidence":
        p_dict = dict(case["place"])
        p_dict["evidence"] = [
            {**e, "fetched_at_utc": parse_iso_utc(e["fetched_at_utc"])}
            for e in p_dict["evidence"]
        ]
        place = Place.model_validate(p_dict)
        holidays = frozenset(date.fromisoformat(d) for d in case.get("public_holidays", []))
        out = resolve_place_availability(
            place,
            parse_iso_utc(case["start_utc"]),
            parse_iso_utc(case["end_utc"]),
            parse_iso_utc(case["now_utc"]),
            reg,
            public_holidays=holidays,
        )
        return (out.truth.value, out.epistemic_code)

    raise ValueError(f"Unknown kind: {kind}")


def _run_oracle_case(case: dict) -> tuple[str | None, str]:
    kind = case["kind"]
    if kind == "dst_resolve":
        epoch = oracle_resolve_local_utc(
            date.fromisoformat(case["local_date"]),
            _parse_hhmm(case["local_time"]),
            case["tz_iana"],
            fold_policy=case["fold_policy"],
            gap_policy=case["gap_policy"],
        )
        return (None if epoch is None else str(epoch), "DST")
    if kind == "hotel":
        return oracle_evaluate_hotel(case)
    if kind == "evidence":
        return oracle_evaluate_evidence(case)
    raise ValueError(f"Unknown kind: {kind}")


@pytest.mark.parametrize("case", GOLD_CASES, ids=[c["case_id"] for c in GOLD_CASES])
def test_gold_bank_dual_agreement(case: dict) -> None:
    internal_res = _run_internal_case(case)
    oracle_res = _run_oracle_case(case)

    # 1. Zero disagreement between internal verifier and independent oracle
    assert internal_res == oracle_res, (
        f"Disagreement on {case['case_id']}: internal={internal_res} vs oracle={oracle_res}"
    )

    # 2. Both match hand-computed gold ground truth
    if case["kind"] == "dst_resolve":
        exp = None if case["expected_utc"] is None else str(parse_iso_utc(case["expected_utc"]))
        assert internal_res[0] == exp
    else:
        assert internal_res == (case["expected_truth"], case["expected_code"])


def test_k3_certificate_never_elevates_unverified_holiday_to_true() -> None:
    """v7 Certificate Rule: HOLIDAY_UNVERIFIED -> hard_constraints_status=UNKNOWN,
    verification_certificate=CONDITIONAL_ON_ASSUMPTIONS (NEVER VERIFIED_TRUE)."""
    reg = PinnedTzRegistry()
    now_utc = parse_iso_utc("2026-06-10T10:00:00Z")
    start_utc = parse_iso_utc("2026-06-15T09:00:00Z")
    end_utc = parse_iso_utc("2026-06-15T11:00:00Z")

    place = Place(
        place_id="p_hol_mus",
        name="State Museum",
        city_id="berlin",
        tz_iana="Europe/Berlin",
        lat=52.52,
        lon=13.40,
        category="museum",
        holiday_policy=HolidayPolicy.UNKNOWN,
        evidence=(
            EvidenceRecord(
                record_id="r_wk_1",
                place_id="p_hol_mus",
                authority_tier=AuthorityTier.TIER_1_OFFICIAL,
                specificity=EvidenceSpecificity.WEEKLY_RECURRING,
                verdict=EvidenceVerdict.OPEN,
                source="official_site",
                fetched_at_utc=now_utc - 600,
                ttl_sec=86400,
                rules=(OpeningRule(weekday=0, open_local=time(10, 0), close_local=time(18, 0)),),
            ),
        ),
    )

    stay = CityStay(
        id="stay_ber",
        city_id="berlin",
        tz_iana="Europe/Berlin",
        hotel=HotelBookingContract(
            hotel_id="h_ber",
            hotel_name="Berlin Hotel",
            tz_iana="Europe/Berlin",
            check_in_date=date(2026, 6, 14),
            check_out_date=date(2026, 6, 16),
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )

    act = ActivityNode(
        id="act_1",
        stay_id="stay_ber",
        city_id="berlin",
        place_id="p_hol_mus",
        local_date=date(2026, 6, 15),
        start_utc=start_utc,
        end_utc=end_utc,
        duration_min=120,
    )

    graph = TripGraph(trip_id="t_hol", stays=(stay,), activities=(act,))
    report = verify_trip_graph(
        graph,
        {"p_hol_mus": place},
        lambda _a, _b: 15,
        now_utc,
        reg,
        public_holidays_by_city={"berlin": frozenset({date(2026, 6, 15)})},
    )

    assert report.hard_constraints_status == TruthValue.UNKNOWN
    assert report.verification_certificate == "CONDITIONAL_ON_ASSUMPTIONS"
    assert len(report.violations) == 0
    assert len(report.assumptions_accepted) == 1
    assert report.assumptions_accepted[0].code == "HOLIDAY_UNVERIFIED"
