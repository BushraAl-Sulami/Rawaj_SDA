"""Database helpers and deferred agent calls for the HTTP application."""

import asyncio
import logging
import os
from datetime import datetime
from functools import lru_cache

from sqlalchemy import select

from api.schemas import RestaurantResponse
from database.models import AnalysisJob, QualificationRun, ResearchRun, RestaurantContext

logger = logging.getLogger(__name__)


def restaurant_response(db, restaurant):
    stored_context = db.get(RestaurantContext, restaurant.id)
    response = RestaurantResponse.model_validate(restaurant)
    response.context = stored_context.data if stored_context else {}
    return response


def latest_research(db, restaurant_id):
    return db.scalar(
        select(ResearchRun)
        .where(ResearchRun.restaurant_id == restaurant_id)
        .order_by(ResearchRun.created_at.desc(), ResearchRun.id.desc())
        .limit(1)
    )


def matching_qualification(db, research):
    if research is None:
        return None
    return db.scalar(
        select(QualificationRun)
        .where(
            QualificationRun.restaurant_id == research.restaurant_id,
            QualificationRun.research_run_id == research.id,
            QualificationRun.status == "completed",
        )
        .order_by(QualificationRun.created_at.desc(), QualificationRun.id.desc())
        .limit(1)
    )


def latest_job(db, restaurant_id):
    return db.scalar(
        select(AnalysisJob)
        .where(AnalysisJob.restaurant_id == restaurant_id)
        .order_by(AnalysisJob.created_at.desc(), AnalysisJob.id.desc())
        .limit(1)
    )


def context_runs(db, restaurant_id):
    job = latest_job(db, restaurant_id)
    if job is not None and job.research_run_id is not None:
        research = db.get(ResearchRun, job.research_run_id)
        if research is not None and research.restaurant_id == restaurant_id:
            qualification = db.get(QualificationRun, job.qualification_run_id) if job.qualification_run_id else None
            if qualification is not None and (
                qualification.restaurant_id != restaurant_id
                or qualification.research_run_id != research.id
                or qualification.status != "completed"
            ):
                qualification = None
            return research, qualification, job
    research = latest_research(db, restaurant_id)
    return research, matching_qualification(db, research), job


def saved_field(qualification, name):
    """A field of the saved qualification, read from its full_result (the complete output of the agent).

    The separate columns (marketing_gaps, strengths, data_limitations) hold copies; a row that has no full_result
    (or lacks the field) falls back to its column.
    """
    if qualification is None:
        return None
    full = qualification.full_result if isinstance(qualification.full_result, dict) else {}
    return full[name] if name in full else getattr(qualification, name, None)


def gaps_response(restaurant, qualification):
    """Marketing gaps of the current qualification with a count per severity tier."""
    gaps = []
    for item in saved_field(qualification, "marketing_gaps") or []:
        if isinstance(item, dict) and str(item.get("gap") or "").strip():
            gaps.append({
                "gap": str(item["gap"]).strip(),
                "severity": str(item.get("severity") or "").strip().title(),
                "priority": item.get("priority") if isinstance(item.get("priority"), int) else None,
                "evidence": [str(text) for text in item.get("evidence") or []],
                "recommendation_focus": str(item.get("recommendation_focus") or ""),
                "description": str(item.get("description") or "").strip(),
                "rationale": str(item.get("rationale") or "").strip(),
                "confidence": str(item.get("confidence") or "").strip(),
            })
    gaps.sort(key=lambda item: (item["priority"] is None, item["priority"] or 0))
    limitations = [str(text) for text in saved_field(qualification, "data_limitations") or []]
    strengths = [str(text) for text in saved_field(qualification, "strengths") or []]
    severities = [item["severity"] for item in gaps]
    return {
        "restaurant_id": restaurant.id,
        "restaurant_name": restaurant.name,
        "qualification_id": qualification.id if qualification else None,
        "created_at": qualification.created_at if qualification else None,
        "counts": {
            "total": len(gaps),
            "high": severities.count("High"),
            # Some runs label the middle tier "Medium".
            "moderate": severities.count("Moderate") + severities.count("Medium"),
            "low": severities.count("Low"),
            "strengths": len(strengths),
            "data_limitations": len(limitations),
        },
        "gaps": gaps,
        "strengths": strengths,
        "data_limitations": limitations,
    }


def _enabled(name, default="true"):
    return os.getenv(name, default).strip().lower() not in {"0", "false", "no", "off"}


def run_workflow(*, session_factory=None, **kwargs):
    """Research -> Qualification -> Outreach draft (paused for approval). OUTREACH_AUTOSTART=false stops after Qualification."""
    from orchestration.workflow import run_restaurant_workflow

    return run_restaurant_workflow(
        **kwargs, session_factory=session_factory, start_outreach=_enabled("OUTREACH_AUTOSTART"),
    )


def outreach_application():
    """The wired Outreach Agent runtime (email drafting, approval pauses, sending)."""
    from agents.outreach_followup_agent.agent import RawajOutreachApplication
    from api import accounts

    return RawajOutreachApplication.create(access_provider=accounts.provision_access)


@lru_cache(maxsize=1)
def shared_outreach_application():
    """One Outreach runtime for the background stages (the API's own routes create theirs on first use)."""
    return outreach_application()


def generate_draft(outreach_input):
    """POST /outreach/draft: start Outreach for the current qualification and return its email draft.

    The email is only a draft awaiting human approval (see /api/approvals); nothing is sent here.
    """
    result = shared_outreach_application().workflow.start_outreach(
        restaurant_id=outreach_input["restaurant_id"],
        research_run_id=outreach_input["research_run_id"],
        qualification_run_id=outreach_input["qualification_run_id"],
    )
    if result.email_draft is None:
        raise RuntimeError("; ".join(result.errors) or "Outreach did not produce a draft.")
    return {
        "subject": result.email_draft.subject,
        "body": result.email_draft.plain_text_body,
        "message_type": result.email_draft.message_type.value,
    }


def _seconds(name, default):
    try:
        return max(0, int(os.getenv(name, default)))
    except ValueError:
        return default


def default_background_stages():
    """(callable, interval seconds) pairs the API runs while it is up. PIPELINE_AUTORUN=false turns them all off.

    Each one invokes the pipeline graph (orchestration/workflow.py) with one event.
    """
    if not _enabled("PIPELINE_AUTORUN"):
        return ()
    from functools import partial

    from orchestration import workflow

    stages = []
    if seconds := _seconds("STRATEGY_POLL_SECONDS", 5):
        stages.append((partial(workflow.run_strategy_stage, application_factory=shared_outreach_application), seconds))
    if seconds := _seconds("FOLLOWUP_POLL_SECONDS", 10 if os.getenv("RAWAJ_ENV", "development").lower() not in {"production", "prod"} else 3600):
        stages.append((partial(workflow.run_followups, application_factory=shared_outreach_application), seconds))
    return tuple(stages)


async def background_loop(runner, seconds):
    """Run a blocking pipeline stage every `seconds` in a worker thread; one failure never stops the loop."""
    last_error = None
    while True:
        try:
            await asyncio.to_thread(runner)
            last_error = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"[:300]
            if message != last_error:  # a broken setting would otherwise repeat every tick
                logger.error("Pipeline stage %s failed: %s", getattr(runner, "__name__", runner), message)
            last_error = message
        await asyncio.sleep(seconds)


def execute_analysis(job_id, session_factory, workflow_runner):
    """Each background job owns its sessions; no request session crosses threads."""
    with session_factory() as db:
        job = db.get(AnalysisJob, job_id)
        if job is None or job.status != "queued":
            return
        job.status = "running"
        job.started_at = datetime.utcnow()
        kwargs = {
            "restaurant_id": job.restaurant_id,
            "content_limit": job.content_limit,
            "lookback_days": job.lookback_days,
            "force_refresh": job.force_refresh,
            "context": job.context,
        }
        db.commit()

    result = {}
    error = None
    try:
        candidate = workflow_runner(**kwargs)
        if not isinstance(candidate, dict):
            raise TypeError("Workflow result must be an object")
        result = candidate
        if result.get("error") or not result.get("qualification_run_id"):
            error = "Analysis failed. Check the server configuration and retry."
        elif isinstance(result.get('outreach'), dict) and (
            result['outreach'].get('error') or result['outreach'].get('errors')
            or result['outreach'].get('status') == 'ERROR'
        ):
            error = "Research and qualification finished, but outreach preparation failed. Retry to continue from saved results."
    except Exception as exc:
        # Provider exception strings may contain tokens or private request data.
        logger.error("Analysis %s failed (%s)", job_id, type(exc).__name__)
        error = "Analysis failed. Check the server configuration and retry."

    with session_factory() as db:
        job = db.get(AnalysisJob, job_id)
        job.status = "failed" if error else "completed"
        job.error = error
        job.research_run_id = result.get("research_run_id")
        job.qualification_run_id = result.get("qualification_run_id")
        job.active_restaurant_id = None
        job.finished_at = datetime.utcnow()
        db.commit()
