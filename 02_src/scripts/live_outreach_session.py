"""Local, real-provider Ashi Sushi notebook session. No frontend changes."""
import asyncio
import os
import json
from contextlib import asynccontextmanager
from threading import RLock

from fastapi import Depends, FastAPI, HTTPException
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from sqlalchemy import select
from database.database import Base, engine, SessionLocal
from database.models import OutboundMessage, Strategy, OutreachRelationship
from agents.outreach_followup_agent.agent import RawajOutreachApplication, run_outreach_after_qualification
from agents.outreach_followup_agent.email_service import EmailService
from agents.outreach_followup_agent.config import get_settings
from api import approvals, accounts
from api.auth import require_admin_token
from orchestration.workflow import run_strategy_stage

RID = int(os.environ['LIVE_RESTAURANT_ID'])
RECIPIENT = os.environ['LIVE_RECIPIENT']
lock = RLock()
worker_state = {'state': 'waiting_for_yes', 'error_type': None}

class RestrictedEmail(EmailService):
    def send_approved_message(self, message):
        recipient = message.get('recipient') or message.get('recipient_email')
        if recipient != RECIPIENT or int(message.get('restaurant_id', -1)) != RID:
            raise ValueError('Live session recipient restriction')
        return super().send_approved_message(message)

runtime = RawajOutreachApplication.create(email_service=RestrictedEmail(get_settings()))

def tick():
    with lock:
        return run_strategy_stage(limit=1, application_factory=lambda: runtime)

async def worker():
    while True:
        try:
            result = await asyncio.to_thread(tick)
            if result.get('failed') or any(isinstance(item.get('notification'), str) for item in result.get('notifications', [])):
                print(json.dumps(result, default=str), flush=True)
                worker_state.update(state='stopped_on_error', error_type='StrategyOrNotificationFailure')
                return
            worker_state.update(state='watching', error_type=None)
        except Exception as exc:
            # Stop automatic retries to avoid repeated billable generation on a permanent error.
            worker_state.update(state='stopped_on_error', error_type=type(exc).__name__)
            return
        await asyncio.sleep(5)

@asynccontextmanager
async def lifespan(app):
    Base.metadata.create_all(engine)
    task = asyncio.create_task(worker())
    yield
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

app = FastAPI(lifespan=lifespan)
app.state.session_factory = SessionLocal
app.state.outreach_lock = lock
app.state.outreach_application = runtime
app.state.outreach_application_factory = lambda: runtime
app.include_router(approvals.router)
app.include_router(runtime.response_router())

@app.get('/health')
def health():
    return {'status': 'ok', 'mode': 'REAL_PROVIDERS'}

@app.post('/live/start', dependencies=[Depends(require_admin_token)])
def start():
    with lock, SessionLocal() as db:
        if db.scalar(select(OutreachRelationship).where(OutreachRelationship.restaurant_id == RID)):
            raise HTTPException(409, 'Already started. Review pending approvals or status.')
        return run_outreach_after_qualification(restaurant_id=RID,
            research_run_id=int(os.environ['LIVE_RESEARCH_ID']),
            qualification_run_id=int(os.environ['LIVE_QUALIFICATION_ID']), application=runtime)

@app.get('/live/status', dependencies=[Depends(require_admin_token)])
def status():
    with SessionLocal() as db:
        relation = db.scalar(select(OutreachRelationship).where(OutreachRelationship.restaurant_id == RID))
        messages = db.scalars(select(OutboundMessage).where(OutboundMessage.restaurant_id == RID)).all()
        strategies = db.scalars(select(Strategy).where(Strategy.restaurant_id == RID)).all()
        return {'relationship': relation.status if relation else None, 'worker': worker_state,
            'messages': [{'id': m.id, 'type': m.message_type, 'status': m.status,
                'recipient': m.recipient_email, 'provider_id': m.provider_message_id} for m in messages],
            'strategies': [{'id': s.id, 'data': s.strategy_data} for s in strategies]}

@app.get('/live')
@app.get('/live/strategy')
def strategy(credentials: HTTPBasicCredentials = Depends(HTTPBasic())):
    with SessionLocal() as db:
        try:
            account = accounts.authenticate(db, credentials.username, credentials.password)
        except accounts.AuthError:
            raise HTTPException(401, 'Invalid credentials', headers={'WWW-Authenticate': 'Basic'}) from None
        if account.restaurant_id != RID:
            raise HTTPException(403)
        plan = db.scalar(select(Strategy).where(Strategy.restaurant_id == RID).order_by(Strategy.id.desc()))
        if plan is None:
            raise HTTPException(404, 'Strategy is not ready')
        return {'restaurant': 'Ashi Sushi', 'strategy': plan.strategy_data,
                'trial': 'Activated on first successful login for 30 days'}
