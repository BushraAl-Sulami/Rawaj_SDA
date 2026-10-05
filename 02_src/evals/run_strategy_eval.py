"""Run the Strategy Agent over the LangSmith dataset and score it (code checks + LLM judges).

    python -m evals.run_strategy_eval --limit 1                       # smoke test on one case
    python -m evals.run_strategy_eval                                 # all cases
    python -m evals.run_strategy_eval --model gpt-5.6-luna            # compare another model
    python -m evals.run_strategy_eval --no-judges                     # code checks only (cheap)

Each case runs the full strategy stage (ReAct draft + Self-Reflection) and then the judges, so it makes
paid model and search calls. Results appear as an experiment in the LangSmith project from .env; run it
once per model or prompt version and compare the experiments there.
"""

import argparse
import os

from dotenv import load_dotenv

DATASET = "rawaj-strategy-agent"


def target(inputs: dict) -> dict:
    """Run the Strategy Agent (ReAct draft, then Shaimaa's self-reflection) and report what the evaluators need."""
    from agents.strategy_agent.guardrails import check_strategy
    from agents.strategy_agent.strategy_agent import generate_strategy

    qualification, start = inputs["qualification_context"], inputs["strategy_start_date"]
    try:
        result = generate_strategy(qualification, start, return_evaluation_data=True)
    except Exception as error:
        return {"strategy": None, "draft": None, "rounds": [], "errors": [], "warnings": [], "error": f"{type(error).__name__}: {error}"}
    final, reflection = result["final_strategy"], result["reflection_result"]
    return {
        "strategy": final,
        "draft": result["initial_strategy"],
        "rounds": [{"round": 1, "issues": reflection.get("issues", []), "passed": reflection.get("passed")}],
        # The contract checks are applied to the final strategy here, for scoring; they do not gate saving.
        **check_strategy(final, qualification, start),
        "error": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--limit", type=int, help="only the first N cases")
    parser.add_argument("--model", help="model for this run, e.g. gpt-5.6-luna (default: OPENAI_MODEL, else gpt-5.6-luna)")
    parser.add_argument("--no-judges", action="store_true", help="skip the LLM judges")
    args = parser.parse_args()

    load_dotenv()
    if args.model:
        os.environ["OPENAI_MODEL"] = args.model

    from langsmith import Client

    from evals.evaluators import build_evaluators, judge_model

    model = os.getenv("OPENAI_MODEL", "gpt-5.6-luna")
    client = Client()
    data = client.list_examples(dataset_name=DATASET, limit=args.limit) if args.limit else DATASET
    results = client.evaluate(
        target,
        data=data,
        evaluators=build_evaluators(with_judges=not args.no_judges),
        experiment_prefix=f"strategy-{model}",
        metadata={"strategy_model": model, "judge_model": None if args.no_judges else judge_model()},
        max_concurrency=1,
    )
    print(f"Done: {results.experiment_name}. Open the LangSmith project from .env to compare experiments.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
