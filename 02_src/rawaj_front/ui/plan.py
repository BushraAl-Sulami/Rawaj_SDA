"""The restaurant's strategy, shared by the Strategy and Content pages (data comes from the API).

The API returns the latest strategy saved for the restaurant in one shape, whichever way it was made:
- source "agent": the Strategy Agent's 30-day plan (days with a focus and an action, targets, gaps, services)
- source "template": a monthly content plan (a goal, pillars and dated tasks that are a Reel, a Post or a Story)
"""

import calendar
from datetime import date
from html import escape

import streamlit as st

from ui import api
from ui.icons import icon


def load(restaurant: dict) -> dict | None:
    """The restaurant's current strategy with its days as tasks; None if none exists yet."""
    plan = api.get_agent_strategy(restaurant["id"])
    if plan is None:
        return None
    plan["start"] = date.fromisoformat(plan["start_date"])
    plan["end"] = date.fromisoformat(plan["end_date"])
    plan["occasions"] = [
        {**item, "first": date.fromisoformat(item["start_date"]), "last": date.fromisoformat(item["end_date"])}
        for item in plan.get("occasions") or []
    ]
    plan["tasks"] = [
        {
            "id": f"day-{d['day']}", "day": d["day"], "date": date.fromisoformat(d["date"]),
            "format": d.get("format") or f"Day {d['day']}", "typed": bool(d.get("format")),
            "title": d["focus"] or f"Day {d['day']}", "text": d["action"], "status": d["status"],
            "ideas": d.get("ideas", True), "ideas_note": d.get("ideas_note", ""),
            "counts": d.get("counts", (d["focus"] or "").strip().lower() != "break"),
            "post_url": d.get("post_url", ""), "outcome": d.get("outcome", ""),  # what the owner added after posting
        }
        for d in plan["days"]
    ]
    return plan


def occasions_on(plan: dict, day: date) -> list[dict]:
    """The Saudi occasions that include this day."""
    return [item for item in plan.get("occasions", []) if item["first"] <= day <= item["last"]]


def occasion_dates(item: dict) -> str:
    first, last = item["first"], item["last"]
    if first == last:
        return f"{first:%a, %d %b}"
    return f"{first:%d %b} – {last:%d %b}"


def period_label(plan: dict) -> str:
    """'September 2026' for a plan that covers a whole calendar month, else '20 Sep – 19 Oct 2026'."""
    start, end = plan["start"], plan["end"]
    if start.day == 1 and (end.year, end.month) == (start.year, start.month) and end.day == calendar.monthrange(end.year, end.month)[1]:
        return f"{start:%B %Y}"
    return f"{start:%d %b} – {end:%d %b %Y}"


def load_or_stop(restaurant: dict | None) -> dict:
    """Load the plan, or explain why it is unavailable and stop the page."""
    if restaurant is None:
        st.warning(f"No restaurant found. Check that the Rawaj API is running at {api.API_URL}.")
        st.stop()
    try:
        plan = load(restaurant)
    except api.ApiError as exc:
        st.warning(f"Could not load the strategy from the Rawaj API. {exc}")
        st.stop()
    if plan is None or not plan["tasks"]:
        st.markdown(
            f"""
            <div class="empty">
              <div class="ring">{icon('calendar', 26)}</div>
              <h3>No strategy yet for {escape(restaurant['name'])}.</h3>
              <p>A strategy appears here as soon as one is saved.</p>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.stop()
    return plan


def by_date(tasks: list[dict]) -> dict[date, list[dict]]:
    days: dict[date, list[dict]] = {}
    for task in tasks:
        days.setdefault(task["date"], []).append(task)
    return days


def progress(tasks: list[dict]) -> dict:
    """Counts for the plan: total, completed, remaining, percent, and completion per group.

    The groups are the content types (Reel, Post, Story) of a monthly plan, or the weeks of a 30-day plan.
    Break days are left out (`skipped` says how many): only days with something to do are counted, that is a post,
    a story, a reel or a profile update.
    """
    skipped = sum(not t.get("counts", True) for t in tasks)
    tasks = [t for t in tasks if t.get("counts", True)]
    done = sum(t["status"] == "Completed" for t in tasks)
    typed = any(t.get("typed") for t in tasks)
    groups: dict[str, list[int]] = {}
    for task in tasks:
        label = task["format"] if typed else f"Week {(task['day'] - 1) // 7 + 1}"
        counts = groups.setdefault(label, [0, 0])
        counts[1] += 1
        counts[0] += task["status"] == "Completed"
    total = len(tasks)
    return {
        "total": total, "done": done, "remaining": total - done, "skipped": skipped,
        "percent": round(100 * done / total) if total else 0,
        "groups": [(label, d, t) for label, (d, t) in (groups.items() if typed else sorted(groups.items(), key=lambda kv: int(kv[0].split()[1])))],
    }


def toggle(restaurant_id: int, task: dict) -> None:
    """Button callback: flip a day between Planned and Completed through the API."""
    status = "Planned" if task["status"] == "Completed" else "Completed"
    try:
        api.set_day_status(restaurant_id, task["day"], status)
    except api.ApiError as exc:
        st.session_state.plan_error = str(exc)
