"""Capture live output from scripts/live_demo.py and render crisp dark-mode assets/demo.gif."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from matplotlib import font_manager
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT_GIF = ROOT / "assets" / "demo.gif"

# High-contrast dark terminal palette
BG = (13, 17, 23)         # #0D1117
TITLEBAR = (22, 27, 34)   # #161B22
BORDER = (48, 54, 61)     # #30363D
WHITE = (230, 237, 243)   # #E6EDF3
GREEN = (0, 255, 136)     # #00FF88
YELLOW = (250, 204, 21)   # #FACC15
CYAN = (56, 189, 248)     # #38BDF8
RED = (248, 113, 113)     # #F87171
MUTED = (139, 148, 158)   # #8B949E

W, H = 1040, 740
PAD_X, TOP_Y = 26, 56
LINE_H = 19


def _line_color(line: str) -> tuple[int, int, int]:
    s = line.strip()
    if s.startswith("==="):
        return YELLOW
    if "VIOLATED_FALSE" in s or "CLOSED_BY_AUTHORITATIVE_SOURCE" in s or '"FALSE"' in s:
        return RED
    if any(k in s for k in ("PROVEN_OPTIMAL", "VERIFIED_TRUE", "blast_radius", "completed", "Final committed", "HTTP 200", "HTTP 201")):
        return GREEN
    if s.startswith("data:") or "HTTP 202" in s or "GLOBAL_REPLAN_ESCALATED" in s:
        return CYAN
    if s.startswith("$ "):
        return WHITE
    return WHITE


def _render_frame(lines: list[tuple[str, tuple[int, int, int]]], font, show_cursor: bool = False) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # Window frame & macOS/Linux-style titlebar
    draw.rectangle([0, 0, W - 1, 38], fill=TITLEBAR)
    draw.line([(0, 38), (W - 1, 38)], fill=BORDER, width=1)
    for idx, col in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = 22 + idx * 20
        draw.ellipse([cx - 6, 13, cx + 6, 25], fill=col)
    draw.text((W // 2 - 175, 11), "tripgraph-engine — live Render API demo", fill=MUTED, font=font)

    visible = lines[-34:]
    y = TOP_Y
    for text, color in visible:
        if text.startswith("$ "):
            draw.text((PAD_X, y), "$ ", fill=GREEN, font=font)
            draw.text((PAD_X + 18, y), text[2:], fill=WHITE, font=font)
            if show_cursor and text is visible[-1][0]:
                cx = PAD_X + 18 + int(draw.textlength(text[2:], font=font)) + 2
                draw.rectangle([cx, y + 2, cx + 9, y + 16], fill=GREEN)
        else:
            draw.text((PAD_X, y), text[:108], fill=color, font=font)
        y += LINE_H

    return img


def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "https://tripgraph-engine.onrender.com"
    cmd_display = f"python3 scripts/live_demo.py {target}"
    print(f"Capturing live output from: {cmd_display}")

    proc = subprocess.run(
        [sys.executable, "scripts/live_demo.py", target],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    raw_lines = proc.stdout.strip().splitlines()
    # Keep JSON blocks compact so all 5 steps fit crisply in one viewport
    compact_lines: list[str] = []
    for ln in raw_lines:
        if ln.strip() in ("{", "}", "[", "]", "},"):
            continue
        compact_lines.append(ln)

    font_path = font_manager.findfont("DejaVu Sans Mono")
    font = ImageFont.truetype(font_path, 14)

    frames: list[Image.Image] = []
    durations: list[int] = []

    # 1. Typing animation for the command
    for end in range(4, len(cmd_display) + 1, 5):
        frames.append(_render_frame([(f"$ {cmd_display[:end]}", WHITE)], font, show_cursor=True))
        durations.append(75)
    frames.append(_render_frame([(f"$ {cmd_display}", WHITE)], font, show_cursor=True))
    durations.append(450)

    # 2. Stream real output section by section
    shown: list[tuple[str, tuple[int, int, int]]] = [(f"$ {cmd_display}", WHITE), ("", WHITE)]
    for ln in compact_lines:
        shown.append((ln, _line_color(ln)))
        is_header = ln.strip().startswith("===")
        is_sse = ln.strip().startswith("data:")
        is_end = "Final committed" in ln
        if is_header or is_sse or is_end or ln.strip().startswith("HTTP "):
            frames.append(_render_frame(list(shown), font))
            if is_end:
                durations.append(3500)
            elif is_sse:
                durations.append(380)
            elif is_header:
                durations.append(550)
            else:
                durations.append(300)

    # Ensure final frame holds cleanly
    frames.append(_render_frame(list(shown), font))
    durations.append(3500)

    p_frames = [f.convert("P", palette=Image.ADAPTIVE, colors=64) for f in frames]
    p_frames[0].save(
        OUT_GIF,
        save_all=True,
        append_images=p_frames[1:],
        duration=durations,
        loop=0,
        optimize=True,
    )
    print(f"Rendered {OUT_GIF} ({OUT_GIF.stat().st_size / 1024:.1f} KB, {len(frames)} frames).")


if __name__ == "__main__":
    main()
