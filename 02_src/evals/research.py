"""Evaluation of the Research Agent.

The agent scrapes Instagram (a paid, slow external call), so this evaluates the research results already saved in
the database instead of scraping again: is each result well-formed, are its numbers consistent, can every research
signal be traced to the metric it claims, and does the wording of each signal say what its numbers say.
"""

from sqlalchemy import select

from database.database import SessionLocal
from database.models import ResearchRun
from evals.common import make_judge, result

NAME = "research"
DATASET = "rawaj-research-agent"
DESCRIPTION = "Saved Research Agent results. Checks structure, numeric consistency, traceability and wording of the signals."


def build_examples() -> list[dict]:
    examples = []
    with SessionLocal() as db:
        latest = {}
        for run in db.scalars(select(ResearchRun).where(ResearchRun.status == "complete").order_by(ResearchRun.id)):
            if run.full_result:
                latest[run.restaurant_id] = run
        for run in latest.values():
            examples.append({
                "key": f"research_run_{run.id}",
                "inputs": {"research_result": run.full_result},
                "outputs": {},
                "metadata": {"restaurant": run.full_result.get("restaurant", {}).get("name"), "research_run_id": run.id},
            })
    return examples


def target(inputs: dict) -> dict:
    research = inputs["research_result"]
    return {
        "research": research,
        "signals": [
            {"signal_id": s.get("signal_id"), "observation": s.get("observation"), "metric_path": s.get("metric_path"), "value": s.get("value")}
            for s in research.get("research_signals", [])
        ],
        "error": None,
    }


# ---------------------------------------------------------------- code evaluators

def schema_valid(outputs: dict) -> dict:
    """The result follows the Research Agent's own output model."""
    from pydantic import ValidationError

    from agents.research_agent.research_schemas import ResearchProfile

    try:
        ResearchProfile.model_validate(outputs["research"])
    except ValidationError as error:
        return result("schema_valid", 0, "; ".join(f"{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in error.errors()[:5]))
    return result("schema_valid", 1)


def _walk(value, path=""):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk(item, f"{path}.{key}" if path else key)
    else:
        yield path, value


def metrics_consistent(outputs: dict) -> dict:
    """Percentages are between 0 and 100, counts are not negative, posts per week matches posts in 30 days, formats add up to 100%."""
    metrics = outputs["research"].get("metrics") or {}
    problems = []
    for path, value in _walk(metrics):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if path.endswith("pct") and not 0 <= value <= 100:
            problems.append(f"{path}={value} is not a percentage")
        if path.endswith(("count", "_last_30_days", "sample_size")) and value < 0:
            problems.append(f"{path}={value} is negative")
    activity = metrics.get("activity") or {}
    if {"content_last_30_days", "content_per_week"} <= activity.keys():
        expected = activity["content_last_30_days"] * 7 / 30
        if abs(expected - activity["content_per_week"]) > 0.06:
            problems.append(f"{activity['content_last_30_days']} posts in 30 days should be {expected:.2f} a week, not {activity['content_per_week']}")
    mix = metrics.get("format_mix") or {}
    shares = [item["pct"] for item in mix.values() if isinstance(item, dict) and "pct" in item]
    if shares and abs(sum(shares) - 100) > 1:
        problems.append(f"format shares add up to {sum(shares):.1f}%, not 100%")
    return result("metrics_consistent", 0 if problems else 1, "; ".join(problems))


def _resolve(root: dict, path: str):
    node = root
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


def signals_traceable(outputs: dict) -> dict:
    """Each research signal points to a metric that exists and repeats its values exactly."""
    research, signals = outputs["research"], outputs["signals"]
    if not signals:
        return result("signals_traceable", 0, "there are no research signals")
    problems = []
    for signal in signals:
        found, metric = _resolve(research, signal["metric_path"] or "")
        if not found:
            problems.append(f"{signal['signal_id']}: {signal['metric_path']} does not exist")
        elif isinstance(signal["value"], dict) and isinstance(metric, dict):
            wrong = [k for k, v in signal["value"].items() if k in metric and metric[k] != v]
            if wrong:
                problems.append(f"{signal['signal_id']}: {wrong} differ from {signal['metric_path']}")
    return result("signals_traceable", 1 - len(problems) / len(signals), "; ".join(problems))


def data_quality_reported(outputs: dict) -> dict:
    """The result says how complete the data is, so downstream agents can qualify their claims."""
    quality = outputs["research"].get("data_quality") or {}
    ok = bool(quality.get("status")) and "warnings" in quality
    return result("data_quality_reported", int(ok), "" if ok else "data_quality has no status or warnings")


RUBRIC = (
    "Each research signal has an 'observation' sentence and the 'value' (numbers) it is based on. Does every "
    "observation say exactly what its numbers say? Lower the score for a number that differs from the value, an "
    "invented fact, an unsupported comparison, or wording that claims more than the number shows."
)


def build_evaluators(with_judges: bool = True) -> list:
    evaluators = [schema_valid, metrics_consistent, signals_traceable, data_quality_reported]
    if with_judges:
        evaluators.append(make_judge("signal_faithfulness", RUBRIC, _judge(), output_of=lambda o: o["signals"]))
    return evaluators


def _judge():
    from evals.common import judge_llm

    return judge_llm()
