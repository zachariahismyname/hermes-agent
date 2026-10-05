"""Mailbox polling shares one completion deadline, not one warning per slice."""
import logging
import time

import pytest

from hermes_cli import quiet_single_query as quiet
from tools import process_registry as processes


@pytest.mark.parametrize("completes", [False, True])
def test_polling_admits_mailbox_work_and_warns_only_at_overall_deadline(monkeypatch, caplog, completes):
    registry = processes.ProcessRegistry()
    session = processes.ProcessSession(id="proc_slow", command="synthetic", task_id="chat", started_at=time.time())
    session.notify_on_complete = True
    registry._running[session.id] = session
    monkeypatch.setattr(processes, "process_registry", registry)
    seen = []
    sweeps = []
    emitted = []

    def idle_work():
        sweeps.append(True)
        if len(sweeps) == 2:
            seen.append("mailbox admitted during wait")
            return {"final_response": "mailbox reply"}
        if completes and len(sweeps) == 4:
            session._completion_event.set()

    def drain(**kwargs):
        if session._completion_event.is_set() and not emitted:
            emitted.append(True)
            return [({"type": "completion", "session_key": "chat"}, "slow result")]
        return []

    monkeypatch.setattr(registry, "drain_notifications", drain)
    caplog.set_level(logging.WARNING)
    result = quiet.continue_quiet_notify_completions(
        "chat", lambda text: seen.append(text) or {"final_response": text},
        idle_work=idle_work, linger_budget=3 if completes else 1.2)
    warnings = [r for r in caplog.records if "One-shot exit linger timed out" in r.message]
    assert seen[0] == "mailbox admitted during wait"
    assert len(sweeps) >= 3
    if completes:
        assert warnings == []
        assert seen == ["mailbox admitted during wait", "slow result"]
        assert result == {"final_response": "slow result"}
    else:
        assert len(warnings) == 1
        assert "1.2s" in warnings[0].message
        assert result == {"final_response": "mailbox reply"}


def test_immediate_slice_timeouts_keep_the_mailbox_poll_cadence(monkeypatch):
    """A slice may report timed_out at once (pending interrupt, vanished detached session);
    the loop must not spin the mailbox poll (disk scans + locks) until the deadline."""
    registry = processes.ProcessRegistry()
    monkeypatch.setattr(processes, "process_registry", registry)
    monkeypatch.setattr(registry, "wait_for_pending_completions",
                        lambda *_a, **_kw: {"waited": ["p"], "completed": [], "timed_out": ["p"]})
    monkeypatch.setattr(registry, "drain_notifications", lambda **_kw: [])
    polls = []
    started = time.monotonic()
    quiet.continue_quiet_notify_completions(
        "chat", lambda text: pytest.fail("no completion to run"),
        idle_work=lambda: polls.append(time.monotonic()) and None, linger_budget=2.0)
    elapsed = time.monotonic() - started
    assert elapsed >= 1.9
    assert len(polls) <= elapsed / quiet._IDLE_POLL_SECONDS + 2
