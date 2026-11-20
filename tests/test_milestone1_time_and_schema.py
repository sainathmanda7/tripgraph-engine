"""Milestone 1 verification tests: Byte-pinned TZ, DST math, and v7 schemas."""

from __future__ import annotations

import ast
from datetime import date, time
from pathlib import Path

import pytest
from pydantic import ValidationError

from tripgraph.schema.models import (
    ActivityNode,
    HotelBookingContract,
    OpeningRule,
)
from tripgraph.time.tz_registry import (
    PinnedTzRegistry,
    resolve_local_to_utc,
    utc_to_local,
)


def test_ast_bans_direct_zoneinfo_constructor() -> None:
    """v7 Contract: No module in src/tripgraph/ may call ZoneInfo(key) directly."""
    src_root = Path(__file__).resolve().parents[1] / "src" / "tripgraph"
    violations: list[str] = []

    for py_file in src_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if isinstance(func, ast.Name) and func.id == "ZoneInfo":
                violations.append(f"{py_file}:{node.lineno}")
            elif isinstance(func, ast.Attribute) and func.attr == "ZoneInfo":
                violations.append(f"{py_file}:{node.lineno}")

    assert violations == [], f"Banned direct ZoneInfo() call(s) found: {violations}"


def test_pinned_tz_registry_hashes_real_bytes() -> None:
    reg = PinnedTzRegistry()
    tokyo = reg.get("Asia/Tokyo")
    berlin = reg.get("Europe/Berlin")

    assert len(tokyo.tzif_sha256) == 64
    assert len(berlin.tzif_sha256) == 64
    assert tokyo.tzif_sha256 != berlin.tzif_sha256
    assert reg.get("Asia/Tokyo") is tokyo  # cached instance


def test_dst_spring_forward_gap_exact_transition() -> None:
    """Europe/Berlin jumps 02:00 -> 03:00 on Sunday 2026-03-29."""
    reg = PinnedTzRegistry()
    berlin = reg.get("Europe/Berlin")

    shifted = resolve_local_to_utc(
        date(2026, 3, 29), time(2, 30), berlin, gap_policy="shift_forward"
    )
    assert shifted.is_gap is True
    assert shifted.utc_epoch is not None
    local_dt = utc_to_local(shifted.utc_epoch, berlin)
    # Must land on EXACT first valid post-gap instant: 03:00:00+02:00 (not 03:30!)
    assert (local_dt.hour, local_dt.minute, local_dt.second) == (3, 0, 0)

    closed = resolve_local_to_utc(
        date(2026, 3, 29), time(2, 30), berlin, gap_policy="closed"
    )
    assert closed.is_gap is True
    assert closed.utc_epoch is None


def test_dst_fall_back_overnight_bar_hours() -> None:
    """Europe/London repeats 01:00-02:00 on Sunday 2026-10-25."""
    reg = PinnedTzRegistry()
    london = reg.get("Europe/London")

    # 22:00 Sat Oct 24 -> 02:00 Sun Oct 25 = 5 full physical hours
    open_res = resolve_local_to_utc(date(2026, 10, 24), time(22, 0), london)
    close_0200 = resolve_local_to_utc(date(2026, 10, 25), time(2, 0), london)
    assert open_res.utc_epoch is not None and close_0200.utc_epoch is not None
    assert (close_0200.utc_epoch - open_res.utc_epoch) == 5 * 3600

    # 22:00 -> ambiguous 01:30: fold_0 = 3.5h, fold_1 = 4.5h
    close_f0 = resolve_local_to_utc(
        date(2026, 10, 25), time(1, 30), london, ambiguous_policy="fold_0"
    )
    close_f1 = resolve_local_to_utc(
        date(2026, 10, 25), time(1, 30), london, ambiguous_policy="fold_1"
    )
    assert close_f0.is_ambiguous is True and close_f1.is_ambiguous is True
    assert close_f0.utc_epoch is not None and close_f1.utc_epoch is not None
    assert (close_f0.utc_epoch - open_res.utc_epoch) == int(3.5 * 3600)
    assert (close_f1.utc_epoch - open_res.utc_epoch) == int(4.5 * 3600)


def test_stage0_activity_axiom_rejects_contradiction() -> None:
    """start=12:00, end=11:00, dur=60 must be rejected at Stage 0 before CP-SAT."""
    with pytest.raises(ValidationError, match="Stage-0 Axiom Contradiction"):
        ActivityNode(
            id="act_bad",
            stay_id="stay_1",
            city_id="berlin",
            place_id="p_1",
            local_date=date(2026, 6, 12),
            start_utc=1_781_265_600,
            end_utc=1_781_262_000,  # 1 hour earlier than start
            duration_min=60,
        )


def test_hotel_contract_reserved_nights_and_access_guards() -> None:
    contract = HotelBookingContract(
        hotel_id="h_ber_1",
        hotel_name="Hotel Mitte",
        tz_iana="Europe/Berlin",
        check_in_date=date(2026, 6, 12),
        check_out_date=date(2026, 6, 15),
        guarantee_type="CARD_GUARANTEED",
        access_method="STAFFED_24H",
    )
    assert contract.reserved_nights == (
        date(2026, 6, 12),
        date(2026, 6, 13),
        date(2026, 6, 14),
    )

    with pytest.raises(ValidationError, match="STAFFED_HOURS"):
        HotelBookingContract(
            hotel_id="h_bad",
            hotel_name="No Cutoff",
            tz_iana="Europe/Berlin",
            check_in_date=date(2026, 6, 12),
            check_out_date=date(2026, 6, 14),
            guarantee_type="PREPAID",
            access_method="STAFFED_HOURS",
            staffed_desk_cutoff=None,
        )


def test_opening_rule_overnight_validation() -> None:
    with pytest.raises(ValidationError, match="close_day_offset=1"):
        OpeningRule(weekday=5, open_local=time(22, 0), close_local=time(2, 0), close_day_offset=0)

    rule = OpeningRule(weekday=5, open_local=time(22, 0), close_local=time(2, 0), close_day_offset=1)
    assert rule.close_day_offset == 1
