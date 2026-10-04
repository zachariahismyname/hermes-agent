"""Lease recovery only adopts unclaimed work in the same canonical conversation."""
from pathlib import Path

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox


def _lease(home, session='chat', live='live'):
    lease, refusal = try_acquire_active_session(session_id=session, surface='desktop', config={},
        registry_home=home, metadata={'live_session_id': live, 'bot_live_delivery_consumer': True})
    assert refusal is None
    return lease


def test_restart_adopts_unclaimed_fifo_but_never_replays_claimed_or_changes_identity(tmp_path):
    db = SessionDB(db_path=tmp_path / 'state.db')
    db.create_session(session_id='chat', source='cli')
    db.set_session_title('chat', 'Bot Chat')
    first = _lease(tmp_path)
    original = mailbox.find_canonical_live_owner(tmp_path)
    claimed = mailbox.deliver_to_live_owner(tmp_path, original, 'already started')
    mailbox.claim_pending_delivery(tmp_path, original)
    queued = [mailbox.deliver_to_live_owner(tmp_path, original, message) for message in ['one', 'two']]
    first.release()
    second = _lease(tmp_path, live='restarted')
    try:
        current = mailbox.find_canonical_live_owner(tmp_path)
        assert mailbox.claim_pending_delivery(tmp_path, current) is None
        for before in queued:
            after = mailbox.claim_pending_delivery(tmp_path, current, recover_unclaimed=True)
            assert after['delivery_id'] == before['delivery_id']
            assert after['owner'] == before['owner']
            assert after['claimed_owner'] == current
            assert mailbox.deliver_to_live_owner(tmp_path, original, before['message'], delivery_id=before['delivery_id']) == after
        assert mailbox.claim_pending_delivery(tmp_path, current, recover_unclaimed=True) is None
        assert mailbox.read_delivery_result(tmp_path, claimed['delivery_id'])['owner'] == original
    finally:
        second.release()
        db.close()


def test_recovery_requires_real_owner_and_never_crosses_chat_or_profile(tmp_path):
    homes = [tmp_path/'a', tmp_path/'b']
    for home in homes:
        home.mkdir()
        db = SessionDB(db_path=home/'state.db')
        db.create_session(session_id='chat', source='cli')
        db.set_session_title('chat', 'Bot Chat')
        old = _lease(home)
        owner = mailbox.find_canonical_live_owner(home)
        ticket = mailbox.deliver_to_live_owner(home, owner, 'keep scoped')
        old.release()
        forged = dict(owner, lease_id='not-a-lease', live_session_id='fake')
        assert mailbox.claim_pending_delivery(home, forged, recover_unclaimed=True) is None
        db.set_session_title('chat', 'Archived Bot Chat')
        db.create_session(session_id='different', source='cli')
        db.set_session_title('different', 'Bot Chat')
        current = _lease(home, session='different', live='new-chat')
        try:
            assert mailbox.claim_pending_delivery(home, mailbox.find_canonical_live_owner(home), recover_unclaimed=True) is None
            assert mailbox.read_delivery_result(home, ticket['delivery_id'])['status'] == 'queued'
        finally:
            current.release()
            db.close()
