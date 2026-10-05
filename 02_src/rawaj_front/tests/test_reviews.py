"""Exercise the real frontend with in-memory API responses; no backend writes."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

FRONT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(FRONT))
from ui import api, components, plan as plans, reviews


def sample_plan():
    return {
        "id": 11, "restaurant_id": 1, "restaurant_name": "Test Cafe", "source": "agent",
        "start_date": "2026-09-01", "end_date": "2026-09-30", "targets": ["Build consistency"],
        "services": [], "trends": [], "occasions": [], "approved": False,
        "days": [
            {"day": 1, "date": "2026-09-01", "focus": "Post", "action": "Share a dish", "status": "Planned", "counts": True},
            {"day": 2, "date": "2026-09-02", "focus": "Story", "action": "Share a story", "status": "Planned", "counts": True},
            {"day": 3, "date": "2026-09-03", "focus": "Break", "action": "Rest", "status": "Planned", "counts": False},
        ],
    }


class ReviewFlowTests(unittest.TestCase):
    def setUp(self):
        self.restaurant = {"id": 1, "name": "Test Cafe", "email": "test@example.test"}
        self.plan = sample_plan()
        self.enterContext(patch.object(components, "current_restaurant", return_value=self.restaurant))
        self.enterContext(patch.object(api, "get_agent_strategy", side_effect=lambda rid: deepcopy(self.plan)))
        self.status_api = self.enterContext(patch.object(api, "set_day_status", side_effect=self.set_status))
        self.enterContext(patch.object(api, "get_gaps", return_value={
            "counts": {"total": 0, "high": 0, "moderate": 0, "low": 0, "strengths": 0},
            "gaps": [], "strengths": [], "restaurant_name": "Test Cafe", "qualification_id": None,
        }))
        self.app = AppTest.from_file(str(FRONT / "app.py"), default_timeout=10)
        self.app.session_state.authenticated = True
        self.app.session_state.user_email = self.restaurant["email"]
        self.app.run()
        self.assertFalse(self.app.exception)

    def set_status(self, restaurant_id, day, status):
        self.assertEqual(restaurant_id, 1)
        next(item for item in self.plan["days"] if item["day"] == day)["status"] = status
        return {}

    def complete_calendar(self):
        self.app.switch_page("views/strategy.py").run()
        self.app.button(key="mark_day-1").click().run()
        self.assertFalse(self.app.text_area)  # one of two tasks: no prompt
        self.app.button(key="day_2026-09-02").click().run()
        self.app.button(key="mark_day-2").click().run()
        self.assertFalse(self.app.exception)
        self.assertEqual(len(self.app.text_area), 1)

    def submit(self):
        next(button for button in self.app.button if button.label in {"Save review", "Update review"}).click().run()

    def test_last_task_opens_review_and_saved_review_appears_on_home(self):
        self.complete_calendar()
        self.assertFalse(self.plan["approved"])  # strategy approval is unrelated
        self.assertEqual(self.plan["days"][2]["status"], "Planned")  # break does not block
        self.submit()
        self.assertTrue(self.app.error)  # rating/text are required
        self.app.radio[0].set_value(4)
        self.app.text_area[0].set_value("Clear tasks. <script>unsafe</script>")
        self.submit()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.text_area)
        self.app.switch_page("views/home.py").run()
        html = "\n".join(item.value for item in self.app.markdown)
        self.assertIn("Your review", html)
        self.assertIn("Clear tasks.", html)
        self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", html)
        self.assertNotIn("<script>unsafe</script>", html)
        self.assertEqual(self.status_api.call_count, 2)  # saving review made no status/API write
        self.app.switch_page("views/strategy.py").run()
        self.assertFalse(self.app.text_area)  # saved review does not reopen automatically
        next(button for button in self.app.button if button.label == "Edit review").click().run()
        self.assertEqual(self.app.text_area[0].value, "Clear tasks. <script>unsafe</script>")

    def test_later_does_not_loop_and_review_can_be_reopened(self):
        self.complete_calendar()
        next(button for button in self.app.button if button.label == "Maybe later").click().run()
        self.assertFalse(self.app.text_area)
        self.app.run()
        self.assertFalse(self.app.text_area)
        next(button for button in self.app.button if button.label == "Write a review").click().run()
        self.assertEqual(len(self.app.text_area), 1)

    def test_break_only_plan_is_not_complete(self):
        self.plan["days"] = self.plan["days"][2:]
        self.app.switch_page("views/strategy.py").run()
        self.assertFalse(self.app.exception)
        self.assertFalse(self.app.text_area)

    def test_new_plan_and_other_restaurant_have_distinct_reviews(self):
        loaded = plans.load(self.restaurant)
        key = reviews.review_key(self.restaurant, loaded)
        self.assertNotEqual(key, reviews.review_key({"id": 2}, loaded))
        self.assertNotEqual(key, reviews.review_key(self.restaurant, {**loaded, "id": 12}))

    def test_completion_uses_exact_task_count_not_rounded_percentage(self):
        tasks = [{"day": n + 1, "format": "Post", "status": "Completed"} for n in range(200)]
        tasks[-1]["status"] = "Planned"
        self.assertEqual(plans.progress(tasks)["percent"], 100)
        self.assertFalse(reviews.all_tasks_complete({"tasks": tasks}))
        self.assertFalse(reviews.all_tasks_complete({"tasks": []}))


if __name__ == "__main__":
    unittest.main()
