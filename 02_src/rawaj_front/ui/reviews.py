"""Task completion and plan reviews kept in the current frontend session only."""

from datetime import datetime
from html import escape

import streamlit as st

from ui.icons import icon
from ui.plan import period_label, progress


def review_key(restaurant: dict, plan: dict) -> str:
    return f"{restaurant['id']}:{plan.get('source', 'agent')}:{plan['id']}:{plan['start']}:{plan['end']}"


def all_tasks_complete(plan: dict) -> bool:
    stats = progress(plan["tasks"])
    return stats["total"] > 0 and stats["done"] == stats["total"]


def saved_review(restaurant: dict, plan: dict) -> dict | None:
    return st.session_state.get("plan_reviews", {}).get(review_key(restaurant, plan))


def save_review(restaurant: dict, plan: dict, rating: int | None, text: str) -> None:
    if not all_tasks_complete(plan):
        raise ValueError("Complete all your tasks before writing a review.")
    if rating not in (1, 2, 3, 4, 5):
        raise ValueError("Choose a rating before saving your review.")
    if not text.strip():
        raise ValueError("Write a few words about your experience.")
    if len(text.strip()) > 2000:
        raise ValueError("Keep your review within 2,000 characters.")
    reviews = dict(st.session_state.get("plan_reviews", {}))
    reviews[review_key(restaurant, plan)] = {
        "rating": rating, "text": text.strip(), "period": period_label(plan),
        "saved_at": datetime.now().strftime("%d %b %Y"),
    }
    st.session_state.plan_reviews = reviews


def dismiss_review() -> None:
    st.session_state.pop("active_review_key", None)


@st.dialog("You completed all your tasks", width="large", on_dismiss=dismiss_review)
def review_dialog(restaurant: dict, plan: dict) -> None:
    key = review_key(restaurant, plan)
    review = saved_review(restaurant, plan) or {}
    count = progress(plan["tasks"])["total"]
    st.markdown(
        f'<div class="review-celebration"><div class="review-seal">{icon("check", 30)}</div>'
        '<div><div class="eyebrow">Every task. A step forward.</div><h2>You did it.</h2>'
        f'<p>{count} of {count} tasks complete · {escape(period_label(plan))}</p></div></div>'
        '<p class="review-intro">What worked well, and what could be better? Share your experience with this plan.</p>',
        unsafe_allow_html=True,
    )
    with st.form(f"plan_review_form_{key}"):
        rating = st.radio(
            "How was your experience?", [1, 2, 3, 4, 5],
            index=review["rating"] - 1 if review else None,
            format_func=lambda value: f"{value} ★", horizontal=True, key=f"review_rating_{key}",
        )
        text = st.text_area(
            "Your review", value=review.get("text", ""), height=180, max_chars=2000,
            placeholder="Tell us what helped you, what you achieved, or what you would change…",
            key=f"review_text_{key}",
        )
        submitted = st.form_submit_button("Update review" if review else "Save review", type="primary")
    if submitted:
        try:
            save_review(restaurant, plan, rating, text)
        except ValueError as error:
            st.error(str(error))
        else:
            st.session_state.review_saved_notice = key
            dismiss_review()
            st.rerun()
    if st.button("Maybe later", key=f"review_later_{key}", type="tertiary"):
        dismiss_review()
        st.rerun()


def completion_review(restaurant: dict, plan: dict, *, auto_prompt: bool = True) -> None:
    """Prompt once per restaurant/plan after all counted tasks are completed."""
    key = review_key(restaurant, plan)
    if not all_tasks_complete(plan):
        if st.session_state.get("active_review_key") == key:
            dismiss_review()
        return
    review = saved_review(restaurant, plan)
    prompted = st.session_state.setdefault("review_prompted_plans", set())
    with st.container(key="card_task_completion"):
        st.markdown(
            f'<div class="review-finish">{icon("check", 24)}<div><h3>All tasks complete!</h3>'
            f'<p>{"Your review is saved and visible on Home." if review else "Take a moment to look back and share your experience."}</p></div></div>',
            unsafe_allow_html=True,
        )
        left, right = st.columns([1, 1])
        open_review = left.button(
            "Edit review" if review else "Write a review", type="primary", key=f"open_review_{key}",
            icon=":material/rate_review:",
        )
        right.page_link("views/home.py", label="View Home", icon=":material/home:")
    if st.session_state.get("review_saved_notice") == key:
        st.session_state.pop("review_saved_notice")
        st.toast("Your review is now on Home.", icon=":material/check_circle:")
    if open_review or (auto_prompt and not review and key not in prompted):
        prompted.add(key)
        st.session_state.active_review_key = key
    if st.session_state.get("active_review_key") == key:
        review_dialog(restaurant, plan)


def dashboard_progress(restaurant: dict, plan: dict) -> None:
    """Show task progress and the current plan's review on the owner's Home page."""
    stats = progress(plan["tasks"])
    if not stats["total"]:
        return
    with st.container(key="card_task_dashboard"):
        st.markdown(
            '<div class="card-head"><h3 class="card-title">Your task progress</h3>'
            f'<span class="muted">{escape(period_label(plan))}</span></div>', unsafe_allow_html=True,
        )
        st.progress(stats["done"] / stats["total"], text=f"{stats['done']} of {stats['total']} tasks complete")
        review = saved_review(restaurant, plan)
        if review:
            st.markdown(
                '<div class="review-readout"><div class="review-meta"><b>Your review</b>'
                f'<span aria-label="{review["rating"]} out of 5 stars">{"★" * review["rating"]}{"☆" * (5 - review["rating"])}</span></div>'
                f'<p class="review-copy">{escape(review["text"])}</p>'
                f'<small>Saved {escape(review["saved_at"])}</small></div>', unsafe_allow_html=True,
            )
        elif all_tasks_complete(plan):
            st.markdown("**All tasks complete!** Open your calendar to share your review.")
        st.page_link("views/strategy.py", label="Open calendar", icon=":material/calendar_month:")
