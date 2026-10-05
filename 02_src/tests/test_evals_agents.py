"""Offline tests for the evaluation of every agent: the checks written in code, the scoring and the online hooks.

No model, network or LangSmith call is made. Run with: python -m unittest tests.test_evals_agents -v
"""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch("dotenv.load_dotenv", return_value=False):
    from evals import online, outreach, qualification, research
    from evals.common import normalize_number, numbers_in
    from evals.run_all import mean_scores, registry


class NumberTests(unittest.TestCase):
    def test_the_same_quantity_compares_equal(self):
        self.assertEqual(normalize_number("10.71"), "10.71")
        self.assertEqual(normalize_number("0.0"), "0")
        self.assertEqual(normalize_number("1,234"), "1234")
        self.assertEqual(numbers_in("3.92% above 2.0%–2.5%"), {"3.92", "2", "2.5"})
        self.assertEqual(numbers_in({"posts": 13, "rate": 0.0}), {"13", "0"})


RESEARCH = {
    "metrics": {
        "activity": {"content_last_30_days": 13, "content_per_week": 3.03, "days_since_last_content": 1},
        "format_mix": {"image": {"count": 17, "pct": 56.67}, "reel": {"count": 12, "pct": 40.0}, "carousel": {"count": 1, "pct": 3.33}},
    },
    "research_signals": [
        {"signal_id": "activity_01", "observation": "13 posts in 30 days.", "metric_path": "metrics.activity", "value": {"content_last_30_days": 13}},
    ],
    "data_quality": {"status": "complete", "warnings": []},
}


def research_outputs(data):
    return research.target({"research_result": data})


class ResearchEvaluatorTests(unittest.TestCase):
    def test_consistent_research_passes(self):
        outputs = research_outputs(RESEARCH)
        self.assertEqual(research.metrics_consistent(outputs)["score"], 1)
        self.assertEqual(research.signals_traceable(outputs)["score"], 1)
        self.assertEqual(research.data_quality_reported(outputs)["score"], 1)

    def test_inconsistent_numbers_are_reported(self):
        bad = {**RESEARCH, "metrics": {
            "activity": {"content_last_30_days": 13, "content_per_week": 9.0},
            "format_mix": {"image": {"count": 1, "pct": 140.0}},
        }}
        outcome = research.metrics_consistent(research_outputs(bad))
        self.assertEqual(outcome["score"], 0)
        self.assertIn("should be 3.03 a week", outcome["comment"])
        self.assertIn("not a percentage", outcome["comment"])

    def test_a_signal_must_point_to_a_real_metric_with_the_same_values(self):
        missing = {**RESEARCH, "research_signals": [
            {"signal_id": "reels_01", "observation": "x", "metric_path": "metrics.reels", "value": {}},
            {"signal_id": "activity_01", "observation": "y", "metric_path": "metrics.activity", "value": {"content_last_30_days": 99}},
        ]}
        outcome = research.signals_traceable(research_outputs(missing))
        self.assertEqual(outcome["score"], 0)
        self.assertIn("metrics.reels does not exist", outcome["comment"])
        self.assertIn("differ from metrics.activity", outcome["comment"])
        self.assertEqual(research.signals_traceable(research_outputs({**RESEARCH, "research_signals": []}))["score"], 0)


REPORT = {
    "restaurant": "Cafe", "qualification": "Qualified", "decision_rationale": "Good fit.",
    "marketing_gaps": [
        {"gap": "Inactivity", "status": "Confirmed", "severity": "High", "priority": 1, "recommendation_focus": "Consistency",
         "evidence": ["13 posts in the last 30 days.", "Dash Social reports 2-3 posts a week.", "Research signal: activity_01."]},
        {"gap": "Narrow mix", "status": "Confirmed", "severity": "Moderate", "priority": 2, "recommendation_focus": "Mix", "evidence": ["Reels are 40.0% of the content."]},
    ],
    "strengths": [], "data_limitations": [],
}
EVIDENCE = {"metrics": {"activity": {"content_last_30_days": 13}, "format_mix": {"reel": {"pct": 40.0}}},
            "research_signals": [{"signal_id": "activity_01"}]}


class QualificationEvaluatorTests(unittest.TestCase):
    def test_a_good_report_passes_every_check(self):
        self.assertEqual(qualification.report_valid(REPORT)["score"], 1)
        self.assertEqual(qualification.evidence_grounded(REPORT, EVIDENCE)["score"], 1)
        self.assertEqual(qualification.signals_exist(REPORT, EVIDENCE)["score"], 1)

    def test_bad_severity_priority_and_missing_evidence_are_found(self):
        bad = {**REPORT, "marketing_gaps": [
            {**REPORT["marketing_gaps"][0], "severity": "Critical", "priority": 2},
            {**REPORT["marketing_gaps"][1], "evidence": []},
        ]}
        outcome = qualification.report_valid(bad)
        self.assertEqual(outcome["score"], 0)
        for text in ("unknown severities ['Critical']", "priorities are [2, 2]", "a gap has no evidence"):
            self.assertIn(text, outcome["comment"])

    def test_a_number_that_is_not_in_the_research_is_ungrounded_unless_it_is_a_cited_benchmark(self):
        report = {**REPORT, "marketing_gaps": [{**REPORT["marketing_gaps"][0], "evidence": [
            "The engagement rate was 4.87%.", "Hootsuite reports an engagement rate of 3.1%.", "13 posts in the last 30 days."]}]}
        outcome = qualification.evidence_grounded(report, EVIDENCE)
        self.assertAlmostEqual(outcome["score"], 2 / 3)
        self.assertIn("4.87", outcome["comment"])

    def test_a_cited_signal_must_exist(self):
        report = {**REPORT, "marketing_gaps": [{**REPORT["marketing_gaps"][0], "evidence": ["See activity_01 and made_up_07."]}]}
        self.assertIn("made_up_07", qualification.signals_exist(report, EVIDENCE)["comment"])


class OutreachEvaluatorTests(unittest.TestCase):
    def test_the_labelled_cases_cover_good_and_bad_emails(self):
        cases = outreach.review._review_examples() if hasattr(outreach.review, "_review_examples") else outreach.review.build_examples()
        should_pass = [c["outputs"]["should_pass"] for c in cases]
        self.assertTrue(any(should_pass) and not all(should_pass))
        self.assertEqual({"privacy", "scope", "grounding", "tone", "quality", "good"}, {c["outputs"]["category"] for c in cases})

    def test_verdict_and_error_rates(self):
        reference = [{"should_pass": True}, {"should_pass": True}, {"should_pass": False}, {"should_pass": False}]
        outputs = [{"passed": True}, {"passed": False}, {"passed": True}, {"passed": False}]
        self.assertEqual(outreach.verdict_correct(outputs[1], reference[1])["score"], 0)
        self.assertEqual(outreach._accuracy(outputs, reference)["score"], 0.5)
        self.assertEqual(outreach._false_approval_rate(outputs, reference)["score"], 0.5)   # one of two bad emails got through
        self.assertEqual(outreach._false_rejection_rate(outputs, reference)["score"], 0.5)  # one of two good emails was blocked

    def test_email_checks(self):
        good = "Hi Zaitoon team, we are Rawaj. We noticed no posts lately. We would like to offer a complimentary Free Trial."
        self.assertEqual(outreach.no_links(good)["score"], 1)
        self.assertEqual(outreach.no_links("see https://x.example")["score"], 0)
        self.assertEqual(outreach.no_internal_terms(good)["score"], 1)
        self.assertIn("qualified", outreach.no_internal_terms("You are Qualified with a high priority gap")["comment"])
        self.assertEqual(outreach.names_the_restaurant(good, "Zaitoon Restaurant")["score"], 1)
        self.assertEqual(outreach.mentions_free_trial(good)["score"], 1)
        self.assertEqual(outreach.concise("word " * 200)["score"], 0)

    def test_the_decision_must_be_an_acceptable_action(self):
        reference = {"acceptable_actions": ["SEND_FOLLOW_UP"]}
        self.assertEqual(outreach.action_correct({"action": "SEND_FOLLOW_UP", "basis": []}, reference)["score"], 1)
        self.assertEqual(outreach.action_correct({"action": "WAIT_FOR_RESPONSE", "basis": ["too early"]}, reference)["score"], 0)
        self.assertEqual(len(outreach.decision.build_examples()), 5)


class RunAllTests(unittest.TestCase):
    def test_every_agent_is_registered_with_a_dataset(self):
        agents = registry()
        self.assertEqual(set(agents), {"research", "qualification", "outreach_review", "outreach_email", "outreach_decision", "strategy"})
        self.assertEqual(len({a.DATASET for a in agents.values()}), 6)
        for agent in agents.values():
            self.assertTrue(callable(agent.build_examples) and callable(agent.target) and callable(agent.build_evaluators))

    def test_scores_are_averaged_per_check(self):
        item = lambda key, score: SimpleNamespace(key=key, score=score)
        rows = [{"evaluation_results": {"results": [item("a", 1), item("b", 0.5)]}},
                {"evaluation_results": {"results": [item("a", 0), item("b", None)]}}]
        self.assertEqual(mean_scores(rows), {"a": 0.5, "b": 0.5})


class OnlineEvaluationTests(unittest.TestCase):
    def test_nothing_happens_without_a_traced_run(self):
        with patch.object(online, "_current_run", return_value=None), patch.object(online, "record") as record:
            online.check_qualification(REPORT, EVIDENCE)
        record.assert_not_called()

    def test_scores_are_recorded_on_the_current_run(self):
        with patch.object(online, "_current_run", return_value=SimpleNamespace(id="run-1")), patch.object(online, "record") as record:
            online.check_qualification(REPORT, EVIDENCE)
            online.check_strategy({"restaurant": "x"}, None, None)
        first = {score["key"] for score in record.call_args_list[0].args[0]}
        self.assertEqual(first, {"report_valid", "evidence_grounded", "signals_exist"})
        self.assertEqual({score["key"] for score in record.call_args_list[1].args[0]}, {"contract_valid", "grounding"})

    def scores_for(self, check, summary):
        with patch.object(online, "_current_run", return_value=SimpleNamespace(id="run-1")), patch.object(online, "record") as record:
            check(summary)
        return record.call_args.args[0][0] if record.called else None

    def test_email_sending_is_scored(self):
        ok = {"approved": [{"message_id": "m1", "errors": []}], "skipped": []}
        self.assertEqual(self.scores_for(online.check_emails_sent, ok)["score"], 1)
        failed = {"approved": [{"message_id": "m1", "errors": ["provider rejected"]}], "skipped": []}
        self.assertEqual(self.scores_for(online.check_emails_sent, failed)["score"], 0)
        self.assertIsNone(self.scores_for(online.check_emails_sent, {"approved": [], "skipped": []}))

    def test_the_handoff_to_strategy_is_scored(self):
        done = {"scanned": 1, "processed": 1, "items": [{"status": "STRATEGY_GENERATED_AND_SAVED"}]}
        self.assertEqual(self.scores_for(online.check_strategy_handoff, done)["score"], 1)
        broken = {"scanned": 2, "processed": 1, "items": [{"status": "FAILED", "strategy_request_id": "r", "error": "boom"}]}
        score = self.scores_for(online.check_strategy_handoff, broken)
        self.assertEqual(score["score"], 0.5)
        self.assertIn("boom", score["comment"])
        self.assertIsNone(self.scores_for(online.check_strategy_handoff, {"scanned": 0, "processed": 0, "items": []}))

    def test_a_failing_check_never_breaks_the_pipeline(self):
        with patch.object(online, "_current_run", return_value=SimpleNamespace(id="run-1")), \
                patch.object(online, "record", side_effect=RuntimeError("LangSmith is down")):
            online.check_qualification(REPORT, EVIDENCE)  # must not raise

    def test_it_can_be_switched_off(self):
        with patch.dict("os.environ", {"ONLINE_EVALS": "false"}), patch.object(online, "record") as record, \
                patch.object(online, "_current_run", return_value=SimpleNamespace(id="run-1")):
            online.check_research(RESEARCH)
        record.assert_not_called()


if __name__ == "__main__":
    unittest.main()
