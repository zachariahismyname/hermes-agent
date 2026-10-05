"""A live-DM waiter never cancels a ticket that the successor owner of the same Bot Chat will adopt.

The pinned owner's lease is gone, but another live owner already holds the canonical conversation
and claims orphaned queued tickets (claim_pending_delivery(recover_unclaimed=True)). Cancelling it
as "runtime offline" would drop a message that is about to be delivered.
"""
import json

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tools import bot_live_delivery as live
from tools import bot_mode_dm as dm


def test_waiter_keeps_a_ticket_the_successor_owner_adopts(tmp_path, monkeypatch, capsys):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(
        session_id="chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "live-b", "bot_live_delivery_consumer": True})
    assert refusal is None
    try:
        gone = dict(profile_home=str(tmp_path.resolve()), session_id="chat", lease_id="lease-a-gone",
                    live_session_id="live-a")
        record = live.deliver_to_live_owner(tmp_path, gone, "ping", delivery_id="2" * 32)
        monkeypatch.setattr(dm, "_LIVE_WAIT_SECONDS", 0.01)
        monkeypatch.setattr(dm, "_LIVE_WAIT_MAX_SECONDS", 0.3)

        assert dm._wait_live_dm(str(tmp_path), record["delivery_id"]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "queued"
        assert live.read_delivery_result(tmp_path, record["delivery_id"])["status"] == "queued"

        successor = live.find_canonical_live_owner(tmp_path)
        claimed = live.claim_pending_delivery(tmp_path, successor, recover_unclaimed=True)
        assert claimed is not None and claimed["delivery_id"] == record["delivery_id"]
    finally:
        lease.release()
        db.close()


def test_waiter_keeps_waiting_while_the_successor_runs_the_adopted_ticket(tmp_path, monkeypatch, capsys):
    import threading

    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    lease, refusal = try_acquire_active_session(
        session_id="chat", surface="desktop", config={}, registry_home=tmp_path,
        metadata={"live_session_id": "live-b", "bot_live_delivery_consumer": True})
    assert refusal is None
    try:
        gone = dict(profile_home=str(tmp_path.resolve()), session_id="chat", lease_id="lease-a-gone",
                    live_session_id="live-a")
        record = live.deliver_to_live_owner(tmp_path, gone, "ping", delivery_id="3" * 32)
        claimed = live.claim_pending_delivery(tmp_path, live.find_canonical_live_owner(tmp_path),
                                              recover_unclaimed=True)
        assert claimed is not None and live.owner_holds_delivery(tmp_path, claimed)
        monkeypatch.setattr(dm, "_LIVE_WAIT_SECONDS", 0.02)
        monkeypatch.setattr(dm, "_LIVE_WAIT_MAX_SECONDS", 10)
        outcome = []
        waiter = threading.Thread(target=lambda: outcome.append(
            dm._wait_live_dm(str(tmp_path), record["delivery_id"])), daemon=True)
        waiter.start()
        waiter.join(timeout=0.5)  # many owner-check windows while the successor still runs
        assert waiter.is_alive(), "a successor still running the adopted ticket holds it"
        live.complete_delivery(tmp_path, record["delivery_id"], status="settled", reply="pong")
        waiter.join(timeout=5)
        assert outcome == [0] and json.loads(capsys.readouterr().out)["status"] == "settled"
    finally:
        lease.release()
        db.close()
