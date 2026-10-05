"""Run pending Strategy handoff requests created by the Outreach Agent."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from agents.outreach_followup_agent.config import get_settings
from agents.outreach_followup_agent.schemas import StrategyRequestHandoff

from .strategy_agent import generate_and_save_strategy_from_handoff


def process_strategy_requests_once(limit: int = 10) -> dict:
    """
    Process pending Strategy requests once.

    Outreach creates a StrategyRequestHandoff after a verified
    Interested response. This worker consumes the request,
    runs the Strategy Agent, and saves the generated strategy.
    """

    settings = get_settings()

    inbox_dir = Path(settings.strategy_handoff_outbox)
    processed_dir = inbox_dir.parent / "processed"

    inbox_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)

    request_files = sorted(
        inbox_dir.glob("*.json")
    )[:limit]

    results = []

    for request_path in request_files:
        try:
            payload = json.loads(
                request_path.read_text(encoding="utf-8")
            )

            handoff = StrategyRequestHandoff.model_validate(
                payload
            )

            from database.outreach_repository import OutreachRepository
            from database.database import SessionLocal
            from database.models import Strategy
            from sqlalchemy import select
            repository = OutreachRepository()
            request = repository.load_strategy_request(strategy_request_id=handoff.strategy_request_id)
            event = repository.get_button_event(event_id=handoff.interest_event_id)
            memory = repository.read_relationship_memory(restaurant_id=int(handoff.restaurant_id))
            if not request or not event or event["action"] != "INTERESTED" or event["status"] != "APPLIED":
                raise ValueError("Strategy requires a persisted, applied Interested response.")
            if str(event["restaurant_id"]) != str(handoff.restaurant_id) or event["outreach_message_id"] != handoff.outreach_message_id:
                raise ValueError("Interest response does not match this restaurant and message.")
            for key in ("restaurant_id", "research_run_id", "qualification_run_id", "interest_event_id", "outreach_message_id"):
                if str(request[key]) != str(getattr(handoff, key)):
                    raise ValueError("Strategy request provenance mismatch.")
            if not memory or memory["status"] == "DO_NOT_CONTACT" or memory.get("do_not_contact_at"):
                request_path.replace(processed_dir / request_path.name)
                results.append({"strategy_request_id": handoff.strategy_request_id, "status": "CANCELLED_OPT_OUT"})
                continue
            with SessionLocal() as db:
                saved = next((row for row in db.scalars(select(Strategy).where(Strategy.restaurant_id == int(handoff.restaurant_id)))
                    if (row.strategy_data or {}).get("strategy_request_id") == handoff.strategy_request_id), None)
                strategy_result = {"strategy_id": saved.id} if saved else None
            if strategy_result is None:
                strategy_result = generate_and_save_strategy_from_handoff(handoff)
            # Persist the notification before removing the input; recovery never loses email 2.
            notify_dir = inbox_dir.parent / "notify"
            notify_dir.mkdir(parents=True, exist_ok=True)
            from agents.outreach_followup_agent.strategy_handoff import FileStrategyHandoffDispatcher
            FileStrategyHandoffDispatcher._atomic_write(notify_dir / request_path.name, {
                "strategy_request_id": handoff.strategy_request_id,
                "restaurant_id": handoff.restaurant_id, "strategy_id": strategy_result["strategy_id"]})

            processed_path = processed_dir / request_path.name

            request_path.replace(processed_path)

            results.append(
                {
                    "strategy_request_id": handoff.strategy_request_id,
                    "restaurant_id": handoff.restaurant_id,
                    "strategy_id": strategy_result["strategy_id"],
                    "status": "STRATEGY_GENERATED_AND_SAVED",
                }
            )

        except Exception as error:
            results.append(
                {
                    "request_file": str(request_path),
                    "status": "FAILED",
                    "error": str(error),
                }
            )

    return {
        "scanned": len(request_files),
        "processed": sum(
            item["status"] == "STRATEGY_GENERATED_AND_SAVED"
            for item in results
        ),
        "failed": sum(
            item["status"] == "FAILED"
            for item in results
        ),
        "items": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run pending Rawaj Strategy requests once."
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=10,
        help="Maximum Strategy requests to process.",
    )

    args = parser.parse_args()

    from orchestration.workflow import run_strategy_stage
    result = run_strategy_stage(limit=args.limit)

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())