"""Sender-side indexes for mailbox receipts, independent of waiter lifetime.

Receipt delivery reuses the existing mailbox claim/settlement protocol. The index
never executes an agent turn or treats a terminal process exit as delivery success.
"""
from __future__ import annotations

import hashlib
import json
import logging
import subprocess
import sys
import threading
import time
from pathlib import Path

_workers = set()
_workers_lock = threading.Lock()
_last_sweep = {}
logger = logging.getLogger(__name__)
RECEIPT_AUTHOR = {"id": "bot:__delivery_receipt__", "name": "Hermes delivery receipt", "is_bot": True}


def _indexes(paths):
    for path in paths:
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(record, dict) and "sender_session" in record:
                yield path, record
        except (ValueError, OSError):
            logger.warning("Unreadable delivery index retained: %s", path, exc_info=True)


def register_outgoing(agent, record, argv, dm_file, author):
    from utils import atomic_json_write
    db = getattr(agent, "_session_db", None)
    key = getattr(agent, "session_id", None)
    if not db or not isinstance(key, str) or not key:
        return False
    home = Path(db.db_path).resolve().parent
    directory = home / "runtime" / "bot_delivery_outbox"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (record["delivery_id"] + ".json")
    sender_argv = list(argv)
    if len(sender_argv) >= 3 and sender_argv[1] == "-p":
        sender_argv[2] = home.name if home.parent.name == "profiles" else "default"
    else:
        sender_argv = []
    if not path.exists():
        atomic_json_write(path, dict(delivery_id=record["delivery_id"], sender_session=key,
            recipient_home=record["profile_home"], argv=argv, sender_argv=sender_argv,
            dm_file=str(dm_file), author=author),
            mode=0o600, fsync_dir=True)
    return True


def _recover_in_background(home, record):
    """Revisit an existing intent after restart; child still claims under its lease."""
    from agent.memory_provider import spawn_context_thread
    from hermes_cli.active_sessions import _FileLock
    from tools.bot_relay import delivery_env
    from hermes_cli._subprocess_compat import windows_hide_flags
    from tools import bot_mode_dm
    from tools.bot_live_delivery import read_delivery_result
    from utils import atomic_json_write, atomic_write_text

    marker = str(home / "runtime" / "bot_delivery_outbox" / (record["delivery_id"] + ".runner.lock"))
    with _workers_lock:
        if marker in _workers:
            return
        _workers.add(marker)

    def work():
        try:
            with _FileLock(Path(marker)):
                attempt = Path(marker + ".attempt")
                if attempt.exists() and time.time() - float(attempt.read_text(encoding="utf-8")) < 30:
                    return
                attempt.write_text(str(time.time()), encoding="utf-8")
                ticket = read_delivery_result(record["recipient_home"], record["delivery_id"])
                if not ticket or ticket["status"] != "queued":
                    return
                # The durable mailbox is authoritative, not an old temporary
                # query file. Reconstruct transport artifacts for THIS ID only.
                payload = home / "runtime/bot_delivery_outbox" / (record["delivery_id"] + ".recovery.txt")
                atomic_write_text(payload, ticket["message"], mode=0o600, fsync_dir=True)
                atomic_json_write(Path(str(payload) + ".live.json"), dict(
                    owner=ticket["owner"], message=ticket["message"], delivery_id=ticket["delivery_id"],
                    author=ticket.get("author")), mode=0o600, fsync_dir=True)
                argv = [sys.executable, str(Path(bot_mode_dm.__file__).resolve()), "--run-delivery"]
                if ticket.get("author"):
                    argv += ["--author", json.dumps(ticket["author"])]
                argv += ["query-file", str(payload), "--profile-home", record["recipient_home"], *record["argv"]]
                subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               env=delivery_env(None, home), creationflags=windows_hide_flags(), check=False)
        except Exception:
            logger.warning("Delivery recovery failed; original intent retained: %s", record.get("delivery_id"), exc_info=True)
        finally:
            with _workers_lock:
                _workers.discard(marker)
    spawn_context_thread(target=work, name="bot-outbox-recovery", daemon=True).start()


def _recover_receipt(home, record, receipt):
    from tools.bot_live_delivery import find_canonical_owner
    if receipt["status"] != "queued" or not record.get("sender_argv") or find_canonical_owner(home) is not None:
        return
    _recover_in_background(home, dict(delivery_id=receipt["delivery_id"], recipient_home=str(home),
                                     argv=record["sender_argv"]))


def poll_outgoing(home, session_id):
    """Queue each terminal receipt once into the exact originating conversation."""
    from hermes_state import SessionDB
    from tools.bot_live_delivery import read_delivery_result, deliver_to_live_owner, find_canonical_live_owner, find_canonical_owner
    from utils import atomic_json_write

    home = Path(home).resolve()
    paths = list((home / "runtime" / "bot_delivery_outbox").glob("*.json"))
    if not paths:
        return
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        for path, record in _indexes(paths):
            if db.get_compression_tip(record["sender_session"]) != session_id:
                continue
            if record.get("uncertainty_queued"):
                uncertain = read_delivery_result(home, record["uncertainty_queued"])
                if uncertain:
                    _recover_receipt(home, record, uncertain)
            if record.get("receipt_queued"):
                receipt = read_delivery_result(home, record["receipt_queued"])
                if receipt:
                    _recover_receipt(home, record, receipt)
                continue
            result = read_delivery_result(record["recipient_home"], record["delivery_id"])
            if not result:
                continue
            if result["status"] == "queued":
                if find_canonical_owner(record["recipient_home"]) is None:
                    _recover_in_background(home, record)
                continue
            receipt_field = "receipt_queued"
            if result["status"] == "claimed":
                claimed_owner = result.get("claimed_owner") or result.get("owner") or {}
                active_owner = find_canonical_owner(record["recipient_home"])
                if active_owner and active_owner["lease_id"] == claimed_owner.get("lease_id"):
                    continue  # Still executing under its actual writer lease.
                if record.get("uncertainty_queued"):
                    continue
                # Preserve the original CLAIMED record. Tell the sender why no
                # answer arrived, without retrying potentially completed effects.
                receipt_field = "uncertainty_queued"
                result = {"delivery_id": result["delivery_id"], "status": "requires_reconciliation",
                          "error": "The delivery's writer is no longer its owner. Outcome unknown; do not resend. Inspect the original delivery ID."}
            owner = find_canonical_live_owner(home)
            if owner is None and find_canonical_owner(home) is None:
                from tools.bot_mailbox_recovery import canonical_unowned_target
                owner = canonical_unowned_target(home)
            if not owner or owner["session_id"] != session_id:
                continue
            kind = "uncertain:" if receipt_field == "uncertainty_queued" else ""
            receipt_id = hashlib.sha256(("receipt:" + kind + str(home) + ":" + record["delivery_id"]).encode()).hexdigest()
            existing = read_delivery_result(home, receipt_id)
            if existing is None:
                payload = {key: result[key] for key in ("delivery_id", "status", "reply", "error", "reason") if key in result}
                existing = deliver_to_live_owner(home, owner,
                    "[Delivery receipt — status of your earlier message, not a new assignment. "
                    "Do not acknowledge or resend solely because this receipt arrived.]\n" + json.dumps(payload),
                    delivery_id=receipt_id, author=RECEIPT_AUTHOR)
            # A crash after enqueue is safe: next pass finds the same receipt ID.
            record[receipt_field] = receipt_id
            atomic_json_write(path, record, mode=0o600, fsync_dir=True)
            _recover_receipt(home, record, existing)
    finally:
        db.close()


def has_pending_outgoing(home, session_id):
    from hermes_state import SessionDB
    home = Path(home).resolve()
    paths = list((home / "runtime" / "bot_delivery_outbox").glob("*.json"))
    if not paths:
        return False
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        return any(not record.get("receipt_queued") and not record.get("uncertainty_queued")
                   and db.get_compression_tip(record["sender_session"]) == session_id
                   for _, record in _indexes(paths))
    finally:
        db.close()


def sweep_install_outgoing(profile_home):
    """Use the existing host poller to recover senders that have no open UI tile."""
    from tools.bot_mode_probe import _hermes_root, _roster
    from tools.bot_mailbox_recovery import canonical_unowned_target
    root = _hermes_root(Path(profile_home))
    key = str(root.resolve())
    with _workers_lock:
        if time.monotonic() - _last_sweep.get(key, float("-inf")) < 5:
            return
        _last_sweep[key] = time.monotonic()
    for _name, home in _roster(root):
        if not (home / "runtime" / "bot_delivery_outbox").is_dir():
            continue
        target = canonical_unowned_target(home)
        if target:
            poll_outgoing(home, target["session_id"])
