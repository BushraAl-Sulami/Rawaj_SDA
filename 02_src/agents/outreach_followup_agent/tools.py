"""LangChain tools for Rawaj's Outreach & Follow-Up Agent.

Each public function is a real LangChain tool. The tools do not invent outputs:
they delegate to configured persistence, Calendar, email, and Strategy-handoff
services. Missing configuration is reported as a truthful structured error.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from langchain_core.tools import tool
from pydantic import ValidationError
from .guardrails import require_matching_recipient, WorkflowSafetyError

try:  # Supports both package imports and direct local notebook imports.
    from .schemas import (
        ButtonAction,
        ButtonEventStatus,
        HumanEscalation,
        OutboundExecution,
        RelationshipMemory,
        StrategyOutputHandoff,
        StrategyRequestHandoff,
    )
except ImportError:  # pragma: no cover - used when a notebook imports this file directly.
    from schemas import (
        ButtonAction,
        ButtonEventStatus,
        HumanEscalation,
        OutboundExecution,
        RelationshipMemory,
        StrategyOutputHandoff,
        StrategyRequestHandoff,
    )


class ToolRuntimeNotConfigured(RuntimeError):
    """Raised when a tool needs a service that the application did not wire up."""


@dataclass
class OutreachToolServices:
    """Runtime dependencies injected by the application, never hard-coded here.

    repository must provide the persistence methods invoked below.
    calendar must expose get_relevant_events.
    email must expose send_approved_message.
    strategy_dispatcher must expose dispatch_strategy_request.
    """

    repository: Any
    calendar: Any | None = None
    email: Any | None = None
    strategy_dispatcher: Any | None = None


_TOOL_SERVICES = ContextVar("rawaj_tool_services", default=None)


def configure_tool_services(services: OutreachToolServices) -> None:
    """Connect real infrastructure to the tools at application startup."""

    _TOOL_SERVICES.set(services)


def clear_tool_services() -> None:
    """Remove configured services; useful for tests and controlled shutdown."""

    _TOOL_SERVICES.set(None)


def _services() -> OutreachToolServices:
    if _TOOL_SERVICES.get() is None:
        raise ToolRuntimeNotConfigured(
            "Outreach tool services are not configured. Configure real repository "
            "and provider services before invoking this tool."
        )
    return _TOOL_SERVICES.get()


def _ok(data: Any, status: str = "OK") -> dict[str, Any]:
    return {"ok": True, "status": status, "data": _serialize(data)}


def _error(
    code: str,
    message: str,
    *,
    retryable: bool = False,
    data: Any | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": False,
        "status": code,
        "error": message,
        "retryable": retryable,
    }
    if data is not None:
        result["data"] = _serialize(data)
    return result


def _serialize(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


def _run(operation: Callable[[], Any]) -> dict[str, Any]:
    """Run an infrastructure operation without leaking provider secrets/traces."""

    try:
        return _ok(operation())
    except ToolRuntimeNotConfigured as error:
        return _error("NOT_CONFIGURED", str(error), retryable=False)
    except ValidationError as error:
        return _error("VALIDATION_ERROR", "Input validation failed; rejected values are not logged.", retryable=False)
    except ValueError as error:
        return _error("VALIDATION_ERROR", "Input validation failed; rejected values are not logged.", retryable=False)
    except Exception as error:  # Provider/database details remain in server logs.
        return _error(
            "TOOL_EXECUTION_FAILED",
            f"{type(error).__name__}: operation could not be completed.",
            retryable=True,
        )


def _parse_time(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("Datetime values must use ISO-8601 format.") from error


@tool
def load_research_handoff(restaurant_id: int, research_run_id: int) -> dict[str, Any]:
    """
    Load the exact persisted Research Agent output for one restaurant and one
    Research run. Use this when grounded outreach context is missing or needs
    verification. Never replace run IDs with a restaurant-name lookup.
    """

    return _run(
        lambda: _services().repository.load_research_handoff(
            restaurant_id=restaurant_id,
            research_run_id=research_run_id,
        )
    )


@tool
def load_qualification_handoff(
    restaurant_id: int,
    qualification_run_id: int,
    research_run_id: int,
) -> dict[str, Any]:
    """
    Load the exact Qualification Agent output linked to the supplied Research
    run. Use it as an internal eligibility gate; do not expose its rationale,
    score, priority, or raw wording to the restaurant.
    """

    return _run(
        lambda: _services().repository.load_qualification_handoff(
            restaurant_id=restaurant_id,
            qualification_run_id=qualification_run_id,
            research_run_id=research_run_id,
        )
    )


@tool
def read_relationship_memory(restaurant_id: int) -> dict[str, Any]:
    """
    Read durable Outreach relationship memory before any history-dependent
    decision: follow-up, button event, strategy notification,
    client check-in, feedback, or issue handling.
    """

    return _run(
        lambda: _services().repository.read_relationship_memory(
            restaurant_id=restaurant_id
        )
    )


@tool
def write_relationship_memory(
    memory: dict[str, Any],
    expected_version: int | None = None,
) -> dict[str, Any]:
    """
    Validate and persist relationship memory after a meaningful state change.
    expected_version enables optimistic locking so concurrent graph runs do not
    silently overwrite each other.
    """

    def operation() -> Any:
        validated = RelationshipMemory.model_validate(memory)
        return _services().repository.write_relationship_memory(
            memory=validated,
            expected_version=expected_version,
        )

    return _run(operation)


@tool
def get_relevant_calendar_events(
    restaurant_id: int,
    start_at: str,
    end_at: str,
    restaurant_context: dict[str, Any],
) -> dict[str, Any]:
    """
    Read real upcoming events from the configured Rawaj Events Google Calendar
    and return only events that are relevant to the supplied restaurant context.

    Use this only when a seasonal or occasion-based opportunity could materially
    improve the communication decision. It never creates calendar events and
    never fabricates an event when Google Calendar is unavailable.
    """

    def operation() -> Any:
        start = _parse_time(start_at)
        end = _parse_time(end_at)
        if end <= start:
            raise ValueError("end_at must be later than start_at")

        services = _services()
        if services.calendar is None:
            raise ToolRuntimeNotConfigured(
                "Google Calendar is not configured for Rawaj Events."
            )

        return services.calendar.get_relevant_events(
            restaurant_id=restaurant_id,
            start_at=start,
            end_at=end,
            restaurant_context=restaurant_context,
        )

    return _run(operation)


@tool
def create_calendar_event(
    title: str,
    start_at: str,
    end_at: str,
    relevance_reason: str,
    approval_id: str | None = None,
    idempotency_key: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Create one real confirmed Calendar event only through an approval guard.

    This tool is intentionally not part of automatic prospect follow-up.  It
    can be used only for a genuinely confirmed meeting with complete timing,
    Calendar WRITE enabled, and a persistence-backed Human Approval matching
    the exact event content.  It never creates holiday reminders or imagined
    marketing events.
    """

    def operation() -> Any:
        services = _services()
        if services.calendar is None:
            raise ToolRuntimeNotConfigured(
                "Google Calendar is not configured for Rawaj Events."
            )
        start = _parse_time(start_at)
        end = _parse_time(end_at)
        return services.calendar.create_approved_event(
            title=title,
            start_at=start,
            end_at=end,
            relevance_reason=relevance_reason,
            approval_id=approval_id,
            idempotency_key=idempotency_key,
            description=description,
        )

    return _run(operation)


@tool
def create_strategy_request_handoff(handoff: dict[str, Any]) -> dict[str, Any]:
    """
    Create one idempotent Strategy Request Handoff after a verified Interested
    button event. The tool rejects requests without a stored APPLIED INTERESTED
    event and never asks the Strategy Agent to generate work before consent.
    """

    def operation() -> dict[str, Any]:
        validated = StrategyRequestHandoff.model_validate(handoff)
        services = _services()

        interest_event = services.repository.get_button_event(
            event_id=validated.interest_event_id
        )
        if interest_event is None:
            raise ValueError("Interest event was not found.")

        event_action = str(interest_event.get("action", ""))
        event_status = str(interest_event.get("status", ""))
        if event_action != ButtonAction.INTERESTED.value:
            raise ValueError("A Strategy request requires an Interested event.")
        if event_status != ButtonEventStatus.APPLIED.value:
            raise ValueError("The Interested event has not been applied.")
        if str(interest_event.get("restaurant_id")) != str(validated.restaurant_id):
            raise ValueError("Interest event restaurant_id does not match handoff.")
        if str(interest_event.get("outreach_message_id")) != validated.outreach_message_id:
            raise ValueError("Interest event message ID does not match handoff.")

        saved = services.repository.create_strategy_request_if_absent(validated)
        if services.strategy_dispatcher is None:
            return {
                "handoff": saved,
                "dispatch_status": "HANDOFF_NOT_CONFIGURED",
                "detail": "Request was recorded but not claimed as dispatched.",
            }

        # The repository payload also carries database-only fields such as the
        # relationship ID and dispatch metadata.  The teammate Strategy Agent
        # accepts the strict handoff contract only.  Dispatch even when the DB
        # row already exists: a crash after persistence but before outbox write
        # is safely recovered by the dispatcher's own idempotency key.
        dispatch = services.strategy_dispatcher.dispatch_strategy_request(
            validated.model_dump(mode="json")
        )
        if hasattr(services.repository, "record_strategy_dispatch"):
            saved = services.repository.record_strategy_dispatch(
                strategy_request_id=validated.strategy_request_id,
                dispatch=dispatch,
            )
        return {
            "handoff": saved,
            "dispatch_status": (
                "HANDED_OFF"
                if saved.get("created", True)
                else "RECOVERED_OR_ALREADY_HANDED_OFF"
            ),
            "dispatch": dispatch,
        }

    return _run(operation)


@tool
def load_strategy_handoff(strategy_request_id: str) -> dict[str, Any]:
    """
    Load the Strategy Agent output associated with one outstanding Strategy
    request. This tool only retrieves data; it does not mark the strategy ready.
    """

    return _run(
        lambda: _services().repository.load_strategy_handoff(
            strategy_request_id=strategy_request_id
        )
    )


@tool
def validate_strategy_handoff(
    strategy_request_id: str,
    strategy_output: dict[str, Any],
) -> dict[str, Any]:
    """
    Validate a received Strategy output against the outstanding request.
    Restaurant ID, request ID, READY status, dashboard URL, and permission for a
    client notification must all match before the workflow may notify a client.
    """

    def operation() -> dict[str, Any]:
        services = _services()
        request = services.repository.load_strategy_request(
            strategy_request_id=strategy_request_id
        )
        if request is None:
            raise ValueError("Strategy request was not found.")

        # Repository records contain operational fields outside the public
        # contract.  Project them explicitly instead of relaxing the strict
        # handoff schema used between teammates.
        request_data = {
            key: value
            for key, value in request.items()
            if key in StrategyRequestHandoff.model_fields
        }
        request_data.setdefault("handoff_type", "STRATEGY_REQUEST")
        request_model = StrategyRequestHandoff.model_validate(request_data)
        output_model = StrategyOutputHandoff.model_validate(strategy_output)
        errors = output_model.validation_errors_for(request_model)

        if errors:
            return {
                "valid": False,
                "status": "INVALID_STRATEGY_HANDOFF",
                "errors": errors,
            }

        return {
            "valid": True,
            "status": "VALID_STRATEGY_HANDOFF",
            "strategy_output": output_model.model_dump(mode="json"),
        }

    return _run(operation)


@tool
def send_approved_email(message_id: str, approval_id: str) -> dict[str, Any]:
    """
    Send an email through the configured real provider only after the repository
    verifies that the exact message revision has an unexpired APPROVED record.

    This tool must be called only by the LangGraph executor after its Human
    Approval interruption resumes. A missing provider or invalid approval is
    reported truthfully; it never returns a fabricated SENT result.
    """

    try:
        services = _services()
    except ToolRuntimeNotConfigured as error:
        return _error("NOT_CONFIGURED", str(error), retryable=False)
    if services.email is None:
        return _error(
            "EMAIL_NOT_CONFIGURED",
            "A real email provider is not configured for outbound sending.",
            retryable=False,
        )

    # Validate the provider *before* the repository claims the message as
    # SENDING.  Otherwise a missing .env setting could leave an approved email
    # in a misleading in-progress state even though no provider was contacted.
    settings = getattr(services.email, "settings", None)
    if settings is not None:
        try:
            settings.require_email()
        except Exception as error:
            return _error(
                "EMAIL_NOT_CONFIGURED",
                "A real email provider must be configured before submission.",
                retryable=False,
            )

    def operation() -> Any:

        sendable_message = services.repository.get_sendable_message(
            message_id=message_id,
            approval_id=approval_id,
        )
        if sendable_message is None:
            raise ValueError(
                "Message is not sendable: approval may be missing, expired, "
                "rejected, or tied to a different message revision."
            )

        if hasattr(services.repository, "load_research_handoff"):
            handoff = services.repository.load_research_handoff(
                restaurant_id=sendable_message["restaurant_id"],
                research_run_id=sendable_message["research_run_id"],
            )
            try:
                require_matching_recipient(sendable_message, handoff["restaurant"])
            except WorkflowSafetyError:
                return {"status": "EMAIL_SEND_FAILED", "provider_result": {
                    "success": False, "provider": "guardrail", "failure_code": "RECIPIENT_MISMATCH",
                    "failure_detail": "The current restaurant recipient differs from the reviewed message."}}
        provider_result = services.email.send_approved_message(sendable_message)
        if not provider_result.get("success"):
            return {
                "status": "EMAIL_SEND_FAILED",
                "provider_result": provider_result,
            }

        if not provider_result.get("provider_message_id"):
            raise ValueError(
                "Provider accepted a message without returning a provider message ID."
            )

        return {
            "status": "SENT",
            "provider_result": provider_result,
        }

    return _run(operation)


@tool
def record_outbound_execution(execution: dict[str, Any]) -> dict[str, Any]:
    """
    Persist the exact outcome from the email provider. This records GENERATED,
    APPROVED, SENT, DELIVERED, or FAILED truthfully and does not infer delivery
    from an API request alone.
    """

    def operation() -> Any:
        validated = OutboundExecution.model_validate(execution)
        return _services().repository.record_outbound_execution(validated)

    return _run(operation)


@tool
def create_human_escalation(escalation: dict[str, Any]) -> dict[str, Any]:
    """
    Create a durable escalation for sensitive issues, authority-sensitive
    requests, or maximum no-response attempts. This tool does not promise a
    refund, legal response, compensation, or resolution to the restaurant.
    """

    def operation() -> Any:
        validated = HumanEscalation.model_validate(escalation)
        return _services().repository.create_human_escalation(validated)

    return _run(operation)


OUTREACH_FOLLOWUP_TOOLS = [
    load_research_handoff,
    load_qualification_handoff,
    read_relationship_memory,
    write_relationship_memory,
    get_relevant_calendar_events,
    create_calendar_event,
    create_strategy_request_handoff,
    load_strategy_handoff,
    validate_strategy_handoff,
    send_approved_email,
    record_outbound_execution,
    create_human_escalation,
]


def get_outreach_followup_tools() -> list[Any]:
    """Return the one explicit tool list supplied to LangChain/LangGraph nodes."""

    return list(OUTREACH_FOLLOWUP_TOOLS)


__all__ = [
    "OUTREACH_FOLLOWUP_TOOLS",
    "OutreachToolServices",
    "ToolRuntimeNotConfigured",
    "clear_tool_services",
    "configure_tool_services",
    "create_calendar_event",
    "create_human_escalation",
    "create_strategy_request_handoff",
    "get_outreach_followup_tools",
    "get_relevant_calendar_events",
    "load_qualification_handoff",
    "load_research_handoff",
    "load_strategy_handoff",
    "read_relationship_memory",
    "record_outbound_execution",
    "send_approved_email",
    "validate_strategy_handoff",
    "write_relationship_memory",
]
