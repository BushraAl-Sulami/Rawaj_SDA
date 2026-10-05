"""Saudi occasions that fall inside a date range, from saudi_events.json.

The calendar shows them on their days and the content ideas for a day take them into account, so an idea for the
National Day is about the National Day. Dates marked "tentative" in the file depend on the moon sighting.
"""

import json
from datetime import date
from pathlib import Path

EVENTS_FILE = Path(__file__).resolve().parents[3] / "01_data" / "saudi_events.json"


def occasions_between(start: date, end: date) -> list[dict]:
    """The occasions that overlap start..end (inclusive), earliest first."""
    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8")).get("events", [])
    found = []
    for event in events:
        first, last = date.fromisoformat(event["start_date"]), date.fromisoformat(event["end_date"])
        if first <= end and last >= start:
            found.append({
                "name": event["event"], "start_date": event["start_date"], "end_date": event["end_date"],
                "type": event.get("type", ""), "date_status": event.get("date_status", ""),
            })
    return sorted(found, key=lambda item: (item["start_date"], item["name"]))


def occasion_names_on(occasions: list[dict], day: date) -> list[str]:
    """Names of the occasions that include `day`."""
    return [
        item["name"] for item in occasions
        if date.fromisoformat(item["start_date"]) <= day <= date.fromisoformat(item["end_date"])
    ]
