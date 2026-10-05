"""Derive the agency view from durable evidence, without new lifecycle tables."""
from collections import defaultdict
from agents.outreach_followup_agent.guardrails import safe_preview
from datetime import datetime, timezone
from math import ceil
from sqlalchemy import select
from database import models as m
from agents.outreach_followup_agent.schemas import normalize_qualification_status


def iso(value):
    if value is None:
        return None
    return (value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value).isoformat()


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def grouped(db, model, order, restaurant_id=None):
    query = select(model).order_by(order.desc())
    if restaurant_id is not None:
        query = query.where(model.restaurant_id == restaurant_id)
    result = defaultdict(list)
    for row in db.scalars(query):
        result[row.restaurant_id].append(row)
    return result


def snapshot(db, restaurant_id=None, now=None):
    now = now or datetime.now(timezone.utc)
    tables = {
        'research': (m.ResearchRun, m.ResearchRun.created_at),
        'qualification': (m.QualificationRun, m.QualificationRun.created_at),
        'relationship': (m.OutreachRelationship, m.OutreachRelationship.updated_at),
        'messages': (m.OutboundMessage, m.OutboundMessage.created_at),
        'responses': (m.ButtonResponseEvent, m.ButtonResponseEvent.clicked_at),
        'requests': (m.StrategyRequest, m.StrategyRequest.created_at),
        'strategies': (m.Strategy, m.Strategy.created_at),
        'feedback': (m.ClientFeedbackRecord, m.ClientFeedbackRecord.received_at),
        'issues': (m.HumanEscalationRecord, m.HumanEscalationRecord.created_at),
        'jobs': (m.AnalysisJob, m.AnalysisJob.created_at),
        'trials': (m.ClientTrial, m.ClientTrial.activated_at),
    }
    data = {key: grouped(db, *value, restaurant_id) for key, value in tables.items()}
    query = select(m.Restaurant).order_by(m.Restaurant.created_at.desc(), m.Restaurant.id.desc())
    if restaurant_id is not None:
        query = query.where(m.Restaurant.id == restaurant_id)
    results = []
    for restaurant in db.scalars(query):
        rows = {key: value[restaurant.id] for key, value in data.items()}
        results.append(project(restaurant, rows, now))
    return results, data


def project(r, rows, now):
    latest = lambda key: next(iter(rows[key]), None)
    rel, trial, request = latest('relationship'), latest('trials'), latest('requests')
    research = next((x for x in rows['research'] if x.status.lower() in {'complete', 'completed'}), None)
    # Qualification must belong to the completed Research record being displayed.
    qualification = next((x for x in rows['qualification'] if x.status.lower() in {'complete', 'completed'}
                          and research and x.research_run_id == research.id), None)
    qual = normalize_qualification_status(qualification.qualification if qualification else None).value
    messages = rows['messages']
    sent = [x for x in messages if x.status in {'SENT', 'DELIVERED'} and x.provider_message_id]
    pending = [x for x in messages if x.message_type == 'INITIAL_OUTREACH' and x.status in {'GENERATED', 'PENDING_APPROVAL'}]
    # Superseded drafts must not count as another approval.
    superseded = {x.supersedes_message_id for x in messages if x.supersedes_message_id}
    pending = [x for x in pending if x.id not in superseded]
    if not rel or rel.status != 'PENDING_OUTBOUND_APPROVAL':
        pending = []
    responses = [x for x in rows['responses'] if x.status in {'RECEIVED', 'APPLIED'}]
    response = responses[0].action if responses else None
    status = rel.status if rel else (qual if qual != 'UNKNOWN' else 'NOT_STARTED')
    closed = status in {'DO_NOT_CONTACT', 'CLOSED_LOST', 'NOT_INTERESTED'} or bool(rel and rel.do_not_contact_at)
    interested = bool(rel and rel.interest_event_id) or any(x.action == 'INTERESTED' for x in responses)
    strategy = latest('strategies')
    # Existing plans may predate outreach. Do not invent a Strategy handoff from a saved plan.
    strategy_ready = bool(strategy or request and request.status == 'READY')
    delivered = any(x.message_type == 'STRATEGY_READY_NOTIFICATION' for x in sent)
    active_trial = bool(trial and utc(trial.activated_at) <= now < utc(trial.expires_at))
    trial_data = None
    if trial:
        duration = max(1, ceil((utc(trial.expires_at) - utc(trial.activated_at)).total_seconds()/86400))
        elapsed = max(0, (now - utc(trial.activated_at)).total_seconds()/86400)
        trial_data = dict(start=iso(trial.activated_at), end=iso(trial.expires_at),
            days_remaining=max(0, ceil((utc(trial.expires_at)-now).total_seconds()/86400)),
            current_day=min(duration, int(elapsed)+1), total_days=duration,
            percent=min(100, round(elapsed/duration*100)), status='Active' if active_trial else 'Ended',
            feedback_due_at=iso(trial.feedback_due_at), feedback_requested=bool(trial.feedback_requested_at))
    feedback_due = bool(active_trial and trial.feedback_due_at and utc(trial.feedback_due_at) <= now and not rows['feedback'])
    followup_due = bool(rel and status == 'WAITING_FOR_RESPONSE' and not closed and rel.next_contact_at
                        and utc(rel.next_contact_at) <= now)
    issues = [x for x in rows['issues'] if x.status.upper() not in {'RESOLVED', 'CLOSED'}]
    job = next((x for x in rows['jobs'] if x.status in {'queued', 'running'}), None)
    failed_job = latest('jobs') if rows['jobs'] and latest('jobs').status == 'failed' else None
    stage = ('Closed' if closed else 'Feedback' if rows['feedback'] else 'Free Trial' if trial else
             'Onboarding' if delivered else 'Strategy' if interested or request else
             'Outreach' if messages else 'Qualified' if qual == 'QUALIFIED' else
             'Qualification' if research else 'Research')
    next_action = ('No further outreach' if closed else 'Review issue' if issues else
                   'Review initial email' if pending else 'Review feedback' if rows['feedback'] else
                   'Feedback due' if feedback_due else 'Trial ending soon' if active_trial and trial_data['days_remaining'] <= 5 else
                   'Trial ended' if trial and not active_trial else 'Trial in progress' if active_trial else
                   'Waiting for activation' if delivered else 'Strategy ready for delivery' if request and request.status == 'READY' else
                   'Check strategy issue' if request and request.status in {'FAILED', 'ERROR'} else
                   'Strategy in progress' if interested or request else
                   'Automatic follow-up due' if followup_due else 'Waiting for response' if sent else
                   ('Queued for automatic processing' if job.status == 'queued' else 'Preparing initial draft' if qualification else 'Qualifying restaurant' if research else 'Researching restaurant') if job else 'Retry analysis' if failed_job else
                   'Add contact email' if qual == 'QUALIFIED' and not r.email else 'Outreach preparation needed' if qual == 'QUALIFIED' else 'Not qualified' if qual == 'NOT_QUALIFIED' else 'Run analysis')
    completed = [bool(research), bool(qualification), bool(sent), interested,
                 strategy_ready, bool(trial), bool(trial and not active_trial), bool(rows['feedback'])]
    labels = ['Research', 'Qualification', 'Outreach', 'Interested', 'Strategy', 'Onboarding', 'Free Trial', 'Feedback']
    current = {'Research':0, 'Qualification':1, 'Qualified':2, 'Outreach':3 if sent else 2,
               'Strategy':4, 'Onboarding':5, 'Free Trial':6, 'Feedback':7}.get(stage)
    lifecycle = [dict(label=label, state='completed' if completed[i] else
                  'current' if i == current and not closed else 'pending' if any(completed) else 'not_started')
                 for i,label in enumerate(labels)]
    flags = dict(qualified=qual == 'QUALIFIED', awaiting_approval=bool(pending), outreach_sent=bool(sent),
        waiting_response=status == 'WAITING_FOR_RESPONSE' and not closed, interested=interested and not closed,
        strategy=bool(request or interested) and not delivered and not closed,
        active_client=status == 'ACTIVE_CLIENT' or active_trial, free_trial=active_trial,
        followup_due=followup_due, feedback_due=feedback_due, closed=closed,
        trial_ending=bool(active_trial and trial_data['days_remaining'] <= 5),
        strategy_ready=bool(request and request.status == 'READY' and not delivered and not closed),
        feedback_received=bool(rows['feedback']), attention=bool(issues), analysis_failed=bool(failed_job and not job))
    return dict(id=r.id, name=r.name, instagram=r.instagram_username, email=r.email, location=r.location,
        created_at=iso(r.created_at), is_active=r.is_active, stage=stage, status=status,
        qualification=qual, next_action=next_action, lifecycle=lifecycle, flags=flags, trial=trial_data,
        response='NOT_INTERESTED' if closed else response or ('WAITING' if sent else 'NOT_CONTACTED'),
        last_contact=iso(rel.last_outbound_at) if rel else None,
        last_response=iso(rel.last_inbound_at) if rel else None,
        next_contact=iso(rel.next_contact_at) if rel and not closed else None,
        followup_attempts=sum(x.message_type == 'NO_RESPONSE_FOLLOW_UP' for x in sent),
        research_run_id=research.id if research else None, qualification_run_id=qualification.id if qualification else None,
        strategy_id=strategy.id if strategy else None,
        strategy_request_status=request.status if request else None, strategy_delivered=delivered,
        analysis_status=job.status if job else 'failed' if failed_job else None,
        can_start=bool(r.is_active and r.email and qual == 'QUALIFIED' and (not rel or rel.status == 'READY_TO_CONTACT') and not messages and not job),
        latest_feedback=dict(message=safe_preview(rows['feedback'][0].message)[:180], date=iso(rows['feedback'][0].received_at)) if rows['feedback'] else None)
