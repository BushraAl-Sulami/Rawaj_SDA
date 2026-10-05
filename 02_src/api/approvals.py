"""Manual decisions on paused Outreach emails.

Routine emails are sent automatically (the `send_emails` node in orchestration/workflow.py). These routes exist for the
emails that are not: they list the paused emails and carry a reviewer's decision back into the same paused
run. "APPROVED" makes the agent send that exact, immutable email; "REJECTED" requires a reason and produces a replacement draft for review. There is no page for
this in the dashboard.
"""

import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from api.auth import require_admin_token
from api.schemas import InputModel
from database.models import OutboundMessage, Restaurant
from orchestration.workflow import paused_email_requests

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/approvals", tags=["Approvals"], dependencies=[Depends(require_admin_token)])


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


def get_outreach_application(request: Request):
    """The wired Outreach runtime, created on first use so the API starts without Outreach settings."""
    state = request.app.state
    with state.outreach_lock:
        if state.outreach_application is None:
            try:
                state.outreach_application = state.outreach_application_factory()
            except Exception as error:
                logger.error("Outreach agent is not available: %s", error)
                raise HTTPException(503, "The Outreach agent is not configured. Check its settings and retry.") from None
        return state.outreach_application


Outreach = Annotated[Any, Depends(get_outreach_application)]


class PendingApproval(BaseModel):
    message_id: str
    revision: int
    thread_id: str
    restaurant_id: int | None
    restaurant_name: str | None
    recipient: str
    subject: str
    body: str
    message_type: str
    expires_at: str | None
    review: dict[str, Any] | None = None


class DecisionRequest(InputModel):
    decision: Literal["APPROVED", "REJECTED"]
    reviewer_id: str = Field(min_length=1, max_length=255)
    note: str | None = Field(default=None, max_length=1000)


class DecisionResponse(BaseModel):
    message_id: str
    decision: str
    email_status: str | None
    sent: bool
    errors: list[str]


def _paused_email_requests(application) -> list[dict[str, Any]]:
    return paused_email_requests(application)


def _pending(db: Session, request_payload: dict[str, Any]) -> PendingApproval:
    message = db.get(OutboundMessage, request_payload["message_id"])
    restaurant = db.get(Restaurant, message.restaurant_id) if message else None
    return PendingApproval(
        message_id=request_payload["message_id"],
        revision=int(request_payload["message_revision"]),
        thread_id=request_payload["thread_id"],
        restaurant_id=message.restaurant_id if message else None,
        restaurant_name=restaurant.name if restaurant else None,
        recipient=request_payload.get("recipient", ""),
        subject=request_payload.get("subject", ""),
        body=message.plain_text_body if message else "",
        message_type=request_payload.get("message_type", ""),
        expires_at=request_payload.get("expires_at"),
        review=request_payload.get("review") if isinstance(request_payload.get("review"), dict) else None,
    )


@router.get("", response_model=list[PendingApproval])
def list_pending(db: Database, application: Outreach):
    return [_pending(db, payload) for payload in _paused_email_requests(application)]


@router.post("/{message_id}/decision", response_model=DecisionResponse)
def decide(message_id: Annotated[str, Path(min_length=1, max_length=255)], payload: DecisionRequest,
           request: Request, application: Outreach):
    with request.app.state.outreach_lock:
        pending = next((p for p in _paused_email_requests(application) if p["message_id"] == message_id), None)
        if pending is None:
            raise HTTPException(404, "No email is waiting for approval with this ID. It may already be decided or expired.")
        try:
            result = application.workflow.resume_human_approval(
                thread_id=pending["thread_id"],
                reviewer_id=payload.reviewer_id,
                decision=payload.decision,
                note=payload.note,
                message_id=pending["message_id"],
                message_revision=pending["message_revision"],
                content_sha256=pending["content_sha256"],
            )
        except Exception as error:
            # A safety refusal (expired, content changed, ...) is the reviewer's to see; anything else is not.
            if type(error).__name__ == "WorkflowSafetyError":
                raise HTTPException(409, str(error)) from None
            logger.error("Approval decision failed (%s)", type(error).__name__)
            raise HTTPException(502, "The decision could not be applied. Check the Outreach settings and retry.") from None
    execution_status = getattr(getattr(result, "execution", None), "email_status", None)
    draft_status = getattr(getattr(result, "email_draft", None), "status", None)
    return DecisionResponse(
        message_id=message_id,
        decision=payload.decision,
        email_status=str(getattr(draft_status, "value", draft_status)) if draft_status else None,
        sent=str(getattr(execution_status, "value", execution_status)) in {"SENT", "DELIVERED"},
        errors=list(result.errors or []),
    )
