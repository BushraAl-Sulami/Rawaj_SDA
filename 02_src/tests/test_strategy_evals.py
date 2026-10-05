"""Offline tests for the evaluation code (no model, network or LangSmith calls).

Run with: python -m unittest tests.test_strategy_evals -v
"""

import os
import unittest
from unittest.mock import patch

with patch("dotenv.load_dotenv", return_value=False):
    from evals import evaluators
    from evals.cases import build_cases
    from tests.test_strategy_guardrails import QUALIFICATION, START, valid_strategy

INPUTS = {"qualification_context": QUALIFICATION, "strategy_start_date": START}
REFERENCE = {"high_gaps": ["Prolonged Posting Inactivity"], "moderate_gaps": ["Narrow Content Mix"]}


def outputs(strategy=None, **extra):
    strategy = valid_strategy() if strategy is None else strategy
    return {"strategy": strategy, "draft": strategy, "rounds": [], "errors": [], "warnings": [], "error": None, **extra}


class CodeEvaluatorTests(unittest.TestCase):
    def test_contract_valid(self):
        self.assertEqual(evaluators.contract_valid(outputs())["score"], 1)
        self.assertEqual(evaluators.contract_valid(outputs(errors=["plan must contain days 1-30"]))["score"], 0)
        self.assertEqual(evaluators.contract_valid({"strategy": None, "error": "bad json", "errors": []})["score"], 0)

    def test_grounding_loses_a_quarter_per_warning(self):
        self.assertEqual(evaluators.grounding(outputs())["score"], 1)
        self.assertEqual(evaluators.grounding(outputs(warnings=["a", "b"]))["score"], 0.5)
        self.assertEqual(evaluators.grounding(outputs(warnings=list("abcde")))["score"], 0)

    def test_high_gap_coverage(self):
        self.assertEqual(evaluators.high_gap_coverage(outputs(), REFERENCE)["score"], 1)
        strategy = valid_strategy()
        strategy["primary_marketing_gaps"][0]["gap"] = "Something Else"
        result = evaluators.high_gap_coverage(outputs(strategy), REFERENCE)
        self.assertEqual(result["score"], 0)
        self.assertIn("Prolonged Posting Inactivity", result["comment"])
        self.assertEqual(evaluators.high_gap_coverage(outputs(), {"high_gaps": []})["score"], 1)

    def test_reflection_effect_counts_fixed_findings(self):
        broken = valid_strategy()
        broken["thirty_day_plan"].pop()
        result = evaluators.reflection_effect(outputs(valid_strategy(), draft=broken, rounds=[{}]), INPUTS)
        self.assertEqual(result["score"], 1)
        unchanged = evaluators.reflection_effect(outputs(), INPUTS)
        self.assertEqual(unchanged["score"], 0)

    def test_judges_are_skipped_when_no_strategy_was_produced(self):
        class NeverCalled:
            def __call__(self, **kwargs):
                raise AssertionError("judge must not be called")

        with patch.object(evaluators, "create_llm_as_judge", return_value=NeverCalled()):
            judge = evaluators.make_judge("groundedness", judge_llm=object())
        result = judge(INPUTS, {"strategy": None, "error": "failed"}, REFERENCE)
        self.assertEqual(result["score"], 0)

    def test_judge_prompt_keeps_openevals_placeholders(self):
        prompt = evaluators.PROMPT.format(rubric=evaluators.RUBRICS["plan_quality"])
        for placeholder in ("{inputs}", "{outputs}", "{reference_outputs}"):
            self.assertIn(placeholder, prompt)

    def test_the_judge_uses_the_one_shared_model(self):
        with patch.dict(os.environ, {}):
            os.environ.pop("OPENAI_MODEL", None)
            self.assertEqual(evaluators.judge_model(), "gpt-5.6-luna")
        with patch.dict(os.environ, {"OPENAI_MODEL": "custom-model"}):
            self.assertEqual(evaluators.judge_model(), "custom-model")


class CasesTests(unittest.TestCase):
    def test_cases_are_built_from_stored_qualification_runs(self):
        cases = build_cases()
        for case in cases:
            self.assertIn("qualification_context", case["inputs"])
            self.assertEqual(case["inputs"]["strategy_start_date"], "2026-09-20")
            self.assertTrue(case["reference"]["restaurant"])


if __name__ == "__main__":
    unittest.main()
