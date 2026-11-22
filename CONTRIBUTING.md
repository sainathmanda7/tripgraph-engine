# Contributing to TripGraph v7

Contributions, bug reports, and solver/benchmark improvements are welcome. Because TripGraph enforces strict formal and runtime invariants, every pull request must preserve the architectural contracts below.

## Local Development Setup

```bash
git clone https://github.com/sainathmanda7/tripgraph-engine.git
cd tripgraph-engine
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
make test
```

## Non-Negotiable CI Invariants

1. **Zero In-Process Solver Calls in the API Layer:** `src/tripgraph/api/` must never import `ortools` or call `CpSolver.solve()` directly (enforced by AST test `test_ast_api_layer_never_imports_ortools_in_process`).
2. **Byte-Pinned Timezone Discipline:** Never call `zoneinfo.ZoneInfo(key)` directly; always resolve through `PinnedTzRegistry` (`ZoneInfo.from_file()`) (enforced by AST test `test_ast_bans_direct_zoneinfo_constructor`).
3. **Zero Silent Policy Pruning:** Every candidate disallowed by `VerificationPolicy` must remain in `CompiledConstraintIR` via an explicit `VERIFICATION_POLICY_GATE` assumption group.
4. **Frozen Benchmark Controls:** Do not modify `TAXONOMY_V1` or `RANKER_WEIGHTS_V1` without explicitly re-locking `eval/FROZEN_CONTROLS.lock` and noting the change in the PR description.
5. **Independent Reference Oracle Isolation:** `eval/reference_oracle/` must keep zero imports from `tripgraph.*`.

## Pull Request Checklist

- [ ] `pytest -v --cov=tripgraph --cov-report=term-missing` passes with zero failures.
- [ ] New solver or verifier behaviors include regression tests (and gold oracle cases where applicable).
- [ ] Documentation in `docs/` or `docs/adr/` is updated if an architectural boundary changed.
