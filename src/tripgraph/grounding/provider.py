"""GroundingProvider protocol, SearchIntent, and hard BudgetedProviderProxy (TripGraph v7).

Contract:
- Both S2_taxonomy and S3 MUST access candidate search exclusively through
  BudgetedProviderProxy so retrieval budgets are mathematically identical:
    * max_provider_queries = 1 batched search call per synchronous cycle
    * max_candidates_fetched = 20 grounded records
    * max_candidates_to_cpsat = 5 ranked candidates injected into CP-SAT
- Every returned record carries versioned provenance (source, fetched_at_utc, etag).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Generic, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field

from tripgraph.schema.models import Place, TransitMode

T = TypeVar("T")


class RetrievalBudgetExceededError(RuntimeError):
    """Raised if any arm exceeds its allocated provider query or candidate budget."""


class ProviderOutageError(RuntimeError):
    """Raised when an upstream grounding provider returns 429/503 or times out."""


class ETagDriftError(RuntimeError):
    """Raised when an inventory item's ETag changed between plan and edit commit."""


@dataclass(frozen=True)
class GroundedRecord(Generic[T]):
    payload: T
    source: str
    fetched_at_utc: int
    ttl_seconds: int
    etag: str


@dataclass(frozen=True)
class RailTimetableSlot:
    slot_id: str
    origin_station_id: str
    dest_station_id: str
    service_date: date
    depart_utc: int
    arrive_utc: int
    cost_usd: int
    etag: str = "gtfs_static_v1"


class CandidateSearchIntent(BaseModel):
    """Structured tool query emitted by S2_taxonomy OR Phase B LLM."""

    model_config = ConfigDict(frozen=True)

    city_id: str
    target_categories: tuple[str, ...] = Field(min_length=1, max_length=6)
    required_dietary_tags: tuple[str, ...] = ()
    preferred_tags: tuple[str, ...] = ()
    avoid_tags: tuple[str, ...] = ()
    excluded_place_ids: frozenset[str] = frozenset()
    max_cost_usd: int | None = None
    near_lat: float | None = None
    near_lon: float | None = None
    max_radius_km: float | None = None


class GroundingProvider(Protocol):
    def get_place(self, place_id: str) -> GroundedRecord[Place] | None: ...
    def all_places(self) -> dict[str, Place]: ...
    def search_candidates(
        self, intent: CandidateSearchIntent, *, limit: int = 20
    ) -> list[GroundedRecord[Place]]: ...
    def transit_minutes(
        self, origin_id: str, dest_id: str, mode: TransitMode = TransitMode.METRO
    ) -> int: ...
    def get_rail_slots(
        self, origin_station_id: str, dest_station_id: str, service_date: date
    ) -> list[RailTimetableSlot]: ...
    def get_public_holidays(self, city_id: str) -> frozenset[date]: ...


@dataclass
class RetrievalTelemetry:
    provider_queries: int = 0
    candidates_fetched: int = 0
    candidates_to_cpsat: int = 0
    etags_seen: dict[str, str] = field(default_factory=dict)


class BudgetedProviderProxy:
    """Wraps any GroundingProvider and hard-enforces equal v7 retrieval budgets."""

    def __init__(
        self,
        inner: GroundingProvider,
        *,
        max_provider_queries: int = 1,
        max_candidates_fetched: int = 20,
        max_candidates_to_cpsat: int = 5,
    ) -> None:
        self._inner = inner
        self.max_provider_queries = max_provider_queries
        self.max_candidates_fetched = max_candidates_fetched
        self.max_candidates_to_cpsat = max_candidates_to_cpsat
        self.telemetry = RetrievalTelemetry()

    def search_candidates(
        self, intent: CandidateSearchIntent
    ) -> list[GroundedRecord[Place]]:
        if self.telemetry.provider_queries >= self.max_provider_queries:
            raise RetrievalBudgetExceededError(
                f"Exceeded max_provider_queries={self.max_provider_queries}."
            )
        self.telemetry.provider_queries += 1
        records = self._inner.search_candidates(
            intent, limit=self.max_candidates_fetched
        )[: self.max_candidates_fetched]
        self.telemetry.candidates_fetched += len(records)
        for rec in records:
            self.telemetry.etags_seen[rec.payload.place_id] = rec.etag
        return records

    def record_cpsat_injection(self, candidate_ids: list[str]) -> list[str]:
        if len(candidate_ids) > self.max_candidates_to_cpsat:
            raise RetrievalBudgetExceededError(
                f"Attempted to inject {len(candidate_ids)} candidates > "
                f"max_candidates_to_cpsat={self.max_candidates_to_cpsat}."
            )
        self.telemetry.candidates_to_cpsat += len(candidate_ids)
        return candidate_ids

    def get_place(self, place_id: str) -> GroundedRecord[Place] | None:
        return self._inner.get_place(place_id)

    def transit_minutes(
        self, origin_id: str, dest_id: str, mode: TransitMode = TransitMode.METRO
    ) -> int:
        return self._inner.transit_minutes(origin_id, dest_id, mode=mode)

    def verify_etags_unchanged(self) -> None:
        """Optimistic lock verification before commit: detect mid-edit supplier drift."""
        for pid, old_etag in self.telemetry.etags_seen.items():
            cur = self._inner.get_place(pid)
            if cur is None or cur.etag != old_etag:
                raise ETagDriftError(
                    f"Inventory ETag drifted for {pid}: expected {old_etag!r}, "
                    f"got {None if cur is None else cur.etag!r}."
                )
