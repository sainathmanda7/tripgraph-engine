"""Tier A verified multi-city grounding fixtures: Tokyo, Kyoto, London, Paris.

Provides zero-key, deterministic, realistic places, opening schedules, Monday
closures, dietary & vibe tags, haversine transit durations, and rail timetables.
"""

from __future__ import annotations

from datetime import date, time
import math

from tripgraph.grounding.provider import (
    CandidateSearchIntent,
    GroundedRecord,
    RailTimetableSlot,
)
from tripgraph.schema.models import (
    AuthorityTier,
    EvidenceRecord,
    EvidenceSpecificity,
    EvidenceVerdict,
    HolidayPolicy,
    OpeningRule,
    Place,
    TransitMode,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

DEFAULT_FIXTURE_NOW_UTC = 1_781_085_600  # 2026-06-10T10:00:00Z


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _weekly_rules(
    open_h: int,
    close_h: int,
    *,
    closed_weekdays: tuple[int, ...] = (),
    close_day_offset: int = 0,
) -> tuple[OpeningRule, ...]:
    rules: list[OpeningRule] = []
    for wd in range(7):
        if wd in closed_weekdays:
            continue
        rules.append(
            OpeningRule(
                weekday=wd,
                open_local=time(open_h, 0),
                close_local=time(close_h, 0),
                close_day_offset=close_day_offset,
            )
        )
    return tuple(rules)


def _make_place(
    place_id: str,
    name: str,
    city_id: str,
    tz_iana: str,
    lat: float,
    lon: float,
    category: str,
    *,
    open_h: int = 9,
    close_h: int = 18,
    closed_weekdays: tuple[int, ...] = (),
    tags: tuple[str, ...] = (),
    dietary_tags: tuple[str, ...] = (),
    cost: int = 20,
    rating: float = 4.5,
    holiday_policy: HolidayPolicy = HolidayPolicy.OPEN_NORMAL,
    verified_dates: tuple[date, ...] = (),
    closed_dates: tuple[date, ...] = (),
    now_utc: int = DEFAULT_FIXTURE_NOW_UTC,
) -> Place:
    evidence: list[EvidenceRecord] = [
        EvidenceRecord(
            record_id=f"ev_wk_{place_id}",
            place_id=place_id,
            authority_tier=AuthorityTier.TIER_1_OFFICIAL,
            specificity=EvidenceSpecificity.WEEKLY_RECURRING,
            verdict=EvidenceVerdict.OPEN,
            source="verified_fixture_official",
            fetched_at_utc=now_utc - 1800,
            ttl_sec=604800,
            rules=_weekly_rules(open_h, close_h, closed_weekdays=closed_weekdays),
        )
    ]
    for vd in verified_dates:
        wd = vd.weekday()
        if wd in closed_weekdays or vd in closed_dates:
            evidence.append(
                EvidenceRecord(
                    record_id=f"ev_cl_{place_id}_{vd.isoformat()}",
                    place_id=place_id,
                    authority_tier=AuthorityTier.TIER_1_OFFICIAL,
                    specificity=EvidenceSpecificity.DATE_SPECIFIC,
                    verdict=EvidenceVerdict.CLOSED,
                    source="verified_fixture_official",
                    fetched_at_utc=now_utc - 900,
                    ttl_sec=604800,
                    applicable_date=vd,
                )
            )
        else:
            evidence.append(
                EvidenceRecord(
                    record_id=f"ev_dt_{place_id}_{vd.isoformat()}",
                    place_id=place_id,
                    authority_tier=AuthorityTier.TIER_1_OFFICIAL,
                    specificity=EvidenceSpecificity.DATE_SPECIFIC,
                    verdict=EvidenceVerdict.OPEN,
                    source="verified_fixture_official",
                    fetched_at_utc=now_utc - 900,
                    ttl_sec=604800,
                    applicable_date=vd,
                    rules=(
                        OpeningRule(
                            weekday=wd,
                            open_local=time(open_h, 0),
                            close_local=time(close_h, 0),
                        ),
                    ),
                )
            )
    return Place(
        place_id=place_id,
        name=name,
        city_id=city_id,
        tz_iana=tz_iana,
        lat=lat,
        lon=lon,
        category=category,
        tags=tags,
        dietary_tags=dietary_tags,
        estimated_cost_usd=cost,
        rating=rating,
        holiday_policy=holiday_policy,
        evidence=tuple(evidence),
    )


def build_default_places(now_utc: int = DEFAULT_FIXTURE_NOW_UTC) -> dict[str, Place]:
    """Build curated Tokyo, Kyoto, London, and Paris verified POI registry."""
    jun_dates = tuple(date(2026, 6, d) for d in range(12, 20))
    dst_dates = (date(2026, 10, 24), date(2026, 10, 25), date(2026, 10, 26))

    raw = [
        # --- TOKYO ---
        _make_place(
            "tyo_mori_art", "Mori Art Museum", "tokyo", "Asia/Tokyo",
            35.6604, 139.7292, "contemporary_art", open_h=10, close_h=20,
            closed_weekdays=(0,),  # Closed Mondays!
            tags=("indoor", "minimalist_craft", "stroller_friendly", "quiet"),
            cost=20, rating=4.7, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_2121_design", "21_21 DESIGN SIGHT", "tokyo", "Asia/Tokyo",
            35.6655, 139.7302, "design_museum", open_h=10, close_h=19,
            closed_weekdays=(1,),  # Open Mondays!
            tags=("indoor", "minimalist_craft", "stroller_friendly", "quiet", "architecture"),
            cost=15, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_nezu_museum", "Nezu Museum & Garden", "tokyo", "Asia/Tokyo",
            35.6622, 139.7171, "art_gallery", open_h=10, close_h=17,
            closed_weekdays=(0,),
            tags=("quiet", "minimalist_craft", "garden"),
            cost=15, rating=4.8, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_national_art", "National Art Center Tokyo", "tokyo", "Asia/Tokyo",
            35.6653, 139.7264, "art_gallery", open_h=10, close_h=18,
            closed_weekdays=(1,),  # Open Mondays!
            tags=("indoor", "architecture", "stroller_friendly", "crowded_tourist"),
            cost=18, rating=4.7, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_mingeikan", "Japan Folk Crafts Museum (Mingeikan)", "tokyo", "Asia/Tokyo",
            35.6611, 139.6790, "craft_gallery", open_h=10, close_h=17,
            closed_weekdays=(1,),  # Open Mondays!
            tags=("indoor", "minimalist_craft", "quiet", "stroller_friendly"),
            cost=12, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_teamlab", "teamLab Planets", "tokyo", "Asia/Tokyo",
            35.6491, 139.7898, "interactive_art", open_h=9, close_h=21,
            tags=("indoor", "crowded_tourist", "loud", "once_immersive"),
            cost=35, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_meiji_shrine", "Meiji Jingu Shrine", "tokyo", "Asia/Tokyo",
            35.6764, 139.6993, "shrine", open_h=6, close_h=18,
            tags=("outdoor", "walking_heavy", "quiet"),
            cost=0, rating=4.7, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_shinjuku_gyoen", "Shinjuku Gyoen National Garden", "tokyo", "Asia/Tokyo",
            35.6852, 139.7101, "park", open_h=9, close_h=17,
            closed_weekdays=(0,),
            tags=("outdoor", "stroller_friendly", "quiet"),
            cost=5, rating=4.7, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_yoyogi_park", "Yoyogi Park", "tokyo", "Asia/Tokyo",
            35.6717, 139.6949, "park", open_h=6, close_h=21,
            tags=("outdoor", "stroller_friendly", "relaxed"),
            cost=0, rating=4.5, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_tsukiji_hall", "Tsukiji Outer Market Food Hall", "tokyo", "Asia/Tokyo",
            35.6655, 139.7707, "food", open_h=7, close_h=15,
            tags=("crowded_tourist", "loud", "street_food"),
            dietary_tags=("pescatarian",),
            cost=25, rating=4.4, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_ain_soph", "AIN SOPH. Ginza", "tokyo", "Asia/Tokyo",
            35.6708, 139.7689, "food", open_h=11, close_h=21,
            tags=("indoor", "quiet", "stroller_friendly"),
            dietary_tags=("vegan", "vegetarian", "gluten_free"),
            cost=28, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_t_s_tantan", "T's Tantan Tokyo Station", "tokyo", "Asia/Tokyo",
            35.6812, 139.7671, "food", open_h=9, close_h=21,
            tags=("indoor", "quick_bite"),
            dietary_tags=("vegan", "vegetarian"),
            cost=14, rating=4.5, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_koffee_mameya", "Koffee Mameya Omotesando", "tokyo", "Asia/Tokyo",
            35.6669, 139.7118, "cafe", open_h=10, close_h=18,
            tags=("indoor", "minimalist_craft", "quiet", "stroller_friendly"),
            dietary_tags=("vegan", "vegetarian", "gluten_free"),
            cost=10, rating=4.7, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "tyo_station", "Tokyo Station (Shinkansen)", "tokyo", "Asia/Tokyo",
            35.6812, 139.7671, "transit_hub", open_h=5, close_h=23,
            cost=0, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        # --- KYOTO ---
        _make_place(
            "kyo_station", "Kyoto Station", "kyoto", "Asia/Tokyo",
            34.9858, 135.7588, "transit_hub", open_h=5, close_h=23,
            cost=0, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_fushimi", "Fushimi Inari Taisha", "kyoto", "Asia/Tokyo",
            34.9671, 135.7727, "shrine", open_h=6, close_h=21,
            tags=("outdoor", "walking_heavy", "crowded_tourist"),
            cost=0, rating=4.8, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_kyocera_art", "Kyoto City KYOCERA Museum of Art", "kyoto", "Asia/Tokyo",
            35.0127, 135.7836, "contemporary_art", open_h=10, close_h=18,
            closed_weekdays=(0,),  # Closed Mondays!
            tags=("indoor", "architecture", "stroller_friendly", "quiet"),
            cost=18, rating=4.7, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_craft_center", "Kyoto Museum of Crafts and Design", "kyoto", "Asia/Tokyo",
            35.0138, 135.7810, "craft_gallery", open_h=9, close_h=17,
            closed_weekdays=(),  # Open Mondays!
            tags=("indoor", "minimalist_craft", "stroller_friendly", "quiet"),
            cost=10, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_hosomi", "Hosomi Museum", "kyoto", "Asia/Tokyo",
            35.0117, 135.7800, "art_gallery", open_h=10, close_h=18,
            closed_weekdays=(0,),
            tags=("indoor", "quiet", "minimalist_craft"),
            cost=14, rating=4.5, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_philosopher", "Philosopher's Path", "kyoto", "Asia/Tokyo",
            35.0268, 135.7955, "park", open_h=6, close_h=21,
            tags=("outdoor", "quiet", "stroller_friendly"),
            cost=0, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_mumokuteki", "Mumokuteki Cafe Kyoto", "kyoto", "Asia/Tokyo",
            35.0063, 135.7664, "food", open_h=11, close_h=20,
            tags=("indoor", "quiet", "stroller_friendly"),
            dietary_tags=("vegan", "vegetarian", "gluten_free"),
            cost=20, rating=4.6, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_nishiki", "Nishiki Market", "kyoto", "Asia/Tokyo",
            35.0050, 135.7649, "food", open_h=9, close_h=18,
            tags=("crowded_tourist", "loud", "street_food"),
            dietary_tags=("pescatarian",),
            cost=22, rating=4.4, verified_dates=jun_dates, now_utc=now_utc,
        ),
        _make_place(
            "kyo_ippodo", "Ippodo Tea Kaboku Tearoom", "kyoto", "Asia/Tokyo",
            35.0145, 135.7676, "cafe", open_h=10, close_h=17,
            tags=("indoor", "quiet", "minimalist_craft", "tea_ceremony"),
            dietary_tags=("vegan", "vegetarian", "gluten_free"),
            cost=15, rating=4.8, verified_dates=jun_dates, now_utc=now_utc,
        ),
        # --- LONDON + PARIS (DST-Crossing Fixtures) ---
        _make_place(
            "lon_tate_modern", "Tate Modern", "london", "Europe/London",
            51.5076, -0.0994, "contemporary_art", open_h=10, close_h=18,
            tags=("indoor", "architecture", "stroller_friendly"),
            cost=0, rating=4.6, verified_dates=dst_dates, now_utc=now_utc,
        ),
        _make_place(
            "lon_design_mus", "Design Museum Kensington", "london", "Europe/London",
            51.4998, -0.2003, "design_museum", open_h=10, close_h=18,
            tags=("indoor", "minimalist_craft", "quiet", "stroller_friendly"),
            cost=20, rating=4.5, verified_dates=dst_dates, now_utc=now_utc,
        ),
        _make_place(
            "lon_st_pancras", "St Pancras International", "london", "Europe/London",
            51.5319, -0.1263, "transit_hub", open_h=5, close_h=23,
            cost=0, rating=4.6, verified_dates=dst_dates, now_utc=now_utc,
        ),
        _make_place(
            "par_gare_nord", "Paris Gare du Nord", "paris", "Europe/Paris",
            48.8809, 2.3553, "transit_hub", open_h=5, close_h=23,
            cost=0, rating=4.1, verified_dates=dst_dates, now_utc=now_utc,
        ),
        _make_place(
            "par_pompidou", "Centre Pompidou", "paris", "Europe/Paris",
            48.8606, 2.3522, "contemporary_art", open_h=11, close_h=21,
            closed_weekdays=(1,),
            tags=("indoor", "architecture", "crowded_tourist"),
            cost=18, rating=4.5, verified_dates=dst_dates, now_utc=now_utc,
        ),
        _make_place(
            "par_palais_tokyo", "Palais de Tokyo", "paris", "Europe/Paris",
            48.8641, 2.2974, "contemporary_art", open_h=12, close_h=22,
            closed_weekdays=(1,),
            tags=("indoor", "minimalist_craft", "quiet"),
            cost=16, rating=4.4, verified_dates=dst_dates, now_utc=now_utc,
        ),
    ]
    return {p.place_id: p for p in raw}


class FixtureGroundingProvider:
    """Zero-key Tier A GroundingProvider backed by verified multi-city fixtures."""

    def __init__(
        self,
        places: dict[str, Place] | None = None,
        *,
        now_utc: int = DEFAULT_FIXTURE_NOW_UTC,
        public_holidays: dict[str, frozenset[date]] | None = None,
    ) -> None:
        self._now_utc = now_utc
        self._places: dict[str, Place] = (
            dict(places) if places is not None else build_default_places(now_utc)
        )
        self._etags: dict[str, str] = {pid: f"fix_v1_{pid}" for pid in self._places}
        self._holidays: dict[str, frozenset[date]] = public_holidays or {
            "tokyo": frozenset({date(2026, 7, 20)}),
            "kyoto": frozenset({date(2026, 7, 20)}),
            "london": frozenset({date(2026, 8, 31)}),
            "paris": frozenset({date(2026, 7, 14)}),
        }
        self._tz_reg = PinnedTzRegistry()

    def set_etag(self, place_id: str, new_etag: str) -> None:
        self._etags[place_id] = new_etag

    def get_place(self, place_id: str) -> GroundedRecord[Place] | None:
        p = self._places.get(place_id)
        if p is None:
            return None
        return GroundedRecord(
            payload=p,
            source="verified_fixture_v1",
            fetched_at_utc=self._now_utc - 600,
            ttl_seconds=604800,
            etag=self._etags[place_id],
        )

    def all_places(self) -> dict[str, Place]:
        return dict(self._places)

    def search_candidates(
        self, intent: CandidateSearchIntent, *, limit: int = 20
    ) -> list[GroundedRecord[Place]]:
        target_cats = set(intent.target_categories)
        req_diet = set(intent.required_dietary_tags)
        avoid_tags = set(intent.avoid_tags)
        pref_tags = set(intent.preferred_tags)

        matched: list[tuple[float, Place]] = []
        for p in self._places.values():
            if p.city_id != intent.city_id:
                continue
            if p.place_id in intent.excluded_place_ids:
                continue
            if p.category not in target_cats:
                continue
            if req_diet and not req_diet.issubset(set(p.dietary_tags)):
                continue
            if intent.max_cost_usd is not None and p.estimated_cost_usd > intent.max_cost_usd:
                continue
            if avoid_tags and (avoid_tags & set(p.tags)):
                continue

            dist_km = 0.0
            if intent.near_lat is not None and intent.near_lon is not None:
                dist_km = haversine_km(intent.near_lat, intent.near_lon, p.lat, p.lon)
                if intent.max_radius_km is not None and dist_km > intent.max_radius_km:
                    continue

            # Deterministic tie-breaking sort for reproducible catalog retrieval
            tag_hits = len(pref_tags & set(p.tags))
            score = tag_hits * 2.0 + p.rating - dist_km * 0.15
            matched.append((score, p))

        matched.sort(key=lambda pair: (-pair[0], pair[1].place_id))
        return [
            GroundedRecord(
                payload=p,
                source="verified_fixture_v1",
                fetched_at_utc=self._now_utc - 600,
                ttl_seconds=604800,
                etag=self._etags[p.place_id],
            )
            for _, p in matched[:limit]
        ]

    def transit_minutes(
        self, origin_id: str, dest_id: str, mode: TransitMode = TransitMode.METRO
    ) -> int:
        if origin_id == dest_id:
            return 0
        p1 = self._places.get(origin_id)
        p2 = self._places.get(dest_id)
        if p1 is None or p2 is None:
            return 25
        km = haversine_km(p1.lat, p1.lon, p2.lat, p2.lon)
        if mode == TransitMode.WALK:
            return max(5, int(round(km * 14.0)))
        if mode == TransitMode.DRIVE:
            return max(8, int(round(km * 3.5 + 5)))
        # Urban metro/transit: base wait + speed
        return max(10, int(round(km * 4.0 + 10)))

    def get_rail_slots(
        self, origin_station_id: str, dest_station_id: str, service_date: date
    ) -> list[RailTimetableSlot]:
        if (origin_station_id, dest_station_id) == ("tyo_station", "kyo_station"):
            tz = self._tz_reg.get("Asia/Tokyo")
            slots: list[RailTimetableSlot] = []
            for dep_h in (7, 9, 11, 13, 15, 17, 19):
                dep_u = resolve_local_to_utc(service_date, time(dep_h, 0), tz).utc_epoch
                assert dep_u is not None
                slots.append(
                    RailTimetableSlot(
                        slot_id=f"nozomi_{service_date.isoformat()}_{dep_h:02d}00",
                        origin_station_id=origin_station_id,
                        dest_station_id=dest_station_id,
                        service_date=service_date,
                        depart_utc=dep_u,
                        arrive_utc=dep_u + 135 * 60,  # 2h 15m Shinkansen
                        cost_usd=95,
                    )
                )
            return slots
        if (origin_station_id, dest_station_id) == ("lon_st_pancras", "par_gare_nord"):
            tz = self._tz_reg.get("Europe/London")
            slots = []
            for dep_h in (8, 10, 12, 15, 18):
                dep_u = resolve_local_to_utc(service_date, time(dep_h, 0), tz).utc_epoch
                assert dep_u is not None
                slots.append(
                    RailTimetableSlot(
                        slot_id=f"eurostar_{service_date.isoformat()}_{dep_h:02d}00",
                        origin_station_id=origin_station_id,
                        dest_station_id=dest_station_id,
                        service_date=service_date,
                        depart_utc=dep_u,
                        arrive_utc=dep_u + 140 * 60,  # 2h 20m Eurostar
                        cost_usd=130,
                    )
                )
            return slots
        return []

    def get_public_holidays(self, city_id: str) -> frozenset[date]:
        return self._holidays.get(city_id, frozenset())
