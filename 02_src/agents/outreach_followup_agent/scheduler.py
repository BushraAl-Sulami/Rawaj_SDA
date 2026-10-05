"""Safe scheduler entrypoints for Rawaj prospect follow-up.

The scheduler never sends email by itself.  It finds relationships whose
``next_contact_at`` is due, starts their LangGraph follow-up run, and leaves
reviewed follow-up drafts send automatically. Only the first outreach needs human approval.
This makes a two-minute demo cadence and a weekly production cadence a
configuration change, not a different workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:  # Supports package imports and direct notebook imports.
    from .workflow import OutreachFollowUpWorkflow
except ImportError:  # pragma: no cover - direct notebook import.
    from workflow import OutreachFollowUpWorkflow


@dataclass(frozen=True)
class FollowUpSchedulerResult:
    """Safe summary of one scheduler pass; it contains no customer content."""

    scanned: int
    started: int
    paused_for_human_approval: int
    stopped_or_waiting: int
    failed: int
    items: list[dict[str, Any]] = field(default_factory=list)


class OutreachFollowUpScheduler:
    """Invoke due follow-up graph runs from a worker, cron job, or notebook.

    The repository supplies a deliberately narrow list of due prospect
    relationships.  The graph independently checks the state again, so a race
    with an opt-out or a recent response cannot create a promotional email.
    """

    def __init__(self, *, workflow: OutreachFollowUpWorkflow, repository: Any) -> None:
        self.workflow = workflow
        self.repository = repository

    def run_due_followups_once(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> FollowUpSchedulerResult:
        """Start at most ``limit`` due graph runs; never approve or send them."""

        timestamp = now or datetime.now(timezone.utc)
        due_relationships = self.repository.list_due_follow_up_relationships(
            now=timestamp,
            limit=limit,
        )
        items: list[dict[str, Any]] = []
        started = paused = stopped = failed = 0

        for relationship in due_relationships:
            restaurant_id = relationship.get("restaurant_id")
            research_run_id = relationship.get("research_run_id")
            qualification_run_id = relationship.get("qualification_run_id")
            if (
                restaurant_id is None
                or research_run_id is None
                or qualification_run_id is None
            ):
                failed += 1
                items.append(
                    {
                        "restaurant_id": restaurant_id,
                        "status": "SKIPPED_MISSING_PROVENANCE",
                    }
                )
                continue

            try:
                result = self.workflow.run_follow_up_due(
                    restaurant_id=restaurant_id,
                    research_run_id=research_run_id,
                    qualification_run_id=qualification_run_id,
                )
            except Exception as error:
                failed += 1
                items.append(
                    {
                        "restaurant_id": restaurant_id,
                        "status": "WORKFLOW_FAILED",
                        "error_type": type(error).__name__,
                    }
                )
                continue

            started += 1
            if result.errors:
                failed += 1
                status = "WORKFLOW_STOPPED_WITH_ERRORS"
            elif result.email_draft is not None and result.approval is None:
                # The graph is paused at Human Approval.  This is truthful even
                # when no email provider has been configured yet.
                paused += 1
                status = "PENDING_HUMAN_APPROVAL"
            else:
                stopped += 1
                status = result.relationship_memory.status.value
            items.append(
                {
                    "restaurant_id": restaurant_id,
                    "status": status,
                    "thread_id": result.thread_id,
                }
            )

        return FollowUpSchedulerResult(
            scanned=len(due_relationships),
            started=started,
            paused_for_human_approval=paused,
            stopped_or_waiting=stopped,
            failed=failed,
            items=items,
        )

    def recover_pending_responses_once(self, *, limit: int = 100) -> None:
        if not hasattr(self.repository, "list_pending_button_events"):
            return
        for event in self.repository.list_pending_button_events(limit=limit):
            self.workflow.resume_from_button_event(event)

    def run_due_client_checks_once(
        self,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> FollowUpSchedulerResult:
        """Start due client reassessments without forcing a generic check-in."""

        timestamp = now or datetime.now(timezone.utc)
        due_relationships = self.repository.list_due_client_check_relationships(
            now=timestamp,
            limit=limit,
        )
        items: list[dict[str, Any]] = []
        started = paused = stopped = failed = 0

        for relationship in due_relationships:
            restaurant_id = relationship.get("restaurant_id")
            research_run_id = relationship.get("research_run_id")
            qualification_run_id = relationship.get("qualification_run_id")
            if None in {restaurant_id, research_run_id, qualification_run_id}:
                failed += 1
                items.append(
                    {
                        "restaurant_id": restaurant_id,
                        "status": "SKIPPED_MISSING_PROVENANCE",
                    }
                )
                continue
            try:
                result = self.workflow.run_scheduled_client_check(
                    restaurant_id=restaurant_id,
                    research_run_id=research_run_id,
                    qualification_run_id=qualification_run_id,
                )
            except Exception as error:
                failed += 1
                items.append(
                    {
                        "restaurant_id": restaurant_id,
                        "status": "WORKFLOW_FAILED",
                        "error_type": type(error).__name__,
                    }
                )
                continue

            started += 1
            if result.errors:
                failed += 1
                status = "WORKFLOW_STOPPED_WITH_ERRORS"
            elif result.email_draft is not None and result.approval is None:
                paused += 1
                status = "PENDING_HUMAN_APPROVAL"
            else:
                stopped += 1
                status = result.relationship_memory.status.value
            items.append(
                {
                    "restaurant_id": restaurant_id,
                    "status": status,
                    "thread_id": result.thread_id,
                }
            )

        return FollowUpSchedulerResult(
            scanned=len(due_relationships),
            started=started,
            paused_for_human_approval=paused,
            stopped_or_waiting=stopped,
            failed=failed,
            items=items,
        )


__all__ = ["FollowUpSchedulerResult", "OutreachFollowUpScheduler"]
