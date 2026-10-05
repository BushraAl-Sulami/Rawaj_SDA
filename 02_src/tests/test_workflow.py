"""Offline regression coverage for the restaurant agent workflow.

Run with: python -m unittest discover -s tests -v
"""

import os
from pathlib import Path
import subprocess
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from datetime import datetime, timezone

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

# Importing the application must never load local credentials for these tests.
with patch("dotenv.load_dotenv", return_value=False), patch.dict(
    os.environ, {"DATABASE_URL": "sqlite:///:memory:"}
):
    from database.database import Base
    from database.models import QualificationRun, ResearchRun, Restaurant
    from database.repository import get_latest_research
    import orchestration.workflow as workflow


class RestaurantWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine)
        self.enterContext(patch.object(workflow, "SessionLocal", self.sessions))
        with self.sessions() as db:
            restaurant = Restaurant(
                name="Test Cafe",
                instagram_username="testcafe",
                instagram_url="https://www.instagram.com/testcafe/",
            )
            db.add(restaurant)
            db.commit()
            self.restaurant_id = restaurant.id

        self.research_calls = []
        self.qualification_calls = []
        self.research_status = "complete"
        self.fake_research = ModuleType("agents.research_agent.research_agent")
        self.fake_research.run_research_agent = Mock(side_effect=self.run_research)
        self.fake_qualification = ModuleType(
            "agents.qualification_agent.qualification_agent"
        )
        self.fake_qualification.run_qualification_agent = Mock(
            side_effect=self.run_qualification
        )
        self.enterContext(patch.dict(sys.modules, {
            self.fake_research.__name__: self.fake_research,
            self.fake_qualification.__name__: self.fake_qualification,
        }))

    def run_research(self, restaurant, content_limit, lookback_days):
        self.research_calls.append((restaurant, content_limit, lookback_days))
        result = {
            "analysis_metadata": {
                "status": self.research_status,
                "analyzed_at": datetime.now(timezone.utc).isoformat(),
            },
            "restaurant": restaurant.model_dump(mode="json"),
            "profile": {"followers": 500},
            "profile_analysis": {},
            "metrics": {},
            "research_signals": [],
            "analysis_coverage": {"content_requested": content_limit},
            "data_quality": {"status": self.research_status},
        }
        return SimpleNamespace(model_dump=lambda **kwargs: result)

    def run_qualification(self, evidence):
        self.qualification_calls.append(evidence)
        return {
            "restaurant": evidence["restaurant"]["name"],
            "qualification": "qualified",
            "marketing_gaps": [],
            "strengths": [],
            "data_limitations": [],
        }

    def count(self, model):
        with self.sessions() as db:
            return db.scalar(select(func.count()).select_from(model))

    def test_context_and_coverage_reach_agent_with_one_research_save(self):
        context = {"goals": ["More bookings"], "cuisine": "Saudi"}
        result = workflow.run_restaurant_workflow(
            self.restaurant_id, 8, 45, context=context
        )
        self.assertIsNone(result.get("error"))
        self.assertEqual(self.count(ResearchRun), 1)
        self.assertEqual(self.count(QualificationRun), 1)
        self.assertEqual(self.research_calls[0][1:], (8, 45))
        self.assertEqual(
            self.research_calls[0][0].instagram_url,
            "https://www.instagram.com/testcafe/",
        )
        evidence = self.qualification_calls[0]
        self.assertEqual(evidence["restaurant_context"], context)
        self.assertEqual(evidence["analysis_coverage"], {"content_requested": 8})
        self.assertEqual(evidence["data_quality"], {"status": "complete"})
        self.assertIsNot(evidence["restaurant_context"], context)

    def test_cli_cache_reuses_research_and_qualification(self):
        first = workflow.run_restaurant_workflow(self.restaurant_id)
        second = workflow.run_restaurant_workflow(self.restaurant_id)
        self.assertEqual(first["research_run_id"], second["research_run_id"])
        self.assertEqual(first["qualification_run_id"], second["qualification_run_id"])
        self.assertEqual(len(self.research_calls), 1)
        self.assertEqual(len(self.qualification_calls), 1)

    def test_each_node_feedback_stays_under_one_graph_including_cached_results(self):
        from langsmith import Client, tracing_context
        from langsmith.run_helpers import get_current_run_tree

        client = Mock(spec=Client)
        evaluated = []

        def grade(node, inputs, outputs):
            run = get_current_run_tree()
            evaluated.append((node, run.id, run.parent_run_id))
            self.assertEqual(run.name, node)
            if node == "research":
                self.assertEqual(outputs["research"]["profile"], {"followers": 500})
            if node == "qualification":
                self.assertEqual(inputs["research"]["profile"], {"followers": 500})
                self.assertEqual(outputs["qualification_result"]["qualification"], "qualified")
            return {"score": 0.9, "comment": "Evidence supports the output."}

        with patch.dict(os.environ, {"ONLINE_EVALS": "true", "NODE_LLM_JUDGE": "true", "LANGCHAIN_CALLBACKS_BACKGROUND": "false"}), \
                tracing_context(enabled=True, client=client), patch.object(workflow, "_online"), \
                patch.object(workflow, "_grade_node", side_effect=grade):
            first = workflow.run_restaurant_workflow(self.restaurant_id)
            second = workflow.run_restaurant_workflow(self.restaurant_id)

        self.assertEqual(first, second)
        self.assertEqual([item[0] for item in evaluated], ["research", "qualification"] * 2)
        graph_ids = [call.kwargs["id"] for call in client.create_run.call_args_list if call.kwargs.get("name") == "LangGraph"]
        self.assertEqual(len(graph_ids), 2)
        for index, (node, run_id, parent_id) in enumerate(evaluated):
            self.assertEqual(parent_id, graph_ids[index // 2])
            feedback = client.create_feedback.call_args_list[index].kwargs
            self.assertEqual(feedback["run_id"], run_id)
            self.assertEqual(feedback["key"], f"{node}_quality")
        self.assertEqual(len(self.research_calls), 1)
        self.assertEqual(len(self.qualification_calls), 1)

    def test_failed_research_is_judged_without_running_other_nodes(self):
        self.research_status = "failed"
        with patch.object(workflow, "_judge_node") as judge:
            result = workflow.run_restaurant_workflow(self.restaurant_id)
        judge.assert_called_once()
        self.assertEqual(judge.call_args.args[0], "research")
        self.assertIn("Research failed", judge.call_args.args[2]["error"])
        self.assertIn("Research failed", result["error"])
        self.assertEqual(self.qualification_calls, [])

    def test_outreach_gets_its_own_evaluation_after_qualification(self):
        with self.sessions() as db:
            db.get(Restaurant, self.restaurant_id).email = "owner@example.test"
            db.commit()
        with patch.object(workflow, "_judge_node") as judge:
            result = workflow.run_restaurant_workflow(
                self.restaurant_id, start_outreach=True,
                outreach_runner=lambda **kwargs: {"outreach_status": "PENDING_OUTBOUND_APPROVAL", "outreach_pending_human_approval": True},
            )
        self.assertEqual([call.args[0] for call in judge.call_args_list], ["research", "qualification", "outreach"])
        self.assertEqual(judge.call_args.args[1]["qualification_result"], result["qualification_result"])
        self.assertTrue(judge.call_args.args[2]["outreach"]["pending_human_approval"])

    def test_cleared_frontend_context_reruns_qualification(self):
        workflow.run_restaurant_workflow(
            self.restaurant_id, context={"notes": "Old context"}
        )
        workflow.run_restaurant_workflow(self.restaurant_id, context={})
        self.assertEqual(len(self.research_calls), 1)
        self.assertEqual(len(self.qualification_calls), 2)
        self.assertEqual(self.qualification_calls[1]["restaurant_context"], {})

    def test_changed_limits_select_matching_research(self):
        first = workflow.run_restaurant_workflow(
            self.restaurant_id, content_limit=8, lookback_days=45
        )
        second = workflow.run_restaurant_workflow(
            self.restaurant_id, content_limit=9, lookback_days=46
        )
        reused = workflow.run_restaurant_workflow(
            self.restaurant_id, content_limit=8, lookback_days=45, context={}
        )
        self.assertEqual(len(self.research_calls), 2)
        self.assertEqual(self.research_calls[-1][1:], (9, 46))
        self.assertNotEqual(first["research_run_id"], second["research_run_id"])
        self.assertEqual(reused["research_run_id"], first["research_run_id"])

    def test_force_refresh_reruns_both_agents(self):
        workflow.run_restaurant_workflow(self.restaurant_id)
        workflow.run_restaurant_workflow(self.restaurant_id, force_refresh=True)
        self.assertEqual(len(self.research_calls), 2)
        self.assertEqual(len(self.qualification_calls), 2)

    def test_partial_research_is_reused(self):
        self.research_status = "partial"
        workflow.run_restaurant_workflow(self.restaurant_id)
        workflow.run_restaurant_workflow(self.restaurant_id)
        self.assertEqual(len(self.research_calls), 1)
        self.assertEqual(len(self.qualification_calls), 1)

    def test_failed_research_stops_qualification_and_is_not_cached(self):
        self.research_status = "failed"
        first = workflow.run_restaurant_workflow(self.restaurant_id)
        self.assertIsNotNone(first.get("error"))
        self.assertIn("research_run_id", first)
        workflow.run_restaurant_workflow(self.restaurant_id)
        self.assertEqual(len(self.research_calls), 2)
        self.assertEqual(len(self.qualification_calls), 0)
        self.assertEqual(self.count(ResearchRun), 2)

    def test_legacy_completed_research_with_unknown_limits_is_reusable(self):
        with self.sessions() as db:
            research = ResearchRun(
                restaurant_id=self.restaurant_id,
                status="completed",
                full_result={},
            )
            db.add(research)
            db.commit()
            existing_id = research.id
            self.assertEqual(
                get_latest_research(db, self.restaurant_id, 10, 40).id, existing_id
            )
        result = workflow.run_restaurant_workflow(
            self.restaurant_id, 10, 40, context={}
        )
        self.assertEqual(result["research_run_id"], existing_id)
        self.assertEqual(len(self.research_calls), 0)

    def test_research_provider_error_is_sanitized_in_result_and_logs(self):
        self.fake_research.run_research_agent.side_effect = RuntimeError(
            "test-private-provider-token"
        )
        with self.assertLogs(workflow.logger, level="ERROR") as logs:
            result = workflow.run_restaurant_workflow(self.restaurant_id)
        self.assertEqual(result["error"], "Research failed. Please try again.")
        self.assertNotIn("test-private-provider-token", str(result))
        self.assertNotIn("test-private-provider-token", " ".join(logs.output))
        self.assertEqual(self.count(QualificationRun), 0)

    def test_qualification_provider_error_preserves_research_id(self):
        self.fake_qualification.run_qualification_agent.side_effect = RuntimeError(
            "test-private-provider-token"
        )
        with self.assertLogs(workflow.logger, level="ERROR") as logs:
            result = workflow.run_restaurant_workflow(self.restaurant_id)
        self.assertEqual(result["error"], "Qualification failed. Please try again.")
        self.assertIn("research_run_id", result)
        self.assertNotIn("test-private-provider-token", str(result))
        self.assertNotIn("test-private-provider-token", " ".join(logs.output))
        self.assertEqual(self.count(ResearchRun), 1)
        self.assertEqual(self.count(QualificationRun), 0)

    def test_validation_and_missing_restaurant(self):
        invalid_values = (
            {"restaurant_id": 0},
            {"content_limit": 0},
            {"content_limit": 101},
            {"content_limit": True},
            {"lookback_days": 0},
            {"lookback_days": 366},
            {"context": []},
            {"force_refresh": "yes"},
        )
        for kwargs in invalid_values:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                workflow.run_restaurant_workflow(
                    **{"restaurant_id": self.restaurant_id, **kwargs}
                )
        result = workflow.run_restaurant_workflow(9999)
        self.assertEqual(result["error"], "Restaurant not found.")
        self.assertEqual(len(self.research_calls), 0)

    def test_explicit_session_factory_is_used_in_every_node(self):
        with patch.object(
            workflow, "SessionLocal", side_effect=AssertionError("Wrong database")
        ):
            first = workflow.run_restaurant_workflow(
                self.restaurant_id, session_factory=self.sessions
            )
            cached = workflow.run_restaurant_workflow(
                self.restaurant_id, session_factory=self.sessions
            )
        self.assertIsNone(first.get("error"))
        self.assertIsNone(cached.get("error"))
        self.assertEqual(first["qualification_run_id"], cached["qualification_run_id"])
        self.assertEqual(self.count(ResearchRun), 1)
        self.assertEqual(self.count(QualificationRun), 1)


class WorkflowImportTests(unittest.TestCase):
    def test_import_requires_no_credentials_or_provider_initialization(self):
        code = """
import os
import sys
from unittest.mock import patch
os.environ['DATABASE_URL'] = 'sqlite:///:memory:'
for key in ('OPENAI_API_KEY', 'TAVILY_API_KEY', 'APIFY_API_TOKEN'):
    os.environ.pop(key, None)
with patch('dotenv.load_dotenv', return_value=False):
    import orchestration.workflow
assert 'agents.research_agent.research_agent' not in sys.modules
assert 'agents.qualification_agent.qualification_agent' not in sys.modules
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class NodeJudgeTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"ONLINE_EVALS": "true", "NODE_LLM_JUDGE": "true"}))
        self.run = SimpleNamespace(id="node-id", trace_id="graph-id", client=Mock())
        self.enterContext(patch("langsmith.run_helpers.get_current_run_tree", return_value=self.run))
        self.tracing = self.enterContext(patch("langsmith.utils.tracing_is_enabled", return_value=True))
        self.evidence = self.enterContext(patch.object(workflow, "_judge_snapshot", side_effect=lambda value, config: value))
        self.grade = self.enterContext(patch.object(workflow, "_grade_node", return_value={"score": 0.8, "comment": "Grounded result"}))

    def test_disabled_untraced_and_empty_nodes_make_no_model_call(self):
        for setting in ("ONLINE_EVALS", "NODE_LLM_JUDGE"):
            with patch.dict(os.environ, {setting: "false"}):
                workflow._judge_node("research", {}, {}, {})
        self.tracing.return_value = False
        workflow._judge_node("research", {}, {}, {})
        self.tracing.return_value = True
        with patch("langsmith.run_helpers.get_current_run_tree", return_value=None):
            workflow._judge_node("research", {}, {}, {})
        for name, output in (
            ("outreach", {}), ("outreach", {"outreach": {"status": "SKIPPED_NO_EMAIL"}}),
            ("strategy", {"strategy_summary": {"scanned": 0}}), ("notify_client", {"notifications": []}),
            ("send_emails", {"email_summary": {"approved": [], "skipped": []}}),
            ("followups", {"followup_summary": {"prospects": {"scanned": 8, "started": 0}}}),
        ):
            workflow._judge_node(name, {}, output, {})
        self.grade.assert_not_called()
        self.evidence.assert_not_called()
        self.run.client.create_feedback.assert_not_called()

    def test_bad_scores_and_model_failures_do_not_propagate_or_upload(self):
        for score, comment in ((-1, "bad"), (2, "bad"), (float("nan"), "bad"), (0.5, "")):
            self.grade.return_value = {"score": score, "comment": comment}
            workflow._judge_node("research", {}, {}, {})
        self.grade.side_effect = TimeoutError("provider details")
        workflow._judge_node("research", {}, {}, {})
        self.run.client.create_feedback.assert_not_called()

    def test_feedback_failure_leaves_node_output_intact(self):
        self.run.client.create_feedback.side_effect = ConnectionError("offline")
        output = {"research_run_id": 10}
        wrapped = workflow._judged_node("research")(lambda state, config: output)
        self.assertIs(wrapped({}, {}), output)
        self.assertEqual(self.run.client.create_feedback.call_args.kwargs["run_id"], "node-id")

    def test_shared_judge_accepts_a_new_node_and_its_full_state_without_custom_code(self):
        state = {"previous_step": {"decision": "ready"}, "new_input": "example"}
        output = {"new_result": "done"}
        wrapped = workflow._judged_node("new_step")(lambda state, config: output)
        self.assertIs(wrapped(state, {}), output)
        self.grade.assert_called_once_with("new_step", state, output)
        self.assertEqual(self.run.client.create_feedback.call_args.kwargs["key"], "new_step_quality")

    def test_original_node_exception_is_preserved(self):
        def fail(state, config):
            raise RuntimeError("original node failure")
        with self.assertRaisesRegex(RuntimeError, "original node failure"):
            workflow._judged_node("strategy")(fail)({}, {})
        self.assertEqual(self.evidence.call_args.args[0], {"error_type": "RuntimeError"})

    def test_notification_status_omits_credentials_and_signed_links(self):
        status = workflow._judge_outreach_status({
            "outreach_status": "SENT", "outreach_action": "SEND_STRATEGY_READY",
            "password": "SECRET_PASSWORD", "body": "SECRET_BODY", "url": "SECRET_LINK",
            "outreach_errors": ["SECRET_ERROR"],
        })
        self.assertEqual(status["outreach_status"], "SENT")
        self.assertTrue(status["has_errors"])
        self.assertNotIn("SECRET", str(status))
        self.assertEqual(workflow._judge_outreach_status("PENDING_RETRY: SECRET_ERROR"), {"status": "PENDING_RETRY"})


class JudgeSnapshotTests(unittest.TestCase):
    def test_shared_snapshot_resolves_records_without_mutating_state_or_exposing_notifications(self):
        from contextlib import nullcontext
        db = Mock()
        db.get.side_effect = [
            SimpleNamespace(full_result={"metrics": {"followers": 10}, "recent_content": ["raw"]}),
            SimpleNamespace(strategy_data={"goal": "bookings"}, qualification_run_id=9),
            SimpleNamespace(full_result={"qualification": "qualified"}),
        ]
        state = {"research_run_id": 3, "strategy_saved": [{"strategy_id": 7}], "custom": {"keep": True},
                 "notifications": [{"strategy_id": 7, "notification": {"outreach_status": "SENT", "password": "SECRET", "body": "SECRET"}}]}
        snapshot = workflow._judge_snapshot(state, {"configurable": {"session_factory": lambda: nullcontext(db)}})
        self.assertEqual([call.args[1] for call in db.get.call_args_list], [3, 7, 9])
        self.assertEqual(snapshot["research"], {"metrics": {"followers": 10}})
        self.assertEqual(snapshot["strategy_saved"][0]["qualification"], {"qualification": "qualified"})
        self.assertEqual(snapshot["custom"], {"keep": True})
        self.assertNotIn("SECRET", str(snapshot))
        self.assertNotIn("strategy", state["strategy_saved"][0])
        self.assertEqual(state["notifications"][0]["notification"]["password"], "SECRET")


if __name__ == "__main__":
    unittest.main()
