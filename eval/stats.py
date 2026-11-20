"""Exact statistical inference & multi-rater semantic evaluation engine (TripGraph v7).

Implements:
- Exact McNemar's test + Holm-Bonferroni correction
- Two-stage scenario-clustered bootstrap 95% CIs (zero pseudo-replication)
- Exact two-sided binomial Sign Test & exact realized power via scipy.stats.binom
- Exact Clopper-Pearson 95% confidence intervals
- 3x3 inter-rater adjudication (S3/TIE conservatively -> TIE in 2-rater mode)
- Linear-weighted Cohen's kappa with bootstrap 95% CI
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import random
from typing import Literal, Sequence

from scipy import stats

Verdict3 = Literal["S3", "S2_TAX", "TIE"]
_ORDINAL_MAP: dict[Verdict3, int] = {"S2_TAX": -1, "TIE": 0, "S3": 1}


@dataclass(frozen=True)
class McNemarResult:
    n_pairs: int
    n01_b_better: int  # Arm A failed, Arm B succeeded
    n10_a_better: int  # Arm A succeeded, Arm B failed
    p_value_exact: float
    net_delta_pp: float


def exact_mcnemar(arm_a_ok: Sequence[bool], arm_b_ok: Sequence[bool]) -> McNemarResult:
    """Exact two-sided McNemar test on paired binary outcomes."""
    assert len(arm_a_ok) == len(arm_b_ok)
    n = len(arm_a_ok)
    n01 = sum((not a) and b for a, b in zip(arm_a_ok, arm_b_ok))
    n10 = sum(a and (not b) for a, b in zip(arm_a_ok, arm_b_ok))
    disc = n01 + n10
    if disc == 0:
        pval = 1.0
    else:
        pval = float(stats.binomtest(n01, disc, p=0.5, alternative="two-sided").pvalue)
    delta_pp = ((sum(arm_b_ok) - sum(arm_a_ok)) / max(1, n)) * 100.0
    return McNemarResult(
        n_pairs=n,
        n01_b_better=n01,
        n10_a_better=n10,
        p_value_exact=pval,
        net_delta_pp=delta_pp,
    )


def holm_bonferroni(p_values: Sequence[float]) -> list[float]:
    """Return Holm-Bonferroni adjusted p-values in original input order."""
    m = len(p_values)
    indexed = sorted(enumerate(p_values), key=lambda pair: pair[1])
    adjusted = [0.0] * m
    running_max = 0.0
    for rank, (orig_idx, p) in enumerate(indexed):
        adj = min(1.0, p * (m - rank))
        running_max = max(running_max, adj)
        adjusted[orig_idx] = running_max
    return adjusted


def exact_binomial_sign_test_power(
    n_non_tie: int,
    *,
    p_alt: float = 0.75,
    alpha: float = 0.05,
) -> float:
    """Exact power of a two-sided exact binomial Sign Test (H0: p=0.50 vs H1: p=p_alt).

    Zero normal approximations: directly sums binomial rejection region probabilities.
    """
    if n_non_tie <= 0:
        return 0.0
    power = 0.0
    for k in range(n_non_tie + 1):
        pval = float(stats.binomtest(k, n_non_tie, p=0.5, alternative="two-sided").pvalue)
        if pval <= alpha:
            power += float(stats.binom.pmf(k, n_non_tie, p_alt))
    return power


def clopper_pearson_ci(k: int, n: int, *, alpha: float = 0.05) -> tuple[float, float]:
    """Exact Clopper-Pearson 95% binomial confidence interval."""
    if n <= 0:
        return (0.0, 1.0)
    lo = 0.0 if k == 0 else float(stats.beta.ppf(alpha / 2.0, k, n - k + 1))
    hi = 1.0 if k == n else float(stats.beta.ppf(1.0 - alpha / 2.0, k + 1, n - k))
    return (lo, hi)


def _percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_vals[lo]
    frac = pos - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


@dataclass(frozen=True)
class ClusteredQuantileCI:
    point: float
    ci_low: float
    ci_high: float


def scenario_clustered_quantile_ci(
    scenario_repeats: Sequence[Sequence[float]],
    quantile: float,
    *,
    n_boot: int = 1000,
    seed: int = 42,
) -> ClusteredQuantileCI:
    """Two-stage cluster bootstrap 95% CI: resamples SCENARIOS with replacement,
    preserving all within-scenario repeat correlations (zero pseudo-replication)."""
    n_scen = len(scenario_repeats)
    if n_scen == 0:
        return ClusteredQuantileCI(0.0, 0.0, 0.0)

    # Compute point estimate over pooled scenario means (or full clustered vector)
    all_vals = sorted(x for cluster in scenario_repeats for x in cluster)
    point = _percentile(all_vals, quantile)

    rng = random.Random(seed)
    boot_stats: list[float] = []
    for _ in range(n_boot):
        sampled = [
            x
            for _i in range(n_scen)
            for x in scenario_repeats[rng.randrange(n_scen)]
        ]
        sampled.sort()
        boot_stats.append(_percentile(sampled, quantile))
    boot_stats.sort()
    return ClusteredQuantileCI(
        point=point,
        ci_low=_percentile(boot_stats, 0.025),
        ci_high=_percentile(boot_stats, 0.975),
    )


def adjudicate_pair(
    r1: Verdict3,
    r2: Verdict3,
    r3: Verdict3 | None = None,
) -> Verdict3:
    """Complete 3x3 v6/v7 adjudication rule.

    - Unanimous (r1 == r2) -> accepted directly.
    - Any disagreement (including soft S3/TIE or direct reversal S3/S2_TAX):
        * With r3: strict majority (2 of 3) wins; 1-1-1 split -> TIE.
        * Without r3 (2-rater mode): conservatively resolved as TIE.
    """
    if r1 == r2:
        return r1
    if r3 is None:
        return "TIE"
    votes = [r1, r2, r3]
    for cand in ("S3", "S2_TAX", "TIE"):
        if votes.count(cand) >= 2:
            return cand  # type: ignore[return-value]
    return "TIE"


def linear_weighted_cohens_kappa(
    r1_list: Sequence[Verdict3], r2_list: Sequence[Verdict3]
) -> float:
    """Linear-weighted Cohen's kappa over ordinal scale S2_TAX (-1), TIE (0), S3 (+1)."""
    assert len(r1_list) == len(r2_list)
    n = len(r1_list)
    if n == 0:
        return 0.0
    cats: tuple[Verdict3, ...] = ("S2_TAX", "TIE", "S3")
    k = len(cats)
    idx = {c: i for i, c in enumerate(cats)}

    obs = [[0] * k for _ in range(k)]
    for a, b in zip(r1_list, r2_list):
        obs[idx[a]][idx[b]] += 1

    row_tot = [sum(obs[i]) for i in range(k)]
    col_tot = [sum(obs[i][j] for i in range(k)) for j in range(k)]

    obs_dis = 0.0
    exp_dis = 0.0
    for i in range(k):
        for j in range(k):
            w = abs(i - j) / float(k - 1)
            obs_dis += w * (obs[i][j] / n)
            exp_dis += w * ((row_tot[i] * col_tot[j]) / (n * n))

    if exp_dis == 0.0:
        return 1.0 if obs_dis == 0.0 else 0.0
    return 1.0 - (obs_dis / exp_dis)


@dataclass(frozen=True)
class SemanticEvalSummary:
    divergent_pairs: int
    s3_wins: int
    s2_tax_wins: int
    ties: int
    n_non_tie: int
    kappa_linear_weighted: float
    kappa_ci_low: float
    kappa_ci_high: float
    sign_test_p_value: float
    exact_realized_power_75_25: float
    conditional_s3_rate: float
    conditional_ci: tuple[float, float]
    unconditional_s3_rate: float
    unconditional_ci: tuple[float, float]
    claim_gate_passed: bool
    claim_verdict: str


def summarize_blind_human_eval(
    r1_list: Sequence[Verdict3],
    r2_list: Sequence[Verdict3],
    r3_list: Sequence[Verdict3 | None] | None = None,
) -> SemanticEvalSummary:
    """Compute complete v7 semantic-quality summary with hard power & kappa claim gates."""
    n = len(r1_list)
    assert len(r2_list) == n
    r3_seq = r3_list if r3_list is not None else [None] * n
    adjudicated = [adjudicate_pair(a, b, c) for a, b, c in zip(r1_list, r2_list, r3_seq)]

    s3_w = adjudicated.count("S3")
    s2_w = adjudicated.count("S2_TAX")
    ties = adjudicated.count("TIE")
    n_non_tie = s3_w + s2_w

    kappa = linear_weighted_cohens_kappa(r1_list, r2_list)
    # Bootstrap 95% CI for weighted kappa
    rng = random.Random(42)
    k_boots: list[float] = []
    if n > 0:
        for _ in range(500):
            idxs = [rng.randrange(n) for _ in range(n)]
            k_boots.append(
                linear_weighted_cohens_kappa([r1_list[i] for i in idxs], [r2_list[i] for i in idxs])
            )
        k_boots.sort()
        k_lo, k_hi = _percentile(k_boots, 0.025), _percentile(k_boots, 0.975)
    else:
        k_lo, k_hi = 0.0, 0.0

    p_sign = (
        float(stats.binomtest(s3_w, n_non_tie, p=0.5, alternative="two-sided").pvalue)
        if n_non_tie > 0
        else 1.0
    )
    power = exact_binomial_sign_test_power(n_non_tie, p_alt=0.75, alpha=0.05)

    cond_rate = (s3_w / n_non_tie) if n_non_tie > 0 else 0.0
    cond_ci = clopper_pearson_ci(s3_w, n_non_tie)
    uncond_rate = (s3_w / n) if n > 0 else 0.0
    uncond_ci = clopper_pearson_ci(s3_w, n)

    gate_ok = (power >= 0.80) and (kappa >= 0.60) and (p_sign < 0.05) and (s3_w > s2_w)
    if power < 0.80:
        verdict = f"UNDERPOWERED (realized power={power:.1%} < 80%; n_non_tie={n_non_tie})"
    elif kappa < 0.60:
        verdict = f"INCONCLUSIVE_LOW_AGREEMENT (weighted kappa={kappa:.2f} < 0.60)"
    elif p_sign >= 0.05:
        verdict = f"NO_SIGNIFICANT_DIFFERENCE (sign test p={p_sign:.4f})"
    elif s3_w > s2_w:
        verdict = "S3_STATISTICALLY_SUPERIOR"
    else:
        verdict = "S2_TAXONOMY_STATISTICALLY_SUPERIOR"

    return SemanticEvalSummary(
        divergent_pairs=n,
        s3_wins=s3_w,
        s2_tax_wins=s2_w,
        ties=ties,
        n_non_tie=n_non_tie,
        kappa_linear_weighted=kappa,
        kappa_ci_low=k_lo,
        kappa_ci_high=k_hi,
        sign_test_p_value=p_sign,
        exact_realized_power_75_25=power,
        conditional_s3_rate=cond_rate,
        conditional_ci=cond_ci,
        unconditional_s3_rate=uncond_rate,
        unconditional_ci=uncond_ci,
        claim_gate_passed=gate_ok,
        claim_verdict=verdict,
    )
