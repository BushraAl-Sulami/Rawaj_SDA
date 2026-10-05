"""Validated contracts shared by the monthly planner and content generation."""

from datetime import date
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator, model_validator

from agents.strategy_agent.content_ideas import ContentType, Idea, IdeaResponse, ShortText
from api.schemas import InputModel


def valid_month(value: str) -> str:
    date.fromisoformat(f"{value}-01")
    return value


Month = Annotated[str, Field(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"), AfterValidator(valid_month)]
TaskStatus = Literal["Planned", "Completed"]


class StrategyRequest(InputModel):
    month: Month
    regenerate: bool = False


class StrategyTask(BaseModel):
    id: str
    date: str
    type: ContentType
    title: str
    objective: str
    status: TaskStatus = "Planned"
    saved_idea: Idea | None = None


class StrategyPillar(BaseModel):
    title: str
    description: str
    metric: str


class StrategyResponse(BaseModel):
    id: int
    restaurant_id: int
    month: Month
    restaurant_name: str
    business_type: Literal["cafe", "restaurant"]
    goal: str
    summary: str
    focus: str
    pillars: list[StrategyPillar]
    tasks: list[StrategyTask]
    occasions: list[dict] = Field(default_factory=list)
    context_signature: str
    generation_method: Literal["context_template"] = "context_template"


class TaskUpdate(InputModel):
    month: Month
    status: TaskStatus | None = None
    saved_idea: Idea | None = None

    @field_validator("status")
    @classmethod
    def non_null_status(cls, value):
        if value is None:
            raise ValueError("Status cannot be null")
        return value

    @model_validator(mode="after")
    def require_change(self):
        if not ({"status", "saved_idea"} & self.model_fields_set):
            raise ValueError("Provide a status or a saved idea")
        return self


class TaskCreate(InputModel):
    month: Month
    date: Annotated[str, Field(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    title: ShortText
    type: ContentType
    objective: Annotated[str, Field(min_length=1, max_length=2000)]

    @model_validator(mode="after")
    def date_matches_month(self):
        date.fromisoformat(self.date)
        if self.date[:7] != self.month:
            raise ValueError("Task date must be inside the selected month")
        return self


class IdeaRequest(InputModel):
    month: Month
    task_id: Annotated[str, Field(min_length=1, max_length=100)]
    previous_ideas: list[Idea] = Field(default_factory=list, max_length=30)
    feedback: Annotated[str, Field(max_length=2000)] = ""
