"""Milestone 7 verification tests (TripGraph v7):
1. Stratified counts: 18 pilot + 116 held-out (24 R1, 18 R2A, 50 R2B, 24 R3).
2. Exact binomial power calculation matches verified table (n=23->65.4%, n=30->80.3%, n>=35-> >=80%).
3. 3x3 adjudication rules (S3/TIE -> TIE in 2-rater mode; majority in 3-rater mode).
4. Blind packet strips arm labels & decodes back losslessly.
5. Full 116-scenario deterministic benchmark executes cleanly against FROZEN_CONTROLS.lock
   and proves S2_taxonomy strictly beats S2_greedy with Holm-Bonferroni p < 0.05.
"""

from __future__ import annotations

import pytest

from eval.blind_grade import build_blinded_packet, decode_rater_verdicts
from eval.run_benchmark import run_deterministic_benchmark, verify_frozen_controls
from eval.scenarios import build_held_out_scenarios, build_pilot_scenarios
from eval.stats import (
    adjudicate_pair,
    exact_binomial_sign_test_power,
    holm_bonferroni,
    summarize_blind_human_eval,
)


def test_scenario_stratification_and_frozen_lock() -> None:
    pilot = build_pilot_scenarios()
    held = build_held_out_scenarios()

    assert len(pilot) == 18
    assert len(held) == 116
    assert sum(s.regime == "R1_SHIFTABLE" for s in held) == 24
    assert sum(s.regime == "R2A_STRUCTURED_CLOSURE" for s in held) == 18
    assert sum(s.regime == "R2B_FREETEXT_INTENT" for s in held) == 50
    assert sum(s.regime == "R3_HARD_LOCK_UNSAT" for s in held) == 24
    assert len(verify_frozen_controls()) == 64


def test_exact_binomial_power_matches_v6_review_table() -> None:
    p23 = exact_binomial_sign_test_power(23, p_alt=0.75, alpha=0.05)
    p30 = exact_binomial_sign_test_power(30, p_alt=0.75, alpha=0.05)
    p35 = exact_binomial_sign_test_power(35, p_alt=0.75, alpha=0.05)

    assert abs(p23 - 0.654) < 0.005
    assert abs(p30 - 0.803) < 0.005
    assert p35 >= 0.85
    # Verify monotonic >= 80% floor for every n in [35..50]
    for n in range(35, 51):
        assert exact_binomial_sign_test_power(n, p_alt=0.75, alpha=0.05) >= 0.80


def test_3x3_adjudication_and_claim_gate() -> None:
    # 2-rater mode: soft S3/TIE and direct S3/S2_TAX both resolve conservatively to TIE
    assert adjudicate_pair("S3", "S3") == "S3"
    assert adjudicate_pair("S3", "TIE") == "TIE"
    assert adjudicate_pair("TIE", "S2_TAX") == "TIE"
    assert adjudicate_pair("S3", "S2_TAX") == "TIE"

    # 3-rater mode: strict majority wins; 1-1-1 -> TIE
    assert adjudicate_pair("S3", "TIE", "S3") == "S3"
    assert adjudicate_pair("S3", "TIE", "TIE") == "TIE"
    assert adjudicate_pair("S3", "TIE", "S2_TAX") == "TIE"

    # Underpowered sample (n=23) MUST fail claim gate even with 18/23 wins
    r1 = ["S3"] * 18 + ["S2_TAX"] * 5
    res_small = summarize_blind_human_eval(r1, r1)
    assert res_small.claim_gate_passed is False
    assert "UNDERPOWERED" in res_small.claim_verdict

    # Powered high-agreement sample (n=36, 28 S3 wins) passes claim gate
    r_big = ["S3"] * 28 + ["S2_TAX"] * 8
    res_big = summarize_blind_human_eval(r_big, r_big)
    assert res_big.claim_gate_passed is True
    assert res_big.claim_verdict == "S3_STATISTICALLY_SUPERIOR"

    # Holm-Bonferroni check
    adj = holm_bonferroni([0.01, 0.04, 0.03])
    assert adj == pytest.approx([0.03, 0.06, 0.06])


def test_blind_grading_packet_strips_arm_labels_and_roundtrips() -> None:
    pairs = [
        {"scenario_id": f"s{i}", "user_intent_notes": "quiet", "s3_plan": {"p": "quiet_craft"}, "s2_tax_plan": {"p": "crowded"}}
        for i in range(6)
    ]
    visible, secret = build_blinded_packet(pairs, rater_id="R1")
    for row in visible:
        assert "s3_plan" not in row and "s2_tax_plan" not in row
        # Rater always prefers the quiet_craft plan blindly
        row["verdict"] = "A_BETTER" if row["plan_a"]["p"] == "quiet_craft" else "B_BETTER"

    decoded = decode_rater_verdicts(visible, secret)
    assert decoded == ["S3"] * 6


def test_full_116_scenario_deterministic_benchmark_runs_and_beats_greedy() -> None:
    res = run_deterministic_benchmark(split="held_out", repeats=2, write_outputs=False)
    tax = res["arms"]["S2_taxonomy"]
    greedy = res["arms"]["S2_greedy"]
    cpsat = res["arms"]["S2_cpsat"]

    # S2_taxonomy achieves 100% accuracy and 0.0 blast radius across all 116 held-out cases
    assert tax["overall_correct_rate"] == 1.0
    assert tax["mean_semantic_blast_radius"] == 0.0
    assert tax["lock_guard_pass_rate"] == 1.0

    # S2_greedy gets trapped on R1 locked-window cases -> S2_taxonomy wins statistically!
    assert greedy["by_regime"]["R1_SHIFTABLE"] < 1.0
    assert res["mcnemar"]["S2_taxonomy_vs_S2_greedy"]["p_holm"] < 0.05

    # S2_cpsat solves R1 & R3 cleanly (100%) and hits UNSAT on R2A/R2B closures
    assert cpsat["by_regime"]["R1_SHIFTABLE"] == 1.0
    assert cpsat["by_regime"]["R3_HARD_LOCK_UNSAT"] == 1.0
    assert cpsat["by_regime"]["R2A_STRUCTURED_CLOSURE"] == 0.0
