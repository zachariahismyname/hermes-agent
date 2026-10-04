import contextlib
import json
import os
import time
from pathlib import Path

import pytest

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tools import bot_mode_dm as dm, bot_live_delivery as mailbox
from tools.bot_failure_reasons import session_ownership_refused


@pytest.mark.parametrize(('code', 'stderr', 'safe'), [
    (1, 'hermes-refusal-reason: SESSION_NOT_OWNED', True),
    (0, 'hermes-refusal-reason: SESSION_NOT_OWNED', False),
    (1, 'already has a live owner', False),
    (1, 'hermes-refusal-reason: NEW_REASON\nalready has a live owner', False),
])
def test_only_explicit_pre_execution_refusal_permits_handoff(code, stderr, safe):
    assert session_ownership_refused(code, stderr) is safe


def test_owner_appears_while_waiting_and_runner_uses_mailbox_without_cli(tmp_path, monkeypatch, capsys):
    db = SessionDB(db_path=tmp_path/'state.db')
    db.create_session(session_id='chat', source='cli')
    db.set_session_title('chat', 'Bot Chat')
    leases = []
    @contextlib.contextmanager
    def waiting(*args, **kwargs):
        lease, refusal = try_acquire_active_session(session_id='chat', surface='desktop', config={},
            registry_home=tmp_path, metadata={'live_session_id':'live', 'bot_live_delivery_consumer':True})
        assert refusal is None
        leases.append(lease)
        yield
    monkeypatch.setattr(dm, '_delivery_lock', waiting)
    monkeypatch.setattr(dm, '_LIVE_WAIT_SECONDS', 0)
    def no_cli(*args, **kwargs):
        raise AssertionError('CLI must not compete with the new live owner')
    monkeypatch.setattr(dm, '_run_local_turn', no_cli)
    payload = tmp_path/'message.txt'
    payload.write_text('safe after wait', encoding='utf-8')
    try:
        assert dm._run_delivery([], str(payload), stdin_file=False, profile_home=tmp_path) == 0
        assert json.loads(capsys.readouterr().out)['status'] == 'queued'
        assert mailbox.read_delivery_result(tmp_path, dm._dm_delivery_id(payload))['message'] == 'safe after wait'
        assert payload.exists()
    finally:
        for lease in leases: lease.release()
        db.close()


def test_uncertain_transport_survives_cleanup_and_is_not_reexecuted(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(dm.tempfile, 'gettempdir', lambda: str(tmp_path))
    payload = Path(dm._write_dm_file('unknown outcome'))
    calls = []
    monkeypatch.setattr(dm, '_run_local_turn', lambda *a, **k: calls.append(1) or 7)
    assert dm._run_delivery([], str(payload), stdin_file=False) == 7
    stale = time.time() - dm._DM_STALE_SECONDS - 100
    os.utime(payload, (stale, stale))
    assert dm.cleanup_bot_dm_cache() == 0
    assert payload.read_text() == 'unknown outcome'
    assert dm._run_delivery([], str(payload), stdin_file=False) == 1
    assert len(calls) == 1
    assert json.loads(capsys.readouterr().out)['status'] == 'ambiguous'


def test_concurrent_runners_cannot_execute_the_same_payload_twice(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
    from threading import Barrier, Event, Lock

    payload = tmp_path / 'message.txt'
    payload.write_text('one execution', encoding='utf-8')
    discovery = Barrier(2)
    release = Event()
    guard = Lock()
    discoveries = executions = 0

    def no_owner(*args, **kwargs):
        nonlocal discoveries
        with guard:
            discoveries += 1
            first_check = discoveries <= 2
        if first_check:
            discovery.wait(timeout=10)
        return None

    def turn(*args, **kwargs):
        nonlocal executions
        with guard:
            executions += 1
        assert release.wait(timeout=10)
        return 0

    monkeypatch.setattr(dm, '_admit_live_dm', no_owner)
    monkeypatch.setattr(dm, '_run_local_turn', turn)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(dm._run_delivery, [], str(payload), stdin_file=False,
                               profile_home=tmp_path) for _ in range(2)]
        try:
            done, _ = wait(futures, timeout=5, return_when=FIRST_COMPLETED)
            assert len(done) == 1 and next(iter(done)).result() == 1
        finally:
            release.set()
        assert sorted(f.result(timeout=10) for f in futures) == [0, 1]
    assert executions == 1
