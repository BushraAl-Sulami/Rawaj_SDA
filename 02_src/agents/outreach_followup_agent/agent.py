"""Application-facing entrypoint for Rawaj's Outreach & Follow-Up Agent.

This file is intentionally small.  It wires the real services together and
provides clear functions for the team's orchestration graph, the email-button
API, and the follow-up scheduler.  Business decisions stay in ``workflow.py``
and prompts stay in ``prompt.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

try:  # Package import in the team repository.
    from database.outreach_repository import OutreachRepository
except ImportError:  # pragma: no cover - direct notebook import support.
    from outreach_repository import OutreachRepository

try:
    from .calendar_service import CalendarService
    from .config import RawajSettings, get_settings
    from .email_service import EmailService
    from .llm_service import RawajLLMService
    from .response_api import create_response_router
    from .scheduler import OutreachFollowUpScheduler
    from .strategy_handoff import FileStrategyHandoffDispatcher
    from .workflow import OutreachFollowUpWorkflow
except ImportError:  # pragma: no cover - direct notebook import support.
    from calendar_service import CalendarService
    from config import RawajSettings, get_settings
    from email_service import EmailService
    from llm_service import RawajLLMService
    from response_api import create_response_router
    from scheduler import OutreachFollowUpScheduler
    from strategy_handoff import FileStrategyHandoffDispatcher
    from workflow import OutreachFollowUpWorkflow


@dataclass
class RawajOutreachApplication:
    """Fully wired, replaceable runtime for the one Outreach agent."""

    settings: RawajSettings
    repository: Any
    workflow: OutreachFollowUpWorkflow
    scheduler: OutreachFollowUpScheduler

    @classmethod
    def create(
        cls,
        *,
        settings: RawajSettings | None = None,
        repository: Any | None = None,
        llm: Any | None = None,
        email_service: Any | None = None,
        calendar_service: Any | None = None,
        strategy_dispatcher: Any | None = None,
        checkpointer: Any | None = None,
        access_provider: Any | None = None,
    ) -> "RawajOutreachApplication":
        """Build the real production wiring or inject deterministic test fakes.

        No provider call happens during creation.  Missing API credentials are
        reported only at the relevant graph node, and never become a mocked LLM
        answer or a fake email send.
        """

        resolved_settings = settings or get_settings()
        resolved_repository = repository or OutreachRepository()
        resolved_llm = llm or RawajLLMService(resolved_settings)
        resolved_email = email_service or EmailService(resolved_settings)
        resolved_calendar = calendar_service or CalendarService(resolved_settings)
        resolved_dispatcher = strategy_dispatcher or FileStrategyHandoffDispatcher(
            resolved_settings
        )
        if access_provider is None:
            from api.accounts import provision_access
            from functools import partial
            access_provider = partial(provision_access, session_factory=resolved_repository._session_factory)
        workflow = OutreachFollowUpWorkflow(
            settings=resolved_settings,
            repository=resolved_repository,
            llm=resolved_llm,
            email_service=resolved_email,
            calendar_service=resolved_calendar,
            strategy_dispatcher=resolved_dispatcher,
            checkpointer=checkpointer,
            access_provider=access_provider,
        )
        return cls(
            settings=resolved_settings,
            repository=resolved_repository,
            workflow=workflow,
            scheduler=OutreachFollowUpScheduler(
                workflow=workflow,
                repository=resolved_repository,
            ),
        )

    def response_router(self) -> Any:
        """Return the real scanner-safe FastAPI router for email buttons."""

        return create_response_router(
            email_service=self.workflow.email_service,
            repository=self.repository,
            workflow_dispatcher=self.workflow,
        )


def run_outreach_after_qualification(
    *,
    restaurant_id: int,
    research_run_id: int,
    qualification_run_id: int,
    application: RawajOutreachApplication | None = None,
) -> dict[str, Any]:
    """Team-workflow adapter called after a persisted Qualification result.

    It accepts only immutable IDs—not restaurant names or ad-hoc notes—so the
    agent loads exactly the Research and Qualification outputs selected by the
    earlier team nodes.
    """

    runtime = application or RawajOutreachApplication.create()
    result = runtime.workflow.start_outreach(
        restaurant_id=restaurant_id,
        research_run_id=research_run_id,
        qualification_run_id=qualification_run_id,
    )
    return {
        "outreach_thread_id": result.thread_id,
        "outreach_status": result.relationship_memory.status.value,
        "outreach_action": result.decision.action.value if result.decision else None,
        "outreach_message_id": result.email_draft.message_id if result.email_draft else None,
        "outreach_pending_human_approval": bool(
            result.email_draft and result.approval is None and not result.errors
        ),
        "outreach_errors": result.errors,
    }


def receive_strategy_agent_output(
    strategy_output: dict[str, Any],
    *,
    application: RawajOutreachApplication | None = None,
) -> dict[str, Any]:
    """Adapter called only when the teammate Strategy Agent supplies output.

    The contract must contain a matching request ID, restaurant ID, READY
    status, client-notification permission, and dashboard URL.  A strategy-ready
    email is generated only after this validation and sends automatically after content review.
    """

    runtime = application or RawajOutreachApplication.create()
    result = runtime.workflow.receive_strategy_output(strategy_output)
    return {
        "outreach_thread_id": result.thread_id,
        "outreach_status": result.relationship_memory.status.value,
        "outreach_message_id": result.email_draft.message_id if result.email_draft else None,
        "outreach_pending_human_approval": bool(
            result.email_draft and result.approval is None and not result.errors
        ),
        "outreach_errors": result.errors,
    }


__all__ = [
    "RawajOutreachApplication",
    "receive_strategy_agent_output",
    "run_outreach_after_qualification",
]
