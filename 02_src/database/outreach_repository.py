"""Persistence layer for Rawaj's Outreach & Follow-Up Agent.

This repository is the only component that reads or writes the Outreach tables.
It preserves immutable provenance, approval integrity, button idempotency, and
truthful email/provider status without turning the project into a full CRM.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Generator

from sqlalchemy import select, update, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.database import SessionLocal
from database.models import (
    ClientTrial,
    ClientFeedbackRecord,
    ButtonResponseEvent,
    EmailButtonConfirmation,
    HumanEscalationRecord,
    OutreachApproval,
    OutreachEvent,
    OutreachRelationship,
    OutboundMessage,
    QualificationRun,
    ResearchRun,
    Restaurant,
    StrategyRequest,
)


def _dump(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return dict(value)
    raise TypeError("Expected a Pydantic model or dictionary.")


def _iso(value: datetime | None) -> str | None:
    return value.replace(tzinfo=timezone.utc).isoformat() if value else None


def _to_datetime(value: str | datetime | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _require_string(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{field_name} is required.")
    return text


def _require_int(value: Any, field_name: str) -> int:
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field_name} must be an integer.") from error


class OutreachRepository:
    """Database operations used by LangGraph nodes and the LangChain tools."""

    def __init__(
        self,
        session_factory: Callable[[], Session] = SessionLocal,
    ) -> None:
        self._session_factory = session_factory

    @contextmanager
    def _session(self) -> Generator[Session, None, None]:
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    # ------------------------------------------------------------------
    # Shared serializers
    # ------------------------------------------------------------------

    @staticmethod
    def _restaurant_payload(restaurant: Restaurant) -> dict[str, Any]:
        return {
            "restaurant_id": restaurant.id,
            "name": restaurant.name,
            "email": restaurant.email,
            "instagram_username": restaurant.instagram_username,
            "instagram_url": restaurant.instagram_url,
            "location": restaurant.location,
        }

    @classmethod
    def _relationship_payload(
        cls, relationship: OutreachRelationship
    ) -> dict[str, Any]:
        extra = relationship.memory_json or {}
        return {
            "relationship_id": f"relationship_{relationship.id}",
            "restaurant_id": relationship.restaurant_id,
            "research_run_id": relationship.research_run_id,
            "qualification_run_id": relationship.qualification_run_id,
            "status": relationship.status,
            "outreach_attempts": relationship.outreach_attempts,
            "last_outbound_message_id": relationship.last_outbound_message_id,
            "last_outbound_at": _iso(relationship.last_outbound_at),
            "last_inbound_event_id": relationship.last_inbound_event_id,
            "last_inbound_at": _iso(relationship.last_inbound_at),
            "next_contact_at": _iso(relationship.next_contact_at),
            "contact_later_until": _iso(relationship.contact_later_until),
            "interest_event_id": relationship.interest_event_id,
            "strategy_request_id": relationship.strategy_request_id,
            "strategy_id": relationship.strategy_id,
            "strategy_status": extra.get("strategy_status"),
            "feedback": extra.get("feedback", []),
            "issues": extra.get("issues", []),
            "important_context": extra.get("important_context", []),
            "do_not_contact_at": _iso(relationship.do_not_contact_at),
            "version": relationship.version,
            "updated_at": _iso(relationship.updated_at),
        }

    @staticmethod
    def _message_payload(message: OutboundMessage) -> dict[str, Any]:
        return {
            "message_id": message.id,
            "relationship_id": message.relationship_id,
            "restaurant_id": message.restaurant_id,
            "research_run_id": message.research_run_id,
            "qualification_run_id": message.qualification_run_id,
            "supersedes_message_id": message.supersedes_message_id,
            "message_type": message.message_type,
            "action": message.action,
            "recipient": message.recipient_email,
            "subject": message.subject,
            "html_body": message.html_body,
            "plain_text_body": message.plain_text_body,
            "language": message.language,
            "personalization_context": message.personalization_context_json or {},
            "content_version": message.content_version,
            "content_sha256": message.content_sha256,
            "status": message.status,
            "provider_name": message.provider_name,
            "provider_message_id": message.provider_message_id,
            "provider_status": message.provider_status,
            "last_error": message.last_error,
            "created_at": _iso(message.created_at),
            "approved_at": _iso(message.approved_at),
            "sent_at": _iso(message.sent_at),
            "delivered_at": _iso(message.delivered_at),
        }

    @staticmethod
    def _button_event_payload(event: ButtonResponseEvent) -> dict[str, Any]:
        return {
            "event_id": event.id,
            "restaurant_id": event.restaurant_id,
            "outreach_message_id": event.outreach_message_id,
            "token_id": event.token_id,
            "action": event.action,
            "status": event.status,
            "idempotency_key": event.idempotency_key,
            "clicked_at": _iso(event.clicked_at),
            "processed_at": _iso(event.processed_at),
        }

    @staticmethod
    def _strategy_request_payload(request: StrategyRequest) -> dict[str, Any]:
        return {
            "strategy_request_id": request.id,
            "relationship_id": request.relationship_id,
            "restaurant_id": request.restaurant_id,
            "research_run_id": request.research_run_id,
            "qualification_run_id": request.qualification_run_id,
            "outreach_message_id": request.outreach_message_id,
            "interest_event_id": request.interest_event_id,
            "customer_request": request.customer_request,
            "research_context": request.research_context_json,
            "qualification_context": request.qualification_context_json,
            "customer_context": request.customer_context_json or {},
            "status": request.status,
            "dispatch_metadata": request.dispatch_metadata_json or {},
            "strategy_id": request.strategy_id,
            "strategy_output": request.strategy_output_json,
            "dashboard_strategy_url": request.dashboard_strategy_url,
            "client_notification_allowed": request.client_notification_allowed,
            "created_at": _iso(request.created_at),
            "completed_at": _iso(request.completed_at),
        }

    # ------------------------------------------------------------------
    # Upstream Research and Qualification handoffs
    # ------------------------------------------------------------------

    @staticmethod
    def _assert_provenance(
        session: Session,
        *,
        restaurant_id: int,
        research_run_id: int,
        qualification_run_id: int,
    ) -> tuple[Restaurant, ResearchRun, QualificationRun]:
        restaurant = session.get(Restaurant, restaurant_id)
        if restaurant is None:
            raise ValueError("Restaurant was not found.")

        research = session.get(ResearchRun, research_run_id)
        if research is None or research.restaurant_id != restaurant_id:
            raise ValueError("Research run does not belong to this restaurant.")

        qualification = session.get(QualificationRun, qualification_run_id)
        if qualification is None or qualification.restaurant_id != restaurant_id:
            raise ValueError(
                "Qualification run does not belong to this restaurant."
            )
        if qualification.research_run_id != research_run_id:
            raise ValueError(
                "Qualification run is not linked to the supplied Research run."
            )

        return restaurant, research, qualification

    def load_research_handoff(
        self,
        *,
        restaurant_id: int,
        research_run_id: int,
    ) -> dict[str, Any]:
        """Load one exact Research run rather than a name-based latest result."""

        with self._session() as session:
            restaurant = session.get(Restaurant, restaurant_id)
            research = session.get(ResearchRun, research_run_id)

            if restaurant is None:
                raise ValueError("Restaurant was not found.")
            if research is None or research.restaurant_id != restaurant_id:
                raise ValueError(
                    "Research run does not belong to the supplied restaurant."
                )

            full_result = research.full_result or {}
            metadata = full_result.get("analysis_metadata", {})

            return {
                "restaurant": self._restaurant_payload(restaurant),
                "research_run_id": research.id,
                "analysis_status": research.status,
                "generated_at": _iso(research.analyzed_at)
                or metadata.get("analyzed_at"),
                "profile": research.profile or {},
                "profile_analysis": research.profile_analysis or {},
                "metrics": research.metrics or {},
                "research_signals": research.research_signals or [],
                "analysis_coverage": research.analysis_coverage or {},
                "data_quality": research.data_quality or {},
                "schema_version": research.schema_version,
            }

    def load_qualification_handoff(
        self,
        *,
        restaurant_id: int,
        qualification_run_id: int,
        research_run_id: int,
    ) -> dict[str, Any]:
        """Load one exact Qualification run with its Research provenance."""

        with self._session() as session:
            _, _, qualification = self._assert_provenance(
                session,
                restaurant_id=restaurant_id,
                research_run_id=research_run_id,
                qualification_run_id=qualification_run_id,
            )

            return {
                "restaurant_id": qualification.restaurant_id,
                "qualification_run_id": qualification.id,
                "research_run_id": qualification.research_run_id,
                "qualification": qualification.qualification or "",
                "decision_rationale": qualification.decision_rationale,
                "marketing_gaps": qualification.marketing_gaps or [],
                "strengths": qualification.strengths or [],
                "data_limitations": qualification.data_limitations or [],
                "agent": qualification.agent,
                "status": qualification.status,
            }

    # ------------------------------------------------------------------
    # Relationship memory
    # ------------------------------------------------------------------

    def get_or_create_relationship(
        self,
        *,
        restaurant_id: int,
        research_run_id: int,
        qualification_run_id: int,
    ) -> dict[str, Any]:
        """Create the one Outreach relationship for a verified upstream chain."""

        with self._session() as session:
            self._assert_provenance(
                session,
                restaurant_id=restaurant_id,
                research_run_id=research_run_id,
                qualification_run_id=qualification_run_id,
            )

            relationship = session.scalar(
                select(OutreachRelationship).where(
                    OutreachRelationship.restaurant_id == restaurant_id
                )
            )

            if relationship is None:
                relationship = OutreachRelationship(
                    restaurant_id=restaurant_id,
                    research_run_id=research_run_id,
                    qualification_run_id=qualification_run_id,
                    status="READY_TO_CONTACT",
                )
                session.add(relationship)
                session.flush()
            elif (
                relationship.research_run_id not in {None, research_run_id}
                or relationship.qualification_run_id
                not in {None, qualification_run_id}
            ):
                raise ValueError(
                    "Existing Outreach relationship is linked to different "
                    "Research or Qualification runs."
                )
            else:
                changed = False
                if relationship.research_run_id != research_run_id:
                    relationship.research_run_id = research_run_id
                    changed = True
                if relationship.qualification_run_id != qualification_run_id:
                    relationship.qualification_run_id = qualification_run_id
                    changed = True
                if changed:
                    relationship.version += 1
                    session.flush()

            return self._relationship_payload(relationship)

    def read_relationship_memory(
        self,
        *,
        restaurant_id: int,
    ) -> dict[str, Any] | None:
        """Return persisted relationship memory, or None before outreach begins."""

        with self._session() as session:
            relationship = session.scalar(
                select(OutreachRelationship).where(
                    OutreachRelationship.restaurant_id == restaurant_id
                )
            )
            return (
                self._relationship_payload(relationship)
                if relationship is not None
                else None
            )

    def list_due_follow_up_relationships(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return only prospect relationships that are genuinely due to revisit.

        This is a scheduler-facing query, not a CRM export.  It deliberately
        excludes opt-outs and all non-prospect states.  The LangGraph hard gate
        still enforces timing, qualification, and maximum-attempt policy before
        it can draft or send a follow-up.
        """

        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000.")
        due_before = now or datetime.utcnow()
        with self._session() as session:
            relationships = session.scalars(
                select(OutreachRelationship)
                .where(
                    OutreachRelationship.status == "WAITING_FOR_RESPONSE",
                    OutreachRelationship.next_contact_at.is_not(None),
                    OutreachRelationship.next_contact_at <= due_before,
                    OutreachRelationship.do_not_contact_at.is_(None),
                )
                .order_by(OutreachRelationship.next_contact_at.asc())
                .limit(limit)
            ).all()
            return [self._relationship_payload(item) for item in relationships]

    def list_due_client_check_relationships(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Return due Free-Trial/client relationships for contextual reassessment.

        This does not send a generic weekly check-in.  It simply starts a graph
        run, which reads memory and decides whether a helpful message is
        warranted.  Explicit opt-outs remain excluded at the query boundary.
        """

        if limit <= 0 or limit > 1_000:
            raise ValueError("limit must be between 1 and 1000.")
        due_before = now or datetime.utcnow()
        with self._session() as session:
            relationships = session.scalars(
                select(OutreachRelationship)
                .where(
                    OutreachRelationship.status.in_(
                        {"STRATEGY_DELIVERED", "ACTIVE_CLIENT"}
                    ),
                    OutreachRelationship.next_contact_at.is_not(None),
                    OutreachRelationship.next_contact_at <= due_before,
                    OutreachRelationship.do_not_contact_at.is_(None),
                )
                .order_by(OutreachRelationship.next_contact_at.asc())
                .limit(limit)
            ).all()
            return [self._relationship_payload(item) for item in relationships]

    def write_relationship_memory(
        self,
        *,
        memory: Any,
        expected_version: int | None = None,
    ) -> dict[str, Any]:
        """Persist state/memory with optimistic locking."""

        data = _dump(memory)
        restaurant_id = _require_int(data.get("restaurant_id"), "restaurant_id")

        with self._session() as session:
            relationship = session.scalar(
                select(OutreachRelationship)
                .where(OutreachRelationship.restaurant_id == restaurant_id)
                .with_for_update()
            )
            if relationship is None:
                raise ValueError(
                    "Relationship does not exist. Create it from verified "
                    "Research and Qualification provenance first."
                )

            if (
                expected_version is not None
                and relationship.version != expected_version
            ):
                raise ValueError(
                    "Relationship memory was updated by another workflow run."
                )

            from agents.outreach_followup_agent.schemas import RelationshipStatus
            relationship.status = RelationshipStatus(data.get("status")).value
            relationship.outreach_attempts = _require_int(
                data.get("outreach_attempts", 0),
                "outreach_attempts",
            )
            relationship.last_outbound_message_id = data.get(
                "last_outbound_message_id"
            )
            relationship.last_outbound_at = _to_datetime(
                data.get("last_outbound_at")
            )
            relationship.last_inbound_event_id = data.get(
                "last_inbound_event_id"
            )
            relationship.last_inbound_at = _to_datetime(
                data.get("last_inbound_at")
            )
            relationship.next_contact_at = _to_datetime(
                data.get("next_contact_at")
            )
            relationship.contact_later_until = _to_datetime(
                data.get("contact_later_until")
            )
            relationship.interest_event_id = data.get("interest_event_id")
            relationship.strategy_request_id = data.get(
                "strategy_request_id"
            )
            relationship.strategy_id = data.get("strategy_id")
            relationship.do_not_contact_at = _to_datetime(
                data.get("do_not_contact_at")
            )
            for feedback in data.get("feedback", []):
                if session.get(ClientFeedbackRecord, feedback["feedback_id"]) is None:
                    session.add(ClientFeedbackRecord(id=feedback["feedback_id"], restaurant_id=restaurant_id,
                        message=feedback["message"], source="inbound", received_at=_to_datetime(feedback.get("received_at")) or datetime.utcnow()))
            relationship.memory_json = {
                "strategy_status": data.get("strategy_status"),
                "feedback": data.get("feedback", []),
                "issues": data.get("issues", []),
                "important_context": data.get("important_context", []),
            }
            relationship.version += 1
            session.flush()

            return self._relationship_payload(relationship)

    # ------------------------------------------------------------------
    # Drafts, Human Approval, and real provider execution
    # ------------------------------------------------------------------

    def save_outbound_message(
        self,
        *,
        draft: Any,
        provenance: Any,
        content_sha256: str,
        supersedes_message_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist an exact final email version before it can be approved."""

        draft_data = _dump(draft)
        provenance_data = _dump(provenance)
        restaurant_id = _require_int(
            provenance_data.get("restaurant_id"),
            "restaurant_id",
        )
        research_run_id = _require_int(
            provenance_data.get("research_run_id"),
            "research_run_id",
        )
        qualification_run_id = _require_int(
            provenance_data.get("qualification_run_id"),
            "qualification_run_id",
        )
        message_id = _require_string(draft_data.get("message_id"), "message_id")

        if str(draft_data.get("restaurant_id")) != str(restaurant_id):
            raise ValueError("Draft restaurant_id does not match provenance.")
        if len(content_sha256) != 64:
            raise ValueError("content_sha256 must be a SHA-256 hexadecimal hash.")

        with self._session() as session:
            self._assert_provenance(
                session,
                restaurant_id=restaurant_id,
                research_run_id=research_run_id,
                qualification_run_id=qualification_run_id,
            )
            relationship = session.scalar(
                select(OutreachRelationship).where(
                    OutreachRelationship.restaurant_id == restaurant_id
                )
            )
            if relationship is None:
                raise ValueError(
                    "Relationship must be created before saving an email draft."
                )
            if (
                relationship.research_run_id != research_run_id
                or relationship.qualification_run_id != qualification_run_id
            ):
                raise ValueError(
                    "Draft provenance does not match the active Outreach relationship."
                )

            existing = session.get(OutboundMessage, message_id)
            if existing is not None:
                # A draft that has never reached Human Approval may be revised
                # safely after a validation failure. Once any approval exists,
                # or a provider lifecycle has begun, its exact content remains
                # immutable and cannot be silently replaced.
                if existing.content_sha256 == content_sha256:
                    return self._message_payload(existing)

                existing_approval = session.scalar(
                    select(OutreachApproval.id)
                    .where(OutreachApproval.outreach_message_id == message_id)
                    .limit(1)
                )
                if existing.status != "GENERATED" or existing_approval is not None:
                    raise ValueError(
                        "An existing outbound message has already entered approval "
                        "or delivery and cannot be revised."
                    )

                existing.recipient_email = _require_string(
                    draft_data.get("recipient"), "recipient"
                )
                existing.subject = _require_string(draft_data.get("subject"), "subject")
                existing.html_body = _require_string(
                    draft_data.get("html_body"), "html_body"
                )
                existing.plain_text_body = _require_string(
                    draft_data.get("plain_text_body"), "plain_text_body"
                )
                existing.language = _require_string(draft_data.get("language"), "language")
                existing.personalization_context_json = {
                    "observation_ids": draft_data.get(
                        "personalization_observation_ids", []
                    ),
                    "context": draft_data.get("personalization_context", []),
                }
                existing.content_version += 1
                existing.content_sha256 = content_sha256
                existing.last_error = None
                self._record_event(
                    session,
                    restaurant_id=restaurant_id,
                    event_type="OUTBOUND_MESSAGE_REVISED_BEFORE_APPROVAL",
                    status="GENERATED",
                    recipient_email=existing.recipient_email,
                    metadata={
                        "message_id": existing.id,
                        "content_version": existing.content_version,
                    },
                )
                session.flush()
                return self._message_payload(existing)

            message = OutboundMessage(
                id=message_id,
                relationship_id=relationship.id,
                restaurant_id=restaurant_id,
                research_run_id=research_run_id,
                qualification_run_id=qualification_run_id,
                supersedes_message_id=supersedes_message_id,
                message_type=_require_string(
                    draft_data.get("message_type"),
                    "message_type",
                ),
                action=_require_string(draft_data.get("action"), "action"),
                recipient_email=_require_string(
                    draft_data.get("recipient"),
                    "recipient",
                ),
                subject=_require_string(draft_data.get("subject"), "subject"),
                html_body=_require_string(
                    draft_data.get("html_body"),
                    "html_body",
                ),
                plain_text_body=_require_string(
                    draft_data.get("plain_text_body"),
                    "plain_text_body",
                ),
                language=_require_string(
                    draft_data.get("language"),
                    "language",
                ),
                personalization_context_json={
                    "observation_ids": draft_data.get(
                        "personalization_observation_ids", []
                    ),
                    "context": draft_data.get("personalization_context", []),
                },
                content_version=_require_int(
                    draft_data.get("revision", 1),
                    "revision",
                ),
                content_sha256=content_sha256,
                status="GENERATED",
            )
            session.add(message)
            self._record_event(
                session,
                restaurant_id=restaurant_id,
                event_type="OUTBOUND_MESSAGE_GENERATED",
                status="GENERATED",
                recipient_email=message.recipient_email,
                metadata={"message_id": message.id},
            )
            session.flush()
            return self._message_payload(message)

    def save_human_approval(self, *, approval: Any) -> dict[str, Any]:
        """Store a Human Approval and bind it to immutable message content."""

        data = _dump(approval)
        approval_id = _require_string(data.get("approval_id"), "approval_id")
        message_id = _require_string(data.get("message_id"), "message_id")
        status = _require_string(data.get("status"), "status")

        with self._session() as session:
            message = session.get(OutboundMessage, message_id)
            if message is not None and str(data.get("reviewer_id", "")).startswith(("auto:", "system:")) and message.message_type == "INITIAL_OUTREACH":
                raise ValueError("Initial outreach requires a human decision.")
            if message is None:
                raise ValueError("Outbound message was not found.")

            if (
                message.content_version
                != _require_int(data.get("message_revision"), "message_revision")
                or message.content_sha256
                != _require_string(data.get("content_sha256"), "content_sha256")
            ):
                raise ValueError(
                    "Approval does not match the current immutable message version."
                )

            saved = session.get(OutreachApproval, approval_id)
            if saved is None:
                saved = OutreachApproval(
                    id=approval_id,
                    outreach_message_id=message_id,
                    content_version=message.content_version,
                    content_sha256=message.content_sha256,
                    status=status,
                    reviewer_id=data.get("reviewer_id"),
                    requested_at=_to_datetime(data.get("reviewed_at"))
                    or datetime.utcnow(),
                    expires_at=_to_datetime(data.get("expires_at")),
                    decided_at=_to_datetime(data.get("reviewed_at")),
                    note=data.get("note"),
                )
                session.add(saved)
            else:
                if saved.status != status or saved.content_sha256 != data["content_sha256"]:
                    raise ValueError("An authorization decision is immutable.")
                return {"approval_id": saved.id, "message_id": saved.outreach_message_id,
                    "status": saved.status, "content_version": saved.content_version,
                    "content_sha256": saved.content_sha256, "expires_at": _iso(saved.expires_at)}

            if status == "APPROVED":
                message.status = "APPROVED"
                message.approved_at = datetime.utcnow()
            elif status == "REJECTED":
                message.status = "REJECTED"

            self._record_event(
                session,
                restaurant_id=message.restaurant_id,
                event_type="AUTOMATIC_AUTHORIZATION" if str(data.get("reviewer_id", "")).startswith("system:") else "HUMAN_APPROVAL",
                status=status,
                recipient_email=message.recipient_email,
                metadata={"message_id": message.id, "approval_id": saved.id},
            )
            session.flush()

            return {
                "approval_id": saved.id,
                "message_id": saved.outreach_message_id,
                "status": saved.status,
                "content_version": saved.content_version,
                "content_sha256": saved.content_sha256,
                "expires_at": _iso(saved.expires_at),
            }

    def get_sendable_message(
        self,
        *,
        message_id: str,
        approval_id: str,
    ) -> dict[str, Any] | None:
        """Atomically claim one approved message immediately before provider send."""

        with self._session() as session:
            message = session.scalar(
                select(OutboundMessage)
                .where(OutboundMessage.id == message_id)
                .with_for_update()
            )
            approval = session.get(OutreachApproval, approval_id)

            if message is None or approval is None:
                return None
            if approval.outreach_message_id != message.id:
                return None
            if approval.status != "APPROVED" or message.status != "APPROVED":
                return None

            # A human-approved draft must never override a later explicit
            # opt-out.  Lock the relationship in the same transaction as the
            # message claim so a queued workflow cannot send after it sees a
            # persisted DO_NOT_CONTACT state.
            relationship = session.scalar(
                select(OutreachRelationship)
                .where(OutreachRelationship.id == message.relationship_id)
                .with_for_update()
            )
            if (
                relationship is None
                or relationship.restaurant_id != message.restaurant_id
                or relationship.status == "DO_NOT_CONTACT"
                or relationship.do_not_contact_at is not None
            ):
                return None
            if (
                approval.content_version != message.content_version
                or approval.content_sha256 != message.content_sha256
            ):
                return None
            if approval.expires_at and approval.expires_at <= datetime.utcnow():
                approval.status = "EXPIRED"
                return None

            claimed = session.execute(update(OutboundMessage).where(
                OutboundMessage.id == message_id, OutboundMessage.status == "APPROVED"
            ).values(status="SENDING"))
            if claimed.rowcount != 1:
                return None
            session.flush()
            session.refresh(message)
            return self._message_payload(message)

    def record_outbound_execution(self, execution: Any) -> dict[str, Any]:
        """Persist actual provider evidence and never infer delivery.

        The provider call happens outside the database transaction.  A graph
        retry can therefore submit the *same* result more than once.  Terminal
        rows are returned only when that result exactly matches the previously
        recorded outcome; they are never re-counted as another outreach attempt.
        """

        data = _dump(execution)
        message_id = _require_string(data.get("message_id"), "message_id")
        success = bool(data.get("success"))
        email_status = _require_string(data.get("email_status"), "email_status")
        provider_name = _require_string(data.get("provider_name"), "provider_name")
        provider_message_id = data.get("provider_message_id")
        provider_status = data.get("provider_status")
        detail = data.get("detail")

        if success:
            if email_status not in {"SENT", "DELIVERED"}:
                raise ValueError(
                    "A successful provider execution must be SENT or DELIVERED."
                )
            if not provider_message_id:
                raise ValueError(
                    "A successful provider execution needs provider_message_id."
                )
        elif email_status != "FAILED":
            raise ValueError("A failed provider execution must be FAILED.")

        with self._session() as session:
            message = session.scalar(
                select(OutboundMessage)
                .where(OutboundMessage.id == message_id)
                .with_for_update()
            )
            if message is None:
                raise ValueError("Outbound message was not found.")

            expected_terminal_status = email_status
            if message.status in {"SENT", "DELIVERED", "FAILED"}:
                # Idempotent replay: do not change timestamps, audit history,
                # relationship memory, or outreach_attempts.
                same_result = (
                    message.status == expected_terminal_status
                    and message.provider_name == provider_name
                    and message.provider_message_id == provider_message_id
                    and message.provider_status == provider_status
                    and message.last_error == (None if success else detail)
                )
                if same_result:
                    return self._message_payload(message)
                raise ValueError(
                    "A different provider result cannot overwrite a terminal "
                    "outbound message. Reconcile it manually."
                )

            if message.status != "SENDING":
                raise ValueError(
                    "Outbound execution can be recorded only for a claimed "
                    "SENDING message."
                )

            message.provider_name = provider_name
            message.provider_message_id = provider_message_id
            message.provider_status = provider_status

            if success:
                message.status = email_status
                message.sent_at = datetime.utcnow()
                if email_status == "DELIVERED":
                    message.delivered_at = datetime.utcnow()
                message.last_error = None

                relationship = session.scalar(
                    select(OutreachRelationship)
                    .where(OutreachRelationship.id == message.relationship_id)
                    .with_for_update()
                )
                if relationship is not None:
                    relationship.last_outbound_message_id = message.id
                    relationship.last_outbound_at = message.sent_at
                    if message.message_type in {
                        "INITIAL_OUTREACH",
                        "NO_RESPONSE_FOLLOW_UP",
                    }:
                        relationship.outreach_attempts += 1
                    relationship.version += 1
            else:
                message.status = "FAILED"
                message.last_error = detail

            self._record_event(
                session,
                restaurant_id=message.restaurant_id,
                event_type="OUTBOUND_EXECUTION",
                status=message.status,
                recipient_email=message.recipient_email,
                metadata={
                    "message_id": message.id,
                    "provider": message.provider_name,
                    "provider_message_id": message.provider_message_id,
                },
            )
            session.flush()
            return self._message_payload(message)

    def get_sending_message_for_reconciliation(
        self,
        *,
        message_id: str,
    ) -> dict[str, Any] | None:
        """Read an unresolved provider submission without changing its status.

        ``SENDING`` deliberately means the system has claimed the approved
        email but has no durable provider result yet.  Callers must query the
        real provider or resolve the issue with a human; this method never
        guesses that an email was delivered and never makes it sendable again.
        """

        with self._session() as session:
            message = session.get(OutboundMessage, message_id)
            if message is None or message.status != "SENDING":
                return None
            return self._message_payload(message)

    # ------------------------------------------------------------------
    # Signed button events and Strategy handoff gate
    # ------------------------------------------------------------------

    def create_button_confirmation(
        self,
        *,
        confirmation_id: str,
        email_token_sha256: str,
        outreach_message_id: str,
        expires_at: str | datetime,
    ) -> dict[str, Any]:
        """Store one short-lived server confirmation for a viewed email link.

        A GET request may create this record but cannot consume it. The raw email
        token is never written to SQLite; only its SHA-256 digest is retained.
        """

        confirmation_id = _require_string(confirmation_id, "confirmation_id")
        token_hash = _require_string(
            email_token_sha256, "email_token_sha256"
        )
        message_id = _require_string(
            outreach_message_id, "outreach_message_id"
        )
        expires = _to_datetime(expires_at)
        if expires is None or expires <= datetime.utcnow():
            raise ValueError("Confirmation expiry must be in the future.")

        with self._session() as session:
            message = session.get(OutboundMessage, message_id)
            if message is None or message.status not in {"SENT", "DELIVERED"}:
                raise ValueError(
                    "A confirmation may be created only for a real sent email."
                )
            row = EmailButtonConfirmation(
                id=confirmation_id,
                outreach_message_id=message_id,
                email_token_sha256=token_hash,
                expires_at=expires,
            )
            session.add(row)
            session.flush()
            return {
                "confirmation_id": row.id,
                "outreach_message_id": row.outreach_message_id,
                "expires_at": _iso(row.expires_at),
                "status": "ISSUED",
            }

    def consume_button_confirmation(
        self,
        *,
        confirmation_id: str,
        email_token_sha256: str,
        outreach_message_id: str,
    ) -> dict[str, Any]:
        """Atomically consume a confirmation so a form POST cannot be replayed."""

        with self._session() as session:
            row = session.scalar(
                select(EmailButtonConfirmation)
                .where(EmailButtonConfirmation.id == confirmation_id)
                .with_for_update()
            )
            if row is None:
                return {"consumed": False, "status": "NOT_FOUND"}
            if row.consumed_at is not None:
                return {"consumed": False, "status": "ALREADY_CONSUMED"}
            if row.expires_at <= datetime.utcnow():
                return {"consumed": False, "status": "EXPIRED"}
            if (
                row.email_token_sha256 != email_token_sha256
                or row.outreach_message_id != outreach_message_id
            ):
                return {"consumed": False, "status": "MISMATCH"}

            row.consumed_at = datetime.utcnow()
            session.flush()
            return {
                "consumed": True,
                "status": "CONSUMED",
                "outreach_message_id": row.outreach_message_id,
            }

    def record_button_event(self, *, event: Any) -> dict[str, Any]:
        """Store one verified click without allowing duplicate state changes."""

        data = _dump(event)
        event_id = _require_string(data.get("event_id"), "event_id")
        restaurant_id = _require_int(data.get("restaurant_id"), "restaurant_id")
        message_id = _require_string(
            data.get("outreach_message_id"),
            "outreach_message_id",
        )
        action = _require_string(data.get("action"), "action")
        if action not in {"INTERESTED", "NOT_INTERESTED"}:
            raise ValueError("Button action must be INTERESTED or NOT_INTERESTED.")

        try:
            with self._session() as session:
                message = session.get(OutboundMessage, message_id)
                if message is None or message.restaurant_id != restaurant_id:
                    raise ValueError(
                        "Button event does not belong to the supplied restaurant."
                    )
                if message.status not in {"SENT", "DELIVERED"}:
                    raise ValueError(
                        "A response button is accepted only after its email was "
                        "submitted to a real provider."
                    )
                if message.message_type not in {
                    "INITIAL_OUTREACH",
                    "NO_RESPONSE_FOLLOW_UP",
                }:
                    raise ValueError(
                        "Only prospect outreach emails accept offer buttons."
                    )

                relationship = session.scalar(
                    select(OutreachRelationship)
                    .where(OutreachRelationship.id == message.relationship_id)
                    .with_for_update()
                )
                if relationship is None:
                    raise ValueError("Outreach relationship was not found.")
                if (
                    action == "INTERESTED"
                    and relationship.status == "DO_NOT_CONTACT"
                ):
                    return {
                        "accepted": False,
                        "status": "DO_NOT_CONTACT_ALREADY_SET",
                    }

                existing = session.scalar(
                    select(ButtonResponseEvent).where(
                        ButtonResponseEvent.outreach_message_id == message_id
                    )
                )

                def apply_do_not_contact() -> None:
                    """Apply an opt-out once; never refresh it on a replay."""

                    already_suppressed = (
                        relationship.status == "DO_NOT_CONTACT"
                        and relationship.do_not_contact_at is not None
                        and relationship.next_contact_at is None
                        and relationship.contact_later_until is None
                    )
                    if already_suppressed:
                        return
                    relationship.status = "DO_NOT_CONTACT"
                    relationship.do_not_contact_at = (
                        relationship.do_not_contact_at or datetime.utcnow()
                    )
                    relationship.next_contact_at = None
                    relationship.contact_later_until = None
                    relationship.version += 1

                if existing is not None:
                    if existing.action == action:
                        if action == "NOT_INTERESTED":
                            # Repair an inconsistent historic row without
                            # treating a replay as another response.
                            apply_do_not_contact()
                        return {
                            "accepted": False,
                            "status": "DUPLICATE",
                            "canonical_event": self._button_event_payload(existing),
                        }
                    if action == "NOT_INTERESTED":
                        # An explicit opt-out wins even when an Interested
                        # click was already recorded for this same message.
                        apply_do_not_contact()
                        self._record_event(
                            session,
                            restaurant_id=restaurant_id,
                            event_type="BUTTON_RESPONSE_CONFLICT_SUPPRESSED",
                            status="DO_NOT_CONTACT",
                            recipient_email=message.recipient_email,
                            metadata={
                                "message_id": message_id,
                                "canonical_event_id": existing.id,
                                "new_action": action,
                                "reason": "Explicit opt-out always overrides promotion.",
                            },
                        )
                        session.flush()
                        return {
                            "accepted": True,
                            "status": "DO_NOT_CONTACT_APPLIED",
                            "canonical_event": self._button_event_payload(existing),
                        }
                    return {
                        "accepted": False,
                        "status": "DO_NOT_CONTACT_ALREADY_SET",
                        "canonical_event": self._button_event_payload(existing),
                    }

                # An explicit opt-out is safety-critical. Apply it durably in
                # this same transaction before returning a confirmation page;
                # the later LangGraph run only audits/continues the state flow.
                if action == "NOT_INTERESTED":
                    apply_do_not_contact()

                if action == "INTERESTED" and relationship.do_not_contact_at is None and relationship.status != "DO_NOT_CONTACT":
                    relationship.status = "INTERESTED"
                    relationship.next_contact_at = None
                    relationship.version += 1

                event_row = ButtonResponseEvent(
                    id=event_id,
                    restaurant_id=restaurant_id,
                    outreach_message_id=message_id,
                    token_id=_require_string(data.get("token_id"), "token_id"),
                    action=action,
                    status="RECEIVED",
                    idempotency_key=_require_string(
                        data.get("idempotency_key"),
                        "idempotency_key",
                    ),
                    source_ip_hash=data.get("source_ip_hash"),
                    clicked_at=_to_datetime(data.get("clicked_at"))
                    or datetime.utcnow(),
                )
                session.add(event_row)
                self._record_event(
                    session,
                    restaurant_id=restaurant_id,
                    event_type="BUTTON_RESPONSE_RECEIVED",
                    status="RECEIVED",
                    recipient_email=message.recipient_email,
                    metadata={
                        "event_id": event_id,
                        "message_id": message_id,
                        "action": action,
                    },
                )
                session.flush()
                return {
                    "accepted": True,
                    "status": "RECEIVED",
                    "event": self._button_event_payload(event_row),
                }
        except IntegrityError as error:
            # Do not claim a successful duplicate after an unknown DB failure.
            raise ValueError(
                "Button response could not be recorded safely. Please retry."
            ) from error

    def get_button_event(self, *, event_id: str) -> dict[str, Any] | None:
        with self._session() as session:
            event = session.get(ButtonResponseEvent, event_id)
            return self._button_event_payload(event) if event else None

    def list_pending_button_events(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Recover durable responses after a crash between recording and graph dispatch."""
        with self._session() as session:
            rows = session.scalars(select(ButtonResponseEvent).outerjoin(
                StrategyRequest, StrategyRequest.interest_event_id == ButtonResponseEvent.id
            ).where(or_(ButtonResponseEvent.status == "RECEIVED",
                (ButtonResponseEvent.action == "INTERESTED") &
                or_(StrategyRequest.id.is_(None), StrategyRequest.status == "CREATED")))
                .order_by(ButtonResponseEvent.clicked_at).limit(limit)).all()
            return [self._button_event_payload(row) for row in rows]

    def trial_feedback_due(self, *, restaurant_id: int, now: datetime) -> bool:
        with self._session() as session:
            trial = session.get(ClientTrial, restaurant_id)
            return bool(trial and trial.feedback_requested_at is None and trial.feedback_due_at <= _to_datetime(now))

    def mark_feedback_requested(self, *, restaurant_id: int, at: datetime) -> None:
        with self._session() as session:
            trial = session.get(ClientTrial, restaurant_id)
            if trial and trial.feedback_requested_at is None:
                trial.feedback_requested_at = _to_datetime(at)

    def mark_button_event_applied(self, *, event_id: str) -> dict[str, Any]:
        """Mark a received button event after its graph state update succeeds."""

        with self._session() as session:
            event = session.get(ButtonResponseEvent, event_id)
            if event is None:
                raise ValueError("Button event was not found.")
            if event.status == "APPLIED":
                return self._button_event_payload(event)
            if event.status != "RECEIVED":
                raise ValueError(
                    "Only a RECEIVED button event can be applied."
                )

            event.status = "APPLIED"
            event.processed_at = datetime.utcnow()
            session.flush()
            return self._button_event_payload(event)

    def create_strategy_request_if_absent(
        self,
        handoff: Any,
    ) -> dict[str, Any]:
        """Create one Strategy request only after an APPLIED Interested event."""

        data = _dump(handoff)
        request_id = _require_string(
            data.get("strategy_request_id"),
            "strategy_request_id",
        )
        interest_event_id = _require_string(
            data.get("interest_event_id"),
            "interest_event_id",
        )

        with self._session() as session:
            event = session.get(ButtonResponseEvent, interest_event_id)
            if event is None:
                raise ValueError("Interest event was not found.")
            if event.action != "INTERESTED" or event.status != "APPLIED":
                raise ValueError(
                    "Strategy request requires an APPLIED Interested event."
                )

            existing = session.scalar(
                select(StrategyRequest).where(
                    StrategyRequest.interest_event_id == interest_event_id
                )
            )
            if existing is not None:
                payload = self._strategy_request_payload(existing)
                payload["created"] = False
                return payload

            relationship = session.scalar(
                select(OutreachRelationship)
                .where(OutreachRelationship.restaurant_id == event.restaurant_id)
                .with_for_update()
            )
            if relationship is None:
                raise ValueError("Outreach relationship was not found.")
            if (
                relationship.status == "DO_NOT_CONTACT"
                or relationship.do_not_contact_at is not None
            ):
                raise ValueError(
                    "A Strategy request cannot be created after a recorded opt-out."
                )

            if str(data.get("outreach_message_id")) != event.outreach_message_id:
                raise ValueError(
                    "Strategy request message ID does not match interest event."
                )

            request = StrategyRequest(
                id=request_id,
                relationship_id=relationship.id,
                restaurant_id=event.restaurant_id,
                research_run_id=_require_int(
                    data.get("research_run_id"),
                    "research_run_id",
                ),
                qualification_run_id=_require_int(
                    data.get("qualification_run_id"),
                    "qualification_run_id",
                ),
                outreach_message_id=event.outreach_message_id,
                interest_event_id=event.id,
                customer_request=data.get("customer_request"),
                research_context_json=data.get("research_context") or {},
                qualification_context_json=data.get(
                    "qualification_context"
                )
                or {},
                customer_context_json=data.get("customer_context") or {},
                status="CREATED",
            )
            session.add(request)
            relationship.strategy_request_id = request.id
            relationship.status = "AWAITING_STRATEGY_OUTPUT"
            relationship.version += 1
            self._record_event(
                session,
                restaurant_id=event.restaurant_id,
                event_type="STRATEGY_REQUEST_CREATED",
                status="CREATED",
                metadata={
                    "strategy_request_id": request.id,
                    "interest_event_id": event.id,
                },
            )
            session.flush()

            payload = self._strategy_request_payload(request)
            payload["created"] = True
            return payload

    def load_strategy_request(
        self,
        *,
        strategy_request_id: str,
    ) -> dict[str, Any] | None:
        with self._session() as session:
            request = session.get(StrategyRequest, strategy_request_id)
            return self._strategy_request_payload(request) if request else None

    def record_strategy_dispatch(
        self,
        *,
        strategy_request_id: str,
        dispatch: dict[str, Any],
    ) -> dict[str, Any]:
        """Persist confirmed outbox dispatch metadata without creating strategy.

        The local file dispatcher is responsible only for a durable request
        outbox.  This method records that narrow fact after it returns, so the
        relationship can distinguish ``CREATED`` from ``HANDED_OFF`` without
        pretending that Strategy work is complete.
        """

        request_id = _require_string(strategy_request_id, "strategy_request_id")
        if not isinstance(dispatch, dict):
            raise ValueError("dispatch must be a dictionary.")
        dispatch_status = _require_string(dispatch.get("status"), "dispatch.status")

        with self._session() as session:
            request = session.get(StrategyRequest, request_id)
            if request is None:
                raise ValueError("Strategy request was not found.")
            if request.status in {"READY", "REJECTED"}:
                return self._strategy_request_payload(request)
            request.status = "HANDED_OFF"
            request.dispatch_metadata_json = {
                "status": dispatch_status,
                "strategy_request_id": dispatch.get("strategy_request_id"),
            }
            self._record_event(
                session,
                restaurant_id=request.restaurant_id,
                event_type="STRATEGY_REQUEST_DISPATCHED",
                status="HANDED_OFF",
                metadata={"strategy_request_id": request.id, "dispatch_status": dispatch_status},
            )
            session.flush()
            return self._strategy_request_payload(request)

    def load_strategy_handoff(
        self,
        *,
        strategy_request_id: str,
    ) -> dict[str, Any] | None:
        """Return only a stored Strategy output; None means it has not arrived."""

        with self._session() as session:
            request = session.get(StrategyRequest, strategy_request_id)
            if request is None or request.strategy_output_json is None:
                return None
            return dict(request.strategy_output_json)

    def record_strategy_output(
        self,
        *,
        strategy_request_id: str,
        strategy_output: dict[str, Any],
    ) -> dict[str, Any]:
        """Store a validated Strategy Agent result for later client notification."""

        with self._session() as session:
            request = session.get(StrategyRequest, strategy_request_id)
            if request is None:
                raise ValueError("Strategy request was not found.")

            if str(strategy_output.get("restaurant_id")) != str(
                request.restaurant_id
            ):
                raise ValueError(
                    "Strategy output restaurant_id does not match request."
                )
            if strategy_output.get("strategy_request_id") != request.id:
                raise ValueError(
                    "Strategy output request ID does not match request."
                )

            if request.strategy_output_json == strategy_output and request.status == "READY":
                return self._strategy_request_payload(request)
            request.strategy_id = _require_int(
                strategy_output.get("strategy_id"),
                "strategy_id",
            )
            request.strategy_output_json = dict(strategy_output)
            request.dashboard_strategy_url = strategy_output.get(
                "dashboard_strategy_url"
            )
            request.client_notification_allowed = bool(
                strategy_output.get("client_notification_allowed")
            )
            request.status = str(strategy_output.get("status", "READY"))
            request.completed_at = datetime.utcnow()

            relationship = session.scalar(
                select(OutreachRelationship)
                .where(OutreachRelationship.id == request.relationship_id)
                .with_for_update()
            )
            ready_for_notification = (
                request.status == "READY"
                and bool(request.dashboard_strategy_url)
                and request.client_notification_allowed
            )
            if (
                relationship is not None
                and ready_for_notification
                and relationship.status != "DO_NOT_CONTACT"
                and relationship.do_not_contact_at is None
            ):
                previous_memory = relationship.memory_json or {}
                changed = (
                    relationship.strategy_id != request.strategy_id
                    or relationship.status != "STRATEGY_READY"
                    or previous_memory.get("strategy_status") != request.status
                )
                relationship.strategy_id = request.strategy_id
                relationship.status = "STRATEGY_READY"
                memory = dict(previous_memory)
                memory["strategy_status"] = request.status
                relationship.memory_json = memory
                if changed:
                    relationship.version += 1

            session.flush()
            return self._strategy_request_payload(request)

    # ------------------------------------------------------------------
    # Human escalation and generic audit timeline
    # ------------------------------------------------------------------

    def create_human_escalation(self, escalation: Any) -> dict[str, Any]:
        """Create a human-owned escalation and move the relationship safely."""

        data = _dump(escalation)
        restaurant_id = _require_int(data.get("restaurant_id"), "restaurant_id")

        with self._session() as session:
            relationship = session.scalar(
                select(OutreachRelationship)
                .where(OutreachRelationship.restaurant_id == restaurant_id)
                .with_for_update()
            )
            if relationship is None:
                raise ValueError("Outreach relationship was not found.")

            existing = session.get(HumanEscalationRecord, data["escalation_id"])
            if existing:
                return {"escalation_id": existing.id, "restaurant_id": existing.restaurant_id, "status": existing.status}
            relationship.next_contact_at = None
            record = HumanEscalationRecord(
                id=_require_string(data.get("escalation_id"), "escalation_id"),
                relationship_id=relationship.id,
                restaurant_id=restaurant_id,
                related_issue_id=data.get("related_issue_id"),
                reason_code=_require_string(
                    data.get("reason_code"),
                    "reason_code",
                ),
                reason=_require_string(data.get("reason"), "reason"),
                severity=_require_string(data.get("severity"), "severity"),
                status=_require_string(data.get("status"), "status"),
            )
            session.add(record)
            relationship.status = "ESCALATED_TO_HUMAN"
            relationship.version += 1
            self._record_event(
                session,
                restaurant_id=restaurant_id,
                event_type="HUMAN_ESCALATION_CREATED",
                status=record.status,
                metadata={"escalation_id": record.id},
            )
            session.flush()

            return {
                "escalation_id": record.id,
                "restaurant_id": record.restaurant_id,
                "status": record.status,
                "severity": record.severity,
                "created_at": _iso(record.created_at),
            }

    @staticmethod
    def _record_event(
        session: Session,
        *,
        restaurant_id: int,
        event_type: str,
        status: str,
        recipient_email: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Append a lightweight audit item to the team's existing OutreachEvent."""

        session.add(
            OutreachEvent(
                restaurant_id=restaurant_id,
                event_type=event_type,
                status=status,
                recipient_email=recipient_email,
                metadata_json=metadata,
            )
        )


__all__ = ["OutreachRepository"]
