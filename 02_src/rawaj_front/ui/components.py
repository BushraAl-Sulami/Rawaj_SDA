"""Shared layout pieces: sidebar, top bar, page header, footer."""

import os
from html import escape

import streamlit as st

from ui import api
from ui.icons import icon, star


def user_name() -> str:
    email = st.session_state.get("user_email", "demo@rawaj.app")
    return (email.split("@")[0] or "Demo").title()


@st.cache_data(ttl=5, show_spinner=False)
def _restaurants() -> list[dict] | None:
    try:
        return api.list_restaurants()
    except api.ApiError:
        return None


def forget_cached_data() -> None:
    """Drop what the pages remember (the restaurant list is kept for 5 seconds) so the next read comes from the API."""
    _restaurants.clear()


def current_restaurant() -> dict | None:
    """The signed-in owner's restaurant; the workspace belongs to one restaurant, so there is no picker.

    Resolved from RAWAJ_RESTAURANT_ID if set, else the restaurant whose contact email matches the
    sign-in email, else the first restaurant (demo sign-in accepts any email).
    """
    restaurants = _restaurants() or []
    if not restaurants:
        return None
    if st.session_state.get("role") == "owner":  # an owner only ever sees their own restaurant
        return next((r for r in restaurants if r["id"] == st.session_state.get("restaurant_id")), None)
    pinned = os.getenv("RAWAJ_RESTAURANT_ID", "").strip()
    email = st.session_state.get("user_email", "").strip().lower()
    return (
        next((r for r in restaurants if pinned.isdigit() and r["id"] == int(pinned)), None)
        or next((r for r in restaurants if email and (r.get("email") or "").lower() == email), None)
        or restaurants[0]
    )


def sidebar() -> None:
    workspace = current_restaurant()
    name = workspace["name"] if workspace else "Your workspace"
    with st.sidebar:
        st.markdown(
            f"""
            <div class="brand">
              <div class="brand-mark">{star(18)}</div>
              <div><p class="brand-name">Rawaj</p><p class="brand-tag">Growth for Local Flavors</p></div>
            </div>
            <div class="workspace">
              <span class="pill">{escape(name[:1])}</span>
              <div><small>Your workspace</small><strong>{escape(name)}</strong></div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        st.page_link("views/home.py", label="Home", icon=":material/home:")
        st.page_link("views/strategy.py", label="Monthly Strategy", icon=":material/target:")
        st.page_link("views/content.py", label="Content Creation", icon=":material/auto_awesome:")

        with st.container(key="side_bottom"):
            st.markdown(
                f"""
                <div class="rooted">
                  <div class="row"><b>Rooted in your story.</b>{icon('sparkles', 15)}</div>
                  <p>Your business context connects every part of your workspace.</p>
                  <div class="tag">{icon('utensils', 14)} Your workspace</div>
                </div>
                <div class="copyright">© 2026 Rawaj</div>
                """,
                unsafe_allow_html=True,
            )


def topbar(title: str) -> None:
    with st.container(key="topbar"):
        left, mid, right = st.columns([6, 1.3, 0.45], vertical_alignment="center", gap="small")
        left.markdown(
            f'<div class="crumb">Workspace<i>/</i><b>{escape(title)}</b></div>', unsafe_allow_html=True
        )
        name = user_name()
        mid.markdown(
            f'<div class="user"><span class="avatar">{escape(name[:1])}</span>{escape(name)}</div>',
            unsafe_allow_html=True,
        )
        with right:
            if st.button("", icon=":material/logout:", key="logout", help="Sign out"):
                for key in ("authenticated", "role", "restaurant_id", "username", "user_email"):
                    st.session_state.pop(key, None)
                st.rerun()


def page_head(title: str, subtitle: str = "", badge: str = "", badge_icon: str = "") -> None:
    sub = f"<p>{escape(subtitle)}</p>" if subtitle else ""
    chip = f'<span class="chip ghost">{badge_icon}{escape(badge)}</span>' if badge else ""
    st.markdown(
        f'<div class="page-head"><div><h2>{escape(title)}</h2>{sub}</div>{chip}</div>',
        unsafe_allow_html=True,
    )


def footer() -> None:
    st.markdown(
        '<div class="footer"><span>Rawaj · Your strategy, made actionable.</span>'
        "<span>Made for local food &amp; drink businesses</span></div>",
        unsafe_allow_html=True,
    )
