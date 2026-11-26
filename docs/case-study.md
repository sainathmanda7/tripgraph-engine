# Case Study: Localized Neuro-Symbolic Verification & Replanning for Multi-City Travel Engines

**Project:** `tripgraph-engine` (TripGraph v7)  
**Repository:** https://github.com/sainathmanda7/tripgraph-engine  
**Public Demo Swagger API:** https://tripgraph-engine.onrender.com/docs *(seeded fixtures)*  

---

## 1. The Operational Problem

Consider a travel platform or DMC agent managing a multi-city itinerary (e.g., Tokyo &rarr; Shinkansen &rarr; Kyoto) where hotels, trains, and key experiences are already approved by the customer. When the customer asks to replace or move one activity:

1. **Cascading Constraint Violations:** A local change collides with weekday opening rules, DST transitions, hotel check-in/no-show cutoffs, intra-day transit physics, and trip-wide budgets. Pure LLM rewrites lack mathematical feasibility guarantees and routinely fabricate certainty on conflicting or unverified holiday data.
2. **Unbounded Blast Radius:** Regenerating the whole itinerary to fix one day mutates stays and experiences the traveler already approved.
3. **Systems & Concurrency Hazards:** Running native C++ constraint solvers inside async web workers freezes HTTP event loops, and without commit barriers across *all* execution paths (sync, all-locked, and async global), background jobs can violate user locks or overwrite newer state.

---

## 2. Where TripGraph Fits (Component Positioning)

TripGraph is engineered as an **embeddable constraint optimization and itinerary verification component** -- not a standalone OTA/DMC booking platform. Live supplier inventory, commercial rate sheets, taxes, currency conversion, and booking execution sit outside `GroundingProvider`; TripGraph owns deterministic schedule feasibility, lock preservation, conflict diagnosis, and concurrency-safe state transitions.

### Core Systems Mechanisms
- **Dual-Graph Isolation & Commit-Path Parity:** Separates the acyclic itinerary spine from the cyclic constraint factor graph, projects frozen global constraints onto dirty days (verified via exhaustive `2^n` CI proofs), and enforces `FreezeSnapshot` + `LockGuard` + independent Kleene K3 verification on **every** commit path (`DAY_BIN`, `ALL_LOCKED_VERIFY_ONLY`, and `GLOBAL_REPLAN`).
- **Dual-Model CP-SAT Compiler:** One immutable `CompiledConstraintIR` emits a multi-worker optimizer (`< 2^53` overflow-safe weights) and -- only on `INFEASIBLE` -- a single-worker, zero-objective diagnostic model with cloned per-constraint optional intervals and bounded 6-probe MUS (`<= 250 ms`), separating physical conflicts from verification policy exclusions without silent pruning.
- **Kleene K3 & Policy-Gated `UNKNOWN`:** Pareto single-witness evidence resolution over byte-pinned `ZoneInfo.from_file()` TZDB rules (cross-checked **25/25** against a zero-shared-code `dateutil` oracle). One shared `is_epistemic_allowed()` policy governs both IR compilation and pre-commit gating.
- **True OS Process Isolation & Fenced SQLite Commits:** Pure-Python `solver/types.py` keeps `ortools` completely out of the API process (`'ortools' not in sys.modules` verified in fresh subprocess). Both Phase A and Phase B solves run in isolated Linux worker subprocesses (`PR_SET_PDEATHSIG(SIGKILL)` armed *before* importing `ortools`, `ppid` re-checked, two-stage handshake, hard `SIGKILL` timeouts) with atomic SQLite version + request commit fencing.

---

## 3. Scoped Benchmark & Verification Evidence

Measured **locally** (WSL2 Linux, Python 3.11.18, warm-started, compile + solve + verify) across **116 stratified held-out fixture scenarios x 10 repeats** (`3,480` solves, two-stage clustered bootstrap 95% CIs):

| Metric | Measured Result | Scope / Baseline |
| :--- | :--- | :--- |
| **Held-Out Suite Feasibility & Refusal** | **100.0%** (`S2_taxonomy`) | **+6.9 pp** vs matched deterministic greedy (`93.1%`, Holm exact McNemar **`p = 0.0078`**) on curated fixtures (physical feasibility, not free-text preference) |
| **Local Edit Latency (116 Scenarios)** | **13.7 ms** p50 / **29.2 ms** p95 / **40.7 ms** p99 | Local benchmark timing (not Render end-to-end HTTP) |
| **Incremental Scaling (12-40 Activities)** | **~13-14 ms** p50 (`13.57 ms` &rarr; `13.47 ms`) | **5.36x speedup** vs full-trip replan (`72.19 ms`) at 40 activities; whole-graph K3 verify scales $O(|V|)$ (`1.2` &rarr; `2.5 ms`) |
| **Blast Radius Outside Dirty Scope** | **0.0%** semantic / **0.0%** temporal | Enforced pre-commit via SHA-256 `LockGuard` |
| **Automated CI Verification** | **78 / 78** passing (**88%** coverage across **3,077 statements**) | Zero-`ortools` API check, 25/25 oracle, `2^n` proofs, 60-case MUS fuzz, `SIGKILL` recovery, DST/cross-city & global-lock barriers |

---

## 4. Honest Operational Limits

- **Non-Durable Async Jobs:** `GlobalReplanMode` (`202 Accepted`) executes via in-process `asyncio.create_task()` with in-memory event lists (an API restart drops active job state; roadmap moves jobs to a durable queue + PostgreSQL row locks).
- **Seeded Fixtures in Public Demo:** Public Render deployment uses `FixtureGroundingProvider` and `:memory:` SQLite on `0.1 vCPU`.
