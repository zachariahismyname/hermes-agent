"""Preserve the actionable part of user rows folded with runtime notices."""
from __future__ import annotations

import copy

MERGED_USER_ORIGIN = "_merged_user_origin"


def merged_user_origin(message):
    """Return a detached original request, if a mixed row has durable provenance."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return None
    metadata = message.get("display_metadata")
    origin = metadata.get(MERGED_USER_ORIGIN) if isinstance(metadata, dict) else None
    if (not isinstance(origin, dict) or origin.get("role") != "user"
            or not isinstance(origin.get("content"), (str, list))):
        return None
    return copy.deepcopy(origin)


def preserve_merged_user_origin(target, left, right):
    """Call before overwriting content; keep runtime text but not its task authority."""
    from agent.context_compressor import user_originated_turn_view
    from agent.conversation_compression import _is_real_user_message

    views = [user_originated_turn_view(row) if _is_real_user_message(row) else None
             for row in (left, right)]
    if any(not row.get("content") and not row.get("display_kind") for row in (left, right)):
        existing = merged_user_origin(left) or merged_user_origin(right)
        if existing:
            metadata = dict(target.get("display_metadata") or {})
            metadata[MERGED_USER_ORIGIN] = existing
            target["display_metadata"] = metadata
        return  # A blank echo contributes neither scaffolding nor authority.
    # Ordinary adjacent human asks remain one combined ask. Only a mixed row needs
    # an origin projection; subsequent folds carry that projection forward.
    if all(views) and not any(merged_user_origin(row) for row in (left, right)):
        return
    origin = views[1] or views[0]
    if origin is None:
        return
    origin = {key: copy.deepcopy(origin[key]) for key in
              ("role", "content", "message_uid", "timestamp", "_row_id") if key in origin}
    if all(views):
        from agent.agent_runtime_helpers import _merge_user_content
        origin["content"] = _merge_user_content(views[0]["content"], views[1]["content"])
    metadata = dict(target.get("display_metadata") or {})
    metadata[MERGED_USER_ORIGIN] = origin
    target["display_metadata"] = metadata
