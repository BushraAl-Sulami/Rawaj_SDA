"""Evaluation of the Strategy Agent (the checks and the judges live in evaluators.py; this file plugs them into run_all)."""

from evals.cases import build_cases
from evals.evaluators import build_evaluators
from evals.run_strategy_eval import target

NAME = "strategy"
DATASET = "rawaj-strategy-agent"
DESCRIPTION = "Qualification reports -> 30-day strategy. Reference = the High/Moderate gaps to address."


def build_examples() -> list[dict]:
    return [
        {
            "key": f"qualification_run_{case['qualification_run_id']}",
            "inputs": case["inputs"],
            "outputs": case["reference"],
            "metadata": {
                "restaurant": case["name"],
                "qualification_run_id": case["qualification_run_id"],
            },
        }
        for case in build_cases()
        if case["name"] == "3Brews"
    ]
