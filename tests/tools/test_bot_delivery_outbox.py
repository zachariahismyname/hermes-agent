import json
from types import SimpleNamespace

from hermes_cli.quiet_mailbox import QuietMailbox
from tests.hermes_cli.test_quiet_mailbox import seed
from tools import bot_live_delivery as mailbox
from tools.bot_delivery_outbox import register_outgoing, poll_outgoing, has_pending_outgoing


def test_native_runner_recovers_ownerless_target_and_sender_after_waiter_exit(tmp_path, monkeypatch):
    """Two real child processes, two homes, no original waiter or Desktop consumer."""
    import sys
    import time
    from pathlib import Path
    from tests.hermes_cli.test_quiet_mailbox import synthetic_worker
    from tools import bot_mode_dm as dm, bot_delivery_outbox as outbox

    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    worker = synthetic_worker(tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("HERMES_RUNTIME_DIR", str(tmp_path / "runtime"))
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    payload = tmp_path / "retained.txt"
    payload.write_text("execute once", encoding="utf-8")
    try:
        QuietMailbox(cb, None)
        ticket = dm._admit_live_dm(b, str(payload))
        argv = [sys.executable, str(worker)]
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, argv, payload, None)
        index = a / "runtime/bot_delivery_outbox" / (ticket["id"] + ".json")
        record = json.loads(index.read_text())
        # Test launcher substitutes a deterministic model worker for hermes.exe;
        # the recovery transport and child CLI entry point are production code.
        record["sender_argv"] = argv
        index.write_text(json.dumps(record))
        poll_outgoing(a, "chat")
        assert mailbox.read_delivery_result(b, ticket["id"])["status"] == "queued"
        payload.unlink()
        Path(str(payload) + ".live.json").unlink()  # Temporary cache loss must not lose accepted work.
        la.release(); lb.release()  # The original sender, target and waiter are gone.
        deadline = time.monotonic() + 70
        receipt = None
        while time.monotonic() < deadline:
            outbox.sweep_install_outgoing(a)  # No per-session poller is present after restart.
            state = json.loads(index.read_text())
            if state.get("receipt_queued"):
                receipt = mailbox.read_delivery_result(a, state["receipt_queued"])
                if receipt and receipt["status"] == "settled":
                    break
            time.sleep(0.05)
        assert mailbox.read_delivery_result(b, ticket["id"])["status"] == "settled"
        assert receipt and receipt["status"] == "settled"
        assert [m["content"] for m in dbb.get_messages("chat") if m["role"] == "user"] == ["execute once"]
        assert len([m for m in dba.get_messages("chat") if m["role"] == "user"]) == 1
        assert ticket["id"] in dba.get_messages("chat")[0]["content"]
        # Drain thread cleanup before pytest removes these homes.
        while outbox._workers and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not outbox._workers
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_late_receipt_survives_sender_restart_and_repeated_polls(tmp_path):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        original = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "question")
        sender = SimpleNamespace(_session_db=dba, session_id="chat")
        assert register_outgoing(sender, original, ["hermes", "-p", "b"], tmp_path / "payload.txt", None)
        assert has_pending_outgoing(a, "chat")
        # No waiter/process registry exists. Recipient settles after sender's old writer exits.
        la.release()
        assert wb.drain_one(lambda *_: {"final_response": "late answer"})
        from hermes_cli.active_sessions import try_acquire_active_session
        la, refusal = try_acquire_active_session(session_id="chat", surface="cli", config={}, registry_home=a,
                                                 metadata={"live_session_id": "new-sender"})
        assert refusal is None
        wa = QuietMailbox(SimpleNamespace(_active_session_lease=la, session_id="chat"), None)
        poll_outgoing(a, "chat")
        poll_outgoing(a, "chat")
        assert not has_pending_outgoing(a, "chat")
        received = []
        assert wa.drain_one(lambda text, author: received.append((text, author)) or {"final_response": "recorded"})
        assert not wa.drain_one(lambda *_: (_ for _ in ()).throw(AssertionError("duplicate receipt")))
        assert len(received) == 1 and "late answer" in received[0][0]
        assert original["id"] in received[0][0]
        assert received[0][1]["is_bot"] is True  # Never manufacture a human memory turn.
        assert received[0][1]["id"] == "bot:__delivery_receipt__"
        # Recipient gets no automatic acknowledgment ticket back.
        assert not wb.drain_one(lambda *_: (_ for _ in ()).throw(AssertionError("receipt loop")))
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_lost_claim_reports_uncertainty_once_without_replay_or_hiding_late_result(tmp_path):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        wa, wb = QuietMailbox(ca, None), QuietMailbox(cb, None)
        owner = mailbox.find_canonical_live_owner(b)
        ticket = mailbox.deliver_to_live_owner(b, owner, "may have side effects")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), ticket, [], tmp_path / "unused", None)
        mailbox.claim_pending_delivery(b, owner)
        lb.release()
        messages = []
        def receive(text, author):
            messages.append(text)
            return {"final_response": "recorded"}
        poll_outgoing(a, "chat")
        poll_outgoing(a, "chat")
        assert wa.drain_one(receive)
        assert not wa.drain_one(receive)
        assert "requires_reconciliation" in messages[0]
        assert mailbox.read_delivery_result(b, ticket["id"])["status"] == "claimed"
        # A late original writer can still publish its receipt. It is distinct
        # from the one uncertainty notice and never triggers execution again.
        mailbox.complete_delivery(b, ticket["id"], status="settled", reply="late original result")
        poll_outgoing(a, "chat")
        assert wa.drain_one(receive)
        assert not wa.drain_one(receive)
        assert len(messages) == 2 and "late original result" in messages[1]
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()


def test_an_unreadable_index_keeps_the_sender_waiting_for_its_receipt(tmp_path, monkeypatch):
    """A scan that could not read an index is not proof that nothing is pending: the quiet worker
    must keep waiting (within its deadline) instead of exiting before the receipt is consumed."""
    from pathlib import Path

    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        QuietMailbox(cb, None)
        original = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "question")
        assert register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), original,
                                 ["hermes", "-p", "b"], tmp_path / "payload.txt", None)
        index = a / "runtime/bot_delivery_outbox" / (original["id"] + ".json")
        real_read_text = Path.read_text

        def contended(self, *args, **kwargs):
            if self == index:
                raise PermissionError(13, "Permission denied", str(self))
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", contended)
        assert has_pending_outgoing(a, "chat")
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()
