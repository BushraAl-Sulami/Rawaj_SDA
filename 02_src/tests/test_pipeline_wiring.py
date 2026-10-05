"""Offline tests for the end-to-end pipeline wiring: outreach start, Interested -> Strategy -> client email
with sign-in details, accounts, and the approvals API. No model, network or email provider is used.

Run with: python -m unittest tests.test_pipeline_wiring -v
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

with patch("dotenv.load_dotenv", return_value=False):
    from agents.outreach_followup_agent.schemas import ActionType, EmailDraft, OutboundMessageType, StrategyOutputHandoff
    from agents.outreach_followup_agent.workflow import OutreachFollowUpWorkflow, WorkflowSafetyError
    from agents.strategy_agent import strategy_agent
    from api import accounts
    from api.main import create_app
    from database.database import Base
    from database.models import Restaurant, UserAccount
    from orchestration import workflow

SECRET = "x" * 40


class Db(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rawaj-pipeline-")
        self.addCleanup(temporary.cleanup)
        self.dir = Path(temporary.name)
        self.engine = create_engine(f"sqlite:///{(self.dir / 'db.sqlite').as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.enterContext(patch.dict(os.environ, {"ACCOUNT_SECRET": SECRET, "DASHBOARD_URL": "http://dash.example:8501"}))
        with self.sessions() as db:
            db.add(Restaurant(id=1, name="3Brews", instagram_username="3brews.sa", email="owner@3brews.sa"))
            db.add(Restaurant(id=2, name="No Mail", instagram_username="nomail"))
            db.commit()


def config(sessions, **extra):
    return {"configurable": {"session_factory": sessions, **extra}}


class GraphTests(Db):
    """One LangGraph holds every agent; each entry point starts at the right node."""

    def test_the_graph_has_every_agent_as_a_node(self):
        graph = workflow.graph.get_graph()
        nodes = {name for name in graph.nodes if not name.startswith("__")}
        self.assertEqual(nodes, {"research", "qualification", "outreach", "strategy", "notify_client", "send_emails", "followups"})
        edges = {(edge.source, edge.target) for edge in graph.edges}
        self.assertTrue({("research", "qualification"), ("qualification", "outreach"), ("strategy", "notify_client")} <= edges)

    def test_an_unknown_restaurant_stops_the_analysis_at_research(self):
        with patch.object(workflow, "SessionLocal", self.sessions):
            self.assertEqual(workflow.run_restaurant_workflow(999), {"error": "Restaurant not found."})


class OutreachStageTests(Db):
    STATE = {"request": {"start_outreach": True}, "restaurant_id": 1, "research_run_id": 7, "qualification_run_id": 9}

    def test_outreach_starts_after_qualification_with_the_exact_run_ids(self):
        calls = []

        def runner(**kwargs):
            calls.append(kwargs)
            return {"outreach_thread_id": "t1", "outreach_status": "PENDING_OUTBOUND_APPROVAL", "outreach_action": "SEND_INITIAL",
                    "outreach_message_id": "m1", "outreach_pending_human_approval": True, "outreach_errors": []}

        result = workflow.outreach_stage_node(self.STATE, config(self.sessions, outreach_runner=runner))
        self.assertEqual(calls, [{"restaurant_id": 1, "research_run_id": 7, "qualification_run_id": 9}])
        self.assertEqual(result["outreach"]["status"], "PENDING_OUTBOUND_APPROVAL")
        self.assertTrue(result["outreach"]["pending_human_approval"])

    def test_outreach_is_off_unless_requested_and_skipped_without_email(self):
        def boom(**kwargs):
            raise AssertionError("must not be called")

        off = {**self.STATE, "request": {"start_outreach": False}}
        self.assertEqual(workflow.outreach_stage_node(off, config(self.sessions, outreach_runner=boom)), {})
        no_mail = {**self.STATE, "restaurant_id": 2}
        result = workflow.outreach_stage_node(no_mail, config(self.sessions, outreach_runner=boom))
        self.assertEqual(result["outreach"], {"status": "SKIPPED_NO_EMAIL"})

    def test_a_rejected_email_is_written_again_until_the_review_passes(self):
        outcomes = iter([
            {"outreach_pending_human_approval": False, "outreach_errors": ["Email review did not pass; no Human Approval or send was requested."]},
            {"outreach_pending_human_approval": False, "outreach_errors": ["Email review did not pass; no Human Approval or send was requested."]},
            {"outreach_thread_id": "t1", "outreach_status": "PENDING_OUTBOUND_APPROVAL", "outreach_pending_human_approval": True, "outreach_errors": []},
        ])
        calls = []

        def runner(**kwargs):
            calls.append(1)
            return next(outcomes)

        result = workflow.outreach_stage_node(self.STATE, config(self.sessions, outreach_runner=runner))
        self.assertEqual(len(calls), 3)
        self.assertTrue(result["outreach"]["pending_human_approval"])

    def test_writing_again_is_capped_and_only_for_review_rejections(self):
        rejected = {"outreach_pending_human_approval": False, "outreach_errors": ["Email review did not pass; x"]}
        calls = []
        outcome = workflow._write_until_reviewed(lambda: calls.append(1) or rejected)
        self.assertEqual((len(calls), outcome), (workflow.REVIEW_ATTEMPTS, rejected))
        other_failure = {"outreach_pending_human_approval": False, "outreach_errors": ["EMAIL_GENERATION_FAILED: TimeoutError"]}
        calls.clear()
        workflow._write_until_reviewed(lambda: calls.append(1) or other_failure)
        self.assertEqual(len(calls), 1)  # a different kind of failure is not retried blindly

    def test_an_outreach_failure_is_reported_not_raised(self):
        def runner(**kwargs):
            raise RuntimeError("provider down")

        result = workflow.outreach_stage_node(self.STATE, config(self.sessions, outreach_runner=runner))
        self.assertEqual(result["outreach"]["status"], "ERROR")


class StrategyStageTests(Db):
    STRATEGY_DATA = {"thirty_day_target": ["Post twice a week"], "recommended_services": [{"service": "Content Strategy"}]}

    @staticmethod
    def worker(items):
        return lambda limit: {"scanned": len(items), "items": items}

    @staticmethod
    def saved(request_id="req1"):
        return {"strategy_request_id": request_id, "restaurant_id": 1, "strategy_id": 5, "status": "STRATEGY_GENERATED_AND_SAVED"}

    def test_strategy_output_is_ready_client_safe_and_points_to_the_dashboard(self):
        request = {"strategy_request_id": "req1", "restaurant_id": 1}
        output = StrategyOutputHandoff.model_validate(workflow.build_strategy_output(request, 5, self.STRATEGY_DATA))
        self.assertEqual(output.status.value, "READY")
        self.assertTrue(output.client_notification_allowed)
        self.assertEqual(output.dashboard_strategy_url, "http://dash.example:8501/strategy")
        self.assertIn("Post twice a week", output.client_safe_summary)
        self.assertEqual(output.client_safe_deliverables, ["Content Strategy"])

    def test_a_saved_strategy_triggers_the_client_notification(self):
        notified = []

        def notify(saved):
            notified.append(saved)
            return {"outreach_status": "PENDING_OUTBOUND_APPROVAL"}

        summary = workflow.run_strategy_stage(strategy_process=self.worker([self.saved()]), notify=notify, notify_dir=self.dir / "notify")
        self.assertEqual(notified, [{"strategy_request_id": "req1", "restaurant_id": 1, "strategy_id": 5}])
        self.assertEqual(summary["notifications"][0]["notification"], {"outreach_status": "PENDING_OUTBOUND_APPROVAL"})

    def test_email_2_is_written_again_when_the_review_rejects_it(self):
        outcomes = iter([
            {"outreach_pending_human_approval": False, "outreach_errors": ["Email review did not pass; no Human Approval or send was requested."]},
            {"outreach_status": "STRATEGY_READY", "outreach_pending_human_approval": True, "outreach_errors": []},
        ])
        calls = []

        def notify(saved):
            calls.append(1)
            return next(outcomes)

        summary = workflow.run_strategy_stage(strategy_process=self.worker([self.saved()]), notify=notify, notify_dir=self.dir / "notify")
        self.assertEqual(len(calls), 2)
        self.assertTrue(summary["notifications"][0]["notification"]["outreach_pending_human_approval"])

    def test_requests_that_did_not_produce_a_strategy_are_not_notified(self):
        failed = {"request_file": "x.json", "status": "FAILED", "error": "no interest"}
        summary = workflow.run_strategy_stage(
            strategy_process=self.worker([failed]), notify=lambda saved: self.fail("must not notify"), notify_dir=self.dir / "notify",
        )
        self.assertEqual(summary["notifications"], [])

    def test_a_failed_notification_is_retried_without_regenerating_the_strategy(self):
        notify_dir, attempts, generated = self.dir / "notify", [], []

        def process(limit):
            generated.append(1)
            return {"items": [self.saved()]}

        def flaky(saved):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("email provider down")
            return {"outreach_status": "PENDING_OUTBOUND_APPROVAL"}

        first = workflow.run_strategy_stage(strategy_process=process, notify=flaky, notify_dir=notify_dir)
        self.assertTrue(first["notifications"][0]["notification"].startswith("PENDING_RETRY"))
        self.assertEqual([p.name for p in notify_dir.glob("*.json")], ["req1.json"])

        second = workflow.run_strategy_stage(strategy_process=self.worker([]), notify=flaky, notify_dir=notify_dir)
        self.assertEqual(len(second["notifications"]), 1)
        self.assertEqual(list(notify_dir.glob("*.json")), [])
        self.assertEqual(len(generated), 1)  # the strategy itself was generated only once


class StrategyKnowsAboutInterestTests(unittest.TestCase):
    REQUEST = {
        "restaurant_id": 1, "qualification_run_id": 3, "qualification_context": {"marketing_gaps": []},
        "strategy_start_date": "2026-09-20", "customer_request": "Restaurant selected Interested from Rawaj outreach.",
        "interest_event_id": "evt_1", "strategy_request_id": "req_1",
    }

    def test_a_strategy_is_refused_without_a_verified_interested_event(self):
        request = {**self.REQUEST, "interest_event_id": ""}
        with patch.object(strategy_agent, "generate_strategy") as generate:
            with self.assertRaises(ValueError):
                strategy_agent.generate_strategy_from_handoff(request)
        generate.assert_not_called()

    def test_the_agent_is_told_the_restaurant_became_interested(self):
        with patch.object(strategy_agent, "generate_strategy", return_value={}) as generate:
            strategy_agent.generate_strategy_from_handoff(self.REQUEST)
        interest = generate.call_args.kwargs["interest"]
        self.assertEqual(interest["interested_on"], "2026-09-20")
        section = strategy_agent._interest_section(interest)
        self.assertIn('clicking "Interested"', section)
        self.assertIn("2026-09-20", section)
        self.assertEqual(strategy_agent._interest_section(None), "")

    def test_outreach_research_identity_reaches_real_strategy_adapter(self):
        research = {"restaurant": {"id": 1, "name": "Ashi Sushi"}, "metrics": {"posts": 7}}
        request = {**self.REQUEST, "research_context": research}
        with patch.object(strategy_agent, "generate_strategy", return_value={}) as generate:
            strategy_agent.generate_strategy_from_handoff(request)
        evidence = generate.call_args.kwargs["qualification_data"]
        self.assertEqual(evidence["restaurant"]["name"], "Ashi Sushi")
        self.assertEqual(evidence["research_context"]["metrics"], {"posts": 7})
        self.assertNotIn("research_context", request["qualification_context"])

    def test_the_saved_strategy_keeps_which_interested_click_it_answers(self):
        saved = {}

        class FakeSession:
            def close(self):
                pass

        def save(db, restaurant_id, qualification_run_id, result):
            saved.update(result)
            return SimpleNamespace(id=11)

        with patch.object(strategy_agent, "generate_strategy_from_handoff", return_value={"restaurant": "3Brews"}), \
                patch.object(strategy_agent, "check_strategy", return_value={"errors": [], "warnings": []}), \
                patch.object(strategy_agent, "SessionLocal", FakeSession), \
                patch.object(strategy_agent, "save_strategy_result", save):
            outcome = strategy_agent.generate_and_save_strategy_from_handoff(self.REQUEST)
        self.assertEqual(outcome["strategy_id"], 11)
        self.assertEqual(saved["interest_event_id"], "evt_1")
        self.assertEqual(saved["strategy_request_id"], "req_1")
        self.assertEqual(saved["strategy_start_date"], "2026-09-20")


class ClientEmailSignInTests(Db):
    """The strategy-ready email carries the client's username, password and link; the models never see the password."""

    def workflow_with_access(self):
        instance = object.__new__(OutreachFollowUpWorkflow)
        instance.access_provider = lambda restaurant_id: accounts.provision_access(restaurant_id, session_factory=self.sessions)
        return instance

    def state(self):
        output = workflow.build_strategy_output({"strategy_request_id": "req1", "restaurant_id": 1}, 5, {})
        return {"strategy_output": output, "provenance": {"restaurant_id": 1}}

    def test_email_contains_username_password_and_link(self):
        body = self.workflow_with_access()._append_trusted_dashboard_link(
            body="Hello!", action=ActionType.NOTIFY_STRATEGY_READY, state=self.state(),
        )
        password = accounts.client_password(1)
        self.assertIn("Username: 3brews.sa", body)
        self.assertIn(f"Password: {password}", body)
        self.assertIn("http://dash.example:8501/strategy", body)  # the strategy link
        self.assertIn("Sign in here: http://dash.example:8501", body)
        self.assertIn("30 days from activation", body) #  # the model's text announces it; the footer does not repeat it
        self.assertIn("never asks you to send passwords", body)  # we do not collect sensitive information

    def test_rebuilding_the_email_gives_the_same_credentials_and_they_sign_in(self):
        instance = self.workflow_with_access()
        first = instance._append_trusted_dashboard_link(body="x", action=ActionType.NOTIFY_STRATEGY_READY, state=self.state())
        second = instance._append_trusted_dashboard_link(body="x", action=ActionType.NOTIFY_STRATEGY_READY, state=self.state())
        self.assertEqual(first, second)
        with self.sessions() as db:
            self.assertEqual(db.scalars(select(UserAccount)).all().__len__(), 1)
            account = accounts.authenticate(db, "3brews.sa.1_rawaj", accounts.client_password(1))
            self.assertEqual(account.restaurant_id, 1)

    def test_no_access_provider_blocks_incomplete_email(self):
        instance = object.__new__(OutreachFollowUpWorkflow)
        instance.access_provider = None
        with self.assertRaises(WorkflowSafetyError):
            instance._append_trusted_dashboard_link(body="x", action=ActionType.NOTIFY_STRATEGY_READY, state=self.state())

    def test_missing_account_secret_stops_the_email_instead_of_sending_no_password(self):
        with patch.dict(os.environ, {"ACCOUNT_SECRET": ""}):
            with self.assertRaises(accounts.AccountConfigurationError):
                self.workflow_with_access()._append_trusted_dashboard_link(
                    body="x", action=ActionType.NOTIFY_STRATEGY_READY, state=self.state(),
                )

    def test_the_reviewing_model_never_sees_the_password(self):
        password = accounts.client_password(1)
        body = self.workflow_with_access()._append_trusted_dashboard_link(
            body="Hello!", action=ActionType.NOTIFY_STRATEGY_READY, state=self.state(),
        )
        draft = EmailDraft(
            relationship_id="rel1", restaurant_id=1, message_type=OutboundMessageType.STRATEGY_READY_NOTIFICATION,
            action=ActionType.NOTIFY_STRATEGY_READY, recipient="owner@3brews.sa", subject="Ready",
            html_body="<p>x</p>", plain_text_body=body,
        )
        redacted = OutreachFollowUpWorkflow._redacted_draft_for_review(draft)
        self.assertNotIn(password, json.dumps(redacted))
        # Everything the system appends (link, sign-in details, privacy note) is outside the model's review.
        self.assertNotIn("Username", redacted["plain_text_body"])
        self.assertNotIn("http://dash.example", redacted["plain_text_body"])
        self.assertIn("appends the dashboard link", redacted["plain_text_body"])
        self.assertTrue(redacted["plain_text_body"].startswith("Hello!"))


class AccountAndAuthTests(Db):
    def client(self):
        return self.enterContext(TestClient(create_app(database_engine=self.engine)))

    def test_password_is_stable_readable_and_needs_a_secret(self):
        self.assertEqual(accounts.client_password(1), accounts.client_password(1))
        self.assertNotEqual(accounts.client_password(1), accounts.client_password(2))
        self.assertEqual(len(accounts.client_password(1)), 12)
        with patch.dict(os.environ, {"ACCOUNT_SECRET": "short"}):
            with self.assertRaises(accounts.AccountConfigurationError):
                accounts.client_password(1)

    def test_only_a_hash_is_stored(self):
        access = accounts.provision_access(1, session_factory=self.sessions)
        with self.sessions() as db:
            stored = db.scalar(select(UserAccount)).password_hash
        self.assertTrue(stored.startswith("scrypt$"))
        self.assertNotIn(access["password"], stored)

    def test_changed_secret_cannot_silently_invalidate_emailed_password(self):
        access = accounts.provision_access(1, session_factory=self.sessions)
        with self.sessions() as db:
            original_hash = db.scalar(select(UserAccount)).password_hash
        with patch.dict(os.environ, {"ACCOUNT_SECRET": "different-session-secret-" * 3}):
            with self.assertRaises(accounts.AccountConfigurationError):
                accounts.provision_access(1, session_factory=self.sessions)
        with self.sessions() as db:
            account = db.scalar(select(UserAccount))
            self.assertEqual(account.password_hash, original_hash)
            self.assertTrue(accounts.verify_password(access["password"], account.password_hash))

    def test_login_route_for_owners_wrong_passwords_and_lockout(self):
        access = accounts.provision_access(1, session_factory=self.sessions)
        client = self.client()
        ok = client.post("/api/auth/login", json={"username": access["username"], "password": access["password"]})
        self.assertEqual(ok.status_code, 200, ok.text)
        self.assertEqual({k: ok.json()[k] for k in ("role", "username", "restaurant_id")}, {"role": "owner", "username": access["username"], "restaurant_id": 1})
        self.assertTrue(ok.json()["access_token"])
        wrong = client.post("/api/auth/login", json={"username": access["username"], "password": "nope"})
        self.assertEqual(wrong.status_code, 401)
        self.assertEqual(client.post("/api/auth/login", json={"username": "ghost", "password": "nope"}).json(), wrong.json())
        for _ in range(accounts.MAX_FAILED_ATTEMPTS):
            client.post("/api/auth/login", json={"username": access["username"], "password": "nope"})
        locked = client.post("/api/auth/login", json={"username": access["username"], "password": access["password"]})
        self.assertEqual(locked.status_code, 401)  # locked even with the right password

    def test_admin_login_uses_environment_credentials(self):
        client = self.client()
        with patch.dict(os.environ, {"ADMIN_USERNAME": "staff", "ADMIN_PASSWORD": "s3cret-pass"}):
            ok = client.post("/api/auth/login", json={"username": "Staff", "password": "s3cret-pass"})
            self.assertEqual(ok.json()["role"], "admin")
            self.assertEqual(client.post("/api/auth/login", json={"username": "staff", "password": "bad"}).status_code, 401)
        self.assertEqual(client.post("/api/auth/login", json={"username": "staff", "password": "s3cret-pass"}).status_code, 401)


class FakeOutreach:
    """Just enough of RawajOutreachApplication for the approvals routes."""

    def __init__(self):
        self.paused = {
            "thread-a": {"type": "HUMAN_EMAIL_APPROVAL_REQUIRED", "kind": "OUTBOUND_EMAIL", "message_id": "m1", "message_revision": 2,
                         "content_sha256": "h" * 64, "recipient": "owner@3brews.sa", "subject": "Ready", "message_type": "STRATEGY_READY_NOTIFICATION",
                         "expires_at": "2099-01-01T00:00:00+00:00", "review": {"passed": True}},
            "thread-done": None,
        }
        self.resumed = []
        outer = self

        class Checkpointer:
            def list(self, config):
                return [SimpleNamespace(config={"configurable": {"thread_id": t}}) for t in outer.paused]

        class Graph:
            def get_state(self, config):
                value = outer.paused[config["configurable"]["thread_id"]]
                tasks = [SimpleNamespace(interrupts=[SimpleNamespace(value=value)])] if value else []
                return SimpleNamespace(tasks=tasks)

        def resume(**kwargs):
            outer.resumed.append(kwargs)
            if kwargs["decision"] == "APPROVED":
                return SimpleNamespace(errors=[], execution=SimpleNamespace(email_status="SENT"), email_draft=SimpleNamespace(status=SimpleNamespace(value="SENT")))
            return SimpleNamespace(errors=[], execution=None, email_draft=SimpleNamespace(status=SimpleNamespace(value="REJECTED")))

        self.workflow = SimpleNamespace(checkpointer=Checkpointer(), graph=Graph(), resume_human_approval=resume)
        self.settings = SimpleNamespace(email_provider="smtp", from_email="hello@rawaj.app")


class ApprovalsApiTests(Db):
    def setUp(self):
        super().setUp()
        # Approval-route tests must not mount a real button runtime from a developer's .env.
        from agents.outreach_followup_agent.config import RawajSettings
        self.enterContext(patch('agents.outreach_followup_agent.config.get_settings',
                                return_value=RawajSettings(environment='test')))
        self.fake = FakeOutreach()
        self.client = self.enterContext(TestClient(create_app(database_engine=self.engine, outreach_application=self.fake)))

    def test_lists_only_paused_email_approvals(self):
        response = self.client.get("/api/approvals")
        self.assertEqual(response.status_code, 200, response.text)
        items = response.json()
        self.assertEqual([i["message_id"] for i in items], ["m1"])
        self.assertEqual(items[0]["recipient"], "owner@3brews.sa")
        self.assertEqual(items[0]["message_type"], "STRATEGY_READY_NOTIFICATION")

    def test_approval_resumes_the_exact_paused_draft(self):
        response = self.client.post("/api/approvals/m1/decision", json={"decision": "APPROVED", "reviewer_id": "staff", "note": "ok"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["sent"])
        self.assertEqual(self.fake.resumed, [{
            "thread_id": "thread-a", "reviewer_id": "staff", "decision": "APPROVED", "note": "ok",
            "message_id": "m1", "message_revision": 2, "content_sha256": "h" * 64,
        }])

    def test_rejection_does_not_send(self):
        response = self.client.post("/api/approvals/m1/decision", json={"decision": "REJECTED", "reviewer_id": "staff"})
        self.assertFalse(response.json()["sent"])

    def test_unknown_message_and_bad_decisions_are_refused_without_resuming(self):
        self.assertEqual(self.client.post("/api/approvals/nope/decision", json={"decision": "APPROVED", "reviewer_id": "s"}).status_code, 404)
        self.assertEqual(self.client.post("/api/approvals/m1/decision", json={"decision": "MAYBE", "reviewer_id": "s"}).status_code, 422)
        self.assertEqual(self.client.post("/api/approvals/m1/decision", json={"decision": "APPROVED"}).status_code, 422)
        self.assertEqual(self.fake.resumed, [])

    def test_a_safety_refusal_is_shown_and_other_errors_are_not_leaked(self):
        def refuse(**kwargs):
            raise WorkflowSafetyError("Approval expired.")

        self.fake.workflow.resume_human_approval = refuse
        response = self.client.post("/api/approvals/m1/decision", json={"decision": "APPROVED", "reviewer_id": "s"})
        self.assertEqual((response.status_code, response.json()["detail"]), (409, "Approval expired."))

        def crash(**kwargs):
            raise RuntimeError("smtp password=hunter2")

        self.fake.workflow.resume_human_approval = crash
        response = self.client.post("/api/approvals/m1/decision", json={"decision": "APPROVED", "reviewer_id": "s"})
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("hunter2", response.text)

    def test_admin_token_protects_the_routes_when_configured(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "tok-123"}):
            self.assertEqual(self.client.get("/api/approvals").status_code, 401)
            self.assertEqual(self.client.get("/api/approvals", headers={"X-Admin-Token": "wrong"}).status_code, 401)
            self.assertEqual(self.client.get("/api/approvals", headers={"X-Admin-Token": "tok-123"}).status_code, 200)
            self.assertEqual(self.client.post("/api/approvals/m1/decision", json={"decision": "APPROVED", "reviewer_id": "s"}).status_code, 401)
        self.assertEqual(self.fake.resumed, [])

    def test_approvals_report_503_when_outreach_is_not_configured(self):
        def broken():
            raise RuntimeError("missing settings")

        client = self.enterContext(TestClient(create_app(database_engine=self.engine, outreach_application_factory=broken)))
        response = client.get("/api/approvals")
        self.assertEqual(response.status_code, 503)


def paused(message_id, message_type="INITIAL_OUTREACH", passed=True, privacy_safe=True, thread=None):
    return {
        "type": "HUMAN_EMAIL_APPROVAL_REQUIRED", "kind": "OUTBOUND_EMAIL", "message_id": message_id, "message_revision": 1,
        "content_sha256": "h" * 64, "recipient": "owner@3brews.sa", "subject": "Hi", "message_type": message_type,
        "expires_at": "2099-01-01T00:00:00+00:00", "review": {"passed": passed, "privacy_safe": privacy_safe},
    }


class InitialApprovalPolicyTests(unittest.TestCase):
    def test_background_sweep_never_approves_a_paused_initial_email(self):
        fake = FakeOutreach()
        fake.paused = {"initial": paused("m1")}
        for setting in ("true", "false"):
            with patch.dict(os.environ, {"AUTO_APPROVE_EMAILS": setting}):
                outcome = workflow.send_reviewed_emails(lambda: fake)
                self.assertEqual(outcome["approved"], [])
                self.assertEqual(fake.resumed, [])


if __name__ == "__main__":
    unittest.main()
