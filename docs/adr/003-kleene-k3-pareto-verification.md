# ADR-003: Kleene K3 Three-Valued Evidence Resolution & Byte-Pinned TZDB

- **Status:** Accepted
- **Date:** 2026-11
- **Files:** `src/tripgraph/verifier/evidence.py`, `src/tripgraph/verifier/hotel.py`, `src/tripgraph/time/tz_registry.py`

## Context
Real travel venue data mixes official date-specific overrides, aggregator feeds, stale caches, and community OSM edits—alongside public holidays and DST transitions. Binary `open: bool` checks either silently book closed venues or falsely reject valid venues when evidence is incomplete.

## Decision
1. Evaluate availability in **Kleene three-valued logic (K3: `TRUE`, `FALSE`, `UNKNOWN`)** using **Pareto single-witness dominance** (`Specificity > Authority > Freshness`). Never merge fields across separate records. Unverified holidays, single Tier-3 closures, stale records, and frontier conflicts resolve to `UNKNOWN`, surfacing explicit `assumptions_accepted` rather than fake `VERIFIED_TRUE`.
2. Model hotels as a contractual state machine (`reserved_nights`, `no_show_cutoff`, `late_notify_policy`, `STAFFED_24H`/`STAFFED_HOURS`/`CONTACTLESS_CODE`, `PRE_CHECKIN_BAG_DROP` follow-up).
3. Load IANA timezones exclusively via byte-pinned `ZoneInfo.from_file()` with SHA-256 verification (AST-banning `ZoneInfo(key)`), cross-checked against a zero-shared-code `dateutil` reference oracle across 25 hand-computed adversarial cases.

## Alternatives Considered
- **Boolean open/closed checks with optimistic defaults:** Produces silent false certainty on public holidays and conflicting supplier feeds.
- **Standard `zoneinfo.ZoneInfo(key)`:** Vulnerable to silent DST offset drift between local dev machines and Linux Docker images.

## Consequences
- **Positive:** 25/25 dual agreement with independent reference oracle; explicit two-axis certificates (`solver_certificate` x `verification_certificate`).
- **Negative:** Higher schema richness required from grounding adapters (mitigated by `parse_osm_opening_hours` quarantining unparseable OSM strings as `UNPARSEABLE`).
