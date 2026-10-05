"""Bot Chat mailbox consumption by an already-admitted quiet CLI writer."""
from __future__ import annotations

import os
import logging
from pathlib import Path

# Internal per-invocation transport token, consumed before any tool can inherit it.
DELIVERY_ID_ENV = "HERMES_BOT_MAILBOX_DELIVERY_ID"


class QuietMailbox:
    def __init__(self, cli, author):
        self.cli = cli
        self.requested_id = os.environ.pop(DELIVERY_ID_ENV, None)
        self.lease = getattr(cli, "_active_session_lease", None)
        self.home = None
        self.last_result = None
        self.sender = (author or {}).get("name") if isinstance(author, dict) else None
        self.internal = bool(self.requested_id or (isinstance(author, dict) and author.get("is_bot")))
        if self.lease is None or self.lease.state_path is None:
            if self.requested_id:
                raise RuntimeError("Mailbox execution requires a live session lease")
            return
        self.home = Path(self.lease.state_path).parent.parent
        self.advertise()

    def advertise(self):
        from hermes_cli.active_sessions import transfer_active_session
        from tools.bot_live_delivery import find_canonical_owner

        if self.home is None:
            return None
        owner = find_canonical_owner(self.home)
        if not owner or owner["lease_id"] != self.lease.lease_id:
            return None
        metadata = dict(owner.get("metadata") or {})
        metadata.update(bot_live_delivery_consumer=True, live_session_id=owner["session_id"],
                        internal_bot_worker=self.internal)
        if self.sender:
            metadata["sender_name"] = self.sender
        if not transfer_active_session(self.lease, session_id=owner["session_id"], metadata=metadata):
            raise RuntimeError("Mailbox worker lost its session lease")
        return {key: owner[key] for key in ("profile_home", "session_id", "lease_id")} | {
            "live_session_id": owner["session_id"]}

    def drain_one(self, run_turn):
        from tools.bot_live_delivery import claim_pending_delivery, complete_delivery
        from tools.bot_receipt_policy import MAX_PASSIVE_PER_PASS, settle_passive_receipt, silence_reason

        owner = self.advertise()
        if owner is None:
            return False
        from tools.bot_delivery_outbox import poll_outgoing
        poll_outgoing(self.home, owner["session_id"])
        # Status-only receipts are not turns: settle a run of them in this one pass
        # instead of spending a notify round (or a poll interval) on each.
        settled = 0
        while True:
            record = claim_pending_delivery(self.home, owner, recover_unclaimed=True)
            if record is None:
                return settled > 0
            if (original_id := settle_passive_receipt(self.home, record)) is None:
                break
            logging.getLogger(__name__).info(
                "Delivery %s completed without a reply; status note deferred to the next turn", original_id)
            self.last_result = None
            settled += 1
            if settled >= MAX_PASSIVE_PER_PASS:
                return True
        if self.internal and record.get("author"):
            self.sender = record["author"].get("name")
            self.advertise()
        try:
            result = run_turn(record["message"], record.get("author"))
        except BaseException as exc:
            # A turn may already have performed effects. Retain the claim for
            # reconciliation; an exception is never permission to rerun it.
            logging.getLogger(__name__).warning(
                "Bot delivery interrupted: id=%s session=%s lease=%s pid=%s exception=%s; claim retained",
                record["delivery_id"], owner["session_id"], owner["lease_id"], os.getpid(), type(exc).__name__)
            raise
        failed = (not isinstance(result, dict) or result.get("failed") or result.get("partial")
                  or result.get("error") or result.get("interrupted") or result.get("completed") is False)
        reply = str(result.get("final_response") or "") if isinstance(result, dict) and not failed else ""
        complete_delivery(self.home, record["delivery_id"],
                          status="failed" if failed else "settled", reply=reply,
                          error=str(result.get("error") or "") if isinstance(result, dict) else "No turn result",
                          reason="" if failed else silence_reason(reply))
        self.last_result = result
        return True

    def with_status_note(self, text, run_kwargs):
        """Model input carrying deferred delivery-status notes; the stored user row stays ``text``."""
        from tools.bot_receipt_policy import take_status_note

        if self.home is None or not isinstance(text, str):
            return text
        note = take_status_note(self.home, getattr(self.cli, "session_id", None))
        if not note:
            return text
        run_kwargs.setdefault("persist_user_message", text)
        return f"{note}\n\n{text}"

    def has_pending(self):
        from tools.bot_delivery_outbox import has_pending_outgoing
        return self.home is not None and has_pending_outgoing(self.home, self.cli.session_id)
