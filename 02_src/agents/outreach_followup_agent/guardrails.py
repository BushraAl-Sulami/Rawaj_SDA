"""Executable guardrails for Rawaj Outreach & Follow-Up.

Presentation guide (read this file from top to bottom):
1. Secret redaction: sanitize model inputs, logs and notebook previews.
2. Input/output gates: validate evidence, links, identity and generated text.
3. Workflow policy: eligibility, consent, timing, limits and exact approval.

The workflow calls these functions; this is not a second, unused rule list.
Database transactions still enforce atomic sending and persisted provenance.
Email signing and HTML escaping remain in the email transport implementation.
Redaction is NOT encryption: private delivery bodies/checkpoints still contain
the credentials required by the requested email-based account delivery flow.
Never share runtime databases, checkpoint files or exported raw graph states.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlparse

from .schemas import (ActionType, ApprovalStatus, EmailDraft, EscalationReason,
    OutreachDecision, OutreachGraphState, QualificationHandoff, ResearchHandoff,
    RelationshipStatus, WorkflowPhase, WorkflowTrigger, utc_now)


class WorkflowSafetyError(RuntimeError):
    """Stop before a side effect. Messages contain codes, never rejected values."""


REDACTED = '[REDACTED]'
_SECRET_KEYS = {'username','rawajusername','password','rawajpassword','passwordhash',
    'apikey','openaiapikey','resendapikey','smtppassword','smtpusername',
    'accountsecret','buttonsigningsecret','adminapitoken','accesstoken',
    'authorization','clientsecret','sessionsecrets','token'}
_LABEL = re.compile(
    r'(?im)(?:rawaj[_\s-]*)?(?:user\s*name|password|اسم\s*المستخدم|كلمة\s*المرور|الرقم\s*السري)'
    r'["\x27]?\s*[=:]\s*["\x27]?([^\s<>"\x27,}]+)')
_API_KEY = re.compile(r'\b(?:sk|re)_[A-Za-z0-9_-]{16,}\b|\bsk-[A-Za-z0-9_-]{16,}\b')
_TOKEN_URL = re.compile(r'([?&](?:token|signature|access_token)=)[^&\s<>"\x27]+',re.I)


def _key(value: str) -> str:
    return re.sub(r'[^a-z0-9]','',value.lower())


class SecretProtector:
    """Keep a private per-workflow value set; redact a COPY, never the email.

    Known values are removed even when repeated under an innocent JSON key.
    No secret registry is printed, serialized or added to LangGraph state.
    Unknown, unlabelled secrets cannot be reliably recognized by a regex.
    """
    def __init__(self):
        self._values: set[str] = set()

    def register(self, *values: str) -> None:
        self._values.update(str(v) for v in values if v and str(v)!=REDACTED)

    def safe(self, value: Any) -> Any:
        if hasattr(value,'model_dump'): value=value.model_dump(mode='json')
        # Discover labelled secrets first, then redact every occurrence.
        def collect(item):
            if isinstance(item,dict):
                for key,val in item.items():
                    if _key(str(key)) in _SECRET_KEYS and isinstance(val,str): self.register(val)
                    collect(val)
            elif isinstance(item,(list,tuple)):
                for val in item: collect(val)
            elif isinstance(item,str):
                plain=re.sub(r'<[^>]*>',' ',html.unescape(item))
                self.register(*(m.group(1).strip() for m in _LABEL.finditer(plain)))
        collect(value)
        def clean(item):
            if isinstance(item,dict):
                return {key:REDACTED if _key(str(key)) in _SECRET_KEYS else clean(val) for key,val in item.items()}
            if isinstance(item,(list,tuple)): return [clean(val) for val in item]
            if not isinstance(item,str): return item
            for secret in sorted(self._values,key=len,reverse=True):
                for encoded in {secret,html.escape(secret),quote(secret,safe=''),json.dumps(secret)[1:-1]}:
                    item=item.replace(encoded,REDACTED)
            item=_LABEL.sub(lambda m:m.group(0).replace(m.group(1),REDACTED),item)
            return _TOKEN_URL.sub(r'\1[REDACTED]',_API_KEY.sub(REDACTED,item))
        return clean(value)


def safe_preview(value: Any) -> Any:
    """Public helper for notebooks and diagnostics; never print raw states."""
    protector=SecretProtector()
    protector.register(*(v for k,v in os.environ.items() if _key(k) in _SECRET_KEYS))
    return protector.safe(value)


class SecretLogFilter(logging.Filter):
    """Opt-in handler filter. Also removes exception text that may echo input."""
    def __init__(self, protector=None):
        super().__init__(); self.protector=protector or SecretProtector()
    def filter(self, record):
        record.msg=self.protector.safe(safe_preview(record.getMessage()))
        record.args=()
        if record.exc_info:
            record.msg += ' [exception details omitted]'
            record.exc_info=None; record.exc_text=None
        return True


def protect_logging(protector):
    """Attach to existing handlers; custom application handlers must opt in too."""
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretLogFilter(protector))


def model_context(value, protector=None):
    """Treat research, calendar entries and customer messages as untrusted data.

    This detector catches obvious override attempts; it is not a complete
    prompt-injection solution. Deterministic tool and approval gates below
    remain authoritative even if the model follows a malicious instruction.
    """
    safe=(protector or SecretProtector()).safe(safe_preview(value))
    text=json.dumps(safe,ensure_ascii=False)
    if re.search(r'ignore (?:all |the )?(?:previous|system) instructions|'
                 r'تجاهل\s+(?:كل\s+)?(?:التعليمات|تعليمات النظام)|'
                 r'<\|(?:im_start|system)\|>',text,re.I):
        raise WorkflowSafetyError('UNTRUSTED_INSTRUCTION_DETECTED')
    return safe


def validate_proposal(proposal, evidence):
    """Reject invented links/credentials, unsupported percentages and promises.

    Semantic grounding is also checked by the existing independent review
    model. These deterministic checks do not prove all natural-language facts.
    """
    text=proposal.subject+'\n'+proposal.plain_text_body
    if re.search(r'https?://|www\.',text,re.I): raise WorkflowSafetyError('MODEL_GENERATED_URL')
    if _LABEL.search(text) or _API_KEY.search(text): raise WorkflowSafetyError('MODEL_GENERATED_CREDENTIALS')
    if re.search(r'guarantee(?:d)?\s+(?:sales|revenue|results|growth)|'
                 r'نضمن\s+(?:زيادة|مضاعفة|نتائج)|مبيعات\s+مضمونة',text,re.I):
        raise WorkflowSafetyError('UNAPPROVED_MARKETING_PROMISE')
    if not set(proposal.personalization_observation_ids)<=set(evidence.observation_ids):
        raise WorkflowSafetyError('UNSUPPORTED_FACT_REFERENCE')
    facts=json.dumps(evidence.observations,ensure_ascii=False)
    for percentage in re.findall(r'\b\d+(?:\.\d+)?\s*%',text):
        if percentage not in facts: raise WorkflowSafetyError('UNSUPPORTED_PERCENTAGE')


def validate_provenance(provenance, research, qualification):
    """One restaurant and one selected Research/Qualification chain only."""
    pairs=[(research.restaurant.restaurant_id,provenance['restaurant_id']),
        (qualification.restaurant_id,provenance['restaurant_id']),
        (research.research_run_id,provenance['research_run_id']),
        (qualification.research_run_id,provenance['research_run_id']),
        (qualification.qualification_run_id,provenance['qualification_run_id'])]
    if any(str(left)!=str(right) for left,right in pairs):
        raise WorkflowSafetyError('HANDOFF_PROVENANCE_MISMATCH')


def strategy_errors(output, request):
    """Validate the teammate signal without changing the Strategy Agent."""
    errors=[]
    if str(output.restaurant_id)!=str(request.restaurant_id): errors.append('Strategy restaurant_id does not match its request')
    if output.strategy_request_id!=request.strategy_request_id: errors.append('Strategy request ID does not match its request')
    if str(getattr(output.status,'value',output.status))!='READY': errors.append('Strategy output is not READY')
    if not output.dashboard_strategy_url: errors.append('Strategy output has no dashboard URL')
    if not output.client_notification_allowed: errors.append('Strategy is not approved for a client notification')
    return errors


def require_matching_recipient(message, restaurant):
    """Recheck the persisted restaurant at the last pre-provider boundary."""
    expected=restaurant.get('email')
    actual=message.get('recipient') or message.get('recipient_email')
    if not expected or str(actual).casefold()!=str(expected).casefold():
        raise WorkflowSafetyError('RECIPIENT_MISMATCH')


def credential_delivery_error(message):
    """Only the strategy-ready email may contain account credentials.

    This check runs immediately before SMTP/Resend. Never redact the actual
    approved delivery body: doing so would silently email unusable credentials.
    """
    subject=str(message.get('subject') or '')
    text='\n'.join(str(message.get(k) or '') for k in ('plain_text_body','html_body'))
    if _LABEL.search(subject) or _API_KEY.search(subject+text):
        return 'SECRET_IN_UNSAFE_EMAIL_FIELD'
    if _LABEL.search(text) and message.get('message_type')!='STRATEGY_READY_NOTIFICATION':
        return 'CREDENTIALS_IN_WRONG_MESSAGE_TYPE'
    return None


def _as_utc(value):
    if not value:return None
    try: result=datetime.fromisoformat(value.replace('Z','+00:00'))
    except ValueError:return None
    return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result.astimezone(timezone.utc)


class OutreachGuardrails:
    """Workflow policy mixin: existing policies are moved here, not duplicated.

    Atomic claims, approval expiry and consent are rechecked by the repository
    in its transaction. Do not replace those storage guarantees with prompts.
    A BLOCK stops execution; a human REJECT requests a new reviewed draft.
    """
    _TRUSTED_FOOTER_MARKER = '\n\nOpen your strategy here:'


    def _hard_gate(self, state: OutreachGraphState) -> dict[str, Any]:
        """Apply non-negotiable eligibility, timing, DNC, and safety gates."""

        if state.get("errors"):
            return {"next_node": "finish", "workflow_phase": WorkflowPhase.FAILED.value}

        try:
            trigger = WorkflowTrigger(state["trigger"])
            memory = self._memory_from_payload(state["relationship_memory"])
            qualification = QualificationHandoff.model_validate(state["qualification"])

            # A hard opt-out wins over every later communication trigger.
            if not memory.promotional_contact_allowed:
                return self._stop(
                    "DO_NOT_CONTACT_ACTIVE",
                    RelationshipStatus.DO_NOT_CONTACT,
                )

            # Strategy output may still be recorded internally after an opt-out,
            # but it must never generate an external notification.
            if trigger is WorkflowTrigger.STRATEGY_HANDOFF_RECEIVED:
                return {"next_node": "process_strategy_output"}

            if not qualification.is_qualified:
                updated = self._transition_memory(
                    memory,
                    status=RelationshipStatus.CLOSED_LOST,
                    important_context="Outreach stopped: the exact Qualification handoff was not Qualified.",
                )
                return {
                    "relationship_memory": updated.model_dump(mode="json"),
                    "next_node": "finish",
                    "workflow_phase": WorkflowPhase.STOPPED.value,
                    "events": [
                        {
                            "type": "OUTREACH_BLOCKED_NOT_QUALIFIED",
                            "at": utc_now(),
                        }
                    ],
                }

            if trigger is WorkflowTrigger.BUTTON_CLICKED:
                if not state.get("button_event"):
                    raise WorkflowSafetyError("BUTTON_CLICKED requires a verified button event.")
                return {"next_node": "process_button_event"}

            if trigger is WorkflowTrigger.FOLLOW_UP_DUE:
                due_at = _as_utc(memory.next_contact_at)
                if memory.status is not RelationshipStatus.WAITING_FOR_RESPONSE:
                    return self._stop("FOLLOW_UP_NOT_APPLICABLE", memory.status)
                if due_at is None or due_at > self._now():
                    return self._stop("FOLLOW_UP_NOT_DUE", memory.status)
                if memory.outreach_attempts >= self.settings.follow_up_max_attempts:
                    return {"next_node": "prepare_escalation"}
                return {"next_node": "decide_next_action"}

            if trigger is WorkflowTrigger.CLIENT_MESSAGE_RECEIVED:
                client_message = str(state.get("client_message") or "").strip()
                if not client_message:
                    raise WorkflowSafetyError("CLIENT_MESSAGE_RECEIVED requires client_message.")
                updated = self._remember_client_message(memory, client_message)
                if updated != memory:
                    updated = self._write_memory(updated, expected_version=memory.version)
                update = {"relationship_memory": updated.model_dump(mode="json")}
                strategy_context = self._load_optional_strategy_context(
                    updated.strategy_request_id
                )
                if strategy_context:
                    update["strategy_context"] = strategy_context
                if self._is_sensitive_issue(client_message):
                    update["next_node"] = "prepare_escalation"
                else:
                    update["next_node"] = "decide_next_action"
                return update

            if trigger is WorkflowTrigger.START_OUTREACH:
                if memory.status not in {
                    RelationshipStatus.READY_TO_CONTACT,
                    RelationshipStatus.WAIT,
                }:
                    return self._stop("OUTREACH_ALREADY_STARTED", memory.status)
                research = ResearchHandoff.model_validate(state["research"])
                if not research.can_support_personalization:
                    return {
                        "next_node": "prepare_escalation",
                        "events": [
                            {
                                "type": "OUTREACH_HELD_NO_GROUNDED_RESEARCH",
                                "at": utc_now(),
                            }
                        ],
                    }
                return {"next_node": "decide_next_action"}

            if trigger is WorkflowTrigger.SCHEDULED_CLIENT_CHECK:
                if hasattr(self.repository, "trial_feedback_due") and not self.repository.trial_feedback_due(
                    restaurant_id=int(memory.restaurant_id), now=self._now()
                ):
                    return self._stop("TRIAL_FEEDBACK_NOT_DUE", memory.status)
                if memory.status not in {
                    RelationshipStatus.ACTIVE_CLIENT,
                    RelationshipStatus.STRATEGY_DELIVERED,
                    RelationshipStatus.STRATEGY_READY,
                }:
                    return self._stop("CLIENT_CHECK_NOT_APPLICABLE", memory.status)
                due_at = _as_utc(memory.next_contact_at)
                if due_at is not None and due_at > self._now():
                    return self._stop("CLIENT_CHECK_NOT_DUE", memory.status)
                result: dict[str, Any] = {"next_node": "decide_next_action"}
                strategy_context = self._load_optional_strategy_context(
                    memory.strategy_request_id
                )
                if strategy_context:
                    result["strategy_context"] = strategy_context
                return result

            return self._stop("UNSUPPORTED_TRIGGER", memory.status)
        except Exception as error:
            return self._safe_failure("HARD_GATE_FAILED", error)


    def _constrain_decision(
        self, state: OutreachGraphState, decision: OutreachDecision
    ) -> OutreachDecision:
        """Prevent the LLM from bypassing deterministic workflow boundaries."""

        trigger = WorkflowTrigger(state["trigger"])
        memory = self._memory_from_payload(state["relationship_memory"])
        research = ResearchHandoff.model_validate(state["research"])
        valid_observations = {signal.signal_id for signal in research.research_signals}
        valid_events = {
            event.get("event_id")
            for event in state.get("calendar_events", [])
            if isinstance(event, dict) and event.get("event_id")
        }
        action = decision.action
        next_status = decision.next_status

        if trigger is WorkflowTrigger.START_OUTREACH:
            action = ActionType.SEND_INITIAL_OUTREACH
            next_status = RelationshipStatus.PENDING_OUTBOUND_APPROVAL
        elif trigger is WorkflowTrigger.FOLLOW_UP_DUE:
            # A due time starts a reassessment; it is never permission to send
            # blindly.  The decision model may select WAIT when the history and
            # current context do not support a useful new message.
            if action not in {
                ActionType.SEND_FOLLOW_UP,
                ActionType.WAIT,
                ActionType.CONTACT_LATER,
                ActionType.ESCALATE_TO_HUMAN,
            }:
                action = ActionType.WAIT
                next_status = RelationshipStatus.WAITING_FOR_RESPONSE
            elif action is ActionType.SEND_FOLLOW_UP:
                next_status = RelationshipStatus.PENDING_OUTBOUND_APPROVAL
        elif trigger is WorkflowTrigger.CLIENT_MESSAGE_RECEIVED:
            message = str(state.get("client_message") or "")
            if self._is_sensitive_issue(message):
                action = ActionType.ESCALATE_TO_HUMAN
                next_status = RelationshipStatus.ESCALATED_TO_HUMAN
            elif action not in {
                ActionType.SEND_CLIENT_CHECK_IN,
                ActionType.REQUEST_FEEDBACK,
                ActionType.REVIEW_CLIENT_ISSUE,
                ActionType.CONTACT_LATER,
                ActionType.WAIT,
                ActionType.ESCALATE_TO_HUMAN,
            }:
                action = ActionType.REVIEW_CLIENT_ISSUE
                next_status = RelationshipStatus.PENDING_OUTBOUND_APPROVAL
        elif trigger is WorkflowTrigger.SCHEDULED_CLIENT_CHECK and action not in {
            ActionType.SEND_CLIENT_CHECK_IN,
            ActionType.REQUEST_FEEDBACK,
            ActionType.WAIT,
            ActionType.ESCALATE_TO_HUMAN,
        }:
            action = ActionType.WAIT
            next_status = memory.status

        if trigger is WorkflowTrigger.FOLLOW_UP_DUE:
            action = ActionType.SEND_FOLLOW_UP
            next_status = RelationshipStatus.WAITING_FOR_RESPONSE
        if trigger is WorkflowTrigger.SCHEDULED_CLIENT_CHECK:
            action = ActionType.REQUEST_FEEDBACK
            next_status = RelationshipStatus.ACTIVE_CLIENT

        if memory.outreach_attempts >= self.settings.follow_up_max_attempts and action is ActionType.SEND_FOLLOW_UP:
            action = ActionType.ESCALATE_TO_HUMAN
            next_status = RelationshipStatus.NO_RESPONSE_HUMAN_REVIEW

        if action is ActionType.SEND_INITIAL_OUTREACH:
            requires_human_approval = True
        else:
            requires_human_approval = False
        requires_escalation = action is ActionType.ESCALATE_TO_HUMAN
        language = "en" if action in {
            ActionType.SEND_INITIAL_OUTREACH,
            ActionType.SEND_FOLLOW_UP,
        } else decision.language
        return decision.model_copy(
            update={
                "action": action,
                "next_status": next_status,
                "language": language,
                "personalization_observation_ids": [
                    item
                    for item in decision.personalization_observation_ids
                    if item in valid_observations
                ],
                "selected_calendar_event_ids": [
                    item
                    for item in decision.selected_calendar_event_ids
                    if item in valid_events
                ],
                "requires_human_approval": requires_human_approval,
                "requires_escalation": requires_escalation,
            }
        )


    @staticmethod
    def _validated_dashboard_url(value: str | None) -> str:
        text = str(value or "").strip()
        parsed = urlparse(text)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or any(ord(c)<32 for c in text)):
            raise WorkflowSafetyError("A validated absolute dashboard URL is required.")
        if os.getenv('RAWAJ_ENV','').lower() in {'production','prod'} and parsed.scheme!='https':
            raise WorkflowSafetyError('DASHBOARD_REQUIRES_HTTPS')
        return text


    @staticmethod
    def _redacted_draft_for_review(draft: EmailDraft) -> dict[str, Any]:
        """Give the content reviewer no recipient data or signed-link secrets.

        The final email has already been deterministically rendered and validated
        by ``EmailService``. The review model needs the human-facing wording
        and button labels, but not bearer tokens or a hostname it cannot verify.
        """

        payload = draft.model_dump(mode="json")
        text = payload["plain_text_body"]
        for button in draft.buttons:
            text = text.replace(button.url, "[trusted signed response link]")

        # The dashboard link, sign-in details and privacy note are added by trusted code after generation. The
        # reviewing model judges only the wording the model wrote; the client's password must never reach it.
        marker = OutreachGuardrails._TRUSTED_FOOTER_MARKER
        if marker in text:
            text = text.split(marker)[0] + (
                "\n\n[The system appends the dashboard link, the client's sign-in details and a privacy note "
                "after review; they are not part of this review.]"
            )
        text = re.sub(r"(?im)^(Password:).*$", r"\1 [redacted]", text)

        payload["recipient"] = "[redacted recipient]"
        payload["plain_text_body"] = text
        payload["html_body"] = (
            "[trusted rendered email shell; signed response URLs are redacted "
            "from the content reviewer]"
        )
        payload["buttons"] = [
            {
                "action": button.action.value,
                "label": button.label,
                "url": "[trusted signed response link]",
            }
            for button in draft.buttons
        ]
        return safe_preview(payload)


    def _approval_decision(self, response: Any) -> dict[str, Any]:
        if not isinstance(response, dict):
            raise WorkflowSafetyError("Human Approval response must be an object.")
        decision = str(response.get("decision") or "").upper()
        reviewer_id = str(response.get("reviewer_id") or "").strip()
        if decision not in {ApprovalStatus.APPROVED.value, ApprovalStatus.REJECTED.value}:
            raise WorkflowSafetyError("Human Approval must be APPROVED or REJECTED.")
        if not reviewer_id:
            raise WorkflowSafetyError("Human Approval requires reviewer_id.")
        return {
            "decision": decision,
            "reviewer_id": reviewer_id,
            "note": str(response.get("note") or "").strip() or None,
            "message_id": response.get("message_id"),
            "message_revision": response.get("message_revision"),
            "content_sha256": response.get("content_sha256"),
        }


    @staticmethod
    def _verify_approval_binding(
        request: dict[str, Any], response: dict[str, Any]
    ) -> None:
        for key in ("message_id", "message_revision", "content_sha256"):
            if str(response.get(key) or "") != str(request.get(key) or ""):
                raise WorkflowSafetyError(
                    "Human Approval did not bind to the exact rendered email."
                )


    @staticmethod
    def _safe_failure(code: str, error: Exception) -> dict[str, Any]:
        # Persist concise safe diagnostics in state.  Provider stack traces,
        # secret-bearing messages, and LLM prompts remain out of customer text.
        return {
            "workflow_phase": WorkflowPhase.FAILED.value,
            "next_node": "finish",
            "errors": [f"{code}: {type(error).__name__}" + (
                ": " + error.diagnostic_code if getattr(error, "diagnostic_code", None) in {
                    "MODEL_CONNECTION_FAILED", "MODEL_ACCESS_DENIED", "MODEL_UNAVAILABLE",
                    "MODEL_RATE_LIMITED", "MODEL_REQUEST_REJECTED", "MODEL_OUTPUT_INVALID"
                } else "")],
            "events": [{"type": code, "at": utc_now()}],
        }


    def _guarded_graph_invoke(self, *args, **kwargs):
        # Graph states include final delivery bodies. Never upload them to tracing.
        from langsmith import tracing_context
        with tracing_context(enabled=False):
            return self.graph.invoke(*args, **kwargs)
