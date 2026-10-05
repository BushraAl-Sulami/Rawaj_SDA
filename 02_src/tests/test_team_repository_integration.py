"""Integration check for the merged Rawaj team database and Outreach repository.

This test is intentionally skipped while the component sits alone in a staging
folder. It runs after the database models addition and outreach repository are
copied into the shared team project. No LLM, email, Calendar, or external
network call is made.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path

TEAM_DATABASE_AVAILABLE = False
TEAM_DATABASE_SKIP_REASON = ""

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database.database import Base
    import database.models as team_models

    # This optional validation hook lets the component staging folder prove the
    # append-only models block against an unchanged temporary team checkout.
    # In the merged project this variable is absent because the classes already
    # live in database.models after the one-time append.
    addition_path = os.getenv("RAWAJ_TEST_MODELS_ADDITION_PATH")
    if addition_path and not hasattr(team_models, "OutreachRelationship"):
        exec(Path(addition_path).read_text(encoding="utf-8"), team_models.__dict__)

    from database.models import QualificationRun, ResearchRun, Restaurant
    from database.outreach_repository import OutreachRepository
except ImportError as error:  # Staging folder has no team database module.
    TEAM_DATABASE_SKIP_REASON = (
        "Copy this test into the merged team repository after the database "
        f"addition: {error}"
    )
else:
    TEAM_DATABASE_AVAILABLE = True


@unittest.skipUnless(TEAM_DATABASE_AVAILABLE, TEAM_DATABASE_SKIP_REASON)
class TeamRepositoryIntegrationTests(unittest.TestCase):
    """Prove the actual repository understands the team Research/Qualification rows."""

    def setUp(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine, autoflush=False, autocommit=False)
        self.repository = OutreachRepository(session_factory=self.Session)

        with self.Session() as session:
            restaurant = Restaurant(
                name="Repository Demo Cafe",
                instagram_username="repository_demo_cafe",
                instagram_url="https://www.instagram.com/repository_demo_cafe/",
                email="owner@repository-demo.example",
                location="Jeddah",
            )
            session.add(restaurant)
            session.flush()

            research = ResearchRun(
                restaurant_id=restaurant.id,
                status="completed",
                source="test",
                research_signals=[
                    {
                        "signal_id": "public_signal",
                        "dimension": "content",
                        "observation": "Public posts show seasonal drinks.",
                    }
                ],
                full_result={
                    "restaurant": {
                        "id": restaurant.id,
                        "name": restaurant.name,
                        "email": restaurant.email,
                        "location": restaurant.location,
                    },
                    "research_signals": [
                        {
                            "signal_id": "public_signal",
                            "dimension": "content",
                            "observation": "Public posts show seasonal drinks.",
                        }
                    ],
                },
            )
            session.add(research)
            session.flush()

            qualification = QualificationRun(
                restaurant_id=restaurant.id,
                research_run_id=research.id,
                status="completed",
                agent="qualification-agent-test",
                qualification="Qualified for Rawaj.",
                marketing_gaps=[],
                strengths=["Seasonal product identity"],
                data_limitations=[],
                full_result={
                    "qualification": "Qualified for Rawaj.",
                    "marketing_gaps": [],
                    "strengths": ["Seasonal product identity"],
                    "data_limitations": [],
                },
            )
            session.add(qualification)
            session.commit()

            self.restaurant_id = restaurant.id
            self.research_run_id = research.id
            self.qualification_run_id = qualification.id

    def tearDown(self) -> None:
        self.engine.dispose()

    def test_repository_loads_exact_team_handoffs_and_creates_memory(self) -> None:
        research = self.repository.load_research_handoff(
            restaurant_id=self.restaurant_id,
            research_run_id=self.research_run_id,
        )
        qualification = self.repository.load_qualification_handoff(
            restaurant_id=self.restaurant_id,
            research_run_id=self.research_run_id,
            qualification_run_id=self.qualification_run_id,
        )
        relationship = self.repository.get_or_create_relationship(
            restaurant_id=self.restaurant_id,
            research_run_id=self.research_run_id,
            qualification_run_id=self.qualification_run_id,
        )

        self.assertEqual(research["restaurant"]["restaurant_id"], self.restaurant_id)
        self.assertEqual(research["research_run_id"], self.research_run_id)
        self.assertEqual(
            research["research_signals"][0]["signal_id"], "public_signal"
        )
        self.assertEqual(qualification["qualification"], "Qualified for Rawaj.")
        self.assertEqual(
            qualification["qualification_run_id"], self.qualification_run_id
        )
        self.assertEqual(relationship["restaurant_id"], self.restaurant_id)
        self.assertEqual(relationship["status"], "READY_TO_CONTACT")
        self.assertEqual(relationship["research_run_id"], self.research_run_id)
        self.assertEqual(
            relationship["qualification_run_id"], self.qualification_run_id
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
