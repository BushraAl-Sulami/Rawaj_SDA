import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.database import SessionLocal
from database.models import Restaurant, ResearchRun


def check_database():
    db = SessionLocal()

    try:
        restaurants = db.query(Restaurant).all()
        research_runs = db.query(ResearchRun).all()

        print("\n=== DATABASE SUMMARY ===")
        print(f"Restaurants: {len(restaurants)}")
        print(f"Research runs: {len(research_runs)}")

        for restaurant in restaurants:
            print("\n----------------------------")
            print(f"Restaurant ID: {restaurant.id}")
            print(f"Name: {restaurant.name}")
            print(f"Instagram: @{restaurant.instagram_username}")
            print(f"Location: {restaurant.location}")

            runs = (
                db.query(ResearchRun)
                .filter(
                    ResearchRun.restaurant_id == restaurant.id
                )
                .all()
            )

            print(f"Research runs: {len(runs)}")

            for run in runs:
                print(f"  Run ID: {run.id}")
                print(f"  Status: {run.status}")
                print(f"  Source: {run.source}")
                print(f"  Analyzed at: {run.analyzed_at}")
                print(f"  Content items: {len(run.content or [])}")
                print(
                    f"  Research signals: "
                    f"{len(run.research_signals or [])}"
                )
                print(
                    f"  Full result stored: "
                    f"{run.full_result is not None}"
                )

    finally:
        db.close()


if __name__ == "__main__":
    check_database()