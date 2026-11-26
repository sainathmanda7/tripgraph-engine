"""Bounded async subprocess supervisor for non-blocking CP-SAT execution (TripGraph v7).

Guarantees:
1. FastAPI's asyncio event loop NEVER invokes native CpSolver.solve() in-process.
2. Enforces bounded worker pool (max_workers), max queue depth, and 250ms queue wait.
3. On solve timeout or request cancellation: sends SIGKILL to the worker's
   process group, reaps the PID asynchronously, and spawns a fresh replacement worker.
"""

from __future__ import annotations

import asyncio
import os
import pickle
import signal
import struct
import sys
from dataclasses import dataclass

from tripgraph.solver.types import DualSolveResult
from tripgraph.solver.ir import CompiledConstraintIR

_HEADER = struct.Struct("!I")


class SolverOverloadedError(RuntimeError):
    """Raised when solver queue is full or queue wait exceeds 250ms."""


class SolverTimeoutError(TimeoutError):
    """Raised when a solver worker exceeds its hard stage deadline and is SIGKILLed."""


@dataclass(eq=False)
class _WorkerHandle:
    worker_id: int
    proc: asyncio.subprocess.Process
    pgid: int


class SolverProcessSupervisor:
    """Async manager for a fixed pool of persistent isolated CP-SAT subprocesses."""

    def __init__(
        self,
        *,
        max_workers: int = 2,
        max_queue_depth: int = 16,
        queue_wait_sec: float = 0.25,
    ) -> None:
        self.max_workers = max(1, max_workers)
        self.max_queue_depth = max_queue_depth
        self.queue_wait_sec = queue_wait_sec
        self._idle: asyncio.Queue[_WorkerHandle] = asyncio.Queue()
        self._all_workers: set[_WorkerHandle] = set()
        self._in_flight = 0
        self._next_id = 1
        self._started = False
        self._closed = False
        self.kills_count = 0

    async def start(self) -> None:
        if self._started:
            return
        self._closed = False
        for _ in range(self.max_workers):
            w = await self._spawn_worker()
            await self._idle.put(w)
        self._started = True

    async def _spawn_worker(self) -> _WorkerHandle:
        wid = self._next_id
        self._next_id += 1
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "tripgraph.runtime.worker_main",
            "--expected-ppid",
            str(os.getpid()),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # Own process group for atomic SIGKILL
        )
        assert proc.pid is not None and proc.stdout is not None
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            pgid = proc.pid
        handle = _WorkerHandle(worker_id=wid, proc=proc, pgid=pgid)
        self._all_workers.add(handle)

        # Wait for worker to arm PR_SET_PDEATHSIG and confirm readiness.
        try:
            async with asyncio.timeout(15.0):
                hdr = await proc.stdout.readexactly(_HEADER.size)
                (ready_len,) = _HEADER.unpack(hdr)
                ready_bytes = await proc.stdout.readexactly(ready_len)
            ready_msg = pickle.loads(ready_bytes)
            if not ready_msg.get("ready"):
                raise RuntimeError(f"Worker {wid} unexpected startup frame: {ready_msg!r}")
        except Exception as exc:
            stderr_msg = ""
            if proc.stderr is not None:
                try:
                    stderr_bytes = await asyncio.wait_for(proc.stderr.read(), timeout=0.5)
                    stderr_msg = stderr_bytes.decode("utf-8", errors="replace").strip()
                except Exception:
                    pass
            await self._kill_and_reap(handle)
            raise RuntimeError(
                f"Worker {wid} failed startup handshake: {exc}"
                + (f" | stderr: {stderr_msg}" if stderr_msg else "")
            ) from exc

        return handle

    async def _kill_and_reap(self, handle: _WorkerHandle) -> None:
        self._all_workers.discard(handle)
        proc = handle.proc
        if proc.returncode is None:
            try:
                os.killpg(handle.pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except PermissionError:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                pass
        self.kills_count += 1

    async def shutdown(self) -> None:
        self._closed = True
        self._started = False
        while not self._idle.empty():
            try:
                self._idle.get_nowait()
            except asyncio.QueueEmpty:
                break
        for handle in list(self._all_workers):
            await self._kill_and_reap(handle)

    async def solve(
        self,
        ir: CompiledConstraintIR,
        *,
        hard_deadline_sec: float = 1.0,
        opt_timeout_sec: float = 0.75,
        diag_total_budget_ms: float = 250.0,
        run_mus: bool = True,
        opt_workers: int = 2,
        test_hang_sec: float = 0.0,
    ) -> DualSolveResult:
        """Dispatch IR to a subprocess worker with hard timeout + SIGKILL enforcement."""
        if not self._started:
            await self.start()
        if self._closed:
            raise RuntimeError("SolverProcessSupervisor is shut down.")
        if hard_deadline_sec <= 0.02:
            raise SolverTimeoutError("Remaining solve budget <= 20ms before IPC dispatch.")
        if self._in_flight >= self.max_workers + self.max_queue_depth:
            raise SolverOverloadedError("Solver admission queue saturated.")

        self._in_flight += 1
        worker: _WorkerHandle | None = None
        worker_healthy = True

        try:
            try:
                worker = await asyncio.wait_for(
                    self._idle.get(), timeout=self.queue_wait_sec
                )
            except asyncio.TimeoutError as exc:
                raise SolverOverloadedError(
                    f"Solver queue wait exceeded {self.queue_wait_sec * 1000:.0f}ms."
                ) from exc

            # Replace dead worker proactively if needed
            if worker.proc.returncode is not None:
                await self._kill_and_reap(worker)
                worker = await self._spawn_worker()

            assert worker.proc.stdin is not None and worker.proc.stdout is not None
            payload = pickle.dumps(
                {
                    "ir": ir,
                    "opt_timeout_sec": opt_timeout_sec,
                    "diag_total_budget_ms": diag_total_budget_ms,
                    "run_mus": run_mus,
                    "opt_workers": opt_workers,
                    "test_hang_sec": test_hang_sec,
                },
                protocol=pickle.HIGHEST_PROTOCOL,
            )

            try:
                async with asyncio.timeout(hard_deadline_sec):
                    worker.proc.stdin.write(_HEADER.pack(len(payload)))
                    worker.proc.stdin.write(payload)
                    await worker.proc.stdin.drain()

                    hdr = await worker.proc.stdout.readexactly(_HEADER.size)
                    (resp_len,) = _HEADER.unpack(hdr)
                    resp_bytes = await worker.proc.stdout.readexactly(resp_len)
            except (TimeoutError, asyncio.TimeoutError) as exc:
                worker_healthy = False
                raise SolverTimeoutError(
                    f"Worker {worker.worker_id} exceeded hard deadline {hard_deadline_sec:.3f}s; SIGKILLed."
                ) from exc
            except asyncio.CancelledError:
                worker_healthy = False
                raise
            except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError) as exc:
                worker_healthy = False
                raise RuntimeError(f"Worker {worker.worker_id} pipe broken: {exc}") from exc

            resp = pickle.loads(resp_bytes)
            if not resp.get("ok"):
                raise RuntimeError(f"Worker solve error: {resp.get('error')}")
            return resp["result"]

        finally:
            self._in_flight -= 1
            if worker is not None:
                if worker_healthy and not self._closed:
                    await self._idle.put(worker)
                else:
                    await self._kill_and_reap(worker)
                    if not self._closed:
                        replacement = await self._spawn_worker()
                        await self._idle.put(replacement)
