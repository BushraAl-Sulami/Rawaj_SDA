"""Small bridge from the team pipeline into the Outreach & Follow-Up Agent.

Copy this file into orchestration/outreach_node.py in the shared project.
It does not replace the team's workflow and it does not create a Strategy.
It simply passes the immutable IDs already produced by Research and
Qualification to the Outreach Agent's own LangGraph workflow.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping


def outreach_node(
    state: Mapping[str, Any],
    *,
    outreach_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Start the outreach graph after the exact Qualification run is available.

    The target agent reloads the canonical Research and Qualification records
    from the shared database. It therefore cannot be started with a detached
    restaurant name or an ad-hoc notebook payload.
    """

    restaurant_id = state.get("restaurant_id")
    research_run_id = state.get("research_run_id")
    qualification_run_id = state.get("qualification_run_id")

    if any(
        value is None
        for value in (restaurant_id, research_run_id, qualification_run_id)
    ):
        return {
            "error": (
                "Outreach requires restaurant_id, research_run_id, "
                "and qualification_run_id."
            ),
            "next": "end",
        }

    try:
        # Import lazily so the existing Research / Qualification-only workflow
        # can still be imported before this component's optional live settings
        # are configured.
        if outreach_runner is None:
            from agents.outreach_followup_agent.agent import (
                run_outreach_after_qualification,
            )

            outreach_runner = run_outreach_after_qualification

        result = outreach_runner(
            restaurant_id=int(restaurant_id),
            research_run_id=int(research_run_id),
            qualification_run_id=int(qualification_run_id),
        )
        return {
            "outreach_thread_id": result.get("outreach_thread_id"),
            "outreach_status": result.get("outreach_status"),
            "outreach_action": result.get("outreach_action"),
            "outreach_message_id": result.get("outreach_message_id"),
            "outreach_pending_human_approval": result.get(
                "outreach_pending_human_approval", False
            ),
            "outreach_errors": result.get("outreach_errors", []),
            "next": "end",
        }
    except Exception as error:
        # The outer workflow receives a truthful error instead of a fictional
        # email status. The persisted Outreach memory, if any, remains the
        # source of truth for a later retry or human review.
        return {
            "error": f"Outreach Agent could not start: {error}",
            "next": "end",
        }


__all__ = ["outreach_node"]
