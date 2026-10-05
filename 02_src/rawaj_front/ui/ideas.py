"""Content ideas for one day of the strategy: ask the API for them and show them as cards.

Shared by the Monthly Strategy page (the ideas appear under the calendar, so the day stays in view while you choose)
and the Content page. The ideas, the ones already shown and the chosen one are kept for the session, per restaurant
and day, so nothing is written twice and two restaurants never share ideas.
"""

from html import escape

import streamlit as st

from ui import api


def _init() -> None:
    for name in ("ideas", "saved_ideas", "idea_history"):
        st.session_state.setdefault(name, {})


def idea_key(restaurant: dict, task: dict) -> str:
    return f"{restaurant['id']}:{task['id']}"


def has_ideas(restaurant: dict, task: dict) -> bool:
    _init()
    return idea_key(restaurant, task) in st.session_state.ideas


def fetch_ideas(restaurant: dict, task: dict, more: bool = False, feedback: str = "") -> None:
    """Ask the API for three ideas; with `more`, send the ones already shown so the new ones are different."""
    _init()
    key = idea_key(restaurant, task)
    history = st.session_state.idea_history.setdefault(key, [])
    if more and not feedback.strip():
        feedback = "Generate a completely different set of ideas. Keep the required content format."
    try:
        with st.spinner(f"Writing ideas for {restaurant['name']}..."):
            fresh = api.get_day_ideas(restaurant["id"], task["day"], history[-15:] if more else [], feedback)
    except api.ApiError as exc:
        st.session_state.idea_error = str(exc)
        return
    st.session_state.ideas[key] = fresh
    history.extend(fresh)


def idea_cards(restaurant: dict, task: dict, where: str) -> None:
    """The three idea cards: a name and a short description, and a button to choose one.

    Choosing on the Monthly Strategy page opens the Content page with that idea; on the Content page it replaces the
    chosen idea in place. `where` keeps widget keys unique per page.
    """
    _init()
    key = idea_key(restaurant, task)
    chosen = st.session_state.saved_ideas.get(key)
    for i, idea in enumerate(st.session_state.ideas.get(key) or [], 1):
        with st.container(key=f"card_ideas_{where}_{i}"):
            body, act = st.columns([4, 1], vertical_alignment="center")
            body.markdown(
                f"""
                <div class="idea"><span class="n">{i}</span>
                  <div><b>{escape(idea['name'])}</b><p>{escape(idea['description'])}</p></div>
                </div>
                """,
                unsafe_allow_html=True,
            )
            is_chosen = chosen == idea and where == "content"
            if act.button(
                "Selected" if is_chosen else "Select this idea", key=f"pick_{where}_{i}",
                icon=":material/check:" if is_chosen else None, disabled=is_chosen,
            ):
                st.session_state.saved_ideas[key] = idea
                if where == "strategy":
                    st.session_state.selected_task = task["id"]
                    st.switch_page("views/content.py")
                st.rerun()


def chosen_idea_card(restaurant: dict, task: dict) -> dict | None:
    """The idea the user chose for this day, with the details the cards leave out. Returns that idea, if any."""
    _init()
    idea = st.session_state.saved_ideas.get(idea_key(restaurant, task))
    if not idea:
        return None
    with st.container(key="card_chosen_idea"):
        st.markdown(
            f"""
            <div class="eyebrow" style="color:var(--blue);">Your chosen idea</div>
            <h2 style="font:600 22px var(--head); margin:.4rem 0 .5rem;">{escape(idea['name'])}</h2>
            <p style="margin:0 0 .6rem;">{escape(idea['description'])}</p>
            <div class="idea"><div>
              <p><strong>Hook:</strong> {escape(idea['hook'])}</p>
              <p><strong>Angle:</strong> {escape(idea['angle'])}</p>
              <p><strong>Format and effort:</strong> {escape(idea['content_format'])} · {escape(idea['effort'])}</p>
              <p><strong>Why it fits:</strong> {escape(idea['why_it_fits'])}</p>
            </div></div>
            """,
            unsafe_allow_html=True,
        )
    return idea


def show_ideas(restaurant: dict, task: dict, where: str) -> None:
    """Write the ideas when there are none yet, show them, and offer three different ones if none fits."""
    _init()
    if not has_ideas(restaurant, task) and not st.session_state.get("idea_error"):
        fetch_ideas(restaurant, task)
    if st.session_state.get("idea_error"):
        st.error(st.session_state["idea_error"])
        if st.button("Try again", key=f"retry_{where}", type="primary"):
            st.session_state.pop("idea_error", None)
            st.rerun()
        return
    idea_cards(restaurant, task, where)
    if st.button("None fit? Generate others", key=f"more_{where}", icon=":material/auto_awesome:", type="primary"):
        fetch_ideas(restaurant, task, more=True)
        st.rerun()
