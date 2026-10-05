"""Sign-in routes for the dashboard: restaurant owners (accounts) and the staff admin (environment)."""

import hmac
import os
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api import accounts
from api.schemas import InputModel

router = APIRouter(prefix="/api/auth", tags=["Auth"])


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


def require_admin_token(x_admin_token: Annotated[str | None, Header()] = None) -> None:
    """Guards staff-only routes (email approvals). Set ADMIN_API_TOKEN; when it is unset (local development) the routes are open."""
    expected = os.getenv("ADMIN_API_TOKEN", "")
    if not expected and os.getenv("RAWAJ_ENV", "").lower() in {"production", "prod"}:
        raise HTTPException(503, "ADMIN_API_TOKEN must be configured.")
    if expected and not hmac.compare_digest(x_admin_token or "", expected):
        raise HTTPException(401, "Admin access required.")


class LoginRequest(InputModel):
    username: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=255)


class LoginResponse(BaseModel):
    role: Literal["owner", "admin"]
    username: str
    restaurant_id: int | None = None
    access_token: str | None = None


@router.post("/login", response_model=LoginResponse)
def login(payload: LoginRequest, db: Database):
    if accounts.admin_login(payload.username, payload.password):
        return LoginResponse(role="admin", username=payload.username.strip().lower())
    try:
        account = accounts.authenticate(db, payload.username, payload.password)
    except accounts.AuthError:
        raise HTTPException(401, "Incorrect username or password.") from None
    return LoginResponse(role="owner", username=account.username, restaurant_id=account.restaurant_id, access_token=accounts.owner_token(account))
