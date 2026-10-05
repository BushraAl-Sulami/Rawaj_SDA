import calendar
from datetime import date
from html import escape

import streamlit as st

from ui.components import current_restaurant, footer, page_head, topbar
from ui.icons import icon
from ui.ideas import show_ideas
from ui.plan import by_date, load_or_stop, occasion_dates, occasions_on, period_label, progress, toggle
from ui.reviews import completion_review


restaurant = current_restaurant()
plan = load_or_stop(restaurant)
tasks = by_date(plan["tasks"])
stats = progress(plan["tasks"])
name = plan["restaurant_name"] or restaurant["name"]
monthly = plan["source"] == "template"  # a monthly content plan (Reels, Posts, Stories) rather than the agent's 30-day plan

chosen = st.session_state.get("selected_date")
calendar_months = sorted({(d.year, d.month) for d in tasks})
if not (isinstance(chosen, date) and (chosen.year, chosen.month) in calendar_months):
    today = date.today()
    st.session_state.selected_date = today if today in tasks else min(tasks)


def select_day(day) -> None:
    st.session_state.selected_date = day
    st.session_state.pop("ideas_for", None)  # the ideas below belong to the day that was chosen before


def want_ideas(task_id) -> None:
    st.session_state.ideas_for = task_id  # shown under the calendar, so the day stays in view while you choose


def close_ideas() -> None:
    st.session_state.pop("ideas_for", None)


def short(text: str, limit: int = 14) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


topbar("Monthly Strategy")
if monthly:
    page_head(
        "Monthly strategy", f"{period_label(plan)} · a content plan built around {name}'s profile.",
        badge="Profile-based plan", badge_icon=icon("utensils", 13),
    )
else:
    page_head(
        "Monthly strategy",
        f"{period_label(plan)} · built by the Strategy Agent from your marketing assessment.",
        badge="Started after you said Interested" if plan.get("interest_event_id") else "30-day plan",
        badge_icon=icon("utensils", 13),
    )

if monthly:
    main, side = st.columns([2.1, 1], gap="medium")
else:  # the agent's plan has two cards (targets, services): stacked full width they leave no empty corner
    main = side = st.container()

with main:
    with st.container(key="card_north"):
        targets = "".join(
            f"""
            <div class="pillar">
              <span class="n">{i:02d}</span>
              <div class="t"><b>{escape(text)}</b></div>
            </div>"""
            for i, text in enumerate(plan["targets"], 1)
        )
        if monthly:
            st.markdown(
                f"""
                <div class="north">
                  <div class="badge">{icon('target', 20)}</div>
                  <div>
                    <div class="eyebrow">This month's north star</div>
                    <h3>{escape(plan['targets'][0] if plan['targets'] else 'Grow with ' + name)}</h3>
                    <p>{escape(plan['summary'])}</p>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            st.markdown(
                f"""
                <div class="north">
                  <div class="badge">{icon('target', 20)}</div>
                  <div>
                    <div class="eyebrow">Your 30-day targets</div>
                    <h3>What {escape(name)} is working toward</h3>
                  </div>
                </div>
                <div class="pillars-grid">{targets}</div>
                {'' if targets else '<p class="muted">No targets were set for this plan.</p>'}
                """,
                unsafe_allow_html=True,
            )

    if monthly:  # the agent's plan does not repeat its priority gaps here: they are on the Home page
        with st.container(key="card_pillars"):
            pillars = "".join(
                f"""
                <div class="pillar">
                  <span class="n">{i:02d}</span>
                  <div class="t"><b>{escape(p['title'])}</b><span>{escape(p['description'])}</span></div>
                  {f'<span class="signal">{escape(p["metric"])}</span>' if p['metric'] else ''}
                </div>"""
                for i, p in enumerate(plan["pillars"], 1)
            )
            st.markdown(
                f"""
                <div class="card-head"><h3 class="card-title">Strategy pillars</h3>
                <span class="muted" style="font-size:10.5px;">{len(plan['pillars'])} priorities</span></div>
                {pillars or '<p class="muted">No pillars were set for this plan.</p>'}
                """,
                unsafe_allow_html=True,
            )

with side:
    points = "".join(
        f'<div class="pt">{icon("check", 13)}<span><b>{escape(s["service"])}</b>'
        f'{"<br>" + escape(s["why_this_service_fits"]) if s["why_this_service_fits"] else ""}</span></div>'
        for s in plan["services"]
    )
    if monthly:
        focus_points = "".join(
            f'<div class="pt">{icon("check", 13)}{escape(text)}</div>'
            for text in ([f"Goal: {plan['targets'][0]}"] if plan["targets"] else []) + [f"Plan month: {period_label(plan)}"]
        )
        st.markdown(
            f"""
            <div class="focus">
              <div class="top">{icon('lightbulb', 15)} Your business, in focus</div>
              <h3>Let people experience {escape(name)} before they visit.</h3>
              <p>{escape(plan['focus'])}</p>
              {focus_points}
            </div>
            """,
            unsafe_allow_html=True,
        )
    else:
        st.markdown(
            f"""
            <div class="focus wide">
              <div class="top">{icon('lightbulb', 15)} Recommended for you</div>
              <h3>How we can help {escape(name)} grow.</h3>
              <p>Services matched to the gaps found in your assessment.</p>
              <div class="pts">{points or '<p>No services were recommended.</p>'}</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

if plan["trends"]:
    with st.container(key="card_trends"):
        rows = "".join(
            f'<div class="note good"><span class="dot">{icon("sparkles", 13, 2.2)}</span>'
            f'<span>{escape(t["insight"])}'
            + (f' <a href="{escape(t["source_url"], quote=True)}" target="_blank" rel="noopener noreferrer">Source</a>' if t["source_url"] else "")
            + "</span></div>"
            for t in plan["trends"]
        )
        st.markdown(
            f"""
            <div class="card-head"><h3 class="card-title">Market trends behind the plan · {len(plan['trends'])}</h3></div>
            <div class="notes">{rows}</div>
            """,
            unsafe_allow_html=True,
        )


def progress_card() -> None:
    """How many days are planned and how many are completed."""
    groups = "".join(
        f'<span class="chip{" sev-low" if done == total else ""}">{escape(label)} · {done}/{total}</span>'
        for label, done, total in stats["groups"]
    )
    st.markdown(
        f"""
        <div class="card-head"><h3 class="card-title">Task progress · {period_label(plan)}</h3>
        <span class="muted">{stats['done']} of {stats['total']} completed{f" · {stats['skipped']} break days not counted" if stats['skipped'] else ""}</span></div>
        <div class="tiers three">
          <div class="tier"><small>{"Items this month" if monthly else "Days in the plan"}</small><b>{stats['total']}</b></div>
          <div class="tier low{' zero' if not stats['done'] else ''}"><small>Completed</small><b>{stats['done']}</b></div>
          <div class="tier{' zero' if not stats['remaining'] else ''}"><small>Remaining</small><b>{stats['remaining']}</b></div>
        </div>
        <div class="bar-row"><div class="bar"><i style="width:{stats['percent']}%"></i></div><b>{stats['percent']}%</b></div>
        <div class="formats">{groups}</div>
        """,
        unsafe_allow_html=True,
    )


with st.container(key="card_progress"):
    progress_card()

completion_review(restaurant, plan)

page_head("Content calendar", "Choose a day to see what to publish and when, and track your progress.")

if plan["occasions"]:
    chips = "".join(
        f'<span class="occ-chip">&#9733; {escape(item["name"])} <b>{escape(occasion_dates(item))}</b></span>'
        for item in plan["occasions"]
    )
    st.markdown(f'<div class="occ-row"><span class="occ-label">Occasions in this calendar</span>{chips}</div>', unsafe_allow_html=True)

cal_col, detail_col = st.columns([1.75, 1], gap="medium")

with cal_col:
    with st.container(key="card_calendar"):
        st.markdown(
            '<div class="dow">' + "".join(f"<span>{d}</span>" for d in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")) + "</div>",
            unsafe_allow_html=True,
        )
        # The 30 days usually straddle two calendar months: draw each month that the plan touches.
        for year, month in calendar_months:
            st.markdown(f'<h3 class="cal-title">{date(year, month, 1):%B %Y}</h3>', unsafe_allow_html=True)
            for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
                cols = st.columns(7, gap="xsmall")
                for col, day in zip(cols, week):
                    if day.month != month:
                        col.markdown('<div class="blank"></div>', unsafe_allow_html=True)
                        continue
                    day_tasks = tasks.get(day, [])
                    finished = bool(day_tasks) and all(t["status"] == "Completed" for t in day_tasks)
                    one = (day_tasks[0]["format"] if monthly else short(day_tasks[0]["title"])) if day_tasks else ""
                    tag = one if len(day_tasks) == 1 else f"{len(day_tasks)} tasks"
                    occasions = occasions_on(plan, day)
                    parts = [f"{day.day}{' ✓' if finished else ''}"]
                    if day_tasks:
                        parts.append(tag)
                    if occasions:
                        parts.append("★ " + short(occasions[0]["name"].removeprefix("Saudi "), 14))  # "Saudi National Day" -> "National Day"
                    label = "\n\n".join(parts)
                    picked = day == st.session_state.selected_date
                    col.button(
                        label,
                        key=f"{'dayon' if picked else 'day'}_{'occ_' if occasions else ''}{day.isoformat()}",
                        on_click=select_day,
                        args=(day,),
                        use_container_width=True,
                    )

with detail_col:
    with st.container(key="card_selected"):
        day = st.session_state.selected_date
        st.markdown(
            f'<div class="selected-head"><span class="eyebrow">Selected date</span><b>{day:%a, %b} {day.day}</b></div>',
            unsafe_allow_html=True,
        )
        if st.session_state.get("plan_error"):
            st.error(st.session_state.pop("plan_error"))
        day_tasks = tasks.get(day, [])
        for item in occasions_on(plan, day):
            note = "The date depends on the moon sighting." if item.get("date_status") == "tentative" else "Confirmed date."
            st.markdown(
                f'<div class="occasion"><b>&#9733; {escape(item["name"])}</b>'
                f'<span>{escape(occasion_dates(item))} · {escape(note)}</span></div>',
                unsafe_allow_html=True,
            )
        for task in day_tasks:
            finished = task["status"] == "Completed"
            st.markdown(
                f"""
                <div class="task">
                  <div class="ico">{icon('check' if finished else 'calendar', 18)}</div>
                  <div>
                    <div class="meta"><span class="fmt">{escape(task['format'])}</span>
                      <span>{'Completed' if finished else 'Planned'}</span></div>
                    <h4>{escape(task['title'])}</h4>
                    <p>{escape(task['text'])}</p>
                  </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            a, b = st.columns([1.2, 1])
            if task.get("ideas", True):  # a plan without the field (an older API) still offers ideas
                a.button(
                    "Get content ideas", icon=":material/auto_awesome:", type="primary", key=f"ideas_{task['id']}",
                    on_click=want_ideas, args=(task["id"],),
                )
            else:  # a break or a profile update: nothing to publish, so no ideas
                a.markdown(f'<p class="muted" style="font-size:11.5px;">{escape(task.get("ideas_note", ""))}</p>', unsafe_allow_html=True)
            b.button(
                "Mark incomplete" if finished else "Mark complete", icon=":material/check:",
                type="tertiary", key=f"mark_{task['id']}", on_click=toggle,
                args=(restaurant["id"], task),
            )
        if not day_tasks:
            st.markdown(
                f'<p class="muted">Nothing is planned for this day at {escape(name)}.</p>',
                unsafe_allow_html=True,
            )

# Content ideas for the chosen day, under the calendar so the plan stays in view while you pick one.
idea_task = next((t for t in plan["tasks"] if t["id"] == st.session_state.get("ideas_for")), None)
if idea_task:
    with st.container(key="card_ideas_panel"):
        head, close = st.columns([5, 1], vertical_alignment="center")
        head.markdown(
            f"""
            <div class="eyebrow" style="color:var(--blue);">{icon('sparkles', 14)} Content ideas</div>
            <h2 style="font:600 22px var(--head); margin:.4rem 0 .2rem;">{idea_task['date']:%a, %b} {idea_task['date'].day} · {escape(idea_task['title'])}</h2>
            <p class="muted" style="margin:0;">Three directions shaped by {escape(name)}'s profile and this day's plan. Choose one, or ask for others.</p>
            """,
            unsafe_allow_html=True,
        )
        close.button("Close", key="close_ideas", type="tertiary", icon=":material/close:", on_click=close_ideas)
        show_ideas(restaurant, idea_task, "strategy")

footer()
