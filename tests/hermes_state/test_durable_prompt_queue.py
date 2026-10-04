import pytest

from hermes_state import SessionDB
from hermes_state_queue import pending_prompt_envelopes, replace_queued_row


def seed(home):
    db = SessionDB(db_path=home / "state.db")
    db.create_session(session_id="chat", source="desktop")
    db.append_message("chat", "user", "current")
    queued = db.append_message("chat", "user", "next", display_metadata={
        "_queued_prompt": True, "_queue_envelope": {"image_paths": ["synthetic.png"], "turn_author": None}})
    db.append_message("chat", "assistant", "done")
    return db, queued


def test_restart_recovers_complete_envelope_but_not_into_model_history(tmp_path):
    db, early = seed(tmp_path)
    uid = next(r["message_uid"] for r in db.get_messages("chat") if r["id"] == early)
    db.close()
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.reopen_session("chat")
        model, display = db.get_resume_conversations("chat")
        assert all(m["content"] != "next" for m in model)
        assert any(m["content"] == "next" for m in display)
        envelope, = pending_prompt_envelopes(db, "chat")
        assert envelope["image_paths"] == ["synthetic.png"]
        fresh = replace_queued_row(db, "chat", "chat", early, "next", dispatch=True)
        assert fresh["message_uid"] == uid
        assert pending_prompt_envelopes(db, "chat") == []
        assert [m["content"] for m in db.get_messages_as_conversation("chat")] == ["current", "done", "next"]
        with pytest.raises(ValueError, match="queued request"):
            replace_queued_row(db, "chat", "chat", early, "next", dispatch=True)
        assert [m["content"] for m in db.get_messages_as_conversation("chat")].count("next") == 1
    finally:
        db.close()


def test_crash_before_atomic_admission_commit_keeps_request_pending(tmp_path, monkeypatch):
    db, early = seed(tmp_path)
    try:
        original = db._insert_message_rows
        def crash(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError("synthetic crash before commit")
        monkeypatch.setattr(db, "_insert_message_rows", crash)
        with pytest.raises(RuntimeError, match="synthetic crash"):
            replace_queued_row(db, "chat", "chat", early, "next", dispatch=True)
        assert len(pending_prompt_envelopes(db, "chat")) == 1
        assert [r["content"] for r in db.get_messages("chat")].count("next") == 1
    finally:
        db.close()


def test_later_row_failure_rolls_back_entire_queue_batch(tmp_path, monkeypatch):
    from hermes_state_queue import replace_queued_rows
    db, early = seed(tmp_path)
    later = db.append_message("chat", "user", "later", display_metadata={
        "_queued_prompt": True, "_queue_envelope": {"image_paths": []}})
    original = db._insert_message_rows
    calls = 0
    def crash(*args, **kwargs):
        nonlocal calls
        calls += 1
        result = original(*args, **kwargs)
        if calls == 2:
            raise RuntimeError("tail write failed")
        return result
    monkeypatch.setattr(db, "_insert_message_rows", crash)
    try:
        with pytest.raises(RuntimeError, match="tail write failed"):
            replace_queued_rows(db, "chat", [("chat", early, "next", True), ("chat", later, "later", False)])
        assert [e["text"] for e in pending_prompt_envelopes(db, "chat")] == ["next", "later"]
        assert [m["content"] for m in db.get_messages_as_conversation("chat")] == ["current", "done"]
    finally:
        db.close()


def test_rotated_queue_admits_continuation_copy_of_same_identity(tmp_path):
    db, early = seed(tmp_path)
    try:
        row = next(r for r in db.get_messages_as_conversation("chat", include_queued=True) if r["content"] == "next")
        db.end_session("chat", "compression")
        db.create_session("tip", source="desktop", parent_session_id="chat")
        db.append_message("tip", "user", "next", message_uid=row["message_uid"], display_metadata=row["display_metadata"])
        fresh = replace_queued_row(db, "chat", "tip", early, "next", dispatch=True)
        assert fresh["message_uid"] == row["message_uid"]
        assert pending_prompt_envelopes(db, "tip") == []
        assert [r["content"] for r in db.get_messages_as_conversation("tip")].count("next") == 1
    finally:
        db.close()
