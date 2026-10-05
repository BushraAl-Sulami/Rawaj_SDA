"""Agency-only routes. No new authentication, database, or agent implementation."""
from pathlib import Path
import re
from datetime import datetime, timezone
from typing import Annotated
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from database import models as m
from api import approvals
from agency.services.prospects import snapshot, iso
from agents.outreach_followup_agent.guardrails import safe_preview

router = APIRouter(prefix='/api/agency', tags=['Agency'])
ROOT = Path(__file__).resolve().parent


def get_db(request: Request):
    with request.app.state.session_factory() as db:
        yield db


Database = Annotated[Session, Depends(get_db)]


def public_text(value):
    # Email bodies may contain account credentials. Never return a delivery footer to agency views.
    text = safe_preview(str(value or ''))
    text = re.split(r'(?im)^.*(?:Username:|Password:|rawaj_username|rawaj_password).*$', text)[0]
    text = re.sub(r'https?://\S+', '[link hidden]', text)
    return safe_preview(text)


def same_origin(request):
    origin = request.headers.get('origin')
    if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
        raise HTTPException(403, 'Open this action from the Agency workspace.')


@router.get('/prospects')
def prospects(db: Database):
    items, _ = snapshot(db)
    return {'items': items, 'updated_at': iso(datetime.now(timezone.utc))}


@router.get('/dashboard')
def dashboard(request: Request, db: Database):
    items, _ = snapshot(db)
    keys = ['qualified','awaiting_approval','outreach_sent','waiting_response','interested','strategy',
            'active_client','free_trial','followup_due','feedback_due','trial_ending','strategy_ready','attention','analysis_failed','feedback_received']
    return {'automation_enabled': bool(getattr(request.app.state, 'agency_automation_configured', False)), 'total':len(items), 'counts': {key:sum(x['flags'][key] for x in items) for key in keys},
            'stages': {stage:sum(x['stage']==stage for x in items) for stage in
                ['Research','Qualification','Qualified','Outreach','Strategy','Onboarding','Free Trial','Feedback','Closed']},
            'updated_at': iso(datetime.now(timezone.utc))}


_EMPTY_TEXT = {'', 'not available', 'n/a', 'none', 'null'}
_SOURCE_LINK = re.compile(r'\[([^\]]+)\]\((https?://[^)\s]+)\)')


def _plain(value, limit=600):
    """Readable text for the agency: internal metric paths removed, whitespace collapsed."""
    if not isinstance(value, str):
        return ''
    text = re.sub(r'\s*\(Metric Path:[^)]*\)', '', value)
    return ' '.join(text.split())[:limit]


def _texts(values, limit=8):
    items = [_plain(v) for v in (values if isinstance(values, list) else [])]
    return [v for v in items if v.casefold() not in _EMPTY_TEXT][:limit]


def _benchmark(value):
    """A benchmark sentence and its source link (only http/https), as the agent wrote them."""
    text = value if isinstance(value, str) else ''
    link = _SOURCE_LINK.search(text)
    return dict(text=_plain(_SOURCE_LINK.sub('', text)), url=link.group(2) if link else None)


def qualification_summary(qualification):
    """What the Qualification Agent decided and why: decision, rationale, marketing gaps, strengths and limits."""
    if qualification is None:
        return None
    full = qualification.full_result if isinstance(qualification.full_result, dict) else {}
    pick = lambda key: full.get(key) if full.get(key) not in (None, '', []) else getattr(qualification, key, None)
    gaps = []
    for gap in pick('marketing_gaps') or []:
        if not isinstance(gap, dict) or not _plain(gap.get('gap')):
            continue
        gaps.append(dict(
            gap=_plain(gap.get('gap'), 200), description=_plain(gap.get('description')),
            severity=_plain(gap.get('severity'), 40), confidence=_plain(gap.get('confidence'), 40),
            status=_plain(gap.get('status'), 40), rationale=_plain(gap.get('rationale')),
            priority=gap.get('priority') if isinstance(gap.get('priority'), int) else None,
            evidence=_texts(gap.get('evidence'), 5),
            benchmarks=[b for b in map(_benchmark, gap.get('benchmark_evidence') or []) if b['text']][:3],
        ))
    gaps.sort(key=lambda g: (g['priority'] is None, g['priority'] or 0))
    return dict(
        decision=_plain(pick('qualification'), 80), confidence=_plain(full.get('qualification_confidence'), 40),
        rationale=_plain(pick('decision_rationale'), 1500), gaps=gaps,
        strengths=_texts(pick('strengths')), limitations=_texts(pick('data_limitations')),
        date=iso(qualification.created_at),
    )


@router.get('/prospects/{restaurant_id}')
def detail(restaurant_id: int, db: Database):
    items, data = snapshot(db, restaurant_id)
    if not items:
        raise HTTPException(404, 'Prospect not found')
    item = items[0]
    research = db.get(m.ResearchRun, item['research_run_id']) if item['research_run_id'] else None
    if item['latest_feedback']:
        item['latest_feedback']['message'] = public_text(item['latest_feedback']['message'])
    observations = [x.get('observation','') for x in (research.research_signals or []) if isinstance(x,dict)] if research else []
    # Remove scraper label dictionaries, retaining the grounded human-readable observation.
    observations = [re.split(r'(?i)Observed (?:promotion )?intent labels:', text)[0].strip()[:300]
                    for text in observations if isinstance(text,str) and text.strip()]
    metrics = {}
    if research:
        # Only public research metrics, not raw scraper output or internal prompts.
        for source in (research.profile, research.metrics):
            for key,value in (source if isinstance(source,dict) else {}).items():
                if key in {'followers','followers_count','follower_count','posts_count','engagement_rate','posting_frequency','average_likes','average_comments'} and isinstance(value,(str,int,float)):
                    metrics[key.replace('_',' ').title()] = value
    timeline = []
    if research:
        timeline.append(dict(at=iso(research.created_at), title='Research completed', status='COMPLETED', href=None))
    qualification = db.get(m.QualificationRun, item['qualification_run_id']) if item['qualification_run_id'] else None
    if qualification:
        timeline.append(dict(at=iso(qualification.created_at), title='Qualification completed', status=item['qualification'], href=None))
    for msg in data['messages'][restaurant_id]:
        timeline.append(dict(at=iso(msg.sent_at or msg.created_at), title=msg.message_type.replace('_',' ').title(),
            status=msg.status if msg.status not in {'SENT','DELIVERED'} or msg.provider_message_id else 'UNCONFIRMED',
            href=f'#/outreach?restaurant={restaurant_id}&message={msg.id}'))
    for event in data['responses'][restaurant_id]:
        timeline.append(dict(at=iso(event.clicked_at), title='Restaurant response', status=event.action, href=None))
    for row in db.scalars(select(m.OutreachApproval).join(m.OutboundMessage,
            m.OutreachApproval.outreach_message_id==m.OutboundMessage.id).where(m.OutboundMessage.restaurant_id==restaurant_id)):
        timeline.append(dict(at=iso(row.decided_at or row.requested_at), title='Initial email review', status=row.status,
            href=f'#/outreach?restaurant={restaurant_id}'))
    for event in db.scalars(select(m.OutreachEvent).where(m.OutreachEvent.restaurant_id==restaurant_id)):
        if event.event_type in {'FEEDBACK_RECEIVED','STRATEGY_REQUESTED','STRATEGY_READY','TRIAL_ACTIVATED','CLIENT_MESSAGE_RECEIVED','ESCALATED_TO_HUMAN'}:
            timeline.append(dict(at=iso(event.created_at),title=event.event_type.replace('_',' ').title(),status=event.status,href=None))
    timeline.sort(key=lambda x:x['at'] or '',reverse=True)
    # Only show occasions actually selected in saved strategy or outreach context.
    events = []
    for msg in data['messages'][restaurant_id]:
        context = msg.personalization_context_json
        if isinstance(context,dict):
            for value in context.get('calendar_context',[]):
                if isinstance(value,str) and value not in events:
                    events.append(value)
    item.update(research={'observations':observations[:8], 'metrics':metrics, 'date':iso(research.created_at) if research else None},
                timeline=timeline[:100], relevant_events=events,
                qualification_detail=qualification_summary(qualification))
    return item


def draft_review_binding(msg):
    return dict(restaurant_id=msg.restaurant_id, research_run_id=msg.research_run_id,
        qualification_run_id=msg.qualification_run_id, message_id=msg.id,
        revision=msg.content_version, content_sha256=msg.content_sha256)


def initial_review_eligibility(db, msg):
    """A historical, sent, superseded or opted-out draft must never regain send controls."""
    if msg.message_type != 'INITIAL_OUTREACH' or msg.status not in {'GENERATED','PENDING_APPROVAL'} or msg.provider_message_id:
        return False
    restaurant = db.get(m.Restaurant, msg.restaurant_id)
    rel = db.scalar(select(m.OutreachRelationship).where(m.OutreachRelationship.restaurant_id == msg.restaurant_id))
    if not restaurant or not restaurant.is_active or not restaurant.email or not rel or rel.do_not_contact_at or rel.status not in {'WAIT','READY_TO_CONTACT','PENDING_OUTBOUND_APPROVAL'} or rel.outreach_attempts:
        return False
    sent = db.scalar(select(m.OutboundMessage.id).where(m.OutboundMessage.restaurant_id == msg.restaurant_id, m.OutboundMessage.provider_message_id.is_not(None)).limit(1))
    superseded = db.scalar(select(m.OutboundMessage.id).where(m.OutboundMessage.supersedes_message_id == msg.id).limit(1))
    return not sent and not superseded


@router.get('/outreach')
def outreach(request: Request, db: Database):
    # History remains readable if the model/runtime is temporarily unavailable.
    pending, runtime_error, application = {}, None, None
    try:
        application = approvals.get_outreach_application(request)
        pending = {p['message_id']:p for p in approvals._paused_email_requests(application)}
    except Exception:
        runtime_error = 'Approval service unavailable. Check the Outreach configuration and refresh.'
    restaurant_rows = {x.id:x for x in db.scalars(select(m.Restaurant))}
    restaurants = {key:value.name for key,value in restaurant_rows.items()}
    result = []
    messages = db.scalars(select(m.OutboundMessage).order_by(m.OutboundMessage.created_at.desc())).all()
    superseded = {msg.supersedes_message_id for msg in messages if msg.supersedes_message_id}
    for msg in messages:
        approval = pending.get(msg.id)
        restaurant = restaurant_rows.get(msg.restaurant_id)
        changed = bool(restaurant and restaurant.email and restaurant.email.casefold() != msg.recipient_email.casefold())
        eligible = initial_review_eligibility(db, msg)
        info = application.workflow.initial_draft_review_state(**draft_review_binding(msg)) if eligible and application else {}
        recoverable = bool(eligible and info.get('recoverable'))
        reviewable = bool(eligible and approval and not info.get('expired'))
        reason = ('This draft was replaced by a newer message.' if msg.id in superseded else
                  'This message has already been sent.' if msg.provider_message_id else
                  'The approval window expired. Renew the content review to enable approval.' if info.get('expired') else
                  'Content review did not finish. Retry review to enable approval and regeneration.' if recoverable else
                  'Historical message: an initial email has already been sent, or this relationship cannot be contacted.' if not eligible else
                  'This draft has no active approval. Check its saved workflow before sending.')
        recipient_revision = msg.id not in superseded and msg.message_type == 'INITIAL_OUTREACH' and not msg.provider_message_id and ((changed and (approval is not None or recoverable)) or (msg.status == 'FAILED' and msg.provider_status == 'RECIPIENT_MISMATCH'))
        result.append(dict(id=msg.id, restaurant_id=msg.restaurant_id, restaurant=restaurants.get(msg.restaurant_id,'Restaurant'),
            recipient=msg.recipient_email, subject=public_text(msg.subject), body=public_text(msg.plain_text_body),
            type=msg.message_type, status='SUPERSEDED' if msg.id in superseded and msg.status == 'GENERATED' else msg.status if msg.status not in {'SENT','DELIVERED'} or msg.provider_message_id else 'UNCONFIRMED',
            created_at=iso(msg.created_at), sent_at=iso(msg.sent_at), revision=msg.content_version,
            reviewable=reviewable, review_recovery=recoverable, read_only_reason=reason,
            recipient_revision=recipient_revision, current_recipient=restaurant.email if recipient_revision else None,
            expires_at=approval.get('expires_at') if approval else None))
    return {'items':result,'runtime_error':runtime_error}


class ReviewDecision(BaseModel):
    decision: str = Field(pattern='^(APPROVED|REJECTED)$')
    note: str | None = Field(default=None,max_length=1000)
    revision: int = Field(ge=1)


class ReviewRevision(BaseModel):
    revision: int = Field(ge=1)


@router.post('/outreach/{message_id}/retry-review')
def retry_review(message_id: str, payload: ReviewRevision, request: Request, db: Database):
    same_origin(request)
    application = approvals.get_outreach_application(request)
    with request.app.state.outreach_lock:
        msg = db.get(m.OutboundMessage, message_id)
        if not msg or msg.content_version != payload.revision or not initial_review_eligibility(db, msg):
            raise HTTPException(409, 'This draft cannot be reopened. Refresh and select the latest unsent draft.')
        restaurant = db.get(m.Restaurant, msg.restaurant_id)
        if restaurant.email.casefold() != msg.recipient_email.casefold():
            raise HTTPException(409, 'The recipient changed. Prepare a replacement for the current email first.')
        try:
            result = application.workflow.retry_initial_draft_review(**draft_review_binding(msg))
        except Exception:
            raise HTTPException(409, 'The saved draft changed or cannot be reviewed. Refresh and retry.') from None
        if result.errors:
            raise HTTPException(502, draft_failure_message(result.errors))
        if not any(p['message_id'] == msg.id for p in approvals._paused_email_requests(application)):
            raise HTTPException(409, 'The content review has not reached human approval yet.')
        return {'message_id': msg.id, 'sent': False, 'status': 'AWAITING_REVIEW'}


@router.post('/outreach/{message_id}/recipient-revision')
def revise_recipient(message_id: str, request: Request, db: Database):
    same_origin(request)
    application = approvals.get_outreach_application(request)
    with request.app.state.outreach_lock:
        msg = db.get(m.OutboundMessage, message_id)
        if not msg or msg.message_type != 'INITIAL_OUTREACH':
            raise HTTPException(404, 'Initial draft not found.')
        restaurant = db.get(m.Restaurant, msg.restaurant_id)
        relationship = db.scalar(select(m.OutreachRelationship).where(m.OutreachRelationship.restaurant_id == msg.restaurant_id))
        sent = db.scalar(select(m.OutboundMessage.id).where(m.OutboundMessage.restaurant_id == msg.restaurant_id, m.OutboundMessage.provider_message_id.is_not(None)).limit(1))
        if sent or not restaurant or not restaurant.is_active or not restaurant.email or (relationship and (relationship.do_not_contact_at or relationship.status in {'DO_NOT_CONTACT','CLOSED_LOST','NOT_INTERESTED'})):
            raise HTTPException(409, 'This relationship cannot restart initial outreach.')
        pending = any(p['message_id'] == message_id for p in approvals._paused_email_requests(application))
        changed = restaurant.email.casefold() != msg.recipient_email.casefold()
        info = application.workflow.initial_draft_review_state(**draft_review_binding(msg)) if initial_review_eligibility(db, msg) else {}
        if not (((pending or info.get('recoverable')) and changed) or (msg.status == 'FAILED' and msg.provider_status == 'RECIPIENT_MISMATCH')):
            raise HTTPException(409, 'This message does not need a recipient revision. Refresh.')
        try:
            result = application.workflow.revise_recipient(restaurant_id=msg.restaurant_id,
                research_run_id=msg.research_run_id, qualification_run_id=msg.qualification_run_id, message_id=msg.id)
        except Exception:
            raise HTTPException(409, 'The saved draft could not be replaced. Refresh and check its workflow.') from None
        if result.errors or not result.email_draft:
            raise HTTPException(502, draft_failure_message(result.errors))
        return {'message_id': result.email_draft.message_id, 'sent': False}


@router.post('/outreach/{message_id}/decision')
def review(message_id: str, payload: ReviewDecision, request: Request, db: Database):
    same_origin(request)
    msg = db.get(m.OutboundMessage,message_id)
    if not msg or msg.message_type != 'INITIAL_OUTREACH':
        raise HTTPException(404,'Initial outreach draft not found')
    if payload.decision == 'APPROVED' and not initial_review_eligibility(db, msg):
        raise HTTPException(404 if msg.status in {'SENT','DELIVERED','REJECTED'} else 409, 'This historical draft cannot be sent. Refresh and select the latest pending draft.')
    if msg.content_version != payload.revision:
        raise HTTPException(409,'The draft changed. Refresh and review the latest version.')
    if payload.decision=='REJECTED' and not (payload.note or '').strip():
        raise HTTPException(422,'Tell the agent what to change before regenerating.')
    restaurant = db.get(m.Restaurant, msg.restaurant_id)
    if payload.decision == 'APPROVED' and (not restaurant or not restaurant.email or restaurant.email.casefold() != msg.recipient_email.casefold()):
        raise HTTPException(409, 'The contact email changed. Refresh and prepare a draft for the current email, then review it again. No email was sent.')
    application = approvals.get_outreach_application(request)
    result = approvals.decide(message_id, approvals.DecisionRequest(decision=payload.decision,
        reviewer_id='agency-reviewer', note=payload.note), request, application)
    db.refresh(msg)
    # Do not return raw provider exceptions or checkpoint state.
    failure = 'The contact email changed. Prepare a draft for the current email and review it again. No email was sent.' if msg.provider_status == 'RECIPIENT_MISMATCH' else 'The agent could not finish this action. Check its configuration and refresh.'
    return {'sent':result.sent,'decision':result.decision,
            'errors':[failure] if result.errors else []}


DRAFT_FAILURE_MESSAGES = {
    'MODEL_CONNECTION_FAILED': 'The Outreach agent cannot reach the model provider. Check the server network connection, then retry. No email was sent.',
    'MODEL_ACCESS_DENIED': 'The model provider rejected access. Check the API key and model permissions. No email was sent.',
    'MODEL_UNAVAILABLE': 'The configured model is unavailable for this account. Check the model setting. No email was sent.',
    'MODEL_RATE_LIMITED': 'The model provider is rate-limiting this account or its quota is exhausted. Check usage and retry. No email was sent.',
    'MODEL_REQUEST_REJECTED': 'The model provider rejected the model request. Check model compatibility. No email was sent.',
    'MODEL_OUTPUT_INVALID': 'The model response did not meet the required format. Retry draft generation. No email was sent.',
    'EMAIL_REVIEW_FAILED': 'The draft did not pass its content review. No email was sent.',
}


def draft_failure_message(errors):
    for code, message in DRAFT_FAILURE_MESSAGES.items():
        if any(code in str(error) for error in errors or []):
            return message
    # The workflow reports a failed review in words ("Email review did not pass"), not with its code.
    if any('review did not pass' in str(error).casefold() for error in errors or []):
        return DRAFT_FAILURE_MESSAGES['EMAIL_REVIEW_FAILED']
    return 'The agent could not complete this draft. Check its saved workflow status and retry. No email was sent.'


@router.post('/prospects/{restaurant_id}/start')
def start(restaurant_id: int, request: Request, db: Database):
    same_origin(request)
    application = approvals.get_outreach_application(request)
    with request.app.state.outreach_lock:
        items,_ = snapshot(db,restaurant_id)
        if not items:
            raise HTTPException(404,'Prospect not found')
        row=items[0]
        if not row['can_start']:
            raise HTTPException(409,'Complete research and qualification, add an email, and check existing outreach before starting.')
        try:
            result=application.workflow.start_outreach(restaurant_id=restaurant_id,
                research_run_id=row['research_run_id'],qualification_run_id=row['qualification_run_id'])
        except Exception:
            raise HTTPException(502,'The agent could not generate a draft. Check its configuration.') from None
        if result.errors or not result.email_draft:
            raise HTTPException(502,draft_failure_message(result.errors))
    return {'message_id':result.email_draft.message_id,'status':'AWAITING_REVIEW'}


@router.get('/feedback')
def feedback(db: Database):
    names = {x.id:x.name for x in db.scalars(select(m.Restaurant))}
    items = [dict(id=x.id,restaurant_id=x.restaurant_id,restaurant=names.get(x.restaurant_id),
        message=public_text(x.message),rating=x.rating,date=iso(x.received_at),source=x.source)
        for x in db.scalars(select(m.ClientFeedbackRecord).order_by(m.ClientFeedbackRecord.received_at.desc()))]
    issues=[dict(id=x.id,restaurant_id=x.restaurant_id,restaurant=names.get(x.restaurant_id),
        reason=public_text(x.reason),severity=x.severity,status=x.status,date=iso(x.created_at))
        for x in db.scalars(select(m.HumanEscalationRecord).order_by(m.HumanEscalationRecord.created_at.desc()))]
    return {'items':items,'issues':issues}


@router.post('/issues/{issue_id}/resolve')
def resolve_issue(issue_id: str, request: Request, db: Database):
    same_origin(request)
    issue=db.get(m.HumanEscalationRecord,issue_id)
    if not issue:
        raise HTTPException(404,'Issue not found')
    issue.status='RESOLVED'
    issue.resolved_at=datetime.utcnow()
    db.commit()
    return {'status':'RESOLVED'}


def mount_agency(application):
    application.include_router(router)
    # Serve only frontend assets; Python services and route source are not public files.
    for folder in ('pages', 'components', 'styles'):
        application.mount('/agency/assets/' + folder, StaticFiles(directory=ROOT/folder), name='agency-' + folder)

    @application.middleware('http')
    async def agency_assets_revalidate(request, call_next):
        # Without this the browser guesses how long to keep old CSS/JS; "no-cache" makes it ask (a cheap 304 when unchanged).
        response = await call_next(request)
        if request.url.path.startswith('/agency/assets/'):
            response.headers['Cache-Control'] = 'no-cache'
        return response

    @application.get('/agency/assets/app.js', include_in_schema=False)
    def agency_script():
        return FileResponse(ROOT/'app.js', media_type='text/javascript')
    @application.get('/agency/assets/services/api.js', include_in_schema=False)
    def agency_api_script():
        return FileResponse(ROOT/'services'/'api.js', media_type='text/javascript')
    @application.get('/agency',include_in_schema=False)
    @application.get('/agency/',include_in_schema=False)
    def agency_page():
        return FileResponse(ROOT/'index.html',headers={'Cache-Control':'no-cache'})
