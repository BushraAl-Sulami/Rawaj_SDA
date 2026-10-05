"""Run the Qualification Agent on its own for one restaurant and update that restaurant's row.

    python scripts/run_qualification.py 1              # run for real, save into qualification_runs, show a summary
    python scripts/run_qualification.py 1 --show       # ... and print the whole result as JSON
    python scripts/run_qualification.py 1 --dry-run    # run for real but do not save anything

It uses the restaurant's latest saved research (nothing is scraped again), builds the same input the workflow
builds, and saves through `save_qualification_result`, so the restaurant's row is rewritten in place. It calls
OpenAI (and Tavily if the agent searches for a benchmark), so a run costs money and takes a few minutes.
"""

import argparse
import json
import sys
from copy import deepcopy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from sqlalchemy import select

from agents.qualification_agent.prompt import QUALIFICATION_PROMPT_VERSION
from database.database import SessionLocal
from database.models import QualificationRun, ResearchRun, Restaurant, RestaurantContext
from database.repository import build_qualification_input, save_qualification_result


def build_input(db, restaurant_id: int):
    """The workflow's input for this restaurant, from its latest saved research; (research_run, input, context)."""
    research_run = db.scalar(
        select(ResearchRun)
        .where(ResearchRun.restaurant_id == restaurant_id, ResearchRun.status.in_(["complete", "partial"]))
        .order_by(ResearchRun.id.desc())
        .limit(1)
    )
    if research_run is None:
        raise SystemExit(f"Restaurant {restaurant_id} has no saved research. Run the research first.")
    stored = db.get(RestaurantContext, restaurant_id)
    context = deepcopy(stored.data) if stored and stored.data else {}
    evidence = build_qualification_input(research_run)
    evidence["restaurant_context"] = deepcopy(context)
    evidence["analysis_coverage"] = research_run.analysis_coverage or {}
    evidence["data_quality"] = research_run.data_quality or {}
    return research_run, evidence, context


def main(argv=None, agent=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("restaurant_id", type=int, help="the restaurant to qualify (its id in the database)")
    parser.add_argument("--show", action="store_true", help="print the whole result as JSON")
    parser.add_argument("--dry-run", action="store_true", help="do not save anything")
    args = parser.parse_args(argv)

    if agent is None:
        from agents.qualification_agent.qualification_agent import run_qualification_agent as agent

    with SessionLocal() as db:
        restaurant = db.get(Restaurant, args.restaurant_id)
        if restaurant is None:
            raise SystemExit(f"No restaurant with id {args.restaurant_id}.")
        research_run, evidence, context = build_input(db, args.restaurant_id)
        before = db.scalar(
            select(QualificationRun)
            .where(QualificationRun.restaurant_id == args.restaurant_id)
            .order_by(QualificationRun.created_at.desc(), QualificationRun.id.desc())
            .limit(1)
        )
        before_id = before.id if before else None
        print(f"Running the Qualification Agent for {restaurant.name} (research run {research_run.id})...", flush=True)

        result = agent(evidence)
        result["restaurant_context"] = deepcopy(context)
        result["prompt_version"] = QUALIFICATION_PROMPT_VERSION

        print(f"\nDecision: {result.get('qualification')}")
        print(f"Confidence: {str(result.get('qualification_confidence') or '')[:160]}")
        print(f"Marketing gaps ({len(result.get('marketing_gaps') or [])}):")
        for gap in result.get("marketing_gaps") or []:
            print(f"  {gap.get('priority')}. [{gap.get('severity')}] {gap.get('gap')}")
        print(f"Strengths: {len(result.get('strengths') or [])} | Data limitations: {len(result.get('data_limitations') or [])}")

        if args.dry_run:
            print("\nDry run: nothing was saved.")
        else:
            saved = save_qualification_result(
                db=db, restaurant_id=args.restaurant_id, research_run_id=research_run.id, result=result,
            )
            action = "created" if before_id is None else "updated in place"
            print(f"\nqualification_runs row {saved.id} {action} (restaurant {args.restaurant_id}, research run {research_run.id}).")

        if args.show:
            print("\n" + json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
