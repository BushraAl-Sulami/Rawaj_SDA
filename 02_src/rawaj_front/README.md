# Rawaj front-end (Streamlit)

Talks to the Rawaj backend **only through the FastAPI API** (no direct database access).

Start the backend from the project root, then the front-end:

    uvicorn api.main:app --reload            # http://127.0.0.1:8000
    cd rawaj_front
    pip install -r requirements.txt
    streamlit run app.py

Set `RAWAJ_API_URL` if the backend is not on `http://127.0.0.1:8000`.

## Files
- `app.py`               entry point: page setup, sign-in gate, navigation
- `views/`               the 4 pages (login, home, strategy, content)
- `ui/api.py`            HTTP client for the FastAPI backend
- `ui/theme.py`          all CSS: colors (navy / light blue / white / black) and fonts
- `ui/components.py`     sidebar, top bar, page header, footer; resolves the owner's restaurant
- `ui/icons.py`          inline SVG icons
- `.streamlit/config.toml` light theme, hides Streamlit's default page nav

## Home page
The workspace belongs to one restaurant (no picker): `RAWAJ_RESTAURANT_ID` if set, else the restaurant
whose email matches the sign-in email, else the first one. `GET /api/restaurants/{id}/gaps` returns the
latest marketing gaps with counts per severity (High / Moderate / Low). Data limitations are saved but not shown on the page.
The page re-reads the API every 5 seconds.

## Task completion review

Marking calendar tasks complete updates Task progress. After every counted task
is Completed, a large review dialog opens once for that restaurant and plan.
Break days do not count, and an empty plan cannot trigger completion. The same
prompt is available when the final task is marked posted on Content Creation.
Closing the dialog leaves a Write a review button; a saved review can be edited.

Home shows task progress and the current plan's submitted rating and review.
Reviews and prompt dismissal are frontend session state only: they survive page
navigation in that session but are lost when the Streamlit session resets. No
review API, database write, email or Agency-dashboard sync is added. Task status
continues to use the existing API. Different restaurants and plans have separate
review keys.
