"""Tier B Synthetic Resilience Fault Injection (ChaosDriftMiddleware).

Explicitly documented as deterministic CI fault injection — NOT proof of live
third-party supplier compatibility (see Tier C live_adapters.py for real APIs).
Tests:
1. Optimistic ETag drift between plan/search and edit commit.
2. Stale-while-revalidate TTL expiry injection.
3. Cross-provider conflicting opening-hour metadata injection.
4. Deterministic HTTP 429/503 outage injection.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from tripgraph.grounding.provider import (
    CandidateSearchIntent,
    GroundedRecord,
    GroundingProvider,
    ProviderOutageError,
    RailTimetableSlot,
)
from tripgraph.schema.models import (
    AuthorityTier,
    EvidenceRecord,
    EvidenceSpecificity,
    EvidenceVerdict,
    Place,
    TransitMode,
)


@dataclass
class ChaosConfig:
    fail_search_with_429: bool = False
    drift_etag_on_second_read: frozenset[str] = frozenset()
    inject_stale_age_sec: int = 0
    inject_conflicting_closed_for: frozenset[str] = frozenset()


class ChaosDriftMiddleware:
    """Wraps any GroundingProvider to inject reproducible production pathologies."""

    def __init__(self, inner: GroundingProvider, config: ChaosConfig) -> None:
        self._inner = inner
        self.config = config
        self._read_counts: dict[str, int] = {}

    def _mutate_record(self, rec: GroundedRecord[Place]) -> GroundedRecord[Place]:
        pid = rec.payload.place_id
        self._read_counts[pid] = self._read_counts.get(pid, 0) + 1

        etag = rec.etag
        if pid in self.config.drift_etag_on_second_read and self._read_counts[pid] >= 2:
            etag = f"{etag}__DRIFTED"

        place = rec.payload
        if self.config.inject_stale_age_sec > 0:
            stale_ev = tuple(
                ev.model_copy(
                    update={"fetched_at_utc": max(0, ev.fetched_at_utc - self.config.inject_stale_age_sec)}
                )
                for ev in place.evidence
            )
            place = place.model_copy(update={"evidence": stale_ev})

        if pid in self.config.inject_conflicting_closed_for:
            # Inject a peer Tier-1 DATE_SPECIFIC CLOSED conflict on same dates
            extra = tuple(
                EvidenceRecord(
                    record_id=f"chaos_conflict_{ev.record_id}",
                    place_id=pid,
                    authority_tier=AuthorityTier.TIER_1_OFFICIAL,
                    specificity=EvidenceSpecificity.DATE_SPECIFIC,
                    verdict=EvidenceVerdict.CLOSED,
                    source="chaos_peer_supplier",
                    fetched_at_utc=ev.fetched_at_utc,
                    ttl_sec=ev.ttl_sec,
                    applicable_date=ev.applicable_date,
                )
                for ev in place.evidence
                if ev.specificity == EvidenceSpecificity.DATE_SPECIFIC
            )
            place = place.model_copy(update={"evidence": place.evidence + extra})

        return GroundedRecord(
            payload=place,
            source=rec.source,
            fetched_at_utc=max(0, rec.fetched_at_utc - self.config.inject_stale_age_sec),
            ttl_seconds=rec.ttl_seconds,
            etag=etag,
        )

    def get_place(self, place_id: str) -> GroundedRecord[Place] | None:
        rec = self._inner.get_place(place_id)
        return None if rec is None else self._mutate_record(rec)

    def all_places(self) -> dict[str, Place]:
        return {
            pid: self._mutate_record(rec).payload
            for pid in self._inner.all_places()
            if (rec := self._inner.get_place(pid)) is not None
        }

    def search_candidates(
        self, intent: CandidateSearchIntent, *, limit: int = 20
    ) -> list[GroundedRecord[Place]]:
        if self.config.fail_search_with_429:
            raise ProviderOutageError("Injected HTTP 429 Too Many Requests from supplier.")
        return [
            self._mutate_record(r)
            for r in self._inner.search_candidates(intent, limit=limit)
        ]

    def transit_minutes(
        self, origin_id: str, dest_id: str, mode: TransitMode = TransitMode.METRO
    ) -> int:
        return self._inner.transit_minutes(origin_id, dest_id, mode=mode)

    def get_rail_slots(
        self, origin_station_id: str, dest_station_id: str, service_date: date
    ) -> list[RailTimetableSlot]:
        return self._inner.get_rail_slots(origin_station_id, dest_station_id, service_date)

    def get_public_holidays(self, city_id: str) -> frozenset[date]:
        return self._inner.get_public_holidays(city_id)
