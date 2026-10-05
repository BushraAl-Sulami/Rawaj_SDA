from datetime import datetime, timezone
from typing import Annotated, Any, Literal

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    field_serializer,
    field_validator,
)


def normalize_username(value: Any) -> Any:
    return value.strip().removeprefix("@").lower() if isinstance(value, str) else value


def validate_email(value: str) -> str:
    # These addresses are contact details; actual sending belongs to outreach.
    import re

    if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", value):
        raise ValueError("Enter a valid email address")
    return value


Name = Annotated[str, Field(min_length=1, max_length=255)]
Email = Annotated[str, Field(min_length=3, max_length=255), AfterValidator(validate_email)]
Username = Annotated[
    str,
    Field(min_length=1, max_length=30, pattern=r"^[a-z0-9._]+$"),
    BeforeValidator(normalize_username),
]


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class RestaurantCreate(InputModel):
    name: Name
    instagram_username: Username
    email: Email | None = None
    location: Annotated[str, Field(max_length=255)] | None = None
    context: dict[str, JsonValue] = Field(default_factory=dict)


class RestaurantUpdate(InputModel):
    name: Name | None = None
    email: Email | None = None
    location: Annotated[str, Field(max_length=255)] | None = None
    is_active: bool | None = None
    context: dict[str, JsonValue] | None = None

    @field_validator("name", "is_active", "context")
    @classmethod
    def reject_explicit_null(cls, value: Any) -> Any:
        if value is None:
            raise ValueError("This field cannot be null")
        return value


class OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_serializer("created_at", "updated_at", "started_at", "finished_at", check_fields=False)
    def serialize_utc(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return value.replace(tzinfo=timezone.utc).isoformat() if value.tzinfo is None else value.isoformat()


class RestaurantResponse(OutputModel):
    id: int
    name: str
    instagram_username: str
    instagram_url: str | None
    email: str | None
    location: str | None
    is_active: bool
    created_at: datetime
    updated_at: datetime
    context: dict[str, JsonValue] = Field(default_factory=dict)


class RestaurantList(BaseModel):
    items: list[RestaurantResponse]
    total: int
    offset: int
    limit: int


class AnalyzeRequest(InputModel):
    content_limit: int = Field(default=30, ge=1, le=100)
    lookback_days: int = Field(default=90, ge=1, le=365)
    force_refresh: bool = False


class JobResponse(OutputModel):
    id: str
    restaurant_id: int
    status: Literal["queued", "running", "completed", "failed"]
    content_limit: int
    lookback_days: int
    force_refresh: bool
    research_run_id: int | None
    qualification_run_id: int | None
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class ResearchResponse(OutputModel):
    id: int
    status: str
    created_at: datetime
    result: dict[str, Any]


class QualificationResponse(ResearchResponse):
    research_run_id: int


class ContextResponse(BaseModel):
    restaurant: RestaurantResponse
    research: ResearchResponse | None
    qualification: QualificationResponse | None
    latest_job: JobResponse | None


class GapItem(BaseModel):
    gap: str
    severity: str
    priority: int | None = None
    evidence: list[str] = Field(default_factory=list)
    recommendation_focus: str = ""
    description: str = ""  # description, rationale and confidence exist in gaps saved by the current Qualification Agent
    rationale: str = ""
    confidence: str = ""


class GapCounts(BaseModel):
    total: int = 0
    high: int = 0
    moderate: int = 0
    low: int = 0
    strengths: int = 0
    data_limitations: int = 0


class GapsResponse(OutputModel):
    restaurant_id: int
    restaurant_name: str
    qualification_id: int | None
    created_at: datetime | None
    counts: GapCounts
    gaps: list[GapItem]
    strengths: list[str]
    data_limitations: list[str]


class OutreachDraft(BaseModel):
    subject: str
    body: str
    message_type: str = "initial"
