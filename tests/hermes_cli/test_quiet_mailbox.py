import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli.active_sessions import try_acquire_active_session, session_already_owned_message
from hermes_cli.quiet_mailbox import QuietMailbox, DELIVERY_ID_ENV
from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox


def seed(home):
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(session_id="chat", surface="cli", config={},
                                               registry_home=home, metadata={"live_session_id": "chat"})
    assert refusal is None
    cli = SimpleNamespace(_active_session_lease=lease, session_id="chat")
    return db, lease, cli


def test_cli_consumes_fifo_with_original_authors_and_durable_receipts(tmp_path):
    db, lease, cli = seed(tmp_path)
    try:
        worker = QuietMailbox(cli, {"name": "A", "is_bot": True})
        owner = mailbox.find_canonical_live_owner(tmp_path)
        author = {"id": "bot:a", "name": "A", "is_bot": True}
        tickets = [mailbox.deliver_to_live_owner(tmp_path, owner, s, author=author) for s in ["one", "two"]]
        turns = []
        def run(text, sender):
            turns.append((text, sender))
            return {"final_response": "answer:" + text}
        assert worker.drain_one(run)
        assert worker.drain_one(run)
        assert not worker.drain_one(run)
        assert turns == [("one", author), ("two", author)]
        assert [mailbox.read_delivery_result(tmp_path, t["id"])["reply"] for t in tickets] == ["answer:one", "answer:two"]
        detail = session_already_owned_message("chat", mailbox.find_canonical_owner(tmp_path))
        assert "background assignment from A" in detail
        assert "not accepted" in detail
    finally:
        lease.release()
        db.close()


def test_crashed_execution_is_not_replayed_by_new_cli_owner(tmp_path):
    db, lease, cli = seed(tmp_path)
    try:
        worker = QuietMailbox(cli, None)
        ticket = mailbox.deliver_to_live_owner(tmp_path, mailbox.find_canonical_live_owner(tmp_path), "one effect")
        effects = []
        def crash(text, sender):
            effects.append(text)
            raise RuntimeError("crash after effect")
        with pytest.raises(RuntimeError, match="after effect"):
            worker.drain_one(crash)
        lease.release()
        lease, refusal = try_acquire_active_session(session_id="chat", surface="cli", config={}, registry_home=tmp_path,
                                                   metadata={"live_session_id": "replacement"})
        assert refusal is None
        resumed = QuietMailbox(SimpleNamespace(_active_session_lease=lease, session_id="chat"), None)
        assert not resumed.drain_one(crash)
        assert effects == ["one effect"]
        assert mailbox.read_delivery_result(tmp_path, ticket["id"])["status"] == "claimed"
    finally:
        lease.release()
        db.close()


def synthetic_worker(tmp_path):
    """Real CLI/lease/DB entry point; only the model boundary is deterministic."""
    script = tmp_path / "worker.py"
    script.write_text('''
import os
from pathlib import Path
from types import SimpleNamespace
from hermes_state import SessionDB
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_cli.quiet_mailbox import DELIVERY_ID_ENV
import cli
from hermes_cli import quiet_single_query
home = Path(os.environ["HERMES_HOME"])
db = SessionDB(db_path=home/"state.db")
lease, refusal = try_acquire_active_session(session_id="chat", surface="cli", config={}, registry_home=home,
                                          metadata={"live_session_id":"worker"})
if refusal:
    raise SystemExit(75)
def run_conversation(user_message, conversation_history, **kwargs):
    assert DELIVERY_ID_ENV not in os.environ
    db.append_message("chat", "user", user_message)
    db.append_message("chat", "assistant", "synthetic result")
    return {"final_response":"synthetic result", "messages": db.get_messages_as_conversation("chat")}
quiet_single_query.quiet_notify_linger_seconds = lambda: 0
agent = SimpleNamespace(run_conversation=run_conversation, session_id="chat")
the_cli = SimpleNamespace(agent=agent, session_id="chat", conversation_history=[], _active_session_lease=lease)
try:
    cli._run_quiet_single_query(the_cli, "MUST NOT EXECUTE THIS FRESH COPY")
finally:
    lease.release()
    db.close()
''', encoding="utf-8")
    return script


def test_actual_quiet_entrypoint_recovers_retained_ticket_in_child_process(tmp_path):
    home = tmp_path / "profiles" / "b"
    db, lease, cli = seed(home)
    worker = QuietMailbox(cli, None)
    ticket = mailbox.deliver_to_live_owner(home, mailbox.find_canonical_live_owner(home), "synthetic current task")
    lease.release()
    script = synthetic_worker(tmp_path)
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_TEST_ISOLATION="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    env[DELIVERY_ID_ENV] = ticket["id"]
    try:
        first = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
        assert first.returncode == 0, first.stderr
        second = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
        assert second.returncode == 0, second.stderr
        assert mailbox.read_delivery_result(home, ticket["id"])["status"] == "settled"
        assert [r["content"] for r in db.get_messages("chat") if r["role"] == "user"] == ["synthetic current task"]
    finally:
        db.close()


def test_native_competing_workers_execute_each_ticket_once(tmp_path):
    home = tmp_path / "profiles" / "recipient"
    db, lease, cli = seed(home)
    QuietMailbox(cli, None)
    owner = mailbox.find_canonical_live_owner(home)
    tickets = [mailbox.deliver_to_live_owner(home, owner, f"task-{i:02d}") for i in range(12)]
    lease.release()
    script = synthetic_worker(tmp_path)
    env = dict(os.environ, HERMES_HOME=str(home), HERMES_TEST_ISOLATION="1",
               PYTHONPATH=str(Path(__file__).resolve().parents[2]))
    env[DELIVERY_ID_ENV] = tickets[0]["id"]
    children = [subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(3)]
    try:
        for child in children:
            stdout, stderr = child.communicate(timeout=60)
            assert child.returncode in (0, 75), stderr  # another actual writer may own the lease
        # The bounded linger may leave a FIFO tail; a subsequent consumer drains it.
        tail = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
        assert tail.returncode == 0, tail.stderr
        assert [mailbox.read_delivery_result(home, ticket["id"])["status"] for ticket in tickets] == ["settled"] * 12
        assert [r["content"] for r in db.get_messages("chat") if r["role"] == "user"] == [f"task-{i:02d}" for i in range(12)]
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)
        db.close()
