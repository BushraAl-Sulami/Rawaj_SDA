"""The Strategy Agent's output contract (mirrors "FINAL ANSWER FORMAT" in prompt.py)."""

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field


def _as_text(value):
    """The prompt allows a number or text for a highlight; keep it as text."""
    return None if value is None else str(value)


OptionalText = Annotated[str | None, BeforeValidator(_as_text)]


class _Strict(BaseModel):
    # "Do not add fields outside the required JSON structure."
    model_config = ConfigDict(extra="forbid")


class PrimaryGap(_Strict):
    gap: str = Field(min_length=1)
    severity: Literal["High", "Moderate"]
    highlight: OptionalText = None
    highlight_label: OptionalText = None
    key_point: str = Field(min_length=1)


class RecommendedService(_Strict):
    service: str = Field(min_length=1)
    why_this_service_fits: str = Field(min_length=1)


class PlanDay(_Strict):
    day: int
    focus: str = Field(min_length=1)
    action: str = Field(min_length=1)


class TrendSupport(_Strict):
    insight: str = Field(min_length=1)
    source_url: str = Field(min_length=1)


class StrategyOutput(_Strict):
    restaurant: str = Field(min_length=1)
    primary_marketing_gaps: list[PrimaryGap]
    thirty_day_target: list[str] = Field(min_length=1, max_length=3)
    recommended_services: list[RecommendedService]
    thirty_day_plan: list[PlanDay]
    external_trend_support: list[TrendSupport] = Field(default_factory=list)


class StrategyReflection(_Strict):
    passed: bool
    issues: list[str]
    corrected_strategy: StrategyOutput
