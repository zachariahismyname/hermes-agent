import copy

import pytest

from agent.agent_runtime_helpers import repair_message_sequence, drop_thinking_only_and_merge_users
from agent.context_compressor import ContextCompressor, user_originated_turn_view
from hermes_state import SessionDB


@pytest.mark.parametrize("notice", [
    {"role": "user", "content": "[System: model changed]", "display_kind": "model_switch"},
    {"role": "user", "content": "[IMPORTANT: Background process synthetic completed]"},
    {"role": "user", "content": "runtime scaffold", "_todo_snapshot_synthetic": True},
])
@pytest.mark.parametrize("notice_first", [True, False])
def test_real_request_survives_mixed_merge_and_database_reopen(tmp_path, notice, notice_first):
    request = dict(role="user", content="Current assignment", message_uid="request-identity", timestamp=2.)
    pair = [notice, request] if notice_first else [request, notice]
    rows = copy.deepcopy(pair)
    assert repair_message_sequence(None, rows) == 1
    assert user_originated_turn_view(rows[0])["content"] == request["content"]
    assert user_originated_turn_view(rows[0])["message_uid"] == request["message_uid"]
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.append_message("chat", "user", rows[0]["content"], display_kind=rows[0].get("display_kind"),
                      display_metadata=rows[0].get("display_metadata"))
    db.close()
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        restored = db.get_messages_as_conversation("chat", repair_alternation=True)
        assert user_originated_turn_view(restored[0])["content"] == request["content"]
    finally:
        db.close()


def test_repeated_merge_preserves_both_human_requests_but_not_notice_authority():
    rows = [dict(role="user", content="runtime notice", display_kind="model_switch"),
            dict(role="user", content="first ask"), dict(role="user", content="second ask")]
    repair_message_sequence(None, rows)
    assert user_originated_turn_view(rows[0])["content"] == "first ask\n\nsecond ask"


def test_multimodal_api_merge_preserves_origin_without_mutating_history():
    parts = [{"type": "text", "text": "Inspect this"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,synthetic"}}]
    rows = [dict(role="user", content="model notice", display_kind="model_switch"), dict(role="user", content=parts)]
    before = copy.deepcopy(rows)
    repaired = drop_thinking_only_and_merge_users(rows)
    assert rows == before
    assert user_originated_turn_view(repaired[0])["content"] == parts


def test_pure_runtime_notices_never_become_user_tasks():
    rows = [dict(role="user", content="first notice", display_kind="model_switch"),
            dict(role="user", content="second notice", display_kind="model_switch")]
    repair_message_sequence(None, rows)
    assert user_originated_turn_view(rows[0]) is None
    assert ContextCompressor._find_inflight_user_task(rows) is None


def test_full_compression_keeps_current_request_after_model_notice(monkeypatch):
    from tests.agent.test_cron_inflight_prompt_reappend import _compress, _tool_pairs
    current = "CURRENT-REQUEST-SENTINEL"
    rows = [dict(role="system", content="stable system"),
            dict(role="user", content="OLD-PAUSE-SENTINEL"),
            dict(role="assistant", content="Pause acknowledged."),
            dict(role="user", content="[System: model changed]", display_kind="model_switch"),
            dict(role="user", content=current), *_tool_pairs(40)]
    repair_message_sequence(None, rows)
    compressed = _compress(rows)
    views = [user_originated_turn_view(m) for m in compressed]
    assert any(v and current in str(v["content"]) for v in views)
    # Protected historical head rows may remain, but cannot become the current task.
    selected = ContextCompressor._find_inflight_user_task(compressed)
    assert selected is not None and current in str(selected["content"])
    assert "OLD-PAUSE-SENTINEL" not in str(selected["content"])
    # A second compression cycle must not bring back the prior notice or old task.
    again = _compress(compressed + _tool_pairs(40, start=100))
    active = ContextCompressor._find_inflight_user_task(again)
    assert active is not None and current in str(active["content"])
