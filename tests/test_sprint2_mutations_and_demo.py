"""Sprint 2 Mutation, Calendar/DST Integrity & Multi-City Demo Regression Tests."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import json

import httpx
import pytest

from tripgraph.api.main import create_app
from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC, FixtureGroundingProvider
from tripgraph.runtime.orchestrator import TripOrchestrator, apply_direct_mutation
from tripgraph.runtime.store import FencedTripStore
from tripgraph.runtime.supervisor import SolverProcessSupervisor
from tripgraph.schema.edits import ExtendStayEdit, MoveActivityEdit, ShiftStartDateEdit
from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    HotelBookingContract,
    NodeTier,
    TransitLeg,
    TransitMode,
    TripGraph,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

# Import multi-city builder directly from scripts/live_demo.py
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "live_demo_mod", Path(__file__).resolve().parents[1] / "scripts" / "live_demo.py"
)
assert _spec is not None and _spec.loader is not None
_live_demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_live_demo)


def _utc(tz_iana: str, d: date, h: int, m: int = 0) -> int:
    reg = PinnedTzRegistry()
    res = resolve_local_to_utc(d, time(h, m), reg.get(tz_iana))
    assert res.utc_epoch is not None
    return res.utc_epoch


def _local_hm(utc_epoch: int, tz_iana: str) -> tuple[date, int, int]:
    tz = PinnedTzRegistry().get(tz_iana).tzinfo
    dt = datetime.fromtimestamp(utc_epoch, tz=timezone.utc).astimezone(tz)
    return dt.date(), dt.hour, dt.minute


def test_dst_spring_forward_and_fall_back_shift_preserves_local_wall_clock_and_transit() -> None:
    """ShiftStartDateEdit across America/New_York DST preserves wall-clock time, not +86400s."""
    reg = PinnedTzRegistry()
    # US 2026 spring-forward occurs Sunday 2026-03-08 02:00 -> 03:00 (23-hour day)
    d_sat = date(2026, 3, 7)
    d_sun = date(2026, 3, 8)
    d_mon = date(2026, 3, 9)

    stay1 = CityStay(
        id="s_nyc", city_id="nyc", tz_iana="America/New_York",
        hotel=HotelBookingContract(
            hotel_id="h_nyc", hotel_name="Midtown", tz_iana="America/New_York",
            check_in_date=d_sat, check_out_date=d_sun,
            guarantee_type="CARD_GUARANTEED", access_method="STAFFED_24H",
        ),
    )
    stay2 = CityStay(
        id="s_bos", city_id="boston", tz_iana="America/New_York",
        hotel=HotelBookingContract(
            hotel_id="h_bos", hotel_name="Back Bay", tz_iana="America/New_York",
            check_in_date=d_sun, check_out_date=d_mon,
            guarantee_type="CARD_GUARANTEED", access_method="STAFFED_24H",
        ),
    )
    leg = TransitLeg(
        id="leg_amtrak", from_stay_id="s_nyc", to_stay_id="s_bos",
        origin_place_id="nyc_penn", dest_place_id="bos_south",
        mode=TransitMode.RAIL,
        depart_utc=_utc("America/New_York", d_sat, 14, 0),
        arrive_utc=_utc("America/New_York", d_sat, 17, 30),
        carrier_slot_id="acela_2150",
    )
    act = ActivityNode(
        id="a_nyc_1", stay_id="s_nyc", city_id="nyc", place_id="nyc_moma",
        local_date=d_sat,
        start_utc=_utc("America/New_York", d_sat, 10, 0),
        end_utc=_utc("America/New_York", d_sat, 12, 0),
        duration_min=120, category="museum",
    )
    trip = TripGraph(trip_id="t_dst", stays=(stay1, stay2), transit_legs=(leg,), activities=(act,))

    # Shift +2 days across spring-forward (Sat Mar 7 EST -> Mon Mar 9 EDT)
    shifted = apply_direct_mutation(trip, ShiftStartDateEdit(delta_days=2, allow_shift_locked=True), reg)

    a_new = shifted.activities[0]
    assert _local_hm(a_new.start_utc, "America/New_York") == (date(2026, 3, 9), 10, 0)
    assert _local_hm(a_new.end_utc, "America/New_York") == (date(2026, 3, 9), 12, 0)
    # Must be 2*86400 - 3600 (NOT naive 2*86400)!
    assert a_new.start_utc - act.start_utc == 2 * 86400 - 3600

    l_new = shifted.transit_legs[0]
    assert _local_hm(l_new.depart_utc, "America/New_York") == (date(2026, 3, 9), 14, 0)
    assert _local_hm(l_new.arrive_utc, "America/New_York") == (date(2026, 3, 9), 17, 30)
    assert (l_new.arrive_utc - l_new.depart_utc) == (leg.arrive_utc - leg.depart_utc)

    # Now shift across fall-back (Sat Oct 31 2026 EDT -> Mon Nov 2 2026 EST; 25-hour day)
    d_oct31 = date(2026, 10, 31)
    d_nov1 = date(2026, 11, 1)
    fall_stay = CityStay(
        id="s_nyc_f", city_id="nyc", tz_iana="America/New_York",
        hotel=HotelBookingContract(
            hotel_id="h_nyc", hotel_name="Midtown", tz_iana="America/New_York",
            check_in_date=d_oct31, check_out_date=d_nov1,
            guarantee_type="CARD_GUARANTEED", access_method="STAFFED_24H",
        ),
    )
    fall_act = ActivityNode(
        id="a_fall", stay_id="s_nyc_f", city_id="nyc", place_id="nyc_moma",
        local_date=d_oct31,
        start_utc=_utc("America/New_York", d_oct31, 10, 0),
        end_utc=_utc("America/New_York", d_oct31, 12, 0),
        duration_min=120, category="museum",
    )
    fall_trip = TripGraph(trip_id="t_dst_fall", stays=(fall_stay,), activities=(fall_act,))
    fall_shifted = apply_direct_mutation(fall_trip, ShiftStartDateEdit(delta_days=2, allow_shift_locked=True), reg)
    assert _local_hm(fall_shifted.activities[0].start_utc, "America/New_York") == (date(2026, 11, 2), 10, 0)
    assert fall_shifted.activities[0].start_utc - fall_act.start_utc == 2 * 86400 + 3600


def test_extend_stay_propagates_to_downstream_stays_activities_and_transit_legs() -> None:
    """ExtendStayEdit shifts downstream hotels, downstream activities, and departing/downstream legs."""
    reg = PinnedTzRegistry()
    trip = _live_demo.build_demo_trip("t_extend")

    extended = apply_direct_mutation(trip, ExtendStayEdit(stay_id="s_tyo", delta_nights=1), reg)

    s_tyo = next(s for s in extended.stays if s.id == "s_tyo")
    s_kyo = next(s for s in extended.stays if s.id == "s_kyo")
    assert s_tyo.check_in_date == date(2026, 6, 14)
    assert s_tyo.check_out_date == date(2026, 6, 17)
    assert s_kyo.check_in_date == date(2026, 6, 17)
    assert s_kyo.check_out_date == date(2026, 6, 19)

    # Tokyo activities stay put
    a_sun = next(a for a in extended.activities if a.id == "a_sun_park")
    assert a_sun.local_date == date(2026, 6, 14)

    # Shinkansen departing Tokyo -> Kyoto shifted +1 day preserving 13:00-15:15 JST
    leg = extended.transit_legs[0]
    assert _local_hm(leg.depart_utc, "Asia/Tokyo") == (date(2026, 6, 17), 13, 0)
    assert _local_hm(leg.arrive_utc, "Asia/Tokyo") == (date(2026, 6, 17), 15, 15)

    # Kyoto downstream activities shifted +1 day preserving wall-clock time
    a_tue = next(a for a in extended.activities if a.id == "a_tue_fushimi")
    a_wed = next(a for a in extended.activities if a.id == "a_wed_art")
    assert a_tue.local_date == date(2026, 6, 17)
    assert _local_hm(a_tue.start_utc, "Asia/Tokyo") == (date(2026, 6, 17), 16, 30)
    assert a_wed.local_date == date(2026, 6, 18)
    assert _local_hm(a_wed.start_utc, "Asia/Tokyo") == (date(2026, 6, 18), 10, 0)


@pytest.mark.asyncio
async def test_cross_city_move_updates_city_id_and_swaps_to_valid_kyoto_venue() -> None:
    """Moving a Tokyo activity into Kyoto updates city_id and swaps to a verified Kyoto venue."""
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
        trip = _live_demo.build_demo_trip("t_cross_city")
        store.create_trip(trip)

        # Move Tokyo's a_mon_art (contemporary_art) across cities into Kyoto on Wed Jun 17
        out = await orch.apply_edit(
            trip_id="t_cross_city",
            edit=MoveActivityEdit(
                activity_id="a_mon_art",
                target_stay_id="s_kyo",
                target_local_date=date(2026, 6, 17),
                target_start_utc=None,  # Verify wall-clock preserved when start omitted
            ),
        )
        assert out.http_status == 200, f"Unexpected cross-city response: {out}"
        assert out.payload["verification_certificate"] == "VERIFIED_TRUE"
        moved = next(a for a in out.payload["graph"]["activities"] if a["id"] == "a_mon_art")
        assert moved["stay_id"] == "s_kyo"
        assert moved["city_id"] == "kyoto"
        # Old Tokyo place_id MUST be replaced by an actual Kyoto venue!
        assert moved["place_id"].startswith("kyo_")
        assert moved["place_id"] != "tyo_mori_art"
    finally:
        await sup.shutdown()
        store.close()


@pytest.mark.asyncio
async def test_hardened_multicity_tokyo_kyoto_demo_end_to_end() -> None:
    """Full end-to-end verification of the exact 2-city Tokyo -> Kyoto demo sequence."""
    app = create_app(db_path=":memory:", max_solver_workers=1)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://demo") as client:
            trip = _live_demo.build_demo_trip("t_demo_e2e")
            assert len(trip.stays) == 2
            assert len(trip.transit_legs) == 1
            assert len(trip.activities) == 6

            r1 = await client.post("/trips", json=trip.model_dump(mode="json"))
            assert r1.status_code == 201

            r2 = await client.post("/trips/t_demo_e2e/verify")
            assert r2.status_code == 200
            assert r2.json()["verification_certificate"] == "VIOLATED_FALSE"

            # Localized Monday repair -> 0.0 blast radius on Sun Tokyo, Shinkansen, & Kyoto
            r3 = await client.patch(
                "/trips/t_demo_e2e/edit",
                json={
                    "edit": {
                        "edit_type": "move_activity",
                        "activity_id": "a_mon_art",
                        "target_stay_id": "s_tyo",
                        "target_local_date": "2026-06-15",
                        "target_start_utc": _utc("Asia/Tokyo", date(2026, 6, 15), 12, 0),
                    }
                },
            )
            assert r3.status_code == 200
            b3 = r3.json()
            assert b3["version"] == 2
            assert b3["verification_certificate"] == "VERIFIED_TRUE"
            assert b3["blast_radius"] == {"semantic": 0.0, "temporal": 0.0}

            # Default ShiftStartDateEdit refused because a_mon_shrine is locked
            r4 = await client.patch(
                "/trips/t_demo_e2e/edit",
                json={"edit": {"edit_type": "shift_start_date", "delta_days": 2}},
            )
            assert r4.status_code == 409
            assert r4.json()["status"] == "LOCKED_NODE_CONFLICT"

            # Explicit allow_shift_locked=True -> 202 + SSE + LockGuard + K3 VERIFIED_TRUE
            r5 = await client.patch(
                "/trips/t_demo_e2e/edit",
                json={"edit": {"edit_type": "shift_start_date", "delta_days": 2, "allow_shift_locked": True}},
            )
            assert r5.status_code == 202
            r_sse = await client.get(r5.json()["events_url"])
            assert r_sse.status_code == 200
            assert '"stage": "completed"' in r_sse.text
            assert '"verification_certificate": "VERIFIED_TRUE"' in r_sse.text

            r6 = await client.get("/trips/t_demo_e2e")
            assert r6.status_code == 200
            assert r6.json()["version"] == 3
            kyo_stay = next(s for s in r6.json()["graph"]["stays"] if s["id"] == "s_kyo")
            assert kyo_stay["hotel"]["check_in_date"] == "2026-06-18"
            leg = r6.json()["graph"]["transit_legs"][0]
            assert _local_hm(leg["depart_utc"], "Asia/Tokyo") == (date(2026, 6, 18), 13, 0)
