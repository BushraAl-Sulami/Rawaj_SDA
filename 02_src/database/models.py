from datetime import datetime
from typing import Optional, Any

from sqlalchemy import (
    String,
    Text,
    DateTime,
    ForeignKey,
    Integer,
    Boolean,
    JSON,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from database.database import Base


class Restaurant(Base):
    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    instagram_username: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
    )

    instagram_url: Mapped[Optional[str]] = mapped_column(
        String(500),
        nullable=True,
    )

    email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    location: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    research_runs = relationship(
        "ResearchRun",
        back_populates="restaurant",
        cascade="all, delete-orphan",
    )

    qualification_runs = relationship(
        "QualificationRun",
        back_populates="restaurant",
        cascade="all, delete-orphan",
    )

    strategies = relationship(
        "Strategy",
        back_populates="restaurant",
        cascade="all, delete-orphan",
    )

    context_record = relationship(
        "RestaurantContext",
        back_populates="restaurant",
        uselist=False,
        cascade="all, delete-orphan",
    )

    analysis_jobs = relationship(
        "AnalysisJob",
        back_populates="restaurant",
        foreign_keys="[AnalysisJob.restaurant_id]",
        cascade="all, delete-orphan",
    )

    active_analysis_jobs = relationship(
        "AnalysisJob",
        back_populates="active_restaurant",
        foreign_keys="[AnalysisJob.active_restaurant_id]",
        cascade="all, delete-orphan",
    )


class RestaurantContext(Base):
    __tablename__ = "restaurant_contexts"

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        primary_key=True,
    )

    data: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    restaurant = relationship(
        "Restaurant",
        back_populates="context_record",
    )


class AnalysisJob(Base):
    __tablename__ = "analysis_jobs"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    active_restaurant_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(20),
        default="queued",
        index=True,
    )

    content_limit: Mapped[int] = mapped_column(
        Integer,
        default=30,
    )

    lookback_days: Mapped[int] = mapped_column(
        Integer,
        default=90,
    )

    force_refresh: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    context: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )

    research_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_runs.id"),
        nullable=True,
        index=True,
    )

    qualification_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("qualification_runs.id"),
        nullable=True,
        index=True,
    )

    error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    started_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    finished_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    restaurant = relationship(
        "Restaurant",
        back_populates="analysis_jobs",
        foreign_keys=[restaurant_id],
    )

    active_restaurant = relationship(
        "Restaurant",
        back_populates="active_analysis_jobs",
        foreign_keys=[active_restaurant_id],
    )


class ResearchRun(Base):
    __tablename__ = "research_runs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    # -------------------------
    # Research run metadata
    # -------------------------

    schema_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    analysis_version: Mapped[Optional[str]] = mapped_column(
        String(50),
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        default="completed",
    )

    source: Mapped[str] = mapped_column(
        String(50),
        default="live",
    )

    analyzed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    content_limit: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    lookback_days: Mapped[Optional[int]] = mapped_column(
        Integer,
        nullable=True,
    )

    # -------------------------
    # Research Agent output
    # -------------------------

    profile: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    profile_analysis: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    metrics: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    research_signals: Mapped[Optional[list[Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    content: Mapped[Optional[list[Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    analysis_coverage: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    data_quality: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    # Complete original Research Agent output
    full_result: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    # -------------------------
    # Error / system metadata
    # -------------------------

    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    restaurant = relationship(
        "Restaurant",
        back_populates="research_runs",
    )


class QualificationRun(Base):
    __tablename__ = "qualification_runs"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    # Restaurant being qualified
    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    # Exact ResearchRun used as evidence
    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("research_runs.id"),
        nullable=False,
        index=True,
    )

    # -------------------------
    # Qualification run metadata
    # -------------------------

    status: Mapped[str] = mapped_column(
        String(50),
        default="completed",
    )

    agent: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    # -------------------------
    # Qualification Agent output
    # -------------------------

    qualification: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    decision_rationale: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    marketing_gaps: Mapped[Optional[list[Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    strengths: Mapped[Optional[list[Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    data_limitations: Mapped[Optional[list[Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    # Complete original Qualification Agent output
    full_result: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    # -------------------------
    # Error / system metadata
    # -------------------------

    error_message: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    restaurant = relationship(
        "Restaurant",
        back_populates="qualification_runs",
    )

class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    restaurant_name: Mapped[Optional[str]] = mapped_column(
    String(255),
    nullable=True,
   )


    qualification_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("qualification_runs.id"),
        nullable=True,
    )

    strategy_data: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    pdf_path: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )

    approved: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    restaurant = relationship(
        "Restaurant",
        back_populates="strategies",
    )


class OutreachEvent(Base):
    __tablename__ = "outreach_events"

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    strategy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=True,
    )

    event_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    recipient_email: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    next_followup_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    restaurant = relationship(
        "Restaurant",
    )


class OutreachRelationship(Base):
    """Small persistent relationship memory for one restaurant, not a full CRM."""

    __tablename__ = "outreach_relationships"
    __table_args__ = (
        UniqueConstraint(
            "restaurant_id",
            name="uq_outreach_relationship_restaurant",
        ),
    )

    id: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    # Exact upstream provenance. These stay nullable only for safe migration of
    # existing restaurant rows that have not yet entered Outreach.
    research_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_runs.id"),
        nullable=True,
        index=True,
    )

    qualification_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("qualification_runs.id"),
        nullable=True,
        index=True,
    )

    strategy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=True,
        index=True,
    )

    status: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        default="READY_TO_CONTACT",
        index=True,
    )

    # Increment only after the email provider actually accepts a promotional
    # outreach email; drafts and rejected approvals do not count.
    outreach_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )

    last_outbound_message_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    last_outbound_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    last_inbound_event_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    last_inbound_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    next_contact_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
        index=True,
    )

    contact_later_until: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    interest_event_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    strategy_request_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
        index=True,
    )

    do_not_contact_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    # Feedback, issues, and small relationship notes live here. It is not a CRM.
    memory_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    # Used by the repository for optimistic locking.
    version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    restaurant = relationship("Restaurant")
    strategy = relationship("Strategy")


class OutboundMessage(Base):
    """An immutable generated email version and its real provider lifecycle."""

    __tablename__ = "outbound_messages"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    relationship_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_relationships.id"),
        nullable=False,
        index=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    research_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("research_runs.id"),
        nullable=True,
        index=True,
    )

    qualification_run_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("qualification_runs.id"),
        nullable=True,
        index=True,
    )

    # A later edited draft gets a new message ID and points to this predecessor.
    supersedes_message_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("outbound_messages.id"),
        nullable=True,
    )

    message_type: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    action: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    recipient_email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    subject: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
    )

    html_body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    plain_text_body: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    language: Mapped[str] = mapped_column(
        String(8),
        nullable=False,
        default="en",
    )

    personalization_context_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    content_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
    )

    # Hash of recipient + subject + body + buttons. Approval binds to this value.
    content_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="GENERATED",
        index=True,
    )

    provider_name: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    provider_message_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
        unique=True,
    )

    provider_status: Mapped[Optional[str]] = mapped_column(
        String(100),
        nullable=True,
    )

    last_error: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    approved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    sent_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    delivered_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    relationship_state = relationship("OutreachRelationship")
    restaurant = relationship("Restaurant")


class OutreachApproval(Base):
    """A Human Approval bound to one exact immutable message content hash."""

    __tablename__ = "outreach_approvals"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    outreach_message_id: Mapped[str] = mapped_column(
        ForeignKey("outbound_messages.id"),
        nullable=False,
        index=True,
    )

    content_version: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    content_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="PENDING",
        index=True,
    )

    reviewer_id: Mapped[Optional[str]] = mapped_column(
        String(255),
        nullable=True,
    )

    requested_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    decided_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    note: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    outbound_message = relationship("OutboundMessage")


class ButtonResponseEvent(Base):
    """A verified Interested or Not Interested click from a real email button."""

    __tablename__ = "button_response_events"
    __table_args__ = (
        # One message has one canonical response. A later Not Interested click
        # remains audit-visible through OutreachEvent and always applies global
        # promotional suppression in the repository.
        UniqueConstraint(
            "outreach_message_id",
            name="uq_button_response_one_response_per_message",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    outreach_message_id: Mapped[str] = mapped_column(
        ForeignKey("outbound_messages.id"),
        nullable=False,
        index=True,
    )

    token_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
    )

    action: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="RECEIVED",
        index=True,
    )

    idempotency_key: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        unique=True,
    )

    source_ip_hash: Mapped[Optional[str]] = mapped_column(
        String(128),
        nullable=True,
    )

    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    clicked_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    processed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    restaurant = relationship("Restaurant")
    outbound_message = relationship("OutboundMessage")


class EmailButtonConfirmation(Base):
    """One server-issued confirmation page for a signed email button link."""

    __tablename__ = "email_button_confirmations"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    outreach_message_id: Mapped[str] = mapped_column(
        ForeignKey("outbound_messages.id"),
        nullable=False,
        index=True,
    )

    # The raw email button token is never persisted.
    email_token_sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        index=True,
    )

    consumed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    outbound_message = relationship("OutboundMessage")


class StrategyRequest(Base):
    """The idempotent handoff record created after a verified Interested event."""

    __tablename__ = "strategy_requests"
    __table_args__ = (
        UniqueConstraint(
            "interest_event_id",
            name="uq_strategy_request_interest_event",
        ),
    )

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    relationship_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_relationships.id"),
        nullable=False,
        index=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    research_run_id: Mapped[int] = mapped_column(
        ForeignKey("research_runs.id"),
        nullable=False,
        index=True,
    )

    qualification_run_id: Mapped[int] = mapped_column(
        ForeignKey("qualification_runs.id"),
        nullable=False,
        index=True,
    )

    outreach_message_id: Mapped[str] = mapped_column(
        ForeignKey("outbound_messages.id"),
        nullable=False,
        index=True,
    )

    interest_event_id: Mapped[str] = mapped_column(
        ForeignKey("button_response_events.id"),
        nullable=False,
        index=True,
    )

    customer_request: Mapped[Optional[str]] = mapped_column(
        Text,
        nullable=True,
    )

    research_context_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    qualification_context_json: Mapped[dict[str, Any]] = mapped_column(
        JSON,
        nullable=False,
    )

    customer_context_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="CREATED",
        index=True,
    )

    dispatch_metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    strategy_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("strategies.id"),
        nullable=True,
        index=True,
    )

    strategy_output_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    dashboard_strategy_url: Mapped[Optional[str]] = mapped_column(
        String(1000),
        nullable=True,
    )

    client_notification_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    completed_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    relationship_state = relationship("OutreachRelationship")
    restaurant = relationship("Restaurant")
    strategy = relationship("Strategy")


class HumanEscalationRecord(Base):
    """Persistent human-owned review item for sensitive communication issues."""

    __tablename__ = "human_escalations"

    id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
    )

    relationship_id: Mapped[int] = mapped_column(
        ForeignKey("outreach_relationships.id"),
        nullable=False,
        index=True,
    )

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        index=True,
    )

    related_issue_id: Mapped[Optional[str]] = mapped_column(
        String(64),
        nullable=True,
    )

    reason_code: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )

    reason: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )

    severity: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="OPEN",
        index=True,
    )

    metadata_json: Mapped[Optional[dict[str, Any]]] = mapped_column(
        JSON,
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
    )

    acknowledged_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    resolved_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime,
        nullable=True,
    )

    relationship_state = relationship("OutreachRelationship")
    restaurant = relationship("Restaurant")


class UserAccount(Base):
    """A restaurant owner's sign-in for the Rawaj dashboard (created when the restaurant becomes a client)."""

    __tablename__ = "user_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    restaurant_id: Mapped[int] = mapped_column(
        ForeignKey("restaurants.id"),
        nullable=False,
        unique=True,
    )

    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)

    # "scrypt$<salt hex>$<hash hex>"; the password itself is never stored (it is re-derivable, see api/accounts.py).
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)

    failed_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ClientTrial(Base):
    """One immutable activation window per restaurant; email delivery does not start it."""
    __tablename__ = "client_trials"
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"), primary_key=True)
    activated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    feedback_due_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    feedback_requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class ClientFeedbackRecord(Base):
    __tablename__ = "client_feedback"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    restaurant_id: Mapped[int] = mapped_column(ForeignKey("restaurants.id"), nullable=False, index=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    rating: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), nullable=False, default="dashboard")
    received_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
