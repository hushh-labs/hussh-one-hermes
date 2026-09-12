"""Real subprocess admission checks, isolated by the Hermes test home."""
import os
import subprocess
import sys

import pytest

from agent.local_admission import acquire_process_permit


def test_another_process_cannot_take_live_permit_and_dead_claim_is_recovered():
    script = '''
from agent.local_admission import acquire_process_permit
import sys
permit = acquire_process_permit("process-test", 1, 0, "background")
print("acquired", flush=True)
sys.stdin.read()
'''
    child = subprocess.Popen([sys.executable, "-c", script], env=os.environ.copy(),
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout.readline().strip() == "acquired"
        with pytest.raises(TimeoutError):
            acquire_process_permit("process-test", 1, 0, "interactive")
        child.kill()
        child.wait(timeout=10)
        permit = acquire_process_permit("process-test", 1, 0, "interactive")
        permit.release()
        permit.release()
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
        child.stdin.close()
        child.stdout.close()


@pytest.mark.macos_only
def test_idle_sleep_assertion_is_owned_and_released(monkeypatch):
    import hermes_cli.config
    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", lambda: {"agent": {"local_keep_awake": True}})
    permit = acquire_process_permit("awake-test", 1, 0, "interactive")
    process = permit._awake_process
    try:
        assert process is not None and process.poll() is None
    finally:
        permit.release()
    assert process.poll() is not None
    permit.release()


@pytest.mark.macos_only
def test_idle_sleep_assertion_can_be_disabled(monkeypatch):
    import hermes_cli.config
    monkeypatch.setattr(hermes_cli.config, "load_config_readonly", lambda: {"agent": {"local_keep_awake": False}})
    permit = acquire_process_permit("no-awake-test", 1, 0, "interactive")
    try:
        assert permit._awake_process is None
    finally:
        permit.release()
