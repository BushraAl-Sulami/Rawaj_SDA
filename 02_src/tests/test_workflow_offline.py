"""Deterministic offline tests for the Rawaj Outreach & Follow-Up workflow.

These tests use explicit in-memory infrastructure doubles under ``RAWAJ_ENV``
test semantics.  They never call OpenAI, Gmail/SMTP, Resend, or Google.  The
production workflow still has no automatic LLM or provider fallback.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from typing import Any

from langgraph.checkpoint.memory import MemorySaver

from agents.outreach_followup_agent.config import RawajSettings
from agents.outreach_followup_agent.email_service import EmailService
from agents.outreach_followup_agent.schemas import (
    ActionType,
    ButtonAction,
    ButtonClickEvent,
    ButtonEventStatus,
    EmailContentProposal,
    MessageReview,
    OutreachDecision,
    RelationshipMemory,
    RelationshipStatus,
    StrategyOutputHandoff,
    StrategyOutputStatus,
)
from agents.outreach_followup_agent.workflow import OutreachFollowUpWorkflow


class InMemoryRepository:
    """Narrow persistence double implementing the workflow's repository contract."""

    def __init__(self) -> None:
        self.memory: RelationshipMemory | None = None
        self.research_run_id = 101
        self.qualification_run_id = 202
        self.messages: dict[str, dict[str, Any]] = {}
        self.approvals: dict[str, dict[str, Any]] = {}
        self.button_events: dict[str, dict[str, Any]] = {}
        self.strategy_requests: dict[str, dict[str, Any]] = {}
        self.escalations: list[dict[str, Any]] = []

    def load_research_handoff(self, restaurant_id: int, research_run_id: int) -> dict[str, Any]:
        assert research_run_id == self.research_run_id
        return {
            "restaurant": {
                "restaurant_id": restaurant_id,
                "name": "Demo Cafe",
                "email": "demo@example.com",
                "location": "Jeddah",
            },
            "research_run_id": research_run_id,
            "analysis_status": "COMPLETED",
            "research_signals": [
                {
                    "signal_id": "menu_observation",
                    "dimension": "content",
                    "observation": "Public posts highlight seasonal iced drinks.",
                }
            ],
        }

    def load_qualification_handoff(
        self,
        restaurant_id: int,
        qualification_run_id: int,
        research_run_id: int,
    ) -> dict[str, Any]:
        assert qualification_run_id == self.qualification_run_id
        assert research_run_id == self.research_run_id
        return {
            "restaurant_id": restaurant_id,
            "qualification_run_id": qualification_run_id,
            "research_run_id": research_run_id,
            "qualification": "Qualified for Rawaj.",
            "marketing_gaps": [],
        }

    def get_or_create_relationship(
        self,
        *,
        restaurant_id: int,
        research_run_id: int,
        qualification_run_id: int,
    ) -> dict[str, Any]:
        if self.memory is None:
            self.memory = RelationshipMemory(
                relationship_id="relationship_demo",
                restaurant_id=restaurant_id,
            )
        self.research_run_id = research_run_id
        self.qualification_run_id = qualification_run_id
        return self._memory_payload()

    def read_relationship_memory(self, *, restaurant_id: int) -> dict[str, Any] | None:
        if self.memory is None:
            return None
        assert restaurant_id == self.memory.restaurant_id
        return self._memory_payload()

    def write_relationship_memory(
        self,
        *,
        memory: Any,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        candidate = RelationshipMemory.model_validate(memory)
        if self.memory is None:
            raise ValueError("relationship missing")
        if expected_version is not None and expected_version != self.memory.version:
            raise ValueError("unexpected version")
        self.memory = candidate.model_copy(update={"version": self.memory.version + 1})
        return self._memory_payload()

    def save_outbound_message(
        self,
        *,
        draft: Any,
        provenance: Any,
        content_sha256: str,
        supersedes_message_id: str | None = None,
    ) -> dict[str, Any]:
        data = draft.model_dump(mode="json")
        existing = self.messages.get(data["message_id"])
        if existing is not None:
            return existing
        record = {
            **data,
            # Match the real repository's immutable provenance at send time.
            "research_run_id": provenance["research_run_id"],
            "qualification_run_id": provenance["qualification_run_id"],
            "content_version": data["revision"],
            "content_sha256": content_sha256,
            "status": "GENERATED",
            "supersedes_message_id": supersedes_message_id,
        }
        self.messages[data["message_id"]] = record
        return record

    def save_human_approval(self, *, approval: Any) -> dict[str, Any]:
        data = approval.model_dump(mode="json")
        message = self.messages[data["message_id"]]
        assert message["content_sha256"] == data["content_sha256"]
        self.approvals[data["approval_id"]] = data
        message["status"] = data["status"]
        return {
            "approval_id": data["approval_id"],
            "message_id": data["message_id"],
            "status": data["status"],
            "content_sha256": data["content_sha256"],
        }

    def get_sendable_message(self, *, message_id: str, approval_id: str) -> dict[str, Any] | None:
        message = self.messages.get(message_id)
        approval = self.approvals.get(approval_id)
        if not message or not approval or message["status"] != "APPROVED":
            return None
        if self.memory and not self.memory.promotional_contact_allowed:
            return None
        message["status"] = "SENDING"
        return message

    def record_outbound_execution(self, execution: Any) -> dict[str, Any]:
        data = execution.model_dump(mode="json")
        message = self.messages[data["message_id"]]
        if message["status"] in {"SENT", "FAILED"}:
            return message
        assert message["status"] == "SENDING"
        message["status"] = data["email_status"]
        message["provider_name"] = data["provider_name"]
        message["provider_message_id"] = data["provider_message_id"]
        if data["success"] and message["message_type"] in {
            "INITIAL_OUTREACH",
            "NO_RESPONSE_FOLLOW_UP",
        }:
            self.memory = self.memory.model_copy(
                update={"outreach_attempts": self.memory.outreach_attempts + 1}
            )
        return message

    def get_button_event(self, *, event_id: str) -> dict[str, Any] | None:
        return self.button_events.get(event_id)

    def mark_button_event_applied(self, *, event_id: str) -> dict[str, Any]:
        event = self.button_events[event_id]
        if event["status"] == "RECEIVED":
            event["status"] = "APPLIED"
        return event

    def create_strategy_request_if_absent(self, handoff: Any) -> dict[str, Any]:
        data = handoff.model_dump(mode="json")
        existing = self.strategy_requests.get(data["interest_event_id"])
        if existing:
            return {**existing, "created": False}
        record = {**data, "created": True, "relationship_id": "relationship_demo"}
        self.strategy_requests[data["interest_event_id"]] = record
        self.memory = self.memory.model_copy(
            update={
                "strategy_request_id": data["strategy_request_id"],
                "status": RelationshipStatus.AWAITING_STRATEGY_OUTPUT,
            }
        )
        return record

    def record_strategy_dispatch(
        self,
        *,
        strategy_request_id: str,
        dispatch: dict[str, Any],
    ) -> dict[str, Any]:
        for event_id, request in self.strategy_requests.items():
            if request["strategy_request_id"] == strategy_request_id:
                request["status"] = "HANDED_OFF"
                request["dispatch_metadata"] = {"status": dispatch["status"]}
                return request
        raise ValueError("strategy request missing")

    def load_strategy_request(self, *, strategy_request_id: str) -> dict[str, Any] | None:
        for request in self.strategy_requests.values():
            if request["strategy_request_id"] == strategy_request_id:
                return request
        return None

    def load_strategy_handoff(self, *, strategy_request_id: str) -> dict[str, Any] | None:
        request = self.load_strategy_request(strategy_request_id=strategy_request_id)
        return request.get("strategy_output") if request else None

    def record_strategy_output(
        self,
        *,
        strategy_request_id: str,
        strategy_output: dict[str, Any],
    ) -> dict[str, Any]:
        request = self.load_strategy_request(strategy_request_id=strategy_request_id)
        if request is None:
            raise ValueError("strategy request missing")
        request["strategy_output"] = dict(strategy_output)
        request["status"] = "READY"
        self.memory = self.memory.model_copy(
            update={
                "strategy_id": strategy_output["strategy_id"],
                "strategy_status": StrategyOutputStatus.READY,
                "status": RelationshipStatus.STRATEGY_READY,
            }
        )
        return request

    def create_human_escalation(self, escalation: Any) -> dict[str, Any]:
        data = escalation.model_dump(mode="json")
        self.escalations.append(data)
        self.memory = self.memory.model_copy(
            update={"status": RelationshipStatus.ESCALATED_TO_HUMAN}
        )
        return {
            "escalation_id": data["escalation_id"],
            "restaurant_id": data["restaurant_id"],
            "status": "OPEN",
            "severity": data["severity"],
        }

    def _memory_payload(self) -> dict[str, Any]:
        assert self.memory is not None
        return {
            **self.memory.model_dump(mode="json"),
            "research_run_id": self.research_run_id,
            "qualification_run_id": self.qualification_run_id,
        }


class RecordingDispatcher:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []

    def dispatch_strategy_request(self, request: dict[str, Any]) -> dict[str, Any]:
        self.requests.append(dict(request))
        return {
            "status": "DISPATCHED_TO_TEST_OUTBOX",
            "strategy_request_id": request["strategy_request_id"],
        }


class FakeEmailService:
    """Use trusted local rendering, while pretending only in explicit test code."""

    def __init__(self, settings: RawajSettings) -> None:
        self._renderer = EmailService(settings)
        self.sent: list[str] = []

    def prepare_for_approval(self, draft: Any) -> Any:
        return self._renderer.prepare_for_approval(draft)

    def send_approved_message(self, message: dict[str, Any]) -> dict[str, Any]:
        self.sent.append(message["message_id"])
        return {
            "success": True,
            "provider": "test_provider",
            "provider_message_id": f"provider_{message['message_id']}",
            "provider_status": "ACCEPTED_BY_TEST_PROVIDER",
        }


class DeterministicLLM:
    """Explicit model double used only by these offline tests."""

    def __init__(self, *, ask_for_calendar: bool = False) -> None:
        self.ask_for_calendar = ask_for_calendar

    def decide(self, context: dict[str, Any]) -> OutreachDecision:
        trigger = context["trigger"]
        if trigger == "CLIENT_MESSAGE_RECEIVED":
            return OutreachDecision(
                action=ActionType.REVIEW_CLIENT_ISSUE,
                next_status=RelationshipStatus.PENDING_OUTBOUND_APPROVAL,
                decision_basis=["Client message supplied"],
                language="en",
            )
        if trigger == "FOLLOW_UP_DUE":
            return OutreachDecision(
                action=ActionType.SEND_FOLLOW_UP,
                next_status=RelationshipStatus.PENDING_OUTBOUND_APPROVAL,
                decision_basis=["A due follow-up has fresh grounded context."],
                language="en",
                personalization_observation_ids=["menu_observation"],
            )
        return OutreachDecision(
            action=ActionType.SEND_INITIAL_OUTREACH,
            next_status=RelationshipStatus.PENDING_OUTBOUND_APPROVAL,
            decision_basis=["Grounded test decision"],
            language="en",
            read_calendar=self.ask_for_calendar,
            personalization_observation_ids=["menu_observation"],
        )

    def generate_email(self, context: dict[str, Any]) -> EmailContentProposal:
        message_type = context["message_type"]
        body = (
            "Hi Demo Cafe team,\n\n"
            "We are Rawaj, and we noticed your public seasonal iced-drink posts. "
            "There is a thoughtful opportunity to build on that interest with our "
            "complimentary Free Trial.\n\nPlease choose an option below."
        )
        if message_type == "STRATEGY_READY_NOTIFICATION":
            body = "Hi Demo Cafe team,\n\nYour Rawaj complimentary Free Trial is ready."
        return EmailContentProposal(
            subject=f"Rawaj | {message_type}",
            plain_text_body=body,
            language=context["language"],
            personalization_observation_ids=context["observation_ids"],
        )

    def review_email(
        self, *, draft: dict[str, Any], customer_safe_context: dict[str, Any]
    ) -> MessageReview:
        return MessageReview(
            passed=True,
            privacy_safe=True,
            approved_fact_references=customer_safe_context["observation_ids"],
        )


class ActionChoosingLLM(DeterministicLLM):
    """Test-only LLM double that makes an explicit permitted next-action choice.

    This lets the offline suite prove that a due trigger alone does not cause a
    message: the LangGraph decision must select a send action first.  It also
    records the generation context used after an optional Calendar read.
    """

    def __init__(
        self,
        *,
        follow_up_action: ActionType | None = None,
        scheduled_check_action: ActionType | None = None,
        ask_for_calendar: bool = False,
    ) -> None:
        super().__init__(ask_for_calendar=ask_for_calendar)
        self.follow_up_action = follow_up_action
        self.scheduled_check_action = scheduled_check_action
        self.decision_contexts: list[dict[str, Any]] = []
        self.generation_contexts: list[dict[str, Any]] = []

    def decide(self, context: dict[str, Any]) -> OutreachDecision:
        self.decision_contexts.append(dict(context))
        trigger = context["trigger"]
        if trigger == "FOLLOW_UP_DUE" and self.follow_up_action is not None:
            action = self.follow_up_action
            return OutreachDecision(
                action=action,
                next_status=(
                    RelationshipStatus.PENDING_OUTBOUND_APPROVAL
                    if action is ActionType.SEND_FOLLOW_UP
                    else RelationshipStatus.WAITING_FOR_RESPONSE
                ),
                decision_basis=["Explicit deterministic follow-up decision."],
                language="en",
                personalization_observation_ids=["menu_observation"]
                if action is ActionType.SEND_FOLLOW_UP
                else [],
            )
        if (
            trigger == "SCHEDULED_CLIENT_CHECK"
            and self.scheduled_check_action is not None
        ):
            action = self.scheduled_check_action
            return OutreachDecision(
                action=action,
                next_status=RelationshipStatus.ACTIVE_CLIENT,
                decision_basis=["Explicit deterministic client-check decision."],
                language="en",
            )
        return super().decide(context)

    def generate_email(self, context: dict[str, Any]) -> EmailContentProposal:
        self.generation_contexts.append(dict(context))
        return super().generate_email(context)


class WorkflowOfflineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = RawajSettings(
            environment="test",
            public_base_url="http://localhost:8000",
            button_signing_secret="x" * 32,
            allow_local_button_demo=True,
            follow_up_delay_minutes=2,
            follow_up_max_attempts=3,
        )
        self.repository = InMemoryRepository()
        self.email = FakeEmailService(self.settings)
        self.dispatcher = RecordingDispatcher()
        self.workflow = OutreachFollowUpWorkflow(
            settings=self.settings,
            repository=self.repository,
            llm=DeterministicLLM(),
            email_service=self.email,
            strategy_dispatcher=self.dispatcher,
            checkpointer=MemorySaver(),
            access_provider=lambda _: {"username": "demo_rawaj", "password": "test-password", "login_url": "https://rawaj.test"},
        )

    def _start_and_approve(self) -> Any:
        result = self.workflow.start_outreach(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
        )
        draft = result.email_draft
        assert draft is not None
        content_sha256 = self.repository.messages[draft.message_id]["content_sha256"]
        return self.workflow.resume_human_approval(
            thread_id=result.thread_id,
            reviewer_id="team-reviewer",
            decision="APPROVED",
            message_id=draft.message_id,
            message_revision=draft.revision,
            content_sha256=content_sha256,
        )

    def test_initial_outreach_is_english_button_email_and_pauses(self) -> None:
        result = self.workflow.start_outreach(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
        )
        self.assertEqual(result.relationship_memory.status, RelationshipStatus.PENDING_OUTBOUND_APPROVAL)
        self.assertEqual(result.email_draft.message_type.value, "INITIAL_OUTREACH")
        self.assertEqual(result.email_draft.language, "en")
        self.assertEqual({button.action.value for button in result.email_draft.buttons}, {"INTERESTED", "NOT_INTERESTED"})
        self.assertEqual(
            [button.label for button in result.email_draft.buttons],
            ["Yes, I'm interested", "No, thank you"],
        )
        self.assertEqual(self.email.sent, [])
        self.assertEqual(result.errors, [])

    def test_only_provider_evidence_marks_message_sent(self) -> None:
        result = self._start_and_approve()
        self.assertEqual(result.execution.email_status.value, "SENT")
        self.assertEqual(len(self.email.sent), 1)
        self.assertEqual(result.relationship_memory.status, RelationshipStatus.WAITING_FOR_RESPONSE)
        self.assertEqual(result.relationship_memory.outreach_attempts, 1)
        self.assertIsNotNone(result.relationship_memory.next_contact_at)

    def test_interested_starts_strategy_handoff_without_inventing_strategy(self) -> None:
        sent = self._start_and_approve()
        message_id = sent.execution.message_id
        event = ButtonClickEvent(
            event_id="button_interested",
            restaurant_id=1,
            outreach_message_id=message_id,
            action=ButtonAction.INTERESTED,
            token_id="verified-token",
            status=ButtonEventStatus.RECEIVED,
        )
        self.repository.button_events[event.event_id] = event.model_dump(mode="json")
        result = self.workflow.resume_from_button_event(event)
        self.assertIsNone(result.email_draft)
        self.assertEqual(result.relationship_memory.status, RelationshipStatus.AWAITING_STRATEGY_OUTPUT)
        self.assertEqual(len(self.dispatcher.requests), 1)
        self.assertEqual(self.dispatcher.requests[0]["handoff_type"], "STRATEGY_REQUEST")
        self.assertNotIn("strategy", self.dispatcher.requests[0].get("customer_context", {}))

    def test_valid_strategy_output_sends_strategy_ready_email_automatically(self) -> None:
        self.test_interested_starts_strategy_handoff_without_inventing_strategy()
        request = next(iter(self.repository.strategy_requests.values()))
        output = StrategyOutputHandoff(
            strategy_id=777,
            strategy_request_id=request["strategy_request_id"],
            restaurant_id=1,
            status=StrategyOutputStatus.READY,
            dashboard_strategy_url="https://dashboard.rawaj.test/strategy/777",
            client_notification_allowed=True,
        )
        result = self.workflow.receive_strategy_output(output)
        self.assertEqual(result.email_draft.message_type.value, "STRATEGY_READY_NOTIFICATION")
        self.assertIn("complimentary Free Trial", result.email_draft.plain_text_body)
        self.assertIn("https://dashboard.rawaj.test/strategy/777", result.email_draft.plain_text_body)
        self.assertEqual(len(self.email.sent), 2)
        self.assertTrue(result.execution.success)

    def test_due_follow_up_waits_until_due_and_escalates_at_limit(self) -> None:
        self._start_and_approve()
        future = self.repository.memory.model_copy(
            update={"next_contact_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
        )
        self.repository.memory = future
        not_due = self.workflow.run_follow_up_due(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
        )
        self.assertIsNone(not_due.email_draft)

        self.repository.memory = future.model_copy(
            update={
                "next_contact_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),
                "outreach_attempts": self.settings.follow_up_max_attempts,
            }
        )
        at_limit = self.workflow.run_follow_up_due(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
        )
        self.assertIsNone(at_limit.email_draft)
        self.assertEqual(at_limit.escalation.reason_code.value, "MAX_NO_RESPONSE_ATTEMPTS")

    def test_due_follow_up_sends_even_when_model_suggests_wait(self):
        self._start_and_approve()
        self.repository.memory = self.repository.memory.model_copy(update={
            "next_contact_at": (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()})
        self.workflow.llm = ActionChoosingLLM(follow_up_action=ActionType.WAIT)
        result = self.workflow.run_follow_up_due(restaurant_id=1, research_run_id=101, qualification_run_id=202)
        self.assertEqual(result.decision.action, ActionType.SEND_FOLLOW_UP)
        self.assertTrue(result.execution.success)
        self.assertEqual(len(self.email.sent), 2)

    def test_scheduled_client_check_requests_feedback_automatically(self) -> None:
        """An active client check is context-aware, not a routine weekly email."""

        self.repository.get_or_create_relationship(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
        )
        self.repository.memory = self.repository.memory.model_copy(
            update={
                "status": RelationshipStatus.ACTIVE_CLIENT,
                "next_contact_at": (
                    datetime.now(timezone.utc) - timedelta(minutes=1)
                ).isoformat(),
            }
        )
        self.workflow.llm = ActionChoosingLLM(
            scheduled_check_action=ActionType.WAIT
        )

        result = self.workflow.run_scheduled_client_check(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
            thread_id="scheduled-client-check-wait",
        )
        self.assertEqual(result.decision.action, ActionType.REQUEST_FEEDBACK)
        self.assertTrue(result.execution.success)
        self.assertEqual(len(self.email.sent), 1)

    def test_unavailable_calendar_never_invents_an_event_for_the_draft(self) -> None:
        """Calendar enrichment is optional: no credential means no fabricated occasion."""

        calendar_llm = ActionChoosingLLM(ask_for_calendar=True)
        self.workflow.llm = calendar_llm
        # No Calendar service was injected into this workflow in setUp.
        result = self.workflow.start_outreach(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
            thread_id="calendar-unavailable",
        )

        self.assertIsNotNone(result.email_draft)
        self.assertGreaterEqual(len(calendar_llm.decision_contexts), 2)
        self.assertTrue(
            any(context["calendar_checked"] for context in calendar_llm.decision_contexts)
        )
        self.assertEqual(calendar_llm.generation_contexts[-1]["calendar_context"], [])
        self.assertEqual(calendar_llm.generation_contexts[-1]["calendar_event_ids"], [])
        self.assertNotIn("World Coffee Day", result.email_draft.plain_text_body)

    def test_sensitive_client_issue_creates_internal_review_without_email(self) -> None:
        self.repository.get_or_create_relationship(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
        )
        self.repository.memory = self.repository.memory.model_copy(
            update={"status": RelationshipStatus.ACTIVE_CLIENT}
        )
        result = self.workflow.handle_client_message(
            restaurant_id=1,
            research_run_id=101,
            qualification_run_id=202,
            client_message="We lost 40% of our sales and want a refund.",
        )
        self.assertIsNone(result.email_draft)
        self.assertEqual(result.escalation.reason_code.value, "REFUND_OR_FINANCIAL_LOSS")
        self.assertEqual(result.relationship_memory.status, RelationshipStatus.ESCALATED_TO_HUMAN)
        self.assertEqual(self.email.sent, [])
        self.assertEqual(len(self.repository.escalations), 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
