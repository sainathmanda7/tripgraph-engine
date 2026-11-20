# TripGraph v7 — Automated Benchmark Results

- **Split:** `held_out` (116 paired scenarios × 10 timing repeats)
- **Frozen Controls SHA-256:** `8eb3318603656896cb62b3c5e3624bbcff09fa6f9be7b5d50d3825b2417968f8`
- **Latency CIs:** Two-stage scenario-clustered bootstrap 95% CIs (zero pseudo-replication).

## Deterministic Local Arms (0 LLM Tokens)

| Arm | Overall Accuracy | R1 Shiftable | R2A Closure | R2B Intent | R3 Hard-Lock Refusal | Blast Radius (Sem/Tmp) | p50 ms [95% CI] | p95 ms [95% CI] |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **`S2_greedy`** | **93.1%** | 66.7% | 100.0% | 100.0% | 100.0% | 0.0% / 0.0% | 1.8 [1.5, 1.9] | 3.5 [2.9, 4.3] |
| **`S2_cpsat`** | **41.4%** | 100.0% | 0.0% | 0.0% | 100.0% | 0.0% / 0.0% | 9.0 [8.7, 9.4] | 15.4 [13.8, 16.3] |
| **`S2_taxonomy`** | **100.0%** | 100.0% | 100.0% | 100.0% | 100.0% | 0.0% / 0.0% | 13.4 [11.9, 14.6] | 22.9 [20.3, 27.4] |

## Paired Exact McNemar Tests (Holm-Bonferroni Adjusted)

- **`S2_taxonomy` vs `S2_greedy` (Exact CP-SAT vs Greedy, matched controls):** `+6.9 pp` (`n01=8, n10=0`), exact `p = 0.007812`, Holm-adjusted `p = 0.007812`.
- **`S2_taxonomy` vs `S2_cpsat` (Taxonomy Domain Expansion vs Temporal-Only CP-SAT):** `+58.6 pp` (`n01=68, n10=0`), exact `p = 6.776e-21`, Holm-adjusted `p = 1.355e-20`.

> **Scope & Epistemic Note:** Accuracy in the table above measures **hard-constraint feasibility and honest `UNSAT` refusal** across 0-LLM deterministic arms. On `R2B Intent`, `S2_taxonomy` achieves 100.0% physical feasibility via frozen category ontology expansion, but does not parse free-text sensory/vibe notes—semantic preference (`S3` vs `S2_taxonomy`) is evaluated separately via `OpenAICompatibleLLMClient` + 2-rater blind grading in `eval/blind_grade.py` under the >=80% exact power and weighted κ >= 0.60 gates.
