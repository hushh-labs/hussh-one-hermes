"""Local transport errors must not discard context through compression."""
import pytest
from agent.error_classifier import classify_api_error, FailoverReason

@pytest.mark.parametrize("provider", ["lmstudio", "ollama", "local"])
def test_disconnect_is_transport_failure_even_with_large_history(provider):
    error = classify_api_error(Exception("Server disconnected without sending a response"), provider=provider, approx_tokens=110000, context_length=131072, num_messages=300)
    assert error.reason == FailoverReason.timeout
    assert not error.should_compress

def test_explicit_context_error_still_compresses():
    error = classify_api_error(Exception("maximum context length exceeded"), provider="lmstudio")
    assert error.reason == FailoverReason.context_overflow
