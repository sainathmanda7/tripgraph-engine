# TripGraph v7 — Neuro-Symbolic Itinerary Verifier & Incremental Re-Planner

Production-grade backend proof-of-work implementing **AlphaGeometry-style neuro-symbolic verification** (OR-Tools CP-SAT + tool-mediated grounded LLM recovery) and **zero-blast-radius incremental re-planning** for multi-city travel itineraries.

## Measured Results (116 Held-Out Scenarios × 10 Repeats)

Full reproducible metrics and two-stage scenario-clustered bootstrap 95% CIs live in [`BENCHMARK_RESULTS.md`](./BENCHMARK_RESULTS.md).

| Arm | Overall Accuracy | R1 Shiftable | R2A Closure | R2B Feasibility | R3 Hard-Lock Refusal | Blast Radius (Sem/Tmp) | p50 ms | p95 ms |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`S2_greedy`** (Matched Greedy) | 93.1% | 66.7% | 100.0% | 100.0% | 100.0% | 0.0% / 0.0% | 1.7 | 4.2 |
| **`S2_cpsat`** (Temporal CP-SAT Only) | 41.4% | 100.0% | 0.0% | 0.0% | 100.0% | 0.0% / 0.0% | 9.1 | 15.4 |
| **`S2_taxonomy`** (CP-SAT + Frozen Ontology) | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **0.0% / 0.0%** | **13.3** | **23.8** |

- **`S2_taxonomy` vs `S2_greedy`:** `+6.9 pp` (`n01=8, n10=0`), Holm-adjusted exact McNemar **`p = 0.0078`**.
- **`S2_taxonomy` vs `S2_cpsat`:** `+58.6 pp` (`n01=68, n10=0`), Holm-adjusted exact McNemar **`p = 1.36e-20`**.

## Architecture Highlights

- **Dual-Graph Isolation & Exact Projections (`src/tripgraph/graph/`):** Acyclic structural spine + cyclic constraint factor graph. Day-bin dirty closure, SHA-256 `LockGuard`, and exhaustive $2^n$ equivalence tests for global hard constraints and dynamic intra-day Order-1 Markov adjacency.
- **Kleene K3 Verification & Hotel State Machine (`src/tripgraph/verifier/`):** Pareto single-witness evidence resolution (`TRUE`/`FALSE`/`UNKNOWN`), contractual hotel booking verification (no-show cutoffs, late-notify deadlines, 24h/staffed/contactless access, baggage drop), and byte-pinned `ZoneInfo.from_file()` TZDB drift detection—verified **25/25** against a zero-shared-code `dateutil` reference oracle (`eval/reference_oracle/`).
- **Dual-Model CP-SAT Compiler (`src/tripgraph/solver/`):** Shared `CompiledConstraintIR` emitting a multi-worker `OptimizationModel` (dynamically proven overflow-safe hierarchical weights `< 2^53`) and a single-worker, objective-free `DiagnosticFeasibilityModel` with cloned per-constraint optional intervals and bounded 6-probe MUS extraction (`<= 250 ms`), verified across 60 randomized fuzz proofs.
- **Subprocess Isolation & Atomic Commit Fencing (`src/tripgraph/runtime/`, `src/tripgraph/api/`):** FastAPI never calls `CpSolver.solve()` in-process (AST-enforced). Solves run in a bounded OS subprocess pool (`PR_SET_PDEATHSIG(SIGKILL)`, hard timeout `SIGKILL` + replacement) with SQLite version + request-generation commit fencing and async 30s `GlobalReplanMode` (`202 Accepted` + SSE).

## 2-Minute Zero-Key Quickstart

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

make test     # Runs all 69 unit, AST-firewall, 25/25 oracle, 2^n projection, MUS, SIGKILL & SSE tests
make bench    # Reproduces 116-scenario x 10-repeat benchmark into BENCHMARK_RESULTS.md
make serve    # Starts FastAPI at http://localhost:8000/docs
