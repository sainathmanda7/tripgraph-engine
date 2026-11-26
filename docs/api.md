# TripGraph v7 -- API Reference

- **Public Demo Swagger UI (OpenAPI 3.1):** https://tripgraph-engine.onrender.com/docs *(Render Free tier -- allow ~30s cold start on first request; backed by seeded `FixtureGroundingProvider` and `:memory:` SQLite)*
- **Local Demo Server:** `make serve` (`http://127.0.0.1:8000/docs`)

---

## Endpoints Overview

| Method | Path | Purpose | Status Codes |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Service liveness check | `200` |
| `POST` | `/trips` | Create and persist a validated `TripGraph` (`version=1`) | `201`, `409`, `422` |
| `GET` | `/trips/{trip_id}` | Fetch current committed `TripGraph` and monotonic `version` | `200`, `404` |
| `POST` | `/trips/{trip_id}/verify` | Run independent Kleene K3 verification (read-only) | `200`, `404` |
| `PATCH` | `/trips/{trip_id}/edit` | Execute localized CP-SAT edit or escalate to async `GlobalReplanMode` | `200`, `202`, `404`, `409`, `422`, `503` |
| `GET` | `/jobs/{job_id}/events` | Stream Server-Sent Events (SSE) for an in-memory `202` global replan | `200` (`text/event-stream`), `404` |

---

## 1. `POST /trips` & `POST /trips/{trip_id}/verify`

```bash
curl -s -X POST https://tripgraph-engine.onrender.com/trips \
  -H "Content-Type: application/json" \
  -d @examples/tokyo_kyoto_closed_monday.json

curl -s -X POST https://tripgraph-engine.onrender.com/trips/example_tokyo_kyoto_closed_mon/verify
```

Returns `hard_constraints_status: "FALSE"`, `verification_certificate: "VIOLATED_FALSE"`, and `CLOSED_BY_AUTHORITATIVE_SOURCE` for Mori Art Museum on Monday (seeded fixture evidence).

---

## 2. `PATCH /trips/{trip_id}/edit`

### Request Schema (`EditRequestBody`)

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `edit` | `EditCommand` | *required* | `move_activity`, `drop_activity`, `swap_hotel`, `update_dietary`, `extend_stay`, `shift_start_date` (`allow_shift_locked: bool = false`), `late_night_arrival`. |
| `verification_policy` | `VerificationPolicy` | `ASSUME_WEEKLY_NON_HOLIDAY` | `STRICT` / `ASSUME_WEEKLY_NON_HOLIDAY` / `ALLOW_UNVERIFIED_HOLIDAYS` (enforced identically in IR compilation and pre-commit verification). |
| `allow_auto_drop_optional` | `bool` | `false` | Consent gate: `false` prevents silent activity drops on conflict or LLM outage. |

### Case A -- Localized Incremental Repair (`200 OK`)

```bash
curl -s -X PATCH https://tripgraph-engine.onrender.com/trips/example_tokyo_kyoto_closed_mon/edit \
  -H "Content-Type: application/json" \
  -d @examples/edit_sync_request.json
```

Returns `version: 2`, `solver_certificate: "PROVEN_OPTIMAL_IN_SCOPE"`, `verification_certificate: "VERIFIED_TRUE"`, and `blast_radius: {"semantic": 0.0, "temporal": 0.0}` (zero unintended changes outside the dirty Monday scope; Sunday Tokyo, Shinkansen leg, and Kyoto stays remain SHA-256 frozen).

### Case B -- Locked-Node Refusal on Default Global Shift (`409 Conflict`)

```bash
curl -s -X PATCH https://tripgraph-engine.onrender.com/trips/example_tokyo_kyoto_closed_mon/edit \
  -H "Content-Type: application/json" \
  -d '{"edit": {"edit_type": "shift_start_date", "delta_days": 2}}'
```

Returns `409 LOCKED_NODE_CONFLICT` listing `locked_activities: ["a_mon_shrine"]` without mutating state.

### Case C -- Explicit Opt-In Global Shift (`202 Accepted` + SSE)

```bash
curl -s -X PATCH https://tripgraph-engine.onrender.com/trips/example_tokyo_kyoto_closed_mon/edit \
  -H "Content-Type: application/json" \
  -d '{"edit": {"edit_type": "shift_start_date", "delta_days": 2, "allow_shift_locked": true}}'
```

Escalates to an in-memory `asyncio` background task (non-durable across API restarts; check `GET /trips/{id}` after a restart), enforcing `LockGuard` + independent K3 verification pre-commit, and streams SSE stages over `GET /jobs/{job_id}/events`:

```text
data: {"stage": "global_replan_started", "reason": "DIRTY_RATIO_EXCEEDED:1.00>0.50"}

data: {"stage": "compiling_global_ir", "days": 4}

data: {"stage": "solving_global_cpsat"}

data: {"stage": "completed", "version": 3, "verification_certificate": "VERIFIED_TRUE"}
```

### Special Certificates & Status Codes

| Code / Field | Meaning |
| :--- | :--- |
| `solver_certificate: "NOT_INVOKED_VERIFY_ONLY"` | Returned on `ALL_LOCKED_VERIFY_ONLY` edits where `LockGuard` + K3 verification ran directly without invoking CP-SAT. |
| `409 LOCKED_NODE_CONFLICT` / `LOCK_GUARD_VIOLATION` | Refused because a user-locked or frozen node would be mutated. |
| `409 NEEDS_USER_RESOLUTION` | Provably infeasible under physical constraints or `verification_policy` (includes `PROVEN_MUS` core or `disallowed_assumptions`). |
| `409 STALE_VERSION_OR_ABORTED` | Concurrent edit or expired request blocked by SQLite commit fencing. |
| `503` | `SOLVER_OVERLOADED`, `INDETERMINATE_SOLVER_TIMEOUT` (worker `SIGKILL`ed), or `RETRYABLE_UPSTREAM_LLM_UNAVAILABLE`. |
