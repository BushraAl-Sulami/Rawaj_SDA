import os

import streamlit as st

from ui import api
from ui.icons import icon, star

DEMO_EMAIL = "demo@rawaj.app"
DEMO_PASSWORD = "rawaj-demo"
# Development convenience: any email plus a 6+ character password signs in as staff. Set
# RAWAJ_DEMO_LOGIN=false to require real accounts (restaurant owners or ADMIN_USERNAME).
DEMO_LOGIN = os.getenv("RAWAJ_DEMO_LOGIN", "true").strip().lower() not in {"0", "false", "no", "off"}


def sign_in(role: str, username: str, restaurant_id=None) -> None:
    st.session_state.update(
        authenticated=True, role=role, username=username, user_email=username, restaurant_id=restaurant_id,
    )
    st.rerun()


def fill_demo() -> None:
    st.session_state.login_email = DEMO_EMAIL
    st.session_state.login_pw = DEMO_PASSWORD


left, right = st.columns(2, gap=None)

with left:
    st.markdown(
        f"""
        <div class="login-left">
          <div class="brand">
            <div class="brand-mark">{star(18)}</div>
            <div><p class="brand-name">Rawaj</p><p class="brand-tag">Growth for Local Flavors</p></div>
          </div>
          <div class="mid">
            <div class="eyebrow"><span class="dot"></span>Made for local favorites</div>
            <h1>Good food.<br>Great stories.<br><em>Room to grow.</em></h1>
            <p class="lead">You have something special to share. Turn it into a thoughtful monthly plan and content worth sharing.</p>
            <div class="orbit">
              <div class="ring" style="left:100px; top:0; width:150px; height:150px;"></div>
              <div class="ring" style="left:60px; top:20px; width:230px; height:110px; transform:rotate(-24deg);"></div>
              <div class="star">{star(70)}</div>
              <div class="note" style="left:0; top:6px; transform:rotate(-5deg);">{icon('calendar', 14)} A little more consistency.</div>
              <div class="note" style="left:230px; top:112px; transform:rotate(4deg);">{icon('coffee', 14)} A lot more you.</div>
            </div>
          </div>
          <div class="foot"><span>A clear direction for your next chapter.</span>{icon('sparkles', 16)}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

with right:
    st.markdown(
        f"""
        <div class="login-right">
          <div class="top">
            <div class="brand-mark">{star(18)}</div>
            <span class="chip">Your growth workspace</span>
          </div>
          <div class="eyebrow">Let's make something good</div>
          <h2>Welcome to Rawaj.</h2>
          <p class="lead">One home for your strategy, your calendar, and your next great idea.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.form("login", border=False):
        email = st.text_input("Username", key="login_email", placeholder="The username from your Rawaj email")
        password = st.text_input(
            "Password", key="login_pw", type="password", placeholder="Your password"
        )
        submitted = st.form_submit_button(
            "Sign in", icon=":material/arrow_forward:", type="primary", use_container_width=True
        )

    if submitted:
        try:
            user = api.login(email.strip(), password)
            sign_in(user["role"], user["username"], user.get("restaurant_id"))
        except api.ApiError as error:
            if error.status == 401 and not (DEMO_LOGIN and "@" in email and len(password) >= 6):
                st.error("Incorrect username or password.")
            elif error.status not in (None, 401):
                st.error("Could not sign in right now. Please try again.")
            elif DEMO_LOGIN and "@" in email and len(password) >= 6:
                sign_in("demo", email.strip())
            else:
                st.error("Could not reach the Rawaj service. Please try again.")

    if DEMO_LOGIN:
        st.markdown(
            """
            <div class="login-right"><div class="preview">
              <b>Preview access</b>
              Demo sign-in is on: any email with a password of 6+ characters works.
              Client accounts use the username and password from their Rawaj email.
            </div></div>
            """,
            unsafe_allow_html=True,
        )
        st.button("Fill demo details", icon=":material/arrow_forward:", type="tertiary", key="fill", on_click=fill_demo)
