"""Requested journey against real SQLite persistence; only external providers are faked."""
import os
from datetime import datetime, timedelta, timezone
from functools import partial
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select, func

from test_team_repository_integration import TeamRepositoryIntegrationTests as Seed
from test_workflow_offline import DeterministicLLM, FakeEmailService, RecordingDispatcher
from agents.outreach_followup_agent.config import RawajSettings
from agents.outreach_followup_agent.schemas import ButtonClickEvent, ButtonAction, StrategyOutputHandoff
from agents.outreach_followup_agent.workflow import OutreachFollowUpWorkflow, WorkflowSafetyError
from api import accounts
from database.models import ClientTrial, ClientFeedbackRecord, OutreachRelationship, OutboundMessage, OutreachEvent


@pytest.fixture
def journey():
    seed = Seed()
    seed.setUp()
    settings = RawajSettings(environment="test", public_base_url="http://localhost:8000",
        button_signing_secret="x" * 32, allow_local_button_demo=True, follow_up_delay_minutes=2)
    email = FakeEmailService(settings)
    model = DeterministicLLM()
    workflow = OutreachFollowUpWorkflow(settings=settings, repository=seed.repository, llm=model,
        email_service=email, strategy_dispatcher=RecordingDispatcher(), checkpointer=MemorySaver(),
        access_provider=partial(accounts.provision_access, session_factory=seed.Session))
    ids = dict(restaurant_id=seed.restaurant_id, research_run_id=seed.research_run_id,
               qualification_run_id=seed.qualification_run_id)
    with patch.dict(os.environ, {"ACCOUNT_SECRET": "test-only-secret-" * 4}):
        yield seed, workflow, email, ids
    seed.tearDown()


def decide(workflow, result, decision="APPROVED", note=None):
    state = workflow.graph.get_state(workflow._graph_config(result.thread_id)).values
    request = state["approval_request"]
    return workflow.resume_human_approval(thread_id=result.thread_id, reviewer_id="fatimah",
        decision=decision, note=note, **{k: request[k] for k in ("message_id", "message_revision", "content_sha256")})


def interest(seed, workflow, sent, action=ButtonAction.INTERESTED):
    event = ButtonClickEvent(restaurant_id=seed.restaurant_id, outreach_message_id=sent.email_draft.message_id,
        action=action, token_id="test-verified-token")
    seed.repository.record_button_event(event=event)
    return workflow.resume_from_button_event(event)


def test_reject_requires_reason_rewrites_until_approved(journey):
    seed, workflow, email, ids = journey
    draft = workflow.start_outreach(**ids)
    assert not draft.errors and not email.sent
    with pytest.raises(WorkflowSafetyError, match="rejection reason"):
        decide(workflow, draft, "REJECTED")
    for _ in range(2):
        revised = decide(workflow, draft, "REJECTED", "Make the hook more concise")
        assert not revised.errors
        assert revised.email_draft.message_id != draft.email_draft.message_id
        assert revised.approval is None and not email.sent
        state = workflow.graph.get_state(workflow._graph_config(revised.thread_id)).values
        assert state["revision_feedback"] == "Make the hook more concise"
        draft = revised
    sent = decide(workflow, draft)
    assert not sent.errors and sent.execution.success and len(email.sent) == 1
    workflow.start_outreach(**ids)
    assert len(email.sent) == 1


def test_due_followup_sends_without_human_and_no_stops_contact(journey):
    seed, workflow, email, ids = journey
    sent = decide(workflow, workflow.start_outreach(**ids))
    assert workflow.run_follow_up_due(**ids).email_draft is None
    with seed.Session() as db:
        row = db.scalar(select(OutreachRelationship))
        row.next_contact_at = datetime.utcnow() - timedelta(seconds=1)
        db.commit()
    followup = workflow.run_follow_up_due(**ids)
    assert not followup.errors and followup.execution.success
    assert len(email.sent) == 2 and len(followup.email_draft.buttons) == 2
    assert not followup.decision.requires_human_approval
    interest(seed, workflow, followup, ButtonAction.NOT_INTERESTED)
    stopped = workflow.run_follow_up_due(**ids)
    assert stopped.relationship_memory.status.value == "DO_NOT_CONTACT"
    assert len(email.sent) == 2
    with seed.Session() as db:
        assert db.scalar(select(func.count()).select_from(OutreachEvent).where(OutreachEvent.event_type == "AUTOMATIC_AUTHORIZATION")) == 1


def test_yes_strategy_automatic_email_activation_feedback_and_replay(journey):
    seed, workflow, email, ids = journey
    sent = decide(workflow, workflow.start_outreach(**ids))
    interested = interest(seed, workflow, sent)
    assert not interested.errors
    output = StrategyOutputHandoff(strategy_id=77, restaurant_id=seed.restaurant_id,
        strategy_request_id=interested.strategy_request.strategy_request_id, status="READY",
        dashboard_strategy_url="https://rawaj.example/strategy", client_notification_allowed=True)
    ready = workflow.receive_strategy_output(output)
    assert not ready.errors and ready.execution.success
    assert "30 days from activation" in ready.email_draft.plain_text_body
    assert "Password:" in ready.email_draft.plain_text_body
    assert "do not share" in ready.email_draft.plain_text_body
    with seed.Session() as db:
        assert db.get(ClientTrial, seed.restaurant_id) is None
    workflow.receive_strategy_output(output)
    assert len(email.sent) == 2
    access = accounts.provision_access(seed.restaurant_id, session_factory=seed.Session)
    with seed.Session() as db:
        accounts.authenticate(db, access["rawaj_username"], access["rawaj_password"])
        trial = db.get(ClientTrial, seed.restaurant_id)
        activated = trial.activated_at
        assert trial.expires_at - activated == timedelta(days=30)
        accounts.authenticate(db, access["username"], access["password"])
        assert db.get(ClientTrial, seed.restaurant_id).activated_at == activated
        trial.feedback_due_at = datetime.utcnow() - timedelta(seconds=1)
        row = db.scalar(select(OutreachRelationship))
        row.next_contact_at = trial.feedback_due_at
        db.commit()
    feedback = workflow.run_scheduled_client_check(**ids)
    assert not feedback.errors and feedback.execution.success
    assert feedback.email_draft.message_type.value == "FEEDBACK_REQUEST"
    assert len(email.sent) == 3
    workflow.run_scheduled_client_check(**ids)
    assert len(email.sent) == 3
    with seed.Session() as db:
        assert db.get(ClientTrial, seed.restaurant_id).feedback_requested_at


def test_production_demo_defaults():
    with patch("agents.outreach_followup_agent.config.load_dotenv", return_value=False), patch.dict(os.environ, {"RAWAJ_ENV": "demo"}):
        os.environ.pop("FOLLOW_UP_DELAY_MINUTES", None)
        assert RawajSettings.from_environment().follow_up_delay_minutes == 2
        os.environ["RAWAJ_ENV"] = "production"
        assert RawajSettings.from_environment().follow_up_delay_minutes == 10080
        os.environ["FOLLOW_UP_DELAY_MINUTES"] = "5"
        assert RawajSettings.from_environment().follow_up_delay_minutes == 5


def test_automatic_policy_never_approves_initial(journey):
    seed, workflow, email, ids = journey
    draft = workflow.start_outreach(**ids)
    with pytest.raises(WorkflowSafetyError, match="human reviewer"):
        workflow.resume_human_approval(thread_id=draft.thread_id, reviewer_id="auto:ai-review", decision="APPROVED")
    assert not email.sent


def test_feedback_api_authentication_storage_and_duplicate(journey):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from api.client_lifecycle import router
    seed, workflow, email, ids = journey
    seed.repository.get_or_create_relationship(**ids)
    access = accounts.provision_access(seed.restaurant_id, session_factory=seed.Session)
    with seed.Session() as db:
        account = accounts.authenticate(db, access["username"], access["password"])
        token = accounts.owner_token(account)
    # File-backed SQLite is used here because TestClient handles requests in another thread.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database.database import Base
    from database.models import Restaurant, UserAccount
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as tmp:
        engine = create_engine("sqlite:///" + (Path(tmp) / "api.db").as_posix())
        Base.metadata.create_all(engine)
        sessions = sessionmaker(engine)
        with seed.Session() as source, sessions() as db:
            user = source.scalar(select(UserAccount))
            db.add(Restaurant(id=1, name="Cafe", instagram_username="cafe", instagram_url="https://instagram.com/cafe"))
            db.add(UserAccount(restaurant_id=1, username=user.username, password_hash=user.password_hash))
            db.commit()
            accounts.activate_trial(db, 1)
            db.commit()
        app = FastAPI()
        app.state.session_factory = sessions
        app.include_router(router)
        with TestClient(app) as client:
            assert client.post("/api/client/feedback", json={"message": "Good"}).status_code == 401
            headers = {"Authorization": "Bearer " + token}
            assert client.get("/api/client/trial", headers=headers).status_code == 200
            for _ in range(2):
                response = client.post("/api/client/feedback", headers=headers, json={"message": "Useful plan", "rating": 5})
                assert response.status_code == 200
            assert client.post("/api/client/feedback", headers=headers, json={"message": "  "}).status_code == 422
        with sessions() as db:
            assert db.scalar(select(func.count()).select_from(ClientFeedbackRecord)) == 1
        engine.dispose()


def test_failed_content_review_never_sends(journey):
    from agents.outreach_followup_agent.schemas import MessageReview
    seed, workflow, email, ids = journey
    workflow.llm.review_email = lambda **_: MessageReview(passed=False, privacy_safe=True, issues=["Unsupported claim"])
    result = workflow.start_outreach(**ids)
    assert result.errors and not email.sent


def test_two_workflows_keep_their_own_services(journey):
    seed, first, email, ids = journey
    other = Seed()
    other.setUp()
    second_email = FakeEmailService(first.settings)
    try:
        OutreachFollowUpWorkflow(settings=first.settings, repository=other.repository,
            llm=DeterministicLLM(), email_service=second_email, checkpointer=MemorySaver())
        result = decide(first, first.start_outreach(**ids))
        assert not result.errors and len(email.sent) == 1 and not second_email.sent
        assert other.repository.read_relationship_memory(restaurant_id=other.restaurant_id) is None
    finally:
        other.tearDown()


def test_recorded_yes_is_recovered_after_dispatch_crash(journey):
    from agents.outreach_followup_agent.scheduler import OutreachFollowUpScheduler
    seed, workflow, email, ids = journey
    sent = decide(workflow, workflow.start_outreach(**ids))
    event = ButtonClickEvent(restaurant_id=seed.restaurant_id, outreach_message_id=sent.email_draft.message_id,
        action=ButtonAction.INTERESTED, token_id="verified-before-crash")
    seed.repository.record_button_event(event=event)
    assert seed.repository.read_relationship_memory(restaurant_id=seed.restaurant_id)["next_contact_at"] is None
    OutreachFollowUpScheduler(workflow=workflow, repository=seed.repository).recover_pending_responses_once()
    assert seed.repository.read_relationship_memory(restaurant_id=seed.restaurant_id)["status"] == "AWAITING_STRATEGY_OUTPUT"
    assert not seed.repository.list_pending_button_events()


def test_strategy_worker_creates_durable_notification_and_reuses_saved_strategy(journey, tmp_path):
    import json
    from dataclasses import replace
    from agents.strategy_agent import strategy_worker
    from database import database, outreach_repository
    from database.repository import save_strategy_result
    seed, workflow, email, ids = journey
    sent = decide(workflow, workflow.start_outreach(**ids))
    result = interest(seed, workflow, sent)
    handoff = result.strategy_request
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    path = inbox / (handoff.strategy_request_id + ".json")
    path.write_text(handoff.model_dump_json(), encoding="utf-8")
    calls = []
    def generate(request):
        calls.append(request.strategy_request_id)
        with seed.Session() as db:
            saved = save_strategy_result(db, seed.restaurant_id, seed.qualification_run_id,
                {"restaurant": "Cafe", "strategy_request_id": request.strategy_request_id})
            return {"strategy_id": saved.id}
    with patch.object(strategy_worker, "get_settings", return_value=replace(workflow.settings, strategy_handoff_outbox=str(inbox))), \
         patch.object(strategy_worker, "generate_and_save_strategy_from_handoff", side_effect=generate), \
         patch.object(database, "SessionLocal", seed.Session), \
         patch.object(outreach_repository, "OutreachRepository", return_value=seed.repository):
        first = strategy_worker.process_strategy_requests_once()
        assert first["processed"] == 1, first
        notice = json.loads((tmp_path / "notify" / path.name).read_text())
        assert notice["restaurant_id"] == seed.restaurant_id
        path.write_text(handoff.model_dump_json(), encoding="utf-8")
        replay = strategy_worker.process_strategy_requests_once()
        assert replay["processed"] == 1 and len(calls) == 1


def test_provider_failure_never_claims_sent(journey):
    seed, workflow, email, ids = journey
    draft = workflow.start_outreach(**ids)
    email.send_approved_message = lambda _: {"success": False, "provider": "test", "failure_code": "UNAVAILABLE"}
    failed = decide(workflow, draft)
    assert failed.errors and not failed.execution.success
    with seed.Session() as db:
        assert db.get(OutboundMessage, draft.email_draft.message_id).status == "FAILED"
