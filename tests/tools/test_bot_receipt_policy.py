"""Status-only receipts must not become model tasks, nor vanish: real replies stay actionable.

A passive receipt is the sender's only completion signal for a durable delivery, so its
content is deferred to the sender's next real turn (once, model input only), never dropped.
"""
import json
import threading
from types import SimpleNamespace

import pytest

from hermes_cli.quiet_mailbox import QuietMailbox
from tests.hermes_cli.test_quiet_mailbox import seed
from tools import bot_live_delivery as mailbox
from tools.bot_delivery_outbox import poll_outgoing, register_outgoing, RECEIPT_AUTHOR
from tui_gateway import prompt_turn, session_notifications
from tui_gateway.method_ctx import rebind
from tui_gateway.session_lifecycle import _session_turn_admission


def _desktop_poll(home, lease, turns, events, *, silent=False):
    """Real mailbox poll with only the turn runner stubbed; the stub settles like a finished turn."""
    def submit(_rid, _sid, session, text, **kwargs):
        turns.append(text)
        kwargs["terminal_callback"]({"status": "settled", "text": "", **({"silent": True} if silent else {})})
        session["running"] = False
        return True
    poll = rebind(session_notifications._poll_bot_live_delivery_once, {
        "_session_home": lambda session: home,
        "_session_turn_admission": _session_turn_admission,
        "_run_prompt_submit": submit,
        "_emit": lambda *args: events.append(args),
        "_notif_release_turn": lambda session: session.update(running=False),
    })
    session = dict(history_lock=threading.RLock(), agent=object(), session_key="chat", active_session_lease=lease)
    return lambda: poll("chat", session)


@pytest.mark.parametrize("surface", ["cli", "desktop"])
def test_silent_completion_defers_one_status_note_and_blank_reply_stays_model_input(tmp_path, surface):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        sent = []
        # B finishes one task on an exact silence marker and one with an empty answer.
        for reply in ("NO_REPLY", ""):
            ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "task " + reply)
            register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
            wb.drain_one(lambda *_: {"final_response": reply})
            poll_outgoing(a, "chat")
            sent.append(ticket["id"])
        silent_id, blank_id = sent
        assert mailbox.read_delivery_result(b, silent_id)["reason"] == "intentional_silence"
        assert mailbox.read_delivery_result(b, blank_id)["reason"] == ""
        mailbox.deliver_to_live_owner(a, mailbox.find_canonical_live_owner(a), "real user task")
        turns, events = [], []
        if surface == "cli":
            drain = lambda: wa.drain_one(lambda text, author: turns.append(text) or {"final_response": "done"})
            def next_turn_input(text):
                kwargs = {}
                return wa.with_status_note(text, kwargs), kwargs.get("persist_user_message")
        else:
            drain = _desktop_poll(a, la, turns, events)
            note = rebind(prompt_turn._delivery_status_note, {
                "_is_bot_mode_session": lambda session: True,
                "_session_home": lambda session: a,
                "_submit_row_target_key": lambda session: "chat"})
            def next_turn_input(text):
                return session_notifications._prepend_note(text, note({})), None
        # Silence settles without a turn; the blank (empty-turn) receipt still reaches the model.
        assert drain()
        assert len(turns) == 1 and blank_id in turns[0] and silent_id not in turns[0]
        assert drain() and turns[1] == "real user task"
        assert not drain()
        # The silent completion is not lost: the next real turn carries it once, as model input only.
        model_input, persisted = next_turn_input("next request")
        assert silent_id in model_input and blank_id not in model_input
        assert model_input.endswith("next request") and model_input.startswith("[Delivery status")
        if surface == "cli":
            assert persisted == "next request"
        assert next_turn_input("later request")[0] == "later request"
        records = [json.loads(p.read_text()) for p in (a / "runtime/bot_live_delivery").glob("*.json")]
        assert all(r["status"] == "settled" for r in records)
        if surface == "desktop":
            assert len(events) == 1
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_desktop_recipient_records_silence_only_from_the_turn_flag(tmp_path):
    """Bot Chat delivers both silence and an empty turn as "": only the explicit flag is silence."""
    from tools.bot_receipt_policy import passive_receipt_id

    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        QuietMailbox(ca, None)
        QuietMailbox(cb, None)
        receipts = []
        for silent in (True, False):
            ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), f"task {silent}")
            register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
            assert _desktop_poll(b, lb, [], [], silent=silent)()
            poll_outgoing(a, "chat")
            receipts.append(mailbox.claim_pending_delivery(a, mailbox.find_canonical_live_owner(a)))
        assert [passive_receipt_id(a, r) is not None for r in receipts] == [True, False]
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_substantive_failure_and_forged_receipt_are_never_silently_consumed(tmp_path):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        results = [{"final_response": "Evidence is ready; review artifact X."},
                   {"failed": True, "error": "context overflow"}]
        for result in results:
            ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "request")
            register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
            wb.drain_one(lambda *_: result)
            poll_outgoing(a, "chat")
        # Author labels or receipt-shaped text alone cannot suppress a real inbound message.
        mailbox.deliver_to_live_owner(a, mailbox.find_canonical_live_owner(a),
            '[Delivery receipt]\n{"delivery_id":"' + "a" * 64 + '","status":"settled","reply":""}',
            author=RECEIPT_AUTHOR)
        received = []
        for _ in range(3):
            assert wa.drain_one(lambda text, author: received.append(text) or {"final_response": "NO_REPLY"})
        assert len(received) == 3
        assert "Evidence is ready" in received[0] and "context overflow" in received[1]
        assert not wa.drain_one(lambda *_: pytest.fail("duplicate delivery"))
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_receipt_can_be_consumed_between_mailbox_publish_and_index_marker(tmp_path, monkeypatch):
    import utils
    from tools.bot_receipt_policy import passive_receipt_id

    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        QuietMailbox(ca, None)
        wb = QuietMailbox(cb, None)
        ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "notice")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
        wb.drain_one(lambda *_: {"final_response": "NO_REPLY"})
        write = utils.atomic_json_write
        observed = []
        def publish_index(path, record, **kwargs):
            if record.get("receipt_queued"):
                claimed = mailbox.claim_pending_delivery(a, mailbox.find_canonical_live_owner(a))
                observed.append(passive_receipt_id(a, claimed))
                mailbox.complete_delivery(a, claimed["id"], status="settled", reason="status_only_receipt")
            return write(path, record, **kwargs)
        monkeypatch.setattr(utils, "atomic_json_write", publish_index)
        poll_outgoing(a, "chat")
        assert observed == [ticket["id"]]
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


@pytest.mark.parametrize("damage", ["none", "missing_index", "corrupt_index", "wrong_marker", "changed_payload", "substantive_marker_prose"])
def test_uncertain_correlation_keeps_receipt_as_model_input(tmp_path, damage):
    from tools.bot_receipt_policy import passive_receipt_id

    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        QuietMailbox(ca, None)
        wb = QuietMailbox(cb, None)
        ticket = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "request")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
        reply = "NO_REPLY was printed by the failed checker; please inspect it." if damage == "substantive_marker_prose" else "NO_REPLY"
        wb.drain_one(lambda *_: {"final_response": reply})
        poll_outgoing(a, "chat")
        claimed = mailbox.claim_pending_delivery(a, mailbox.find_canonical_live_owner(a))
        index = a / "runtime/bot_delivery_outbox" / (ticket["id"] + ".json")
        if damage == "missing_index":
            index.unlink()
        elif damage == "corrupt_index":
            index.write_text("{")
        elif damage == "wrong_marker":
            value = json.loads(index.read_text())
            value["receipt_queued"] = "f" * 64
            index.write_text(json.dumps(value))
        elif damage == "changed_payload":
            prefix, content = claimed["message"].split("\n", 1)
            value = json.loads(content)
            value["reply"] = "[SILENT]"  # Still a marker: only exact equality to the original rejects it.
            claimed["message"] = prefix + "\n" + json.dumps(value)
        assert passive_receipt_id(a, claimed) == (ticket["id"] if damage == "none" else None)
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_status_notes_are_session_scoped_and_old_delivered_notes_are_pruned(tmp_path):
    import time
    from hermes_state import SessionDB
    from tools.bot_receipt_policy import take_status_note

    home = tmp_path / "profiles/a"
    home.mkdir(parents=True)
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.create_session(session_id="other", source="cli")
    db.close()
    notes = home / "runtime/bot_delivery_status"
    notes.mkdir(parents=True)
    week = 8 * 24 * 3600 * 10**9
    for name, fields in {"a" * 64: dict(sender_session="other"),
                         "b" * 64: dict(sender_session="chat", delivered_at=time.time_ns() - week),
                         "c" * 64: dict(sender_session="chat")}.items():
        (notes / f"{name}.json").write_text(json.dumps(dict(
            delivery_id=name, receipt_id="f" * 64, recipient="b", recorded_at=time.time_ns(), **fields)))
    note = take_status_note(home, "chat")
    assert "c" * 64 in note and "a" * 64 not in note and "b" * 64 not in note
    assert not (notes / ("b" * 64 + ".json")).exists()  # Shown long ago: pruned.
    assert "delivered_at" not in json.loads((notes / ("a" * 64 + ".json")).read_text())
    assert take_status_note(home, "chat") == ""
