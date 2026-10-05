"""Agency routes exercise the real graph/SQL repository with external provider doubles."""
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from langgraph.checkpoint.memory import MemorySaver
from database.database import Base
from database.models import Restaurant,ResearchRun,QualificationRun,OutboundMessage,ClientTrial,ClientFeedbackRecord,OutreachRelationship
from database.outreach_repository import OutreachRepository
from agents.outreach_followup_agent.config import RawajSettings
from agents.outreach_followup_agent.agent import RawajOutreachApplication
from agents.outreach_followup_agent.schemas import ButtonClickEvent,ButtonAction,StrategyOutputHandoff
from test_workflow_offline import DeterministicLLM,FakeEmailService,RecordingDispatcher
from api.main import create_app

@pytest.fixture
def agency():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions=sessionmaker(engine,expire_on_commit=False)
    with sessions() as db:
        db.add(Restaurant(id=1,name='Agency Test Cafe',instagram_username='agency_test',email='test@example.com'))
        db.add(ResearchRun(id=101,restaurant_id=1,status='completed',source='test',research_signals=[{'signal_id':'menu_observation','dimension':'content','observation':'Public posts highlight seasonal iced drinks.'}],full_result={}))
        db.add(QualificationRun(id=202,restaurant_id=1,research_run_id=101,status='completed',qualification='Qualified for Rawaj.',marketing_gaps=[],full_result={}))
        db.commit()
    settings=RawajSettings(environment='test',public_base_url='http://localhost:8000',button_signing_secret='x'*32,allow_local_button_demo=True,follow_up_delay_minutes=2)
    email=FakeEmailService(settings)
    runtime=RawajOutreachApplication.create(settings=settings,repository=OutreachRepository(session_factory=sessions),llm=DeterministicLLM(),email_service=email,strategy_dispatcher=RecordingDispatcher(),checkpointer=MemorySaver(),access_provider=lambda _: {'username':'secret_account','password':'secret_password','login_url':'https://rawaj.test'})
    app=create_app(database_engine=engine,outreach_application=runtime,background_stages=())
    client=TestClient(app)
    yield SimpleNamespace(client=client,runtime=runtime,email=email,sessions=sessions,app=app)
    client.close();engine.dispose()

def start_and_send(a):
    started=a.client.post('/api/agency/prospects/1/start')
    assert started.status_code==200,started.text
    pending=a.client.get('/api/agency/outreach').json()['items'][0]
    result=a.client.post(f"/api/agency/outreach/{pending['id']}/decision",json={'decision':'APPROVED','revision':pending['revision']})
    assert result.status_code==200 and result.json()['sent'],result.text
    return pending

def test_separate_ui_real_database_and_duplicate_add(agency):
    c=agency.client
    assert c.get('/agency').status_code==200
    assert c.get('/agency/assets/app.js').status_code==200
    assert c.get('/agency/assets/routes.py').status_code==404
    assert c.get('/api/agency/dashboard').json()['total']==1
    p=c.get('/api/agency/prospects').json()['items'][0]
    assert p['name']=='Agency Test Cafe' and p['can_start']
    assert p['lifecycle'][0]['state']=='completed'
    assert c.post('/api/restaurants',json={'name':'Duplicate','instagram_username':'@AGENCY_TEST'}).status_code==409
    assert c.post('/api/restaurants',json={'name':'New Cafe','instagram_username':'new_cafe'}).status_code==201
    assert c.get('/api/agency/dashboard').json()['total']==2
    assert not any(key in c.get('/api/agency/prospects').text for key in ['password_hash','rawaj_password','rawaj_username'])

def test_agency_javascript_imports_are_served(agency):
    """A missing transitive module prevents every page from rendering."""
    import re
    from urllib.parse import urljoin

    pending = ['/agency/assets/app.js']
    visited = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        response = agency.client.get(path)
        assert response.status_code == 200, path
        assert 'javascript' in response.headers['content-type'], path
        for dependency in re.findall(r"\bfrom\s*['\"]([^'\"]+)['\"]", response.text):
            pending.append(urljoin(path, dependency))


def test_revision_approval_provider_result_and_replay(agency):
    c=agency.client
    assert c.post('/api/agency/prospects/1/start').status_code==200
    first=c.get('/api/agency/outreach').json()['items'][0]
    assert first['reviewable'] and not agency.email.sent
    path=f"/api/agency/outreach/{first['id']}/decision"
    assert c.post(path,json={'decision':'REJECTED','revision':1}).status_code==422
    assert c.post(path,json={'decision':'APPROVED','revision':999}).status_code==409
    assert c.post(path,json={'decision':'REJECTED','revision':1,'note':'Make it more concise'}).status_code==200
    pending=[x for x in c.get('/api/agency/outreach').json()['items'] if x['reviewable']]
    assert len(pending)==1 and pending[0]['id']!=first['id'] and not agency.email.sent
    latest=pending[0]
    result=c.post(f"/api/agency/outreach/{latest['id']}/decision",json={'decision':'APPROVED','revision':latest['revision']})
    assert result.status_code==200 and result.json()['sent']
    assert len(agency.email.sent)==1
    assert c.post(f"/api/agency/outreach/{latest['id']}/decision",json={'decision':'APPROVED','revision':latest['revision']}).status_code==404
    assert c.post('/api/agency/prospects/1/start').status_code==409
    assert c.get('/api/agency/dashboard').json()['counts']['outreach_sent']==1

def test_opt_out_blocks_due_contact(agency):
    sent=start_and_send(agency)
    event=ButtonClickEvent(restaurant_id=1,outreach_message_id=sent['id'],action=ButtonAction.NOT_INTERESTED,token_id='verified')
    agency.runtime.repository.record_button_event(event=event)
    agency.runtime.workflow.resume_from_button_event(event)
    p=agency.client.get('/api/agency/prospects/1').json()
    assert p['stage']=='Closed' and p['flags']['closed'] and not p['flags']['followup_due']
    assert not p['can_start'] and p['next_contact'] is None
    assert agency.client.post('/api/agency/prospects/1/start').status_code==409

def test_strategy_signal_trial_feedback_and_credentials_hidden(agency):
    sent=start_and_send(agency)
    event=ButtonClickEvent(restaurant_id=1,outreach_message_id=sent['id'],action=ButtonAction.INTERESTED,token_id='verified')
    agency.runtime.repository.record_button_event(event=event)
    result=agency.runtime.workflow.resume_from_button_event(event)
    assert agency.client.get('/api/agency/prospects/1').json()['stage']=='Strategy'
    ready=agency.runtime.workflow.receive_strategy_output(StrategyOutputHandoff(strategy_id=77,restaurant_id=1,strategy_request_id=result.strategy_request.strategy_request_id,status='READY',dashboard_strategy_url='https://rawaj.test/strategy',client_notification_allowed=True))
    assert not ready.errors and len(agency.email.sent)==2
    p=agency.client.get('/api/agency/prospects/1').json()
    assert p['stage']=='Onboarding' and p['trial'] is None
    payload=agency.client.get('/api/agency/outreach').text
    assert 'secret_password' not in payload and 'secret_account' not in payload
    now=datetime.now(timezone.utc)
    with agency.sessions() as db:
        db.add(ClientTrial(restaurant_id=1,activated_at=now-timedelta(days=27),expires_at=now+timedelta(days=3),feedback_due_at=now-timedelta(days=1)))
        db.commit()
    p=agency.client.get('/api/agency/prospects/1').json()
    assert p['trial']['days_remaining']==3 and p['trial']['current_day']==28
    assert p['flags']['feedback_due'] and p['flags']['trial_ending']
    with agency.sessions() as db:
        db.add(ClientFeedbackRecord(id='feedback-test',restaurant_id=1,message='A useful plan',rating=5,source='dashboard'));db.commit()
    assert agency.client.get('/api/agency/feedback').json()['items'][0]['message']=='A useful plan'
    assert not agency.client.get('/api/agency/prospects/1').json()['flags']['feedback_due']

def test_due_and_failed_delivery_are_truthful(agency):
    start_and_send(agency)
    with agency.sessions() as db:
        rel=db.scalar(select(OutreachRelationship));rel.next_contact_at=datetime.utcnow()-timedelta(minutes=1);db.commit()
    assert agency.client.get('/api/agency/dashboard').json()['counts']['followup_due']==1
    with agency.sessions() as db:
        msg=db.scalar(select(OutboundMessage));msg.provider_message_id=None;db.commit()
    assert agency.client.get('/api/agency/dashboard').json()['counts']['outreach_sent']==0
    assert agency.client.get('/api/agency/outreach').json()['items'][0]['status']=='UNCONFIRMED'

def test_cross_origin_mutation_is_rejected(agency):
    assert agency.client.post('/api/agency/prospects/1/start',headers={'Origin':'https://unrelated.example'}).status_code==403
    assert not agency.email.sent
