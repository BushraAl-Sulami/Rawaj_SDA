"""Provider diagnostics must be actionable without exposing request/response data."""
from agents.outreach_followup_agent.llm_service import LLMIntegrationError,model_failure_code
from agents.outreach_followup_agent.guardrails import OutreachGuardrails
from agency.routes import draft_failure_message
import pytest

@pytest.mark.parametrize('status,code',[(401,'MODEL_ACCESS_DENIED'),(403,'MODEL_ACCESS_DENIED'),(404,'MODEL_UNAVAILABLE'),(429,'MODEL_RATE_LIMITED'),(400,'MODEL_REQUEST_REJECTED')])
def test_provider_failure_codes(status,code):
    error=RuntimeError('private provider details')
    error.status_code=status
    assert model_failure_code(error)==code

def test_connection_failure_stays_safe_through_graph_and_api():
    class APIConnectionError(Exception): pass
    cause=APIConnectionError('private-key and private-prompt')
    outer=RuntimeError('another private value');outer.__cause__=cause
    code=model_failure_code(outer)
    assert code=='MODEL_CONNECTION_FAILED'
    state=OutreachGuardrails._safe_failure('DECISION_FAILED',LLMIntegrationError('private',diagnostic_code=code))
    message=draft_failure_message(state['errors'])
    assert 'cannot reach' in message and 'No email was sent' in message
    assert 'private' not in str(state) and 'private' not in message

def test_unrecognized_code_is_not_exposed():
    error=LLMIntegrationError('secret',diagnostic_code='private-api-key')
    state=OutreachGuardrails._safe_failure('DECISION_FAILED',error)
    assert 'private-api-key' not in str(state)
