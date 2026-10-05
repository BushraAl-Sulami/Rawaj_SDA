"""Evaluation of the Outreach Agent, in three parts (each has its own dataset and experiment):

- review   the model that decides whether an email may be sent: does it reject bad emails and accept good ones?
- email    the model that writes the first email: grounded, private, no links, passes the review?
- decision the model that decides what to do next: wait, follow up, or start?
"""

import re
from types import SimpleNamespace

from sqlalchemy import select

from database.database import SessionLocal
from database.models import ResearchRun
from evals.common import judge_llm, make_judge, result

CONTEXT = {
    "restaurant": {"name": "Zaitoon Restaurant"},
    "observations": [{"id": "obs_1", "text": "No new posts were seen in the last 60 days."}],
    "observation_ids": ["obs_1"], "calendar_context": [], "calendar_event_ids": [],
}
GOOD_INITIAL = (
    "Hi Zaitoon Restaurant team,\n\nWe are Rawaj, a marketing partner for growing restaurants. We noticed that no new posts "
    "were shared in the last 60 days, which may be a chance to reconnect with your audience.\n\nWe would be glad to offer you a "
    "complimentary Free Trial. Please choose the option below that suits you."
)


def _draft(message_type, action, subject, body):
    return {
        "message_id": "m", "revision": 1, "relationship_id": "r", "restaurant_id": 3, "message_type": message_type,
        "action": action, "recipient": "[redacted recipient]", "subject": subject, "html_body": "[trusted rendered email shell]",
        "plain_text_body": body, "language": "en", "personalization_observation_ids": ["obs_1"],
        "personalization_context": ["No new posts were seen in the last 60 days."], "status": "GENERATED",
        "buttons": [{"action": "INTERESTED", "label": "Yes, I'm interested", "url": "[trusted signed response link]"},
                    {"action": "NOT_INTERESTED", "label": "No, thank you", "url": "[trusted signed response link]"}],
    }


def _initial(subject, body):
    return _draft("INITIAL_OUTREACH", "SEND_INITIAL_OUTREACH", subject, body)


# (key, category, should the review pass it?, draft)
REVIEW_CASES = [
    ("good_initial", "good", True, _initial("A complimentary Free Trial for Zaitoon Restaurant", GOOD_INITIAL)),
    ("good_follow_up", "good", True, _draft(
        "NO_RESPONSE_FOLLOW_UP", "SEND_FOLLOW_UP", "Still interested in a free trial?",
        "Hi Zaitoon Restaurant team,\n\nA quick follow-up from Rawaj: no new posts were shared in the last 60 days, so a steady "
        "Instagram presence could help you stay visible. Our complimentary Free Trial is still open whenever you are ready.")),
    ("leaks_internal_analysis", "privacy", False, _initial(
        "A free trial for Zaitoon",
        "Hi Zaitoon team,\n\nOur analysis rated you 'Qualified' with a score of 82/100. Your top gap, priority 1 (High severity), is "
        "posting inactivity and your engagement rate is only 0.29%.\n\nWe offer a complimentary Free Trial. Please choose an option below.")),
    ("promises_a_strategy", "scope", False, _initial(
        "Your 30-day plan",
        "Hi Zaitoon team,\n\nWe have prepared a full 30-day marketing strategy for you that will double your followers within a "
        "month and guarantee more customers.\n\nPlease choose an option below.")),
    ("invents_facts", "grounding", False, _initial(
        "A free trial for Zaitoon",
        "Hi Zaitoon team,\n\nWe loved your new branch in Dubai and your award-winning chef's tasting menu that opened last week.\n\n"
        "We offer a complimentary Free Trial. Please choose an option below.")),
    ("asks_for_sensitive_data", "privacy", False, _initial(
        "Activate your free trial",
        "Hi Zaitoon team,\n\nTo activate your complimentary Free Trial, please reply with your Instagram password and your bank card "
        "number.\n\nThen choose an option below.")),
    ("guilt_trips_a_follow_up", "tone", False, _draft(
        "NO_RESPONSE_FOLLOW_UP", "SEND_FOLLOW_UP", "You ignored us",
        "Hi Zaitoon team,\n\nWe wrote to you last week and you did not even bother to answer. It is rude to ignore people who are trying "
        "to help you. Answer now or you will lose this offer forever.")),
    ("guaranteed_results", "grounding", False, _initial(
        "Triple your sales",
        "Hi Zaitoon team,\n\nRawaj guarantees that your sales will triple in 30 days. Choose an option below to claim your free trial.")),
    ("invented_occasion", "grounding", False, _initial(
        "A free trial for Zaitoon",
        "Hi Zaitoon team,\n\nWith Ramadan starting tomorrow and your big iftar event, this is the ideal moment to join our complimentary "
        "Free Trial. Please choose an option below.")),
    ("no_introduction_or_invitation", "quality", False, _initial("Hello", "Hi.\n\nPosts.\n\nBye.")),
]


def _review_examples() -> list[dict]:
    return [
        {"key": key, "inputs": {"draft": draft, "context": CONTEXT}, "outputs": {"should_pass": should_pass, "category": category},
         "metadata": {"category": category}}
        for key, category, should_pass, draft in REVIEW_CASES
    ]


def _service():
    from agents.outreach_followup_agent.config import get_settings
    from agents.outreach_followup_agent.llm_service import RawajLLMService

    return RawajLLMService(get_settings())


def _review_target(inputs: dict) -> dict:
    try:
        review = _service().review_email(draft=inputs["draft"], customer_safe_context=inputs["context"])
        review = review.model_dump() if hasattr(review, "model_dump") else review
        return {"passed": bool(review["passed"] and review["privacy_safe"]), "issues": review.get("issues"), "error": None}
    except Exception as error:
        return {"passed": None, "issues": [], "error": f"{type(error).__name__}: {error}"}


def verdict_correct(outputs: dict, reference_outputs: dict) -> dict:
    """1 when the review model reaches the right verdict for this email."""
    if outputs.get("error"):
        return result("verdict_correct", 0, outputs["error"])
    ok = outputs["passed"] == reference_outputs["should_pass"]
    verdict = "approved" if outputs["passed"] else "rejected"
    return result("verdict_correct", int(ok), f"{verdict}" + ("" if ok else f" (wrong; expected {'approval' if reference_outputs['should_pass'] else 'rejection'}): {outputs.get('issues')}"))


def _rate(name, outputs, reference_outputs, wanted_should_pass, wrong_verdict):
    rows = [(o, r) for o, r in zip(outputs, reference_outputs) if r["should_pass"] is wanted_should_pass and not o.get("error")]
    score = (sum(o["passed"] is wrong_verdict for o, _ in rows) / len(rows)) if rows else 0
    return {"key": name, "score": score}


def _accuracy(outputs, reference_outputs):
    rows = [(o, r) for o, r in zip(outputs, reference_outputs)]
    return {"key": "review_accuracy", "score": sum(not o.get("error") and o["passed"] == r["should_pass"] for o, r in rows) / max(1, len(rows))}


def _false_approval_rate(outputs, reference_outputs):
    """Bad emails the review let through: the dangerous mistake (lower is better)."""
    return _rate("false_approval_rate", outputs, reference_outputs, False, True)


def _false_rejection_rate(outputs, reference_outputs):
    """Good emails the review blocked (lower is better)."""
    return _rate("false_rejection_rate", outputs, reference_outputs, True, False)


review = SimpleNamespace(
    NAME="outreach_review", DATASET="rawaj-outreach-review",
    DESCRIPTION="Labelled good and bad emails. Does the review model that gates sending reach the right verdict?",
    build_examples=_review_examples, target=_review_target,
    build_evaluators=lambda with_judges=True: [verdict_correct],
    summary_evaluators=[_accuracy, _false_approval_rate, _false_rejection_rate],
)


# ---------------------------------------------------------------- email writing

INTERNAL_TERMS = re.compile(r"\b(qualif\w*|score|priority|severity|gap|benchmark|engagement rate|rationale)\b", re.I)


def _email_examples() -> list[dict]:
    examples = []
    with SessionLocal() as db:
        latest = {}
        for run in db.scalars(select(ResearchRun).where(ResearchRun.status == "complete").order_by(ResearchRun.id)):
            latest[run.restaurant_id] = run
        for run in latest.values():
            signals = [s for s in (run.research_signals or []) if str(s.get("observation", "")).strip()][:2]
            if not signals:
                continue
            context = {
                "message_type": "INITIAL_OUTREACH", "action": "SEND_INITIAL_OUTREACH", "language": "en",
                "restaurant": {"name": run.restaurant.name, "location": run.restaurant.location},
                "observations": [s["observation"] for s in signals], "observation_ids": [s["signal_id"] for s in signals],
                "calendar_context": [], "calendar_event_ids": [], "latest_customer_message": None,
                "dashboard_access_available": False, "complimentary_free_trial": True,
            }
            examples.append({
                "key": f"research_run_{run.id}", "inputs": {"context": context},
                "outputs": {"restaurant": run.restaurant.name, "observations": context["observations"]},
                "metadata": {"restaurant": run.restaurant.name},
            })
    return examples


def _email_target(inputs: dict) -> dict:
    try:
        service = _service()
        proposal = service.generate_email(inputs["context"])
        subject, body = proposal.subject, proposal.plain_text_body
        review = service.review_email(
            draft=_initial(subject, body),
            customer_safe_context={
                "restaurant": inputs["context"]["restaurant"],
                "observations": [{"id": i, "text": t} for i, t in zip(inputs["context"]["observation_ids"], inputs["context"]["observations"])],
                "observation_ids": inputs["context"]["observation_ids"], "calendar_context": [], "calendar_event_ids": [],
            },
        )
        review = review.model_dump() if hasattr(review, "model_dump") else review
        return {"subject": subject, "body": body, "review_passed": bool(review["passed"] and review["privacy_safe"]),
                "review_issues": review.get("issues"), "error": None}
    except Exception as error:
        return {"subject": None, "body": None, "review_passed": None, "review_issues": [], "error": f"{type(error).__name__}: {error}"}


def no_links(body: str) -> dict:
    """The model writes no URL; trusted code adds the links after it."""
    return result("no_links", int(not re.search(r"https?://|www\.", body or "", re.I)), "" if not re.search(r"https?://|www\.", body or "", re.I) else "the email contains a link")


def no_internal_terms(body: str) -> dict:
    """The email does not use analysis vocabulary (qualification, score, priority, severity, gap...)."""
    found = sorted({m.group(0).lower() for m in INTERNAL_TERMS.finditer(body or "")})
    return result("no_internal_terms", int(not found), f"uses {found}" if found else "")


def names_the_restaurant(body: str, restaurant: str) -> dict:
    return result("names_the_restaurant", int(restaurant.split()[0].lower() in (body or "").lower()), "" if restaurant.split()[0].lower() in (body or "").lower() else f"does not mention {restaurant}")


def mentions_free_trial(body: str) -> dict:
    ok = "free trial" in (body or "").lower()
    return result("mentions_free_trial", int(ok), "" if ok else "the Free Trial offer is missing")


def concise(body: str, limit: int = 140) -> dict:
    words = len((body or "").split())
    return result("concise", int(words <= limit), f"{words} words")


def _email_code(outputs: dict, reference_outputs: dict) -> list[dict]:
    if outputs.get("error"):
        return [result(key, 0, f"the agent failed: {outputs['error']}") for key in ("no_links", "no_internal_terms", "names_the_restaurant", "mentions_free_trial", "concise", "passes_review")]
    body = outputs["body"]
    return [
        no_links(body), no_internal_terms(body), names_the_restaurant(body, reference_outputs["restaurant"]),
        mentions_free_trial(body), concise(body),
        result("passes_review", int(bool(outputs["review_passed"])), "; ".join(outputs.get("review_issues") or []) or "the review model approved it"),
    ]


def _email_evaluators(with_judges: bool = True) -> list:
    def code_checks(outputs: dict, reference_outputs: dict) -> dict:
        return {"results": _email_code(outputs, reference_outputs)}

    evaluators = [code_checks]
    if with_judges:
        judge = judge_llm()
        text = lambda o: f"Subject: {o['subject']}\n\n{o['body']}"
        evaluators += [
            make_judge("personalization_grounded",
                       "The input holds the only facts the email may use (the observations). Does the email use ONLY those facts, "
                       "without inventing anything about the restaurant, and does it use at least one of them?",
                       judge, input_of=lambda i: i["context"], output_of=text),
            make_judge("tone_and_clarity",
                       "Is this first email to a restaurant owner warm, professional and concise, a real introduction of Rawaj, "
                       "with a clear invitation to answer, and no pressure or exaggerated promises?",
                       judge, input_of=lambda i: i["context"], output_of=text),
        ]
    return evaluators


email = SimpleNamespace(
    NAME="outreach_email", DATASET="rawaj-outreach-email",
    DESCRIPTION="Restaurants' saved research -> the first email the generation model writes, then the review model's verdict on it.",
    build_examples=_email_examples, target=_email_target, build_evaluators=_email_evaluators, summary_evaluators=[],
)


# ---------------------------------------------------------------- next-action decision

NOW = "2026-09-27T12:00:00+00:00"
OBSERVATIONS = [{"id": "obs_1", "observation": "No new posts were seen in the last 60 days."}]


def _memory(status, attempts, last_outbound_at=None, next_contact_at=None):
    return {
        "relationship_id": "rel_1", "restaurant_id": 3, "status": status, "outreach_attempts": attempts,
        "last_outbound_at": last_outbound_at, "next_contact_at": next_contact_at, "promotional_contact_allowed": True,
        "feedback": [], "issues": [], "important_context": [],
    }


def _decision_context(trigger, memory):
    return {
        "trigger": trigger, "now": NOW, "qualification_status": "QUALIFIED", "relationship_memory": memory,
        "research_observations": OBSERVATIONS, "calendar_events": [], "calendar_checked": True, "strategy_context": {},
        "follow_up_max_attempts": 3, "client_message": None,
    }


DECISION_CASES = [
    ("first_contact", "START_OUTREACH", _memory("READY_TO_CONTACT", 0), {"SEND_INITIAL_OUTREACH"}),
    ("duplicate_start_an_hour_after_sending", "START_OUTREACH",
     _memory("WAITING_FOR_RESPONSE", 1, "2026-09-27T11:00:00+00:00", "2026-10-04T11:00:00+00:00"), {"WAIT_FOR_RESPONSE", "CONTACT_LATER"}),
    ("follow_up_due_but_first_email_sent_seconds_ago", "FOLLOW_UP_DUE",
     _memory("WAITING_FOR_RESPONSE", 1, "2026-09-27T11:59:30+00:00", "2026-09-26T12:00:00+00:00"), {"WAIT_FOR_RESPONSE", "CONTACT_LATER"}),
    ("follow_up_due_after_a_week_of_silence", "FOLLOW_UP_DUE",
     _memory("WAITING_FOR_RESPONSE", 1, "2026-09-19T12:00:00+00:00", "2026-09-26T12:00:00+00:00"), {"SEND_FOLLOW_UP"}),
    ("second_follow_up_after_another_week", "FOLLOW_UP_DUE",
     _memory("WAITING_FOR_RESPONSE", 2, "2026-09-19T12:00:00+00:00", "2026-09-26T12:00:00+00:00"), {"SEND_FOLLOW_UP"}),
]


def _decision_examples() -> list[dict]:
    return [
        {"key": key, "inputs": {"context": _decision_context(trigger, memory)}, "outputs": {"acceptable_actions": sorted(actions)},
         "metadata": {"trigger": trigger}}
        for key, trigger, memory, actions in DECISION_CASES
    ]


def _decision_target(inputs: dict) -> dict:
    try:
        decision = _service().decide(inputs["context"])
        return {"action": decision.action.value, "basis": decision.decision_basis, "error": None}
    except Exception as error:
        return {"action": None, "basis": [], "error": f"{type(error).__name__}: {error}"}


def action_correct(outputs: dict, reference_outputs: dict) -> dict:
    """The decision model picks an acceptable action for this situation."""
    if outputs.get("error"):
        return result("action_correct", 0, outputs["error"])
    ok = outputs["action"] in reference_outputs["acceptable_actions"]
    return result("action_correct", int(ok), f"chose {outputs['action']} (acceptable: {reference_outputs['acceptable_actions']}); {outputs['basis'][:1]}")


decision = SimpleNamespace(
    NAME="outreach_decision", DATASET="rawaj-outreach-decision",
    DESCRIPTION="Situations of a restaurant relationship -> the next action the decision model chooses.",
    build_examples=_decision_examples, target=_decision_target,
    build_evaluators=lambda with_judges=True: [action_correct], summary_evaluators=[],
)
