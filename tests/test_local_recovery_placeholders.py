"""Bookkeeping closures must not train the local replay to echo non-answers."""
from agent.replay_cleanup import (
    _MISSING_FINAL_RESPONSE_CLOSURE,
    _SIDE_EFFECT_RECOVERY_CLOSURE,
    strip_completed_recovery_placeholders,
)


def test_only_completed_bookkeeping_is_removed_without_mutating_history():
    receipt = {"role": "tool", "content": "exit_code=0", "tool_call_id": "committed"}
    unknown = {"role": "assistant", "content": _SIDE_EFFECT_RECOVERY_CLOSURE}
    real = {"role": "assistant", "content": "Explanation: " + _MISSING_FINAL_RESPONSE_CLOSURE}
    user = {"role": "user", "content": "Run the outstanding tests"}
    history = [receipt, {"role": "assistant", "content": "\n".join([_MISSING_FINAL_RESPONSE_CLOSURE] * 2)}, unknown, real, user]
    assert strip_completed_recovery_placeholders(history) == [receipt, unknown, real, user]
    assert len(history) == 5
