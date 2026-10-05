"""Persisted, restaurant-specific monthly plans and optional AI content ideas.

Monthly plans are honest, deterministic context templates. Content ideas alone
call a model, only when requested, using the existing server-side credentials.
"""

import calendar
from copy import deepcopy
import hashlib
import json
import logging
import os
from threading import Lock
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from api import services
from agents.strategy_agent.content_ideas import ContentIdeasUnavailable, generate_content_ideas
from api.planning_schemas import (
    IdeaRequest, IdeaResponse, Month, StrategyRequest, StrategyResponse,
    StrategyTask, TaskCreate, TaskUpdate,
)
from database.models import Restaurant, RestaurantContext, Strategy


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/restaurants", tags=["Planning"])
RestaurantId = Annotated[int, Path(gt=0)]
_fallback_write_lock = Lock()


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


def _write_lock(request):
    return getattr(request.app.state, "restaurant_write_lock", _fallback_write_lock)


def _context_snapshot(db, restaurant_id):
    restaurant = db.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(404, "Restaurant not found")
    stored_context = db.get(RestaurantContext, restaurant_id)
    research, qualification, _ = services.context_runs(db, restaurant_id)
    # Include the complete stored context. A menu, goal, name or analysis change
    # must never silently reuse a calendar prepared for a previous business.
    snapshot = {
        "restaurant": {
            "id": restaurant.id,
            "name": restaurant.name,
            "instagram_username": restaurant.instagram_username,
            "instagram_url": restaurant.instagram_url,
            "email": restaurant.email,
            "location": restaurant.location,
            "is_active": restaurant.is_active,
            "context": stored_context.data if stored_context else {},
        },
        "research": {
            "id": research.id, "status": research.status, "result": research.full_result,
        } if research else None,
        "qualification": {
            "id": qualification.id,
            "research_run_id": qualification.research_run_id,
            "result": qualification.full_result,
            "marketing_gaps": qualification.marketing_gaps,
            "strengths": qualification.strengths,
            "decision_rationale": qualification.decision_rationale,
        } if qualification else None,
    }
    signature = hashlib.sha256(
        json.dumps(snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return snapshot, signature


def _latest_plan(db, restaurant_id, month):
    return db.scalar(
        select(Strategy)
        .where(Strategy.restaurant_id == restaurant_id, Strategy.strategy_data["month"].as_string() == month)
        .order_by(Strategy.id.desc())
        .limit(1)
    )


def _require_current_plan(db, restaurant_id, month):
    snapshot, signature = _context_snapshot(db, restaurant_id)
    strategy = _latest_plan(db, restaurant_id, month)
    if strategy is None:
        raise HTTPException(404, "Create a monthly strategy first")
    if strategy.strategy_data.get("context_signature") != signature:
        raise HTTPException(409, "Restaurant context changed. Refresh the monthly strategy and select a current task.")
    return strategy, snapshot, signature


def _response(strategy):
    return StrategyResponse.model_validate({**strategy.strategy_data, "id": strategy.id})


def _text(value):
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, tuple)):
        return ", ".join(part for item in value if (part := _text(item)))
    return ""


_CAFE_WORDS = {"cafe", "café", "coffee shop", "coffee", "كافيه", "كافي", "مقهى"}
_RESTAURANT_WORDS = {"restaurant", "مطعم"}


def _business_type(context):
    value = _text(context.get("business_type") or context.get("type") or context.get("restaurant_type")).lower()
    return "cafe" if value in _CAFE_WORDS else "restaurant"


def _stated_business_type(context):
    """"cafe" or "restaurant" only when the stored context says so; None otherwise (never guessed)."""
    value = _text(context.get("business_type") or context.get("type") or context.get("restaurant_type")).lower()
    if value in _CAFE_WORDS:
        return "cafe"
    return "restaurant" if value in _RESTAURANT_WORDS else None


# Words used in a monthly plan. When the business type is not stated the plan says neither "restaurant" nor "cafe".
_WORDS = {
    "cafe": {
        "subject": "coffee and cafe visits", "goal": "Encourage repeat coffee visits to {name}", "menu": "the current drinks menu",
        "p1": "Coffee discovery", "p2": "Cafe moments", "p2_text": "coffee preparation and cafe atmosphere", "p3": "Local regulars",
        "t1": "Your coffee moment", "t1_text": "a coffee visit", "reel": "Behind the coffee", "made": "drinks",
        "talk": "Coffee conversation", "moment": "A moment at the cafe", "next": "Your next coffee visit", "offer": "coffee offering",
    },
    "restaurant": {
        "subject": "meals and the dining experience", "goal": "Encourage more dining visits to {name}", "menu": "the current food menu",
        "p1": "Menu discovery", "p2": "At the table", "p2_text": "meal preparation and dining atmosphere", "p3": "Guest connections",
        "t1": "Your next meal", "t1_text": "a dining visit", "reel": "From kitchen to table", "made": "dishes",
        "talk": "Table conversation", "moment": "A moment around the table", "next": "Your next dining visit", "offer": "food offering",
    },
    "neutral": {
        "subject": "menu and the in-person experience", "goal": "Encourage more visits to {name}", "menu": "the current menu",
        "p1": "Menu discovery", "p2": "In the moment", "p2_text": "preparation and atmosphere", "p3": "Guest connections",
        "t1": "Your next visit", "t1_text": "a visit", "reel": "Behind the scenes", "made": "menu items",
        "talk": "Guest conversation", "moment": "A moment with the team", "next": "Your next visit", "offer": "menu",
    },
}


def _gap_focus(snapshot):
    qualification = snapshot.get("qualification") or {}
    gaps = qualification.get("marketing_gaps") or (qualification.get("result") or {}).get("marketing_gaps") or []
    for item in gaps:
        if isinstance(item, dict):
            focus = _text(item.get("recommendation_focus") or item.get("gap"))
        else:
            focus = _text(item)
        if focus:
            return focus
    return ""


def build_plan(snapshot, signature, month, strategy_id):
    """Use supplied business facts without inventing menu items or promotions."""
    restaurant = snapshot["restaurant"]
    context = restaurant["context"]
    name = restaurant["name"]
    business_type = _business_type(context)
    stated = _stated_business_type(context)
    words = _WORDS[stated or "neutral"]
    subject = words["subject"]
    audience = _text(context.get("target_audience")) or "local guests"
    cuisine = _text(context.get("cuisine"))
    menu = _text(context.get("signature_items"))
    tone = _text(context.get("tone") or context.get("brand_tone")) or "clear and welcoming"
    location = restaurant.get("location") or _text(context.get("location"))
    goal = _text(context.get("goals") or context.get("goal")) or words["goal"].format(name=name)
    menu_subject = menu or words["menu"]
    focus = f"Introduce {name}'s {subject} to {audience}."
    if cuisine:
        focus += f" Reflect the supplied {cuisine} offering."
    gap_focus = _gap_focus(snapshot)
    if gap_focus:
        focus += f" Analysis priority: {gap_focus}."
    summary = f"A {month} content plan for {name}"  # the business type is never named in the text
    summary += f" in {location}" if location else ""
    summary += f". Focus on {goal.lower()} for {audience}, using a {tone} tone."
    if menu:
        summary += f" Feature the supplied signature items: {menu}."
    if cuisine:
        summary += f" Cuisine or specialty: {cuisine}."

    pillars = [
        {"title": words["p1"],
         "description": f"Help {audience} discover {menu_subject} at {name}.",
         "metric": "Menu questions and post saves"},
        {"title": words["p2"],
         "description": f"Show the real {words['p2_text']} at {name}.",
         "metric": "Reel views and profile visits"},
        {"title": words["p3"],
         "description": f"Start a conversation with {audience}" + (f" in {location}" if location else "") + f" to support: {goal}.",
         "metric": "Story replies and visit enquiries"},
    ]
    templates = [
        ("Story", words["t1"], f"Ask {audience} what they look for in {words['t1_text']} at {name}."),
        ("Post", f"Discover {menu_subject}", f"Show only confirmed items from {name}'s menu; help guests decide what to try."),
        ("Reel", f"Meet {name}", f"Introduce {name} and its {subject} to {audience}."),
        ("Story", f"Ask {name}", f"Invite questions about the real menu and visit experience; support: {goal}."),
        ("Post", f"A closer look at {menu_subject}", f"Explain the appeal of the confirmed menu at {name}" + (f" and its {cuisine} specialty" if cuisine else "") + "."),
        ("Reel", words["reel"], f"Show how the team at {name} prepares its actual {words['made']}, in a {tone} tone."),
        ("Story", words["talk"], f"Invite {audience} to share their preferences about {menu_subject}; use a {tone} tone."),
        ("Post", f"Plan a visit to {name}", (f"Help guests find {name} in {location}." if location else f"Invite guests to contact {name} for confirmed visit details.") + " Use verified location and opening information only."),
        ("Reel", words["moment"], f"Show the real atmosphere of {name} for {audience}; do not imply facilities that have not been confirmed."),
        ("Story", f"Help shape next month at {name}", f"Ask {audience} which confirmed menu items or moments they want to see next month."),
        ("Post", words["next"], f"Invite a return visit to {name} through its real {words['offer']}; support: {goal}."),
        ("Reel", f"Why try {name}?", f"Connect {menu_subject} with {audience} and the goal: {goal}."),
    ]
    year, month_number = map(int, month.split("-"))
    days_in_month = calendar.monthrange(year, month_number)[1]
    # Four of each type, about a week apart; a day past the end of a short month is pulled back to its last day.
    # The profile refresh below closes the month, so no more than two tasks ever share a day.
    scheduled_days = {
        "Story": [3, 11, 18, 25],
        "Post": [5, 13, 21, 27],
        "Reel": [8, 16, 24, 29],
    }
    type_counts = {"Story": 0, "Post": 0, "Reel": 0}
    tasks = []
    for index, (kind, title, objective) in enumerate(templates):
        occurrence = type_counts[kind]
        preferred = scheduled_days.get(kind, [None])[min(occurrence, len(scheduled_days.get(kind, [None])) - 1)]
        day = days_in_month if preferred is None else preferred
        if preferred is not None and day > days_in_month:
            day = days_in_month
        if gap_focus and index in (3, 6, 9):
            objective += f" Address the analysis priority: {gap_focus}."
        tasks.append(StrategyTask(
            id=f"strategy-{strategy_id}-task-{index + 1:02}", date=f"{month}-{day:02}",
            type=kind, title=title, objective=objective,
        ).model_dump())
        type_counts[kind] += 1

    profile_day = days_in_month
    tasks.append(StrategyTask(
        id=f"strategy-{strategy_id}-task-{len(tasks) + 1:02}",
        date=f"{month}-{profile_day:02}",
        type="Story",
        title="Profile refresh",
        objective="Review and update the profile, menu details, offers, and current business information before the next content cycle.",
    ).model_dump())
    return StrategyResponse(
        id=strategy_id, restaurant_id=restaurant["id"], month=month,
        restaurant_name=name, business_type=business_type, goal=goal, summary=summary,
        focus=focus, pillars=pillars, tasks=tasks, occasions=[], context_signature=signature,
    ).model_dump()


@router.get("/{restaurant_id}/strategy", response_model=StrategyResponse)
def get_strategy(restaurant_id: RestaurantId, month: Month, db: Database):
    _, signature = _context_snapshot(db, restaurant_id)
    strategy = _latest_plan(db, restaurant_id, month)
    if strategy is None or strategy.strategy_data.get("context_signature") != signature:
        raise HTTPException(404, "No current strategy for this restaurant and month")
    return _response(strategy)


@router.post("/{restaurant_id}/strategy", response_model=StrategyResponse)
def create_strategy(restaurant_id: RestaurantId, payload: StrategyRequest, request: Request, db: Database):
    with _write_lock(request):
        snapshot, signature = _context_snapshot(db, restaurant_id)
        existing = _latest_plan(db, restaurant_id, payload.month)
        if existing and not payload.regenerate and existing.strategy_data.get("context_signature") == signature:
            return _response(existing)
        qualification = snapshot.get("qualification") or {}
        strategy = Strategy(restaurant_id=restaurant_id, qualification_run_id=qualification.get("id"))
        db.add(strategy)
        db.flush()
        strategy.strategy_data = build_plan(snapshot, signature, payload.month, strategy.id)
        db.commit()
        return _response(strategy)


def _require_task(strategy, task_id):
    for task in strategy.strategy_data.get("tasks", []):
        if task["id"] == task_id:
            return task
    # Old IDs contain a different strategy ID; never update a superseded plan.
    if task_id.startswith("strategy-"):
        raise HTTPException(409, "This task is no longer current. Refresh the monthly strategy.")
    raise HTTPException(404, "Task not found in this restaurant's current monthly strategy")


@router.patch("/{restaurant_id}/strategy/tasks/{task_id}", response_model=StrategyResponse)
def update_task(restaurant_id: RestaurantId, task_id: str, payload: TaskUpdate, request: Request, db: Database):
    with _write_lock(request):
        strategy, _, _ = _require_current_plan(db, restaurant_id, payload.month)
        task = _require_task(strategy, task_id)
        if payload.saved_idea is not None and payload.saved_idea.content_format != task["type"]:
            raise HTTPException(422, "The saved idea must match this task's content format")
        data = deepcopy(strategy.strategy_data)
        for item in data["tasks"]:
            if item["id"] == task["id"]:
                item.update(payload.model_dump(exclude_unset=True, exclude={"month"}))
        strategy.strategy_data = data
        db.commit()
        return _response(strategy)


@router.post("/{restaurant_id}/strategy/tasks", response_model=StrategyResponse, status_code=201)
def add_task(restaurant_id: RestaurantId, payload: TaskCreate, request: Request, db: Database):
    with _write_lock(request):
        strategy, _, _ = _require_current_plan(db, restaurant_id, payload.month)
        data = deepcopy(strategy.strategy_data)
        data["tasks"].append(StrategyTask(
            id=f"strategy-{strategy.id}-custom-{uuid4().hex}", **payload.model_dump(exclude={"month"}),
        ).model_dump())
        data["tasks"].sort(key=lambda task: (task["date"], task["id"]))
        strategy.strategy_data = data
        db.commit()
        return _response(strategy)


@router.post("/{restaurant_id}/content-ideas", response_model=IdeaResponse)
def content_ideas(restaurant_id: RestaurantId, payload: IdeaRequest, request: Request, db: Database):
    strategy, snapshot, signature = _require_current_plan(db, restaurant_id, payload.month)
    task = _require_task(strategy, payload.task_id)
    trusted_input = {
        **snapshot,
        "task": deepcopy(task),
        "strategy": {key: strategy.strategy_data[key] for key in ("month", "business_type", "goal", "summary", "focus", "pillars")},
        "previous_ideas": [idea.model_dump() for idea in payload.previous_ideas],
        "feedback": payload.feedback,
        "business_type": _stated_business_type(snapshot["restaurant"]["context"]) or "unspecified",
    }
    runner = getattr(request.app.state, "content_ideas_runner", generate_content_ideas)
    try:
        result = runner(trusted_input)
        ideas = IdeaResponse.model_validate(result.model_dump() if hasattr(result, "model_dump") else result)
        if any(idea.content_format != task["type"] for idea in ideas.ideas):
            raise ValueError("Generated ideas did not match the selected task format")
    except ContentIdeasUnavailable:
        raise HTTPException(503, "Content ideas are unavailable. Configure OPENAI_API_KEY in the project's .env file.") from None
    except Exception as exc:
        logger.error("Content generation failed (%s)", type(exc).__name__)
        raise HTTPException(502, "Could not generate content ideas. Check the server configuration and retry.") from None
    # A model response can arrive after the user changes restaurants or context.
    db.expire_all()
    _, current_signature = _context_snapshot(db, restaurant_id)
    current_strategy = _latest_plan(db, restaurant_id, payload.month)
    if signature != current_signature or current_strategy.id != strategy.id:
        raise HTTPException(409, "Restaurant context changed. Refresh the strategy before generating ideas again.")
    return ideas
