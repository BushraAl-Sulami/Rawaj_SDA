from pydantic import BaseModel, Field
from typing import List, Literal


class Gap(BaseModel):
    gap: str
    description: str = ""
    status: Literal["Confirmed"]
    severity: Literal["High", "Moderate", "Low"]
    priority: int = Field(ge=1)
    confidence: str = ""

    evidence: List[str] = Field(default_factory=list)
    evidence_source: List[str] = Field(default_factory=list)
    benchmark_evidence: List[str] = Field(default_factory=list)

    rationale: str = ""
    data_limitations: List[str] = Field(default_factory=list)

    # Important for the Strategy Agent later
    recommendation_focus: str = ""


class Report(BaseModel):
    restaurant: str
    qualification: Literal["Qualified", "Needs More Evidence", "Not Qualified"]
    decision_rationale: str
    qualification_confidence: str = ""

    marketing_gaps: List[Gap] = Field(default_factory=list)

    strengths: List[str] = Field(default_factory=list)
    data_limitations: List[str] = Field(default_factory=list)
