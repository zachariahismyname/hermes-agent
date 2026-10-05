"""Image turns leave delivery notes pending; the next text turn durably replays them."""
import json
import threading
import time
from types import SimpleNamespace

from hermes_state import SessionDB
from tests.tools.test_bot_status_note_integrity import seed_notes
from tui_gateway import prompt_turn
from tui_gateway.method_ctx import rebind
from tui_gateway.session_notifications import _prepend_note


def test_real_turn_preparation_defers_images_then_preserves_text_wire(tmp_path, monkeypatch):
    from agent import notification_presentation
    from agent.turn_context import _stamp_api_content_sidecar, build_api_messages
    from agent.session_persistence import SessionPersistenceMixin
    from tools import approval_context, tts_streaming

    directory, ids = seed_notes(tmp_path, 1)
    path = directory / f"{ids[0]}.json"
    before = path.read_bytes()
    monkeypatch.setattr(approval_context, "set_current_session_key", lambda *_: None)
    monkeypatch.setattr(notification_presentation, "event_presentation_muted", lambda *_: True)
    monkeypatch.setattr(tts_streaming, "take_speech_interrupted", lambda: False)
    note = rebind(prompt_turn._delivery_status_note, {
        "_is_bot_mode_session": lambda session: True,
        "_session_home": lambda session: tmp_path,
        "_submit_row_target_key": lambda session: "chat",
    })
    image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    bindings = dict(
        _profile_runtime_scope_tokens=lambda *_: None, _set_session_context=lambda *a, **k: None,
        _session_cwd=lambda *_: str(tmp_path), make_stream_renderer=lambda *_: None,
        _route_turn_images=lambda agent, text, images: [{"type": "text", "text": text}, image],
        _pending_reaction_notes=lambda *_: "", _hud_surface_note=lambda *_: "",
        _prepend_note=_prepend_note, _delivery_status_note=note,
    )
    for name in ("_wire_callbacks", "_sync_agent_fallback_with_config", "_sync_bot_capabilities",
                 "_adopt_out_of_band_turns", "_stage_first_contact_onboarding_note", "_register_session_cwd"):
        bindings[name] = lambda *a, **k: None
    prepare = rebind(prompt_turn._prepare_turn_input, bindings)
    agent = SimpleNamespace(_current_turn_timestamp=time.time(), ephemeral_system_prompt="",
                            _copy_reasoning_content_for_api=lambda *a: None,
                            _should_sanitize_tool_calls=lambda: False)
    session = dict(session_key="chat", agent=agent, history=[], history_lock=threading.RLock())

    def turn(text, images):
        state = SimpleNamespace(scopes=SimpleNamespace(), one_turn_restore=True)
        return prepare("chat", session, state, text, images)[:2]

    prompt, image_input = turn("inspect image", ["synthetic.png"])
    assert prompt == "inspect image"
    assert image_input == [{"type": "text", "text": prompt}, image]
    assert path.read_bytes() == before  # No consumption on the unsupported list-content path.

    prompt, text_input = turn("next request", [])
    assert prompt == "next request" and ids[0] in text_input
    assert "delivered_at" in json.loads(path.read_text())
    messages = [dict(role="user", content=text_input)]
    agent._persist_user_message_idx = 0
    agent._persist_user_message_override = prompt
    _stamp_api_content_sidecar(agent, messages, 0, "", "", preflight_compressed=False)

    def wire(history, current):
        return build_api_messages(agent, history, current_turn_user_idx=current,
                                  ext_prefetch_cache="", plugin_user_context="",
                                  moa_config=None, active_system_prompt="SYSTEM")[0]

    first_api = wire(messages, 0)
    SessionPersistenceMixin._apply_persist_user_message_override(agent, messages)
    assert messages[0]["content"] == prompt  # The visible user transcript stays clean.
    with SessionDB(db_path=tmp_path / "state.db") as db:
        db.append_message("chat", "user", content=prompt, api_content=messages[0]["api_content"])
    with SessionDB(db_path=tmp_path / "state.db", read_only=True) as db:
        restored = db.get_messages_as_conversation("chat")
    next_history = restored + [dict(role="assistant", content="done"), dict(role="user", content="later")]
    assert wire(next_history, 2)[1]["content"] == first_api[1]["content"]
    assert turn("later", [])[1] == "later"
