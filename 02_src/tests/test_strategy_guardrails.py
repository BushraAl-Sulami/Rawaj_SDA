"""Offline tests for the Strategy Agent: guardrail checks, Shaimaa's self-reflection wiring and model selection.

No model or network is used; the ReAct executor and the model are replaced by fakes.
Run with: python -m unittest tests.test_strategy_guardrails -v
"""

import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

with patch("dotenv.load_dotenv", return_value=False):
    from agents.strategy_agent import strategy_agent
    from agents.strategy_agent.guardrails import allowed_services, check_strategy

QUALIFICATION = {
    "marketing_gaps": [
        {"gap": "Prolonged Posting Inactivity", "severity": "High", "priority": 1,
         "evidence": ["0 posts in 30 days.", "Menu visibility 10.71%."], "recommendation_focus": "Consistency"},
        {"gap": "Narrow Content Mix", "severity": "Moderate", "priority": 2, "evidence": [], "recommendation_focus": "Mix"},
    ]
}
START = "2026-09-20"  # Saudi National Day (2026-09-23) is day 4


def valid_strategy():
    return {
        "restaurant": "3Brews",
        "primary_marketing_gaps": [
            {"gap": "Prolonged Posting Inactivity", "severity": "High", "highlight": "10.71%",
             "highlight_label": "menu visibility", "key_point": "No recent posts."},
        ],
        "thirty_day_target": ["Restore a posting rhythm", "Show the menu"],
        "recommended_services": [{"service": "Social Media Strategy", "why_this_service_fits": "Fixes cadence."}],
        "thirty_day_plan": [
            {"day": day, "focus": f"Focus {day}", "action": f"Action {day}."} for day in range(1, 31)
        ],
        "external_trend_support": [],
    }


class GuardrailTests(unittest.TestCase):
    def check(self, data, **kwargs):
        return check_strategy(data, QUALIFICATION, START, **kwargs)

    def test_agency_services_are_parsed_from_the_prompt(self):
        self.assertEqual(allowed_services(), {
            "Social Media Strategy", "Content Strategy", "Campaign Strategy",
            "Engagement Strategy", "Brand Communication Strategy", "Paid Advertising Strategy",
        })

    def test_valid_strategy_has_no_findings(self):
        self.assertEqual(self.check(valid_strategy()), {"errors": [], "warnings": []})

    def test_contract_violations_are_errors(self):
        data = valid_strategy()
        data["thirty_day_plan"].pop()
        data["thirty_day_plan"][0]["day"] = 2
        self.assertTrue(any("plan must contain days 1-30" in e for e in self.check(data)["errors"]))

        data = valid_strategy()
        data["recommended_services"][0]["service"] = "Influencer Marketing"
        self.assertTrue(any("not one of the agency services" in e for e in self.check(data)["errors"]))

        data = valid_strategy()
        data["thirty_day_target"] = ["a", "b", "c", "d"]
        self.assertTrue(any("thirty_day_target" in e for e in self.check(data)["errors"]))

        data = valid_strategy()
        data["expected_marketing_objective"] = "not allowed"
        self.assertTrue(any("expected_marketing_objective" in e for e in self.check(data)["errors"]))

        data = valid_strategy()
        data["primary_marketing_gaps"][0]["severity"] = "Low"
        self.assertTrue(any("severity" in e for e in self.check(data)["errors"]))

    def test_grounding_problems_are_warnings(self):
        data = valid_strategy()
        data["primary_marketing_gaps"] = [
            {"gap": "Slow Website", "severity": "High", "highlight": "99%", "key_point": "x"},
            {"gap": "Narrow Content Mix", "severity": "High", "highlight": None, "key_point": "y"},
        ]
        result = self.check(data)
        self.assertEqual(result["errors"], [])
        joined = " | ".join(result["warnings"])
        self.assertIn("'Slow Website' is not in the Qualification Agent output", joined)
        self.assertIn("is Moderate in qualification but High here", joined)
        self.assertIn("highlight '99%'", joined)

    def test_event_mentions_must_sit_near_the_event(self):
        data = valid_strategy()
        data["thirty_day_plan"][3]["focus"] = "Saudi National Day campaign"      # day 4: on the event
        self.assertEqual(self.check(data)["warnings"], [])
        data["thirty_day_plan"][24]["action"] = "Post about Saudi National Day"   # day 25: 3 weeks late
        self.assertTrue(any("Saudi National Day" in w and "[25]" in w for w in self.check(data)["warnings"]))

    def test_tentative_event_is_not_presented_as_confirmed(self):
        data = valid_strategy()
        data["thirty_day_plan"][0]["action"] = "Confirmed Ramadan preparation"
        result = check_strategy(data, QUALIFICATION, "2027-02-01")  # Ramadan (tentative) starts 2027-02-08
        self.assertTrue(any("tentative event 'Ramadan'" in w for w in result["warnings"]))


class FakeExecutor:
    def __init__(self, output):
        self.output, self.inputs = output, []

    def invoke(self, payload, config=None):
        self.inputs.append(payload["input"])
        return {"output": self.output}


class FakeModel:
    """Answers like a Responses-API model: the text is in .text (.content would be a list of blocks)."""

    def __init__(self, reply):
        self.reply, self.prompts = reply, []

    def invoke(self, prompt, config=None):
        self.prompts.append(prompt)
        return SimpleNamespace(content=[{"type": "text", "text": self.reply}], text=self.reply)


REFLECTION_REPLY = json.dumps({"passed": False, "issues": ["Day 3 repeats day 2."], "corrected_strategy": valid_strategy()})


class ReflectionTests(unittest.TestCase):
    def run_generate(self, draft, reflection_reply=REFLECTION_REPLY, **kwargs):
        executor, model = FakeExecutor(json.dumps(draft)), FakeModel(reflection_reply)
        with patch.object(strategy_agent, "get_executor", return_value=executor), patch.object(strategy_agent, "get_llm", return_value=model):
            return strategy_agent.generate_strategy(QUALIFICATION, START, **kwargs), executor, model

    def test_the_final_strategy_is_the_corrected_one(self):
        broken = valid_strategy()
        broken["thirty_day_plan"].pop()
        final, _, model = self.run_generate(broken)
        self.assertEqual(len(final["thirty_day_plan"]), 30)
        self.assertEqual(len(model.prompts), 1)  # one reflection call

    def test_evaluation_data_keeps_the_draft_and_what_reflection_found(self):
        broken = valid_strategy()
        broken["thirty_day_plan"].pop()
        result, _, _ = self.run_generate(broken, return_evaluation_data=True)
        self.assertEqual(result["initial_strategy"], broken)
        self.assertEqual(result["reflection_result"]["issues"], ["Day 3 repeats day 2."])
        self.assertEqual(len(result["final_strategy"]["thirty_day_plan"]), 30)

    def test_the_reflection_sees_the_draft_the_qualification_and_the_allowed_services(self):
        _, _, model = self.run_generate(valid_strategy())
        prompt = model.prompts[0]
        self.assertIn("Prolonged Posting Inactivity", prompt)  # the qualification report
        self.assertIn("Social Media Strategy", prompt)          # the allowed agency services
        self.assertIn(START, prompt)

    def test_the_agent_gets_its_instructions_and_is_told_the_restaurant_is_interested(self):
        # Regression: the instructions once became "..." and the agent ran without its output format.
        _, executor, _ = self.run_generate(
            valid_strategy(), interest={"interested_on": START, "customer_request": "Restaurant selected Interested."},
        )
        sent = executor.inputs[0]
        self.assertIn("FINAL ANSWER FORMAT", sent)
        self.assertIn('clicking "Interested"', sent)
        self.assertIn(f"Strategy start date: {START}", sent)

    def test_a_reflection_without_a_corrected_strategy_is_an_error(self):
        with self.assertRaises(ValueError):
            self.run_generate(valid_strategy(), reflection_reply=json.dumps({"passed": True, "issues": []}))

    def test_reflection_output_that_is_not_json_is_an_error(self):
        with self.assertRaises(ValueError):  # json.JSONDecodeError is a ValueError
            self.run_generate(valid_strategy(), reflection_reply="I could not review it.")

class GuardrailSaveTests(unittest.TestCase):
    def test_invalid_strategy_is_not_saved(self):
        invalid_strategy = valid_strategy()
        invalid_strategy["thirty_day_plan"].pop()

        request = {
            "restaurant_id": 1,
            "qualification_run_id": 5,
            "qualification_context": QUALIFICATION,
            "strategy_start_date": START,
            "strategy_request_id": 1,
            "interest_event_id": 1,
        }

        with patch.object(
            strategy_agent,
            "generate_strategy_from_handoff",
            return_value=invalid_strategy,
        ), patch.object(
            strategy_agent,
            "save_strategy_result",
        ) as mock_save:

            with self.assertRaises(ValueError):
                strategy_agent.generate_and_save_strategy_from_handoff(request)

            mock_save.assert_not_called()

            
class ModelSelectionTests(unittest.TestCase):
    def built_with(self, env):
        strategy_agent.get_llm.cache_clear()
        self.addCleanup(strategy_agent.get_llm.cache_clear)
        with patch.dict("os.environ", env), patch.object(strategy_agent, "ChatOpenAI") as chat:
            os.environ.pop("OPENAI_MODEL", None) if "OPENAI_MODEL" not in env else None
            strategy_agent.get_llm()
        return chat.call_args.kwargs

    def test_the_default_model_is_the_one_the_qualification_agent_uses(self):
        self.assertEqual(self.built_with({}), {"model": "gpt-5.6-luna", "use_responses_api": True})

    def test_openai_model_in_env_changes_it(self):
        self.assertEqual(self.built_with({"OPENAI_MODEL": "another-model"})["model"], "another-model")


if __name__ == "__main__":
    unittest.main()
