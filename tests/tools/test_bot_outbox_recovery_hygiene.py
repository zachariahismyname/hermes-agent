"""Recovery transport caches are not indexes; concurrent recovery is an idle skip."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

from tools import bot_delivery_outbox as outbox


def test_only_canonical_indexes_are_read_and_corruption_is_still_reported(tmp_path, caplog):
    delivery_id = "a" * 64
    canonical = tmp_path / (delivery_id + ".json")
    canonical.write_text(json.dumps({"sender_session": "chat", "delivery_id": delivery_id}))
    sidecar = tmp_path / (delivery_id + ".recovery.txt.live.json")
    sidecar.write_text("transient cache")
    paths = list(tmp_path.glob("*.json"))
    sidecar.unlink()  # The transport cleans up after enumeration, before index reading.
    assert list(outbox._indexes(paths)) == [(canonical, json.loads(canonical.read_text()))]
    assert not caplog.records
    canonical.write_text("not valid JSON")
    assert list(outbox._indexes([canonical])) == []
    assert canonical.exists()
    assert "Unreadable delivery index retained" in caplog.text


def test_native_concurrent_recovery_skips_busy_runner_and_releases_lock(tmp_path):
    """Two independent interpreters, native file lock, actual recovery transport writes.

    Only the agent subprocess is replaced with a barrier-controlled side effect;
    no provider, user's profile, or live agent is invoked.
    """
    home = tmp_path / "home"
    home.mkdir()
    entered, release = tmp_path / "entered", tmp_path / "release"
    worker = tmp_path / "worker.py"
    worker.write_text('''
import json, sys, time
from pathlib import Path
from tools import bot_delivery_outbox as outbox, bot_live_delivery as mailbox
home, entered, release = map(Path, sys.argv[1:])
delivery_id = "a" * 64
mailbox.read_delivery_result = lambda *_: dict(status="queued", owner={}, message="original payload", delivery_id=delivery_id)
original_run = outbox.subprocess.run
def agent_run(*args, **kwargs):
    if "--run-delivery" not in args[0]:
        return original_run(*args, **kwargs)
    with (home / "effects").open("a") as stream:
        stream.write("one effect\\n")
    entered.write_text("ready")
    deadline = time.monotonic() + 30
    while not release.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("test barrier timeout")
        time.sleep(.02)
outbox.subprocess.run = agent_run
outbox._recover_in_background(home, dict(delivery_id=delivery_id, recipient_home=str(home), argv=[]))
deadline = time.monotonic() + 35
while outbox._workers and time.monotonic() < deadline:
    time.sleep(.02)
assert not outbox._workers
''', encoding="utf-8")
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_RUNTIME_DIR=str(tmp_path / "runtime"),
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    argv = [sys.executable, str(worker), str(home), str(entered), str(release)]
    children = []
    try:
        first = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(first)
        deadline = time.monotonic() + 15
        while not entered.exists() and time.monotonic() < deadline and first.poll() is None:
            time.sleep(.02)
        assert entered.exists(), "first runner did not acquire the lock"
        second = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(second)
        _, errors = second.communicate(timeout=5)
        assert second.returncode == 0 and "Delivery recovery failed" not in errors
        assert first.poll() is None
        assert (home / "effects").read_text().splitlines() == ["one effect"]
        # Keep the real lock beyond msvcrt.LK_LOCK's ten-second retry window.
        with pytest.raises(subprocess.TimeoutExpired):
            first.wait(timeout=11)
        release.write_text("continue")
        _, errors = first.communicate(timeout=10)
        assert first.returncode == 0 and "Delivery recovery failed" not in errors
        # A later sweep can claim the very same lock after the first worker exits.
        (home / "runtime/bot_delivery_outbox" / ("a" * 64 + ".runner.lock.attempt")).unlink()
        third = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        children.append(third)
        _, errors = third.communicate(timeout=10)
        assert third.returncode == 0 and "Delivery recovery failed" not in errors
        assert (home / "effects").read_text().splitlines() == ["one effect", "one effect"]
    finally:
        release.write_text("continue")
        for child in children:
            try:
                child.communicate(timeout=12)
            except subprocess.TimeoutExpired:
                child.kill()
                child.communicate(timeout=5)


def test_torn_attempt_marker_does_not_block_recovery_forever(tmp_path, monkeypatch):
    """Older code wrote the marker non-atomically; a torn one must read as stale, not raise every sweep."""
    from tools import bot_live_delivery as mailbox

    delivery_id = "b" * 64
    home = tmp_path / "home"
    directory = home / "runtime" / "bot_delivery_outbox"
    directory.mkdir(parents=True)
    attempt = directory / f"{delivery_id}.runner.lock.attempt"
    attempt.write_text("")
    monkeypatch.setattr(mailbox, "read_delivery_result", lambda *_: dict(
        status="queued", owner={}, message="original payload", delivery_id=delivery_id))
    runs = []
    monkeypatch.setattr(outbox.subprocess, "run", lambda argv, **_kw: runs.append(argv))
    outbox._recover_in_background(home, dict(delivery_id=delivery_id, recipient_home=str(home), argv=[]))
    deadline = time.monotonic() + 10
    while outbox._workers and time.monotonic() < deadline:
        time.sleep(.02)
    assert len(runs) == 1 and "--run-delivery" in runs[0]
    assert time.time() - float(attempt.read_text(encoding="utf-8")) < 60
