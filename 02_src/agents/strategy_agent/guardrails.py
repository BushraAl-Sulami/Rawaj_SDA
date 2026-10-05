"""Deterministic checks on a generated strategy, used before saving and by the evaluations.

``check_strategy`` returns two lists:
- errors: the strategy breaks the contract and must not be saved (schema, 30 days, allowed services).
- warnings: a grounding or timing concern a reviewer should look at (unknown gap, unmatched event).
"""

import json
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from pydantic import ValidationError

from .prompt import AGENCY_SERVICES
from .schemas import StrategyOutput

EVENTS_FILE = Path(__file__).resolve().parents[3] / "01_data" / "saudi_events.json"
PLAN_DAYS = 30
# Preparation and follow-up posts around an occasion are expected; this is how far from it we allow.
EVENT_WINDOW_DAYS = 7


def allowed_services() -> set[str]:
    """Top-level service names from AGENCY_SERVICES ("1. Social Media Strategy" -> "Social Media Strategy")."""
    return {m.group(1).strip() for m in re.finditer(r"^\d+\.\s+(.+?)\s*$", AGENCY_SERVICES, re.M)}


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", str(text).casefold()).strip()


def _qualification_gaps(qualification: dict | None) -> dict[str, str]:
    """{normalised gap name: severity} from the Qualification Agent output."""
    gaps = (qualification or {}).get("marketing_gaps") or []
    return {_norm(g["gap"]): g.get("severity", "") for g in gaps if isinstance(g, dict) and g.get("gap")}


def _events_in_period(start: date) -> list[dict]:
    events = json.loads(EVENTS_FILE.read_text(encoding="utf-8"))["events"]
    end = start + timedelta(days=PLAN_DAYS - 1)
    found = []
    for event in events:
        first = datetime.strptime(event["start_date"], "%Y-%m-%d").date()
        last = datetime.strptime(event["end_date"], "%Y-%m-%d").date()
        if first <= end and last >= start:
            found.append({**event, "first_day": (first - start).days + 1, "last_day": (last - start).days + 1})
    return found


def check_strategy(data: dict, qualification: dict | None = None, start_date: str | None = None) -> dict[str, list[str]]:
    errors: list[str] = []
    warnings: list[str] = []

    try:
        strategy = StrategyOutput.model_validate(data)
    except ValidationError as exc:
        for item in exc.errors():
            location = ".".join(str(part) for part in item["loc"])
            errors.append(f"schema: {location}: {item['msg']}")
        return {"errors": errors, "warnings": warnings}

    days = [d.day for d in strategy.thirty_day_plan]
    if sorted(days) != list(range(1, PLAN_DAYS + 1)):
        missing = sorted(set(range(1, PLAN_DAYS + 1)) - set(days))
        repeated = sorted({d for d in days if days.count(d) > 1})
        extra = sorted(d for d in set(days) if d < 1 or d > PLAN_DAYS)
        errors.append(
            f"plan must contain days 1-{PLAN_DAYS} exactly once "
            f"(missing {missing or 'none'}, repeated {repeated or 'none'}, out of range {extra or 'none'})"
        )

    allowed = allowed_services()
    for item in strategy.recommended_services:
        if item.service not in allowed:
            errors.append(f"service '{item.service}' is not one of the agency services {sorted(allowed)}")

    known = _qualification_gaps(qualification)
    if known:
        for item in strategy.primary_marketing_gaps:
            key = _norm(item.gap)
            match = key if key in known else next((k for k in known if key in k or k in key), None)
            if match is None:
                warnings.append(f"gap '{item.gap}' is not in the Qualification Agent output")
            elif known[match] and known[match] != item.severity:
                warnings.append(f"gap '{item.gap}' is {known[match]} in qualification but {item.severity} here")
        evidence = json.dumps(qualification, ensure_ascii=False)
        for item in strategy.primary_marketing_gaps:
            if item.highlight and _norm(item.highlight) and item.highlight.rstrip("%") not in evidence:
                warnings.append(f"highlight '{item.highlight}' for '{item.gap}' does not appear in the qualification evidence")

    if start_date:
        try:
            start = date.fromisoformat(start_date)
        except ValueError:
            errors.append(f"strategy_start_date '{start_date}' is not an ISO date")
        else:
            by_day = {d.day: f"{d.focus} {d.action}".casefold() for d in strategy.thirty_day_plan}
            for event in _events_in_period(start):
                mentions = [n for n, text in by_day.items() if event["event"].casefold() in text]
                if not mentions:
                    continue
                low, high = event["first_day"] - EVENT_WINDOW_DAYS, event["last_day"] + EVENT_WINDOW_DAYS
                off = [n for n in mentions if not low <= n <= high]
                if off:
                    warnings.append(
                        f"'{event['event']}' falls on day {event['first_day']} but is mentioned on day(s) {off}"
                    )
                if event.get("date_status") == "tentative":
                    for n in mentions:
                        if "confirmed" in by_day[n]:
                            warnings.append(f"tentative event '{event['event']}' is described as confirmed on day {n}")

    return {"errors": errors, "warnings": warnings}
