"""Generate dark-mode SVG performance charts directly from measured JSONL/JSON data."""

from __future__ import annotations

import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np

from eval.stats import scenario_clustered_quantile_ci

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ROOT / "assets"
ASSETS.mkdir(exist_ok=True)

# High-contrast dark palette
BG = "#0D1117"
PANEL = "#161B22"
GRID = "#30363D"
TEXT = "#E6EDF3"
MUTED = "#8B949E"
GREEN = "#00FF88"
YELLOW = "#FACC15"
BLUE = "#38BDF8"
CORAL = "#F87171"


def _style_ax(ax) -> None:
    ax.set_facecolor(PANEL)
    for spine in ("top", "right"):
        ax.spines[spine].set_visible(False)
    for spine in ("left", "bottom"):
        ax.spines[spine].set_color(GRID)
    ax.tick_params(colors=TEXT, labelsize=9.5)
    ax.yaxis.grid(True, color=GRID, linestyle="--", linewidth=0.7, alpha=0.8)
    ax.set_axisbelow(True)


def generate_accuracy_chart(rows: list[dict]) -> None:
    regimes = [
        ("R1_SHIFTABLE", "R1: Shiftable\n(n=24)"),
        ("R2A_STRUCTURED_CLOSURE", "R2A: Closure\n(n=18)"),
        ("R2B_FREETEXT_INTENT", "R2B: Intent*\n(n=50)"),
        ("R3_HARD_LOCK_UNSAT", "R3: Hard UNSAT\n(n=24)"),
        ("OVERALL", "Overall\n(n=116)"),
    ]
    arms = [
        ("S2_greedy", "S2_greedy (Matched Greedy)", CORAL),
        ("S2_cpsat", "S2_cpsat (Temporal CP-SAT)", YELLOW),
        ("S2_taxonomy", "S2_taxonomy (CP-SAT + Ontology)", GREEN),
    ]

    fig, ax = plt.subplots(figsize=(10.5, 4.8), facecolor=BG)
    _style_ax(ax)

    x = np.arange(len(regimes))
    width = 0.25

    for idx, (arm_key, label, color) in enumerate(arms):
        arm_rows = [r for r in rows if r["arm"] == arm_key]
        vals = []
        for r_key, _ in regimes:
            sub = arm_rows if r_key == "OVERALL" else [r for r in arm_rows if r["regime"] == r_key]
            vals.append(100.0 * sum(r["outcome_correct"] for r in sub) / max(1, len(sub)))

        bars = ax.bar(x + (idx - 1) * width, vals, width, label=label, color=color, edgecolor=BG, linewidth=0.8)
        for b, v in zip(bars, vals):
            ax.text(
                b.get_x() + b.get_width() / 2,
                v + 2.0,
                f"{v:.1f}%" if v not in (0.0, 100.0) else f"{int(v)}%",
                ha="center",
                va="bottom",
                color=TEXT,
                fontsize=8,
                fontweight="bold" if arm_key == "S2_taxonomy" else "normal",
            )

    ax.set_xticks(x)
    ax.set_xticklabels([lbl for _, lbl in regimes], color=TEXT, fontsize=9.5)
    ax.set_ylabel("Accuracy / Honest Refusal Rate (%)", color=TEXT, fontsize=10, fontweight="bold")
    ax.set_ylim(0, 118)
    ax.set_title(
        "TripGraph v7 — Accuracy Across 116 Stratified Held-Out Scenarios",
        color=TEXT,
        fontsize=12.5,
        fontweight="bold",
        pad=14,
    )
    ax.legend(facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT, loc="lower left", fontsize=9)
    fig.text(
        0.015,
        0.01,
        "Exact McNemar (Holm-adjusted): S2_taxonomy vs S2_greedy = +6.9 pp (p = 0.0078) | *R2B measures physical feasibility.",
        color=MUTED,
        fontsize=8.2,
    )
    plt.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(ASSETS / "accuracy_by_regime.svg", format="svg", facecolor=BG)
    plt.close(fig)


def generate_latency_chart(rows: list[dict]) -> None:
    arms = [
        ("S2_greedy", "S2_greedy\n(93.1% acc)", CORAL),
        ("S2_cpsat", "S2_cpsat\n(41.4% acc)", YELLOW),
        ("S2_taxonomy", "S2_taxonomy\n(100.0% acc)", GREEN),
    ]
    quantiles = [("p50", 0.50, 0.95), ("p95", 0.95, 0.72), ("p99", 0.99, 0.48)]

    fig, ax = plt.subplots(figsize=(9.5, 4.6), facecolor=BG)
    _style_ax(ax)

    x = np.arange(len(arms))
    width = 0.24

    for q_idx, (q_name, q_val, alpha) in enumerate(quantiles):
        points, err_lo, err_hi = [], [], []
        colors = []
        for arm_key, _, color in arms:
            clusters = [r["repeat_latencies_ms"] for r in rows if r["arm"] == arm_key]
            ci = scenario_clustered_quantile_ci(clusters, q_val, n_boot=400, seed=42)
            points.append(ci.point)
            err_lo.append(max(0.0, ci.point - ci.ci_low))
            err_hi.append(max(0.0, ci.ci_high - ci.point))
            colors.append(color)

        pos = x + (q_idx - 1) * width
        bars = ax.bar(
            pos,
            points,
            width,
            color=colors,
            alpha=alpha,
            edgecolor=BG,
            yerr=[err_lo, err_hi],
            capsize=4,
            error_kw={"ecolor": TEXT, "elinewidth": 1.1},
        )
        for b, pt, hi in zip(bars, points, err_hi):
            ax.text(
                b.get_x() + b.get_width() / 2,
                pt + hi + 0.8,
                f"{q_name}\n{pt:.1f}ms",
                ha="center",
                va="bottom",
                color=TEXT,
                fontsize=7.8,
            )

    ax.set_xticks(x)
    ax.set_xticklabels([lbl for _, lbl, _ in arms], color=TEXT, fontsize=10, fontweight="bold")
    ax.set_ylabel("Wall Latency (ms)", color=TEXT, fontsize=10, fontweight="bold")
    max_y = max(r["repeat_latencies_ms"][-1] for r in rows)
    ax.set_ylim(0, max(35, max_y * 1.15))
    ax.set_title(
        "Latency Distribution (p50 / p95 / p99) — 116 Scenarios × 10 Repeats",
        color=TEXT,
        fontsize=12.5,
        fontweight="bold",
        pad=14,
    )
    fig.text(
        0.015,
        0.01,
        "Error bars = two-stage scenario-clustered bootstrap 95% CIs (zero pseudo-replication). Bar opacity: solid=p50, med=p95, light=p99.",
        color=MUTED,
        fontsize=8.2,
    )
    plt.tight_layout(rect=(0, 0.04, 1, 1))
    fig.savefig(ASSETS / "latency_comparison.svg", format="svg", facecolor=BG)
    plt.close(fig)


def generate_ablation_chart(ablation: list[dict]) -> None:
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11.5, 4.6), facecolor=BG)
    _style_ax(ax1)
    _style_ax(ax2)

    days = [d["num_days"] for d in ablation]
    labels = [f"{d['num_days']}d\n({d['total_activities']} acts)" for d in ablation]
    inc_ms = [d["incremental"]["total_ms"] for d in ablation]
    full_ms = [d["full_replan"]["total_ms"] for d in ablation]

    x = np.arange(len(days))
    ax1.plot(x, full_ms, marker="o", linewidth=2.4, color=CORAL, label="Naive Full-Trip Replan")
    ax1.plot(x, inc_ms, marker="s", linewidth=2.4, color=GREEN, label="TripGraph Incremental (Dirty Closure)")
    ax1.fill_between(x, inc_ms, full_ms, color=GREEN, alpha=0.08)

    for i, (im, fm, d) in enumerate(zip(inc_ms, full_ms, ablation)):
        ax1.text(i, fm + max(full_ms) * 0.04, f"{fm:.1f}ms", ha="center", color=CORAL, fontsize=8.5, fontweight="bold")
        ax1.text(i, max(1.0, im - max(full_ms) * 0.08), f"{im:.1f}ms ({d['speedup_x']}x)", ha="center", color=GREEN, fontsize=8.5, fontweight="bold")

    ax1.set_xticks(x)
    ax1.set_xticklabels(labels, color=TEXT, fontsize=9)
    ax1.set_ylabel("Median Total Edit Latency (ms)", color=TEXT, fontsize=9.5, fontweight="bold")
    ax1.set_ylim(0, max(full_ms) * 1.25)
    ax1.set_title("Scaling: Incremental Dirty Closure vs Full Replan", color=TEXT, fontsize=11, fontweight="bold", pad=10)
    ax1.legend(facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT, loc="upper left", fontsize=8.8)

    # Right panel: Stage latency breakdown on largest trip (10 days / 40 activities)
    largest = ablation[-1]
    stages = ["Dirty Closure\n& Freeze", "IR Compile", "CP-SAT\nSolve", "K3 Verify"]
    inc_stages = [
        largest["incremental"]["closure_ms"],
        largest["incremental"]["compile_ms"],
        largest["incremental"]["solve_ms"],
        largest["incremental"]["verify_ms"],
    ]
    full_stages = [
        largest["full_replan"]["closure_ms"],
        largest["full_replan"]["compile_ms"],
        largest["full_replan"]["solve_ms"],
        largest["full_replan"]["verify_ms"],
    ]
    sx = np.arange(len(stages))
    w = 0.34
    ax2.bar(sx - w / 2, full_stages, w, color=CORAL, label="Full Replan (40 acts)")
    ax2.bar(sx + w / 2, inc_stages, w, color=GREEN, label="Incremental (4 dirty acts)")
    for i, (fs, ins) in enumerate(zip(full_stages, inc_stages)):
        ax2.text(i - w / 2, fs + max(full_stages) * 0.03, f"{fs:.1f}", ha="center", color=CORAL, fontsize=8)
        ax2.text(i + w / 2, ins + max(full_stages) * 0.03, f"{ins:.1f}", ha="center", color=GREEN, fontsize=8)

    ax2.set_xticks(sx)
    ax2.set_xticklabels(stages, color=TEXT, fontsize=8.8)
    ax2.set_ylabel("Median Stage Latency (ms)", color=TEXT, fontsize=9.5, fontweight="bold")
    ax2.set_ylim(0, max(full_stages) * 1.25)
    ax2.set_title("Per-Stage Bottleneck Profile (10-Day / 40-Act Trip)", color=TEXT, fontsize=11, fontweight="bold", pad=10)
    ax2.legend(facecolor=PANEL, edgecolor=GRID, labelcolor=TEXT, loc="upper left", fontsize=8.8)

    plt.tight_layout()
    fig.savefig(ASSETS / "incremental_ablation.svg", format="svg", facecolor=BG)
    plt.close(fig)


def main() -> None:
    jsonl_path = ROOT / "eval" / "per_scenario_metrics.jsonl"
    rows = [json.loads(line) for line in jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    ablation = json.loads((ROOT / "eval" / "ablation_metrics.json").read_text(encoding="utf-8"))

    generate_accuracy_chart(rows)
    generate_latency_chart(rows)
    generate_ablation_chart(ablation)
    print("Generated 3 dark-mode SVG charts in assets/:")
    for name in ("accuracy_by_regime.svg", "latency_comparison.svg", "incremental_ablation.svg"):
        size_kb = (ASSETS / name).stat().st_size / 1024.0
        print(f"  assets/{name} ({size_kb:.1f} KB)")


if __name__ == "__main__":
    main()
