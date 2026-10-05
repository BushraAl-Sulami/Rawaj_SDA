import re
from html import escape

import streamlit as st

from ui import api
from ui.components import current_restaurant, footer, forget_cached_data, page_head, topbar
from ui.evidence import display_evidence
from ui.icons import icon
from ui.plan import load as load_plan
from ui.reviews import dashboard_progress

# Facts come from the saved qualification through the API. Technical evidence fields
# are formatted for restaurant owners; their saved values are not changed.

TIERS = [
    ("total", "Total gaps", ""),
    ("high", "High", "high"),
    ("moderate", "Moderate", "moderate"),
    ("low", "Low", "low"),
    ("strengths", "Strengths", ""),
]
SEVERITY_CHIP = {"High": " sev-high", "Moderate": " sev-moderate", "Medium": " sev-moderate", "Low": " sev-low"}
VISIBLE = 3  # points shown at first; the rest sit behind "Show more"
ONE_LINE = 70  # a point shorter than this is shown as it is; a longer one is cut by the layout and opens on click


def insight(text: str, kind: str, symbol: str) -> str:
    """One saved sentence, word for word: two lines at first, the whole sentence when it is clicked."""
    dot = f'<span class="dot">{icon(symbol, 13, 2.4)}</span>'
    if len(text) <= ONE_LINE:
        return f'<div class="insight {kind}"><div class="row">{dot}<span class="txt">{escape(text)}</span></div></div>'
    return f'<details class="insight {kind}"><summary>{dot}<span class="txt">{escape(text)}</span></summary></details>'


def insights_card(key: str, title: str, subtitle: str, items: list[str], kind: str, symbol: str) -> None:
    """A card with the saved count and the saved points (strengths).

    The header opens and closes the whole list; inside, "Show more" / "Show less" reveals the points after the first few.
    """
    with st.container(key=key):
        rows = [insight(text, kind, symbol) for text in items]
        more = ""
        if len(rows) > VISIBLE:
            more = (
                f'<details class="more"><summary><span class="t-open">Show {len(rows) - VISIBLE} more</span>'
                f'<span class="t-close">Show less</span></summary>{"".join(rows[VISIBLE:])}</details>'
            )
        st.markdown(
            f"""
            <details class="insbox" open>
              <summary class="ins-head {kind}"><span class="badge">{icon(symbol, 18, 2.2)}</span>
                <div><b>{title}</b><small>{subtitle}</small></div><span class="count">{len(items)}</span></summary>
              {"".join(rows[:VISIBLE])}{more}
            </details>
            """,
            unsafe_allow_html=True,
        )


def paragraphs(text: str) -> str:
    return "".join(f"<p>{escape(part.strip())}</p>" for part in re.split(r"\n\s*\n", text) if part.strip())


def gap_details(gap: dict) -> str:
    """What opens under a gap: its saved description, why it is a gap (rationale), the readable evidence, the confidence."""
    parts = []
    if gap.get("description"):
        parts.append(f'<div class="dl"><small>Description</small>{paragraphs(gap["description"])}</div>')
    if gap.get("rationale"):
        parts.append(f'<div class="dl"><small>Why this is a gap</small>{paragraphs(gap["rationale"])}</div>')
    evidence = display_evidence(gap.get("evidence") or [])
    if evidence:
        items = "".join(f"<li>{escape(text)}</li>" for text in evidence)
        parts.append(f'<div class="dl"><small>Evidence</small><ul class="ev">{items}</ul></div>')
    if gap.get("confidence"):
        parts.append(f'<div class="dl"><small>Confidence</small><p>{escape(gap["confidence"])}</p></div>')
    return "".join(parts)


def gap_row(number: int, gap: dict) -> str:
    """One saved gap; its saved details open when the row is clicked."""
    head = (
        f'<span class="num">{number:02d}</span>'
        f'<div class="body"><b>{escape(gap["gap"])}</b><span>{escape(gap["recommendation_focus"])}</span></div>'
        f'<span class="chip{SEVERITY_CHIP.get(gap["severity"], "")}">{escape(gap["severity"] or "Unrated")}</span>'
    )
    details = gap_details(gap)
    if not details:
        return f'<div class="gap">{head}</div>'
    return f'<details class="gapx"><summary class="gap">{head}</summary><div class="gapdetail">{details}</div></details>'


def refresh_now() -> None:
    """The Refresh button: forget what is cached and read everything from the API again."""
    forget_cached_data()
    st.session_state.just_refreshed = True


def source_line(data: dict) -> str:
    """Which saved qualification is on screen (its row number in the table); no times."""
    if data["qualification_id"] is None:
        return "no saved qualification for this restaurant"
    return f"read from qualification_runs.full_result · row {data['qualification_id']}"


@st.fragment(run_every=5)
def live_gaps() -> None:
    restaurant = current_restaurant()
    if restaurant is None:
        st.warning(f"No restaurants to show. Check that the Rawaj API is running at {api.API_URL}.")
        return
    try:
        data = api.get_gaps(restaurant["id"])
    except api.ApiError as exc:
        st.warning(f"Could not load the marketing gaps from the Rawaj API. {exc}")
        return

    counts, gaps = data["counts"], data["gaps"]
    st.markdown(
        '<div class="tiers">'
        + "".join(
            f'<div class="tier {extra}{" zero" if extra and not counts[key] else ""}">'
            f"<small>{label}</small><b>{counts[key]}</b></div>"
            for key, label, extra in TIERS
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    main, side = st.columns([2.2, 1], gap="medium")

    with main:
        with st.container(key="card_gaps"):
            if gaps:
                st.markdown(
                    f"""
                    <div class="card-head"><h3 class="card-title">Where {escape(data['restaurant_name'])} can grow</h3>
                    <span class="muted">{counts['total']} gaps · click a gap to see why</span></div>
                    {"".join(gap_row(i, g) for i, g in enumerate(gaps, 1))}
                    """,
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    f"""
                    <div class="empty">
                      <div class="ring">{icon('target', 26)}</div>
                      <h3>No marketing gaps yet.</h3>
                      <p>Run research to generate the latest qualification analysis.</p>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    with side:
        with st.container(key="card_next"):
            top = gaps[0] if gaps else None  # the gap with the highest saved priority
            direction = (
                f'<p style="margin:.7rem 0 0;"><b>{escape(top["gap"])}</b></p>'
                f'<p class="muted" style="margin:.3rem 0 0;">{escape(top["recommendation_focus"])}</p>'
                if top
                else '<p class="muted" style="margin:.7rem 0 0;">Nothing to act on until a qualification is saved.</p>'
            )
            st.markdown(
                f"""
                <div class="eyebrow" style="letter-spacing:0; text-transform:none; font-size:11px; color:var(--navy);">
                  {icon('sparkles', 15)} <b>Your next direction</b>
                </div>
                {direction}
                """,
                unsafe_allow_html=True,
            )
            if st.button("Refresh", type="primary", key="refresh_gaps", icon=":material/refresh:"):
                refresh_now()
                st.rerun()  # the whole page, not only this block: the sidebar and every card read the API again
            st.markdown(
                f'<div class="status"><b>live</b><span>{escape(source_line(data))} · updates every 5 seconds.</span></div>',
                unsafe_allow_html=True,
            )
            if st.session_state.pop("just_refreshed", False):
                st.toast("Refreshed", icon=":material/check_circle:")

    if data["strengths"]:
        insights_card("card_strengths", "Strengths", "What already works well", data["strengths"], "good", "check")


topbar("Home")
page_head("Marketing gaps", badge="Marketing assessment")
restaurant = current_restaurant()
if restaurant:
    try:
        plan = load_plan(restaurant)
    except api.ApiError:
        plan = None
    if plan:
        dashboard_progress(restaurant, plan)
live_gaps()
footer()
