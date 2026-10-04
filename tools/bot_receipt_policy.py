"""Recognize correlated success notices that contain no work for the model.

Substantive replies and failures still need the sender's attention. Do not turn
an arbitrary empty model response into success, or infer machinery from prose.
The terminal mailbox record remains the durable status history.

A passive receipt is still the sender's only completion signal for a durable
delivery (``message_agent`` promised it would arrive), so it is deferred, not
dropped: a status note is written beside the outbox and shown to the sender's
model once, as model input on its next real turn.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Recorded by the recipient only when its successful turn ended on an exact
# silence marker. A blank reply without it is the empty-response path, not silence.
SILENCE_REASON = "intentional_silence"
_NOTE_DIR = ("runtime", "bot_delivery_status")
_ID_FILE = re.compile(r"[0-9a-f]{32,64}\.json")
_MAX_NOTE_LINES = 20
_DELIVERED_NOTE_RETENTION_NS = 7 * 24 * 3600 * 10**9
# Damaged notes already reported by this process (mutated only under the notes file lock).
_warned_notes: set[Path] = set()
# Longest the note writer waits for the notes lock before the receipt falls back to a model turn.
_NOTE_LOCK_WAIT_S = 2.0
# Status-only receipts one mailbox pass settles before yielding: the TUI pass holds the session
# admission guard, and each settlement is a claim plus two fsynced writes.
MAX_PASSIVE_PER_PASS = 8


def silence_reason(final_response) -> str:
    """Terminal ``reason`` for a successful recipient turn ('' unless exact silence)."""
    from gateway.response_filters import is_intentional_silence_response
    return SILENCE_REASON if is_intentional_silence_response(final_response) else ""


def passive_receipt_id(home: Path | str, envelope: dict) -> str | None:
    """Original task ID for a verified, non-actionable success receipt, else None."""
    from gateway.response_filters import is_intentional_silence_response
    from tools.bot_delivery_outbox import RECEIPT_AUTHOR
    from tools.bot_live_delivery import read_delivery_result

    if envelope.get("author") != RECEIPT_AUTHOR:
        return None
    try:
        payload = json.loads(envelope["message"].split("\n", 1)[1])
        if not isinstance(payload, dict):
            return None
        task_id = payload.get("delivery_id")
        reply = payload.get("reply", "")
        if (not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32,64}", task_id)
                or payload.get("status") != "settled" or payload.get("error")
                or payload.get("reason") != SILENCE_REASON or not isinstance(reply, str)
                or (reply.strip() and not is_intentional_silence_response(reply))):
            return None
        home = Path(home).resolve()
        receipt_id = hashlib.sha256(f"receipt:{home}:{task_id}".encode()).hexdigest()
        if envelope.get("delivery_id") != receipt_id:
            return None
        index = _read_index(home, task_id)
        # Publication is mailbox first, index marker second. The deterministic
        # ID + original durable outcome also validate that crash/race window.
        if index.get("delivery_id") != task_id or index.get("receipt_queued") not in (None, receipt_id):
            return None
        result = read_delivery_result(index["recipient_home"], task_id)
        if not result:
            return None
        expected = {key: result[key] for key in ("delivery_id", "status", "reply", "error", "reason") if key in result}
        return task_id if payload == expected else None
    except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
        # Missing/corrupt correlation must never authorize dropping model input.
        return None


def wake_on_silent_completion() -> bool:
    """``bot_mode.wake_on_silent_completion``: operator switch back to a receipt turn per silent
    completion, without a code rollback. Read per receipt in the caller's profile scope."""
    from tools.bot_relay import _bot_mode_cfg
    return _bot_mode_cfg("wake_on_silent_completion", loader="load_config") is True


def settle_passive_receipt(home: Path | str, envelope: dict) -> str | None:
    """Settle a claimed passive receipt without a model turn; returns the original task ID.

    The status note is written before the receipt settles: a crash in between
    leaves a claimed receipt (inspectable) rather than a settled one with no note.
    """
    from tools.bot_live_delivery import complete_delivery

    task_id = passive_receipt_id(home, envelope)
    if task_id is None or wake_on_silent_completion():
        return None
    try:
        _record_status_note(Path(home).resolve(), task_id, envelope["delivery_id"])
    except (OSError, ValueError, KeyError, TypeError, RuntimeError):
        # Without a durable note the receipt is the only signal: let the model see it.
        logger.warning("Delivery status note not recorded for %s; receipt kept as model input", task_id, exc_info=True)
        return None
    complete_delivery(home, envelope["delivery_id"], status="settled", reason="status_only_receipt")
    return task_id


def take_status_note(home: Path | str | None, session_id: str | None) -> str:
    """Model-input note for this conversation's silently completed deliveries ('' if none).

    Announced once: notes are stamped ``delivered_at`` under a lock when read,
    like pending reaction notes. The note is never persisted as the user's text.
    """
    if home is None or not session_id:
        return ""
    try:
        return _take_status_note(Path(home).resolve(), session_id)
    except Exception:
        # A status note is advisory; it must never block the turn that would carry it.
        logger.warning("Delivery status notes unavailable this turn", exc_info=True)
        return ""


def _take_status_note(home: Path, session_id: str) -> str:
    from hermes_cli.active_sessions import _FileLock
    from hermes_state import SessionDB
    from utils import atomic_json_write

    directory = home.joinpath(*_NOTE_DIR)
    if not directory.is_dir() or not (home / "state.db").is_file():
        return ""
    lines = []
    with _FileLock(directory / ".lock"):
        candidates = []
        for path in directory.glob("*.json"):
            if _ID_FILE.fullmatch(path.name) is None:
                continue
            try:
                note = _validate_status_note(path, json.loads(path.read_text(encoding="utf-8-sig")))
            except (OSError, ValueError):
                # Rescanned on every Bot Chat turn: WARNING once per record and process, then DEBUG.
                first = path not in _warned_notes
                _warned_notes.add(path)
                logger.log(logging.WARNING if first else logging.DEBUG,
                           "Unreadable delivery status note retained: %s", path, exc_info=first)
                continue
            _warned_notes.discard(path)
            if "delivered_at" not in note:
                candidates.append((path, note))
            elif (isinstance(note["delivered_at"], int)
                  and time.time_ns() - note["delivered_at"] > _DELIVERED_NOTE_RETENTION_NS):
                try:  # Shown long ago; the mailbox receipt stays the record. Best effort only.
                    path.unlink(missing_ok=True)
                except OSError:
                    logger.debug("Delivered status note not pruned: %s", path, exc_info=True)
        if not candidates:
            return ""
        db = SessionDB(db_path=home / "state.db", read_only=True)
        try:
            mine = [(p, n) for p, n in candidates if db.get_compression_tip(n.get("sender_session")) == session_id]
        finally:
            db.close()
        # Select before stamping: overflow must remain pending for a later turn.
        selected = sorted(mine, key=lambda item: (item[1]["recorded_at"], item[0].name))[:_MAX_NOTE_LINES]
        for path, note in selected:
            # Render before consuming anything. One damaged record cannot consume its siblings.
            line = f"- {note['delivery_id']} (to {note['recipient']}, {_when(note)})"
            note["delivered_at"] = time.time_ns()
            try:
                atomic_json_write(path, note, mode=0o600, fsync_dir=True)
            except OSError:
                # Show what is already stamped; the rest stay pending for a later turn.
                logger.warning("Delivery status note left pending: %s", path, exc_info=True)
                break
            lines.append(line)
    if not lines:
        return ""
    return ("[Delivery status — informational, not a new assignment. These earlier messages you sent "
            "were completed by the recipient, who chose not to reply. Do not resend them or reply "
            "to this note on its own.]\n" + "\n".join(lines))


def _when(note: dict) -> str:
    """When the sender settled the receipt (at or after the recipient finished), so a note that
    surfaces late (idle sender, rollback) is not read as news."""
    recorded = note.get("recorded_at")
    if not isinstance(recorded, int):
        return "time unknown"
    try:
        return time.strftime("receipt %Y-%m-%d %H:%M UTC", time.gmtime(recorded / 1e9))
    except (OverflowError, OSError, ValueError):
        return "time unknown"


def _validate_status_note(path: Path, note: object) -> dict:
    """Reject unusable records before consumption or idempotent settlement; retain the file."""
    if not isinstance(note, dict) or note.get("delivery_id") != path.stem:
        raise ValueError("Status note delivery ID does not match its filename")
    if (_ID_FILE.fullmatch(path.name) is None
            or not isinstance(note.get("receipt_id"), str)
            or re.fullmatch(r"[0-9a-f]{64}", note["receipt_id"]) is None):
        raise ValueError("Invalid status note identity")
    if any(not isinstance(note.get(k), str) or not note[k].strip()
           for k in ("sender_session", "recipient")):
        raise ValueError("Invalid status note sender or recipient")
    for key in ("recorded_at", "delivered_at"):
        if key == "delivered_at" and key not in note:
            continue
        if type(note.get(key)) is not int or note[key] < 0:
            raise ValueError(f"Invalid status note {key}")
    return note


@contextlib.contextmanager
def _bounded_note_lock(lock: Path):
    """The notes lock, with a wait bounded on every OS (POSIX flock alone never times out).

    The TUI poller records notes while holding the session admission guard, so a stalled
    reader elsewhere must cost a fallback to an ordinary receipt turn, never an open-ended
    stall. Same byte/primitive as ``_FileLock``, so readers and writers still exclude.
    """
    from tools.bot_delivery_outbox import _try_recovery_lock

    deadline = time.monotonic() + _NOTE_LOCK_WAIT_S
    while True:
        with _try_recovery_lock(lock) as acquired:
            if acquired:
                yield
                return
        if time.monotonic() >= deadline:
            raise RuntimeError("Delivery status notes are locked by another reader")
        time.sleep(0.05)


def _read_index(home: Path, task_id: str) -> dict:
    from utils import read_text_contended  # Unlocked exact read racing the outbox writer's atomic replace.
    return json.loads(read_text_contended(home / "runtime/bot_delivery_outbox" / f"{task_id}.json"))


def _record_status_note(home: Path, task_id: str, receipt_id: str) -> None:
    from utils import atomic_json_write

    path = home.joinpath(*_NOTE_DIR, f"{task_id}.json")
    index = _read_index(home, task_id)
    recipient = Path(index["recipient_home"])
    note = dict(delivery_id=task_id, receipt_id=receipt_id, sender_session=index["sender_session"],
                recipient=recipient.name if recipient.parent.name == "profiles" else "default",
                recorded_at=time.time_ns())
    _validate_status_note(path, note)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _bounded_note_lock(path.parent / ".lock"):
        if path.exists() or path.is_symlink():
            existing = _validate_status_note(path, json.loads(path.read_text(encoding="utf-8-sig")))
            if any(existing[k] != note[k] for k in ("delivery_id", "receipt_id", "sender_session", "recipient")):
                raise ValueError("Existing status note does not match this receipt")
            return  # Valid pending/delivered record: preserve its consumption state.
        atomic_json_write(path, note, mode=0o600, fsync_dir=True)
