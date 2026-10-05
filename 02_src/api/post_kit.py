"""Validate HTTP input, load strategy context, and call injectable Post Kit runners."""

import logging
from typing import Annotated, Literal

from agents.strategy_agent.content_ideas import Idea
from agents.strategy_agent.post_kit import (
    MORE_CUTOFF,
    PostFacts,
    PostKitInvalid,
    PostKitUnavailable,
    Rewrite,
    Tone,
    best_posting_time,
    crop_rules,
    detail_words,
    gap_needs_prices,
    make_facts_plan,
    make_post_kit,
    make_rewrite,
)
from api import agent_strategy
from api.schemas import InputModel
from database.models import Restaurant
from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import Field
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/restaurants", tags=["Post Kit"])
RestaurantId = Annotated[int, Path(gt=0)]
Day = Annotated[int, Path(ge=1)]
ContentFormat = Literal["Post", "Reel", "Story"]
CaptionChange = Literal["shorter", "hook", "warm", "playful", "premium", "direct", "claims"]


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


class FactsPlanRequest(InputModel):
    idea: Idea
    # The gap shown to the owner as "Why this post"; the form and the kit are built to fix it.
    gap: Annotated[str, Field(max_length=600)] = ""


class CreatePostKitRequest(FactsPlanRequest):
    facts: PostFacts
    tone: Tone = "warm"


class RewriteRequest(InputModel):
    caption: Annotated[str, Field(min_length=1, max_length=1200)]
    change: CaptionChange
    facts: PostFacts
    content_format: ContentFormat
    claims: list[Annotated[str, Field(min_length=1, max_length=400)]] = Field(default_factory=list, max_length=6)


def _restaurant_context(restaurant: Restaurant) -> dict:
    return {"name": restaurant.name, "location": restaurant.location or "", "handle": restaurant.instagram_username or ""}


def _find_day(db: Session, restaurant_id: int, day: int) -> tuple[dict, dict]:
    agent_strategy._require_restaurant(db, restaurant_id)
    strategy = agent_strategy._latest(db, restaurant_id)
    if strategy is None:
        raise HTTPException(404, "No strategy has been saved for this restaurant yet")
    overview = agent_strategy._response(db, strategy)
    item = next((entry for entry in overview["days"] if entry["day"] == day), None)
    if item is None:
        raise HTTPException(404, "Day not found in this restaurant's strategy")
    return overview, item


def _task_context(item: dict, occasions: list[dict], idea: Idea) -> dict:
    content_format = item.get("format") or idea.content_format
    if content_format not in {"Post", "Reel", "Story"}:
        raise HTTPException(422, "The selected idea must have a Reel, Story, or Post format.")
    if item.get("format") and idea.content_format != content_format:
        raise HTTPException(422, "The selected idea's format does not match the day's format.")
    return {
        "day": item["day"],
        "date": item["date"],
        "focus": item["focus"],
        "action": item["action"],
        "content_format": content_format,
        "occasions": [o["name"] for o in occasions if o["start_date"] <= item["date"] <= o["end_date"]],
    }


def _idea_context(
    db: Session, restaurant_id: int, day: int, idea: Idea, gap: str = ""
) -> tuple[dict, dict, Restaurant]:
    overview, item = _find_day(db, restaurant_id, day)
    restaurant = db.get(Restaurant, restaurant_id)
    task = _task_context(item, overview.get("occasions") or [], idea)
    if gap:
        task["gap"] = gap
    context = {
        "restaurant": _restaurant_context(restaurant),
        "task": task,
        "idea": idea.model_dump(),
    }
    return context, overview, restaurant


def _run(request: Request, runner_name: str, default, context: dict):
    """Keep error handling consistent without exposing credentials or raw provider errors."""
    runner = getattr(request.app.state, runner_name, default)
    try:
        return runner(context)
    except PostKitUnavailable:
        raise HTTPException(503, "Post Kit is unavailable. Configure OPENAI_API_KEY on the server.") from None
    except PostKitInvalid as exc:
        raise HTTPException(502, f"Generated content did not pass validation: {exc}") from None
    except Exception:
        logger.exception("Post Kit runner failed: %s", runner_name)
        raise HTTPException(502, "Could not generate content. Check the server configuration and retry.") from None


@router.post("/{restaurant_id}/agent-strategy/days/{day}/post-kit/facts-plan")
def post_facts_plan(restaurant_id: RestaurantId, day: Day, payload: FactsPlanRequest, request: Request, db: Database):
    context, _, _ = _idea_context(db, restaurant_id, day, payload.idea, payload.gap)
    return _run(request, "facts_plan_runner", make_facts_plan, context).model_dump()


@router.post("/{restaurant_id}/agent-strategy/days/{day}/post-kit")
def post_post_kit(restaurant_id: RestaurantId, day: Day, payload: CreatePostKitRequest, request: Request, db: Database):
    context, overview, restaurant = _idea_context(db, restaurant_id, day, payload.idea, payload.gap)
    context.update(
        {
            "facts": payload.facts.model_dump(),
            "tone": payload.tone,
            "strategy": {
                key: overview.get(key) for key in ("summary", "focus", "targets", "pillars", "gaps", "services")
            },
            "voice": {"recent_captions": []},  # Caption history is not yet available through this API.
            "allowed": {"handles": [restaurant.instagram_username] if restaurant.instagram_username else []},
        }
    )
    result = _run(request, "post_kit_runner", make_post_kit, context)
    return {
        **result.model_dump(),
        "more_cutoff": MORE_CUTOFF,
        "crop_rules": crop_rules(result.kit.shoot.format),
        "best_time": best_posting_time([]),  # No source-post metrics wired here yet; reports insufficient data.
        "detail_words": detail_words(context),  # for the live "uses your details" check while the owner edits
        "price_gap": gap_needs_prices(context),
    }


@router.post("/{restaurant_id}/agent-strategy/days/{day}/post-kit/rewrite")
def post_rewrite(
    restaurant_id: RestaurantId, day: Day, payload: RewriteRequest, request: Request, db: Database
) -> Rewrite:
    _, item = _find_day(db, restaurant_id, day)
    if item.get("format") and item["format"] != payload.content_format:
        raise HTTPException(422, "The caption format does not match the day's format.")
    restaurant = db.get(Restaurant, restaurant_id)
    context = {
        "restaurant": _restaurant_context(restaurant),
        "task": {"content_format": payload.content_format},
        "facts": payload.facts.model_dump(),
        "caption": payload.caption,
        "change": payload.change,
        "claims": payload.claims,
    }
    return _run(request, "caption_rewrite_runner", make_rewrite, context)
