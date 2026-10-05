"""The strategy saved for a restaurant, in one shape for the dashboard, and progress on its days.

A row of ``strategies.strategy_data`` is in one of two formats and both are read here:

- **agent**: written by the Strategy Agent (agents/strategy_agent): targets, primary gaps, recommended services and a
  ``thirty_day_plan`` of numbered days that count from ``strategy_start_date`` (older rows fall back to the row's date).
- **template**: a monthly content plan (api/planning.py): goal, summary, focus, pillars and dated ``tasks`` with a
  content type (Post, Reel or Story) and a status.

The latest row of either kind is the restaurant's current strategy. Its "days" are numbered 1..N (agent: the plan's day
numbers; template: the tasks in date order). Completing a day is saved in the row itself (``day_status`` for an agent
strategy, the task's own ``status`` for a template). Nothing here calls a model.
"""

import calendar
import logging
import re
from copy import deepcopy
from datetime import date, timedelta
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from api import planning
from agents.strategy_agent.content_ideas import ContentIdeasUnavailable, Idea, IdeaResponse, generate_content_ideas
from agents.strategy_agent.occasions import occasion_names_on, occasions_between
from api.schemas import InputModel
from database.models import Restaurant, Strategy

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/restaurants", tags=["Strategy"])
RestaurantId = Annotated[int, Path(gt=0)]
DayStatus = Literal["Planned", "Completed"]
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


class PlanDay(BaseModel):
    day: int
    date: str
    focus: str
    action: str
    status: DayStatus = "Planned"
    format: str | None = None  # Post / Reel / Story for a template plan
    ideas: bool = True  # False for a day with nothing to publish (a break, a profile update): no content ideas for it
    ideas_note: str = ""
    counts: bool = True  # False for a break: it is not a task, so the progress leaves it out


class PrimaryGap(BaseModel):
    gap: str
    severity: str
    highlight: str | None = None
    highlight_label: str | None = None
    key_point: str = ""


class RecommendedService(BaseModel):
    service: str
    why_this_service_fits: str = ""


class TrendSupport(BaseModel):
    insight: str
    source_url: str | None = None


class Pillar(BaseModel):
    title: str
    description: str = ""
    metric: str = ""


class Occasion(BaseModel):
    name: str
    start_date: str
    end_date: str
    type: str = ""
    date_status: str = ""  # "tentative" when the date depends on the moon sighting


class AgentStrategyResponse(BaseModel):
    id: int
    restaurant_id: int
    restaurant_name: str
    source: Literal["agent", "template"] = "agent"
    approved: bool
    strategy_request_id: str | None = None
    interest_event_id: str | None = None
    month: str | None = None
    start_date: str
    end_date: str
    summary: str = ""
    focus: str = ""
    targets: list[str]
    pillars: list[Pillar] = []
    gaps: list[PrimaryGap]
    services: list[RecommendedService]
    trends: list[TrendSupport]
    occasions: list[Occasion] = []
    days: list[PlanDay]


class DayUpdate(InputModel):
    status: DayStatus


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _items(data: dict, key: str) -> list[dict]:
    value = data.get(key)
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _kind(data: Any) -> str | None:
    if not isinstance(data, dict):
        return None
    if isinstance(data.get("thirty_day_plan"), list):
        return "agent"
    if isinstance(data.get("tasks"), list) and _MONTH.match(str(data.get("month") or "")):
        return "template"
    return None


def _latest(db: Session, restaurant_id: int) -> Strategy | None:
    rows = db.scalars(select(Strategy).where(Strategy.restaurant_id == restaurant_id).order_by(Strategy.id.desc()))
    return next((row for row in rows if _kind(row.strategy_data)), None)


# ---------------------------------------------------------------- agent strategies

def _start_date(strategy: Strategy) -> date:
    """Day 1 of the calendar is the strategy's saved start date.

    The agent counts its 30 days from the date the restaurant said it was interested (kept as `strategy_start_date`).
    Older strategies without a saved start date use their creation date.
    """
    try:
        started = date.fromisoformat(str(strategy.strategy_data.get("strategy_start_date")))
    except ValueError:
        started = strategy.created_at.date() if strategy.created_at else date.today()
    return started


def _ideas_fields(focus: str) -> dict:
    """Whether content ideas make sense for a day, and if not, why. A break has nothing to publish, and a profile
    update (the monthly plan's "Profile refresh") is a task on the account itself, not a post, reel or story."""
    name = focus.strip().lower()
    if name == "break":
        return {"ideas": False, "counts": False,
                "ideas_note": "Break day: nothing is planned to publish, so there are no content ideas."}
    if name.startswith("profile"):  # a profile update is still a task to do, so it counts in the progress
        return {"ideas": False, "counts": True,
                "ideas_note": "Profile update: this is a change to the account, not content, so there are no ideas for it."}
    return {"ideas": True, "counts": True, "ideas_note": ""}


def _agent_days(strategy: Strategy) -> list[dict]:
    """Valid, de-duplicated plan days in day order, with dates and completion status."""
    data, start = strategy.strategy_data, _start_date(strategy)
    done = data.get("day_status") if isinstance(data.get("day_status"), dict) else {}
    days: dict[int, dict] = {}
    for item in _items(data, "thirty_day_plan"):
        number = item.get("day")
        if isinstance(number, bool) or not isinstance(number, int) or number < 1 or number in days:
            continue
        days[number] = {
            "day": number,
            "date": (start + timedelta(days=number - 1)).isoformat(),
            "focus": _text(item.get("focus")),
            "action": _text(item.get("action")),
            "status": "Completed" if done.get(str(number)) == "Completed" else "Planned",
            **_ideas_fields(_text(item.get("focus"))),
        }
    return [days[number] for number in sorted(days)]


# ---------------------------------------------------------------- template strategies (monthly content plan)

def _template_tasks(data: dict) -> list[dict]:
    """The plan's tasks in date order (their position is the 'day' number)."""
    tasks = []
    for item in _items(data, "tasks"):
        try:
            date.fromisoformat(str(item.get("date")))
        except ValueError:
            continue
        if _text(item.get("id")):
            tasks.append(item)
    return sorted(tasks, key=lambda item: (item["date"], item["id"]))


def _template_days(strategy: Strategy) -> list[dict]:
    return [
        {
            "day": position, "date": task["date"], "focus": _text(task.get("title")), "action": _text(task.get("objective")),
            "status": "Completed" if task.get("status") == "Completed" else "Planned", "format": _text(task.get("type")) or None,
            **_ideas_fields(_text(task.get("title"))),
        }
        for position, task in enumerate(_template_tasks(strategy.strategy_data), 1)
    ]


def _plan_days(strategy: Strategy) -> list[dict]:
    return _agent_days(strategy) if _kind(strategy.strategy_data) == "agent" else _template_days(strategy)


# ---------------------------------------------------------------- response

def _response(db: Session, strategy: Strategy) -> dict:
    """The strategy with Saudi occasions across the full months shown in its calendar."""
    result = _plan_response(db, strategy)
    start = date.fromisoformat(result["start_date"])
    end = date.fromisoformat(result["end_date"])
    result["occasions"] = occasions_between(
        start.replace(day=1),
        end.replace(day=calendar.monthrange(end.year, end.month)[1]),
    )
    return result


def _plan_response(db: Session, strategy: Strategy) -> dict:
    data = strategy.strategy_data
    kind = _kind(data)
    days = _plan_days(strategy)
    restaurant = db.get(Restaurant, strategy.restaurant_id)
    fallback_name = restaurant.name if restaurant else ""
    common = {"id": strategy.id, "restaurant_id": strategy.restaurant_id, "source": kind, "approved": bool(strategy.approved), "days": days}

    if kind == "template":
        year, month = map(int, data["month"].split("-"))
        return {
            **common,
            "restaurant_name": _text(data.get("restaurant_name")) or fallback_name,
            "month": data["month"],
            "start_date": date(year, month, 1).isoformat(),
            "end_date": date(year, month, calendar.monthrange(year, month)[1]).isoformat(),
            "summary": _text(data.get("summary")),
            "focus": _text(data.get("focus")),
            "targets": [text for text in [_text(data.get("goal"))] if text],
            "pillars": [
                {"title": _text(item.get("title")), "description": _text(item.get("description")), "metric": _text(item.get("metric"))}
                for item in _items(data, "pillars") if _text(item.get("title"))
            ],
            "gaps": [], "services": [], "trends": [],
        }

    return {
        **common,
        "restaurant_name": _text(data.get("restaurant")) or fallback_name,
        "strategy_request_id": _text(data.get("strategy_request_id")) or None,
        "interest_event_id": _text(data.get("interest_event_id")) or None,
        "start_date": days[0]["date"] if days else _start_date(strategy).isoformat(),
        "end_date": days[-1]["date"] if days else _start_date(strategy).isoformat(),
        "targets": [text for text in map(_text, data.get("thirty_day_target") or []) if text],
        "gaps": [
            {
                "gap": _text(item.get("gap")), "severity": _text(item.get("severity")),
                "highlight": str(item["highlight"]) if item.get("highlight") not in (None, "") else None,
                "highlight_label": _text(item.get("highlight_label")) or None,
                "key_point": _text(item.get("key_point")),
            }
            for item in _items(data, "primary_marketing_gaps") if _text(item.get("gap"))
        ],
        "services": [
            {"service": _text(item.get("service")), "why_this_service_fits": _text(item.get("why_this_service_fits"))}
            for item in _items(data, "recommended_services") if _text(item.get("service"))
        ],
        "trends": [
            {"insight": _text(item.get("insight")), "source_url": _text(item.get("source_url")) or None}
            for item in _items(data, "external_trend_support") if _text(item.get("insight"))
        ],
    }


def _require_restaurant(db: Session, restaurant_id: int) -> None:
    if db.get(Restaurant, restaurant_id) is None:
        raise HTTPException(404, "Restaurant not found")


@router.get("/{restaurant_id}/agent-strategy", response_model=AgentStrategyResponse)
def get_agent_strategy(restaurant_id: RestaurantId, db: Database):
    """The restaurant's current strategy (the latest saved one, whichever format it has)."""
    _require_restaurant(db, restaurant_id)
    strategy = _latest(db, restaurant_id)
    if strategy is None:
        raise HTTPException(404, "No strategy has been saved for this restaurant yet")
    return _response(db, strategy)


@router.patch("/{restaurant_id}/agent-strategy/days/{day}", response_model=AgentStrategyResponse)
def update_day(restaurant_id: RestaurantId, day: Annotated[int, Path(ge=1)], payload: DayUpdate,
               request: Request, db: Database):
    with request.app.state.restaurant_write_lock:
        _require_restaurant(db, restaurant_id)
        strategy = _latest(db, restaurant_id)
        if strategy is None:
            raise HTTPException(404, "No strategy has been saved for this restaurant yet")
        if day not in {item["day"] for item in _plan_days(strategy)}:
            raise HTTPException(404, "Day not found in this restaurant's strategy")
        data = deepcopy(strategy.strategy_data)
        if _kind(data) == "agent":
            statuses = data["day_status"] if isinstance(data.get("day_status"), dict) else {}
            statuses[str(day)] = payload.status
            data["day_status"] = statuses
        else:
            wanted = _template_tasks(data)[day - 1]["id"]
            for task in data["tasks"]:
                if isinstance(task, dict) and task.get("id") == wanted:
                    task["status"] = payload.status
        strategy.strategy_data = data
        db.commit()
        return _response(db, strategy)


class DayIdeasRequest(InputModel):
    previous_ideas: list[Idea] = []
    feedback: Annotated[str, Field(max_length=2000)] = ""


@router.post("/{restaurant_id}/agent-strategy/days/{day}/ideas", response_model=IdeaResponse)
def day_content_ideas(restaurant_id: RestaurantId, day: Annotated[int, Path(ge=1)], payload: DayIdeasRequest,
                      request: Request, db: Database):
    """Three content ideas for one day of the current strategy, from the restaurant's own context.

    Send the ideas already shown as ``previous_ideas`` to get three different ones ("generate another").
    """
    _require_restaurant(db, restaurant_id)
    strategy = _latest(db, restaurant_id)
    if strategy is None:
        raise HTTPException(404, "No strategy has been saved for this restaurant yet")
    item = next((entry for entry in _plan_days(strategy) if entry["day"] == day), None)
    if item is None:
        raise HTTPException(404, "Day not found in this restaurant's strategy")
    if not item["ideas"]:
        raise HTTPException(422, item["ideas_note"])

    snapshot, _ = planning._context_snapshot(db, restaurant_id)
    overview = _response(db, strategy)
    context = {
        **snapshot,
        "business_type": planning._stated_business_type(snapshot["restaurant"]["context"]) or "unspecified",
        "task": {
            "day": day, "date": item["date"], "focus": item["focus"], "action": item["action"],
            "content_format": item.get("format"),
            "occasions": occasion_names_on(overview["occasions"], date.fromisoformat(item["date"])),
        },
        "strategy": {key: overview.get(key) for key in ("summary", "focus", "targets", "pillars", "gaps", "services")},
        "previous_ideas": [idea.model_dump() for idea in payload.previous_ideas],
        "feedback": payload.feedback,
    }
    runner = getattr(request.app.state, "content_ideas_runner", generate_content_ideas)
    try:
        result = runner(context)
        ideas = IdeaResponse.model_validate(result.model_dump() if hasattr(result, "model_dump") else result)
        if item.get("format") and any(idea.content_format != item["format"] for idea in ideas.ideas):
            raise ValueError("Generated ideas did not match the selected task format")
    except ContentIdeasUnavailable:
        raise HTTPException(503, "Content ideas are unavailable. Configure OPENAI_API_KEY in the project's .env file.") from None
    except Exception as exc:
        logger.error("Content generation failed (%s): %s", type(exc).__name__, exc)
        raise HTTPException(502, "Could not generate content ideas. Check the server configuration and retry.") from None
    return ideas
