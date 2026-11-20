"""Live HTTP walkthrough against http://127.0.0.1:8000 (TripGraph v7)."""

from __future__ import annotations

from datetime import date, time
import json
import sys
import uuid

import httpx

from tripgraph.schema.models import (
    ActivityNode,
    CityStay,
    HotelBookingContract,
    NodeTier,
    TripGraph,
)
from tripgraph.time.tz_registry import PinnedTzRegistry, resolve_local_to_utc

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8000"
D_SUN = date(2026, 6, 14)
D_MON = date(2026, 6, 15)


def _tyo_utc(d: date, h: int, m: int = 0) -> int:
    res = resolve_local_to_utc(d, time(h, m), PinnedTzRegistry().get("Asia/Tokyo"))
    assert res.utc_epoch is not None
    return res.utc_epoch


def build_demo_trip(trip_id: str) -> TripGraph:
    stay = CityStay(
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
    acts = (
        ActivityNode(
            id="a_sun_park", stay_id="s_tyo", city_id="tokyo", place_id="tyo_yoyogi_park",
            local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 10, 0), end_utc=_tyo_utc(D_SUN, 12, 0),
            duration_min=120, category="park", node_tier=NodeTier.ESSENTIAL,
        ),
        ActivityNode(
            id="a_sun_cafe", stay_id="s_tyo", city_id="tokyo", place_id="tyo_koffee_mameya",
            local_date=D_SUN, start_utc=_tyo_utc(D_SUN, 14, 0), end_utc=_tyo_utc(D_SUN, 15, 0),
            duration_min=60, category="cafe", node_tier=NodeTier.OPTIONAL,
        ),
        ActivityNode(
            id="a_mon_shrine", stay_id="s_tyo", city_id="tokyo", place_id="tyo_meiji_shrine",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 9, 0), end_utc=_tyo_utc(D_MON, 10, 30),
            duration_min=90, category="shrine", locked_by_user=True, node_tier=NodeTier.CRITICAL,
        ),
        ActivityNode(
            id="a_mon_art", stay_id="s_tyo", city_id="tokyo", place_id="tyo_mori_art",
            local_date=D_MON, start_utc=_tyo_utc(D_MON, 11, 30), end_utc=_tyo_utc(D_MON, 13, 30),
            duration_min=120, category="contemporary_art", node_tier=NodeTier.OPTIONAL,
        ),
    )
    return TripGraph(trip_id=trip_id, stays=(stay,), activities=acts)


def main() -> None:
    try:
        httpx.get(f"{BASE_URL}/health", timeout=35.0).raise_for_status()
    except httpx.HTTPError:
        print("Server not running at http://127.0.0.1:8000. Start it with `make serve` first.")
        sys.exit(1)

    tid = f"demo_tokyo_{uuid.uuid4().hex[:6]}"
    trip = build_demo_trip(tid)
    print("\n=== 1. POST /trips (Tokyo trip with Monday-closed Mori Art Museum) ===")
    r1 = httpx.post(f"{BASE_URL}/trips", json=trip.model_dump(mode="json"))
    print(f"HTTP {r1.status_code}: {r1.json()}")

    print(f"\n=== 2. POST /trips/{tid}/verify (K3 Verifier catches closure) ===")
    r2 = httpx.post(f"{BASE_URL}/trips/{tid}/verify")
    print(f"HTTP {r2.status_code}: {json.dumps(r2.json(), indent=2)}")

    print(f"\n=== 3. PATCH /trips/{tid}/edit (Subprocess CP-SAT + S2_taxonomy) ===")
    r3 = httpx.patch(
        f"{BASE_URL}/trips/{tid}/edit",
        json={
            "edit": {
                "edit_type": "move_activity",
                "activity_id": "a_mon_art",
                "target_stay_id": "s_tyo",
                "target_local_date": D_MON.isoformat(),
                "target_start_utc": _tyo_utc(D_MON, 12, 0),
            }
        },
        timeout=10.0,
    )
    b3 = r3.json()
    if r3.status_code != 200 or "graph" not in b3:
        print(f"HTTP {r3.status_code}:", json.dumps(b3, indent=2))
        sys.exit(1)
    swapped = next(a["place_id"] for a in b3["graph"]["activities"] if a["id"] == "a_mon_art")
    print(f"HTTP {r3.status_code} | version={b3['version']} | mode={b3['recovery_mode']}")
    print(f"  solver_certificate      : {b3['solver_certificate']}")
    print(f"  verification_certificate: {b3['verification_certificate']}")
    print(f"  a_mon_art swapped to    : {swapped} (Sunday stayed SHA-256 frozen)")
    print(f"  blast_radius            : {b3['blast_radius']}")

    print(f"\n=== 4. PATCH /trips/{tid}/edit (Shift +2 days -> >50% dirty -> 202 + SSE) ===")
    r4 = httpx.patch(
        f"{BASE_URL}/trips/{tid}/edit",
        json={"edit": {"edit_type": "shift_start_date", "delta_days": 2}},
        timeout=10.0,
    )
    b4 = r4.json()
    print(f"HTTP {r4.status_code}: {json.dumps(b4, indent=2)}")

    print(f"\n=== 5. Streaming live SSE from {b4['events_url']} ===")
    with httpx.stream("GET", f"{BASE_URL}{b4['events_url']}", timeout=15.0) as resp:
        for line in resp.iter_lines():
            if line:
                print(" ", line)

    r5 = httpx.get(f"{BASE_URL}/trips/{tid}")
    print(f"\nFinal committed trip version: {r5.json()['version']}")


if __name__ == "__main__":
    main()
