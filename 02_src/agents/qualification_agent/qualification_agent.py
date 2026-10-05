import os

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from langchain.agents import create_agent

from agents.qualification_agent.guardrails import guarded_benchmark_tool
from agents.qualification_agent.schemas import Report
from agents.qualification_agent.prompt import (
    QUALIFICATION_PROMPT,
    build_qualification_message,
)


load_dotenv()


def get_llm() -> ChatOpenAI:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("OPENAI_API_KEY is missing from the environment.")

    return ChatOpenAI(
        model=os.getenv("OPENAI_MODEL", "gpt-5.6-luna"),
        use_responses_api=True,
        api_key=api_key,
    )


def run_qualification_agent(
    evidence: dict,
    *, audit: dict | None = None,
) -> dict:
    run_audit = audit if audit is not None else {}
    run_audit.update({"tool_calls": []})
    # The benchmark search tool, wrapped with a per-run call limit
    tools = [
        guarded_benchmark_tool(audit=run_audit["tool_calls"])
    ]

    llm = get_llm()

    # Same agent structure
    agent = create_agent(
        model=llm,
        tools=tools,
        system_prompt=QUALIFICATION_PROMPT
    )

    # Run qualification
    result = agent.invoke({
        "messages": [
            {
                "role": "user",
                "content": build_qualification_message(evidence)
            }
        ]
    }, config={"recursion_limit": 16})

    # Get the final plain-text qualification report
    final_report = result["messages"][-1].text
    run_audit["model_calls"] = sum(getattr(message, "type", None) == "ai" for message in result["messages"])
    run_audit["usage"] = [message.usage_metadata for message in result["messages"]
                          if getattr(message, "usage_metadata", None)]

    # Same structured-output conversion from notebook
    structured_llm = llm.with_structured_output(
        Report
    )

    structured_result = structured_llm.invoke(f"""
Convert the following qualification report into structured JSON.

Rules:
- Extract information only from the report.
- Do not invent or add information.
- Preserve the qualification decision and full rationale.
- Preserve every marketing gap separately.
- For EACH marketing gap, extract:
  - gap
  - description
  - status
  - severity
  - priority
  - confidence
  - evidence
  - evidence_source
  - benchmark_evidence
  - rationale
  - data_limitations
  - recommendation_focus

IMPORTANT:
- Include in marketing_gaps only gaps with status Confirmed. Findings that are
  Not Observed, Not Available, or Uncertain belong in data_limitations.
- Set qualification to exactly one of: Qualified, Needs More Evidence,
  Not Qualified.
- Set severity to exactly one of: High, Moderate, Low. If the report says
  Medium, write Moderate.
- recommendation_focus is REQUIRED for every marketing gap. It is the
  marketing area the gap affects (for example "Posting consistency"), taken
  from the gap description in the report. Do not write a solution or strategy.
  If the report gives no focus, use the gap name.
- Preserve all evidence and limitations.
- Preserve exact research metric paths or signal ids alongside numerical evidence.
- Write evidence as plain-language observations, retaining exact metric paths in
  source notes such as "(Metric path: metrics.activity.content_per_week)".
  Do not replace an observation with a raw field assignment. Preserve all values,
  sample scope, and uncertainty; unavailable data is not zero or an absent feature.
- Keep external numerical comparisons in benchmark_evidence, with the retrieved source URL.
- If no benchmark was used, return an empty benchmark_evidence list.
- If any other field is genuinely missing from the report,
  write "Not Available" instead of inventing information.

Qualification Report:
{final_report}
""")

    output = structured_result.model_dump()

    # Correct location of restaurant_id for the new handoff
    output["restaurant_id"] = (
        evidence
        .get("restaurant", {})
        .get("restaurant_id")
    )

    output["agent"] = (
        "Qualification & Marketing Gap Analysis Agent"
    )

    run_audit["model_calls"] += 1
    run_audit["usage_scope"] = "agent messages only; excludes structured extraction"
    return output
