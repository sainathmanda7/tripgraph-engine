# ADR-002: Bounded OS Subprocess Solver Pool with Process-Group SIGKILL

- **Status:** Accepted
- **Date:** 2026-11
- **Files:** `src/tripgraph/runtime/supervisor.py`, `src/tripgraph/runtime/worker_main.py`

## Context
`CpSolver.solve()` executes native C++ code. Calling it inside an `async def` FastAPI handler freezes Python's event loop, stalling `/health`, SSE streams, and concurrent requests. Moving it to a Python thread pool (`asyncio.to_thread`) still fails under adversarial timeouts because Python cannot forcibly terminate a blocked native C++ thread.

## Decision
Run a bounded pool of persistent child OS processes (`start_new_session=True`) communicating via 4-byte length-prefixed binary stdin/stdout pipes:
- Workers register Linux `prctl(PR_SET_PDEATHSIG, SIGKILL)` on startup so a master crash leaves zero orphan processes.
- On timeout or client cancellation, `SolverProcessSupervisor` issues `os.killpg(pgid, SIGKILL)`, reaps the PID asynchronously, and spawns a fresh replacement worker.
- An AST CI guard (`test_ast_api_layer_never_imports_ortools_in_process`) forbids `src/tripgraph/api/` from importing `ortools`.

## Alternatives Considered
- **`ThreadPoolExecutor` / `asyncio.to_thread`:** Hung C++ threads leak permanently and consume CPU quota on constrained containers.
- **Celery + Redis broker:** Adds operational complexity and multi-service overhead unnecessary for sub-20 ms local solves.

## Consequences
- **Positive:** FastAPI event loop stays 100% responsive during heavy solves; guaranteed hard cancellation (`<0.8s` kill + respawn verified in CI); workers hold zero DB credentials.
- **Negative:** Minor pickle IPC overhead (~1 ms) and slightly higher container RAM (~40 MB per worker process).
