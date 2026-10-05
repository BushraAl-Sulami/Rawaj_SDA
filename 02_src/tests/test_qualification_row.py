"""Saving a qualification updates the restaurant's row in place instead of adding a new one (temporary database)."""

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

with patch("dotenv.load_dotenv", return_value=False):
    from database.database import Base
    from database.models import QualificationRun, ResearchRun, Restaurant
    from database.repository import save_qualification_result


def result(decision, gap, agent="Qualification Agent"):
    return {
        "agent": agent, "qualification": decision, "decision_rationale": f"why {decision}",
        "marketing_gaps": [{"gap": gap, "severity": "High", "priority": 1, "evidence": ["Menu visible: 10.71%"]}],
        "strengths": ["Strong branding"], "data_limitations": ["Reach was unavailable"],
    }


class QualificationRowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rawaj-qualification-row-")
        self.addCleanup(temporary.cleanup)
        self.engine = create_engine(f"sqlite:///{(Path(temporary.name) / 'row.db').as_posix()}")
        self.addCleanup(self.engine.dispose)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.addCleanup(self.db.close)
        self.restaurants = []
        for username in ("first_place", "second_place"):
            restaurant = Restaurant(name=username, instagram_username=username)
            self.db.add(restaurant)
            self.db.flush()
            self.restaurants.append(restaurant.id)
        self.research = []
        for restaurant_id in (self.restaurants[0], self.restaurants[0], self.restaurants[1]):
            run = ResearchRun(restaurant_id=restaurant_id, status="complete", full_result={})
            self.db.add(run)
            self.db.flush()
            self.research.append(run.id)
        self.db.commit()

    def rows(self, restaurant_id):
        return self.db.scalars(select(QualificationRun).where(QualificationRun.restaurant_id == restaurant_id)).all()

    def test_a_second_run_rewrites_the_same_row(self):
        first = save_qualification_result(self.db, self.restaurants[0], self.research[0], result("Qualified", "Old gap"))
        first_id, first_time = first.id, first.created_at
        second = save_qualification_result(self.db, self.restaurants[0], self.research[1], result("Not Qualified", "New gap"))

        self.assertEqual(second.id, first_id)  # the same row, so everything pointing at it stays valid
        rows = self.rows(self.restaurants[0])
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row.qualification, row.decision_rationale), ("Not Qualified", "why Not Qualified"))
        self.assertEqual(row.marketing_gaps[0]["gap"], "New gap")
        self.assertEqual(row.full_result["marketing_gaps"][0]["gap"], "New gap")  # full_result rewritten too
        self.assertEqual(row.research_run_id, self.research[1])  # linked to the research it was made from
        self.assertGreaterEqual(row.created_at, first_time)
        self.assertEqual((row.status, row.error_message), ("completed", None))

    def test_each_restaurant_has_its_own_row(self):
        save_qualification_result(self.db, self.restaurants[0], self.research[0], result("Qualified", "A"))
        save_qualification_result(self.db, self.restaurants[1], self.research[2], result("Qualified", "B"))
        save_qualification_result(self.db, self.restaurants[0], self.research[1], result("Qualified", "A again"))
        self.assertEqual(self.db.scalar(select(func.count(QualificationRun.id))), 2)
        self.assertEqual(self.rows(self.restaurants[1])[0].marketing_gaps[0]["gap"], "B")  # untouched by the other restaurant

    def test_a_restaurant_with_old_duplicate_rows_updates_only_the_latest(self):
        # Runs saved before this change left several rows for one restaurant.
        older = QualificationRun(restaurant_id=self.restaurants[0], research_run_id=self.research[0], status="completed",
                                 qualification="Qualified", full_result={"old": True}, created_at=datetime(2026, 9, 17))
        latest_row = QualificationRun(restaurant_id=self.restaurants[0], research_run_id=self.research[0], status="completed",
                                      qualification="Qualified", full_result={"old": False}, created_at=datetime(2026, 9, 21))
        self.db.add_all([older, latest_row])
        self.db.commit()
        older_id, latest_id = older.id, latest_row.id

        saved = save_qualification_result(self.db, self.restaurants[0], self.research[1], result("Qualified", "Y"))

        self.assertEqual(saved.id, latest_id)  # the most recent row is the one rewritten
        self.assertEqual(len(self.rows(self.restaurants[0])), 2)  # nothing is added or deleted
        self.assertEqual(self.db.get(QualificationRun, older_id).full_result, {"old": True})  # the older one is left alone


if __name__ == "__main__":
    unittest.main()
