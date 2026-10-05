"""Shared contracts for Rawaj's Outreach & Follow-Up Agent.

This module defines data only.  It does not call an LLM, send an email, read
Google Calendar, or generate a marketing strategy.  Keeping the contracts in
one place makes the agent safe to connect to the team's Research,
Qualification, and future Strategy agents without relying on restaurant names
as identifiers.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from enum import Enum
from operator import add
from typing import Annotated, Any, Literal, NotRequired, TypedDict
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def utc_now() -> str:
    """Return a timezone-aware timestamp suitable for persisted audit records."""

    return datetime.now(timezone.utc).isoformat()


def new_id(prefix: str) -> str:
    """Create an opaque local identifier; provider IDs are stored separately."""

    return f"{prefix}_{uuid4().hex}"


def normalize_qualification_status(value: str | None) -> "QualificationStatus":
    """Normalize the current team's label without guessing from free-text notes.

    The Qualification Agent currently persists values such as ``Qualified`` and
    ``Qualified for Rawaj.``.  A negative match is deliberately checked first.
    """

    normalized = (value or "").strip().upper()
    if not normalized:
        return QualificationStatus.UNKNOWN
    if re.search(r"\b(NOT[ _-]?QUALIFIED|UNQUALIFIED)\b", normalized):
        return QualificationStatus.NOT_QUALIFIED
    if re.search(r"\bQUALIFIED\b", normalized):
        return QualificationStatus.QUALIFIED
    return QualificationStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Workflow vocabulary
# ---------------------------------------------------------------------------


class RelationshipStatus(str, Enum):
    """Persistent lifecycle states owned by this agent."""

    READY_TO_CONTACT = "READY_TO_CONTACT"
    PENDING_OUTBOUND_APPROVAL = "PENDING_OUTBOUND_APPROVAL"
    WAITING_FOR_RESPONSE = "WAITING_FOR_RESPONSE"
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"
    CONTACT_LATER = "CONTACT_LATER"
    AWAITING_STRATEGY_OUTPUT = "AWAITING_STRATEGY_OUTPUT"
    STRATEGY_READY = "STRATEGY_READY"
    STRATEGY_DELIVERED = "STRATEGY_DELIVERED"
    ACTIVE_CLIENT = "ACTIVE_CLIENT"
    NO_RESPONSE_HUMAN_REVIEW = "NO_RESPONSE_HUMAN_REVIEW"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    ESCALATED_TO_HUMAN = "ESCALATED_TO_HUMAN"
    CLOSED_LOST = "CLOSED_LOST"
    WAIT = "WAIT"


class ActionType(str, Enum):
    """The next action selected by the decision node."""

    SEND_INITIAL_OUTREACH = "SEND_INITIAL_OUTREACH"
    WAIT_FOR_RESPONSE = "WAIT_FOR_RESPONSE"
    SEND_FOLLOW_UP = "SEND_FOLLOW_UP"
    PROCESS_INTEREST = "PROCESS_INTEREST"
    PROCESS_NOT_INTERESTED = "PROCESS_NOT_INTERESTED"
    CREATE_STRATEGY_REQUEST = "CREATE_STRATEGY_REQUEST"
    WAIT_FOR_STRATEGY = "WAIT_FOR_STRATEGY"
    NOTIFY_STRATEGY_READY = "NOTIFY_STRATEGY_READY"
    SEND_CLIENT_CHECK_IN = "SEND_CLIENT_CHECK_IN"
    REQUEST_FEEDBACK = "REQUEST_FEEDBACK"
    REVIEW_CLIENT_ISSUE = "REVIEW_CLIENT_ISSUE"
    CONTACT_LATER = "CONTACT_LATER"
    ESCALATE_TO_HUMAN = "ESCALATE_TO_HUMAN"
    DO_NOT_CONTACT = "DO_NOT_CONTACT"
    WAIT = "WAIT"


class WorkflowTrigger(str, Enum):
    """Events that may begin or resume a LangGraph run."""

    START_OUTREACH = "START_OUTREACH"
    APPROVAL_SUBMITTED = "APPROVAL_SUBMITTED"
    BUTTON_CLICKED = "BUTTON_CLICKED"
    FOLLOW_UP_DUE = "FOLLOW_UP_DUE"
    CLIENT_MESSAGE_RECEIVED = "CLIENT_MESSAGE_RECEIVED"
    STRATEGY_HANDOFF_RECEIVED = "STRATEGY_HANDOFF_RECEIVED"
    SCHEDULED_CLIENT_CHECK = "SCHEDULED_CLIENT_CHECK"


class WorkflowPhase(str, Enum):
    """Graph-control phase, kept separate from relationship lifecycle state."""

    OBSERVE = "OBSERVE"
    DECIDE = "DECIDE"
    DRAFT = "DRAFT"
    PENDING_HUMAN_APPROVAL = "PENDING_HUMAN_APPROVAL"
    EXECUTE_OUTBOUND = "EXECUTE_OUTBOUND"
    WAIT_FOR_EVENT = "WAIT_FOR_EVENT"
    PROCESS_EVENT = "PROCESS_EVENT"
    WAIT_FOR_STRATEGY = "WAIT_FOR_STRATEGY"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


class QualificationStatus(str, Enum):
    QUALIFIED = "QUALIFIED"
    NOT_QUALIFIED = "NOT_QUALIFIED"
    UNKNOWN = "UNKNOWN"


class OutboundMessageType(str, Enum):
    INITIAL_OUTREACH = "INITIAL_OUTREACH"
    NO_RESPONSE_FOLLOW_UP = "NO_RESPONSE_FOLLOW_UP"
    STRATEGY_READY_NOTIFICATION = "STRATEGY_READY_NOTIFICATION"
    CLIENT_CHECK_IN = "CLIENT_CHECK_IN"
    FEEDBACK_REQUEST = "FEEDBACK_REQUEST"
    CLIENT_ISSUE_RESPONSE = "CLIENT_ISSUE_RESPONSE"


class EmailStatus(str, Enum):
    """Truthful email status values.

    ``SENT`` means the configured provider accepted the exact approved message.
    ``DELIVERED`` may only be set by a provider event or an equivalent verified
    provider lookup; it is never inferred from a successful API request.
    """

    GENERATED = "GENERATED"
    PENDING_APPROVAL = "PENDING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENDING = "SENDING"
    SENT = "SENT"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class ButtonAction(str, Enum):
    INTERESTED = "INTERESTED"
    NOT_INTERESTED = "NOT_INTERESTED"


class ButtonEventStatus(str, Enum):
    RECEIVED = "RECEIVED"
    APPLIED = "APPLIED"
    DUPLICATE = "DUPLICATE"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"
    CONFLICT = "CONFLICT"


class StrategyRequestStatus(str, Enum):
    CREATED = "CREATED"
    HANDED_OFF = "HANDED_OFF"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    READY = "READY"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class StrategyOutputStatus(str, Enum):
    PENDING = "PENDING"
    READY = "READY"
    FAILED = "FAILED"
    REJECTED = "REJECTED"


class IssueSeverity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    SENSITIVE = "SENSITIVE"


class EscalationStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED_BY_HUMAN = "RESOLVED_BY_HUMAN"


class EscalationReason(str, Enum):
    REFUND_OR_FINANCIAL_LOSS = "REFUND_OR_FINANCIAL_LOSS"
    LEGAL_OR_COMPLIANCE = "LEGAL_OR_COMPLIANCE"
    SAFETY = "SAFETY"
    UNSUPPORTED_COMMITMENT = "UNSUPPORTED_COMMITMENT"
    MAX_NO_RESPONSE_ATTEMPTS = "MAX_NO_RESPONSE_ATTEMPTS"
    OTHER = "OTHER"


# ---------------------------------------------------------------------------
# Handoffs from Research and Qualification
# ---------------------------------------------------------------------------


class ContractModel(BaseModel):
    """Strict, JSON-serializable base model for this agent's own contracts."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class RestaurantContext(ContractModel):
    """Restaurant identity and contact data shared between team agents."""

    restaurant_id: int | str
    name: str
    email: str | None = None
    instagram_username: str | None = None
    instagram_url: str | None = None
    location: str | None = None
    category: str | None = None
    language_preference: Literal["ar", "en"] | None = None

    @field_validator("restaurant_id")
    @classmethod
    def require_restaurant_id(cls, value: int | str) -> int | str:
        if isinstance(value, str) and not value.strip():
            raise ValueError("restaurant_id cannot be blank")
        return value

    @field_validator("name")
    @classmethod
    def require_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("Restaurant name is required")
        return value

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        value = value.strip().lower()
        if "@" not in value or value.startswith("@") or value.endswith("@"):
            raise ValueError("email must be a valid-looking email address")
        return value


class HandoffProvenance(ContractModel):
    """Immutable linkage to the exact upstream runs used by an outreach run."""

    restaurant_id: int | str
    research_run_id: int | str
    qualification_run_id: int | str
    received_at: str = Field(default_factory=utc_now)


class ResearchSignal(ContractModel):
    signal_id: str
    dimension: str
    observation: str
    metric_path: str | None = None
    value: Any = None
    evidence_content_ids: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0, le=1)


class ResearchHandoff(ContractModel):
    """Normalized view of a full ``ResearchRun.full_result`` payload."""

    restaurant: RestaurantContext
    research_run_id: int | str
    analysis_status: str
    generated_at: str | None = None
    profile: dict[str, Any] = Field(default_factory=dict)
    profile_analysis: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    research_signals: list[ResearchSignal] = Field(default_factory=list)
    analysis_coverage: dict[str, Any] = Field(default_factory=dict)
    data_quality: dict[str, Any] = Field(default_factory=dict)

    @property
    def can_support_personalization(self) -> bool:
        """Only complete/partial research may be used, and only with observations."""

        return self.analysis_status.lower() in {"complete", "completed", "partial"} and bool(
            self.research_signals
        )

    def customer_safe_observations(self, limit: int = 3) -> list[str]:
        """Return public observations, never raw captions, notes, or internal scores."""

        observations: list[str] = []
        for signal in self.research_signals:
            observation = signal.observation.strip()
            if observation and observation not in observations:
                observations.append(observation)
        return observations[:limit]


class CustomerSafeContext(ContractModel):
    """The only context allowed to enter a customer-facing prompt or draft.

    Qualification rationale, raw tool output, and hidden agent notes are
    intentionally excluded.  The context builder in ``tools.py`` will create
    this model from vetted research facts and genuinely returned calendar data.
    """

    restaurant: RestaurantContext
    observation_ids: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)
    calendar_event_ids: list[str] = Field(default_factory=list)
    calendar_context: list[str] = Field(default_factory=list)
    latest_customer_message: str | None = None


class MarketingGap(ContractModel):
    """Internal Qualification output.  It must not be copied directly into email."""

    gap: str
    severity: str | None = None
    priority: int | None = Field(default=None, ge=0)
    evidence: list[str] = Field(default_factory=list)
    recommendation_focus: str | None = None


class QualificationHandoff(ContractModel):
    """Eligibility and internal marketing context from the Qualification Agent."""

    restaurant_id: int | str
    qualification_run_id: int | str
    research_run_id: int | str
    raw_qualification: str
    status: QualificationStatus
    decision_rationale: str | None = None
    marketing_gaps: list[MarketingGap] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    data_limitations: list[str] = Field(default_factory=list)

    @property
    def is_qualified(self) -> bool:
        return self.status is QualificationStatus.QUALIFIED

    @classmethod
    def from_team_result(
        cls,
        result: dict[str, Any],
        *,
        qualification_run_id: int | str,
        research_run_id: int | str,
        restaurant_id: int | str,
    ) -> "QualificationHandoff":
        """Adapt the current team's persisted QualificationRun.full_result safely."""

        raw = str(result.get("qualification") or "").strip()
        return cls(
            restaurant_id=restaurant_id,
            qualification_run_id=qualification_run_id,
            research_run_id=research_run_id,
            raw_qualification=raw,
            status=normalize_qualification_status(raw),
            decision_rationale=result.get("decision_rationale"),
            # The Qualification Agent may add fields (description, status, confidence...); this internal
            # contract keeps only the ones it declares, so they never reach the email prompt.
            marketing_gaps=[
                MarketingGap.model_validate({key: value for key, value in item.items() if key in MarketingGap.model_fields})
                for item in result.get("marketing_gaps", [])
            ],
            strengths=[str(item) for item in result.get("strengths", []) if str(item).strip()],
            data_limitations=[str(item) for item in result.get("data_limitations", []) if str(item).strip()],
        )


# ---------------------------------------------------------------------------
# Calendar and relationship memory
# ---------------------------------------------------------------------------


class CalendarEvent(ContractModel):
    """A fact returned by the real Rawaj Events Google Calendar integration."""

    event_id: str
    title: str
    start_at: str
    end_at: str | None = None
    calendar_id: str
    description: str | None = None
    source_url: str | None = None
    relevance_reason: str | None = None


class ClientFeedback(ContractModel):
    feedback_id: str = Field(default_factory=lambda: new_id("feedback"))
    message: str
    received_at: str = Field(default_factory=utc_now)
    source_message_id: str | None = None


class ClientIssue(ContractModel):
    issue_id: str = Field(default_factory=lambda: new_id("issue"))
    message: str
    severity: IssueSeverity = IssueSeverity.MEDIUM
    received_at: str = Field(default_factory=utc_now)
    source_message_id: str | None = None
    escalation_id: str | None = None


class HumanEscalation(ContractModel):
    escalation_id: str = Field(default_factory=lambda: new_id("escalation"))
    restaurant_id: int | str
    reason: str
    reason_code: EscalationReason = EscalationReason.OTHER
    severity: IssueSeverity
    status: EscalationStatus = EscalationStatus.OPEN
    related_issue_id: str | None = None
    created_at: str = Field(default_factory=utc_now)


class RelationshipMemory(ContractModel):
    """Small durable memory for communication decisions, not a complete CRM."""

    relationship_id: str = Field(default_factory=lambda: new_id("relationship"))
    restaurant_id: int | str
    status: RelationshipStatus = RelationshipStatus.READY_TO_CONTACT
    outreach_attempts: int = Field(default=0, ge=0)
    last_outbound_message_id: str | None = None
    last_outbound_at: str | None = None
    last_inbound_event_id: str | None = None
    last_inbound_at: str | None = None
    next_contact_at: str | None = None
    contact_later_until: str | None = None
    interest_event_id: str | None = None
    strategy_request_id: str | None = None
    strategy_id: int | str | None = None
    strategy_status: StrategyOutputStatus | None = None
    feedback: list[ClientFeedback] = Field(default_factory=list)
    issues: list[ClientIssue] = Field(default_factory=list)
    important_context: list[str] = Field(default_factory=list)
    do_not_contact_at: str | None = None
    version: int = Field(default=1, ge=1)
    updated_at: str = Field(default_factory=utc_now)

    @property
    def promotional_contact_allowed(self) -> bool:
        return self.status not in {
            RelationshipStatus.DO_NOT_CONTACT,
            RelationshipStatus.CLOSED_LOST,
        } and self.do_not_contact_at is None


# ---------------------------------------------------------------------------
# Human-reviewed email and button contracts
# ---------------------------------------------------------------------------


class EmailButton(ContractModel):
    """A signed link is added by the email service after message persistence."""

    action: ButtonAction
    label: str
    url: str

    @field_validator("url")
    @classmethod
    def require_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("A response button requires a signed URL")
        return value


class EmailDraft(ContractModel):
    """The exact customer-facing draft presented to the human reviewer."""

    message_id: str = Field(default_factory=lambda: new_id("outbound"))
    revision: int = Field(default=1, ge=1)
    relationship_id: str
    restaurant_id: int | str
    message_type: OutboundMessageType
    action: ActionType
    recipient: str
    subject: str
    html_body: str
    plain_text_body: str
    language: Literal["ar", "en"] = "en"
    buttons: list[EmailButton] = Field(default_factory=list)
    personalization_observation_ids: list[str] = Field(default_factory=list)
    personalization_context: list[str] = Field(default_factory=list)
    status: EmailStatus = EmailStatus.GENERATED

    @model_validator(mode="after")
    def validate_prospect_response_buttons(self) -> "EmailDraft":
        response_message_types = {
            OutboundMessageType.INITIAL_OUTREACH,
            OutboundMessageType.NO_RESPONSE_FOLLOW_UP,
        }
        if self.message_type in response_message_types:
            actions = {button.action for button in self.buttons}
            if (
                len(self.buttons) != 2
                or actions
                != {ButtonAction.INTERESTED, ButtonAction.NOT_INTERESTED}
            ):
                raise ValueError(
                    "Prospect outreach must include exactly one Interested and one "
                    "Not Interested button"
                )
        return self


class EmailContentProposal(ContractModel):
    """LLM output before trusted rendering adds HTML, buttons, and signature."""

    subject: str = Field(min_length=1, max_length=500)
    plain_text_body: str = Field(min_length=1, max_length=12_000)
    language: Literal["ar", "en"] = "en"
    personalization_observation_ids: list[str] = Field(default_factory=list)


class MessageReview(ContractModel):
    """Validation result; stores concise findings rather than chain-of-thought."""

    passed: bool
    issues: list[str] = Field(default_factory=list)
    approved_fact_references: list[str] = Field(default_factory=list)
    privacy_safe: bool
    reviewed_at: str = Field(default_factory=utc_now)


class HumanApproval(ContractModel):
    """Approval is bound to the exact message revision and may expire."""

    approval_id: str = Field(default_factory=lambda: new_id("approval"))
    message_id: str
    message_revision: int
    content_sha256: str
    status: ApprovalStatus
    reviewer_id: str
    reviewed_at: str = Field(default_factory=utc_now)
    expires_at: str | None = None
    note: str | None = None

    @property
    def is_sendable(self) -> bool:
        return self.status is ApprovalStatus.APPROVED


class OutboundExecution(ContractModel):
    """A provider outcome.  This is the only source allowed to set ``SENT``."""

    execution_id: str = Field(default_factory=lambda: new_id("send"))
    message_id: str
    provider_name: str
    success: bool
    provider_message_id: str | None = None
    provider_status: str | None = None
    email_status: EmailStatus
    detail: str | None = None
    executed_at: str = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def enforce_truthful_send_status(self) -> "OutboundExecution":
        if self.success and self.email_status not in {EmailStatus.SENT, EmailStatus.DELIVERED}:
            raise ValueError("A successful provider submission must be recorded as SENT")
        if not self.success and self.email_status is not EmailStatus.FAILED:
            raise ValueError("A failed provider submission must be recorded as FAILED")
        if self.email_status in {EmailStatus.SENT, EmailStatus.DELIVERED} and not self.provider_message_id:
            raise ValueError("SENT or DELIVERED requires a provider message ID")
        return self


class SignedButtonPayload(ContractModel):
    """The verified contents of a short-lived, signed button token."""

    token_id: str
    restaurant_id: int | str
    outreach_message_id: str
    action: ButtonAction
    issued_at: str
    expires_at: str


class ButtonClickEvent(ContractModel):
    """An already-verified public button response event.

    The response endpoint, not the browser, supplies these values after
    signature verification and idempotency checks.
    """

    event_id: str = Field(default_factory=lambda: new_id("button"))
    restaurant_id: int | str
    outreach_message_id: str
    action: ButtonAction
    clicked_at: str = Field(default_factory=utc_now)
    token_id: str
    status: ButtonEventStatus = ButtonEventStatus.RECEIVED
    idempotency_key: str = Field(default_factory=lambda: new_id("button_key"))
    source_ip_hash: str | None = None
    # Set by the repository once the event was applied; the canonical record carries it.
    processed_at: str | None = None


# ---------------------------------------------------------------------------
# Strategy Agent handoffs
# ---------------------------------------------------------------------------


class StrategyRequestHandoff(ContractModel):
    """Internal event produced only after a verified Interested click."""

    handoff_type: Literal["STRATEGY_REQUEST"] = "STRATEGY_REQUEST"
    strategy_request_id: str = Field(default_factory=lambda: new_id("strategy_request"))
    restaurant_id: int | str
    research_run_id: int | str
    qualification_run_id: int | str
    outreach_message_id: str
    interest_event_id: str
    strategy_start_date: str | None = None
    customer_request: str = "Restaurant expressed interest in Rawaj."
    research_context: dict[str, Any]
    qualification_context: dict[str, Any]
    customer_context: dict[str, Any] = Field(default_factory=dict)
    status: StrategyRequestStatus = StrategyRequestStatus.CREATED
    created_at: str = Field(default_factory=utc_now)


class StrategyOutputHandoff(ContractModel):
    """Minimal data this agent accepts from the teammate-owned Strategy Agent."""

    handoff_type: Literal["STRATEGY_OUTPUT"] = "STRATEGY_OUTPUT"
    strategy_id: int | str
    strategy_request_id: str
    restaurant_id: int | str
    status: StrategyOutputStatus
    dashboard_strategy_url: str | None = None
    client_notification_allowed: bool = False
    client_safe_summary: str | None = None
    client_safe_deliverables: list[str] = Field(default_factory=list)
    completed_at: str | None = None

    def validation_errors_for(self, request: StrategyRequestHandoff) -> list[str]:
        # Lazy import avoids a schema/guardrail import cycle.
        from .guardrails import strategy_errors
        return strategy_errors(self, request)


# ---------------------------------------------------------------------------
# Decisions, graph input, and graph state
# ---------------------------------------------------------------------------


class OutreachDecision(ContractModel):
    """Structured LLM decision.  ``decision_basis`` is concise, not hidden reasoning."""

    action: ActionType
    next_status: RelationshipStatus
    decision_basis: list[str] = Field(default_factory=list)
    language: Literal["ar", "en"] = "en"
    read_calendar: bool = False
    selected_calendar_event_ids: list[str] = Field(default_factory=list)
    personalization_observation_ids: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False
    requires_escalation: bool = False
    next_contact_at: str | None = None


class OutreachWorkflowRequest(ContractModel):
    """Validated request passed into the public Agent entrypoint."""

    trigger: WorkflowTrigger
    provenance: HandoffProvenance
    restaurant: RestaurantContext
    research: ResearchHandoff
    qualification: QualificationHandoff
    relationship_memory: RelationshipMemory
    button_event: ButtonClickEvent | None = None
    strategy_output: StrategyOutputHandoff | None = None
    client_message: str | None = None
    thread_id: str = Field(default_factory=lambda: new_id("thread"))

    @model_validator(mode="after")
    def validate_shared_provenance(self) -> "OutreachWorkflowRequest":
        ids = {
            str(self.provenance.restaurant_id),
            str(self.restaurant.restaurant_id),
            str(self.research.restaurant.restaurant_id),
            str(self.qualification.restaurant_id),
            str(self.relationship_memory.restaurant_id),
        }
        if len(ids) != 1:
            raise ValueError("All handoffs must refer to the same restaurant_id")
        if str(self.provenance.research_run_id) != str(self.qualification.research_run_id):
            raise ValueError("Qualification must reference the supplied research_run_id")
        if str(self.provenance.qualification_run_id) != str(self.qualification.qualification_run_id):
            raise ValueError("Qualification run ID does not match provenance")
        return self


class OutreachGraphState(TypedDict, total=False):
    """JSON-friendly LangGraph state.

    Pydantic models are converted with ``model_dump(mode='json')`` before they
    enter this state so SQLite checkpointing can persist a run safely.
    """

    draft_attempt: int
    revision_feedback: str
    rejected_draft: str
    rejected_message_id: str
    thread_id: str
    trigger: str
    provenance: dict[str, Any]
    restaurant: dict[str, Any]
    research: dict[str, Any]
    qualification: dict[str, Any]
    relationship_memory: dict[str, Any]
    workflow_phase: str
    customer_safe_context: dict[str, Any]
    calendar_events: list[dict[str, Any]]
    calendar_checked: bool
    decision: dict[str, Any]
    email_draft: dict[str, Any]
    review: dict[str, Any]
    message_record: dict[str, Any]
    approval: dict[str, Any]
    approval_request: dict[str, Any]
    execution: dict[str, Any]
    button_event: dict[str, Any]
    strategy_request: dict[str, Any]
    strategy_output: dict[str, Any]
    strategy_context: dict[str, Any]
    escalation: dict[str, Any]
    client_message: str
    next_node: str
    events: Annotated[list[dict[str, Any]], add]
    errors: Annotated[list[str], add]


class OutreachWorkflowResult(ContractModel):
    """Serializable result returned after a graph completes or interrupts."""

    thread_id: str
    relationship_memory: RelationshipMemory
    decision: OutreachDecision | None = None
    email_draft: EmailDraft | None = None
    review: MessageReview | None = None
    approval: HumanApproval | None = None
    execution: OutboundExecution | None = None
    strategy_request: StrategyRequestHandoff | None = None
    escalation: HumanEscalation | None = None
    errors: list[str] = Field(default_factory=list)


__all__ = [
    "ActionType",
    "ApprovalStatus",
    "ButtonAction",
    "ButtonClickEvent",
    "ButtonEventStatus",
    "CalendarEvent",
    "CustomerSafeContext",
    "ClientFeedback",
    "ClientIssue",
    "EmailButton",
    "EmailContentProposal",
    "EmailDraft",
    "EmailStatus",
    "EscalationStatus",
    "EscalationReason",
    "HandoffProvenance",
    "HumanApproval",
    "HumanEscalation",
    "IssueSeverity",
    "MarketingGap",
    "MessageReview",
    "OutboundExecution",
    "OutboundMessageType",
    "OutreachDecision",
    "OutreachGraphState",
    "OutreachWorkflowRequest",
    "OutreachWorkflowResult",
    "QualificationHandoff",
    "QualificationStatus",
    "RelationshipMemory",
    "RelationshipStatus",
    "ResearchHandoff",
    "ResearchSignal",
    "RestaurantContext",
    "SignedButtonPayload",
    "StrategyOutputHandoff",
    "StrategyOutputStatus",
    "StrategyRequestHandoff",
    "StrategyRequestStatus",
    "WorkflowTrigger",
    "WorkflowPhase",
    "new_id",
    "normalize_qualification_status",
    "utc_now",
]
