"""Evaluate every agent on LangSmith with one command.

    python -m evals.run_all                              # every agent, every case (paid: OpenAI, Tavily)
    python -m evals.run_all --limit 1                    # one case per agent, a quick check
    python -m evals.run_all --agents research,outreach_review
    python -m evals.run_all --no-judges                  # only the checks written in code (cheap)
    python -m evals.run_all --upload-only                # create/refresh the datasets and stop

Agents: research, qualification, outreach_review, outreach_email, outreach_decision, strategy.
Each run is an experiment in the LangSmith project of .env; the scores are printed at the end.
"""

import argparse
import os
from collections import defaultdict

from dotenv import load_dotenv


def registry():
    from evals import outreach, qualification, research, strategy

    return {module.NAME: module for module in (research, qualification, outreach.review, outreach.email, outreach.decision, strategy)}


def mean_scores(results) -> dict[str, float]:
    scores = defaultdict(list)
    for row in results:
        for item in row["evaluation_results"]["results"]:
            if item.score is not None:
                scores[item.key].append(float(item.score))
    return {key: sum(values) / len(values) for key, values in scores.items()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--agents", help="comma separated list (default: all)")
    parser.add_argument("--limit", type=int, help="only the first N cases of each agent")
    parser.add_argument("--no-judges", action="store_true", help="skip the LLM judges")
    parser.add_argument("--upload-only", action="store_true", help="create/refresh the datasets and stop")
    parser.add_argument("--model", help="model for this run (default: OPENAI_MODEL, else gpt-5.6-luna)")
    args = parser.parse_args()

    load_dotenv()
    if args.model:
        os.environ["OPENAI_MODEL"] = args.model

    from langsmith import Client

    from evals.common import model_name, upsert_examples

    agents = registry()
    chosen = [name.strip() for name in args.agents.split(",")] if args.agents else list(agents)
    unknown = [name for name in chosen if name not in agents]
    if unknown:
        raise SystemExit(f"Unknown agent(s) {unknown}. Choose from {list(agents)}.")

    client = Client()
    summary = {}
    for name in chosen:
        module = agents[name]
        examples = module.build_examples()
        if not examples:
            print(f"[{name}] no examples in the database; skipped", flush=True)
            continue
        new, existing = upsert_examples(client, module.DATASET, module.DESCRIPTION, examples)
        print(f"[{name}] dataset '{module.DATASET}': {new} new, {existing} already there", flush=True)
        if args.upload_only:
            continue
        data = client.list_examples(dataset_name=module.DATASET, limit=args.limit) if args.limit else module.DATASET
        results = client.evaluate(
            module.target,
            data=data,
            evaluators=module.build_evaluators(with_judges=not args.no_judges),
            summary_evaluators=list(getattr(module, "summary_evaluators", [])) or None,
            experiment_prefix=f"{name}-{model_name()}",
            metadata={"agent": name, "model": model_name(), "judges": not args.no_judges},
            max_concurrency=1,
        )
        summary[name] = (results.experiment_name, mean_scores(results))
        print(f"[{name}] done: {results.experiment_name}", flush=True)

    if summary:
        print("\n==================== SCORES (average over the cases) ====================")
        for name, (experiment, scores) in summary.items():
            print(f"\n{name}   (experiment {experiment})")
            for key, value in sorted(scores.items()):
                print(f"   {key:28} {value:.2f}")
        print("\nOpen LangSmith -> Datasets & Experiments to see every case, the judges' reasoning and to compare runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
