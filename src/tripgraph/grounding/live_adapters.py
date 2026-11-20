"""Tier C Low-Volume External Provider Integration Demos (TripGraph v7).

IMPORTANT PRODUCTION BOUNDARY:
- Public OSRM demo server supports WALK/DRIVE/BIKE only (never RAIL), imposes
  fair-use non-commercial rate limits (~1 req/s), and offers NO uptime/latency SLA.
- Public Overpass API imposes fair-use limits and community-edited metadata
  (always tagged TIER_3_COMMUNITY).
- Inter-city rail timetables are parsed from static GTFS CSV extracts.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, time
import io
import re
import time as pytime

import httpx

from tripgraph.grounding.provider import RailTimetableSlot
from tripgraph.schema.models import (
    AuthorityTier,
    EvidenceRecord,
    EvidenceSpecificity,
    EvidenceVerdict,
    OpeningRule,
    TransitMode,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

_OSM_DAY_MAP = {"Mo": 0, "Tu": 1, "We": 2, "Th": 3, "Fr": 4, "Sa": 5, "Su": 6}
_RULE_RE = re.compile(
    r"^(?P<days>(?:Mo|Tu|We|Th|Fr|Sa|Su)(?:-(?:Mo|Tu|We|Th|Fr|Sa|Su))?(?:,(?:Mo|Tu|We|Th|Fr|Sa|Su))*)\s+"
    r"(?P<spec>off|closed|(?P<sh>\d{1,2}):(?P<sm>\d{2})-(?P<eh>\d{1,2}):(?P<em_>\d{2}))$"
)


def _expand_osm_days(day_expr: str) -> list[int]:
    days: list[int] = []
    for part in day_expr.split(","):
        if "-" in part:
            a, b = part.split("-", 1)
            ia, ib = _OSM_DAY_MAP[a], _OSM_DAY_MAP[b]
            cur = ia
            while True:
                days.append(cur)
                if cur == ib:
                    break
                cur = (cur + 1) % 7
        else:
            days.append(_OSM_DAY_MAP[part])
    return days


def parse_osm_opening_hours(
    raw_hours: str,
    *,
    place_id: str,
    fetched_at_utc: int,
    record_id: str | None = None,
) -> EvidenceRecord:
    """Parse real-world OSM opening_hours into TIER_3_COMMUNITY EvidenceRecord.

    Quarantines any unparseable or ambiguous syntax (e.g. 'sunrise-sunset',
    'PH off', 'by appointment') as EvidenceVerdict.UNPARSEABLE rather than guessing.
    """
    rid = record_id or f"osm_{place_id}"
    cleaned = raw_hours.strip()
    if not cleaned:
        return EvidenceRecord(
            record_id=rid,
            place_id=place_id,
            authority_tier=AuthorityTier.TIER_3_COMMUNITY,
            specificity=EvidenceSpecificity.WEEKLY_RECURRING,
            verdict=EvidenceVerdict.UNPARSEABLE,
            source="osm_overpass",
            fetched_at_utc=fetched_at_utc,
            ttl_sec=604800,
        )

    if cleaned == "24/7":
        rules = tuple(
            OpeningRule(weekday=d, open_local=time(0, 0), close_local=time(23, 59))
            for d in range(7)
        )
        return EvidenceRecord(
            record_id=rid,
            place_id=place_id,
            authority_tier=AuthorityTier.TIER_3_COMMUNITY,
            specificity=EvidenceSpecificity.WEEKLY_RECURRING,
            verdict=EvidenceVerdict.OPEN,
            source="osm_overpass",
            fetched_at_utc=fetched_at_utc,
            ttl_sec=604800,
            rules=rules,
        )

    rules_by_day: dict[int, list[OpeningRule]] = {}
    closed_days: set[int] = set()

    for segment in [s.strip() for s in cleaned.split(";") if s.strip()]:
        m = _RULE_RE.match(segment)
        if not m:
            # Quarantine any non-deterministic or unsupported OSM grammar safely
            return EvidenceRecord(
                record_id=rid,
                place_id=place_id,
                authority_tier=AuthorityTier.TIER_3_COMMUNITY,
                specificity=EvidenceSpecificity.WEEKLY_RECURRING,
                verdict=EvidenceVerdict.UNPARSEABLE,
                source="osm_overpass",
                fetched_at_utc=fetched_at_utc,
                ttl_sec=604800,
            )
        days = _expand_osm_days(m.group("days"))
        if m.group("spec") in ("off", "closed"):
            for d in days:
                rules_by_day.pop(d, None)
                closed_days.add(d)
            continue

        sh, sm = int(m.group("sh")), int(m.group("sm"))
        eh, em = int(m.group("eh")), int(m.group("em_"))
        if sh > 23 or sm > 59 or em > 59 or eh > 24:
            return EvidenceRecord(
                record_id=rid,
                place_id=place_id,
                authority_tier=AuthorityTier.TIER_3_COMMUNITY,
                specificity=EvidenceSpecificity.WEEKLY_RECURRING,
                verdict=EvidenceVerdict.UNPARSEABLE,
                source="osm_overpass",
                fetched_at_utc=fetched_at_utc,
                ttl_sec=604800,
            )

        # Handle 24:00 or overnight close <= open
        if eh == 24 and em == 0:
            open_t, close_t, offset = time(sh, sm), time(0, 0), 1
        else:
            open_t, close_t = time(sh, sm), time(eh, em)
            offset = 1 if close_t <= open_t else 0

        for d in days:
            closed_days.discard(d)
            rules_by_day.setdefault(d, []).append(
                OpeningRule(
                    weekday=d,
                    open_local=open_t,
                    close_local=close_t,
                    close_day_offset=offset,
                )
            )

    all_rules = tuple(r for d in sorted(rules_by_day) for r in rules_by_day[d])
    verdict = EvidenceVerdict.OPEN if all_rules else EvidenceVerdict.CLOSED
    return EvidenceRecord(
        record_id=rid,
        place_id=place_id,
        authority_tier=AuthorityTier.TIER_3_COMMUNITY,
        specificity=EvidenceSpecificity.WEEKLY_RECURRING,
        verdict=verdict,
        source="osm_overpass",
        fetched_at_utc=fetched_at_utc,
        ttl_sec=604800,
        rules=all_rules,
    )


class OsrmDemoAdapter:
    """Rate-limited (<= 0.5 req/s) cached OSRM road/foot routing adapter (NO RAIL)."""

    BASE_URL = "https://router.project-osrm.org/route/v1"

    def __init__(self, *, min_interval_sec: float = 2.0, timeout_sec: float = 3.0) -> None:
        self._min_interval_sec = min_interval_sec
        self._timeout_sec = timeout_sec
        self._last_call_mono = 0.0
        self._cache: dict[tuple[str, float, float, float, float], int] = {}

    def route_minutes(
        self,
        lat1: float,
        lon1: float,
        lat2: float,
        lon2: float,
        mode: TransitMode,
    ) -> int:
        if mode not in (TransitMode.WALK, TransitMode.DRIVE):
            raise ValueError(
                f"OsrmDemoAdapter supports WALK and DRIVE only (got {mode.value}); "
                "use StaticGtfsTimetableAdapter for RAIL/METRO."
            )
        profile = "foot" if mode == TransitMode.WALK else "driving"
        key = (profile, round(lat1, 5), round(lon1, 5), round(lat2, 5), round(lon2, 5))
        if key in self._cache:
            return self._cache[key]

        wait = self._min_interval_sec - (pytime.monotonic() - self._last_call_mono)
        if wait > 0:
            pytime.sleep(wait)

        url = f"{self.BASE_URL}/{profile}/{lon1},{lat1};{lon2},{lat2}"
        self._last_call_mono = pytime.monotonic()
        resp = httpx.get(url, params={"overview": "false"}, timeout=self._timeout_sec)
        resp.raise_for_status()
        data = resp.json()
        dur_sec = float(data["routes"][0]["duration"])
        mins = max(1, int(round(dur_sec / 60.0)))
        self._cache[key] = mins
        return mins


@dataclass
class StaticGtfsTimetableAdapter:
    """Parses real static GTFS CSV stop_times into discrete RailTimetableSlots."""

    stop_times_csv: str
    tz_iana: str
    cost_usd: int = 95

    def get_slots(
        self,
        origin_stop_id: str,
        dest_stop_id: str,
        service_date: date,
        tz_registry: PinnedTzRegistry,
    ) -> list[RailTimetableSlot]:
        pinned_tz = tz_registry.get(self.tz_iana)
        by_trip: dict[str, dict[str, tuple[int, str, str]]] = {}
        reader = csv.DictReader(io.StringIO(self.stop_times_csv.strip()))
        for row in reader:
            tid = row["trip_id"].strip()
            sid = row["stop_id"].strip()
            seq = int(row["stop_sequence"].strip())
            by_trip.setdefault(tid, {})[sid] = (
                seq,
                row["departure_time"].strip(),
                row["arrival_time"].strip(),
            )

        slots: list[RailTimetableSlot] = []
        for tid, stops in sorted(by_trip.items()):
            if origin_stop_id not in stops or dest_stop_id not in stops:
                continue
            o_seq, dep_str, _ = stops[origin_stop_id]
            d_seq, _, arr_str = stops[dest_stop_id]
            if d_seq <= o_seq:
                continue

            dep_h, dep_m, dep_s = (int(x) for x in dep_str.split(":"))
            arr_h, arr_m, arr_s = (int(x) for x in arr_str.split(":"))
            dep_u = resolve_local_to_utc(
                service_date, time(dep_h % 24, dep_m, dep_s), pinned_tz
            ).utc_epoch
            arr_u = resolve_local_to_utc(
                service_date, time(arr_h % 24, arr_m, arr_s), pinned_tz
            ).utc_epoch
            assert dep_u is not None and arr_u is not None
            if dep_h >= 24:
                dep_u += 86400
            if arr_h >= 24:
                arr_u += 86400
            if arr_u <= dep_u:
                continue

            slots.append(
                RailTimetableSlot(
                    slot_id=f"gtfs_{tid}_{service_date.isoformat()}",
                    origin_station_id=origin_stop_id,
                    dest_station_id=dest_stop_id,
                    service_date=service_date,
                    depart_utc=dep_u,
                    arrive_utc=arr_u,
                    cost_usd=self.cost_usd,
                )
            )
        slots.sort(key=lambda s: s.depart_utc)
        return slots
