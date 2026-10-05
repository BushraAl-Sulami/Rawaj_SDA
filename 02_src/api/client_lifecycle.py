"""Authenticated owner trial and feedback endpoints."""
import hashlib
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select

from api.accounts import AuthError, verify_owner_token
from database.models import ClientTrial, ClientFeedbackRecord, OutreachRelationship

router = APIRouter(prefix="/api/client", tags=["Client trial"])


def owner(request: Request, authorization: Annotated[str | None, Header()] = None):
    with request.app.state.session_factory() as db:
        try:
            if not authorization or not authorization.startswith("Bearer "):
                raise AuthError()
            restaurant_id = verify_owner_token(db, authorization[7:])
        except AuthError:
            raise HTTPException(401, "Please sign in to your Rawaj account.") from None
        yield db, restaurant_id


class FeedbackInput(BaseModel):
    message: str = Field(min_length=1, max_length=5000)
    rating: int | None = Field(default=None, ge=1, le=5)

    @field_validator("message")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("Please enter your feedback.")
        return value.strip()


@router.get("/trial")
def trial_status(context=Depends(owner)):
    db, restaurant_id = context
    trial = db.get(ClientTrial, restaurant_id)
    if trial is None:
        raise HTTPException(409, "The account has not been activated.")
    return {"activated_at": trial.activated_at.isoformat() + "Z",
            "expires_at": trial.expires_at.isoformat() + "Z",
            "active": datetime.utcnow() < trial.expires_at}


@router.post("/feedback")
def feedback(payload: FeedbackInput, context=Depends(owner)):
    db, restaurant_id = context
    key = hashlib.sha256(f"{restaurant_id}|{payload.rating}|{payload.message}".encode()).hexdigest()
    if db.get(ClientFeedbackRecord, key) is None:
        db.add(ClientFeedbackRecord(id=key, restaurant_id=restaurant_id,
            message=payload.message, rating=payload.rating, source="dashboard"))
        relation = db.scalar(select(OutreachRelationship).where(OutreachRelationship.restaurant_id == restaurant_id))
        if relation:
            memory = dict(relation.memory_json or {})
            memory["feedback"] = list(memory.get("feedback", [])) + [{"feedback_id": key,
                "message": payload.message, "received_at": datetime.utcnow().isoformat() + "Z"}]
            relation.memory_json = memory
            relation.next_contact_at = None
            relation.version += 1
        db.commit()
    return {"feedback_id": key, "saved": True}
