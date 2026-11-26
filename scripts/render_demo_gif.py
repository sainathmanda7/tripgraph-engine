"""Capture live_demo.py output (local or remote URL) and render dark-mode assets/demo.gif."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys
import time

import httpx
from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT_GIF = ROOT / "assets" / "demo.gif"

BG = (13, 17, 23)
TITLEBAR = (22, 27, 34)
BORDER = (48, 54, 61)
WHITE = (230, 237, 243)
GREEN = (0, 255, 136)
YELLOW = (250, 204, 21)
CYAN = (56, 189, 248)
RED = (248, 113, 113)
MUTED = (139, 148, 158)

W, H = 1080, 760
PAD_X, TOP_Y = 24, 54
LINE_H = 19


def _line_color(line: str) -> tuple[int, int, int]:
    s = line.strip()
    if s.startswith("===") or "DETERMINISTIC FIXTURE" in s:
        return YELLOW
    if any(k in s for k in ("VIOLATED_FALSE", "CLOSED_BY_AUTHORITATIVE_SOURCE", "HTTP 409", "LOCKED_NODE_CONFLICT")):
        return RED
    if any(k in s for k in ("PROVEN_OPTIMAL", "VERIFIED_TRUE", "blast_radius", "completed", "Final committed", "HTTP 200", "HTTP 201")):
        return GREEN
    if s.startswith("data:") or "HTTP 202" in s or "GLOBAL_REPLAN_ESCALATED" in s:
        return CYAN
    return WHITE


def _render_frame(lines: list[tuple[str, tuple[int, int, int]]], font, show_cursor: bool = False) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, W - 1, 38], fill=TITLEBAR)
    draw.line([(0, 38), (W - 1, 38)], fill=BORDER, width=1)
    for idx, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = 22 + idx * 20
        draw.ellipse([cx - 6, 13, cx + 6, 25], fill=col)
    draw.text((W // 2 - 235, 11), "tripgraph-engine -- seeded fixture demo (Tokyo -> Kyoto)", fill=MUTED, font=font)

    visible = lines[-35:]
    y = TOP_Y
    for text, color in visible:
        if text.startswith("$ "):
            draw.text((PAD_X, y), "$ ", fill=GREEN, font=font)
            draw.text((PAD_X + 18, y), text[2:], fill=WHITE, font=font)
            if show_cursor and text is visible[-1][0]:
                cx = PAD_X + 18 + int(draw.textlength(text[2:], font=font)) + 2
                draw.rectangle([cx, y + 2, cx + 9, y + 16], fill=GREEN)
        else:
            draw.text((PAD_X, y), text[:114], fill=color, font=font)
        y += LINE_H
    return img


def _wait_health(url: str, timeout_sec: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_sec
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/health", timeout=2.0)
            if r.status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Server did not become healthy at {url}")


def main() -> None:
    use_local = len(sys.argv) > 1 and sys.argv[1] == "--local"
    server_proc = None
    if use_local:
        target = "http://127.0.0.1:8765"
        cmd_display = "python3 scripts/live_demo.py http://127.0.0.1:8000"
        server_proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "tripgraph.api.main:app", "--host", "127.0.0.1", "--port", "8765"],
            cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    else:
        target = sys.argv[1] if len(sys.argv) > 1 else "https://tripgraph-engine.onrender.com"
        cmd_display = f"python3 scripts/live_demo.py {target}"

    try:
        _wait_health(target, timeout_sec=45.0)
        proc = subprocess.run(
            [sys.executable, "scripts/live_demo.py", target],
            cwd=ROOT, capture_output=True, text=True, check=True,
        )
    finally:
        if server_proc is not None:
            server_proc.terminate()
            server_proc.wait(timeout=5)

    raw_lines = [ln for ln in proc.stdout.strip().splitlines() if ln.strip() not in ("{", "}", "[", "]", "},")]
    font = ImageFont.truetype(font_manager.findfont("DejaVu Sans Mono"), 14)

    frames: list[Image.Image] = []
    durations: list[int] = []
    for end in range(4, len(cmd_display) + 1, 5):
        frames.append(_render_frame([(f"$ {cmd_display[:end]}", WHITE)], font, show_cursor=True))
        durations.append(70)
    frames.append(_render_frame([(f"$ {cmd_display}", WHITE)], font, show_cursor=True))
    durations.append(400)

    shown: list[tuple[str, tuple[int, int, int]]] = [(f"$ {cmd_display}", WHITE), ("", WHITE)]
    for ln in raw_lines:
        shown.append((ln, _line_color(ln)))
        s = ln.strip()
        if s.startswith("===") or s.startswith("data:") or s.startswith("HTTP ") or "Final committed" in s:
            frames.append(_render_frame(list(shown), font))
            durations.append(3600 if "Final committed" in s else (420 if s.startswith("===") else 320))

    frames.append(_render_frame(list(shown), font))
    durations.append(3600)

    p_frames = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
    p_frames[0].save(OUT_GIF, save_all=True, append_images=p_frames[1:], duration=durations, loop=0, optimize=True)
    print(f"Rendered {OUT_GIF} ({OUT_GIF.stat().st_size / 1024:.1f} KB, {len(frames)} frames).")


if __name__ == "__main__":
    main()
