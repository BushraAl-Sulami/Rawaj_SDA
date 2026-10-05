"""Content page: open the workspace for an idea selected on the Strategy page."""

from html import escape

import streamlit as st
from ui import workspace
from ui.components import current_restaurant, footer, topbar
from ui.icons import icon
from ui.ideas import chosen_idea_card
from ui.plan import load_or_stop, period_label
from ui.reviews import completion_review

restaurant = current_restaurant()
plan = load_or_stop(restaurant)
PERIOD = period_label(plan)

business_name = plan.get("restaurant_name") or restaurant["name"]
business_type = str((restaurant.get("context") or {}).get("business_type") or "").strip().title()

tasks = {t["id"]: t for t in plan["tasks"]}
task_ids = list(tasks)

if not task_ids:
    topbar("Content Creation")
    st.info("Your strategy has no content tasks yet.")
    footer()
    st.stop()

if st.session_state.get("selected_task") not in tasks:
    st.session_state.selected_task = task_ids[0]

topbar("Content Creation")
completion_review(restaurant, plan)

# Context row + planning month
ctx, month = st.columns([3, 1.3], vertical_alignment="center")
ctx.markdown(
    f'<div class="ctx-row">{icon("utensils", 15)}<b>{escape(business_name)}</b>'
    f"{'&nbsp;·&nbsp;' + escape(business_type) if business_type else ''}</div>",
    unsafe_allow_html=True,
)
with month:
    label, picker = st.columns([1, 1.4], vertical_alignment="center", gap="small")
    label.markdown(
        f'<div class="ctx-row" style="justify-content:flex-end;">{icon("calendar", 15)} Planning period</div>',
        unsafe_allow_html=True,
    )
    picker.selectbox("Planning period", [PERIOD], label_visibility="collapsed")


# Task picker
title_col, pick_col = st.columns([2, 1.3], vertical_alignment="bottom")
title_col.markdown(
    '<h2 class="section-title">What are we creating?</h2>'
    '<p class="muted" style="margin:.35rem 0 0;">Choose a task from your monthly plan.</p>',
    unsafe_allow_html=True,
)
picked = pick_col.selectbox(
    "Calendar task",
    task_ids,
    index=task_ids.index(st.session_state.selected_task),
    format_func=lambda i: f"{tasks[i]['date']:%b} {tasks[i]['date'].day} · {tasks[i]['format']} · {tasks[i]['title']}",
)
st.session_state.selected_task = picked
task = tasks[picked]
day = task["date"]
done = task["status"] == "Completed"

with st.container(key="card_task"):
    st.markdown(
        f"""
        <div class="task" style="border:0; padding:0;">
          <div class="ico">{icon("calendar", 20)}</div>
          <div>
            <div class="meta"><span class="chip">{escape(task["format"])}</span>
              <span>{day:%a, %b} {day.day}</span><span>{"Completed" if done else "Planned"}</span></div>
            <h4>{escape(task["title"])}</h4>
            <p>{escape(task["text"])}</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

if not task.get("ideas", True):  
    st.info(task.get("ideas_note", ""))
    footer()
    st.stop()

chosen = chosen_idea_card(restaurant, task)
if chosen:
    workspace.show_workspace(restaurant, plan, task, chosen)
else:
    st.info("Select an idea for this task on the Strategy page first.")
    if st.button("Go to Strategy", icon=":material/arrow_back:", key="content_to_strategy"):
        st.switch_page("views/strategy.py")

footer()
