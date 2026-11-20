"""Zero-shared-code independent reference oracle (TripGraph v7).

CRITICAL INVARIANT:
- MUST NOT import anything from `tripgraph` (enforced by AST CI test).
- Uses `dateutil.tz` and explicit brute-force DST gap scanning rather than
  `src/tripgraph/time/tz_registry.py` to prevent common-mode bugs.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from dateutil import tz as dateutil_tz


def parse_iso_utc(ts: str) -> int:
    """Parse an ISO-8601 UTC string (e.g. '2026-06-12T22:30:00Z') to epoch seconds."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return int(dt.astimezone(timezone.utc).timestamp())


def _parse_hhmm(s: str) -> time:
    parts = [int(p) for p in s.split(":")]
    return time(parts[0], parts[1], parts[2] if len(parts) > 2 else 0)


def oracle_resolve_local_utc(
    d: date,
    t: time,
    tz_iana: str,
    *,
    fold_policy: str = "fold_0",
    gap_policy: str = "shift_forward",
) -> int | None:
    """Independent DST resolver using dateutil.tz + brute-force minute gap scan."""
    tzinfo = dateutil_tz.gettz(tz_iana)
    if tzinfo is None:
        raise KeyError(f"Unknown timezone in reference oracle: {tz_iana}")

    probe = datetime(d.year, d.month, d.day, t.hour, t.minute, t.second, tzinfo=tzinfo, fold=0)
    if not dateutil_tz.datetime_exists(probe):
        if gap_policy == "closed":
            return None
        # Brute-force forward scan (1-minute steps, max 4h) to first existing local wall time
        naive = datetime(d.year, d.month, d.day, t.hour, t.minute, 0)
        for step in range(1, 241):
            cand_naive = naive + timedelta(minutes=step)
            cand = cand_naive.replace(tzinfo=tzinfo, fold=0)
            if dateutil_tz.datetime_exists(cand):
                return int(cand.astimezone(timezone.utc).timestamp())
        raise RuntimeError(f"Could not resolve DST gap for {d} {t} in {tz_iana}")

    if dateutil_tz.datetime_ambiguous(probe):
        chosen = probe.replace(fold=0 if fold_policy == "fold_0" else 1)
        return int(chosen.astimezone(timezone.utc).timestamp())

    return int(probe.astimezone(timezone.utc).timestamp())


def oracle_evaluate_hotel(case_payload: dict[str, Any]) -> tuple[str, str]:
    """Independent evaluation of a hotel arrival JSON case -> (truth, state)."""
    c = case_payload["contract"]
    tz_iana = c["tz_iana"]
    d0 = date.fromisoformat(c["check_in_date"])
    d_end = date.fromisoformat(c["check_out_date"])
    arrival_utc = parse_iso_utc(case_payload["arrival_utc"])
    followup_raw = case_payload.get("followup_checkin_utc")
    followup_utc = parse_iso_utc(followup_raw) if followup_raw else None

    overrides = c.get("date_policy_overrides", {})
    ov = overrides.get(c["check_in_date"], {})

    std_ci = ov.get("standard_checkin") or c.get("standard_checkin", {"day_offset": 0, "local_time": "15:00"})
    ns_cut = ov.get("no_show_cutoff") or c.get("no_show_cutoff", {"day_offset": 1, "local_time": "06:00"})
    desk_cut = ov.get("staffed_desk_cutoff") or c.get("staffed_desk_cutoff")

    def res_off(obj: dict[str, Any], fold: str) -> int:
        val = oracle_resolve_local_utc(
            d0 + timedelta(days=obj["day_offset"]),
            _parse_hhmm(obj["local_time"]),
            tz_iana,
            fold_policy=fold,
        )
        assert val is not None
        return val

    room_ready = res_off(std_ci, "fold_0")
    eff_noshow = res_off(ns_cut, "fold_1")

    lnp = c.get("late_notify_policy", {"supported": False})
    notified_raw = c.get("guest_notified_late_at_utc")
    if lnp.get("supported") and notified_raw:
        notified_utc = parse_iso_utc(notified_raw)
        notify_by_utc = res_off(lnp["notify_by"], "fold_1")
        if notified_utc <= notify_by_utc:
            eff_noshow = res_off(lnp["extended_hold_until"], "fold_1")

    stay_checkout = oracle_resolve_local_utc(
        d_end, _parse_hhmm(c.get("standard_checkout", "11:00")), tz_iana, fold_policy="fold_0"
    )
    assert stay_checkout is not None

    tzinfo = dateutil_tz.gettz(tz_iana)
    arr_local_date = datetime.fromtimestamp(arrival_utc, tz=tzinfo).date()

    used_bag_drop = False
    if arrival_utc < room_ready:
        bdp = c.get("baggage_drop_policy", {})
        if arr_local_date == d0 and bdp.get("checkin_day_open") and bdp.get("checkin_day_close"):
            b_open = oracle_resolve_local_utc(d0, _parse_hhmm(bdp["checkin_day_open"]), tz_iana, fold_policy="fold_0")
            b_close = oracle_resolve_local_utc(d0, _parse_hhmm(bdp["checkin_day_close"]), tz_iana, fold_policy="fold_1")
            assert b_open is not None and b_close is not None
            if b_open <= arrival_utc <= b_close:
                used_bag_drop = True
                if followup_utc is None or followup_utc < room_ready:
                    return ("FALSE", "MISSING_FOLLOWUP_CHECKIN")
                t_check = followup_utc
            else:
                return ("FALSE", "MISSING_PRIOR_RESERVATION_NIGHT")
        else:
            return ("FALSE", "MISSING_PRIOR_RESERVATION_NIGHT")
    else:
        t_check = followup_utc if (followup_utc is not None and followup_utc >= arrival_utc) else arrival_utc

    if t_check >= stay_checkout:
        return ("FALSE", "ARRIVAL_AFTER_STAY_CHECKOUT")

    if t_check > eff_noshow and not c.get("multi_night_preserve_on_noshow", False):
        return ("FALSE", "NO_SHOW_CANCELLED")

    method = c["access_method"]
    if method == "STAFFED_HOURS":
        desk_utc = res_off(desk_cut, "fold_1")
        if t_check > desk_utc:
            return ("FALSE", "CHECKIN_REJECTED_DESK_CLOSED")
    elif method == "CONTACTLESS_CODE":
        cp = c["contactless_policy"]
        if not cp["pre_registered"]:
            return ("FALSE", "CONTACTLESS_NOT_PREREGISTERED")
        c_from = res_off(cp["code_valid_from"], "fold_0")
        c_exp = res_off(cp["code_activation_expiry"], "fold_1")
        if not (c_from <= t_check <= c_exp):
            return ("FALSE", "CONTACTLESS_CODE_EXPIRED_OR_INACTIVE")

    return ("TRUE", "PRE_CHECKIN_BAGGAGE_HELD_THEN_CHECKED_IN" if used_bag_drop else "ACTIVE_CHECKED_IN")


def oracle_evaluate_evidence(case_payload: dict[str, Any]) -> tuple[str, str]:
    """Independent Pareto single-witness K3 availability evaluator."""
    place = case_payload["place"]
    tz_iana = place["tz_iana"]
    tzinfo = dateutil_tz.gettz(tz_iana)

    start_utc = parse_iso_utc(case_payload["start_utc"])
    end_utc = parse_iso_utc(case_payload["end_utc"])
    now_utc = parse_iso_utc(case_payload["now_utc"])
    holidays = {date.fromisoformat(d) for d in case_payload.get("public_holidays", [])}

    start_local_date = datetime.fromtimestamp(start_utc, tz=tzinfo).date()
    prev_local_date = start_local_date - timedelta(days=1)

    usable: list[dict[str, Any]] = []
    for rec in place["evidence"]:
        if rec["verdict"] == "UNPARSEABLE":
            continue
        spec = rec["specificity"]  # 1=WEEKLY, 2=DATE_SPECIFIC
        app_d = date.fromisoformat(rec["applicable_date"]) if rec.get("applicable_date") else None
        if spec == 2:
            if app_d != start_local_date and not (
                app_d == prev_local_date and any(r.get("close_day_offset", 0) == 1 for r in rec.get("rules", []))
            ):
                continue

        fetched = parse_iso_utc(rec["fetched_at_utc"])
        age = max(0, now_utc - fetched)
        if age > rec["ttl_sec"] + rec.get("max_stale_age_sec", 86400):
            continue
        is_fresh = age <= rec["ttl_sec"]

        if rec["verdict"] == "CLOSED":
            eff = "CLOSED"
        else:
            rules = rec.get("rules", [])
            if not rules:
                eff = "OPEN"
            else:
                covered = False
                for anchor_d, overnight_only in ((prev_local_date, True), (start_local_date, False)):
                    if app_d is not None and anchor_d != app_d:
                        continue
                    for r in rules:
                        if overnight_only and r.get("close_day_offset", 0) != 1:
                            continue
                        if r["weekday"] != anchor_d.weekday():
                            continue
                        o_utc = oracle_resolve_local_utc(
                            anchor_d,
                            _parse_hhmm(r["open_local"]),
                            tz_iana,
                            fold_policy=r.get("dst_ambiguous_open", "fold_0"),
                            gap_policy=r.get("dst_gap_policy", "shift_forward"),
                        )
                        c_utc = oracle_resolve_local_utc(
                            anchor_d + timedelta(days=r.get("close_day_offset", 0)),
                            _parse_hhmm(r["close_local"]),
                            tz_iana,
                            fold_policy=r.get("dst_ambiguous_close", "fold_1"),
                            gap_policy=r.get("dst_gap_policy", "shift_forward"),
                        )
                        if o_utc is not None and c_utc is not None and o_utc <= start_utc and end_utc <= c_utc:
                            covered = True
                            break
                    if covered:
                        break
                eff = "OPEN" if covered else "CLOSED"

        usable.append({"rec": rec, "fresh": is_fresh, "eff": eff})

    if not usable:
        return ("UNKNOWN", "NO_USABLE_EVIDENCE")

    fresh = [u for u in usable if u["fresh"]]
    active = fresh if fresh else usable
    pool_fresh = bool(fresh)

    # Pareto frontier
    frontier = []
    for a in active:
        dominated = False
        for b in active:
            if a is b:
                continue
            ra, rb = a["rec"], b["rec"]
            if (
                rb["authority_tier"] <= ra["authority_tier"]
                and rb["specificity"] >= ra["specificity"]
                and (rb["authority_tier"] < ra["authority_tier"] or rb["specificity"] > ra["specificity"])
            ):
                dominated = True
                break
        if not dominated:
            frontier.append(a)

    verdicts = {x["eff"] for x in frontier}
    if len(verdicts) > 1:
        return ("UNKNOWN", "CONFLICTING_EVIDENCE_ON_FRONTIER")

    v = next(iter(verdicts))
    if v == "CLOSED":
        if not pool_fresh:
            return ("UNKNOWN", "STALE_CLOSED_UNCONFIRMED")
        if any(x["rec"]["authority_tier"] in (1, 2) for x in frontier):
            return ("FALSE", "CLOSED_BY_AUTHORITATIVE_SOURCE")
        return ("UNKNOWN", "COMMUNITY_REPORTED_CLOSED")

    # Unanimous OPEN
    has_date_spec = any(x["rec"]["specificity"] == 2 for x in frontier)
    if start_local_date in holidays and not has_date_spec:
        hp = place.get("holiday_policy", "UNKNOWN")
        if hp == "CLOSED":
            return ("FALSE", "HOLIDAY_CLOSED_BY_POLICY")
        if hp in ("UNKNOWN", "HOLIDAY_HOURS"):
            return ("UNKNOWN", "HOLIDAY_UNVERIFIED")

    if not pool_fresh:
        return ("UNKNOWN", "STALE_OPEN_ASSUMED")

    # Single-record witness qualification
    if any(x["rec"]["specificity"] == 2 and x["rec"]["authority_tier"] in (1, 2) for x in frontier):
        return ("TRUE", "VERIFIED_DATE_OPEN")

    has_t12_wk = any(x["rec"]["specificity"] == 1 and x["rec"]["authority_tier"] in (1, 2) for x in frontier)
    has_t3_dt = any(x["rec"]["specificity"] == 2 and x["rec"]["authority_tier"] == 3 for x in frontier)
    if has_t12_wk and has_t3_dt:
        return ("UNKNOWN", "UNCORROBORATED_MIXED_EVIDENCE")
    if has_t3_dt:
        return ("UNKNOWN", "COMMUNITY_DATE_UNCORROBORATED")
    return ("UNKNOWN", "WEEKLY_SCHEDULE_ASSUMED")
