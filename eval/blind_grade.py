"""Zero-leakage blind A/B human grading utility (TripGraph v7).

- Strips arm names, latencies, and solver logs before showing pairs to raters.
- Randomly position-swaps Plan A vs Plan B per scenario & rater (deterministic seed).
- Scores completed rater JSON files via eval.stats.summarize_blind_human_eval(),
  enforcing the >=80% exact power gate and weighted Cohen's kappa >= 0.60 gate.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import random
from typing import Any

from eval.stats import Verdict3, summarize_blind_human_eval


@dataclass(frozen=True)
class BlindedPair:
    scenario_id: str
    user_intent_notes: str
    plan_a_summary: dict[str, Any]
    plan_b_summary: dict[str, Any]
    a_is_s3: bool  # Stored ONLY in secret key file, never shown to human rater!


def build_blinded_packet(
    divergent_pairs: list[dict[str, Any]],
    *,
    rater_id: str,
    seed: int = 2026,
) -> tuple[list[dict[str, Any]], dict[str, bool]]:
    """Produce (rater_visible_packet, secret_key_map) with randomized A/B order."""
    rng = random.Random(f"{seed}:{rater_id}")
    visible: list[dict[str, Any]] = []
    key_map: dict[str, bool] = {}

    for item in divergent_pairs:
        sid = item["scenario_id"]
        a_is_s3 = rng.choice([True, False])
        key_map[sid] = a_is_s3
        plan_a = item["s3_plan"] if a_is_s3 else item["s2_tax_plan"]
        plan_b = item["s2_tax_plan"] if a_is_s3 else item["s3_plan"]
        visible.append(
            {
                "scenario_id": sid,
                "user_intent_notes": item["user_intent_notes"],
                "plan_a": plan_a,
                "plan_b": plan_b,
                "verdict": None,  # Rater fills in: "A_BETTER" | "B_BETTER" | "TIE"
            }
        )
    return visible, key_map


def decode_rater_verdicts(
    graded_packet: list[dict[str, Any]],
    secret_key_map: dict[str, bool],
) -> list[Verdict3]:
    """Map anonymized A_BETTER/B_BETTER/TIE back to S3/S2_TAX/TIE."""
    out: list[Verdict3] = []
    for row in graded_packet:
        sid = row["scenario_id"]
        v = row["verdict"]
        a_is_s3 = secret_key_map[sid]
        if v == "TIE":
            out.append("TIE")
        elif v == "A_BETTER":
            out.append("S3" if a_is_s3 else "S2_TAX")
        elif v == "B_BETTER":
            out.append("S2_TAX" if a_is_s3 else "S3")
        else:
            raise ValueError(f"Invalid verdict {v!r} for {sid}")
    return out


def generate_semantic_report(
    r1_verdicts: list[Verdict3],
    r2_verdicts: list[Verdict3],
    r3_verdicts: list[Verdict3 | None] | None = None,
    output_path: Path | None = None,
) -> str:
    s = summarize_blind_human_eval(r1_verdicts, r2_verdicts, r3_verdicts)
    md = "\n".join(
        [
            "# Blind Multi-Rater Semantic Evaluation Report (`S3` vs `S2_taxonomy`)",
            "",
            f"- **Divergent Regime-2B Pairs Graded:** {s.divergent_pairs}",
            f"- **Adjudicated Outcomes:** `S3` wins = {s.s3_wins}, `S2_taxonomy` wins = {s.s2_tax_wins}, Ties = {s.ties} (`n_non_tie = {s.n_non_tie}`)",
            f"- **Inter-Rater Agreement (Linear-Weighted Cohen's κ):** `{s.kappa_linear_weighted:.3f}` (95% CI: `[{s.kappa_ci_low:.3f}, {s.kappa_ci_high:.3f}]`)",
            f"- **Exact Two-Sided Sign Test p-value:** `{s.sign_test_p_value:.4g}`",
            f"- **Exact Realized Power (75% vs 25%, α=0.05):** `{s.exact_realized_power_75_25:.1%}`",
            f"- **Conditional S3 Win Rate (`S3 / n_non_tie`):** `{s.conditional_s3_rate:.1%}` (95% Clopper-Pearson CI: `[{s.conditional_ci[0]:.1%}, {s.conditional_ci[1]:.1%}]`)",
            f"- **Conservative Unconditional S3 Win Rate (`S3 / all divergent`):** `{s.unconditional_s3_rate:.1%}` (95% Clopper-Pearson CI: `[{s.unconditional_ci[0]:.1%}, {s.unconditional_ci[1]:.1%}]`)",
            f"- **Claim Gate Verdict:** **`{s.claim_verdict}`** (`gate_passed={s.claim_gate_passed}`)",
            "",
        ]
    )
    if output_path is not None:
        output_path.write_text(md, encoding="utf-8")
    return md
