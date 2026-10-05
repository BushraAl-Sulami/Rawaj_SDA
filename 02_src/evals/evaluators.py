"""LangSmith evaluators for the Strategy Agent: code checks plus LLM-as-a-Judge.

The evaluated target returns {"strategy", "draft", "rounds", "errors", "warnings", "error"} (see
run_strategy_eval.py). Code evaluators are free and deterministic; the judges call a model and should
come from a different model family than the one that wrote the strategy, to avoid self-preference.
"""

import os
import re

from openevals.llm import create_llm_as_judge

from agents.strategy_agent.guardrails import check_strategy


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).casefold()).strip()


def _has_strategy(outputs: dict) -> bool:
    return bool(outputs.get("strategy")) and not outputs.get("error")


# ---------------------------------------------------------------- code evaluators

def contract_valid(outputs: dict) -> dict:
    """1 when the final strategy meets the output contract (schema, 30 days, allowed services)."""
    errors = outputs.get("errors") or []
    if outputs.get("error"):
        errors = [outputs["error"], *errors]
    ok = _has_strategy(outputs) and not errors
    return {"key": "contract_valid", "score": int(ok), "comment": "; ".join(errors) or "ok"}


def grounding(outputs: dict) -> dict:
    """1.0 with no grounding/timing warnings, minus 0.25 per warning (unknown gap, wrong severity, event dates)."""
    if not _has_strategy(outputs):
        return {"key": "grounding", "score": 0, "comment": "no strategy produced"}
    warnings = outputs.get("warnings") or []
    return {"key": "grounding", "score": max(0.0, 1 - 0.25 * len(warnings)), "comment": "; ".join(warnings) or "ok"}


def high_gap_coverage(outputs: dict, reference_outputs: dict) -> dict:
    """Share of the qualification's High gaps that the strategy lists among its primary gaps."""
    wanted = reference_outputs.get("high_gaps") or []
    if not _has_strategy(outputs):
        return {"key": "high_gap_coverage", "score": 0, "comment": "no strategy produced"}
    if not wanted:
        return {"key": "high_gap_coverage", "score": 1, "comment": "no High gaps in the reference"}
    covered = [_norm(g["gap"]) for g in outputs["strategy"].get("primary_marketing_gaps", [])]
    missed = [g for g in wanted if not any(_norm(g) in c or c in _norm(g) for c in covered)]
    return {
        "key": "high_gap_coverage", "score": 1 - len(missed) / len(wanted),
        "comment": f"missed: {missed}" if missed else "all High gaps covered",
    }


def reflection_effect(outputs: dict, inputs: dict) -> dict:
    """Findings (errors + warnings) fixed by self-reflection: draft findings minus final findings."""
    draft = outputs.get("draft")
    if not draft:
        return {"key": "reflection_issues_fixed", "score": 0, "comment": "no draft"}
    qualification, start = inputs["qualification_context"], inputs["strategy_start_date"]
    before = check_strategy(draft, qualification, start)
    after = {"errors": outputs.get("errors") or [], "warnings": outputs.get("warnings") or []}
    fixed = len(before["errors"]) + len(before["warnings"]) - len(after["errors"]) - len(after["warnings"])
    return {
        "key": "reflection_issues_fixed", "score": fixed,
        "comment": f"draft {len(before['errors'])} errors/{len(before['warnings'])} warnings -> "
                   f"final {len(after['errors'])}/{len(after['warnings'])}; rounds: {len(outputs.get('rounds') or [])}",
    }


# ---------------------------------------------------------------- LLM-as-a-Judge

RUBRICS = {
    "groundedness": (
        "Is every restaurant-specific claim in the strategy supported by the qualification report? "
        "1.0 = every claim, metric, gap and target traces to the report. Lower the score for each invented "
        "fact, metric, offer, product or gap, and for unsupported performance promises."
    ),
    "service_relevance": (
        "Does each recommended service directly address a verified gap or need in the report, and are the "
        "selected primary gaps the highest-impact High/Moderate ones? 1.0 = every service is clearly needed and "
        "the gaps are well prioritised. Lower the score for unnecessary services or ignored major gaps."
    ),
    "plan_quality": (
        "Judge the 30-day plan: one clear focus and one concise strategic action per day; logical progression "
        "toward the targets; no needless repetition; rest or monitoring days are justified; preparation and "
        "follow-up sit around any occasion used; actions stay strategic (no captions, scripts or shot lists); "
        "day 30 does not plan next month. 1.0 = a sequenced plan an agency could hand to the restaurant."
    ),
}

PROMPT = """You are an expert restaurant marketing strategist grading the output of an AI strategy agent.

Criterion: {rubric}

Grade only this criterion, using the qualification report as the sole source of truth about the restaurant.
The reference lists the High and Moderate gaps the strategy is expected to address.

<qualification_report>
{{inputs}}
</qualification_report>

<strategy>
{{outputs}}
</strategy>

<reference>
{{reference_outputs}}
</reference>
"""


def judge_model() -> str:
    """The same model as every agent (OPENAI_MODEL). Note: a judge that is also the author tends to grade kindly."""
    return os.getenv("OPENAI_MODEL", "gpt-5.6-luna")


def make_judge(key: str, judge_llm):
    grade = create_llm_as_judge(
        prompt=PROMPT.format(rubric=RUBRICS[key]), feedback_key=key, judge=judge_llm, continuous=True, use_reasoning=True,
    )

    def evaluator(inputs: dict, outputs: dict, reference_outputs: dict) -> dict:
        if not _has_strategy(outputs):
            return {"key": key, "score": 0, "comment": "no strategy produced"}
        return grade(
            inputs=inputs["qualification_context"], outputs=outputs["strategy"], reference_outputs=reference_outputs,
        )

    evaluator.__name__ = key
    return evaluator


def build_evaluators(with_judges: bool = True) -> list:
    evaluators = [contract_valid, grounding, high_gap_coverage, reflection_effect]
    if with_judges:
        from langchain_openai import ChatOpenAI

        judge_llm = ChatOpenAI(model=judge_model(), use_responses_api=True)
        evaluators += [make_judge(key, judge_llm) for key in RUBRICS]
    return evaluators
