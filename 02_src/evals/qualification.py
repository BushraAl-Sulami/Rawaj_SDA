"""Evaluation of the Qualification Agent: it is run for real on the saved research of each restaurant.

Code checks: the report follows the schema, the severities and priorities are valid, every number in a gap's evidence
comes from the research (or a cited external benchmark), and every research signal it cites exists.
LLM judges: are the gaps supported by the research, are they prioritised sensibly, is the decision consistent.
"""

import json

from sqlalchemy import select

from database.database import SessionLocal
from database.models import ResearchRun
from database.repository import build_qualification_input
from evals.common import judge_llm, make_judge, normalize_number, numbers_in, result

NAME = "qualification"
DATASET = "rawaj-qualification-agent"
DESCRIPTION = "Research results -> the Qualification Agent's report. Reference = the research signal ids it may cite."
SEVERITIES = {"High", "Moderate", "Low"}
# Numbers on a line like these come from an outside source (a benchmark), not from the restaurant's own research.
EXTERNAL_MARKERS = (
    "benchmark", "reference", "reported", "according to", "dash social", "hootsuite", "sprout", "rival iq",
    "restaurant velocity", "directional", "study", "industry", "average instagram", "food and beverage",
)


def build_examples() -> list[dict]:
    examples = []
    with SessionLocal() as db:
        latest = {}
        for run in db.scalars(select(ResearchRun).where(ResearchRun.status == "complete").order_by(ResearchRun.id)):
            latest[run.restaurant_id] = run
        for run in latest.values():
            evidence = build_qualification_input(run)
            evidence["restaurant_context"] = {}
            evidence["analysis_coverage"] = run.analysis_coverage or {}
            evidence["data_quality"] = run.data_quality or {}
            examples.append({
                "key": f"research_run_{run.id}",
                "inputs": {"evidence": json.loads(json.dumps(evidence, default=str))},
                "outputs": {"signal_ids": [s.get("signal_id") for s in (run.research_signals or [])]},
                "metadata": {"restaurant": run.restaurant.name, "research_run_id": run.id},
            })
    return examples


def target(inputs: dict) -> dict:
    from agents.qualification_agent.qualification_agent import run_qualification_agent

    try:
        return {"report": run_qualification_agent(inputs["evidence"]), "error": None}
    except Exception as error:
        return {"report": None, "error": f"{type(error).__name__}: {error}"}


# ---------------------------------------------------------------- code evaluators (also used online, on real runs)

def report_valid(report: dict) -> dict:
    """The report follows the schema; severities are High/Moderate/Low; priorities are 1, 2, 3...; every gap has evidence."""
    from pydantic import ValidationError

    from agents.qualification_agent.schemas import Report

    problems = []
    fields = {key: report[key] for key in Report.model_fields if key in report}
    try:
        Report.model_validate(fields)
    except ValidationError as error:
        problems.extend(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()[:5])
    gaps = report.get("marketing_gaps") or []
    if not gaps:
        problems.append("the report lists no marketing gaps")
    bad = sorted({g["severity"] for g in gaps if g["severity"] not in SEVERITIES})
    if bad:
        problems.append(f"unknown severities {bad}")
    if [g["priority"] for g in gaps] != list(range(1, len(gaps) + 1)):
        problems.append(f"priorities are {[g['priority'] for g in gaps]}, not 1..{len(gaps)}")
    if any(not g.get("evidence") for g in gaps):
        problems.append("a gap has no evidence")
    return result("report_valid", 0 if problems else 1, "; ".join(problems))


def evidence_grounded(report: dict, evidence: dict) -> dict:
    """Share of evidence lines with numbers whose numbers come from the research (or from a cited external benchmark).

    A heuristic: a figure the agent derived itself (a difference, a rounded value) is reported as not grounded.
    """
    known = numbers_in(evidence)
    lines = [line for gap in report.get("marketing_gaps") or [] for line in gap.get("evidence") or [] if numbers_in(line)]
    if not lines:
        return result("evidence_grounded", 1, "no numeric evidence to check")
    ungrounded = []
    for line in lines:
        if any(marker in line.lower() for marker in EXTERNAL_MARKERS):
            continue
        # Whole numbers up to 31 (days, list positions) are too common to prove anything; every other figure is checked.
        missing = sorted(n for n in numbers_in(line) if n not in known and ("." in n or float(n) > 31))
        if missing:
            ungrounded.append(f"{missing} in “{line[:80]}”")
    return result("evidence_grounded", 1 - len(ungrounded) / len(lines), "; ".join(ungrounded[:4]))


def signals_exist(report: dict, evidence: dict) -> dict:
    """Every research signal id the report cites ('activity_01', ...) exists in the research it was given."""
    import re

    valid = {s.get("signal_id") for s in evidence.get("research_signals") or []}
    cited = {ref for gap in report.get("marketing_gaps") or [] for line in gap.get("evidence") or []
             for ref in re.findall(r"\b[a-z_]+_\d{2}\b", line)}
    unknown = sorted(cited - valid)
    return result("signals_exist", 0 if unknown else 1, f"unknown signal ids {unknown}" if unknown else f"{len(cited)} cited, all exist")


def _guard(check):
    def evaluator(inputs: dict, outputs: dict) -> dict:
        if outputs.get("error") or not outputs.get("report"):
            return result(check.__name__, 0, f"the agent failed: {outputs.get('error')}")
        return check(outputs["report"], inputs["evidence"]) if check is not report_valid else check(outputs["report"])

    evaluator.__name__ = check.__name__
    return evaluator


RUBRICS = {
    "gap_groundedness": (
        "The input is the research evidence about a restaurant's Instagram; the output is a report of marketing gaps. "
        "Is every gap and every piece of its evidence supported by the research evidence (or clearly labelled as an "
        "outside benchmark)? Lower the score for invented facts, misread numbers or overstated claims."
    ),
    "prioritisation": (
        "Are the marketing gaps ordered sensibly? The High-severity gaps should rest on the strongest, most "
        "business-relevant evidence and come first; minor issues should not outrank major ones."
    ),
    "decision_consistency": (
        "Is the qualification decision and its rationale consistent with the gaps, strengths and data limitations in the "
        "report, without overpromising? Lower the score for a decision the report does not support."
    ),
}


def build_evaluators(with_judges: bool = True) -> list:
    evaluators = [_guard(report_valid), _guard(evidence_grounded), _guard(signals_exist)]
    if with_judges:
        judge = judge_llm()
        evaluators += [make_judge(key, rubric, judge, output_of=lambda o: o["report"]) for key, rubric in RUBRICS.items()]
    return evaluators
