"""Hardened 2-city Tokyo -> Kyoto HTTP walkthrough for TripGraph v7.

Clearly discloses seeded fixture evidence, validates every HTTP status and
certificate contract, and handles 409/500/503/timeout failures cleanly.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
import json
import sys
import uuid

import httpx

from tripgraph.grounding.fixtures import DEFAULT_FIXTURE_NOW_UTC
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

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
D_SUN = date(2026, 6, 14)
D_MON = date(2026, 6, 15)
D_TUE = date(2026, 6, 16)
D_WED = date(2026, 6, 17)
D_THU = date(2026, 6, 18)


def _jst_utc(d: date, h: int, m: int = 0) -> int:
    res = resolve_local_to_utc(d, time(h, m), PinnedTzRegistry().get("Asia/Tokyo"))
    assert res.utc_epoch is not None
    return res.utc_epoch


def build_demo_trip(trip_id: str) -> TripGraph:
    """Build a 5-day, 2-city Tokyo -> Kyoto itinerary with Shinkansen transit leg."""
    stay_tokyo = CityStay(
        id="s_tyo",
        city_id="tokyo",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_tyo",
            hotel_name="Roppongi Central",
            tz_iana="Asia/Tokyo",
            check_in_date=D_SUN,
            check_out_date=D_TUE,
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    stay_kyoto = CityStay(
        id="s_kyo",
        city_id="kyoto",
        tz_iana="Asia/Tokyo",
        hotel=HotelBookingContract(
            hotel_id="h_kyo",
            hotel_name="Gion Ryokan",
            tz_iana="Asia/Tokyo",
            check_in_date=D_TUE,
            check_out_date=D_THU,
            guarantee_type="CARD_GUARANTEED",
            access_method="STAFFED_24H",
        ),
    )
    shinkansen = TransitLeg(
        id="leg_tyo_kyo",
        from_stay_id="s_tyo",
        to_stay_id="s_kyo",
        origin_place_id="tyo_station",
        dest_place_id="kyo_station",
        mode=TransitMode.RAIL,
        depart_utc=_jst_utc(D_TUE, 13, 0),
        arrive_utc=_jst_utc(D_TUE, 15, 15),
        carrier_slot_id="nozomi_215",
        cost_usd=95,
    )
    acts = (
        # Tokyo Day 1 (Sun Jun 14 — UNTOUCHED during Monday edit; SHA-256 frozen)
        ActivityNode(
            id="a_sun_park", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_jst_utc(D_SUN, 10, 0), end_utc=_jst_utc(D_SUN, 12, 0),
            duration_min=120, category="park", node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="a_sun_cafe", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
            local_date=D_SUN, start_utc=_jst_utc(D_SUN, 14, 0), end_utc=_jst_utc(D_SUN, 15, 0),
            duration_min=60, category="cafe", node_tier=NodeTier.OPTIONAL,
        ),
        # Tokyo Day 2 (Mon Jun 15 — DIRTY DAY: locked shrine + Monday-closed Mori Art Museum)
        ActivityNode(
            id="a_mon_shrine", stay_id="s_tyo", city_id="tokyo", place_id="tyo_meiji_shrine",
            local_date=D_MON, start_utc=_jst_utc(D_MON, 9, 0), end_utc=_jst_utc(D_MON, 10, 30),
            duration_min=90, category="shrine", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="a_mon_art", stay_id="s_tyo", city_id="tokyo", place_id="tyo_mori_art",
            local_date=D_MON, start_utc=_jst_utc(D_MON, 11, 30), end_utc=_jst_utc(D_MON, 13, 30),
            duration_min=120, category="contemporary_art", node_tier=NodeTier.OPTIONAL,
        ),
        # Kyoto Day 3 & Day 4 (Tue–Wed Jun 16–17 — UNTOUCHED during Monday edit; SHA-256 frozen)
        ActivityNode(
            id="a_tue_fushimi", stay_id="s_kyo", city_id="kyoto", place_id="kyo_fushimi",
            local_date=D_TUE, start_utc=_jst_utc(D_TUE, 16, 30), end_utc=_jst_utc(D_TUE, 18, 0),
            duration_min=90, category="shrine", node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="a_wed_art", stay_id="s_kyo", city_id="kyoto", place_id="kyo_kyocera_art",
            local_date=D_WED, start_utc=_jst_utc(D_WED, 10, 0), end_utc=_jst_utc(D_WED, 12, 0),
            duration_min=120, category="contemporary_art", node_tier=NodeTier.OPTIONAL,
        ),
    )
    return TripGraph(
        trip_id=trip_id,
        stays=(stay_tokyo, stay_kyoto),
        transit_legs=(shinkansen,),
        activities=acts,
    )


def _safe_json(resp: httpx.Response) -> dict:
    try:
        return resp.json()
    except ValueError:
        return {"raw_text": resp.text}


def _require_status(step: str, resp: httpx.Response, expected: int) -> dict:
    body = _safe_json(resp)
    if resp.status_code != expected:
        print(f"[FAIL] {step}: expected HTTP {expected}, got HTTP {resp.status_code}")
        print(json.dumps(body, indent=2))
        sys.exit(1)
    return body


def main() -> None:
    fixture_clock_iso = datetime.fromtimestamp(DEFAULT_FIXTURE_NOW_UTC, tz=timezone.utc).isoformat()
    print("=" * 78)
    print("DETERMINISTIC FIXTURE SCENARIO -- SEEDED VENUE EVIDENCE")
    print(f"  Demo API Endpoint : {BASE_URL}")
    print(f"  Fixture Clock     : {fixture_clock_iso}")
    print("  Itinerary Scope   : 2 cities (Tokyo -> Kyoto), 2 hotels, 1 Shinkansen leg, 6 activities")
    print("=" * 78)

    try:
        health = httpx.get(f"{BASE_URL}/health", timeout=45.0)
        health.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[ERROR] Health check failed for {BASE_URL}: {exc}")
        print("        Start locally with `make serve` or wait for cold start.")
        sys.exit(1)

    tid = f"demo_tyo_kyo_{uuid.uuid4().hex[:6]}"
    trip = build_demo_trip(tid)

    # 1. Create 2-city trip
    print("\n=== 1. POST /trips (5-day Tokyo -> Kyoto trip; Monday Mori Art closed in fixture) ===")
    r1 = httpx.post(f"{BASE_URL}/trips", json=trip.model_dump(mode="json"), timeout=15.0)
    b1 = _require_status("POST /trips", r1, 201)
    print(f"HTTP 201: trip_id={b1['trip_id']} | version={b1['version']}")

    # 2. Verify with Kleene K3
    print(f"\n=== 2. POST /trips/{tid}/verify (Independent K3 Verifier) ===")
    r2 = httpx.post(f"{BASE_URL}/trips/{tid}/verify", timeout=15.0)
    b2 = _require_status("POST /verify", r2, 200)
    assert b2["verification_certificate"] == "VIOLATED_FALSE", f"Unexpected verify cert: {b2}"
    print(f"HTTP 200 | status={b2['hard_constraints_status']} | cert={b2['verification_certificate']}")
    for v in b2["violations"]:
        print(f"  violation: {v['check_id']} -> {v['code']} ({v['detail']})")

    # 3. Localized Monday repair (Tokyo Day 1, Shinkansen, and all Kyoto stays stay frozen)
    print(f"\n=== 3. PATCH /trips/{tid}/edit (Localized Monday CP-SAT + S2_taxonomy Repair) ===")
    r3 = httpx.patch(
        f"{BASE_URL}/trips/{tid}/edit",
        json={
            "edit": {
                "edit_type": "move_activity",
                "activity_id": "a_mon_art",
                "target_stay_id": "s_tyo",
                "target_local_date": D_MON.isoformat(),
                "target_start_utc": _jst_utc(D_MON, 12, 0),
            }
        },
        timeout=15.0,
    )
    b3 = _require_status("PATCH /edit (localized)", r3, 200)
    assert b3["blast_radius"]["semantic"] == 0.0 and b3["blast_radius"]["temporal"] == 0.0
    swapped = next(a["place_id"] for a in b3["graph"]["activities"] if a["id"] == "a_mon_art")
    print(f"HTTP 200 | version={b3['version']} | mode={b3['recovery_mode']}")
    print(f"  solver_certificate      : {b3['solver_certificate']}")
    print(f"  verification_certificate: {b3['verification_certificate']}")
    print(f"  a_mon_art swapped to    : {swapped}")
    print(f"  blast_radius (outside dirty Monday): {b3['blast_radius']} (Sun Tokyo + Shinkansen + Kyoto frozen)")

    # 4. Default ShiftStartDateEdit MUST refuse honestly because a_mon_shrine is locked_by_user=True
    print(f"\n=== 4. PATCH /trips/{tid}/edit (Default Shift +2 days with locked Meiji Shrine -> 409) ===")
    r4 = httpx.patch(
        f"{BASE_URL}/trips/{tid}/edit",
        json={"edit": {"edit_type": "shift_start_date", "delta_days": 2}},
        timeout=15.0,
    )
    b4 = _require_status("PATCH /edit (locked shift refusal)", r4, 409)
    assert b4["status"] == "LOCKED_NODE_CONFLICT"
    print(f"HTTP 409 | status={b4['status']} | locked_activities={b4['locked_activities']}")

    # 5. Explicit opt-in allow_shift_locked=True -> 202 Accepted + SSE + LockGuard + K3 Verification
    print(f"\n=== 5. PATCH /trips/{tid}/edit (Shift +2 days with allow_shift_locked=true -> 202 + SSE) ===")
    r5 = httpx.patch(
        f"{BASE_URL}/trips/{tid}/edit",
        json={"edit": {"edit_type": "shift_start_date", "delta_days": 2, "allow_shift_locked": True}},
        timeout=15.0,
    )
    b5 = _require_status("PATCH /edit (global shift opt-in)", r5, 202)
    events_url = b5.get("events_url")
    if not events_url:
        print("[FAIL] Missing events_url in 202 response:", json.dumps(b5, indent=2))
        sys.exit(1)
    print(f"HTTP 202 | mode={b5['mode']} | dirty_ratio={b5['dirty_ratio']:.2f} | job_id={b5['job_id']}")

    completed_event = None
    failed_event = None
    try:
        with httpx.stream("GET", f"{BASE_URL}{events_url}", timeout=35.0) as resp:
            if resp.status_code != 200:
                print(f"[FAIL] SSE stream returned HTTP {resp.status_code}")
                sys.exit(1)
            for line in resp.iter_lines():
                if not line:
                    continue
                print(" ", line)
                if line.startswith("data:"):
                    evt = json.loads(line.removeprefix("data:").strip())
                    if evt.get("stage") == "completed":
                        completed_event = evt
                    elif evt.get("stage") == "failed":
                        failed_event = evt
    except httpx.HTTPError as exc:
        print(f"[ERROR] SSE stream interrupted: {exc}")
        sys.exit(1)

    if failed_event is not None or completed_event is None:
        print("[FAIL] Global replan did not complete cleanly:", failed_event)
        sys.exit(1)

    r6 = httpx.get(f"{BASE_URL}/trips/{tid}", timeout=15.0)
    b6 = _require_status("GET /trips/{id}", r6, 200)
    leg = b6["graph"]["transit_legs"][0]
    kyo_stay = next(s for s in b6["graph"]["stays"] if s["id"] == "s_kyo")
    print(
        f"\nFinal committed version={b6['version']} | "
        f"Kyoto stay={kyo_stay['hotel']['check_in_date']}..{kyo_stay['hotel']['check_out_date']} | "
        f"Shinkansen depart_utc={leg['depart_utc']} | cert={completed_event.get('verification_certificate')}"
    )


if __name__ == "__main__":
    main()
