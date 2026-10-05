import json
import sys
from datetime import datetime
from pathlib import Path

# Allow imports from the project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from database.database import SessionLocal
from database.models import Restaurant, ResearchRun


FILE_PATH = (
    PROJECT_ROOT
    / "research_results"
    / "ashi_sushi_research_20260916_171636.json"
)


def parse_datetime(value: str | None):
    if not value:
        return None

    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).replace(tzinfo=None)


def import_research_file(file_path: Path):

    print(f"Reading: {file_path.name}")

    with open(file_path, "r", encoding="utf-8") as file:
        data = json.load(file)

    restaurant_data = data["restaurant"]
    metadata = data.get("analysis_metadata", {})

    db = SessionLocal()

    try:
        # ---------------------------------
        # 1. Find restaurant
        # ---------------------------------

        restaurant = (
            db.query(Restaurant)
            .filter(
                Restaurant.instagram_username
                == restaurant_data["instagram_username"]
            )
            .first()
        )

        # ---------------------------------
        # 2. Create restaurant if missing
        # ---------------------------------

        if restaurant is None:

            restaurant = Restaurant(
                name=restaurant_data["name"],
                instagram_username=restaurant_data[
                    "instagram_username"
                ],
                instagram_url=restaurant_data.get(
                    "instagram_url"
                ),
                email=restaurant_data.get("email"),
                location=restaurant_data.get("location"),
            )

            db.add(restaurant)
            db.flush()

            print(
                f"Created restaurant: {restaurant.name}"
            )

        else:

            print(
                f"Restaurant already exists: {restaurant.name}"
            )

        # ---------------------------------
        # 3. Create ResearchRun
        # ---------------------------------

        research_run = ResearchRun(
            restaurant_id=restaurant.id,

            schema_version=data.get("schema_version"),

            analysis_version=metadata.get(
                "analysis_version"
            ),

            status=metadata.get(
                "status",
                "completed"
            ),

            source="imported",

            analyzed_at=parse_datetime(
                metadata.get("analyzed_at")
            ),

            profile=data.get("profile"),

            profile_analysis=data.get(
                "profile_analysis"
            ),

            metrics=data.get("metrics"),

            research_signals=data.get(
                "research_signals"
            ),

            content=data.get("content"),

            analysis_coverage=data.get(
                "analysis_coverage"
            ),

            data_quality=data.get(
                "data_quality"
            ),

            # IMPORTANT:
            # preserve the complete original JSON
            full_result=data,
        )

        db.add(research_run)
        db.commit()
        db.refresh(research_run)

        print()
        print("Research imported successfully!")
        print(f"Restaurant ID: {restaurant.id}")
        print(f"Research Run ID: {research_run.id}")
        print(f"Restaurant: {restaurant.name}")
        print(
            f"Instagram: @{restaurant.instagram_username}"
        )
        print(
            f"Schema version: {research_run.schema_version}"
        )
        print(
            f"Analysis version: {research_run.analysis_version}"
        )
        print(
            f"Analyzed at: {research_run.analyzed_at}"
        )
        print(
            f"Content items: {len(research_run.content or [])}"
        )
        print(
            f"Research signals: "
            f"{len(research_run.research_signals or [])}"
        )

    except Exception:
        db.rollback()
        raise

    finally:
        db.close()


if __name__ == "__main__":

    if not FILE_PATH.exists():
        raise FileNotFoundError(
            f"Research file not found:\n{FILE_PATH}"
        )

    import_research_file(FILE_PATH)