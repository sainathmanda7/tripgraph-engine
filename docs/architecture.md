# TripGraph v7 — Architecture & System Design Diagrams

Visual architecture views of `tripgraph-engine` across container, component, verification, dual-graph, workflow, deployment, sequence, and data-flow perspectives. Every boundary below is enforced in code and verified in CI.

---

## 1. High-Level System Architecture (C4 Container)

```mermaid
flowchart TB
    User["Traveler / Client App / Swagger UI"]

    subgraph System["TripGraph v7 Engine (Docker Container)"]
        API["FastAPI HTTP + SSE Gateway<br/>(tripgraph.api.main)<br/>Zero ortools in sys.modules (Fresh-Process + AST Guarded)"]
        Orch["TripOrchestrator<br/>(tripgraph.runtime.orchestrator)<br/>8s Sync / In-Memory Async GlobalReplan (LockGuard + K3 on ALL Paths)"]
        Ver["K3 Independent Verifier<br/>(tripgraph.verifier)<br/>Pareto Evidence + Hotel State Machine"]
        Sup["SolverProcessSupervisor<br/>(tripgraph.runtime.supervisor)<br/>Bounded Pool + Queue Admission"]
        Workers["Isolated OS Solver Workers<br/>(worker_main.py via pipes)<br/>OR-Tools CP-SAT + PR_SET_PDEATHSIG(SIGKILL)<br/>ZERO Database Credentials"]
        Store[("FencedTripStore (SQLite)<br/>trips + edit_requests<br/>Atomic Version + Request Fencing")]
    end

    subgraph External["Grounding & Optional LLM Providers"]
        Grounding["GroundingProvider<br/>(Tier A Fixtures / Tier B Chaos /<br/>Tier C Overpass, OSRM, GTFS)"]
        LLM["OpenAI-Compatible LLM<br/>(Optional Phase B S3 Only)<br/>Strict Whitelist + Consent Guard"]
    end

    User -->|"HTTPS JSON / SSE"| API
    API -->|"Async Dispatch"| Orch
    Orch -->|"Dirty Closure & IR Compile"| Sup
    Orch -->|"Pre-Commit Proof Check"| Ver
    Orch -->|"Fenced Atomic SQLite Commit"| Store
    Orch -->|"BudgetedProviderProxy<br/>(1 query / <=20 fetch / <=5 CP-SAT)"| Grounding
    Orch -.->|"Optional Phase B<br/>(SearchIntent + SemanticRank)"| LLM
    Sup -->|"Length-Prefixed Binary IPC"| Workers
```

---

## 2. Detailed Component Architecture

Internal package boundaries and the two **AST-enforced firewalls** verified on every CI run (`tests/test_milestone1_time_and_schema.py` and `tests/test_milestone6_runtime_and_api.py`).

```mermaid
flowchart LR
    subgraph API_Layer["src/tripgraph/api/ (AST Firewall: NO ortools)"]
        MainAPI["main.py<br/>FastAPI Routes & SSE"]
    end

    subgraph Runtime_Layer["src/tripgraph/runtime/"]
        Orch["orchestrator.py<br/>DeadlineContext + GlobalReplan"]
        Sup["supervisor.py<br/>Subprocess Pool + SIGKILL"]
        Wkr["worker_main.py<br/>Child OS Process"]
        Store["store.py<br/>FencedTripStore"]
    end

    subgraph Core_Graph["src/tripgraph/graph/"]
        Closure["closure.py<br/>Day-Bin Dirty Closure"]
        Freeze["freeze.py<br/>SHA-256 LockGuard"]
        Proj["projections.py<br/>Exact Global & Markov Projections"]
    end

    subgraph Solver_Layer["src/tripgraph/solver/"]
        IR["ir.py<br/>CompiledConstraintIR"]
        CPSAT["cpsat_engine.py<br/>Dual-Model + Bounded MUS"]
    end

    subgraph Repair_Grounding["src/tripgraph/repair/ & grounding/"]
        Tax["taxonomy.py<br/>S2_taxonomy & S2_greedy"]
        PhaseB["phase_b.py<br/>Tool-Mediated S3"]
        Prov["provider.py / fixtures.py /<br/>chaos.py / live_adapters.py"]
    end

    subgraph Verification_Foundation["src/tripgraph/verifier/, schema/, time/"]
        VEng["engine.py / evidence.py / hotel.py<br/>Kleene K3 Verifier"]
        Schema["models.py / edits.py<br/>Pydantic v2 Axioms"]
        TZ["tz_registry.py<br/>AST Firewall: ZoneInfo.from_file() ONLY"]
    end

    MainAPI --> Orch
    Orch --> Closure & Freeze & IR & Sup & Store & Tax & PhaseB & VEng
    Sup -.->|"OS Pipe IPC"| Wkr
    Wkr --> CPSAT
    IR --> CPSAT
    Tax & PhaseB --> Prov
    Closure & Freeze & Proj --> Schema
    IR & VEng --> Schema & TZ
    Prov --> Schema & TZ
```

---

## 3. Neuro-Symbolic Verification Pipeline

Resolves multi-source venue evidence and hotel contracts under Kleene three-valued logic (`TRUE`, `FALSE`, `UNKNOWN`) without fabricating certainty from stale, conflicting, or unverified holiday data.

```mermaid
flowchart LR
    subgraph Inputs["Raw Itinerary & Grounding"]
        Trip["Candidate TripGraph"]
        Ev["Multi-Source Evidence<br/>(Tier 1 Official / Tier 2 API /<br/>Tier 3 OSM Community)"]
        Hotel["HotelBookingContract<br/>(no_show_cutoff, access_method,<br/>pre_checkin_bag_drop)"]
    end

    subgraph TimeLayer["Byte-Pinned Time"]
        TZ["PinnedTzRegistry<br/>ZoneInfo.from_file() + SHA-256<br/>Exact DST Gap & Fold Semantics"]
    end

    subgraph K3Core["Kleene K3 Resolution"]
        Pareto["Pareto Single-Witness Filter<br/>Specificity > Authority > Freshness<br/>(Never merges attributes across records)"]
        Hol["Holiday & Conflict Guard<br/>Holiday UNKNOWN -> UNKNOWN<br/>Frontier Conflict -> UNKNOWN"]
        HSM["Hotel State Machine<br/>Reserved Nights, Late-Notify,<br/>24h / Staffed / Contactless / Bag-Drop"]
    end

    subgraph Output["Two-Axis Verification Certificate"]
        Cert["VerificationReport<br/>hard_constraints_status: TRUE | FALSE | UNKNOWN<br/>verification_certificate:<br/>VERIFIED_TRUE | CONDITIONAL_ON_ASSUMPTIONS | VIOLATED_FALSE"]
        Oracle["Independent Reference Oracle<br/>(eval/reference_oracle/ — dateutil)<br/>25/25 Gold Dual-Agreement CI Proof"]
    end

    Trip & Ev & Hotel --> TZ
    TZ --> Pareto & HSM
    Pareto --> Hol
    Hol & HSM --> Cert
    Cert -.->|"Cross-Checked in CI"| Oracle
```

---

## 4. Dual-Graph Architecture (Acyclic Spine + Cyclic Constraint Factor Graph)

Separates the acyclic itinerary hierarchy from cyclic cross-day constraints (budget, venue anti-duplication, category quotas, intra-day Markov adjacency) and projects frozen nodes into exact constants on the dirty subgraph.

```mermaid
flowchart TB
    subgraph Spine["1. Acyclic Structural Spine (Tree / DAG)"]
        T["TripGraph (v=k)"]
        S1["CityStay 1: Tokyo<br/>(HotelBookingContract)"]
        S2["CityStay 2: Kyoto<br/>(HotelBookingContract)"]
        Leg["TransitLeg: Shinkansen<br/>(Discrete Rail Slot)"]
        D1["Day 1: Sun (FROZEN)<br/>SHA-256 Locked"]
        D2["Day 2: Mon (DIRTY)<br/>Mutable + Locked Anchor"]
        A1["a_sun_park"]
        A2["a_sun_cafe"]
        A3["a_mon_shrine (LOCKED)"]
        A4["a_mon_art (MUTABLE)"]

        T --> S1 & S2
        S1 --> Leg --> S2
        S1 --> D1 & D2
        D1 --> A1 & A2
        D2 --> A3 & A4
    end

    subgraph Factors["2. Cyclic Constraint Factor Graph (Projected onto Dirty Subgraph)"]
        F_Budget(["F_Budget<br/>Remaining USD = Budget - FrozenCost"])
        F_AntiDup(["F_AntiDup<br/>Exclude Frozen Place IDs"])
        F_Quota(["F_CategoryQuota<br/>Rem Quota = Limit - FrozenCount"])
        F_DayBin(["F_DayBin (Mon)<br/>Daily Active Min + Transit + Order-1 Markov"])
    end

    A1 & A2 -.->|"Exact Constant Summary<br/>(2^n Equivalence Proven)"| F_Budget & F_AntiDup & F_Quota
    F_Budget & F_AntiDup & F_Quota --> A3 & A4
    F_DayBin --> A3 & A4
```

---

## 5. Incremental Replanning Workflow

End-to-end decision pipeline inside `TripOrchestrator.apply_edit()`, separating fast synchronous repairs (`8,000 ms` hard ceiling) from heavy global restructurings (`30,000 ms` async worker).

```mermaid
flowchart TD
    Start["PATCH /trips/{id}/edit"] --> Closure["Compute Factor-Graph Dirty Closure"]
    Closure --> AllLock{"All nodes<br/>locked?"}
    AllLock -->|"Yes"| VerifyOnly["Verify Direct Edit Only<br/>(No Divide-by-Zero)"]
    AllLock -->|"No"| Ratio{"dirty_ratio > 0.50<br/>OR Non-Projectable?"}

    Ratio -->|"Yes"| Async["Return 202 Accepted<br/>Enqueue 30s GlobalReplanMode<br/>Stream SSE /jobs/{id}/events"]
    Ratio -->|"No (<= 50%)"| Freeze["Capture SHA-256 FreezeSnapshot<br/>Compile CompiledConstraintIR"]

    Freeze --> H0["Subprocess OptimizationModel<br/>(Multi-Worker, Dynamic Weights < 2^53)"]
    H0 --> H0Stat{"Status?"}
    H0Stat -->|"OPTIMAL / FEASIBLE"| LockCheck["Enforce SHA-256 LockGuard<br/>+ Independent K3 Verifier"]
    H0Stat -->|"TIMEOUT / OVERLOAD"| Err503["Abort Request -> 503"]
    H0Stat -->|"INFEASIBLE"| Diag["Subprocess DiagnosticFeasibilityModel<br/>(1 Worker, Zero Objective, Cloned Intervals)<br/>Bounded MUS (<=6 probes, 250ms)"]

    Diag --> CoreCheck{"Core Classification?"}
    CoreCheck -->|"Empty Core []"| Err500["500 INTERNAL_BASE_AXIOM_CONTRADICTION"]
    CoreCheck -->|"Policy Gate in Core"| Err409Pol["409 INFEASIBLE_UNDER_VERIFICATION_POLICY"]
    CoreCheck -->|"VENUE_CLOSED / DIETARY<br/>on Unlocked Node"| Recov["BudgetedProviderProxy<br/>S2_taxonomy (0 LLM) OR<br/>Phase B S3 (Whitelist + Consent Guard)"]
    CoreCheck -->|"Hard Lock / Budget Conflict"| Err409Phys["409 PROVEN_PHYSICALLY_INFEASIBLE<br/>(With PROVEN_MUS Core)"]

    Recov --> RecovOK{"CP-SAT Proof<br/>Feasible?"}
    RecovOK -->|"Yes"| LockCheck
    RecovOK -->|"LLM Outage (consent=False)"| Err503LLM["503 RETRYABLE_UPSTREAM_LLM_UNAVAILABLE<br/>(Zero Silent Activity Deletion)"]
    RecovOK -->|"No"| Err409Phys

    LockCheck --> Commit["Atomic Fenced SQLite Commit<br/>WHERE version=? AND req=ACTIVE"]
    Commit --> Done200["200 OK (version+1, 2-Axis Certificate,<br/>Blast Radius = 0.0)"]
```

---

## 6. Public Demo Deployment Architecture

Runtime isolation topology running on the public demo deployment at `https://tripgraph-engine.onrender.com` (seeded `FixtureGroundingProvider`, `:memory:` SQLite, in-memory `asyncio` global jobs).

```mermaid
flowchart TB
    Client["Client / Browser / curl"] -->|"TLS 1.3 HTTPS"| Ingress["Render Edge Reverse Proxy<br/>Routes :443 -> Container :8000"]

    subgraph Container["Docker Container (python:3.11-slim)"]
        Uvicorn["Uvicorn ASGI Event Loop<br/>(FastAPI Lifespan Manager)<br/>100% Non-Blocking Async I/O"]
        SQLite[("SQLite DB (FencedTripStore)<br/>trips + edit_requests")]

        subgraph Pool["SolverProcessSupervisor (Bounded Pool)"]
            W1["OS Worker Process 1<br/>start_new_session=True (Own PGID)<br/>prctl(PR_SET_PDEATHSIG) BEFORE ortools + ppid Check<br/>Native OR-Tools C++ CP-SAT"]
            W2["OS Worker Process 2<br/>start_new_session=True (Own PGID)<br/>prctl(PR_SET_PDEATHSIG) BEFORE ortools + ppid Check<br/>Native OR-Tools C++ CP-SAT"]
        end

        Uvicorn <-->|"Thread-Safe Fenced SQL"| SQLite
        Uvicorn <-->|"Length-Prefixed Binary<br/>stdin/stdout Pipes"| W1 & W2
        Uvicorn -.->|"On Timeout/Cancel:<br/>os.killpg(PGID, SIGKILL) + Respawn"| Pool
    end

    Ingress --> Uvicorn
```

---

## 7. Sequence Diagrams: Verification & Incremental Replanning

### 7A. Independent Verification Sequence (`POST /trips/{trip_id}/verify`)

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Gateway
    participant Store as FencedTripStore
    participant Verifier as K3 Verifier Engine
    participant TZ as PinnedTzRegistry
    participant Grounding as GroundingProvider

    Client->>API: POST /trips/{trip_id}/verify
    API->>Store: get_trip(trip_id)
    Store-->>API: CommittedTrip(graph, version)
    API->>Grounding: all_places() + get_public_holidays()
    Grounding-->>API: Grounded Place Registry & Holidays
    API->>Verifier: verify_trip_graph(graph, places, transit_fn, now_utc, tz_registry)
    loop Every ActivityNode & CityStay
        Verifier->>TZ: resolve_local_to_utc(local_date, time, pinned_tz)
        TZ-->>Verifier: Exact UTC Epoch (DST gap/fold aware)
        Verifier->>Verifier: Pareto Evidence Resolution (K3: TRUE / FALSE / UNKNOWN)
        Verifier->>Verifier: Hotel Contract State Machine & Transit Physics
    end
    Verifier-->>API: VerificationReport(status, certificate, violations, assumptions)
    API-->>Client: 200 OK (JSON VerificationReport)
```

### 7B. Incremental Replanning Sequence (`PATCH /trips/{trip_id}/edit`)

```mermaid
sequenceDiagram
    autonumber
    actor Client
    participant API as FastAPI Gateway
    participant Orch as TripOrchestrator
    participant Store as FencedTripStore (SQLite)
    participant Sup as SolverProcessSupervisor
    participant Worker as OS Worker (CP-SAT)
    participant Proxy as BudgetedProviderProxy
    participant Verifier as K3 Verifier + LockGuard

    Client->>API: PATCH /trips/{id}/edit (move_activity on Monday)
    API->>Orch: apply_edit(trip_id, edit, budget_ms=8000)
    Orch->>Store: register_request(req_id, trip_id, deadline_utc)
    Orch->>Orch: compute_dirty_closure() -> dirty_ratio <= 0.50
    Orch->>Orch: FreezeSnapshot.capture() (SHA-256 of untouched Sunday)
    Orch->>Sup: solve(CompiledConstraintIR, hard_deadline)
    Sup->>Worker: Binary IPC Frame (OptimizationModel)
    Worker-->>Sup: INFEASIBLE -> DiagnosticFeasibilityModel + MUS
    Sup-->>Orch: PROVEN_PHYSICALLY_INFEASIBLE (Core: VENUE_CLOSED a_mon_art)
    Orch->>Proxy: expand_candidates_deterministic() [1 query, <=20 fetched, <=5 to CP-SAT]
    Proxy-->>Orch: Open Monday Alternatives (tyo_national_art, tyo_2121_design, ...)
    Orch->>Sup: solve(Expanded IR)
    Sup->>Worker: Binary IPC Frame (OptimizationModel)
    Worker-->>Sup: PROVEN_OPTIMAL_IN_SCOPE (swaps a_mon_art -> tyo_national_art)
    Orch->>Verifier: assert_lock_guard(snapshot) + verify_trip_graph()
    Verifier-->>Orch: LockGuard CLEAN + VERIFIED_TRUE
    Orch->>Store: commit_fenced(version=1 -> 2 WHERE req_id=ACTIVE & unexpired)
    Store-->>Orch: CommittedTrip(version=2)
    Orch-->>API: EditOutcome(200 OK, blast_radius=0.0)
    API-->>Client: 200 OK (version=2, PROVEN_OPTIMAL_IN_SCOPE, VERIFIED_TRUE)
```

---

## 8. Data-Flow Diagram (Inputs, Transformations & Certified Outputs)

```mermaid
flowchart LR
    subgraph Inputs["Inputs"]
        InEdit["EditCommand JSON<br/>(move / drop / swap_hotel /<br/>dietary / extend / shift)"]
        InGraph["Committed TripGraph<br/>(version = v)"]
        InData["Grounded Places,<br/>Holidays & Transit Matrix"]
    end

    subgraph Transforms["Deterministic & Neuro-Symbolic Transformations"]
        T1["1. Dirty Closure<br/>Partition: Frozen vs Dirty Days"]
        T2["2. FreezeSnapshot<br/>SHA-256 Digest of Untouched Nodes"]
        T3["3. Constraint Compiler<br/>CompiledConstraintIR<br/>(Projected Budget/Quotas + Policy Gates)"]
        T4["4. Dual-Model CP-SAT<br/>OptModel -> Assignments<br/>OR DiagModel -> MUS Core"]
        T5["5. Grounded Recovery<br/>BudgetedProviderProxy -> Top-5<br/>(S2_taxonomy or Phase B S3)"]
        T6["6. Post-Solve Barriers<br/>LockGuard + K3 Verifier<br/>+ Fenced SQLite Commit"]
    end

    subgraph Outputs["Certified Outputs"]
        Out200["200 OK: TripGraph (v+1)<br/>solver_certificate + verification_certificate<br/>+ blast_radius metrics"]
        Out202["202 Accepted: Async Job<br/>SSE Stage Stream -> v+1"]
        Out409["409 Conflict: Honest Refusal<br/>PROVEN_MUS Core Groups<br/>(Physical vs Policy Separated)"]
    end

    InEdit & InGraph & InData --> T1
    T1 -->|"<= 50% dirty"| T2 --> T3 --> T4
    T1 -->|"> 50% dirty"| Out202
    T4 -->|"FEASIBLE/OPTIMAL"| T6 --> Out200
    T4 -->|"Recoverable UNSAT"| T5 --> T4
    T4 -->|"Terminal UNSAT"| Out409
```
