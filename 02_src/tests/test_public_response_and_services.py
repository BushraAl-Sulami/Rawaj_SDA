"""Offline safety checks for email buttons and optional Google Calendar access."""

from __future__ import annotations

import re
import unittest
from datetime import datetime
from urllib.parse import parse_qs, urlparse

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agents.outreach_followup_agent.calendar_service import CalendarService
from agents.outreach_followup_agent.config import RawajSettings
from agents.outreach_followup_agent.email_service import EmailService
from agents.outreach_followup_agent.response_api import create_response_router
from agents.outreach_followup_agent.schemas import ButtonAction


class FakeCalendarEvents:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.items = items
        self.list_kwargs: dict[str, object] | None = None

    def list(self, **kwargs: object) -> "FakeCalendarEvents":
        self.list_kwargs = kwargs
        return self

    def execute(self) -> dict[str, object]:
        return {"items": self.items}


class FakeCalendarClient:
    def __init__(self, items: list[dict[str, object]]) -> None:
        self.events_api = FakeCalendarEvents(items)

    def events(self) -> FakeCalendarEvents:
        return self.events_api


class FakeButtonRepository:
    def __init__(self) -> None:
        self.confirmations: dict[str, dict[str, str | None]] = {}
        self.recorded_events: list[dict[str, object]] = []

    def create_button_confirmation(self, **kwargs: str) -> dict[str, str]:
        self.confirmations[kwargs["confirmation_id"]] = {
            "email_token_sha256": kwargs["email_token_sha256"],
            "outreach_message_id": kwargs["outreach_message_id"],
            "consumed": None,
        }
        return {"status": "ISSUED"}

    def consume_button_confirmation(self, **kwargs: str) -> dict[str, object]:
        record = self.confirmations.get(kwargs["confirmation_id"])
        if (
            record is None
            or record["consumed"] is not None
            or record["email_token_sha256"] != kwargs["email_token_sha256"]
            or record["outreach_message_id"] != kwargs["outreach_message_id"]
        ):
            return {"consumed": False}
        record["consumed"] = "yes"
        return {"consumed": True, "status": "CONSUMED"}

    def record_button_event(self, *, event: object) -> dict[str, object]:
        payload = event.model_dump(mode="json")
        self.recorded_events.append(payload)
        return {"accepted": True, "status": "RECEIVED", "event": payload}


class FakeWorkflowDispatcher:
    def __init__(self) -> None:
        self.events: list[dict[str, object]] = []

    def resume_from_button_event(self, event: dict[str, object]) -> None:
        self.events.append(event)


class PublicResponseAndServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = RawajSettings(
            environment="test",
            public_base_url="http://testserver",
            button_signing_secret="x" * 32,
            rawaj_events_calendar_id="rawaj-events@example.com",
            google_service_account_file="test-service-account.json",
        )

    def test_email_buttons_are_signed_and_missing_provider_does_not_send(self) -> None:
        service = EmailService(self.settings)
        prepared = service.prepare_for_approval(
            {
                "message_id": "outbound_service_test",
                "relationship_id": "relationship_service_test",
                "restaurant_id": 101,
                "message_type": "INITIAL_OUTREACH",
                "action": "SEND_INITIAL_OUTREACH",
                "recipient": "owner@example.com",
                "subject": "Rawaj for Demo Cafe",
                "html_body": "<pending trusted rendering>",
                "plain_text_body": "Hi Demo Cafe team,\\n\\nWe are Rawaj.",
                "language": "en",
                "buttons": [],
            }
        )

        self.assertEqual(
            [button.label for button in prepared.draft.buttons],
            ["Yes, I'm interested", "No, thank you"],
        )
        self.assertEqual(
            [button.action for button in prepared.draft.buttons],
            [ButtonAction.INTERESTED, ButtonAction.NOT_INTERESTED],
        )
        for button in prepared.draft.buttons:
            token = parse_qs(urlparse(button.url).query)["token"][0]
            claims = service.verify_button_token(token)
            self.assertEqual(claims.outreach_message_id, "outbound_service_test")
            self.assertEqual(claims.action, button.action)

        provider_result = service.send_approved_message(
            {
                "status": "SENDING",
                "recipient": "owner@example.com",
                "subject": "Rawaj for Demo Cafe",
                "html_body": prepared.draft.html_body,
                "plain_text_body": prepared.draft.plain_text_body,
            }
        )
        self.assertFalse(provider_result["success"])
        self.assertEqual(provider_result["failure_code"], "EMAIL_NOT_CONFIGURED")

    def test_response_get_is_read_only_and_post_is_single_use(self) -> None:
        email_service = EmailService(self.settings)
        # Build a direct signed token without a browser or a live provider.
        from agents.outreach_followup_agent.email_service import ButtonTokenSigner

        signed_token, _ = ButtonTokenSigner(
            self.settings.button_signing_secret or ""
        ).issue(
            restaurant_id=101,
            outreach_message_id="outbound_response_test",
            action=ButtonAction.INTERESTED,
            expires_in_days=1,
        )

        repository = FakeButtonRepository()
        dispatcher = FakeWorkflowDispatcher()
        app = FastAPI()
        app.include_router(
            create_response_router(
                email_service=email_service,
                repository=repository,
                workflow_dispatcher=dispatcher,
            )
        )
        client = TestClient(app)

        opened = client.get(f"/api/outreach/response?token={signed_token}")
        self.assertEqual(opened.status_code, 200)
        self.assertEqual(repository.recorded_events, [])
        self.assertEqual(opened.headers["cache-control"], "no-store, max-age=0")

        match = re.search(
            r'name="confirmation" value="([^"]+)"',
            opened.text,
        )
        self.assertIsNotNone(match)
        confirmation = match.group(1) if match else ""

        recorded = client.post(
            "/api/outreach/response",
            data={"token": signed_token, "confirmation": confirmation},
        )
        self.assertEqual(recorded.status_code, 200)
        self.assertEqual(len(repository.recorded_events), 1)
        self.assertEqual(len(dispatcher.events), 1)

        replay = client.post(
            "/api/outreach/response",
            data={"token": signed_token, "confirmation": confirmation},
        )
        self.assertEqual(replay.status_code, 409)
        self.assertEqual(len(repository.recorded_events), 1)

    def test_calendar_returns_only_relevant_public_event_fields(self) -> None:
        client = FakeCalendarClient(
            [
                {
                    "id": "national-day",
                    "summary": "Saudi National Day",
                    "start": {"date": "2026-09-23"},
                    "end": {"date": "2026-09-24"},
                    "description": "Internal notes must never leave Calendar.",
                },
                {
                    "id": "private-budget",
                    "summary": "Internal budget review",
                    "start": {"date": "2026-09-20"},
                    "end": {"date": "2026-09-21"},
                    "description": "Private finance data.",
                },
            ]
        )
        service = CalendarService(
            self.settings,
            service_factory=lambda write: client,
            now_provider=lambda: datetime.fromisoformat("2026-09-01T09:00:00+03:00"),
        )

        events = service.get_relevant_events(
            restaurant_id=101,
            start_at=datetime.fromisoformat("2026-09-01T09:00:00+03:00"),
            end_at=datetime.fromisoformat("2026-09-30T09:00:00+03:00"),
            restaurant_context={"category": "Specialty coffee", "location": "Jeddah"},
        )

        self.assertEqual([event["event_id"] for event in events], ["national-day"])
        self.assertIsNone(events[0]["description"])
        self.assertIsNone(events[0]["source_url"])
        self.assertNotIn(
            "description",
            str(client.events_api.list_kwargs or {}).lower(),
        )

    def test_calendar_write_never_runs_without_a_persisted_approval_guard(self) -> None:
        write_settings = RawajSettings(
            environment="test",
            rawaj_events_calendar_id="rawaj-events@example.com",
            google_service_account_file="test-service-account.json",
            google_calendar_write_enabled=True,
        )
        client = FakeCalendarClient([])
        service = CalendarService(
            write_settings,
            service_factory=lambda write: client,
            calendar_write_guard=None,
        )

        result = service.create_approved_event(
            title="Rawaj strategy discussion",
            start_at=datetime.fromisoformat("2026-09-20T10:00:00+03:00"),
            end_at=datetime.fromisoformat("2026-09-20T10:30:00+03:00"),
            relevance_reason="A human confirmed a real meeting.",
            approval_id="approval_123",
            idempotency_key="calendar_write_123",
        )

        self.assertFalse(result["created"])
        self.assertEqual(result["status"], "NOT_WRITTEN_APPROVAL_GUARD_NOT_CONFIGURED")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
