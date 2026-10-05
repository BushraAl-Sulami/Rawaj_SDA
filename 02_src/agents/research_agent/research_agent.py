"""
Research Agent

Researches ONE restaurant's Instagram account in two parts, run in order:

1. SCRAPE  (research_scraper)  - raw Instagram profile + recent content.
2. ANALYZE (research_analyzer) - describe that raw data. It never labels
   anything a marketing gap or qualifies the restaurant; that belongs to the
   Qualification Agent.
"""

import json
import re
from datetime import datetime
from pathlib import Path

from .research_analyzer import analyze_instagram_data
from .research_guardrails import clamp_scrape_limits
from .research_schemas import ResearchProfile, RestaurantInfo
from .research_scraper import scrape_instagram_data

DEFAULT_CONTENT_LIMIT = 20
DEFAULT_LOOKBACK_DAYS = 90


def run_research_agent(
    restaurant: RestaurantInfo,
    content_limit: int = DEFAULT_CONTENT_LIMIT,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
) -> ResearchProfile:
    # Checked before any paid Apify call: at most 20 posts from the last 90 days.
    content_limit, lookback_days = clamp_scrape_limits(content_limit, lookback_days)

    # Part 1 — scrape the profile and recent content.
    scraped = scrape_instagram_data(
        username=restaurant.instagram_username,
        content_limit=content_limit,
        lookback_days=lookback_days,
    )

    # Part 2 — analyze that raw data.
    return analyze_instagram_data(restaurant=restaurant, scraped=scraped)


def _safe_filename(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", value.strip())
    return cleaned.strip("_") or "instagram_account"


def save_research_profile(report: ResearchProfile) -> Path:
    """Save every run automatically as a timestamped JSON file."""

    output_dir = Path("results")
    output_dir.mkdir(parents=True, exist_ok=True)

    username = _safe_filename(report.restaurant.instagram_username)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"{username}_research_{timestamp}.json"

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(
            report.model_dump(mode="json"),
            file,
            indent=2,
            ensure_ascii=False,
        )

    return output_path
