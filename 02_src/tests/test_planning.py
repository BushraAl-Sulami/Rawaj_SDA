"""Planning API behavior against temporary databases; no paid model calls."""

from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

with patch("dotenv.load_dotenv", return_value=False):
    from api import planning
    from database.database import Base
    from database.models import QualificationRun, ResearchRun, Restaurant, RestaurantContext, Strategy


class PlanningApiTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rawaj-planning-tests-")
        self.addCleanup(temporary.cleanup)
        path = Path(temporary.name) / "planning.db"
        self.engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"check_same_thread": False})
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        application = FastAPI()
        application.state.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        application.state.content_ideas_runner = self.fake_ideas
        application.include_router(planning.router)
        self.client = self.enterContext(TestClient(application))
        self.idea_calls = []
        self.cafe = self.add_restaurant("Brew Lab", "brew_lab", {
            "business_type": "cafe", "cuisine": "Specialty coffee",
            "target_audience": "nearby office workers", "signature_items": ["V60", "Date latte"],
            "goals": ["Increase morning visits"], "tone": "playful", "language": "English",
        })

    def add_restaurant(self, name, username, context):
        with Session(self.engine) as db:
            restaurant = Restaurant(name=name, instagram_username=username, location="Riyadh")
            db.add(restaurant)
            db.flush()
            restaurant_id = restaurant.id
            db.add(RestaurantContext(restaurant_id=restaurant_id, data=context))
            db.commit()
            return restaurant_id

    def base_url(self, restaurant_id=None):
        return f"/api/restaurants/{restaurant_id or self.cafe}"

    def create_plan(self, restaurant_id=None, month="2028-02", **options):
        response = self.client.post(f"{self.base_url(restaurant_id)}/strategy", json={"month": month, **options})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def change_context(self, **changes):
        with Session(self.engine) as db:
            context = db.get(RestaurantContext, self.cafe)
            context.data = {**context.data, **changes}
            db.commit()

    def fake_ideas(self, context):
        self.idea_calls.append(context)
        return {"ideas": [{
            "id": index, "name": f"Coffee idea {index}", "label": "Coffee discovery",
            "description": f"A real idea for {context['restaurant']['name']}.",
            "angle": f"Distinct angle {index}", "effort": "Low", "hook": f"What is your coffee moment {index}?",
            "content_format": context["task"]["type"], "why_it_fits": "Addresses the stored goal and task.",
        } for index in range(1, 4)]}

    def test_cafe_context_and_real_leap_month_dates_are_reflected(self):
        response = self.client.get(f"{self.base_url()}/strategy", params={"month": "2028-02"})
        self.assertEqual(response.status_code, 404)
        plan = self.create_plan()
        self.assertEqual(plan["business_type"], "cafe")
        self.assertEqual(plan["restaurant_name"], "Brew Lab")
        self.assertEqual(plan["generation_method"], "context_template")
        self.assertIn("Brew Lab", plan["summary"])
        self.assertIn("Riyadh", plan["summary"])
        self.assertIn("Specialty coffee", plan["summary"])
        self.assertIn("Date latte", plan["summary"])
        self.assertIn("nearby office workers", plan["summary"])
        self.assertIn("playful", plan["summary"])
        self.assertEqual(plan["goal"], "Increase morning visits")
        self.assertEqual(len(plan["tasks"]), 13)
        self.assertEqual(len({task["id"] for task in plan["tasks"]}), 13)
        self.assertEqual(plan["tasks"][-1]["date"], "2028-02-29")
        type_dates = {"Story": set(), "Post": set(), "Reel": set()}
        for task in plan["tasks"]:
            type_dates.setdefault(task["type"], set()).add(task["date"])
        self.assertIn("2028-02-03", type_dates["Story"])
        self.assertIn("2028-02-05", type_dates["Post"])
        self.assertIn("2028-02-08", type_dates["Reel"])
        self.assertTrue(any(task["title"].lower().startswith("profile") or "profile" in task["title"].lower() for task in plan["tasks"]))
        profile_tasks = [task for task in plan["tasks"] if "profile" in task["title"].lower() or "profile" in task["objective"].lower()]
        self.assertTrue(profile_tasks)
        self.assertEqual(profile_tasks[-1]["date"], "2028-02-29")
        for task in plan["tasks"]:
            self.assertEqual(date.fromisoformat(task["date"]).month, 2)
            self.assertEqual(task["status"], "Planned")
        self.assertEqual(plan["occasions"], [])

    def test_tasks_are_spread_over_the_month_and_never_pile_up_on_one_day(self):
        from collections import Counter

        for month, most_on_one_day in (("2026-09", 1), ("2026-10", 1), ("2028-02", 2), ("2027-02", 2)):
            with self.subTest(month=month):
                per_day = Counter(task["date"] for task in self.create_plan(month=month)["tasks"])
                self.assertLessEqual(max(per_day.values()), most_on_one_day, per_day)
                self.assertGreaterEqual(len(per_day), 11)

    def test_a_plan_does_not_name_a_business_type_that_was_not_stated(self):
        place = self.add_restaurant("3Brews", "three_brews", {"target_audience": "local guests"})
        plan = self.create_plan(place)
        shown = [plan["summary"], plan["goal"], plan["focus"], plan["pillars"], plan["tasks"]]  # what the dashboard displays
        text = str(shown).lower()
        self.assertIn("3Brews", plan["summary"])
        for word in ("restaurant", "cafe", "café", "dining", "coffee", "meal"):
            self.assertNotIn(word, text, word)
        self.assertEqual(plan["goal"], "Encourage more visits to 3Brews")

    def test_different_restaurants_and_months_have_separate_persisted_plans(self):
        restaurant_id = self.add_restaurant("Family Table", "family_table", {
            "business_type": "restaurant", "cuisine": "Saudi", "signature_items": ["Kabsa"],
            "target_audience": "families", "goals": ["More family dining"],
        })
        cafe = self.create_plan()
        restaurant = self.create_plan(restaurant_id)
        march = self.create_plan(month="2028-03")
        self.assertEqual(restaurant["business_type"], "restaurant")
        self.assertIn("Family Table", restaurant["summary"])
        self.assertIn("Kabsa", restaurant["summary"])
        self.assertIn("dining", restaurant["summary"])
        self.assertNotIn("V60", str(restaurant))
        self.assertNotIn("coffee", str(restaurant).lower())
        self.assertNotEqual(cafe["id"], restaurant["id"])
        self.assertNotEqual(cafe["id"], march["id"])
        response = self.client.get(f"{self.base_url()}/strategy", params={"month": "2028-02"})
        self.assertEqual(response.json(), cafe)
        self.assertEqual(march["tasks"][-1]["date"], "2028-03-31")

    def test_post_reuses_current_plan_and_keeps_explicit_regeneration_history(self):
        first = self.create_plan()
        second = self.create_plan()
        self.assertEqual(first, second)
        third = self.create_plan(regenerate=True)
        self.assertNotEqual(first["id"], third["id"])
        self.assertNotEqual(first["tasks"][0]["id"], third["tasks"][0]["id"])
        with Session(self.engine) as db:
            self.assertEqual(db.scalar(select(func.count(Strategy.id))), 2)
            self.assertEqual(db.get(Strategy, first["id"]).strategy_data, first)

    def test_concurrent_creation_is_idempotent(self):
        with ThreadPoolExecutor(max_workers=3) as pool:
            responses = list(pool.map(lambda _: self.client.post(
                f"{self.base_url()}/strategy", json={"month": "2028-02"},
            ), range(3)))
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(len({response.json()["id"] for response in responses}), 1)

    def test_context_change_invalidates_calendar_and_rejects_old_task_mutation(self):
        old = self.create_plan()
        self.change_context(business_type="restaurant", signature_items=["Kabsa"], goals=["Increase dinner visits"])
        with Session(self.engine) as db:
            db.get(Restaurant, self.cafe).name = "New Dining Room"
            db.commit()
        response = self.client.get(f"{self.base_url()}/strategy", params={"month": old["month"]})
        self.assertEqual(response.status_code, 404)
        update_url = f"{self.base_url()}/strategy/tasks/{old['tasks'][0]['id']}"
        response = self.client.patch(update_url, json={"month": old["month"], "status": "Completed"})
        self.assertEqual(response.status_code, 409)
        new = self.create_plan()
        self.assertEqual(new["restaurant_name"], "New Dining Room")
        self.assertEqual(new["business_type"], "restaurant")
        self.assertIn("Kabsa", new["summary"])
        self.assertNotEqual(new["context_signature"], old["context_signature"])
        self.assertNotEqual(new["id"], old["id"])
        response = self.client.patch(update_url, json={"month": old["month"], "status": "Completed"})
        self.assertEqual(response.status_code, 409)
        with Session(self.engine) as db:
            self.assertEqual(db.get(Strategy, old["id"]).strategy_data, old)

    def test_integrated_home_save_get_404_then_post_creates_current_plan(self):
        # Exercise the exact Home -> context -> GET strategy -> POST strategy
        # sequence through the real app, including its shared write lock.
        with patch("dotenv.load_dotenv", return_value=False):
            from api.main import create_app
        app = create_app(database_engine=self.engine)
        app.state.content_ideas_runner = self.fake_ideas
        client = self.enterContext(TestClient(app))
        base = self.base_url()
        old = client.post(f"{base}/strategy", json={"month": "2028-02"}).json()
        contexts = [
            {"name": "Morning Brew"},
            {"context": {"business_type": "restaurant", "signature_items": ["Kabsa"]}},
            {"location": "Jeddah", "context": {
                "business_type": "restaurant", "signature_items": ["Jareesh"],
                "goals": ["Increase family visits"], "target_audience": "families", "tone": "friendly",
            }},
        ]
        for changes in contexts:
            with self.subTest(changes=changes):
                saved = client.patch(base, json=changes)
                self.assertEqual(saved.status_code, 200, saved.text)
                current = client.get(f"{base}/context").json()["restaurant"]
                result = client.get(f"{base}/strategy", params={"month": "2028-02"})
                self.assertEqual(result.status_code, 404, result.text)
                self.assertEqual(client.patch(f"{base}/strategy/tasks/{old['tasks'][0]['id']}", json={
                    "month": "2028-02", "status": "Completed",
                }).status_code, 409)
                self.assertEqual(client.post(f"{base}/strategy/tasks", json={
                    "month": "2028-02", "date": "2028-02-01", "title": "Current menu",
                    "type": "Post", "objective": "Present real items",
                }).status_code, 409)
                self.assertEqual(client.post(f"{base}/content-ideas", json={
                    "month": "2028-02", "task_id": old["tasks"][0]["id"],
                }).status_code, 409)
                created = client.post(f"{base}/strategy", json={"month": "2028-02"})
                self.assertEqual(created.status_code, 200, created.text)
                new = created.json()
                self.assertNotEqual(new["id"], old["id"])
                self.assertEqual(new["restaurant_name"], current["name"])
                self.assertEqual(new["business_type"], current["context"]["business_type"])
                self.assertIn(current["location"], new["summary"])
                for item in current["context"]["signature_items"]:
                    self.assertIn(item, new["summary"])
                self.assertEqual(client.get(f"{base}/strategy", params={"month": "2028-02"}).json(), new)
                old = new
        self.assertEqual(self.idea_calls, [])

    def test_qualification_context_is_used_and_new_results_invalidate_plan(self):
        first = self.create_plan()
        with Session(self.engine) as db:
            research = ResearchRun(restaurant_id=self.cafe, full_result={"profile": {"biography": "Coffee"}})
            db.add(research)
            db.flush()
            qualification = QualificationRun(
                restaurant_id=self.cafe, research_run_id=research.id,
                marketing_gaps=[{"gap": "Low discovery", "recommendation_focus": "Show real drink preparation"}],
                full_result={"qualification": "qualified"},
            )
            db.add(qualification)
            db.commit()
            qualification_id = qualification.id
        self.assertEqual(self.client.get(f"{self.base_url()}/strategy", params={"month": first["month"]}).status_code, 404)
        current = self.create_plan()
        self.assertIn("Show real drink preparation", current["focus"])
        self.assertTrue(any("Show real drink preparation" in task["objective"] for task in current["tasks"]))
        with Session(self.engine) as db:
            self.assertEqual(db.get(Strategy, current["id"]).qualification_run_id, qualification_id)

    def test_saved_idea_completion_and_custom_tasks_persist(self):
        plan = self.create_plan()
        task = plan["tasks"][0]
        idea = self.fake_ideas({"restaurant": {"name": "Brew Lab"}, "task": task})["ideas"][0]
        response = self.client.patch(f"{self.base_url()}/strategy/tasks/{task['id']}", json={
            "month": plan["month"], "status": "Completed", "saved_idea": idea,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["tasks"][0]["saved_idea"], idea)
        response = self.client.post(f"{self.base_url()}/strategy/tasks", json={
            "month": plan["month"], "date": "2028-02-29", "type": "Story", "title": "Our real menu",
            "objective": "Let guests ask about our current menu",
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(len(response.json()["tasks"]), 14)
        saved = self.client.get(f"{self.base_url()}/strategy", params={"month": plan["month"]}).json()
        self.assertEqual(saved["tasks"][0]["status"], "Completed")
        self.assertEqual(saved["tasks"][0]["saved_idea"], idea)
        self.assertEqual(len(saved["tasks"]), 14)
        response = self.client.patch(f"{self.base_url()}/strategy/tasks/{task['id']}", json={
            "month": plan["month"], "saved_idea": None,
        })
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["tasks"][0]["saved_idea"])

    def test_validation_rejects_invalid_month_dates_empty_edits_and_formats(self):
        for month in ("2028-00", "2028-13", "0000-01", "2028-2", "not-a-month"):
            response = self.client.post(f"{self.base_url()}/strategy", json={"month": month})
            self.assertEqual(response.status_code, 422, response.text)
            response = self.client.get(f"{self.base_url()}/strategy", params={"month": month})
            self.assertEqual(response.status_code, 422, response.text)
        plan = self.create_plan()
        for task_date in ("2028-02-30", "2028-03-01", "2028-02-00"):
            response = self.client.post(f"{self.base_url()}/strategy/tasks", json={
                "month": plan["month"], "date": task_date, "type": "Story", "title": "Title", "objective": "Objective",
            })
            self.assertEqual(response.status_code, 422, response.text)
        task_url = f"{self.base_url()}/strategy/tasks/{plan['tasks'][0]['id']}"
        for extra in ({}, {"status": None}, {"status": "Published"}):
            response = self.client.patch(task_url, json={"month": plan["month"], **extra})
            self.assertEqual(response.status_code, 422, response.text)

    def test_task_and_content_requests_cannot_read_or_edit_other_restaurants(self):
        first = self.create_plan()
        other_id = self.add_restaurant("Other cafe", "other_cafe", {"business_type": "cafe"})
        self.create_plan(other_id)
        task_id = first["tasks"][0]["id"]
        response = self.client.patch(f"{self.base_url(other_id)}/strategy/tasks/{task_id}", json={
            "month": first["month"], "status": "Completed",
        })
        self.assertIn(response.status_code, (404, 409))
        response = self.client.post(f"{self.base_url(other_id)}/content-ideas", json={
            "month": first["month"], "task_id": task_id,
        })
        self.assertIn(response.status_code, (404, 409))
        self.assertEqual(self.idea_calls, [])
        self.assertEqual(self.client.post("/api/restaurants/999/strategy", json={"month": "2028-02"}).status_code, 404)

    def test_content_runner_gets_stored_context_and_task_not_browser_claims(self):
        plan = self.create_plan()
        response = self.client.post(f"{self.base_url()}/content-ideas", json={
            "month": plan["month"], "task_id": plan["tasks"][0]["id"], "feedback": "Keep it simple",
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(len(response.json()["ideas"]), 3)
        supplied = self.idea_calls[-1]
        self.assertEqual(supplied["restaurant"]["name"], "Brew Lab")
        self.assertEqual(supplied["restaurant"]["context"]["signature_items"], ["V60", "Date latte"])
        self.assertEqual(supplied["task"], plan["tasks"][0])
        self.assertEqual(supplied["strategy"]["business_type"], "cafe")
        self.assertEqual(supplied["feedback"], "Keep it simple")
        response = self.client.post(f"{self.base_url()}/content-ideas", json={
            "month": plan["month"], "task_id": plan["tasks"][0]["id"], "restaurant": {"name": "Forged"},
        })
        self.assertEqual(response.status_code, 422)

    def test_content_generation_errors_are_safe_and_schema_checked(self):
        plan = self.create_plan()
        payload = {"month": plan["month"], "task_id": plan["tasks"][0]["id"]}
        url = f"{self.base_url()}/content-ideas"
        self.client.app.state.content_ideas_runner = planning.generate_content_ideas
        with patch.dict("os.environ", {"OPENAI_API_KEY": ""}):
            response = self.client.post(url, json=payload)
        self.assertEqual(response.status_code, 503)
        secret = "do-not-leak-provider-key"

        def failing_runner(context):
            raise RuntimeError(secret)

        self.client.app.state.content_ideas_runner = failing_runner
        response = self.client.post(url, json=payload)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn(secret, response.text)
        for result in ({"ideas": []}, {"ideas": [{"id": 1}]}, None):
            self.client.app.state.content_ideas_runner = lambda context: result
            response = self.client.post(url, json=payload)
            self.assertEqual(response.status_code, 502)

    def test_context_changed_while_generating_does_not_return_obsolete_ideas(self):
        plan = self.create_plan()

        def slow_result(context):
            self.change_context(goals=["New marketing goal"])
            return self.fake_ideas(context)

        self.client.app.state.content_ideas_runner = slow_result
        response = self.client.post(f"{self.base_url()}/content-ideas", json={
            "month": plan["month"], "task_id": plan["tasks"][0]["id"],
        })
        self.assertEqual(response.status_code, 409, response.text)


if __name__ == "__main__":
    unittest.main()
