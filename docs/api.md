# TripGraph v7 — API Reference

- **Live Swagger UI (OpenAPI 3.1):** https://tripgraph-engine.onrender.com/docs *(Render Free tier — allow ~30s cold start on first request)*
- **Local Server:** `make serve` (`http://127.0.0.1:8000/docs`)

---

## Endpoints Overview

| Method | Path | Purpose | Status Codes |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | Service liveness check | `200` |
| `POST` | `/trips` | Create and persist a validated `TripGraph` (`version=1`) | `201`, `409`, `422` |
| `GET` | `/trips/{trip_id}` | Fetch current committed `TripGraph` and monotonic `version` | `200`, `404` |
| `POST` | `/trips/{trip_id}/verify` | Run independent Kleene K3 verification (read-only) | `200`, `404` |
| `PATCH` | `/trips/{trip_id}/edit` | Execute incremental CP-SAT edit or escalate to async `GlobalReplanMode` | `200`, `202`, `404`, `409`, `422`, `503` |
| `GET` | `/jobs/{job_id}/events` | Stream Server-Sent Events (SSE) for an escalated `202` global replan | `200` (`text/event-stream`), `404` |

---

## 1. `GET /health`

```bash
curl -s https://tripgraph-engine.onrender.com/health
```

```json
{"status": "ok", "engine": "tripgraph-v7"}
```

---

## 2. `POST /trips`

Creates a new itinerary after validating Pydantic v2 Stage-0 axioms (`end_utc == start_utc + duration_min * 60`, valid hotel booking contract, unique IDs).

```bash
curl -s -X POST https://tripgraph-engine.onrender.com/trips \
  -H "Content-Type: application/json" \
  -d @examples/invalid_closed_monday.json
```

**Response (`201 Created`):**
```json
{"trip_id": "example_tokyo_closed_mon", "version": 1}
```

---

## 3. `POST /trips/{trip_id}/verify`

Runs the independent Kleene K3 verifier (Pareto evidence resolution, DST-aware opening windows, hotel check-in/no-show/access contracts, intra-day transit physics, and global budget/anti-dup/quota checks).

```bash
curl -s -X POST https://tripgraph-engine.onrender.com/trips/example_tokyo_closed_mon/verify
```

**Response (`200 OK` — Authoritative Violation Detected):**
```json
{
  "hard_constraints_status": "FALSE",
  "verification_certificate": "VIOLATED_FALSE",
  "violations": [
    {
      "check_id": "availability:a_mon_art",
      "code": "CLOSED_BY_AUTHORITATIVE_SOURCE",
      "detail": "Availability for Mori Art Museum: CLOSED_BY_AUTHORITATIVE_SOURCE"
    }
  ],
  "assumptions_accepted": []
}
```

---

## 4. `PATCH /trips/{trip_id}/edit`

### Request Schema (`EditRequestBody`)

| Field | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `edit` | `EditCommand` | *required* | Discriminated union by `edit_type`: `move_activity`, `drop_activity`, `swap_hotel`, `update_dietary`, `extend_stay`, `shift_start_date`. |
| `verification_policy` | `VerificationPolicy` | `ASSUME_WEEKLY_NON_HOLIDAY` | `STRICT` / `ASSUME_WEEKLY_NON_HOLIDAY` / `ALLOW_UNVERIFIED_HOLIDAYS` |
| `allow_auto_drop_optional` | `bool` | `false` | Consent gate: `false` guarantees the engine never silently drops user activities on conflict or LLM outage. |

### Case A — Synchronous Incremental Repair (`200 OK`)

```bash
curl -s -X PATCH https://tripgraph-engine.onrender.com/trips/example_tokyo_closed_mon/edit \
  -H "Content-Type: application/json" \
  -d @examples/edit_sync_request.json
```

Returns `version: 2`, `solver_certificate: "PROVEN_OPTIMAL_IN_SCOPE"`, `verification_certificate: "VERIFIED_TRUE"`, `recovery_mode: "S2_TAXONOMY_DETERMINISTIC"`, `blast_radius: {"semantic": 0.0, "temporal": 0.0}`, and the updated `graph`.

### Case B — Async Global Replan Escalation (`202 Accepted`)

Triggered automatically when `dirty_ratio > 0.50` (e.g., `shift_start_date`) or an edit has non-projectable global coupling:

```bash
curl -s -X PATCH https://tripgraph-engine.onrender.com/trips/example_tokyo_closed_mon/edit \
  -H "Content-Type: application/json" \
  -d '{"edit": {"edit_type": "shift_start_date", "delta_days": 2}}'
```

```json
{
  "mode": "GLOBAL_REPLAN_ESCALATED",
  "reason": "DIRTY_RATIO_EXCEEDED:1.00>0.50",
  "job_id": "job_b26fdac63a65",
  "events_url": "/jobs/job_b26fdac63a65/events",
  "dirty_ratio": 1.0
}
```

### Case C — Honest Conflict Refusal (`409 Conflict`)

Returned when constraints are provably infeasible (never silently mutates state). Includes the bounded MUS core separating physical conflicts from verification policy gates:

```json
{
  "status": "NEEDS_USER_RESOLUTION",
  "scope_tier": "HORIZON_0",
  "solver_certificate": "INFEASIBLE_UNDER_VERIFICATION_POLICY",
  "core_minimality": "PROVEN_MUS",
  "core_groups": [
    {
      "group_id": "POLICY_GATE:A1:p_hol",
      "origin": "VERIFICATION_POLICY_GATE",
      "kind": "POLICY_GATE",
      "node_ids": ["A1"],
      "epistemic_code": "HOLIDAY_UNVERIFIED",
      "detail": "Candidate p_hol excluded under ASSUME_WEEKLY_NON_HOLIDAY (HOLIDAY_UNVERIFIED)."
    }
  ]
}
```

### HTTP Error & Availability Semantics

| HTTP Status | Condition |
| :---: | :--- |
| `404` | Unknown `trip_id` or `job_id`. |
| `409` | Provably infeasible edit (`NEEDS_USER_RESOLUTION`), duplicate `trip_id`, or `STALE_VERSION_OR_ABORTED` concurrency fence violation. |
| `422` | Schema or Stage-0 axiom violation (e.g., mismatched `duration_min` vs timestamps). |
| `500` | Internal safety guard triggered (`INTERNAL_BASE_AXIOM_CONTRADICTION`, `INTERNAL_MODEL_EQUIVALENCE_ERROR`). |
| `503` | `SOLVER_OVERLOADED` (queue > 250ms), `INDETERMINATE_SOLVER_TIMEOUT` (worker `SIGKILL`ed), or `RETRYABLE_UPSTREAM_LLM_UNAVAILABLE`. |

---

## 5. `GET /jobs/{job_id}/events` (SSE Stream)

Streams newline-delimited `text/event-stream` JSON stages until the 30s background global worker commits or fails:

```bash
curl -N https://tripgraph-engine.onrender.com/jobs/<job_id>/events
```

```text
data: {"stage": "global_replan_started", "reason": "DIRTY_RATIO_EXCEEDED:1.00>0.50"}

data: {"stage": "compiling_global_ir", "days": 2}

data: {"stage": "solving_global_cpsat"}

data: {"stage": "completed", "version": 3}
```
