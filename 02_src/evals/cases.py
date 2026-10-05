"""Strategy evaluation cases, built from the Qualification Agent results already stored in the database.

Each restaurant's latest completed qualification run becomes one case. The reference output is derived
from that same report (which High/Moderate gaps the strategy should address), so no hand-labelling is
needed to start; add hand-written cases to the LangSmith dataset later.
"""

from sqlalchemy import select

from database.database import SessionLocal
from database.models import QualificationRun, Restaurant

# One fixed start date keeps runs comparable; Saudi National Day (2026-09-23) falls on day 4.
START_DATE = "2026-09-20"


def build_cases() -> list[dict]:
    cases = []
    with SessionLocal() as db:
        for restaurant in db.scalars(select(Restaurant).order_by(Restaurant.id)):
            run = db.scalar(
                select(QualificationRun)
                .where(QualificationRun.restaurant_id == restaurant.id, QualificationRun.status == "completed")
                .order_by(QualificationRun.created_at.desc(), QualificationRun.id.desc())
            )
            if run is None or not run.full_result:
                continue
            gaps = run.marketing_gaps or []
            cases.append({
                "name": restaurant.name,
                "qualification_run_id": run.id,
                "inputs": {"qualification_context": run.full_result, "strategy_start_date": START_DATE},
                "reference": {
                    "restaurant": restaurant.name,
                    "high_gaps": [g["gap"] for g in gaps if g.get("severity") == "High"],
                    "moderate_gaps": [g["gap"] for g in gaps if g.get("severity") in ("Moderate", "Medium")],
                },
            })
    return cases
