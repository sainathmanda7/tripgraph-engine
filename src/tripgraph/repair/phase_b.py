"""Tool-Mediated Phase B Neuro-Symbolic Repair (TripGraph v7).

Exact Synchronous Execution DAG (Max 2 LLM completions, 1 batched provider fetch):
  [UNSAT Core]
      -> (1) LLM SearchIntent (cap 1400ms)
      -> (2) BudgetedProviderProxy.search_candidates (cap 450ms, max 20 records)
      -> (3) LLM SemanticRank (cap 1400ms, strict whitelist <= 5 grounded IDs)
      -> (4) CP-SAT Proof Solve (cap 650ms)
      -> (5) Optional 0-LLM Fallback CP-SAT over remaining fetched pool (cap 450ms)
      -> If still INFEASIBLE -> Escalate 2nd broadening cycle to 30s Async Worker (202)

Consent-Gated Outage Safety:
- allow_auto_drop_optional=False (DEFAULT): LLM timeout/503 NEVER deletes user
  activities; returns RETRYABLE_UPSTREAM_LLM_UNAVAILABLE + optional uncommitted preview.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date
import json
from typing import Awaitable, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from tripgraph.grounding.provider import (
    BudgetedProviderProxy,
    CandidateSearchIntent,
    GroundedRecord,
)
from tripgraph.schema.models import (
    ActivityNode,
    Place,
    TripGraph,
    VerificationPolicy,
)
from tripgraph.solver.ir import AssumptionGroupSpec, CompiledConstraintIR, compile_dirty_ir
from tripgraph.solver.types import DualSolveResult
from tripgraph.time.tz_registry import PinnedTzRegistry


class LLMOutageError(RuntimeError):
    """Raised on upstream LLM timeout, HTTP 429, or 5xx."""


class SearchIntentLLMOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    target_categories: tuple[str, ...] = Field(min_length=1, max_length=6)
    preferred_tags: tuple[str, ...] = ()
    avoid_tags: tuple[str, ...] = ()
    rationale: str = ""


class SemanticRankLLMOutput(BaseModel):
    model_config = ConfigDict(frozen=True)

    ranked_place_ids: tuple[str, ...] = Field(max_length=5)
    rationale: str = ""


class NegotiationProposal(BaseModel):
    model_config = ConfigDict(frozen=True)

    option_id: str
    description: str
    requires_user_approval: bool = True


@dataclass(frozen=True)
class LLMCallMetrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: float = 0.0


class AsyncIRSolver(Protocol):
    def __call__(
        self,
        ir: CompiledConstraintIR,
        *,
        opt_timeout_sec: float,
        run_mus: bool = False,
    ) -> Awaitable[DualSolveResult]: ...


class PhaseBLLMClient(Protocol):
    def generate_search_intent(
        self,
        *,
        target_act: ActivityNode,
        core_groups: tuple[AssumptionGroupSpec, ...],
        user_intent_notes: str,
        timeout_sec: float = 1.4,
    ) -> tuple[SearchIntentLLMOutput, LLMCallMetrics]: ...

    def rank_candidates(
        self,
        *,
        target_act: ActivityNode,
        candidates: list[Place],
        user_intent_notes: str,
        max_select: int = 5,
        timeout_sec: float = 1.4,
    ) -> tuple[SemanticRankLLMOutput, LLMCallMetrics]: ...


PhaseBStatus = Literal[
    "SOLVED_BY_PHASE_B",
    "SOLVED_BY_POOL_FALLBACK",
    "NEEDS_ASYNC_SECOND_CYCLE",
    "NEEDS_USER_RESOLUTION",
    "RETRYABLE_UPSTREAM_LLM_UNAVAILABLE",
    "DEGRADED_OPTIONAL_DROPPED",
]


@dataclass(frozen=True)
class PhaseBResult:
    status: PhaseBStatus
    solve_result: DualSolveResult | None
    selected_candidates: tuple[str, ...] = ()
    rejected_hallucinated_ids: tuple[str, ...] = ()
    negotiation_proposals: tuple[NegotiationProposal, ...] = ()
    suggested_fallback_preview: DualSolveResult | None = None
    llm_calls_made: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


async def run_phase_b_cycle(
    *,
    graph: TripGraph,
    dirty_days: frozenset[tuple[str, date]],
    target_act: ActivityNode,
    initial_core: tuple[AssumptionGroupSpec, ...],
    proxy: BudgetedProviderProxy,
    place_registry: dict[str, Place],
    now_utc: int,
    tz_registry: PinnedTzRegistry,
    llm_client: PhaseBLLMClient,
    solve_fn: AsyncIRSolver,
    verification_policy: VerificationPolicy = VerificationPolicy.ASSUME_WEEKLY_NON_HOLIDAY,
    public_holidays_by_city: dict[str, frozenset[date]] | None = None,
    allow_auto_drop_optional: bool = False,
) -> PhaseBResult:
    """Execute strictly 1 synchronous Phase B neuro-symbolic expansion & proof cycle."""
    llm_calls = 0
    prompt_toks = 0
    comp_toks = 0

    # --- Step (1): LLM SearchIntent (cap 1400ms) ---
    try:
        intent_out, m1 = await asyncio.to_thread(
            llm_client.generate_search_intent,
            target_act=target_act,
            core_groups=initial_core,
            user_intent_notes=graph.user_intent_notes,
            timeout_sec=1.4,
        )
        llm_calls += 1
        prompt_toks += m1.prompt_tokens
        comp_toks += m1.completion_tokens
    except LLMOutageError:
        return await _handle_llm_outage(
            graph=graph,
            dirty_days=dirty_days,
            place_registry=place_registry,
            proxy=proxy,
            now_utc=now_utc,
            tz_registry=tz_registry,
            solve_fn=solve_fn,
            verification_policy=verification_policy,
            public_holidays_by_city=public_holidays_by_city,
            allow_auto_drop_optional=allow_auto_drop_optional,
            llm_calls=llm_calls,
            prompt_toks=prompt_toks,
            comp_toks=comp_toks,
        )

    # --- Step (2): Grounded Batched Provider Fetch via BudgetedProviderProxy ---
    orig_place = place_registry.get(target_act.place_id)
    excluded = frozenset(a.place_id for a in graph.activities)
    search_intent = CandidateSearchIntent(
        city_id=target_act.city_id,
        target_categories=intent_out.target_categories,
        required_dietary_tags=(
            graph.constraints.required_dietary_tags if target_act.is_meal else ()
        ),
        preferred_tags=intent_out.preferred_tags,
        avoid_tags=intent_out.avoid_tags,
        excluded_place_ids=excluded,
        near_lat=orig_place.lat if orig_place else None,
        near_lon=orig_place.lon if orig_place else None,
    )
    fetched: list[GroundedRecord[Place]] = await asyncio.to_thread(proxy.search_candidates, search_intent)
    if not fetched:
        return PhaseBResult(
            status="NEEDS_USER_RESOLUTION",
            solve_result=None,
            negotiation_proposals=(
                NegotiationProposal(
                    option_id="broaden_or_leave_open",
                    description=(
                        f"No grounded venues found in {target_act.city_id} matching "
                        f"{list(intent_out.target_categories)}. Approve leaving slot open or relaxing constraints."
                    ),
                ),
            ),
            llm_calls_made=llm_calls,
            prompt_tokens=prompt_toks,
            completion_tokens=comp_toks,
        )

    fetched_map: dict[str, Place] = {r.payload.place_id: r.payload for r in fetched}
    merged_registry = {**place_registry, **fetched_map}

    # --- Step (3): LLM SemanticRank (strict grounded whitelist <= 5) ---
    try:
        rank_out, m2 = await asyncio.to_thread(
            llm_client.rank_candidates,
            target_act=target_act,
            candidates=list(fetched_map.values()),
            user_intent_notes=graph.user_intent_notes,
            max_select=proxy.max_candidates_to_cpsat,
            timeout_sec=1.4,
        )
        llm_calls += 1
        prompt_toks += m2.prompt_tokens
        comp_toks += m2.completion_tokens
    except LLMOutageError:
        return await _handle_llm_outage(
            graph=graph,
            dirty_days=dirty_days,
            place_registry=merged_registry,
            proxy=proxy,
            now_utc=now_utc,
            tz_registry=tz_registry,
            solve_fn=solve_fn,
            verification_policy=verification_policy,
            public_holidays_by_city=public_holidays_by_city,
            allow_auto_drop_optional=allow_auto_drop_optional,
            llm_calls=llm_calls,
            prompt_toks=prompt_toks,
            comp_toks=comp_toks,
        )

    # Hallucination Firewall: reject any place_id not present in fetched_map
    valid_ranked: list[str] = []
    rejected_hallucinations: list[str] = []
    for pid in rank_out.ranked_place_ids:
        if pid in fetched_map and pid not in valid_ranked:
            valid_ranked.append(pid)
        elif pid not in fetched_map:
            rejected_hallucinations.append(pid)

    valid_ranked = proxy.record_cpsat_injection(
        valid_ranked[: proxy.max_candidates_to_cpsat]
    )

    # --- Step (4): CP-SAT Proof Solve over LLM Top-5 Grounded Candidates ---
    if valid_ranked:
        extra_pairs = [(pid, idx) for idx, pid in enumerate(valid_ranked)]
        ir_top = compile_dirty_ir(
            graph,
            dirty_days,
            merged_registry,
            proxy.transit_minutes,
            now_utc,
            tz_registry,
            verification_policy=verification_policy,
            extra_candidates_by_act={target_act.id: extra_pairs},
            public_holidays_by_city=public_holidays_by_city,
            allow_auto_drop_optional=False,
        )
        res_top = await solve_fn(ir_top, opt_timeout_sec=0.65, run_mus=False)
        if res_top.certificate in ("PROVEN_OPTIMAL_IN_SCOPE", "FEASIBLE_OPTIMALITY_UNPROVEN"):
            return PhaseBResult(
                status="SOLVED_BY_PHASE_B",
                solve_result=res_top,
                selected_candidates=tuple(valid_ranked),
                rejected_hallucinated_ids=tuple(rejected_hallucinations),
                llm_calls_made=llm_calls,
                prompt_tokens=prompt_toks,
                completion_tokens=comp_toks,
            )

    # --- Step (5): 0-LLM Fallback CP-SAT over untried fetched records ---
    untried = [pid for pid in fetched_map if pid not in valid_ranked]
    if untried:
        extra_fallback = [(pid, 10 + idx) for idx, pid in enumerate(untried)]
        ir_fb = compile_dirty_ir(
            graph,
            dirty_days,
            merged_registry,
            proxy.transit_minutes,
            now_utc,
            tz_registry,
            verification_policy=verification_policy,
            extra_candidates_by_act={target_act.id: extra_fallback},
            public_holidays_by_city=public_holidays_by_city,
            allow_auto_drop_optional=False,
        )
        res_fb = await solve_fn(ir_fb, opt_timeout_sec=0.45, run_mus=False)
        if res_fb.certificate in ("PROVEN_OPTIMAL_IN_SCOPE", "FEASIBLE_OPTIMALITY_UNPROVEN"):
            return PhaseBResult(
                status="SOLVED_BY_POOL_FALLBACK",
                solve_result=res_fb,
                selected_candidates=tuple(untried),
                rejected_hallucinated_ids=tuple(rejected_hallucinations),
                llm_calls_made=llm_calls,
                prompt_tokens=prompt_toks,
                completion_tokens=comp_toks,
            )

    # Still INFEASIBLE after 1 synchronous cycle -> escalate 2nd cycle to 30s Async Worker
    return PhaseBResult(
        status="NEEDS_ASYNC_SECOND_CYCLE",
        solve_result=None,
        selected_candidates=tuple(valid_ranked),
        rejected_hallucinated_ids=tuple(rejected_hallucinations),
        llm_calls_made=llm_calls,
        prompt_tokens=prompt_toks,
        completion_tokens=comp_toks,
    )


async def _handle_llm_outage(
    *,
    graph: TripGraph,
    dirty_days: frozenset[tuple[str, date]],
    place_registry: dict[str, Place],
    proxy: BudgetedProviderProxy,
    now_utc: int,
    tz_registry: PinnedTzRegistry,
    solve_fn: AsyncIRSolver,
    verification_policy: VerificationPolicy,
    public_holidays_by_city: dict[str, frozenset[date]] | None,
    allow_auto_drop_optional: bool,
    llm_calls: int,
    prompt_toks: int,
    comp_toks: int,
) -> PhaseBResult:
    """Consent-gated outage handler: NEVER silently drops user activities when False."""
    ir_drop = compile_dirty_ir(
        graph,
        dirty_days,
        place_registry,
        proxy.transit_minutes,
        now_utc,
        tz_registry,
        verification_policy=verification_policy,
        public_holidays_by_city=public_holidays_by_city,
        allow_auto_drop_optional=True,
    )
    drop_res = await solve_fn(ir_drop, opt_timeout_sec=0.45, run_mus=False)
    drop_feasible = drop_res.certificate in (
        "PROVEN_OPTIMAL_IN_SCOPE",
        "FEASIBLE_OPTIMALITY_UNPROVEN",
    )

    if allow_auto_drop_optional and drop_feasible:
        return PhaseBResult(
            status="DEGRADED_OPTIONAL_DROPPED",
            solve_result=drop_res,
            llm_calls_made=llm_calls,
            prompt_tokens=prompt_toks,
            completion_tokens=comp_toks,
        )

    # Default (allow_auto_drop_optional=False): NEVER commit a drop on LLM outage!
    # Return 503-equivalent status + optional UNCOMMITTED preview for user approval.
    return PhaseBResult(
        status="RETRYABLE_UPSTREAM_LLM_UNAVAILABLE",
        solve_result=None,
        suggested_fallback_preview=drop_res if drop_feasible else None,
        llm_calls_made=llm_calls,
        prompt_tokens=prompt_toks,
        completion_tokens=comp_toks,
    )


class OpenAICompatibleLLMClient:
    """Live Phase B client compatible with OpenAI, OpenRouter, Groq, and Gemini endpoints."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        temperature: float = 0.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._temperature = temperature

    def _chat_json(self, system_prompt: str, user_prompt: str, timeout_sec: float) -> tuple[dict, LLMCallMetrics]:
        import time as pytime

        t0 = pytime.perf_counter()
        try:
            resp = httpx.post(
                f"{self._base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json={
                    "model": self._model,
                    "temperature": self._temperature,
                    "response_format": {"type": "json_object"},
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                },
                timeout=timeout_sec,
            )
            if resp.status_code in (429, 500, 502, 503, 504):
                raise LLMOutageError(f"Upstream LLM HTTP {resp.status_code}")
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            usage = data.get("usage", {})
            return json.loads(content), LLMCallMetrics(
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
                latency_ms=(pytime.perf_counter() - t0) * 1000.0,
            )
        except (httpx.TimeoutException, httpx.HTTPError, KeyError, json.JSONDecodeError) as exc:
            raise LLMOutageError(f"LLM call failed: {exc}") from exc

    def generate_search_intent(
        self,
        *,
        target_act: ActivityNode,
        core_groups: tuple[AssumptionGroupSpec, ...],
        user_intent_notes: str,
        timeout_sec: float = 1.4,
    ) -> tuple[SearchIntentLLMOutput, LLMCallMetrics]:
        sys_p = (
            "You are a grounded travel search planner. Output strictly JSON with keys: "
            "target_categories (list[str]), preferred_tags (list[str]), avoid_tags (list[str]), rationale (str)."
        )
        usr_p = json.dumps(
            {
                "city_id": target_act.city_id,
                "original_category": target_act.category,
                "unsat_reasons": [g.detail for g in core_groups],
                "user_intent_notes": user_intent_notes,
            }
        )
        parsed, metrics = self._chat_json(sys_p, usr_p, timeout_sec)
        return SearchIntentLLMOutput.model_validate(parsed), metrics

    def rank_candidates(
        self,
        *,
        target_act: ActivityNode,
        candidates: list[Place],
        user_intent_notes: str,
        max_select: int = 5,
        timeout_sec: float = 1.4,
    ) -> tuple[SemanticRankLLMOutput, LLMCallMetrics]:
        sys_p = (
            "Rank up to 5 place_ids strictly from the provided candidate list. "
            "Output JSON: ranked_place_ids (list[str]), rationale (str)."
        )
        usr_p = json.dumps(
            {
                "target_activity_id": target_act.id,
                "user_intent_notes": user_intent_notes,
                "max_select": max_select,
                "candidates": [
                    {
                        "place_id": p.place_id,
                        "name": p.name,
                        "category": p.category,
                        "tags": list(p.tags),
                        "dietary_tags": list(p.dietary_tags),
                        "rating": p.rating,
                        "cost_usd": p.estimated_cost_usd,
                    }
                    for p in candidates
                ],
            }
        )
        parsed, metrics = self._chat_json(sys_p, usr_p, timeout_sec)
        return SemanticRankLLMOutput.model_validate(parsed), metrics
