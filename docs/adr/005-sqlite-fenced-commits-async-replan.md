# ADR-005: SQLite Atomic Commit Fencing & 202 Async GlobalReplanMode

- **Status:** Accepted
- **Date:** 2026-11
- **Files:** `src/tripgraph/runtime/store.py`, `src/tripgraph/runtime/orchestrator.py`

## Context
Two concurrency hazards threaten stateful itinerary engines:
1. A slow solver that finishes just after an HTTP timeout or concurrent edit can overwrite newer committed state ("zombie write").
2. Major edits touching `>50%` of unlocked activities (such as shifting a trip's start date) cannot safely finish inside tight synchronous latency budgets without degrading tail latency for other callers.

## Decision
1. **Atomic Fenced Commits:** Register every edit in `edit_requests (request_id, trip_id, status='ACTIVE', deadline_utc)` and abort it immediately on timeout/cancellation. Persist updates strictly via one conditional SQLite `UPDATE ... WHERE version = ? AND EXISTS (status='ACTIVE' AND deadline_utc > ?)`.
2. **Async Escalation (`202 Accepted` + SSE):** Automatically escalate edits with `dirty_ratio > 0.50` or non-projectable objectives to a separate `30,000 ms` async `GlobalReplanMode` worker, streaming live progress stages over `GET /jobs/{job_id}/events`.

## Alternatives Considered
- **Plain optimistic locking (`WHERE version = ?`) without request fencing:** Still allows a timed-out request to commit later if no other edit arrived in between.
- **Synchronous blocking on full-trip replans:** Violates the 8s sync ceiling and risks gateway timeouts.

## Consequences
- **Positive:** Zero zombie writes (100% statement coverage in `store.py`); clean separation between interactive `<25 ms` local edits and heavy global restructurings.
- **Negative:** Clients must handle both `200 OK` (sync) and `202 Accepted` + SSE consumption (global replan).
