"""Run the existing dashboard and complete API against one preserved notebook session."""
from pathlib import Path
import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

def configure(session):
    from dotenv import dotenv_values
    inherited = dict(os.environ)
    for filename in ('.env', '.env.live'):
        os.environ.update({k:v for k,v in dotenv_values(ROOT/filename).items() if v})
    os.environ.update({k:v for k,v in inherited.items() if v})
    session = Path(session).resolve()
    if not (session/'live.db').is_file() or not (session/'session-secrets.json').is_file():
        raise ValueError('Select the existing notebook session containing live.db and session-secrets.json.')
    secrets = json.loads((session/'session-secrets.json').read_text(encoding='utf-8'))
    for key in ('ACCOUNT_SECRET','BUTTON_SIGNING_SECRET','ADMIN_API_TOKEN'):
        if len(secrets.get(key,'')) < 32: raise ValueError('Session secret is missing: '+key)
    os.environ.update(secrets)
    os.environ.update(DATABASE_URL='sqlite:///'+(session/'live.db').as_posix(),
        LANGGRAPH_CHECKPOINT_PATH=str(session/'checkpoints.sqlite'),
        STRATEGY_HANDOFF_OUTBOX=str(session/'handoffs'/'inbox'),
        PUBLIC_BASE_URL='http://127.0.0.1:8000', DASHBOARD_URL='http://127.0.0.1:8501',
        RAWAJ_API_URL='http://127.0.0.1:8000', RAWAJ_DEMO_LOGIN='false',
        ALLOW_LOCAL_BUTTON_DEMO='true', PIPELINE_AUTORUN='false',
        RAWAJ_SESSION_ID=hashlib.sha256(str(session).encode()).hexdigest()[:16],
        LANGSMITH_TRACING='false')
    return session

def create_linked_app():
    from fastapi import Depends
    from fastapi.responses import RedirectResponse
    from api.main import create_app
    from api.auth import require_admin_token
    from api.services import shared_outreach_application
    from orchestration.workflow import run_strategy_stage
    from agents.outreach_followup_agent.agent import run_outreach_after_qualification
    from database.database import SessionLocal
    from database.models import OutboundMessage, Strategy, OutreachRelationship, StrategyRequest
    from sqlalchemy import select
    from pydantic import BaseModel
    runtime = shared_outreach_application()
    state = {'enabled':False,'stage':'paused','failure':None}
    def tick():
        if not state['enabled']: return
        try:
            state.update(stage='recovering_responses',failure=None)
            runtime.scheduler.recover_pending_responses_once()
            state['stage']='strategy_generation_and_notification'
            outcome=run_strategy_stage(application_factory=lambda:runtime)
            if outcome.get('failed') or any(isinstance(n.get('notification'),str) for n in outcome.get('notifications',[])):
                raise RuntimeError('Strategy or notification failed; inspect the saved request and provider error.')
            state['stage']='followups'
            prospect=runtime.scheduler.run_due_followups_once()
            client=runtime.scheduler.run_due_client_checks_once()
            if prospect.failed or client.failed: raise RuntimeError('A scheduled follow-up failed.')
            state['stage']='watching'
        except Exception as error:
            state.update(enabled=False,failure=type(error).__name__,stage='stopped_on_error')
    app=create_app(outreach_application=runtime,background_stages=((tick,10),))
    @app.get('/live',include_in_schema=False)
    @app.get('/live/strategy',include_in_schema=False)
    def old_email_link():
        return RedirectResponse(os.environ['DASHBOARD_URL']+'/strategy',status_code=307)
    @app.get('/api/local/session',dependencies=[Depends(require_admin_token)])
    def session_status():
        return {'session_id':os.environ['RAWAJ_SESSION_ID'],'dashboard_url':os.environ['DASHBOARD_URL']+'/strategy',
                'pipeline':dict(state)}
    @app.post('/api/local/pipeline/start',dependencies=[Depends(require_admin_token)])
    def start_pipeline():
        state.update(enabled=True,stage='waiting',failure=None)
        return dict(state)
    @app.post('/api/local/pipeline/stop',dependencies=[Depends(require_admin_token)])
    def stop_pipeline():
        state['enabled']=False
        return dict(state)
    class StartRequest(BaseModel):
        restaurant_id: int
        research_run_id: int
        qualification_run_id: int
    @app.post('/api/local/outreach/start',dependencies=[Depends(require_admin_token)])
    def start_outreach(payload:StartRequest):
        with app.state.outreach_lock:
            return run_outreach_after_qualification(**payload.model_dump(),application=runtime)
    @app.get('/api/local/journey/{restaurant_id}',dependencies=[Depends(require_admin_token)])
    def journey(restaurant_id:int):
        with SessionLocal() as db:
            relation=db.scalar(select(OutreachRelationship).where(OutreachRelationship.restaurant_id==restaurant_id))
            messages=db.scalars(select(OutboundMessage).where(OutboundMessage.restaurant_id==restaurant_id)).all()
            plans=db.scalars(select(Strategy).where(Strategy.restaurant_id==restaurant_id)).all()
            requests=db.scalars(select(StrategyRequest).where(StrategyRequest.restaurant_id==restaurant_id)).all()
            return {'relationship':relation.status if relation else None,
                'pipeline':dict(state),'strategy_requests':[{'id':r.id,'status':r.status} for r in requests],
                'saved_strategy_ids':[p.id for p in plans],
                'messages':[{'id':m.id,'type':m.message_type,'status':m.status,'provider_id':m.provider_message_id} for m in messages]}
    return app

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--session',required=True)
    parser.add_argument('--api-only',action='store_true')
    args=parser.parse_args()
    session=configure(args.session)
    if args.api_only:
        import uvicorn
        uvicorn.run(create_linked_app(),host='127.0.0.1',port=8000,access_log=False)
        return
    # Refuse an ambiguous old notebook server instead of routing the dashboard to another DB.
    for port in (8000,8501):
        with socket.socket() as sock:
            if sock.connect_ex(('127.0.0.1',port))==0:
                raise RuntimeError(f'Port {port} already in use. Stop the previous notebook/server first.')
    processes=[]
    logs=[]
    try:
        for name,command in [
            ('api',[sys.executable,'-m','scripts.linked_workspace','--session',str(session),'--api-only']),
            ('dashboard',[sys.executable,'-m','streamlit','run','rawaj_front/app.py','--server.address','127.0.0.1',
                '--server.port','8501','--server.headless','true','--browser.gatherUsageStats','false'])]:
            log=open(session/(name+'.log'),'a',encoding='utf-8'); logs.append(log)
            processes.append(subprocess.Popen(command,cwd=ROOT,env=os.environ.copy(),stdout=log,stderr=log))
        import requests
        headers={'X-Admin-Token':os.environ['ADMIN_API_TOKEN']}
        for _ in range(120):
            if any(p.poll() is not None for p in processes): raise RuntimeError('A service exited. Inspect session api.log/dashboard.log.')
            try:
                response=requests.get('http://127.0.0.1:8000/api/local/session',headers=headers,timeout=1)
                ui=requests.get('http://127.0.0.1:8501/_stcore/health',timeout=1)
                if response.ok and response.json()['session_id']==os.environ['RAWAJ_SESSION_ID'] and ui.ok: break
            except requests.RequestException: pass
            time.sleep(.5)
        else: raise RuntimeError('Services did not become ready.')
        print('READY: http://127.0.0.1:8501/strategy (existing session; automation paused)',flush=True)
        while all(p.poll() is None for p in processes): time.sleep(1)
    finally:
        for process in processes:
            if process.poll() is None: process.terminate()
        for process in processes:
            try: process.wait(timeout=10)
            except subprocess.TimeoutExpired: process.kill()
        for log in logs: log.close()

if __name__=='__main__': main()
