"""Isolated OS subprocess worker executing native CP-SAT solves (TripGraph v7).

Security & Reliability Invariants:
- Runs in its own process group (spawned with start_new_session=True).
- Registers Linux PR_SET_PDEATHSIG(SIGKILL) BEFORE importing native OR-Tools,
  verifies prctl() return value == 0, and re-checks os.getppid() == expected_ppid
  to close the parent-exit race window.
- Holds ZERO database connections or write credentials.
- Communicates over binary 4-byte length-prefixed frames on stdin/stdout.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import pickle
import signal
import struct
import sys
import time
from typing import Any, Callable

_HEADER = struct.Struct("!I")
_PR_SET_PDEATHSIG = 1


def _install_linux_pdeathsig(
    expected_ppid: int | None = None,
    *,
    _platform: str | None = None,
    _libc: Any = None,
    _getppid: Callable[[], int] = os.getppid,
) -> None:
    """Arm Linux PR_SET_PDEATHSIG(SIGKILL) and fail loudly on any registration error."""
    plat = sys.platform if _platform is None else _platform
    if not plat.startswith("linux"):
        return

    if _libc is None:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        libc.prctl.argtypes = [
            ctypes.c_int,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
            ctypes.c_ulong,
        ]
        libc.prctl.restype = ctypes.c_int
    else:
        libc = _libc

    ret = int(libc.prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0))
    if ret != 0:
        err = ctypes.get_errno()
        raise RuntimeError(
            f"prctl(PR_SET_PDEATHSIG, SIGKILL) failed with ret={ret}, errno={err}"
        )

    actual_ppid = _getppid()
    if expected_ppid is not None and actual_ppid != expected_ppid:
        raise RuntimeError(
            f"Parent process exited before PR_SET_PDEATHSIG armed "
            f"(expected_ppid={expected_ppid}, actual_ppid={actual_ppid})"
        )


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


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--expected-ppid", type=int, default=None)
    args, _ = parser.parse_known_args(argv)

    # CRITICAL: Arm parent-death protection BEFORE importing native OR-Tools.
    try:
        _install_linux_pdeathsig(expected_ppid=args.expected_ppid)
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write(f"worker_main fatal pdeathsig error: {exc}\n")
        sys.stderr.flush()
        sys.exit(2)

    stdin_buf = sys.stdin.buffer
    stdout_buf = sys.stdout.buffer

    # Stage 1 handshake: confirm PR_SET_PDEATHSIG + ppid check succeeded (~40ms).
    _send_frame(stdout_buf, {"stage": "pdeathsig_armed", "pid": os.getpid()})

    # Late import: OR-Tools only enters memory inside the already-protected child worker.
    from tripgraph.solver.cpsat_engine import solve_ir

    # Stage 2 handshake: confirm native OR-Tools is loaded and worker is warm.
    _send_frame(stdout_buf, {"stage": "ready", "pid": os.getpid()})

    while True:
        hdr = _read_exact(stdin_buf, _HEADER.size)
        if hdr is None:
            break
        (length,) = _HEADER.unpack(hdr)
        payload_bytes = _read_exact(stdin_buf, length)
        if payload_bytes is None:
            break

        try:
            req = pickle.loads(payload_bytes)
            hang_sec = float(req.get("test_hang_sec", 0.0))
            if hang_sec > 0.0:
                time.sleep(hang_sec)

            res = solve_ir(
                req["ir"],
                opt_timeout_sec=float(req.get("opt_timeout_sec", 0.75)),
                diag_total_budget_ms=float(req.get("diag_total_budget_ms", 250.0)),
                diag_initial_sec=float(req.get("diag_initial_sec", 0.070)),
                mus_per_probe_sec=float(req.get("mus_per_probe_sec", 0.020)),
                run_mus=bool(req.get("run_mus", True)),
                opt_workers=int(req.get("opt_workers", 2)),
            )
            _send_frame(stdout_buf, {"ok": True, "result": res})
        except Exception as exc:  # noqa: BLE001
            _send_frame(stdout_buf, {"ok": False, "error": f"{type(exc).__name__}: {exc}"})


if __name__ == "__main__":
    main()
