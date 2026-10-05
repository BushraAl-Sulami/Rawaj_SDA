"""LangGraph workflow for Rawaj's Outreach & Follow-Up Agent.

This is the agent's execution core.  It deliberately keeps decisions, email
generation, review, approval, provider execution, memory, and handoffs inside
one stateful graph instead of treating the LLM as a message generator.

The workflow owns only Outreach, Follow-Up, and Client Relationship work.  It
does not qualify prospects, research restaurants, or create marketing
strategies.  Those boundaries are represented by strict handoffs.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import urlparse

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Overwrite, interrupt

try:  # Supports package imports and direct notebook imports.
    from .config import RawajSettings
    from .schemas import (
        ActionType,
        ApprovalStatus,
        ButtonAction,
        ButtonClickEvent,
        ButtonEventStatus,
        CalendarEvent,
        ClientFeedback,
        ClientIssue,
        CustomerSafeContext,
        EmailContentProposal,
        EmailDraft,
        EmailStatus,
        EscalationReason,
        HumanApproval,
        HumanEscalation,
        IssueSeverity,
        MessageReview,
        OutboundExecution,
        OutboundMessageType,
        OutreachDecision,
        OutreachGraphState,
        OutreachWorkflowRequest,
        OutreachWorkflowResult,
        QualificationHandoff,
        QualificationStatus,
        RelationshipMemory,
        RelationshipStatus,
        ResearchHandoff,
        RestaurantContext,
        StrategyOutputHandoff,
        StrategyRequestHandoff,
        StrategyRequestStatus,
        WorkflowPhase,
        WorkflowTrigger,
        utc_now,
    )
    from .tools import (
        OutreachToolServices,
        configure_tool_services,
        create_human_escalation,
        create_strategy_request_handoff,
        get_relevant_calendar_events,
        load_qualification_handoff,
        load_research_handoff,
        load_strategy_handoff,
        read_relationship_memory,
        record_outbound_execution,
        send_approved_email,
        validate_strategy_handoff,
        write_relationship_memory,
    )
except ImportError:  # pragma: no cover - used by a direct notebook import.
    from config import RawajSettings
    from schemas import (
        ActionType,
        ApprovalStatus,
        ButtonAction,
        ButtonClickEvent,
        ButtonEventStatus,
        CalendarEvent,
        ClientFeedback,
        ClientIssue,
        CustomerSafeContext,
        EmailContentProposal,
        EmailDraft,
        EmailStatus,
        EscalationReason,
        HumanApproval,
        HumanEscalation,
        IssueSeverity,
        MessageReview,
        OutboundExecution,
        OutboundMessageType,
        OutreachDecision,
        OutreachGraphState,
        OutreachWorkflowRequest,
        OutreachWorkflowResult,
        QualificationHandoff,
        QualificationStatus,
        RelationshipMemory,
        RelationshipStatus,
        ResearchHandoff,
        RestaurantContext,
        StrategyOutputHandoff,
        StrategyRequestHandoff,
        StrategyRequestStatus,
        WorkflowPhase,
        WorkflowTrigger,
        utc_now,
    )
    from tools import (
        OutreachToolServices,
        configure_tool_services,
        create_human_escalation,
        create_strategy_request_handoff,
        get_relevant_calendar_events,
        load_qualification_handoff,
        load_research_handoff,
        load_strategy_handoff,
        read_relationship_memory,
        record_outbound_execution,
        send_approved_email,
        validate_strategy_handoff,
        write_relationship_memory,
    )


from .guardrails import (OutreachGuardrails, WorkflowSafetyError, SecretProtector,
    model_context, validate_proposal, validate_provenance, protect_logging)



def create_sqlite_checkpointer(path: str | Path) -> Any:
    """Create the durable checkpointer required for approval interruption.

    The relationship itself is persisted by the repository.  This separate
    SQLite checkpointer preserves the exact paused graph state so an approved
    email can resume the same immutable draft rather than regenerate one.
    ``langgraph-checkpoint-sqlite`` is an explicit project dependency.
    """

    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as error:  # pragma: no cover - dependency setup issue.
        raise WorkflowSafetyError(
            "langgraph-checkpoint-sqlite is required for durable approval resume."
        ) from error

    checkpoint_path = Path(path)
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(str(checkpoint_path), check_same_thread=False)
    return SqliteSaver(connection)


def _stable_id(prefix: str, *parts: object) -> str:
    """Produce a retry-stable opaque ID without exposing customer data."""

    material = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}_{digest}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _safe_int(value: int | str, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise WorkflowSafetyError(
            f"{field_name} must be an integer for the team's SQLite handoff."
        ) from error


class OutreachFollowUpWorkflow(OutreachGuardrails):
    """The one stateful Outreach & Follow-Up Agent used by the Rawaj pipeline.

    Every external dependency is injected.  This keeps the component easy to
    connect to the teammates' real database and future Strategy Agent while
    allowing deterministic test doubles only under ``RAWAJ_ENV=test``.
    """

    _SENSITIVE_ISSUE = re.compile(
        r"\b(refund|refunds|financial loss|lost\s+\d+%|lost sales|lawsuit|"
        r"lawyer|legal|contract|liability|fraud|threat|safety|injury|damage)\b"
        r"|ط§ط³طھط±ط¬ط§ط¹|طھط¹ظˆظٹط¶|ط®ط³ط§ط±ط©|ظ…ط¨ظٹط¹ط§طھ|ظ‚ط§ظ†ظˆظ†|ظ…ط­ط§ظ…ظٹ|ط¹ظ‚ط¯|ظ…ط³ط¤ظˆظ„ظٹط©|ط§ط­طھظٹط§ظ„|طھظ‡ط¯ظٹط¯|ط³ظ„ط§ظ…ط©",
        re.IGNORECASE,
    )
    _ISSUE_WORDS = re.compile(
        r"\b(low|lower|problem|issue|complaint|not working|poor|drop|engagement|"
        r"results?)\b|ظ…ط´ظƒظ„ط©|ط¶ط¹ظٹظپ|ط§ظ†ط®ظپط§ط¶|ط´ظƒظˆظ‰|ظ†طھط§ط¦ط¬",
        re.IGNORECASE,
    )
    _URL_IN_MODEL_TEXT = re.compile(r"https?://", re.IGNORECASE)
    # Everything after this line in a strategy-ready email is written by trusted code, not by the model:
    # the dashboard link, the client's sign-in details and the privacy note.
    _TRUSTED_FOOTER_MARKER = "\n\nOpen your strategy here:"

    _OUTBOUND_ACTIONS = {
        ActionType.SEND_INITIAL_OUTREACH,
        ActionType.SEND_FOLLOW_UP,
        ActionType.NOTIFY_STRATEGY_READY,
        ActionType.SEND_CLIENT_CHECK_IN,
        ActionType.REQUEST_FEEDBACK,
        ActionType.REVIEW_CLIENT_ISSUE,
    }

    def __init__(
        self,
        *,
        settings: RawajSettings,
        repository: Any,
        llm: Any,
        email_service: Any,
        calendar_service: Any | None = None,
        strategy_dispatcher: Any | None = None,
        checkpointer: Any | None = None,
        now_provider: Callable[[], datetime] | None = None,
        access_provider: Callable[[int], dict[str, Any] | None] | None = None,
    ) -> None:
        # Creates the client's dashboard sign-in for the "strategy ready" email. Called only by trusted
        # code after generation, so the model never sees the username or password.
        self.access_provider = access_provider
        self.secrets = SecretProtector()
        protect_logging(self.secrets)
        self.settings = settings
        self.repository = repository
        self.llm = llm
        self.email_service = email_service
        self.calendar_service = calendar_service
        self.strategy_dispatcher = strategy_dispatcher
        self._now = now_provider or _utcnow

        # The visible @tool functions are genuinely used by graph nodes.  The
        # repository remains injected for narrow, transactional persistence
        # operations that must not be LLM-directed (approval, button state,
        # message immutability, and handoff storage).
        configure_tool_services(
            OutreachToolServices(
                repository=repository,
                calendar=calendar_service,
                email=email_service,
                strategy_dispatcher=strategy_dispatcher,
            )
        )

        if checkpointer is None:
            checkpointer = self._default_checkpointer()
        self.checkpointer = checkpointer
        self.graph = self._build_graph(checkpointer)

    # ------------------------------------------------------------------
    # Public entrypoints used by the team workflow, scheduler, and API
    # ------------------------------------------------------------------

    def start_outreach(
        self,
        *,
        restaurant_id: int | str,
        research_run_id: int | str,
        qualification_run_id: int | str,
        thread_id: str | None = None,
    ) -> OutreachWorkflowResult:
        """Start initial outreach from exact upstream Research/Qualification runs."""

        return self._invoke_new_run(
            trigger=WorkflowTrigger.START_OUTREACH,
            restaurant_id=restaurant_id,
            research_run_id=research_run_id,
            qualification_run_id=qualification_run_id,
            thread_id=thread_id
            or _stable_id(
                "outreach_start", restaurant_id, research_run_id, qualification_run_id
            ),
        )

    def initial_draft_review_state(self, *, restaurant_id, research_run_id, qualification_run_id,
                                   message_id, revision, content_sha256):
        """Inspect the exact saved draft, not a leftover approval from an older revision."""
        thread_id = _stable_id("outreach_start", restaurant_id, research_run_id, qualification_run_id)
        saved = self.graph.get_state(self._graph_config(thread_id))
        state = saved.values
        draft, record = state.get("email_draft") or {}, state.get("message_record") or {}
        matches = (draft.get("message_id") == message_id and draft.get("revision") == revision
                   and record.get("content_sha256") == content_sha256
                   and draft.get("message_type") == "INITIAL_OUTREACH")
        request = state.get("approval_request") or {}
        expiry = _as_utc(request.get("expires_at"))
        expired = bool(matches and request.get("message_id") == message_id and expiry and self._now() > expiry)
        review_failed = bool(matches and state.get("workflow_phase") == WorkflowPhase.FAILED.value
                             and state.get("next_node") == "finish" and state.get("errors"))
        return {"thread_id": thread_id, "expired": expired,
                "recoverable": bool(matches and (expired or review_failed)
                    and state.get("customer_safe_context") and not (state.get("execution") or {}).get("success"))}

    def retry_initial_draft_review(self, **binding):
        """Review the saved content again, then pause for a NEW human decision; never send."""
        info = self.initial_draft_review_state(**binding)
        if not info["recoverable"]:
            raise WorkflowSafetyError("This draft is no longer eligible for review recovery. Refresh.")
        config = self._graph_config(info["thread_id"])
        self._bind_tools()
        memory = self._memory_from_payload(self.repository.read_relationship_memory(restaurant_id=binding["restaurant_id"]))
        if not memory.promotional_contact_allowed or memory.outreach_attempts or memory.status not in {
            RelationshipStatus.WAIT, RelationshipStatus.READY_TO_CONTACT, RelationshipStatus.PENDING_OUTBOUND_APPROVAL,
        }:
            raise WorkflowSafetyError("The relationship is not waiting for an initial email.")
        self.graph.update_state(config, {"errors": Overwrite([]), "approval": None,
            "approval_request": None, "execution": None, "review": None,
            "relationship_memory": memory.model_dump(mode="json"),
            "workflow_phase": WorkflowPhase.DRAFT.value, "next_node": "review_email"}, as_node="prepare_and_persist_email")
        result = self._guarded_graph_invoke(None, config)
        return self._result_from_state(info["thread_id"], result)

    def revise_recipient(self, *, restaurant_id, research_run_id, qualification_run_id, message_id):
        """Replace an unsent initial draft; never reuse its approval or send here."""
        thread_id = _stable_id("outreach_start", restaurant_id, research_run_id, qualification_run_id)
        config = self._graph_config(thread_id)
        saved = self.graph.get_state(config)
        state = saved.values
        draft = state.get("email_draft") or {}
        if draft.get("message_id") != message_id or (state.get("execution") or {}).get("success"):
            raise WorkflowSafetyError("The draft changed or was already sent. Refresh before retrying.")
        if draft.get("message_type") != "INITIAL_OUTREACH":
            raise WorkflowSafetyError("Only an initial draft can be replaced here.")
        self._bind_tools()
        self.graph.update_state(config, {
            "errors": Overwrite([]), "execution": None, "approval": None,
            "approval_request": None, "rejected_draft": draft, "rejected_message_id": message_id,
            "revision_feedback": "The contact email changed. Prepare a new initial draft for human review.",
            "draft_attempt": int(state.get("draft_attempt") or 0) + 1,
            "next_node": "generate_email",
        }, as_node="build_customer_safe_context")
        result = self._guarded_graph_invoke(None, config)
        return self._result_from_state(thread_id, result)

    def run_follow_up_due(
        self,
        *,
        restaurant_id: int | str,
        research_run_id: int | str,
        qualification_run_id: int | str,
        thread_id: str | None = None,
    ) -> OutreachWorkflowResult:
        """Run a due prospect follow-up; timing is enforced inside the graph."""

        if thread_id is None:
            current_memory = self.repository.read_relationship_memory(
                restaurant_id=_safe_int(restaurant_id, "restaurant_id")
            )
            due_cycle = (
                current_memory.get("next_contact_at")
                if isinstance(current_memory, dict)
                else None
            )
            thread_id = _stable_id(
                "outreach_followup",
                restaurant_id,
                research_run_id,
                qualification_run_id,
                due_cycle or "no-due-cycle",
            )
        return self._invoke_new_run(
            trigger=WorkflowTrigger.FOLLOW_UP_DUE,
            restaurant_id=restaurant_id,
            research_run_id=research_run_id,
            qualification_run_id=qualification_run_id,
            thread_id=thread_id,
        )

    def run_scheduled_client_check(
        self,
        *,
        restaurant_id: int | str,
        research_run_id: int | str,
        qualification_run_id: int | str,
        thread_id: str | None = None,
    ) -> OutreachWorkflowResult:
        """Reassess whether a client-facing follow-up is genuinely useful.

        A due client check never creates a generic email merely because a week
        passed.  It reaches the decision node with relationship memory, client
        feedback/issues, and optional calendar context first.
        """

        if thread_id is None:
            current_memory = self.repository.read_relationship_memory(
                restaurant_id=_safe_int(restaurant_id, "restaurant_id")
            )
            due_cycle = (
                current_memory.get("next_contact_at")
                if isinstance(current_memory, dict)
                else None
            )
            thread_id = _stable_id(
                "outreach_client_check",
                restaurant_id,
                research_run_id,
                qualification_run_id,
                due_cycle or "no-due-cycle",
            )
        return self._invoke_new_run(
            trigger=WorkflowTrigger.SCHEDULED_CLIENT_CHECK,
            restaurant_id=restaurant_id,
            research_run_id=research_run_id,
            qualification_run_id=qualification_run_id,
            thread_id=thread_id,
        )

    def handle_client_message(
        self,
        *,
        restaurant_id: int | str,
        research_run_id: int | str,
        qualification_run_id: int | str,
        client_message: str,
        thread_id: str | None = None,
    ) -> OutreachWorkflowResult:
        """Process a real inbound client message without exposing hidden context."""

        text = str(client_message or "").strip()
        if not text:
            raise WorkflowSafetyError("client_message cannot be blank.")
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        return self._invoke_new_run(
            trigger=WorkflowTrigger.CLIENT_MESSAGE_RECEIVED,
            restaurant_id=restaurant_id,
            research_run_id=research_run_id,
            qualification_run_id=qualification_run_id,
            client_message=text,
            thread_id=thread_id
            or _stable_id("outreach_client_message", restaurant_id, digest),
        )

    def resume_from_button_event(
        self, event: ButtonClickEvent | dict[str, Any]
    ) -> OutreachWorkflowResult:
        """Resume only after the public endpoint durably verified a button click."""

        button_event = ButtonClickEvent.model_validate(event)
        memory_data = self.repository.read_relationship_memory(
            restaurant_id=_safe_int(button_event.restaurant_id, "restaurant_id")
        )
        if memory_data is None:
            raise WorkflowSafetyError(
                "A button event cannot resume because its relationship is missing."
            )
        research_run_id = memory_data.get("research_run_id")
        qualification_run_id = memory_data.get("qualification_run_id")
        if research_run_id is None or qualification_run_id is None:
            raise WorkflowSafetyError(
                "A button event cannot resume without its upstream provenance."
            )
        return self._invoke_new_run(
            trigger=WorkflowTrigger.BUTTON_CLICKED,
            restaurant_id=button_event.restaurant_id,
            research_run_id=research_run_id,
            qualification_run_id=qualification_run_id,
            button_event=button_event,
            thread_id=_stable_id("outreach_button", button_event.event_id),
        )

    def receive_strategy_output( 
        self, output: StrategyOutputHandoff | dict[str, Any]
    ) -> OutreachWorkflowResult:
        """Accept a supplied Strategy Agent output; never generate one locally."""

        strategy_output = StrategyOutputHandoff.model_validate(output)
        request = self.repository.load_strategy_request(
            strategy_request_id=strategy_output.strategy_request_id
        )
        if request is None:
            raise WorkflowSafetyError("Strategy output refers to an unknown request.")
        return self._invoke_new_run(
            trigger=WorkflowTrigger.STRATEGY_HANDOFF_RECEIVED,
            restaurant_id=strategy_output.restaurant_id,
            research_run_id=request["research_run_id"],
            qualification_run_id=request["qualification_run_id"],
            strategy_output=strategy_output,
            thread_id=_stable_id(
                "outreach_strategy_output",
                strategy_output.strategy_request_id,
                strategy_output.strategy_id,
            ),
        )

    def resume_human_approval(
        self,
        *,
        thread_id: str,
        reviewer_id: str,
        decision: Literal["APPROVED", "REJECTED"],
        note: str | None = None,
        message_id: str | None = None,
        message_revision: int | None = None,
        content_sha256: str | None = None,
    ) -> OutreachWorkflowResult:
        """Resume a paused graph with a reviewer decision bound to one draft.

        ``message_id``, ``message_revision``, and ``content_sha256`` should be
        passed from the interruption payload displayed to the reviewer.  They
        are optional only for the internal escalation approval path, which has
        no customer email.
        """

        reviewer = str(reviewer_id or "").strip()
        if not reviewer:
            raise WorkflowSafetyError("reviewer_id is required for Human Approval.")
        if decision == "REJECTED" and not str(note or "").strip():
            raise WorkflowSafetyError("Please provide a rejection reason so the draft can be rewritten.")
        if reviewer.startswith("auto:") or reviewer.startswith("system:"):
            raise WorkflowSafetyError("Initial outreach requires a human reviewer.")
        if decision not in {"APPROVED", "REJECTED"}:
            raise WorkflowSafetyError("decision must be APPROVED or REJECTED.")
        self._bind_tools()
        result = self._guarded_graph_invoke(
            Command(
                resume={
                    "decision": decision,
                    "reviewer_id": reviewer,
                    "note": str(note).strip() if note else None,
                    "message_id": message_id,
                    "message_revision": message_revision,
                    "content_sha256": content_sha256,
                }
            ),
            self._graph_config(thread_id),
        )
        return self._result_from_state(thread_id, result)

    def run_request(self, request: OutreachWorkflowRequest) -> OutreachWorkflowResult:
        """Run a prevalidated request from a notebook or future team adapter.

        The graph still hydrates canonical persisted handoffs through its tools;
        this method simply makes a typed integration boundary available when the
        teammate workflow already has validated data in memory.
        """

        return self._invoke_new_run(
            trigger=request.trigger,
            restaurant_id=request.provenance.restaurant_id,
            research_run_id=request.provenance.research_run_id,
            qualification_run_id=request.provenance.qualification_run_id,
            button_event=request.button_event,
            strategy_output=request.strategy_output,
            client_message=request.client_message,
            thread_id=request.thread_id,
        )

    # ------------------------------------------------------------------
    # Graph construction
    # ------------------------------------------------------------------

    def _bind_tools(self) -> None:
        configure_tool_services(OutreachToolServices(repository=self.repository,
            email=self.email_service, calendar=self.calendar_service,
            strategy_dispatcher=self.strategy_dispatcher))

    def _default_checkpointer(self) -> Any:
        """Prefer SQLite, preserving an offline memory-only mode for tests."""

        try:
            return create_sqlite_checkpointer(self.settings.langgraph_checkpoint_path)
        except WorkflowSafetyError:
            if self.settings.environment not in {"test", "testing", "development"}:
                raise
            try:
                from langgraph.checkpoint.memory import MemorySaver
            except ImportError as error:  # pragma: no cover - package install issue.
                raise WorkflowSafetyError(
                    "LangGraph checkpoint support is not installed."
                ) from error
            return MemorySaver()

    def _build_graph(self, checkpointer: Any) -> Any:
        builder = StateGraph(OutreachGraphState)
        builder.add_node("hydrate_context", self._hydrate_context)
        builder.add_node("hard_gate", self._hard_gate)
        builder.add_node("load_calendar", self._load_calendar)
        builder.add_node("decide_next_action", self._decide_next_action)
        builder.add_node("process_button_event", self._process_button_event)
        builder.add_node("process_strategy_output", self._process_strategy_output)
        builder.add_node("persist_wait", self._persist_wait)
        builder.add_node("prepare_escalation", self._prepare_escalation)
        builder.add_node("create_escalation", self._create_escalation)
        builder.add_node("build_customer_safe_context", self._build_customer_safe_context)
        builder.add_node("generate_email", self._generate_email)
        builder.add_node("prepare_and_persist_email", self._prepare_and_persist_email)
        builder.add_node("review_email", self._review_email)
        builder.add_node("human_approval", self._human_approval)
        builder.add_node("execute_approved_email", self._execute_approved_email)
        builder.add_node("persist_post_send", self._persist_post_send)
        builder.add_node("finish", self._finish)

        builder.add_edge(START, "hydrate_context")
        builder.add_edge("hydrate_context", "hard_gate")
        builder.add_conditional_edges("hard_gate", self._route, {
            "decide_next_action": "decide_next_action",
            "process_button_event": "process_button_event",
            "process_strategy_output": "process_strategy_output",
            "prepare_escalation": "prepare_escalation",
            "finish": "finish",
        })
        builder.add_conditional_edges("decide_next_action", self._route, {
            "load_calendar": "load_calendar",
            "build_customer_safe_context": "build_customer_safe_context",
            "persist_wait": "persist_wait",
            "prepare_escalation": "prepare_escalation",
            "finish": "finish",
        })
        builder.add_edge("load_calendar", "decide_next_action")
        builder.add_conditional_edges("process_button_event", self._route, {
            "persist_wait": "persist_wait",
            "finish": "finish",
        })
        builder.add_conditional_edges("process_strategy_output", self._route, {
            "build_customer_safe_context": "build_customer_safe_context",
            "finish": "finish",
        })
        builder.add_conditional_edges("prepare_escalation", self._route, {
            "create_escalation": "create_escalation",
            "finish": "finish",
        })
        builder.add_edge("create_escalation", "finish")
        builder.add_edge("persist_wait", "finish")
        builder.add_edge("build_customer_safe_context", "generate_email")
        builder.add_conditional_edges("generate_email", self._route, {
            "prepare_and_persist_email": "prepare_and_persist_email",
            "finish": "finish",
        })
        builder.add_conditional_edges("prepare_and_persist_email", self._route, {
            "review_email": "review_email",
            "finish": "finish",
        })
        builder.add_conditional_edges("review_email", self._route, {
            "human_approval": "human_approval",
            "execute_approved_email": "execute_approved_email",
            "finish": "finish",
        })
        builder.add_conditional_edges("human_approval", self._route, {
            "generate_email": "generate_email",
            "execute_approved_email": "execute_approved_email",
            "finish": "finish",
        })
        builder.add_conditional_edges("execute_approved_email", self._route, {
            "persist_post_send": "persist_post_send",
            "finish": "finish",
        })
        builder.add_edge("persist_post_send", "finish")
        builder.add_edge("finish", END)
        return builder.compile(checkpointer=checkpointer)

    # ------------------------------------------------------------------
    # Context loading and hard workflow gates
    # ------------------------------------------------------------------

    def _hydrate_context(self, state: OutreachGraphState) -> dict[str, Any]:
        """Use named @tool handoffs and load the durable relationship memory."""

        try:
            provenance = state["provenance"]
            restaurant_id = _safe_int(provenance["restaurant_id"], "restaurant_id")
            research_run_id = _safe_int(provenance["research_run_id"], "research_run_id")
            qualification_run_id = _safe_int(
                provenance["qualification_run_id"], "qualification_run_id"
            )

            research_payload = self._require_tool_data(
                load_research_handoff,
                {"restaurant_id": restaurant_id, "research_run_id": research_run_id},
            )
            research = self._research_from_team_payload(research_payload)
            qualification_payload = self._require_tool_data(
                load_qualification_handoff,
                {
                    "restaurant_id": restaurant_id,
                    "qualification_run_id": qualification_run_id,
                    "research_run_id": research_run_id,
                },
            )
            qualification = QualificationHandoff.from_team_result(
                qualification_payload,
                restaurant_id=restaurant_id,
                research_run_id=research_run_id,
                qualification_run_id=qualification_run_id,
            )

            validate_provenance(provenance, research, qualification)

            memory_payload = self._require_tool_data(
                read_relationship_memory,
                {"restaurant_id": restaurant_id},
                allow_none=True,
            )
            if memory_payload is None:
                # This is a system-owned creation step, not an LLM-directed tool
                # invocation.  It also verifies the full upstream provenance.
                memory_payload = self.repository.get_or_create_relationship(
                    restaurant_id=restaurant_id,
                    research_run_id=research_run_id,
                    qualification_run_id=qualification_run_id,
                )
            memory = self._memory_from_payload(memory_payload)

            return {
                "restaurant": research.restaurant.model_dump(mode="json"),
                "research": research.model_dump(mode="json"),
                "qualification": qualification.model_dump(mode="json"),
                "relationship_memory": memory.model_dump(mode="json"),
                "workflow_phase": WorkflowPhase.OBSERVE.value,
                "events": [
                    {
                        "type": "CONTEXT_HYDRATED",
                        "at": utc_now(),
                        "restaurant_id": restaurant_id,
                        "research_run_id": research_run_id,
                        "qualification_run_id": qualification_run_id,
                    }
                ],
            }
        except Exception as error:
            return self._safe_failure("CONTEXT_HYDRATION_FAILED", error)


    # ------------------------------------------------------------------
    # Calendar, decision, and safe customer context
    # ------------------------------------------------------------------

    def _load_calendar(self, state: OutreachGraphState) -> dict[str, Any]:
        """Read Google Calendar only after the decision stage requested it."""

        if state.get("calendar_checked"):
            return {"next_node": "decide_next_action"}
        try:
            restaurant = RestaurantContext.model_validate(state["restaurant"])
            now = self._now()
            result = get_relevant_calendar_events.invoke(
                {
                    "restaurant_id": _safe_int(restaurant.restaurant_id, "restaurant_id"),
                    "start_at": now.isoformat(),
                    "end_at": (
                        now + timedelta(days=self.settings.calendar_lookahead_days)
                    ).isoformat(),
                    "restaurant_context": restaurant.model_dump(mode="json"),
                }
            )
            if not isinstance(result, dict) or not result.get("ok"):
                # Calendar is optional.  A missing credential or an unavailable
                # provider must not turn into an invented event or a fake block.
                status = str(result.get("status") if isinstance(result, dict) else "UNKNOWN")
                return {
                    "calendar_checked": True,
                    "calendar_events": [],
                    "events": [
                        {
                            "type": "CALENDAR_NOT_USED",
                            "status": status,
                            "at": utc_now(),
                        }
                    ],
                }

            raw_events = result.get("data") or []
            events = [
                CalendarEvent.model_validate(item).model_dump(mode="json")
                for item in raw_events
            ]
            return {
                "calendar_checked": True,
                "calendar_events": events,
                "events": [
                    {
                        "type": "CALENDAR_READ",
                        "event_count": len(events),
                        "at": utc_now(),
                    }
                ],
            }
        except Exception as error:
            # Optional Calendar enrichment can fail safely; decision continues
            # without an event rather than faking the occasion.
            return {
                "calendar_checked": True,
                "calendar_events": [],
                "events": [
                    {
                        "type": "CALENDAR_NOT_USED",
                        "status": type(error).__name__,
                        "at": utc_now(),
                    }
                ],
            }

    def _decide_next_action(self, state: OutreachGraphState) -> dict[str, Any]:
        """Call the real decision model, then enforce deterministic constraints."""

        try:
            decision_context = self._decision_context(state)
            decision = OutreachDecision.model_validate(self.llm.decide(model_context(decision_context, self.secrets)))
            decision = self._constrain_decision(state, decision)

            if decision.read_calendar and not state.get("calendar_checked"):
                return {
                    "decision": decision.model_dump(mode="json"),
                    "workflow_phase": WorkflowPhase.DECIDE.value,
                    "next_node": "load_calendar",
                }

            route = self._route_for_action(decision.action)
            return {
                "decision": decision.model_dump(mode="json"),
                "workflow_phase": WorkflowPhase.DECIDE.value,
                "next_node": route,
                "events": [
                    {
                        "type": "NEXT_ACTION_DECIDED",
                        "action": decision.action.value,
                        "at": utc_now(),
                    }
                ],
            }
        except Exception as error:
            return self._safe_failure("DECISION_FAILED", error)

    def _build_customer_safe_context(
        self, state: OutreachGraphState
    ) -> dict[str, Any]:
        """Select only vetted Research observations and verified event titles."""

        try:
            decision = OutreachDecision.model_validate(state["decision"])
            research = ResearchHandoff.model_validate(state["research"])
            restaurant = RestaurantContext.model_validate(state["restaurant"])
            signal_by_id = {
                signal.signal_id: signal for signal in research.research_signals
            }

            selected_ids = [
                signal_id
                for signal_id in decision.personalization_observation_ids
                if signal_id in signal_by_id
            ]
            # Initial outreach must be genuinely personalized.  When the model
            # selected none, use a small vetted set rather than an unsupported
            # generic claim; the facts remain exactly grounded in Research.
            if not selected_ids and decision.action in {
                ActionType.SEND_INITIAL_OUTREACH,
                ActionType.SEND_FOLLOW_UP,
            }:
                selected_ids = [
                    signal.signal_id for signal in research.research_signals[:2]
                ]

            observations = [
                signal_by_id[signal_id].observation
                for signal_id in selected_ids
                if signal_by_id[signal_id].observation.strip()
            ]
            if decision.action in {
                ActionType.SEND_INITIAL_OUTREACH,
                ActionType.SEND_FOLLOW_UP,
            } and not observations:
                raise WorkflowSafetyError(
                    "No grounded Research observation is available for a personalized prospect email."
                )

            event_by_id = {
                event.event_id: event
                for event in [
                    CalendarEvent.model_validate(item)
                    for item in state.get("calendar_events", [])
                ]
            }
            selected_event_ids = [
                event_id
                for event_id in decision.selected_calendar_event_ids
                if event_id in event_by_id
            ]
            calendar_context = [
                event_by_id[event_id].title for event_id in selected_event_ids
            ]
            customer_context = CustomerSafeContext(
                restaurant=restaurant,
                observation_ids=selected_ids,
                observations=observations,
                calendar_event_ids=selected_event_ids,
                calendar_context=calendar_context,
                latest_customer_message=(
                    str(state.get("client_message")).strip()
                    if state.get("client_message")
                    else None
                ),
            )
            return {
                "customer_safe_context": customer_context.model_dump(mode="json"),
                "next_node": "generate_email",
            }
        except Exception as error:
            return self._safe_failure("SAFE_CONTEXT_FAILED", error)

    # ------------------------------------------------------------------
    # Verified response and Strategy-handoff paths
    # ------------------------------------------------------------------

    def _process_button_event(self, state: OutreachGraphState) -> dict[str, Any]:
        """Apply a canonical email response; no browser input is trusted here."""

        try:
            incoming = ButtonClickEvent.model_validate(state["button_event"])
            canonical_payload = self.repository.get_button_event(event_id=incoming.event_id)
            if canonical_payload is None:
                raise WorkflowSafetyError("Verified button event was not found in memory.")
            event = ButtonClickEvent.model_validate(canonical_payload)
            if (
                str(event.restaurant_id) != str(incoming.restaurant_id)
                or event.action is not incoming.action
                or event.outreach_message_id != incoming.outreach_message_id
            ):
                raise WorkflowSafetyError("Button event does not match its canonical record.")

            memory = self._memory_from_payload(state["relationship_memory"])
            if event.action is ButtonAction.NOT_INTERESTED:
                # The response endpoint has already committed DNC atomically
                # before this graph runs.  Marking the event APPLIED makes the
                # audit timeline complete but never restarts promotion.
                if event.status is ButtonEventStatus.RECEIVED:
                    self.repository.mark_button_event_applied(event_id=event.event_id)
                fresh = self._read_memory(event.restaurant_id)
                return {
                    "relationship_memory": fresh.model_dump(mode="json"),
                    "button_event": event.model_dump(mode="json"),
                    "workflow_phase": WorkflowPhase.STOPPED.value,
                    "next_node": "finish",
                    "events": [
                        {
                            "type": "DO_NOT_CONTACT_CONFIRMED",
                            "event_id": event.event_id,
                            "at": utc_now(),
                        }
                    ],
                }

            if event.status is ButtonEventStatus.RECEIVED:
                self.repository.mark_button_event_applied(event_id=event.event_id)
            elif event.status is not ButtonEventStatus.APPLIED:
                raise WorkflowSafetyError("Interested button event is not processable.")

            # Re-read after the transactional event step and record the genuine
            # interest in relationship memory before asking the Strategy Agent.
            fresh = self._read_memory(event.restaurant_id)
            if fresh.interest_event_id not in {None, event.event_id}:
                raise WorkflowSafetyError(
                    "A different interest event is already active for this relationship."
                )
            if fresh.interest_event_id != event.event_id:
                fresh = self._transition_memory(
                    fresh,
                    status=RelationshipStatus.INTERESTED,
                    interest_event_id=event.event_id,
                    last_inbound_event_id=event.event_id,
                    last_inbound_at=event.clicked_at,
                    next_contact_at=None,
                    important_context="Restaurant selected Interested from a verified outreach email.",
                )

            handoff = self._strategy_request_from_state(
                state=state,
                event=event,
                memory=fresh,
            )
            strategy_result = self._require_tool_data(
                create_strategy_request_handoff,
                {"handoff": handoff.model_dump(mode="json")},
            )
            saved_handoff = strategy_result.get("handoff") or handoff.model_dump(mode="json")
            strategy_request_id = str(
                saved_handoff.get("strategy_request_id") or handoff.strategy_request_id
            )
            latest = self._read_memory(event.restaurant_id)
            if latest.strategy_request_id != strategy_request_id or latest.status != RelationshipStatus.AWAITING_STRATEGY_OUTPUT:
                latest = self._transition_memory(
                    latest,
                    status=RelationshipStatus.AWAITING_STRATEGY_OUTPUT,
                    strategy_request_id=strategy_request_id,
                    interest_event_id=event.event_id,
                    next_contact_at=None,
                )
            return {
                "relationship_memory": latest.model_dump(mode="json"),
                "button_event": event.model_dump(mode="json"),
                "strategy_request": handoff.model_dump(mode="json"),
                "workflow_phase": WorkflowPhase.WAIT_FOR_STRATEGY.value,
                "next_node": "persist_wait",
                "events": [
                    {
                        "type": "STRATEGY_REQUEST_HANDOFF",
                        "strategy_request_id": strategy_request_id,
                        "dispatch_status": strategy_result.get("dispatch_status"),
                        "at": utc_now(),
                    }
                ],
            }
        except Exception as error:
            return self._safe_failure("BUTTON_EVENT_PROCESSING_FAILED", error)

    def _process_strategy_output(self, state: OutreachGraphState) -> dict[str, Any]:
        """Validate and persist a teammate Strategy output before notification."""

        try:
            output = StrategyOutputHandoff.model_validate(state["strategy_output"])
            validation = self._require_tool_data(
                validate_strategy_handoff,
                {
                    "strategy_request_id": output.strategy_request_id,
                    "strategy_output": output.model_dump(mode="json"),
                },
            )
            if not validation.get("valid"):
                raise WorkflowSafetyError("Strategy output did not pass the handoff contract.")

            saved = self.repository.record_strategy_output(
                strategy_request_id=output.strategy_request_id,
                strategy_output=output.model_dump(mode="json"),
            )
            memory = self._read_memory(output.restaurant_id)
            if not memory.promotional_contact_allowed:
                return {
                    "relationship_memory": memory.model_dump(mode="json"),
                    "strategy_output": output.model_dump(mode="json"),
                    "next_node": "finish",
                    "events": [
                        {
                            "type": "STRATEGY_OUTPUT_RECORDED_NO_CONTACT",
                            "strategy_request_id": output.strategy_request_id,
                            "at": utc_now(),
                        }
                    ],
                }

            if not output.client_notification_allowed or not output.dashboard_strategy_url:
                return {
                    "relationship_memory": memory.model_dump(mode="json"),
                    "strategy_output": output.model_dump(mode="json"),
                    "next_node": "finish",
                    "events": [
                        {
                            "type": "STRATEGY_OUTPUT_RECORDED_NOTIFICATION_NOT_ALLOWED",
                            "strategy_request_id": output.strategy_request_id,
                            "at": utc_now(),
                        }
                    ],
                }

            decision = OutreachDecision(
                action=ActionType.NOTIFY_STRATEGY_READY,
                next_status=RelationshipStatus.STRATEGY_READY,
                decision_basis=["Validated Strategy Agent output is ready for client notification."],
                language="en",
                requires_human_approval=False,
            )
            return {
                "relationship_memory": memory.model_dump(mode="json"),
                "strategy_output": output.model_dump(mode="json"),
                "strategy_request": saved,
                "decision": decision.model_dump(mode="json"),
                "workflow_phase": WorkflowPhase.DECIDE.value,
                "next_node": "build_customer_safe_context",
                "events": [
                    {
                        "type": "STRATEGY_OUTPUT_VALIDATED",
                        "strategy_request_id": output.strategy_request_id,
                        "at": utc_now(),
                    }
                ],
            }
        except Exception as error:
            return self._safe_failure("STRATEGY_OUTPUT_FAILED", error)

    # ------------------------------------------------------------------
    # Human escalation, email generation, review, approval, and sending
    # ------------------------------------------------------------------

    def _prepare_escalation(self, state: OutreachGraphState) -> dict[str, Any]:
        """Create an internal review item without another outbound approval gate."""

        try:
            memory = self._memory_from_payload(state["relationship_memory"])
            trigger = WorkflowTrigger(state["trigger"])
            client_message = str(state.get("client_message") or "").strip()
            if trigger is WorkflowTrigger.FOLLOW_UP_DUE:
                reason_code = EscalationReason.MAX_NO_RESPONSE_ATTEMPTS
                severity = IssueSeverity.MEDIUM
                reason = (
                    "The configured maximum number of no-response outreach attempts "
                    "was reached; a human must decide whether any further contact is appropriate."
                )
            elif client_message and self._is_sensitive_issue(client_message):
                reason_code = self._sensitive_reason_code(client_message)
                severity = IssueSeverity.SENSITIVE
                reason = "Sensitive client issue requires a human response before any commitment."
            else:
                reason_code = EscalationReason.OTHER
                severity = IssueSeverity.MEDIUM
                reason = "Outreach requires a human decision because grounded communication cannot proceed safely."

            escalation = HumanEscalation(
                escalation_id=_stable_id(
                    "escalation",
                    memory.restaurant_id,
                    trigger.value,
                    memory.last_outbound_message_id or "none",
                    client_message[:256],
                ),
                restaurant_id=memory.restaurant_id,
                reason=reason,
                reason_code=reason_code,
                severity=severity,
                related_issue_id=(
                    memory.issues[-1].issue_id if memory.issues else None
                ),
            )
            return {"escalation": escalation.model_dump(mode="json"),
                    "workflow_phase": WorkflowPhase.DECIDE.value,
                    "next_node": "create_escalation"}
        except Exception as error:
            return self._safe_failure("ESCALATION_PREPARATION_FAILED", error)

    def _create_escalation(self, state: OutreachGraphState) -> dict[str, Any]:
        """Persist the internal escalation without sending an email."""

        try:
            escalation = HumanEscalation.model_validate(state["escalation"])
            saved = self._require_tool_data(
                create_human_escalation,
                {"escalation": escalation.model_dump(mode="json")},
            )
            memory = self._read_memory(escalation.restaurant_id)
            return {
                "relationship_memory": memory.model_dump(mode="json"),
                "workflow_phase": WorkflowPhase.STOPPED.value,
                "events": [
                    {
                        "type": "HUMAN_ESCALATION_CREATED",
                        "escalation_id": saved.get("escalation_id", escalation.escalation_id),
                        "at": utc_now(),
                    }
                ],
            }
        except Exception as error:
            return self._safe_failure("ESCALATION_CREATION_FAILED", error)

    def _generate_email(self, state: OutreachGraphState) -> dict[str, Any]:
        """Use the real generation model on customer-safe data only."""

        try:
            decision = OutreachDecision.model_validate(state["decision"])
            memory = self._memory_from_payload(state["relationship_memory"])
            restaurant = RestaurantContext.model_validate(state["restaurant"])
            safe_context = CustomerSafeContext.model_validate(state["customer_safe_context"])
            # A paused checkpoint can predate a contact edit. Bind each NEW draft
            # to the current persisted address; existing approved drafts stay immutable.
            if hasattr(self.repository, "load_research_handoff"):
                current = self.repository.load_research_handoff(
                    restaurant_id=restaurant.restaurant_id,
                    research_run_id=state["provenance"]["research_run_id"],
                )
                restaurant = restaurant.model_copy(update={"email": current["restaurant"].get("email")})
            if not restaurant.email:
                raise WorkflowSafetyError("Restaurant has no usable email address for outbound contact.")
            message_type = self._message_type_for_action(decision.action)
            language = "en" if message_type in {
                OutboundMessageType.INITIAL_OUTREACH,
                OutboundMessageType.NO_RESPONSE_FOLLOW_UP,
            } else decision.language
            generation_context = {
                "message_type": message_type.value,
                "action": decision.action.value,
                "language": language,
                "restaurant": safe_context.restaurant.model_dump(mode="json"),
                "observations": safe_context.observations,
                "observation_ids": safe_context.observation_ids,
                "calendar_context": safe_context.calendar_context,
                "calendar_event_ids": safe_context.calendar_event_ids,
                "latest_customer_message": safe_context.latest_customer_message,
                # The LLM never receives a trusted URL. The graph appends a
                # validated dashboard link after generation when relevant.
                "dashboard_access_available": decision.action
                in {
                    ActionType.NOTIFY_STRATEGY_READY,
                },
                "complimentary_free_trial": True,
                "trial_days": 30,
                "trial_starts_on": "first successful dashboard sign-in (activation)",
                "revision_feedback": state.get("revision_feedback"),
                "previous_draft": state.get("rejected_draft"),
            }
            proposal = EmailContentProposal.model_validate(
                self.llm.generate_email(model_context(generation_context, self.secrets))
            )
            validate_proposal(proposal, safe_context)
            selected_observations = [
                observation_id
                for observation_id in proposal.personalization_observation_ids
                if observation_id in set(safe_context.observation_ids)
            ]
            body = proposal.plain_text_body.strip()
            body = self._append_trusted_dashboard_link(
                body=body,
                action=decision.action,
                state=state,
            )
            raw_draft = {
                "message_id": self._message_id_for_state(state),
                "revision": 1,
                "relationship_id": memory.relationship_id,
                "restaurant_id": restaurant.restaurant_id,
                "message_type": message_type.value,
                "action": decision.action.value,
                "recipient": restaurant.email,
                "subject": proposal.subject.strip(),
                # Trusted rendering replaces this empty placeholder before the
                # strict EmailDraft model is constructed by EmailService.
                "html_body": "<pending trusted rendering>",
                "plain_text_body": body,
                "language": language,
                "buttons": [],
                "personalization_observation_ids": selected_observations,
                "personalization_context": safe_context.observations,
                "status": EmailStatus.GENERATED.value,
            }
            return {
                "approval": None,
                "email_draft": raw_draft,
                "workflow_phase": WorkflowPhase.DRAFT.value,
                "next_node": "prepare_and_persist_email",
            }
        except Exception as error:
            return self._safe_failure("EMAIL_GENERATION_FAILED", error)

    def _prepare_and_persist_email(
        self, state: OutreachGraphState
    ) -> dict[str, Any]:
        """Trusted-render the exact email then persist its immutable version."""

        try:
            raw_draft = dict(state["email_draft"])
            prepared = self.email_service.prepare_for_approval(raw_draft)
            draft = EmailDraft.model_validate(prepared.draft)
            record = self.repository.save_outbound_message(
                draft=draft,
                provenance=state["provenance"],
                content_sha256=prepared.content_sha256,
                supersedes_message_id=self._superseded_message_id(state),
            )
            # Repository retries may safely revise a draft that never reached
            # approval. Keep the graph's review/approval binding on the exact
            # persisted content version, never on the provisional revision.
            persisted_revision = int(record["content_version"])
            if draft.revision != persisted_revision:
                draft = draft.model_copy(update={"revision": persisted_revision})
            return {
                "email_draft": draft.model_dump(mode="json"),
                "message_record": record,
                "next_node": "review_email",
            }
        except Exception as error:
            return self._safe_failure("EMAIL_PREPARATION_FAILED", error)

    def _review_email(self, state: OutreachGraphState) -> dict[str, Any]:
        """Run the distinct Review model before a human can approve a send."""

        try:
            draft = EmailDraft.model_validate(state["email_draft"])
            customer_context = CustomerSafeContext.model_validate(
                state["customer_safe_context"]
            )
            # The LLM reviews language, grounding, and privacy. It must never
            # receive bearer-style signed response URLs (or the recipient's
            # address), because trusted codeâ€”not the LLMâ€”owns link integrity
            # and endpoint reachability.
            review_draft = self._redacted_draft_for_review(draft)
            review = MessageReview.model_validate(
                self.llm.review_email(
                    draft=model_context(review_draft, self.secrets),
                    customer_safe_context=model_context(customer_context.model_dump(mode="json"), self.secrets),
                )
            )
            allowed_fact_ids = set(customer_context.observation_ids) | set(
                customer_context.calendar_event_ids
            )
            invalid_references = [
                reference
                for reference in review.approved_fact_references
                if reference not in allowed_fact_ids
            ]
            if invalid_references:
                review = review.model_copy(
                    update={
                        "passed": False,
                        "privacy_safe": False,
                        "issues": review.issues
                        + ["Review returned unsupported fact references."],
                    }
                )

            if not review.passed or not review.privacy_safe:
                return {
                    "review": review.model_dump(mode="json"),
                    "workflow_phase": WorkflowPhase.FAILED.value,
                    "next_node": "finish",
                    "errors": ["Email review did not pass; no Human Approval or send was requested."],
                }

            if draft.message_type is not OutboundMessageType.INITIAL_OUTREACH:
                # A machine authorization is audited explicitly, never presented as a human decision.
                authorization = HumanApproval(
                    approval_id=_stable_id("automatic", draft.message_id, draft.revision),
                    message_id=draft.message_id, message_revision=draft.revision,
                    content_sha256=state["message_record"]["content_sha256"],
                    status=ApprovalStatus.APPROVED, reviewer_id="system:outreach-policy",
                    expires_at=(self._now() + timedelta(minutes=self.settings.approval_ttl_minutes)).isoformat(),
                    note="Automatic follow-up authorization after content review; no human approval required.",
                )
                self.repository.save_human_approval(approval=authorization)
                return {"review": review.model_dump(mode="json"),
                        "approval": authorization.model_dump(mode="json"),
                        "next_node": "execute_approved_email"}

            memory = self._memory_from_payload(state["relationship_memory"])
            if memory.status is not RelationshipStatus.PENDING_OUTBOUND_APPROVAL:
                memory = self._transition_memory(
                    memory,
                    status=RelationshipStatus.PENDING_OUTBOUND_APPROVAL,
                )
            record = state["message_record"]
            expires_at = self._now() + timedelta(minutes=self.settings.approval_ttl_minutes)
            return {
                "relationship_memory": memory.model_dump(mode="json"),
                "review": review.model_dump(mode="json"),
                "approval_request": {
                    "kind": "OUTBOUND_EMAIL",
                    "message_id": draft.message_id,
                    "message_revision": draft.revision,
                    "content_sha256": str(record["content_sha256"]),
                    "recipient": draft.recipient,
                    "subject": draft.subject,
                    "message_type": draft.message_type.value,
                    "review": review.model_dump(mode="json"),
                    "expires_at": expires_at.isoformat(),
                },
                "workflow_phase": WorkflowPhase.PENDING_HUMAN_APPROVAL.value,
                "next_node": "human_approval",
            }
        except Exception as error:
            return self._safe_failure("EMAIL_REVIEW_FAILED", error)

    def _human_approval(self, state: OutreachGraphState) -> dict[str, Any]:
        """Interrupt for a reviewer, then bind approval to immutable content."""

        request = dict(state.get("approval_request") or {})
        if request.get("kind") != "OUTBOUND_EMAIL":
            return self._safe_failure(
                "EMAIL_APPROVAL_MISSING", WorkflowSafetyError("No email approval request.")
            )

        # No write before interrupt: a resume re-enters this node from the top.
        response = interrupt(
            {
                "type": "HUMAN_EMAIL_APPROVAL_REQUIRED",
                **request,
                "instruction": "Approve or reject this exact rendered email. Approval is required before provider submission.",
            }
        )
        try:
            expires_at = _as_utc(request.get("expires_at"))
            if expires_at is None or self._now() > expires_at:
                return self._stop("EMAIL_APPROVAL_EXPIRED", RelationshipStatus.WAIT)
            response_data = self._approval_decision(response)
            self._verify_approval_binding(request, response_data)
            status = ApprovalStatus(response_data["decision"])
            approval = HumanApproval(
                approval_id=_stable_id(
                    "approval", request["message_id"], request["message_revision"]
                ),
                message_id=str(request["message_id"]),
                message_revision=int(request["message_revision"]),
                content_sha256=str(request["content_sha256"]),
                status=status,
                reviewer_id=response_data["reviewer_id"],
                expires_at=str(request["expires_at"]),
                note=response_data.get("note"),
            )
            saved = self.repository.save_human_approval(approval=approval)
            if status is ApprovalStatus.REJECTED:
                memory = self._memory_from_payload(state["relationship_memory"])
                if memory.status is RelationshipStatus.PENDING_OUTBOUND_APPROVAL:
                    memory = self._transition_memory(memory, status=RelationshipStatus.WAIT)
                return {
                    "approval": approval.model_dump(mode="json"),
                    "relationship_memory": memory.model_dump(mode="json"),
                    "workflow_phase": WorkflowPhase.DRAFT.value,
                    "next_node": "generate_email",
                    "revision_feedback": response_data["note"],
                    "rejected_draft": state["email_draft"]["plain_text_body"],
                    "rejected_message_id": request["message_id"],
                    "draft_attempt": int(state.get("draft_attempt", 0)) + 1,
                    "events": [
                        {
                            "type": "OUTBOUND_REJECTED_BY_HUMAN",
                            "approval_id": saved["approval_id"],
                            "at": utc_now(),
                        }
                    ],
                }
            return {
                "approval": approval.model_dump(mode="json"),
                "next_node": "execute_approved_email",
            }
        except Exception as error:
            return self._safe_failure("EMAIL_APPROVAL_FAILED", error)

    def _execute_approved_email(self, state: OutreachGraphState) -> dict[str, Any]:
        """Submit an approved immutable email only through a real provider."""

        try:
            draft = EmailDraft.model_validate(state["email_draft"])
            approval = HumanApproval.model_validate(state["approval"])
            tool_result = send_approved_email.invoke(
                {"message_id": draft.message_id, "approval_id": approval.approval_id}
            )
            if not isinstance(tool_result, dict) or not tool_result.get("ok"):
                status = (
                    str(tool_result.get("status"))
                    if isinstance(tool_result, dict)
                    else "UNKNOWN"
                )
                return {
                    "workflow_phase": WorkflowPhase.FAILED.value,
                    "next_node": "finish",
                    "errors": [
                        f"Approved email was not submitted: {status}. No SENT status was recorded."
                    ],
                }

            send_data = tool_result.get("data") or {}
            provider = dict(send_data.get("provider_result") or {})
            if send_data.get("status") == "SENT":
                execution = OutboundExecution(
                    execution_id=_stable_id("send", draft.message_id, approval.approval_id),
                    message_id=draft.message_id,
                    provider_name=str(provider.get("provider") or "unknown"),
                    success=True,
                    provider_message_id=str(provider["provider_message_id"]),
                    provider_status=str(provider.get("provider_status") or "ACCEPTED"),
                    email_status=EmailStatus.SENT,
                )
            else:
                execution = OutboundExecution(
                    execution_id=_stable_id("send", draft.message_id, approval.approval_id),
                    message_id=draft.message_id,
                    provider_name=str(provider.get("provider") or "unknown"),
                    success=False,
                    provider_message_id=None,
                    provider_status=str(provider.get("failure_code") or "FAILED"),
                    email_status=EmailStatus.FAILED,
                    detail=str(provider.get("failure_detail") or "Provider did not accept the email."),
                )

            saved = self._require_tool_data(
                record_outbound_execution,
                {"execution": execution.model_dump(mode="json")},
            )
            if not execution.success:
                return {
                    "execution": execution.model_dump(mode="json"),
                    "workflow_phase": WorkflowPhase.FAILED.value,
                    "next_node": "finish",
                    "errors": [
                        "The real provider did not accept the email; it was recorded as FAILED, not SENT."
                    ],
                }
            return {
                "execution": execution.model_dump(mode="json"),
                "message_record": saved,
                "workflow_phase": WorkflowPhase.EXECUTE_OUTBOUND.value,
                "next_node": "persist_post_send",
            }
        except Exception as error:
            return self._safe_failure("OUTBOUND_EXECUTION_FAILED", error)

    def _persist_post_send(self, state: OutreachGraphState) -> dict[str, Any]:
        """Record a truthful next state only after provider evidence was stored."""

        try:
            draft = EmailDraft.model_validate(state["email_draft"])
            execution = OutboundExecution.model_validate(state["execution"])
            if not execution.success:
                return self._safe_failure(
                    "POST_SEND_WITHOUT_PROVIDER_SUCCESS",
                    WorkflowSafetyError("Cannot update relationship as sent without provider success."),
                )
            memory = self._read_memory(draft.restaurant_id)
            if draft.message_type in {
                OutboundMessageType.INITIAL_OUTREACH,
                OutboundMessageType.NO_RESPONSE_FOLLOW_UP,
            }:
                memory = self._transition_memory(
                    memory,
                    status=RelationshipStatus.WAITING_FOR_RESPONSE,
                    last_outbound_message_id=draft.message_id,
                    last_outbound_at=execution.executed_at,
                    next_contact_at=(
                        self._now()
                        + timedelta(minutes=self.settings.follow_up_delay_minutes)
                    ).isoformat(),
                )
            elif draft.message_type is OutboundMessageType.STRATEGY_READY_NOTIFICATION:
                memory = self._transition_memory(
                    memory,
                    # The provider submission proves delivery notification only;
                    # it does not prove a paid subscription, implementation, or
                    # any marketing outcome.  The team may later confirm an
                    # ACTIVE_CLIENT state explicitly.
                    status=RelationshipStatus.STRATEGY_DELIVERED,
                    last_outbound_message_id=draft.message_id,
                    last_outbound_at=execution.executed_at,
                    next_contact_at=None,
                    important_context="Client received the validated strategy dashboard notification for the complimentary Free Trial.",
                )
            else:
                # Client relationship messages are sent only after approval; do
                # not manufacture a next check-in schedule without actual context.
                memory = self._transition_memory(
                    memory,
                    status=(
                        memory.status
                        if memory.status
                        in {
                            RelationshipStatus.STRATEGY_DELIVERED,
                            RelationshipStatus.ACTIVE_CLIENT,
                        }
                        else RelationshipStatus.ACTIVE_CLIENT
                    ),
                    last_outbound_message_id=draft.message_id,
                    last_outbound_at=execution.executed_at,
                    next_contact_at=None,
                )
            if draft.message_type is OutboundMessageType.FEEDBACK_REQUEST and hasattr(self.repository, "mark_feedback_requested"):
                self.repository.mark_feedback_requested(restaurant_id=int(draft.restaurant_id), at=self._now())
            return {
                "relationship_memory": memory.model_dump(mode="json"),
                "workflow_phase": WorkflowPhase.WAIT_FOR_EVENT.value,
                "events": [
                    {
                        "type": "OUTBOUND_PROVIDER_ACCEPTED",
                        "message_id": draft.message_id,
                        "provider_message_id": execution.provider_message_id,
                        "at": utc_now(),
                    }
                ],
            }
        except Exception as error:
            return self._safe_failure("POST_SEND_PERSISTENCE_FAILED", error)

    def _persist_wait(self, state: OutreachGraphState) -> dict[str, Any]:
        """Finish a no-send path while preserving the last durable memory state."""

        return {
            "workflow_phase": state.get("workflow_phase", WorkflowPhase.WAIT_FOR_EVENT.value),
            "events": [{"type": "WORKFLOW_WAIT", "at": utc_now()}],
        }

    def _finish(self, state: OutreachGraphState) -> dict[str, Any]:
        """Terminal node; it deliberately never sends or mutates external state."""

        return {
            "workflow_phase": state.get("workflow_phase", WorkflowPhase.STOPPED.value),
        }

    # ------------------------------------------------------------------
    # Decision constraints and private helpers
    # ------------------------------------------------------------------

    def _decision_context(self, state: OutreachGraphState) -> dict[str, Any]:
        memory = self._memory_from_payload(state["relationship_memory"])
        qualification = QualificationHandoff.model_validate(state["qualification"])
        research = ResearchHandoff.model_validate(state["research"])
        return {
            "trigger": state["trigger"],
            "now": self._now().isoformat(),
            "qualification_status": qualification.status.value,
            "qualification_context": qualification.model_dump(mode="json"),
            "relationship_memory": memory.model_dump(mode="json"),
            "research_observations": [
                {"id": item.signal_id, "observation": item.observation}
                for item in research.research_signals
            ],
            "calendar_events": state.get("calendar_events", []),
            "calendar_checked": bool(state.get("calendar_checked")),
            "strategy_context": state.get("strategy_context") or {},
            "follow_up_max_attempts": self.settings.follow_up_max_attempts,
            "client_message": state.get("client_message"),
        }


    def _route_for_action(self, action: ActionType) -> str:
        if action in self._OUTBOUND_ACTIONS:
            return "build_customer_safe_context"
        if action is ActionType.ESCALATE_TO_HUMAN:
            return "prepare_escalation"
        return "persist_wait"

    @staticmethod
    def _message_type_for_action(action: ActionType) -> OutboundMessageType:
        mapping = {
            ActionType.SEND_INITIAL_OUTREACH: OutboundMessageType.INITIAL_OUTREACH,
            ActionType.SEND_FOLLOW_UP: OutboundMessageType.NO_RESPONSE_FOLLOW_UP,
            ActionType.NOTIFY_STRATEGY_READY: OutboundMessageType.STRATEGY_READY_NOTIFICATION,
            ActionType.SEND_CLIENT_CHECK_IN: OutboundMessageType.CLIENT_CHECK_IN,
            ActionType.REQUEST_FEEDBACK: OutboundMessageType.FEEDBACK_REQUEST,
            ActionType.REVIEW_CLIENT_ISSUE: OutboundMessageType.CLIENT_ISSUE_RESPONSE,
        }
        try:
            return mapping[action]
        except KeyError as error:
            raise WorkflowSafetyError(f"{action.value} does not produce an email.") from error

    def _message_id_for_state(self, state: OutreachGraphState) -> str:
        decision = OutreachDecision.model_validate(state["decision"])
        memory = self._memory_from_payload(state["relationship_memory"])
        provenance = state["provenance"]
        if decision.action is ActionType.SEND_INITIAL_OUTREACH:
            return _stable_id(
                "outbound_initial",
                provenance["restaurant_id"],
                provenance["research_run_id"],
                provenance["qualification_run_id"],
                state.get("draft_attempt", 0),
            )
        if decision.action is ActionType.SEND_FOLLOW_UP:
            return _stable_id(
                "outbound_followup",
                memory.relationship_id,
                memory.last_outbound_message_id or "none",
                memory.outreach_attempts,
                memory.next_contact_at or "due",
            )
        if decision.action is ActionType.NOTIFY_STRATEGY_READY:
            output = StrategyOutputHandoff.model_validate(state["strategy_output"])
            return _stable_id(
                "outbound_strategy_ready",
                output.strategy_request_id,
                output.strategy_id,
            )
        message_digest = hashlib.sha256(
            str(state.get("client_message") or memory.last_inbound_event_id or "check").encode("utf-8")
        ).hexdigest()[:16]
        return _stable_id("outbound_client", memory.relationship_id, decision.action.value, message_digest)

    def _superseded_message_id(self, state: OutreachGraphState) -> str | None:
        if state.get("rejected_message_id"):
            return state["rejected_message_id"]
        decision = OutreachDecision.model_validate(state["decision"])
        memory = self._memory_from_payload(state["relationship_memory"])
        if decision.action is ActionType.SEND_FOLLOW_UP:
            return memory.last_outbound_message_id
        return None

    def _append_trusted_dashboard_link(
        self,
        *,
        body: str,
        action: ActionType,
        state: OutreachGraphState,
    ) -> str:
        """Append a system-validated link rather than letting the model invent it."""

        if action is ActionType.NOTIFY_STRATEGY_READY:
            output = StrategyOutputHandoff.model_validate(state["strategy_output"])
            url = self._validated_dashboard_url(output.dashboard_strategy_url)
            # The model's own text already announces the Free Trial, so the trusted part only points to the link.
            text = f"{body}{self._TRUSTED_FOOTER_MARKER}\n{url}"
            return text + "\n\nCongratulations! Your free trial lasts 30 days from activation on your first successful sign-in." + self._sign_in_block(state)
        if action is ActionType.REQUEST_FEEDBACK:
            from api.accounts import login_url
            return body + "\n\nShare your feedback in your dashboard: " + self._validated_dashboard_url(login_url())
        return body

    def _sign_in_block(self, state: OutreachGraphState) -> str:
        """The client's username, password and sign-in link; missing provisioning blocks delivery."""

        if self.access_provider is None:
            raise WorkflowSafetyError("Dashboard account provisioning is not configured.")
        access = self.access_provider(int(state["provenance"]["restaurant_id"]))
        if not access or not access.get("username"):
            raise WorkflowSafetyError("The client's dashboard access could not be prepared.")
        sign_in_url = self._validated_dashboard_url(access.get("login_url"))
        if not access.get("password"):
            raise WorkflowSafetyError("The client's dashboard access has no password to send.")
        if not hasattr(self, "secrets"):
            self.secrets = SecretProtector()
        self.secrets.register(access["username"], access["password"])
        block = "\n".join([
            "", "", "Your Rawaj sign-in:",
            f"Username: {access['username']}",
            f"Password: {access['password']}",
            f"Sign in here: {sign_in_url}",
            "",
            "Keep your password just between us: please do not share it with anyone. "
            "Your privacy: Rawaj never asks you to send passwords, payment details or other sensitive "
            "information by email or message, and we do not collect any through this email.",
        ])
        # The email must carry exactly what the account needs; a partial block would lock the client out.
        for required in (access["username"], access["password"], sign_in_url):
            if required not in block:
                raise WorkflowSafetyError("The sign-in details were not rendered completely.")
        return block


    def _strategy_request_from_state(
        self,
        *,
        state: OutreachGraphState,
        event: ButtonClickEvent,
        memory: RelationshipMemory,
    ) -> StrategyRequestHandoff:
        provenance = state["provenance"]
        research = ResearchHandoff.model_validate(state["research"])
        qualification = QualificationHandoff.model_validate(state["qualification"])
        return StrategyRequestHandoff(
            strategy_request_id=_stable_id("strategy_request", event.event_id),
            restaurant_id=memory.restaurant_id,
            research_run_id=provenance["research_run_id"],
            qualification_run_id=provenance["qualification_run_id"],
            outreach_message_id=event.outreach_message_id,
            interest_event_id=event.event_id,
            strategy_start_date=event.clicked_at[:10],
            customer_request="Restaurant selected Interested from Rawaj outreach.",
            research_context=research.model_dump(mode="json"),
            qualification_context=qualification.model_dump(mode="json"),
            customer_context={
                "offer": "COMPLIMENTARY_FREE_TRIAL",
                "language": "en",
                "customer_consent": "VERIFIED_INTERESTED_BUTTON",
            },
            status=StrategyRequestStatus.CREATED,
        )

    def _remember_client_message(
        self, memory: RelationshipMemory, text: str
    ) -> RelationshipMemory:
        text = self.secrets.safe(text)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:24]
        if self._is_sensitive_issue(text) or self._ISSUE_WORDS.search(text):
            issue_id = f"issue_{digest}"
            if any(issue.issue_id == issue_id for issue in memory.issues):
                return memory
            severity = (
                IssueSeverity.SENSITIVE
                if self._is_sensitive_issue(text)
                else IssueSeverity.MEDIUM
            )
            return memory.model_copy(
                update={
                    "issues": memory.issues
                    + [
                        ClientIssue(
                            issue_id=issue_id,
                            message=text,
                            severity=severity,
                        )
                    ],
                    "updated_at": utc_now(),
                }
            )
        feedback_id = f"feedback_{digest}"
        if any(item.feedback_id == feedback_id for item in memory.feedback):
            return memory
        return memory.model_copy(
            update={
                "feedback": memory.feedback
                + [ClientFeedback(feedback_id=feedback_id, message=text)],
                "updated_at": utc_now(),
            }
        )

    def _is_sensitive_issue(self, message: str) -> bool:
        return bool(self._SENSITIVE_ISSUE.search(message))

    def _sensitive_reason_code(self, message: str) -> EscalationReason:
        lowered = message.casefold()
        if any(token in lowered for token in ("refund", "loss", "lost", "طھط¹ظˆظٹط¶", "ط®ط³ط§ط±ط©", "ط§ط³طھط±ط¬ط§ط¹")):
            return EscalationReason.REFUND_OR_FINANCIAL_LOSS
        if any(token in lowered for token in ("legal", "lawyer", "contract", "ظ‚ط§ظ†ظˆظ†", "ظ…ط­ط§ظ…ظٹ", "ط¹ظ‚ط¯")):
            return EscalationReason.LEGAL_OR_COMPLIANCE
        if any(token in lowered for token in ("safety", "injury", "ط³ظ„ط§ظ…ط©")):
            return EscalationReason.SAFETY
        return EscalationReason.UNSUPPORTED_COMMITMENT

    # ------------------------------------------------------------------
    # Repository/tool adapters, result conversion, and error hygiene
    # ------------------------------------------------------------------

    def _invoke_new_run(
        self,
        *,
        trigger: WorkflowTrigger,
        restaurant_id: int | str,
        research_run_id: int | str,
        qualification_run_id: int | str,
        thread_id: str,
        button_event: ButtonClickEvent | None = None,
        strategy_output: StrategyOutputHandoff | None = None,
        client_message: str | None = None,
    ) -> OutreachWorkflowResult:
        previous = self.graph.get_state(self._graph_config(thread_id))
        if previous.values and (previous.next or (previous.values.get("execution") or {}).get("success")):
            # Keep the exact paused draft, or return the already sent result on a retry.
            return self._result_from_state(thread_id, previous.values)
        state: OutreachGraphState = {
            "thread_id": thread_id,
            "trigger": trigger.value,
            "provenance": {
                "restaurant_id": restaurant_id,
                "research_run_id": research_run_id,
                "qualification_run_id": qualification_run_id,
                "received_at": utc_now(),
            },
            "workflow_phase": WorkflowPhase.OBSERVE.value,
            "calendar_checked": False,
            "calendar_events": [],
            # These fields use list-append reducers so that nodes can build an
            # audit trail within one graph run.  A brand-new trigger using the
            # same stable thread ID must explicitly reset them, otherwise an
            # earlier transient failure would incorrectly stop the retry.
            "events": Overwrite([]),
            "errors": Overwrite([]),
        }
        if button_event is not None:
            state["button_event"] = button_event.model_dump(mode="json")
        if strategy_output is not None:
            state["strategy_output"] = strategy_output.model_dump(mode="json")
        if client_message:
            state["client_message"] = client_message
        self._bind_tools()
        result = self._guarded_graph_invoke(state, self._graph_config(thread_id))
        return self._result_from_state(thread_id, result)

    @staticmethod
    def _graph_config(thread_id: str) -> dict[str, Any]:
        return {"configurable": {"thread_id": thread_id}}

    @staticmethod
    def _route(state: OutreachGraphState) -> str:
        return str(state.get("next_node") or "finish")

    def _require_tool_data(
        self,
        tool: Any,
        arguments: dict[str, Any],
        *,
        allow_none: bool = False,
    ) -> Any:
        result = tool.invoke(arguments)
        if not isinstance(result, dict) or not result.get("ok"):
            status = result.get("status") if isinstance(result, dict) else "UNKNOWN"
            raise WorkflowSafetyError(f"Required tool did not complete safely: {status}.")
        data = result.get("data")
        if data is None and not allow_none:
            raise WorkflowSafetyError("Required tool returned no data.")
        return data

    @staticmethod
    def _research_from_team_payload(payload: dict[str, Any]) -> ResearchHandoff:
        allowed = {
            "restaurant",
            "research_run_id",
            "analysis_status",
            "generated_at",
            "profile",
            "profile_analysis",
            "metrics",
            "research_signals",
            "analysis_coverage",
            "data_quality",
        }
        return ResearchHandoff.model_validate(
            {key: value for key, value in payload.items() if key in allowed}
        )


    @staticmethod
    def _memory_from_payload(payload: dict[str, Any]) -> RelationshipMemory:
        allowed = set(RelationshipMemory.model_fields)
        return RelationshipMemory.model_validate(
            {key: value for key, value in payload.items() if key in allowed}
        )

    def _read_memory(self, restaurant_id: int | str) -> RelationshipMemory:
        payload = self._require_tool_data(
            read_relationship_memory,
            {"restaurant_id": _safe_int(restaurant_id, "restaurant_id")},
        )
        return self._memory_from_payload(payload)

    @staticmethod
    def _project_strategy_output(payload: dict[str, Any]) -> dict[str, Any]:
        """Keep only the documented teammate output contract for decisions."""

        allowed = set(StrategyOutputHandoff.model_fields)
        data = {key: value for key, value in payload.items() if key in allowed}
        if not data:
            return {}
        try:
            return StrategyOutputHandoff.model_validate(data).model_dump(mode="json")
        except Exception:
            # An optional Strategy context cannot be guessed or repaired here.
            return {}

    def _load_optional_strategy_context(
        self, strategy_request_id: str | None
    ) -> dict[str, Any]:
        """Read a teammate-owned strategy output only when client work needs it."""

        if not strategy_request_id:
            return {}
        result = load_strategy_handoff.invoke(
            {"strategy_request_id": strategy_request_id}
        )
        if not isinstance(result, dict) or not result.get("ok"):
            return {}
        payload = result.get("data")
        return self._project_strategy_output(payload) if isinstance(payload, dict) else {}

    def _write_memory(
        self, memory: RelationshipMemory, *, expected_version: int | None
    ) -> RelationshipMemory:
        payload = self._require_tool_data(
            write_relationship_memory,
            {
                "memory": memory.model_dump(mode="json"),
                "expected_version": expected_version,
            },
        )
        return self._memory_from_payload(payload)

    def _transition_memory(
        self,
        memory: RelationshipMemory,
        *,
        status: RelationshipStatus | None = None,
        important_context: str | None = None,
        **updates: Any,
    ) -> RelationshipMemory:
        """Write a narrow, retry-safe relationship transition with CAS."""

        values: dict[str, Any] = dict(updates)
        if status is not None:
            values["status"] = status
        if important_context and important_context not in memory.important_context:
            values["important_context"] = memory.important_context + [important_context]
        candidate = memory.model_copy(update=values)
        # Avoid needless version changes when a graph recovery replays an
        # already-completed logical transition.
        if candidate.model_dump(mode="json") == memory.model_dump(mode="json"):
            return memory
        candidate = candidate.model_copy(update={"updated_at": utc_now()})
        return self._write_memory(candidate, expected_version=memory.version)



    def _stop(
        self, event_type: str, status: RelationshipStatus
    ) -> dict[str, Any]:
        return {
            "workflow_phase": WorkflowPhase.STOPPED.value,
            "next_node": "finish",
            "events": [{"type": event_type, "status": status.value, "at": utc_now()}],
        }


    def _result_from_state(
        self, thread_id: str, state: dict[str, Any]
    ) -> OutreachWorkflowResult:
        memory_payload = state.get("relationship_memory")
        if memory_payload is None:
            # A failed hydration did not prove anything about the relationship.
            # This explicit error is safer than fabricating a default lifecycle.
            raise WorkflowSafetyError(
                "Workflow did not load relationship memory; inspect its safe errors."
            )
        return OutreachWorkflowResult(
            thread_id=thread_id,
            relationship_memory=self._memory_from_payload(memory_payload),
            decision=(
                OutreachDecision.model_validate(state["decision"])
                if state.get("decision")
                else None
            ),
            email_draft=(
                EmailDraft.model_validate(state["email_draft"])
                if state.get("email_draft")
                and str(state["email_draft"].get("html_body"))
                != "<pending trusted rendering>"
                else None
            ),
            review=(
                MessageReview.model_validate(state["review"])
                if state.get("review")
                else None
            ),
            approval=(
                HumanApproval.model_validate(state["approval"])
                if state.get("approval")
                else None
            ),
            execution=(
                OutboundExecution.model_validate(state["execution"])
                if state.get("execution")
                else None
            ),
            strategy_request=(
                StrategyRequestHandoff.model_validate(
                    self._strict_strategy_request_payload(state["strategy_request"])
                )
                if state.get("strategy_request")
                else None
            ),
            escalation=(
                HumanEscalation.model_validate(state["escalation"])
                if state.get("escalation")
                else None
            ),
            errors=list(state.get("errors") or []),
        )

    @staticmethod
    def _strict_strategy_request_payload(payload: dict[str, Any]) -> dict[str, Any]:
        allowed = set(StrategyRequestHandoff.model_fields)
        data = {key: value for key, value in payload.items() if key in allowed}
        data.setdefault("handoff_type", "STRATEGY_REQUEST")
        return data


__all__ = [
    "OutreachFollowUpWorkflow",
    "WorkflowSafetyError",
    "create_sqlite_checkpointer",
]
