"""Pre-recording live preflight check for TripGraph v7 demo walkthrough.

Usage:
  python3 scripts/preflight_demo.py https://tripgraph-engine.onrender.com
  python3 scripts/preflight_demo.py --local
"""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]

REQUIRED_MARKERS = (
    "DETERMINISTIC FIXTURE SCENARIO -- SEEDED VENUE EVIDENCE",
    "HTTP 201:",
    "VIOLATED_FALSE",
    "HTTP 200 | version=2",
    "PROVEN_OPTIMAL_IN_SCOPE",
    "VERIFIED_TRUE",
    "HTTP 409 | status=LOCKED_NODE_CONFLICT",
    "HTTP 202 | mode=GLOBAL_REPLAN_ESCALATED",
    '"stage": "completed"',
    "Final committed version=3",
)


def _check_zero_ortools() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, tripgraph.api.main; "
                "bad = [m for m in sys.modules if m == 'ortools' or m.startswith('ortools.')]; "
                "assert not bad, f'ortools leaked: {bad}'"
            ),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print("[NO-GO] API process imported ortools:", proc.stderr.strip())
        sys.exit(1)
    print("[OK] Fresh API import check: ortools_loaded=False")


def _warm_health(base_url: str, max_wait_sec: float = 65.0) -> float:
    deadline = time.monotonic() + max_wait_sec
    last_err = ""
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{base_url}/health", timeout=15.0)
            if r.status_code == 200:
                break
            last_err = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last_err = str(exc)
        time.sleep(2.0)
    else:
        print(f"[NO-GO] {base_url}/health did not recover within {max_wait_sec:.0f}s ({last_err})")
        sys.exit(1)

    # Measure warm round-trip latency
    t0 = time.perf_counter()
    r2 = httpx.get(f"{base_url}/health", timeout=10.0)
    r2.raise_for_status()
    rtt_ms = (time.perf_counter() - t0) * 1000.0
    print(f"[OK] Warm /health check at {base_url}: {rtt_ms:.0f} ms")
    return rtt_ms


def main() -> None:
    use_local = len(sys.argv) > 1 and sys.argv[1] == "--local"
    server_proc = None
    if use_local:
        base_url = "http://127.0.0.1:8766"
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tripgraph.api.main:app", "--host", "127.0.0.1", "--port", "8766"],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        base_url = (sys.argv[1] if len(sys.argv) > 1 else "https://tripgraph-engine.onrender.com").rstrip("/")

    try:
        _check_zero_ortools()
        rtt_ms = _warm_health(base_url)

        t0 = time.perf_counter()
        demo = subprocess.run(
            [sys.executable, "scripts/live_demo.py", base_url],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        total_sec = time.perf_counter() - t0

        if demo.returncode != 0:
            print(f"[NO-GO] live_demo.py exited with code {demo.returncode}")
            print(demo.stdout)
            print(demo.stderr)
            sys.exit(1)

        missing = [m for m in REQUIRED_MARKERS if m not in demo.stdout]
        if missing:
            print("[NO-GO] live_demo.py output missing required markers:", missing)
            print(demo.stdout)
            sys.exit(1)

        print(f"[OK] Full 5-step Tokyo -> Kyoto walkthrough passed in {total_sec:.2f}s (all assertions green).")
        if not use_local and (rtt_ms > 1500.0 or total_sec > 12.0):
            print("[WARN] Remote deployment is slow right now -- prefer recording against local `make serve` fallback.")
        else:
            print("[GO] Environment is warm and verified. Safe to hit record.")
    finally:
        if server_proc is not None:
            server_proc.terminate()
            server_proc.wait(timeout=5)


if __name__ == "__main__":
    main()
