# Case Study: Eliminating LLM Itinerary Hallucinations & Blast Radius with Neuro-Symbolic CP-SAT

**Project:** `tripgraph-engine` (TripGraph v7)  
**Repository:** https://github.com/sainathmanda7/tripgraph-engine  
**Live Swagger API:** https://tripgraph-engine.onrender.com/docs  

---

## 1. The Engineering Problem

Production AI travel planners fail consistently when a traveler modifies a single slot in an existing multi-day itinerary ("move Monday's art museum to noon" or "shift the trip by two days"):

1. **Combinatorial Cascade Failures:** A local shift collides with weekday opening rules, daylight saving transitions, contractual hotel check-in/no-show cutoffs, intra-day transit physics, and trip-wide budgets. Pure LLM rewrites lack mathematical feasibility guarantees and routinely hallucinate open venues or fabricate certainty on unverified public holidays.
2. **Unbounded Blast Radius:** Regenerating the entire itinerary to fix one day mutates days the user already approved and scales latency linearly with trip length.
3. **Runtime Fragility:** Running native C++ constraint solvers inside async web workers freezes HTTP event loops and cannot be safely canceled via Python exceptions.

---

## 2. Architectural Solution

TripGraph v7 separates **semantic preference** from **physical feasibility** using four core systems mechanisms:

### Dual-Graph Isolation & Exact Global Projections
An itinerary is modeled as an **acyclic structural spine** (`CityStay` -> `TransitLeg` -> `ActivityNode`) coupled to a **cyclic constraint factor graph** (trip budget, canonical place anti-duplication, category quotas, intra-day Order-1 Markov transit adjacency). When an edit arrives:
- Untouched days are fingerprinted with **SHA-256 `FreezeSnapshot`** and verified unmodified pre-commit (`LockGuard`).
- Frozen global constraints are projected into exact constants on the dirty subgraph (proven equivalent via exhaustive `2^n` CI tests).
- Mutations affecting `>50%` of unlocked nodes automatically escalate to a separate 30s async `GlobalReplanMode` worker (`202 Accepted` + live SSE stream).

### Dual-Model OR-Tools CP-SAT Compiler
One immutable `CompiledConstraintIR` compiles two distinct models without silent policy pruning:
- **`OptimizationModel`:** Multi-worker hierarchical search with dynamically proven overflow-safe weights (`< 2^53`).
- **`DiagnosticFeasibilityModel` (infeasible path only):** Single-worker, zero-objective model with cloned per-constraint optional intervals and bounded 6-probe Minimal Unsatisfiable Subset (MUS) extraction (`<= 250 ms`), cleanly separating physical conflicts (`PROVEN_PHYSICALLY_INFEASIBLE`) from policy exclusions (`INFEASIBLE_UNDER_VERIFICATION_POLICY`).

### Kleene K3 Verification & Zero-Shared-Code Oracle
Venue availability resolves in three-valued logic (`TRUE`, `FALSE`, `UNKNOWN`) via **Pareto single-witness dominance** (`Specificity > Authority > Freshness`) over byte-pinned `ZoneInfo.from_file()` TZDB rules (AST-banning raw `ZoneInfo(key)`). Correctness is cross-checked against an independent `dateutil` reference oracle (**25/25** adversarial DST, hotel contract, and conflicting-evidence cases).

### OS-Isolated Subprocess Pool & Atomic Commit Fencing
An AST CI firewall forbids `src/tripgraph/api/` from importing `ortools`. Solves dispatch over length-prefixed binary pipes to isolated OS subprocesses (`start_new_session=True`, Linux `PR_SET_PDEATHSIG(SIGKILL)`), holding zero DB credentials. Timed-out C++ solves are terminated via `os.killpg(PGID, SIGKILL)` and replaced in `<0.8s`, while SQLite atomic commit fencing (`WHERE version = ? AND EXISTS (status='ACTIVE' AND deadline_utc > ?)`) eliminates stale zombie writes.

---

## 3. Measured Results

All benchmarks run locally with zero API keys across **116 stratified held-out scenarios x 10 timing repeats** (`3,480` solves) using two-stage scenario-clustered bootstrap 95% CIs:

| Metric | Measured Result | Verification / Baseline |
| :--- | :--- | :--- |
| **Hard-Constraint Accuracy & Honest Refusal** | **100.0%** (`S2_taxonomy`) | **+6.9 pp** vs matched deterministic greedy (`93.1%`, Holm exact McNemar **`p = 0.0078`**) |
| **Incremental Latency (116 Scenarios)** | **13.7 ms** p50 / **29.2 ms** p95 / **40.7 ms** p99 | Zero LLM tokens for structured closures & temporal cascades |
| **Scaling (3-Day to 10-Day / 40-Act Trips)** | **13.5 ms** flat (`O(1)` in trip length) | **5.36x speedup** vs naive full-trip replan (`72.2 ms` at 10 days) |
| **Unintended Blast Radius** | **0.0%** semantic / **0.0%** temporal | Enforced by pre-commit SHA-256 `LockGuard` |
| **Automated Verification Suite** | **69 / 69** passing (**85%** coverage, 2,922 stmts) | 25/25 independent oracle, `2^n` proofs, 60-case MUS fuzzing, live `SIGKILL` recovery |

---

## 4. Key Trade-Offs & Production Boundaries

- **Stability Over Marginal Global Gain:** Horizon 0 localizes edits to touched days (`<= 50%` dirty) to guarantee `0.0%` blast radius and constant-time latency, escalating large shifts (`> 50%`) to async `202` global replans.
- **Honest Scope:** The live Render Free deployment runs single-container `:memory:` SQLite (horizontal scaling moves to PostgreSQL `SELECT ... FOR UPDATE` + stateless gRPC workers), and deterministic CI measures physical feasibility while free-text sensory ranking (`S3` vs `S2_taxonomy`) uses `eval/blind_grade.py` under exact `>=80%` power and weighted kappa `>=0.60` gates.
