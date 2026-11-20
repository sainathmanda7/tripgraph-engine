"""Kleene K3 availability & Pareto single-witness evidence resolver (TripGraph v7).

Key guarantees:
1. EXPIRED and UNPARSEABLE records are dropped before evaluation.
2. FRESH records suppress STALE records in the active pool.
3. Dominance is computed strictly over (authority_tier, specificity).
4. TRUE requires a SINGLE qualifying witness record on the Pareto frontier
   (never cross-record maxima).
5. Single Tier-3 community closures NEVER emit FALSE; they emit
   UNKNOWN (COMMUNITY_REPORTED_CLOSED) to be policy-gated transparently.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from tripgraph.schema.models import (
    AuthorityTier,
    EvidenceRecord,
    EvidenceSpecificity,
    EvidenceVerdict,
    HolidayPolicy,
    OpeningRule,
    Place,
    TruthValue,
)
from tripgraph.time.tz_registry import (
    PinnedTimezone,
    PinnedTzRegistry,
    resolve_local_to_utc,
    utc_to_local,
)


@dataclass(frozen=True)
class AvailabilityOutcome:
    """Result of evaluating venue availability for a concrete [start_utc, end_utc] slot."""

    truth: TruthValue
    epistemic_code: str
    witness_record_id: str | None = None
    audit_notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _EvaluatedRecord:
    record: EvidenceRecord
    is_fresh: bool
    effective_verdict: EvidenceVerdict  # OPEN or CLOSED for the target interval


def materialize_rule_utc_interval(
    anchor_date: date,
    rule: OpeningRule,
    pinned_tz: PinnedTimezone,
) -> tuple[int, int] | None:
    """Convert a wall-clock OpeningRule anchored on anchor_date to [open_utc, close_utc]."""
    open_res = resolve_local_to_utc(
        anchor_date,
        rule.open_local,
        pinned_tz,
        ambiguous_policy=rule.dst_ambiguous_open,
        gap_policy=rule.dst_gap_policy,
    )
    close_date = anchor_date + timedelta(days=rule.close_day_offset)
    close_res = resolve_local_to_utc(
        close_date,
        rule.close_local,
        pinned_tz,
        ambiguous_policy=rule.dst_ambiguous_close,
        gap_policy=rule.dst_gap_policy,
    )
    if open_res.utc_epoch is None or close_res.utc_epoch is None:
        return None
    if close_res.utc_epoch <= open_res.utc_epoch:
        return None
    return (open_res.utc_epoch, close_res.utc_epoch)


def interval_covered_by_rules(
    start_utc: int,
    end_utc: int,
    rules: tuple[OpeningRule, ...],
    pinned_tz: PinnedTimezone,
    applicable_date: date | None = None,
) -> bool:
    """Check exact UTC containment against both d-1 (overnight) and d rules."""
    start_local_date = utc_to_local(start_utc, pinned_tz).date()
    prev_local_date = start_local_date - timedelta(days=1)

    candidate_anchors: list[tuple[date, bool]] = [
        (prev_local_date, True),   # overnight rules opened yesterday
        (start_local_date, False),  # rules opened today
    ]

    for anchor_date, overnight_only in candidate_anchors:
        if applicable_date is not None and anchor_date != applicable_date:
            continue
        anchor_wd = anchor_date.weekday()
        for rule in rules:
            if overnight_only and rule.close_day_offset != 1:
                continue
            if rule.weekday != anchor_wd:
                continue
            utc_span = materialize_rule_utc_interval(anchor_date, rule, pinned_tz)
            if utc_span is None:
                continue
            open_utc, close_utc = utc_span
            if open_utc <= start_utc and end_utc <= close_utc:
                return True
    return False


def _record_applies_to_event(
    rec: EvidenceRecord,
    start_local_date: date,
) -> bool:
    if rec.specificity != EvidenceSpecificity.DATE_SPECIFIC:
        return True
    assert rec.applicable_date is not None
    if rec.applicable_date == start_local_date:
        return True
    if rec.applicable_date == start_local_date - timedelta(days=1):
        # Applies across midnight only if it has an overnight rule
        return any(r.close_day_offset == 1 for r in rec.rules)
    return False


def _strictly_dominates(a: EvidenceRecord, b: EvidenceRecord) -> bool:
    """True iff record a has >= authority AND >= specificity with at least one strict win.

    Remember: AuthorityTier uses lower int = higher authority (TIER_1=1 > TIER_3=3),
    while EvidenceSpecificity uses higher int = more specific (DATE_SPECIFIC=2 > WEEKLY=1).
    """
    auth_ge = a.authority_tier <= b.authority_tier
    spec_ge = a.specificity >= b.specificity
    strict_win = (a.authority_tier < b.authority_tier) or (a.specificity > b.specificity)
    return auth_ge and spec_ge and strict_win


def resolve_place_availability(
    place: Place,
    start_utc: int,
    end_utc: int,
    now_utc: int,
    tz_registry: PinnedTzRegistry,
    public_holidays: frozenset[date] = frozenset(),
) -> AvailabilityOutcome:
    """Evaluate a Place's K3 availability for [start_utc, end_utc] at evaluation time now_utc."""
    pinned_tz = tz_registry.get(place.tz_iana)
    start_local_date = utc_to_local(start_utc, pinned_tz).date()
    audit_notes: list[str] = []

    # 1. Filter applicable, parseable, non-expired records
    usable: list[_EvaluatedRecord] = []
    for rec in place.evidence:
        if rec.verdict == EvidenceVerdict.UNPARSEABLE:
            audit_notes.append(f"DROPPED_UNPARSEABLE:{rec.record_id}")
            continue
        if not _record_applies_to_event(rec, start_local_date):
            continue

        age = max(0, now_utc - rec.fetched_at_utc)
        if age > rec.ttl_sec + rec.max_stale_age_sec:
            audit_notes.append(f"DROPPED_EXPIRED:{rec.record_id}")
            continue
        is_fresh = age <= rec.ttl_sec

        if rec.verdict == EvidenceVerdict.CLOSED:
            eff_verdict = EvidenceVerdict.CLOSED
        else:
            # OPEN record: if rules are present, verify exact interval containment
            if rec.rules:
                covered = interval_covered_by_rules(
                    start_utc,
                    end_utc,
                    rec.rules,
                    pinned_tz,
                    applicable_date=rec.applicable_date,
                )
                eff_verdict = EvidenceVerdict.OPEN if covered else EvidenceVerdict.CLOSED
            else:
                eff_verdict = EvidenceVerdict.OPEN

        usable.append(_EvaluatedRecord(record=rec, is_fresh=is_fresh, effective_verdict=eff_verdict))

    if not usable:
        return AvailabilityOutcome(
            truth=TruthValue.UNKNOWN,
            epistemic_code="NO_USABLE_EVIDENCE",
            audit_notes=tuple(audit_notes),
        )

    # 2. Freshness gating: FRESH suppresses STALE
    fresh_pool = [u for u in usable if u.is_fresh]
    if fresh_pool:
        if len(fresh_pool) < len(usable):
            audit_notes.append("STALE_RECORDS_SUPPRESSED_BY_FRESH")
        active = fresh_pool
        pool_is_fresh = True
    else:
        active = usable
        pool_is_fresh = False

    # 3. Pareto dominance frontier over (authority_tier, specificity)
    frontier: list[_EvaluatedRecord] = []
    for cand in active:
        dominated = any(
            _strictly_dominates(other.record, cand.record)
            for other in active
            if other is not cand
        )
        if not dominated:
            frontier.append(cand)
        else:
            audit_notes.append(f"DOMINATED_RECORD:{cand.record.record_id}")

    verdicts = {e.effective_verdict for e in frontier}

    # 4. Disagreement on Pareto frontier -> UNKNOWN (never FALSE)
    if len(verdicts) > 1:
        return AvailabilityOutcome(
            truth=TruthValue.UNKNOWN,
            epistemic_code="CONFLICTING_EVIDENCE_ON_FRONTIER",
            audit_notes=tuple(audit_notes),
        )

    unanimous_verdict = next(iter(verdicts))

    # 5. Unanimous CLOSED on frontier
    if unanimous_verdict == EvidenceVerdict.CLOSED:
        if not pool_is_fresh:
            return AvailabilityOutcome(
                truth=TruthValue.UNKNOWN,
                epistemic_code="STALE_CLOSED_UNCONFIRMED",
                audit_notes=tuple(audit_notes),
            )
        # Single-record authoritative CLOSED witness (Tier 1 or Tier 2)
        witness = next(
            (
                e.record
                for e in frontier
                if e.record.authority_tier
                in (AuthorityTier.TIER_1_OFFICIAL, AuthorityTier.TIER_2_COMMERCIAL)
            ),
            None,
        )
        if witness is not None:
            return AvailabilityOutcome(
                truth=TruthValue.FALSE,
                epistemic_code="CLOSED_BY_AUTHORITATIVE_SOURCE",
                witness_record_id=witness.record_id,
                audit_notes=tuple(audit_notes),
            )
        # Tier-3 community closure NEVER emits FALSE
        return AvailabilityOutcome(
            truth=TruthValue.UNKNOWN,
            epistemic_code="COMMUNITY_REPORTED_CLOSED",
            audit_notes=tuple(audit_notes),
        )

    # 6. Unanimous OPEN on frontier -> Check Public Holiday if no DATE_SPECIFIC record exists
    has_date_specific = any(
        e.record.specificity == EvidenceSpecificity.DATE_SPECIFIC for e in frontier
    )
    if start_local_date in public_holidays and not has_date_specific:
        if place.holiday_policy == HolidayPolicy.CLOSED:
            return AvailabilityOutcome(
                truth=TruthValue.FALSE,
                epistemic_code="HOLIDAY_CLOSED_BY_POLICY",
                audit_notes=tuple(audit_notes),
            )
        if place.holiday_policy in (HolidayPolicy.UNKNOWN, HolidayPolicy.HOLIDAY_HOURS):
            return AvailabilityOutcome(
                truth=TruthValue.UNKNOWN,
                epistemic_code="HOLIDAY_UNVERIFIED",
                audit_notes=tuple(audit_notes),
            )

    if not pool_is_fresh:
        return AvailabilityOutcome(
            truth=TruthValue.UNKNOWN,
            epistemic_code="STALE_OPEN_ASSUMED",
            audit_notes=tuple(audit_notes),
        )

    # Single-record witness check for VERIFIED_DATE_OPEN (TRUE)
    # Authority <= TIER_2 AND specificity == DATE_SPECIFIC must hold on the SAME record!
    open_witness = next(
        (
            e.record
            for e in frontier
            if e.record.specificity == EvidenceSpecificity.DATE_SPECIFIC
            and e.record.authority_tier
            in (AuthorityTier.TIER_1_OFFICIAL, AuthorityTier.TIER_2_COMMERCIAL)
        ),
        None,
    )
    if open_witness is not None:
        return AvailabilityOutcome(
            truth=TruthValue.TRUE,
            epistemic_code="VERIFIED_DATE_OPEN",
            witness_record_id=open_witness.record_id,
            audit_notes=tuple(audit_notes),
        )

    # Distinguish why it stayed UNKNOWN (never FALSE, never false TRUE)
    has_tier12_weekly = any(
        e.record.specificity == EvidenceSpecificity.WEEKLY_RECURRING
        and e.record.authority_tier
        in (AuthorityTier.TIER_1_OFFICIAL, AuthorityTier.TIER_2_COMMERCIAL)
        for e in frontier
    )
    has_tier3_date = any(
        e.record.specificity == EvidenceSpecificity.DATE_SPECIFIC
        and e.record.authority_tier == AuthorityTier.TIER_3_COMMUNITY
        for e in frontier
    )
    if has_tier12_weekly and has_tier3_date:
        code = "UNCORROBORATED_MIXED_EVIDENCE"
    elif has_tier3_date:
        code = "COMMUNITY_DATE_UNCORROBORATED"
    else:
        code = "WEEKLY_SCHEDULE_ASSUMED"

    return AvailabilityOutcome(
        truth=TruthValue.UNKNOWN,
        epistemic_code=code,
        audit_notes=tuple(audit_notes),
    )
