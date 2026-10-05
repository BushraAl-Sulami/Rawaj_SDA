import json
import os
from datetime import date, datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from langchain_core.tools import tool
from tavily import TavilyClient


load_dotenv()



# =========================================================
# WEB SEARCH TOOL
# =========================================================

@tool
def web_search(query: str) -> str:
    """
    Search the web for current marketing information, trends,
    platform updates, and Saudi-specific market insights.

    Use this tool only when current external information would
    materially improve the restaurant marketing strategy.

    Args:
        query: The specific web search query.

    Returns:
        Search results containing titles, content, and source URLs.
    """

    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return "External search is unavailable. Use supplied research and calendar evidence only; do not invent current trends or sources."
    response = TavilyClient(api_key=api_key).search(
        query=query,
        search_depth="advanced",
        max_results=5,
    )

    results = []

    for result in response.get("results", []):
        results.append(
            f"""
Title: {result.get('title')}
Content: {result.get('content')}
URL: {result.get('url')}
"""
        )

    if not results:
        return "No relevant web search results were found."

    return "\n---\n".join(results)


# =========================================================
# SAUDI EVENTS TOOL
# =========================================================
@tool
def get_upcoming_events(
    start_date: str,
    days: int = 30
) -> str:
    """
    Return Saudi events that overlap with the specified strategy period.
    """

    strategy_start = datetime.strptime(
        start_date,
        "%Y-%m-%d"
    ).date()

    period_end = strategy_start + timedelta(days=days - 1)

    events_file = Path(__file__).resolve().parents[3] / "01_data" / "saudi_events.json"

    with open(events_file, "r", encoding="utf-8") as file:
         events_data = json.load(file)

    events = events_data["events"]

    upcoming_events = []

    for event in events:

        event_start = datetime.strptime(
            event["start_date"],
            "%Y-%m-%d"
        ).date()

        event_end = datetime.strptime(
            event["end_date"],
            "%Y-%m-%d"
        ).date()

        # Event overlaps with the 30-day strategy period
        if event_start <= period_end and event_end >= strategy_start:

            plan_start_day = max(
                1,
                (event_start - strategy_start).days + 1
            )

            plan_end_day = min(
                days,
                (event_end - strategy_start).days + 1
            )

            upcoming_events.append(
                {
                    **event,
                    "plan_start_day": plan_start_day,
                    "plan_end_day": plan_end_day,
                }
            )

    return json.dumps(
        upcoming_events,
        indent=2,
        ensure_ascii=False
    )

# =========================================================
# AVAILABLE TOOLS
# =========================================================

TOOLS = [
    web_search,
    get_upcoming_events,
]
