<div align="center">
  <img src="./assets/logo.svg" alt="TripGraph v7 -- Neuro-Symbolic Itinerary Verifier & Incremental Re-Planner" width="100%" />

  <p>
    <a href="https://github.com/sainathmanda7/tripgraph-engine/actions/workflows/ci.yml"><img src="https://github.com/sainathmanda7/tripgraph-engine/actions/workflows/ci.yml/badge.svg" alt="TripGraph CI" /></a>
    <a href="./docs/performance.md"><img src="https://img.shields.io/badge/tests-78%2F78%20passed-00C853?style=flat" alt="78/78 Tests Passed" /></a>
    <a href="./docs/performance.md"><img src="https://img.shields.io/badge/coverage-88%25-38BDF8?style=flat" alt="88% Statement Coverage" /></a>
    <a href="https://tripgraph-engine.onrender.com/docs"><img src="https://img.shields.io/badge/Public%20Demo%20API-Swagger%20UI-FACC15?style=flat" alt="Public Demo Swagger API" /></a>
    <a href="./LICENSE"><img src="https://img.shields.io/badge/license-MIT-E6EDF3?style=flat" alt="MIT License" /></a>
  </p>

  <p>
    <strong>A neuro-symbolic constraint optimization and itinerary verification engine component for multi-city travel schedules.</strong><br/>
    Pairs <strong>OR-Tools CP-SAT</strong> and <strong>Kleene K3</strong> verification with factor-graph dirty closure, SHA-256 freeze locks, OS-isolated solver subprocesses, and atomic SQLite commit fencing.
  </p>

  <p>
    <a href="https://tripgraph-engine.onrender.com/docs"><strong>Public Demo API</strong></a> &bull;
    <a href="./docs/architecture.md"><strong>Architecture (8 Diagrams)</strong></a> &bull;
    <a href="./docs/performance.md"><strong>Benchmarks &amp; Coverage</strong></a> &bull;
    <a href="./docs/system-design.md"><strong>System Design &amp; ADRs</strong></a> &bull;
    <a href="./docs/case-study.md"><strong>Case Study</strong></a> &bull;
    <a href="./docs/api.md"><strong>API Reference</strong></a>
  </p>
</div>

---

## Deterministic Fixture Walkthrough (2-City Tokyo &rarr; Kyoto)

> **Evidence Disclosure:** The walkthrough below runs against the deployed **public demo API** (`https://tripgraph-engine.onrender.com`) backed by **`FixtureGroundingProvider`** (seeded venue evidence, fixture clock `2026-06-10T00:00:00Z`) and `:memory:` SQLite. It validates the deterministic verification, solver, lock-preservation, and fencing pipeline -- not live commercial supplier feeds or real-time booking inventory.

Running [`scripts/live_demo.py`](./scripts/live_demo.py) executes a 5-day, **2-city Tokyo &rarr; Kyoto** itinerary (2 hotels, 1 Shinkansen rail leg `nozomi_215`, 6 activities):
1. **Kleene K3 Verification (`POST /verify`):** Catches that Mori Art Museum is closed Mondays in the seeded fixture (`VIOLATED_FALSE`).
2. **Localized Subprocess Repair (`PATCH /edit`):** Swaps Monday's art museum to an open Tokyo alternative (`version=2`, `PROVEN_OPTIMAL_IN_SCOPE`, `VERIFIED_TRUE`) with **`0.0%` blast radius outside the calculated dirty scope** -- Sunday Tokyo, the Shinkansen leg, and all Kyoto stays/activities stay SHA-256 frozen.
3. **Locked-Node Refusal (`409 LOCKED_NODE_CONFLICT`):** Attempting a default `+2 day` global shift fails fast because Meiji Shrine is marked `locked_by_user=True`.
4. **Explicit Opt-In Global Shift (`202 Accepted` + SSE):** Passing `allow_shift_locked=true` shifts stays, activities, and the Shinkansen leg using DST-safe `PinnedTzRegistry` wall-clock resolution, enforces both `LockGuard` and independent K3 verification pre-commit, and streams SSE progress to `version=3`.

<div align="center">
  <img src="./assets/demo.gif" alt="TripGraph v7 Seeded Fixture Walkthrough (Tokyo to Kyoto)" width="92%" />
</div>

> **Try the public demo deployment:** Open [https://tripgraph-engine.onrender.com/docs](https://tripgraph-engine.onrender.com/docs) *(Render Free tier -- allow ~30s cold start on the first request)* or run `python3 scripts/live_demo.py https://tripgraph-engine.onrender.com`.

---

## Architectural Scope & Core Guarantees

TripGraph is designed as an **embeddable optimization and verification component** for travel platforms (OTAs, DMCs, and agent itinerary builders) rather than a standalone supplier-inventory, pricing, or booking system.

| Capability | Engineering Implementation | Automated Proof |
| :--- | :--- | :--- |
| **Dual-Model CP-SAT Compiler** | Single immutable `CompiledConstraintIR` emits a multi-worker `OptimizationModel` (dynamic weights `< 2^53`) and -- strictly on `INFEASIBLE` -- a single-worker, zero-objective `DiagnosticFeasibilityModel` with cloned per-constraint optional intervals and bounded 6-probe MUS (`<= 250 ms`). Zero silent policy pruning. | **60-scenario randomized fuzz proof** (`test_milestone4_cpsat_dual_model.py`). |
| **Localized Dirty Closure & `LockGuard`** | Separates acyclic itinerary spine from cyclic constraint factor graph. Projects frozen global budget/anti-dup/quotas onto dirty days and enforces **SHA-256 `LockGuard`** + independent K3 verification on **every** commit path (sync, `ALL_LOCKED_VERIFY_ONLY`, and async `GLOBAL_REPLAN`). | **Exhaustive `2^n` projection equivalence tests** (`test_milestone3_graph_and_projections.py`) + Sprint 1/2 commit-barrier regression tests. |
| **Kleene K3 & Policy-Gated `UNKNOWN`** | Three-valued logic (`TRUE` / `FALSE` / `UNKNOWN`) with Pareto single-witness dominance, hotel check-in/no-show/bag-drop state machine, and byte-pinned `ZoneInfo.from_file()` (AST-banned raw `ZoneInfo(key)`). Commit gate shares `is_epistemic_allowed()` with IR compilation. | **25/25 dual agreement** against zero-shared-code `dateutil` reference oracle (`eval/reference_oracle/`). |
| **True OS Process Isolation & Commit Fencing** | Pure-Python `solver/types.py` keeps `ortools` completely out of the API process (`'ortools' not in sys.modules` verified in fresh subprocess). Both Phase A and Phase B solves run in isolated Linux worker subprocesses (`PR_SET_PDEATHSIG(SIGKILL)` armed *before* importing `ortools`, `ppid` re-checked, two-stage readiness handshake, hard `SIGKILL` timeouts) with atomic SQLite version + request commit fencing. | Fresh-process `sys.modules` guard, live `SIGKILL` recovery (`<0.8s`), Phase B supervisor routing, and `prctl`/race unit tests (`test_sprint1_p0_hardening.py`). |

---

## Measured Benchmark Results (Scoped Local Evaluation)

> **Benchmark Scope:** All timings below were measured **locally** on Linux (WSL2, Python 3.11.18), **warm-started**, covering **IR compile + CP-SAT solve + independent K3 verification** (not end-to-end Render HTTP latency). The **116 stratified held-out scenarios** (`eval/run_benchmark.py`, 10 repeats each = 3,480 solves) are curated deterministic evaluation fixtures measuring physical constraint feasibility and honest `UNSAT` refusal -- not live supplier data or free-text human preference. Full methodology lives in [`docs/performance.md`](./docs/performance.md).

<div align="center">
  <img src="./assets/accuracy_by_regime.svg" alt="Feasibility & Refusal Rate Across 116 Held-Out Fixture Scenarios" width="96%" />
</div>

| Arm (0 LLM Tokens) | Suite Feasibility / Refusal | R1 Shiftable | R2A Closure | R2B Feasibility* | R3 Hard UNSAT | Blast Radius (Outside Scope) | Local p50 ms | Local p95 ms | Local p99 ms |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`S2_greedy`** (Matched Greedy) | 93.1% | 66.7% | 100.0% | 100.0% | 100.0% | 0.0% / 0.0% | 1.8 | 4.2 | 8.8 |
| **`S2_cpsat`** (Temporal CP-SAT) | 41.4% | 100.0% | 0.0% | 0.0% | 100.0% | 0.0% / 0.0% | 9.8 | 15.1 | 21.6 |
| **`S2_taxonomy`** (CP-SAT + Ontology) | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **100.0%** | **0.0% / 0.0%** | **13.7** | **29.2** | **40.7** |

- **Exact McNemar (Holm-Bonferroni adjusted, 116 held-out fixture scenarios):** `S2_taxonomy` outperforms matched `S2_greedy` by **`+6.9 pp`** (`n01=8, n10=0`, **`p = 0.0078`**) and temporal-only `S2_cpsat` by **`+58.6 pp`** (`p = 1.36e-20`).
- **Incremental Scaling Ablation (`python3 scripts/measure_ablation.py`):** Across tested itineraries containing **12 to 40 activities (3 to 10 days)**, localized dirty-closure p50 latency remained approximately **13-14 ms** (`13.57 ms` &rarr; `13.47 ms`), achieving a **5.36x speedup** over the measured full-trip replan baseline (`72.19 ms`) at 40 activities. Note that independent whole-graph K3 verification scales linearly **$O(|V|)$** (`1.2 ms` at 12 activities &rarr; `2.5 ms` at 40 activities).
- **Automated Test Suite (`pytest --cov=tripgraph`):** **78 / 78 passing** (**88%** statement coverage across **3,077 statements**).

<div align="center">
  <img src="./assets/incremental_ablation.svg" alt="Incremental Dirty Closure vs Full Replan Scaling (12-40 Activities)" width="96%" />
</div>

---

## 2-Minute Zero-Key Quickstart

```bash
git clone https://github.com/sainathmanda7/tripgraph-engine.git
cd tripgraph-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 1. Run all 78 tests + coverage report (zero-ortools API check, 25/25 oracle, 2^n proofs, MUS, SIGKILL, DST/cross-city)
make test

# 2. Reproduce the 116-scenario x 10-repeat benchmark into BENCHMARK_RESULTS.md
make bench

# 3. Launch the local FastAPI demo server (Swagger UI at http://127.0.0.1:8000/docs)
make serve
```

---

## Engineering Documentation Hub

| Document | Contents |
| :--- | :--- |
| **[`docs/architecture.md`](./docs/architecture.md)** | 8 Mermaid diagrams: C4 Container, Component Firewalls, K3 Pipeline, Dual-Graph Projections, Replanning Workflow, Deployment Topology, 2 Sequence Diagrams, and Data Flow. |
| **[`docs/system-design.md`](./docs/system-design.md)** | Tech stack rationale, SQLite ER schema & atomic commit fencing, explicit `UNKNOWN` commit contract, trade-offs, failure matrix, non-durable job limits, and roadmap. |
| **[`docs/adr/`](./docs/adr/)** | 5 Architecture Decision Records covering [Dual-Model IR](./docs/adr/001-dual-model-cpsat-ir.md), [SIGKILL Subprocess Pool](./docs/adr/002-subprocess-solver-pool-sigkill.md), [Kleene K3 & Pinned TZDB](./docs/adr/003-kleene-k3-pareto-verification.md), [Dirty Closure & LockGuard](./docs/adr/004-factor-graph-dirty-closure-lockguard.md), and [Fenced SQLite Commits & Async 202](./docs/adr/005-sqlite-fenced-commits-async-replan.md). |
| **[`docs/performance.md`](./docs/performance.md)** | Scoped benchmark tables, incremental scaling ablation (`12-40` activities), `88%` statement coverage (`3,077` statements), and failure-mode verification matrix. |
| **[`docs/case-study.md`](./docs/case-study.md)** | 1-page executive technical case study framed around multi-city travel platform integration and verified trade-offs. |
| **[`docs/api.md`](./docs/api.md)** | Full HTTP & SSE API specification, `200`/`202`/`409`/`503` contracts, `allow_shift_locked` semantics, and `curl` examples. |
| **[`examples/`](./examples/)** | Engine-verified 2-city Tokyo &rarr; Kyoto JSON payloads, `409 LOCKED_NODE_CONFLICT` refusal, and `202 Accepted` + SSE transcript. |

---

## Honest Operational Limits & Roadmap

- **Seeded Fixtures vs. Commercial GDS:** The public demo deployment runs against `FixtureGroundingProvider` (no live supplier contracts, DMC rate sheets, tax/currency pricing, or booking execution). Integrating with a travel platform requires plugging real supplier connectors behind `GroundingProvider`.
- **Non-Durable In-Memory Async Jobs:** Escalated `202 Accepted` global replans run as in-process `asyncio.create_task()` jobs stored in memory (`self.jobs`). An API process restart interrupts active jobs and drops SSE event history; clients must re-read `GET /trips/{id}` to inspect committed version state. Roadmap: durable Redis/Postgres job queue + PostgreSQL `SELECT ... FOR UPDATE`.
- **Single-Container Free-Tier Demo:** The Render Free demo runs `:memory:` SQLite on `0.1 vCPU` (state resets on restart; cold starts take ~30s).
- **Free-Text Semantic Evaluation:** Deterministic CI evaluates physical constraint feasibility and honest refusal on curated fixtures; live `S3` LLM sensory ranking vs. `S2_taxonomy` requires an OpenAI-compatible key plus 2-rater blind grading (`eval/blind_grade.py`).

See [`docs/system-design.md`](./docs/system-design.md#7-known-limitations--future-roadmap) for full details and [`CONTRIBUTING.md`](./CONTRIBUTING.md) to contribute.

---

## Author & License

Built by **Sainath Manda** &bull; [GitHub](https://github.com/sainathmanda7) &bull; [Portfolio](https://sainath.root10x.in)  
Released under the [MIT License](./LICENSE).
