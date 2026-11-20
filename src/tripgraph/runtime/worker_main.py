"""Isolated OS subprocess worker executing native CP-SAT solves (TripGraph v7).

Security & Reliability Invariants:
- Runs in its own process group (spawned with start_new_session=True).
- Registers Linux PR_SET_PDEATHSIG(SIGKILL) so any master crash immediately
  terminates this child process (zero orphaned CP-SAT workers).
- Holds ZERO database connections or write credentials.
- Communicates over binary 4-byte length-prefixed frames on stdin/stdout.
  (Never prints text to stdout so binary framing stays 100% clean.)
"""

from __future__ import annotations

import ctypes
import pickle
import signal
import struct
import sys
import time

from tripgraph.solver.cpsat_engine import solve_ir

_HEADER = struct.Struct("!I")
_PR_SET_PDEATHSIG = 1


def _install_linux_pdeathsig() -> None:
    """Ask the Linux kernel to SIGKILL this worker if the parent process dies."""
    if not sys.platform.startswith("linux"):
        return
    try:
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL)
    except OSError:
        pass


def _read_exact(stream, n: int) -> bytes | None:
    chunks: list[bytes] = []
    remaining = n
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def _send_frame(stream, obj: object) -> None:
    data = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
    stream.write(_HEADER.pack(len(data)))
    stream.write(data)
    stream.flush()


def main() -> None:
    _install_linux_pdeathsig()
    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer

    while True:
        hdr = _read_exact(stdin_buf, _HEADER.size)
        if hdr is None:
            # Parent closed pipe (clean shutdown or crash) -> exit immediately
            break
        (length,) = _HEADER.unpack(hdr)
        payload_bytes = _read_exact(stdin_buf, length)
        if payload_bytes is None:
            break

        try:
            req = pickle.loads(payload_bytes)
            # Test-only hook to simulate a hung C++ solver and verify SIGKILL recovery
            hang_sec = float(req.get("test_hang_sec", 0.0))
            if hang_sec > 0.0:
                time.sleep(hang_sec)

            res = solve_ir(
                req["ir"],
                opt_timeout_sec=float(req.get("opt_timeout_sec", 0.75)),
                diag_total_budget_ms=float(req.get("diag_total_budget_ms", 250.0)),
                run_mus=bool(req.get("run_mus", True)),
                opt_workers=int(req.get("opt_workers", 2)),
            )
            _send_frame(stdout_buf, {"ok": True, "result": res})
        except Exception as exc:  # noqa: BLE001
            _send_frame(stdout_buf, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
