
"""FastAPI routes for restaurant data, agent execution, and result retrieval."""

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime
from functools import partial
from pathlib import Path as FilePath
from threading import Lock
from typing import Annotated
from uuid import uuid4

from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from api import agent_strategy, approvals, auth, planning, post_kit, services
from api.schemas import (
    AnalyzeRequest, ContextResponse, JobResponse, OutreachDraft,
    GapsResponse, QualificationResponse, ResearchResponse, RestaurantCreate,
    RestaurantList, RestaurantResponse, RestaurantUpdate,
)
from database.database import Base, ensure_legacy_database_schema, engine
from database.models import AnalysisJob, QualificationRun, Restaurant, RestaurantContext
from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {"message": "API is running!"}

logger = logging.getLogger(__name__)
RestaurantId = Annotated[int, Path(gt=0)]


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


def require_restaurant(db, restaurant_id):
    restaurant = db.get(Restaurant, restaurant_id)
    if restaurant is None:
        raise HTTPException(404, "Restaurant not found")
    return restaurant


def require_idle(db, restaurant_id):
    active = db.scalar(
        select(AnalysisJob.id).where(AnalysisJob.active_restaurant_id == restaurant_id)
    )
    if active:
        raise HTTPException(409, {"message": "Analysis is already running", "job_id": active})


def create_app(database_engine=engine, workflow_runner=None, outreach_runner=None, *,
               outreach_application=None, outreach_application_factory=None, background_stages=()):
    session_factory = sessionmaker(bind=database_engine, autoflush=False, expire_on_commit=False)
    restaurant_write_lock = Lock()

    @asynccontextmanager
    async def lifespan(application):
        ensure_legacy_database_schema(database_engine)
        Base.metadata.create_all(bind=database_engine)
        # This local development API runs in a single server process.
        with session_factory() as db:
            db.execute(
                update(AnalysisJob)
                .where(AnalysisJob.status.in_(["queued", "running"]))
                .values(
                    status="failed", active_restaurant_id=None,
                    finished_at=datetime.utcnow(),
                    error="The server restarted before analysis finished. Run the analysis again.",
                )
            )
            db.commit()
        from agents.outreach_followup_agent.config import get_settings, ConfigurationError
        try:
            get_settings().require_button_endpoint()
            runtime = approvals.get_outreach_application(type("AppRequest", (), {"app": application})())
            application.include_router(runtime.response_router())
        except ConfigurationError:
            logger.warning("Email response buttons need PUBLIC_BASE_URL and BUTTON_SIGNING_SECRET.")
        # Pipeline stages that run while the API is up (Strategy after "Interested", follow-ups).
        loops = [asyncio.create_task(services.background_loop(runner, seconds)) for runner, seconds in background_stages]
        try:
            yield
        finally:
            for loop in loops:
                loop.cancel()

    application = FastAPI(title="Rawaj API", version="1.0.0", lifespan=lifespan)
    application.state.session_factory = session_factory
    application.state.agency_automation_configured = bool(background_stages)
    application.state.restaurant_write_lock = restaurant_write_lock
    application.state.content_ideas_runner = planning.generate_content_ideas
    application.include_router(planning.router)
    application.include_router(agent_strategy.router)
    application.include_router(post_kit.router)
    application.include_router(approvals.router)
    application.include_router(auth.router)
    from api.client_lifecycle import router as client_router
    application.include_router(client_router)
    from agency.routes import mount_agency
    mount_agency(application)
    application.state.outreach_lock = Lock()
    application.state.outreach_application = outreach_application
    application.state.outreach_application_factory = outreach_application_factory or services.shared_outreach_application
    application.state.workflow_runner = workflow_runner or partial(services.run_workflow, session_factory=session_factory)
    application.state.outreach_runner = outreach_runner or services.generate_draft
    origins = os.getenv(
        "FRONTEND_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:5173,http://127.0.0.1:5173",
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[value.strip().rstrip("/") for value in origins.split(",") if value.strip()],
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Content-Type", "X-Admin-Token"],
    )

    @application.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        logger.error("Database operation failed (%s)", type(exc).__name__)
        return JSONResponse(status_code=500, content={"detail": "Database operation failed"})

    @application.get("/health", tags=["Health"])
    def health():
        return {"status": "ok"}

    @application.get("/api/restaurants", response_model=RestaurantList, tags=["Restaurants"])
    def list_restaurants(db: Database, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100)):
        restaurants = db.scalars(select(Restaurant).order_by(Restaurant.id).offset(offset).limit(limit)).all()
        return RestaurantList(
            items=[services.restaurant_response(db, item) for item in restaurants],
            total=db.scalar(select(func.count(Restaurant.id))), offset=offset, limit=limit,
        )

    @application.post("/api/restaurants", response_model=RestaurantResponse, status_code=201, tags=["Restaurants"])
    def create_restaurant(payload: RestaurantCreate, db: Database, background_tasks: BackgroundTasks,
                          request: Request, autostart: bool = False):
        existing = db.scalar(select(Restaurant.id).where(func.lower(Restaurant.instagram_username) == payload.instagram_username))
        if existing:
            raise HTTPException(409, "A restaurant with this Instagram username already exists")
        restaurant = Restaurant(
            **payload.model_dump(exclude={"context"}),
            instagram_url=f"https://www.instagram.com/{payload.instagram_username}/",
        )
        try:
            db.add(restaurant)
            db.flush()
            db.add(RestaurantContext(restaurant_id=restaurant.id, data=payload.context))
            job = None
            if autostart and restaurant.is_active:
                job = AnalysisJob(id=str(uuid4()), restaurant_id=restaurant.id,
                    active_restaurant_id=restaurant.id, context=payload.context)
                db.add(job)
            db.commit()
        except IntegrityError:
            db.rollback()
            raise HTTPException(409, "A restaurant with this Instagram username already exists") from None
        if job is not None:
            background_tasks.add_task(services.execute_analysis, job.id, session_factory,
                                      request.app.state.workflow_runner)
        return services.restaurant_response(db, restaurant)

    @application.get("/api/restaurants/{restaurant_id}", response_model=RestaurantResponse, tags=["Restaurants"])
    def get_restaurant(restaurant_id: RestaurantId, db: Database):
        return services.restaurant_response(db, require_restaurant(db, restaurant_id))

    @application.patch("/api/restaurants/{restaurant_id}", response_model=RestaurantResponse, tags=["Restaurants"])
    def update_restaurant(restaurant_id: RestaurantId, payload: RestaurantUpdate, db: Database,
                          background_tasks: BackgroundTasks, request: Request, autostart: bool = False):
        with restaurant_write_lock:
            restaurant = require_restaurant(db, restaurant_id)
            require_idle(db, restaurant_id)
            values = payload.model_dump(exclude_unset=True)
            if "context" in values:
                context = db.get(RestaurantContext, restaurant_id)
                if context is None:
                    context = RestaurantContext(restaurant_id=restaurant_id)
                    db.add(context)
                context.data = values.pop("context")
            for key, value in values.items():
                setattr(restaurant, key, value)
            restaurant.updated_at = datetime.utcnow()
            job = None
            if autostart and restaurant.is_active and restaurant.email and 'email' in values:
                from database.models import OutboundMessage, OutreachRelationship
                relationship = db.scalar(select(OutreachRelationship).where(OutreachRelationship.restaurant_id == restaurant_id))
                contacted = db.scalar(select(OutboundMessage.id).where(OutboundMessage.restaurant_id == restaurant_id).limit(1))
                if not contacted and (not relationship or (relationship.status in {'READY_TO_CONTACT', 'WAIT'} and not relationship.do_not_contact_at)):
                    saved_context = db.get(RestaurantContext, restaurant_id)
                    job = AnalysisJob(id=str(uuid4()), restaurant_id=restaurant_id,
                        active_restaurant_id=restaurant_id, context=saved_context.data if saved_context else {})
                    db.add(job)
            db.commit()
            if job is not None:
                background_tasks.add_task(services.execute_analysis, job.id, session_factory,
                                          request.app.state.workflow_runner)
            return services.restaurant_response(db, restaurant)

    @application.get("/api/restaurants/{restaurant_id}/context", response_model=ContextResponse, tags=["Restaurants"])
    def get_context(restaurant_id: RestaurantId, db: Database):
        restaurant = require_restaurant(db, restaurant_id)
        research, qualification, job = services.context_runs(db, restaurant_id)
        return ContextResponse(
            restaurant=services.restaurant_response(db, restaurant),
            research=ResearchResponse(
                id=research.id, status=research.status, created_at=research.created_at, result=research.full_result,
            ) if research else None,
            qualification=QualificationResponse(
                id=qualification.id, research_run_id=qualification.research_run_id,
                status=qualification.status, created_at=qualification.created_at, result=qualification.full_result,
            ) if qualification else None,
            latest_job=JobResponse.model_validate(job) if job else None,
        )

    @application.get("/api/restaurants/{restaurant_id}/gaps", response_model=GapsResponse, tags=["Restaurants"])
    def get_gaps(restaurant_id: RestaurantId, db: Database):
        restaurant = require_restaurant(db, restaurant_id)
        _, qualification, _ = services.context_runs(db, restaurant_id)
        if qualification is None:
            # A failed re-run must not hide gaps from an earlier completed qualification.
            qualification = db.scalar(
                select(QualificationRun)
                .where(QualificationRun.restaurant_id == restaurant_id, QualificationRun.status == "completed")
                .order_by(QualificationRun.created_at.desc(), QualificationRun.id.desc())
            )
        return services.gaps_response(restaurant, qualification)

    @application.post("/api/restaurants/{restaurant_id}/analyze", response_model=JobResponse, status_code=202, tags=["Agents"])
    def analyze(restaurant_id: RestaurantId, background_tasks: BackgroundTasks, request: Request, db: Database,
                payload: Annotated[AnalyzeRequest, Body()] = AnalyzeRequest()):
        with restaurant_write_lock:
            restaurant = require_restaurant(db, restaurant_id)
            if not restaurant.is_active:
                raise HTTPException(409, "Activate this restaurant before running analysis")
            require_idle(db, restaurant_id)
            context = db.get(RestaurantContext, restaurant_id)
            job = AnalysisJob(
                id=str(uuid4()), restaurant_id=restaurant_id, active_restaurant_id=restaurant_id,
                context=context.data if context else {}, **payload.model_dump(),
            )
            try:
                db.add(job)
                db.commit()
            except IntegrityError:
                db.rollback()
                require_idle(db, restaurant_id)
                raise HTTPException(409, "Could not start analysis; retry the request") from None
            response = JobResponse.model_validate(job)
            background_tasks.add_task(
                services.execute_analysis, job.id, session_factory, request.app.state.workflow_runner,
            )
            return response

    @application.get("/api/jobs/{job_id}", response_model=JobResponse, tags=["Agents"])
    def get_job(job_id: str, db: Database):
        job = db.get(AnalysisJob, job_id)
        if job is None:
            raise HTTPException(404, "Analysis job not found")
        return JobResponse.model_validate(job)

    @application.post("/api/restaurants/{restaurant_id}/outreach/draft", response_model=OutreachDraft, tags=["Agents"])
    def outreach_draft(restaurant_id: RestaurantId, request: Request, db: Database):
        restaurant = require_restaurant(db, restaurant_id)
        require_idle(db, restaurant_id)
        if not restaurant.email:
            raise HTTPException(422, "Add a contact email before generating an outreach draft")
        _, qualification, _ = services.context_runs(db, restaurant_id)
        if qualification is None:
            raise HTTPException(409, "Complete restaurant analysis before generating an outreach draft")
        try:
            draft = request.app.state.outreach_runner({
                "restaurant_id": restaurant.id,
                "research_run_id": qualification.research_run_id,
                "qualification_run_id": qualification.id,
                "restaurant_name": restaurant.name,
                "email": restaurant.email,
                "marketing_gaps": [
                    item.get("gap", "") if isinstance(item, dict) else str(item)
                    for item in (qualification.marketing_gaps or [])
                ],
                "qualification_summary": qualification.decision_rationale or "",
                "message_type": "initial",
            })
            return OutreachDraft.model_validate(draft.model_dump() if hasattr(draft, "model_dump") else draft)
        except Exception as exc:
            logger.error("Draft generation failed (%s)", type(exc).__name__)
            raise HTTPException(502, "Could not generate the draft. Check server configuration and retry.") from None

    frontend_dist = FilePath(__file__).resolve().parents[1] / "frontend" / "dist"
    application.mount(
        "/assets", StaticFiles(directory=frontend_dist / "assets", check_dir=False), name="frontend-assets"
    )

    @application.get("/", include_in_schema=False)
    def frontend_page():
        index = frontend_dist / "index.html"
        if not index.is_file():
            raise HTTPException(503, "Build the frontend with: npm --prefix frontend run build")
        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    return application


app = create_app(background_stages=services.default_background_stages())
