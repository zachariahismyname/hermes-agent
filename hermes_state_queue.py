"""Transactional admission of durable UI queue rows; no model or transport work."""
from __future__ import annotations


def _queued_row(conn, owner_key, target_key, early_id):
    origin = conn.execute("SELECT * FROM messages WHERE id = ? AND session_id = ?",
                          (early_id, owner_key)).fetchone()
    if origin is None:
        raise ValueError("queued request no longer has an active row")
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id IN (?, ?) AND message_uid = ? AND active = 1",
        (owner_key, target_key, origin["message_uid"])).fetchall()
    if owner_key != target_key:
        # A rotation preserves the parent's audit rows. Prefer the continuation's
        # copy of the SAME identity, never treat both generations as two requests.
        tip_rows = [row for row in rows if row["session_id"] == target_key]
        if tip_rows:
            rows = tip_rows
    if len(rows) != 1:
        raise ValueError("queued request has no unambiguous active row")
    return rows[0]


def replace_queued_rows(db, target_key, requests):
    """Move the dispatching head and pending tail together, or leave ALL untouched.

    Each request is (owner_key, early_id, text, dispatch). Resolve identities on
    the writer connection so another process cannot race a separate read handle.
    """
    from agent.context_compressor import _DB_PERSISTED_MARKER

    def write(conn):
        db._check_transcript_write_guards(conn, target_key, None)
        result = []
        for owner_key, early_id, text, dispatch in requests:
            row = _queued_row(conn, owner_key, target_key, early_id)
            metadata = db._decode_display_metadata(row["display_metadata"]) or {}
            if not metadata.get("_queued_prompt") or metadata.get("_queue_cancelled"):
                raise ValueError("queued request has already been dispatched or cancelled")
            if dispatch:
                metadata.pop("_queued_prompt", None)
                metadata.pop("_queue_envelope", None)
            fresh = dict(role="user", content=text, display_kind=row["display_kind"],
                         display_metadata=metadata, message_uid=row["message_uid"])
            inserted, tool_count = db._insert_message_rows(conn, target_key, [fresh])
            conn.execute("UPDATE messages SET active = 0 WHERE id = ?", (row["id"],))
            db._bump_session_counters(conn, target_key, inserted, tool_count, unit=True)
            fresh[_DB_PERSISTED_MARKER] = True
            fresh["_submit_row_session_id"] = target_key
            result.append(fresh)
        return result

    return db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)


def replace_queued_row(db, owner_key, target_key, early_id, text, *, dispatch):
    return replace_queued_rows(db, target_key, [(owner_key, early_id, text, dispatch)])[0]


def cancel_queued_rows(db, target_key, requests):
    """Keep cancelled requests visible as audit rows, permanently ineligible to run."""
    import json

    def write(conn):
        db._check_transcript_write_guards(conn, target_key, None)
        for owner_key, early_id in requests:
            row = _queued_row(conn, owner_key, target_key, early_id)
            metadata = db._decode_display_metadata(row["display_metadata"]) or {}
            if not metadata.get("_queued_prompt"):
                continue  # Already admitted: ordinary turn cancellation owns it.
            metadata.pop("_queue_envelope", None)
            metadata["_queue_cancelled"] = True
            conn.execute("UPDATE messages SET display_metadata = ? WHERE id = ?",
                         (json.dumps(metadata), row["id"]))
    db._execute_write(write, patience_s=db._TRANSCRIPT_WRITE_PATIENCE_S)


def pending_prompt_envelopes(db, session_id):
    """Restore only envelopes whose complete dispatch data was durably accepted."""
    rows = db.get_messages_as_conversation(session_id, include_row_ids=True, include_queued=True)
    result = []
    for row in rows:
        metadata = row.get("display_metadata") or {}
        envelope = metadata.get("_queue_envelope")
        if not metadata.get("_queued_prompt") or metadata.get("_queue_cancelled") or not isinstance(envelope, dict):
            continue  # Legacy audit-only rows remain visible, never guessed/replayed.
        result.append(dict(text=row["content"], image_paths=envelope.get("image_paths") or [],
                           turn_author=envelope.get("turn_author"), transport=None,
                           _queued_display_kind=row.get("display_kind"),
                           _submit_user_row={**row, "_submit_row_session_id": session_id}))
    return result
