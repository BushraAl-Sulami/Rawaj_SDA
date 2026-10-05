import streamlit as st

from ui.components import sidebar
from ui.theme import apply_theme

st.set_page_config(
    page_title="Rawaj",
    page_icon="✦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.session_state.setdefault("authenticated", False)
signed_in = st.session_state.authenticated

home = st.Page("views/home.py", title="Home", url_path="home", default=True)
login = st.Page("views/login.py", title="Sign in", url_path="login")
pages = [
    home,
    st.Page("views/strategy.py", title="Monthly Strategy", url_path="strategy"),
    st.Page("views/content.py", title="Content Creation", url_path="content"),
    login,
]
current = st.navigation(pages, position="hidden")

# Gate every page behind sign-in.
if not signed_in and current != login:
    st.switch_page(login)
if signed_in and current == login:
    st.switch_page(home)

apply_theme(login=not signed_in)
if signed_in:
    sidebar()
current.run()
