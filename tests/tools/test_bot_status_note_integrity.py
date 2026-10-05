"""Deferred completions survive batching, damaged siblings and receipt retries."""
import json
import time
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
from tests.hermes_cli.test_quiet_mailbox import seed
from hermes_cli.quiet_mailbox import QuietMailbox
from tools import bot_live_delivery as mailbox
from tools.bot_delivery_outbox import register_outgoing, poll_outgoing
from tools.bot_receipt_policy import _record_status_note, take_status_note


def seed_notes(home, count):
    home.mkdir(parents=True, exist_ok=True)
    with SessionDB(db_path=home / "state.db") as db:
        db.create_session(session_id="chat", source="cli")
    directory = home / "runtime/bot_delivery_status"
    directory.mkdir(parents=True)
    ids = [format(i + 1, "064x") for i in range(count)]
    for i, task_id in enumerate(ids):
        (directory / f"{task_id}.json").write_text(json.dumps(dict(
            delivery_id=task_id, receipt_id=format(i + 100, "064x"), sender_session="chat",
            recipient="peer", recorded_at=time.time_ns() + i)), encoding="utf-8")
    return directory, ids


def test_overflow_is_pending_across_reopen_and_profile_switch(tmp_path):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    directory, ids = seed_notes(a, 25)
    _, bids = seed_notes(b, 1)
    first = take_status_note(a, "chat")  # Reader opens/closes SQLite on every invocation.
    shown = {task_id for task_id in ids if task_id in first}
    assert len(shown) == 20
    assert sum("delivered_at" in json.loads(p.read_text()) for p in directory.glob("*.json")) == len(shown)
    assert bids[0] in take_status_note(b, "chat")
    with SessionDB(db_path=a / "state.db") as db:
        db.end_session("chat", "compression")
        db.create_session("tip", source="cli", parent_session_id="chat")
    assert take_status_note(a, "chat") == ""
    second = take_status_note(a, "tip")
    assert all(task_id in second for task_id in set(ids) - shown)
    assert all(task_id not in second for task_id in shown)
    assert take_status_note(a, "tip") == ""
    assert take_status_note(b, "chat") == ""


@pytest.mark.parametrize("damage", [
    {"delivery_id": None}, {"delivery_id": "f" * 64}, {"receipt_id": "bad"},
    {"sender_session": []}, {"recipient": {}}, {"recorded_at": "yesterday"},
    {"recorded_at": True}, {"recorded_at": -1}, {"delivered_at": "bad"},
])
def test_bad_note_cannot_consume_healthy_sibling(tmp_path, damage):
    directory, ids = seed_notes(tmp_path, 2)
    bad_path = directory / f"{ids[0]}.json"
    note = json.loads(bad_path.read_text())
    note.update(damage)
    if note["delivery_id"] is None:
        del note["delivery_id"]
    before = json.dumps(note)
    bad_path.write_text(before, encoding="utf-8")
    text = take_status_note(tmp_path, "chat")
    assert ids[1] in text and ids[0] not in text
    assert take_status_note(tmp_path, "chat") == ""
    assert bad_path.read_text(encoding="utf-8") == before


def test_extreme_timestamp_is_display_safe(tmp_path):
    directory, ids = seed_notes(tmp_path, 2)
    path = directory / f"{ids[0]}.json"
    note = json.loads(path.read_text())
    note["recorded_at"] = 10 ** 1000
    path.write_text(json.dumps(note), encoding="utf-8")
    text = take_status_note(tmp_path, "chat")
    assert all(task_id in text for task_id in ids)
    assert "time unknown" in text
    assert take_status_note(tmp_path, "chat") == ""


def test_partial_stamp_failure_preserves_remaining_notes(tmp_path, monkeypatch):
    import utils
    directory, ids = seed_notes(tmp_path, 3)
    real_write = utils.atomic_json_write
    writes = []

    def fail_second(path, data, **kwargs):
        writes.append(path)
        if len(writes) == 2:
            raise OSError("synthetic disk failure before write")
        real_write(path, data, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(utils, "atomic_json_write", fail_second)
        first = take_status_note(tmp_path, "chat")
    assert ids[0] in first and all(task_id not in first for task_id in ids[1:])
    assert all("delivered_at" not in json.loads((directory / f"{i}.json").read_text()) for i in ids[1:])
    second = take_status_note(tmp_path, "chat")
    assert ids[0] not in second and all(task_id in second for task_id in ids[1:])
    assert take_status_note(tmp_path, "chat") == ""


@pytest.mark.parametrize("kind", ["truncated", "delivery", "receipt", "sender", "timestamp", "directory", "pending", "delivered"])
def test_existing_note_must_be_valid_before_passive_settlement(tmp_path, kind):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "one synthetic task")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
        effects = []
        assert wb.drain_one(lambda *_: effects.append("one effect") or {"final_response": "NO_REPLY"})
        poll_outgoing(a, "chat")
        index = json.loads((a / "runtime/bot_delivery_outbox" / f"{ticket['id']}.json").read_text())
        receipt_id = index["receipt_queued"]
        _record_status_note(a, ticket["id"], receipt_id)
        path = a / "runtime/bot_delivery_status" / f"{ticket['id']}.json"
        if kind == "truncated":
            path.write_text("{", encoding="utf-8")
        elif kind == "directory":
            path.unlink()
            path.mkdir()
        else:
            record = json.loads(path.read_text())
            if kind == "delivery": record["delivery_id"] = "f" * 64
            if kind == "receipt": record["receipt_id"] = "f" * 64
            if kind == "sender": record["sender_session"] = "other"
            if kind == "timestamp": record["recorded_at"] = []
            if kind == "delivered": record["delivered_at"] = time.time_ns()
            path.write_text(json.dumps(record), encoding="utf-8")
        before = path.read_bytes() if path.is_file() else None
        turns = []
        assert wa.drain_one(lambda text, author: turns.append(text) or {"final_response": "received"})
        result = mailbox.read_delivery_result(a, receipt_id)
        assert result["status"] == "settled"
        if kind in ("pending", "delivered"):
            assert not turns and result["reason"] == "status_only_receipt"
            assert path.read_bytes() == before  # Retry never clears an existing delivered stamp.
            note = take_status_note(a, "chat")
            assert (ticket["id"] in note) == (kind == "pending")
            assert take_status_note(a, "chat") == ""
        else:
            assert len(turns) == 1 and ticket["id"] in turns[0]
            assert result.get("reason") != "status_only_receipt"
            assert (path.read_bytes() if path.is_file() else None) == before
        poll_outgoing(a, "chat")
        assert not wb.drain_one(lambda *_: effects.append("unexpected replay"))
        assert effects == ["one effect"]
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_a_bad_note_is_reported_once_not_on_every_turn(tmp_path, caplog):
    """Every Bot Chat turn rescans the directory; one corrupt record must not flood errors.log."""
    import logging

    directory, ids = seed_notes(tmp_path, 1)
    (directory / ("e" * 64 + ".json")).write_text("{", encoding="utf-8")
    caplog.set_level(logging.DEBUG, logger="tools.bot_receipt_policy")
    assert ids[0] in take_status_note(tmp_path, "chat")
    for _ in range(3):
        assert take_status_note(tmp_path, "chat") == ""
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING and "status note" in r.getMessage()]
    assert len(warnings) == 1
    assert (directory / ("e" * 64 + ".json")).read_text(encoding="utf-8") == "{"  # Evidence retained.


def test_busy_notes_lock_falls_back_to_a_receipt_turn_within_its_budget(tmp_path, monkeypatch):
    """The TUI poller records notes inside the admission guard: a held lock must cost a bounded
    wait and an ordinary receipt turn, never an open-ended stall (POSIX flock has no timeout)."""
    import threading
    from hermes_cli.active_sessions import _FileLock
    from tools import bot_receipt_policy

    monkeypatch.setattr(bot_receipt_policy, "_NOTE_LOCK_WAIT_S", 0.3)
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    held, release = threading.Event(), threading.Event()
    notes = a / "runtime/bot_delivery_status"
    notes.mkdir(parents=True)

    def hold():
        with _FileLock(notes / ".lock"):  # A reader's lock: same byte, other handle.
            held.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "task")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
        assert wb.drain_one(lambda *_: {"final_response": "NO_REPLY"})
        poll_outgoing(a, "chat")
        holder.start()
        assert held.wait(5)
        turns = []
        started = time.monotonic()
        assert wa.drain_one(lambda text, author: turns.append(text) or {"final_response": "ok"})
        assert time.monotonic() - started < 5
        assert len(turns) == 1 and ticket["id"] in turns[0]  # Not lost: the model saw the receipt.
        assert not (notes / f"{ticket['id']}.json").exists()
    finally:
        release.set()
        if holder.is_alive():
            holder.join(10)
        la.release(); lb.release(); dba.close(); dbb.close()


def test_operator_switch_restores_a_receipt_turn_for_silent_completions(tmp_path, monkeypatch):
    """bot_mode.wake_on_silent_completion: true reverts to waking the sender, without a code rollback."""
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    monkeypatch.setenv("HERMES_HOME", str(a))  # The sender's profile scope reads its own config.
    (a / "config.yaml").write_text("bot_mode:\n  wake_on_silent_completion: true\n", encoding="utf-8")
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "task")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
        assert wb.drain_one(lambda *_: {"final_response": "NO_REPLY"})
        poll_outgoing(a, "chat")
        turns = []
        assert wa.drain_one(lambda text, author: turns.append(text) or {"final_response": "NO_REPLY"})
        assert len(turns) == 1 and ticket["id"] in turns[0]
        assert not (a / "runtime/bot_delivery_status" / f"{ticket['id']}.json").exists()
        assert take_status_note(a, "chat") == ""
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()
