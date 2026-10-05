from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from database.models import (
    Restaurant,
    ResearchRun,
    QualificationRun,
    Strategy,
)


def get_restaurant_by_id(
    db: Session,
    restaurant_id: int,
) -> Restaurant | None:

    return db.get(Restaurant, restaurant_id)


def get_restaurant_by_username(
    db: Session,
    instagram_username: str,
) -> Restaurant | None:

    statement = select(Restaurant).where(
        Restaurant.instagram_username == instagram_username
    )

    return db.scalar(statement)


def get_latest_research(
    db: Session,
    restaurant_id: int,
    content_limit: int | None = None,
    lookback_days: int | None = None,
) -> ResearchRun | None:

    filters = [
        ResearchRun.restaurant_id == restaurant_id,
        ResearchRun.status.in_(["completed", "complete", "partial"]),
    ]

    if content_limit is not None:
        filters.append(ResearchRun.content_limit == content_limit)
    if lookback_days is not None:
        filters.append(ResearchRun.lookback_days == lookback_days)

    statement = select(ResearchRun).where(*filters)
    statement = statement.order_by(
        ResearchRun.analyzed_at.desc().nullslast(),
        ResearchRun.id.desc(),
    ).limit(1)
    match = db.scalar(statement)
    if match is not None:
        return match

    if content_limit is not None and lookback_days is not None:
        fallback = (
            select(ResearchRun)
            .where(
                ResearchRun.restaurant_id == restaurant_id,
                ResearchRun.status.in_(["completed", "complete", "partial"]),
                ResearchRun.content_limit.is_(None),
                ResearchRun.lookback_days.is_(None),
            )
            .order_by(
                ResearchRun.analyzed_at.desc().nullslast(),
                ResearchRun.id.desc(),
            )
            .limit(1)
        )
        return db.scalar(fallback)

    return None


def build_qualification_input(
    research_run: ResearchRun,
) -> dict:

    restaurant = research_run.restaurant

    return {
        "restaurant": {
            "restaurant_id": restaurant.id,
            "name": restaurant.name,
            "instagram_username": restaurant.instagram_username,
            "instagram_url": restaurant.instagram_url,
            "email": restaurant.email,
            "location": restaurant.location,
        },
        "profile": research_run.profile,
        "profile_analysis": research_run.profile_analysis,
        "metrics": research_run.metrics,
        "research_signals": research_run.research_signals,
    }


def _parse_datetime(value: str | None) -> datetime | None:

    if not value:
        return None

    return datetime.fromisoformat(
        value.replace("Z", "+00:00")
    ).replace(tzinfo=None)


def save_research_result(
    db: Session,
    restaurant_id: int,
    result: dict,
    content_limit: int | None = None,
    lookback_days: int | None = None,
) -> ResearchRun:

    metadata = result.get("analysis_metadata", {})

    research_run = ResearchRun(
        restaurant_id=restaurant_id,

        schema_version=result.get("schema_version"),

        analysis_version=metadata.get("analysis_version"),

        status=metadata.get("status", "complete"),

        source="live",

        analyzed_at=_parse_datetime(
            metadata.get("analyzed_at")
        ),

        content_limit=content_limit,

        lookback_days=lookback_days,

        profile=result.get("profile"),

        profile_analysis=result.get("profile_analysis"),

        metrics=result.get("metrics"),

        research_signals=result.get("research_signals"),

        content=result.get("content"),

        analysis_coverage=result.get("analysis_coverage"),

        data_quality=result.get("data_quality"),

        full_result=result,
    )

    try:
        db.add(research_run)
        db.commit()
        db.refresh(research_run)

        return research_run

    except Exception:
        db.rollback()
        raise

def save_qualification_result(
    db: Session,
    restaurant_id: int,
    research_run_id: int,
    result: dict,
) -> QualificationRun:
    """
    Save the complete Qualification Agent result: one row per restaurant, updated in place.

    A restaurant that already has a qualification row gets that row rewritten with the new result (and the
    ResearchRun it was made from); only a restaurant without one gets a new row. The row keeps its id, so
    everything that points at it (outreach, strategies) keeps pointing at the current result.
    """

    fields = dict(
        research_run_id=research_run_id,
        status="completed",
        agent=result.get("agent"),
        qualification=result.get("qualification"),
        decision_rationale=result.get("decision_rationale"),
        marketing_gaps=result.get("marketing_gaps"),
        strengths=result.get("strengths"),
        data_limitations=result.get("data_limitations"),
        full_result=result,
        error_message=None,
        created_at=datetime.utcnow(),  # the table has no updated_at: this is the time of the latest result
    )

    try:
        qualification_run = db.scalar(
            select(QualificationRun)
            .where(QualificationRun.restaurant_id == restaurant_id)
            .order_by(QualificationRun.created_at.desc(), QualificationRun.id.desc())
            .limit(1)
        )
        if qualification_run is None:
            qualification_run = QualificationRun(restaurant_id=restaurant_id, **fields)
            db.add(qualification_run)
        else:
            for name, value in fields.items():
                setattr(qualification_run, name, value)
        db.commit()
        db.refresh(qualification_run)

        return qualification_run

    except Exception:
        db.rollback()
        raise


def save_strategy_result(
    db: Session,
    restaurant_id: int,
    qualification_run_id: int | None,
    result: dict,
) -> Strategy:
    """
    Save the generated Strategy Agent result.

    The strategy is linked to the restaurant and the QualificationRun
    that was used to generate it.
    """

    strategy = Strategy(
        restaurant_id=restaurant_id,
        restaurant_name=result.get("restaurant"),
        qualification_run_id=qualification_run_id,
        strategy_data=result,
        approved=False,
    )

    try:
        db.add(strategy)
        db.commit()
        db.refresh(strategy)

        return strategy

    except Exception:
        db.rollback()
        raise


def get_qualification_for_research(
    db: Session,
    research_run_id: int,
) -> QualificationRun | None:
    """
    Return an existing completed QualificationRun
    for a specific ResearchRun.

    This prevents the Qualification Agent from running
    again on research that has already been qualified.
    """

    statement = (
        select(QualificationRun)
        .where(
            QualificationRun.research_run_id == research_run_id,
            QualificationRun.status == "completed",
        )
        .order_by(QualificationRun.created_at.desc())
        .limit(1)
    )

    return db.scalar(statement)