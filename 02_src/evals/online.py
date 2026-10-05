"""Online evaluation: score every real run of the pipeline with the checks written in code.

The orchestration (orchestration/workflow.py) calls these right after an agent produced something. The scores are
attached, as LangSmith feedback, to the run of that graph step, so every real analysis shows how each agent did.

- only code checks run here; orchestration/workflow.py also runs its own per-node LLM judges
- does nothing unless LangSmith tracing is on (LANGSMITH_TRACING=true); set ONLINE_EVALS=false to switch it off
- can never break the pipeline: any problem is logged and ignored
"""

import logging
import os
import re

from evals.common import result

logger = logging.getLogger(__name__)


def enabled() -> bool:
    return os.getenv("ONLINE_EVALS", "true").strip().lower() not in {"0", "false", "no", "off"}


def _current_run():
    from langsmith.run_helpers import get_current_run_tree

    return get_current_run_tree()


def record(scores: list[dict]) -> None:
    """Attach scores to the run that is executing right now."""
    from langsmith import Client

    run = _current_run()
    if run is None:
        return
    # Short timeout and one attempt: by default each feedback retries 10 times with a 90 s read timeout,
    # so a slow LangSmith API could hold the pipeline for many minutes.
    client = Client(timeout_ms=(5_000, 15_000))
    for score in scores:
        client.create_feedback(run.id, key=score["key"], score=score["score"], comment=score.get("comment"),
                               source_info={"source": "rawaj-online-check"}, stop_after_attempt=1)


def _safely(name, work):
    if not enabled():
        return
    try:
        if _current_run() is not None:
            work()
    except Exception as error:  # a failed check must never fail a pipeline run
        logger.warning("Online evaluation of %s was skipped (%s)", name, type(error).__name__)


def check_research(research_result: dict) -> None:
    def work():
        from evals import research

        outputs = research.target({"research_result": research_result})
        record([check(outputs) for check in (research.schema_valid, research.metrics_consistent, research.signals_traceable, research.data_quality_reported)])

    _safely("research", work)


def check_qualification(report: dict, evidence: dict) -> None:
    def work():
        from evals import qualification

        record([qualification.report_valid(report), qualification.evidence_grounded(report, evidence), qualification.signals_exist(report, evidence)])

    _safely("qualification", work)


def check_first_email(body: str, restaurant_name: str) -> None:
    def work():
        from evals import outreach

        # The stored text also holds the answer buttons that trusted code added after the model wrote the email.
        written = re.split(r"Yes, I'm interested", body or "", maxsplit=1)[0]
        record([outreach.no_links(written), outreach.no_internal_terms(written), outreach.names_the_restaurant(written, restaurant_name),
                outreach.mentions_free_trial(written), outreach.concise(written)])

    _safely("the first email", work)


def check_strategy(strategy_data: dict, qualification: dict | None, start_date: str | None) -> None:
    def work():
        from agents.strategy_agent.guardrails import check_strategy as contract_checks

        found = contract_checks(strategy_data, qualification, start_date)
        record([
            result("contract_valid", int(not found["errors"]), "; ".join(found["errors"])),
            result("grounding", max(0.0, 1 - 0.25 * len(found["warnings"])), "; ".join(found["warnings"])),
        ])

    _safely("the strategy", work)


def check_emails_sent(summary: dict) -> None:
    """Did the emails that passed the review really go out? (no human is in the loop until the restaurant answers)"""
    def work():
        approved, skipped = summary.get("approved") or [], summary.get("skipped") or []
        failed = [item for item in approved if item.get("errors")]
        broken = [item for item in skipped if str(item.get("reason", "")).startswith("approval failed")]
        if not (approved or skipped):
            return  # nothing was waiting: nothing to check
        problems = [f"{item['message_id']}: {item['errors']}" for item in failed] + [
            f"{item['message_id']}: {item['reason']}" for item in broken
        ]
        sent = len(approved) - len(failed)
        record([
            result("email_sent", sent / (len(approved) + len(broken)) if approved or broken else 1, "; ".join(problems)),
        ])

    _safely("the email sending", work)


def check_strategy_handoff(summary: dict) -> None:
    """Did every Strategy request written after an "Interested" click reach the Strategy Agent and get saved?"""
    def work():
        scanned = summary.get("scanned") or 0
        if not scanned:
            return
        failures = [f"{item.get('request_file') or item.get('strategy_request_id')}: {item.get('error')}"
                    for item in summary.get("items") or [] if item.get("status") == "FAILED"]
        record([result("strategy_handoff_processed", (summary.get("processed") or 0) / scanned, "; ".join(failures))])

    _safely("the strategy handoff", work)
