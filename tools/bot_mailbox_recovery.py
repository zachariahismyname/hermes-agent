"""Restart a retained mailbox consumer only when its canonical chat has no owner."""
from pathlib import Path


def canonical_unowned_target(home):
    from hermes_state import SessionDB
    home = Path(home).resolve()
    if not (home / "state.db").is_file():
        return None
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        row = db.get_session_by_title("Bot Chat")
        key = db.get_compression_tip(row["id"]) if row else None
        return dict(profile_home=str(home), session_id=key, lease_id="unowned", live_session_id="unowned") if key else None
    finally:
        db.close()


def recover_without_owner(home, delivery_id, argv, dm_file, *, author=None):
    from tools import bot_mode_dm as dm
    from tools.bot_live_delivery import find_canonical_owner, read_delivery_result
    from tools.bot_relay import delivery_env, TurnBusyError
    from hermes_cli.quiet_mailbox import DELIVERY_ID_ENV

    record = read_delivery_result(home, delivery_id)
    if not record or record["status"] != "queued" or find_canonical_owner(home) is not None:
        return False
    from hermes_state import SessionDB
    db = SessionDB(db_path=Path(home) / "state.db", read_only=True)
    try:
        target = canonical_unowned_target(home)
        if not target or db.get_compression_tip(record["owner"]["session_id"]) != target["session_id"]:
            return False
    finally:
        db.close()
    try:
        with dm._delivery_lock(argv, stdin_file=False):
            # A competing Desktop or runner may have won while we waited.
            record = read_delivery_result(home, delivery_id)
            if not record or record["status"] != "queued" or find_canonical_owner(home) is not None:
                return False
            env = delivery_env(author, Path(home))
            env[DELIVERY_ID_ENV] = delivery_id
            # The CHILD claims under its actual conversation lease. This process
            # never claims work speculatively or changes a retained delivery ID.
            dm._run_local_turn(argv, dm_file, env=env, emit_output=False)
            return True
    except (dm._SessionOwnershipRefused, TurnBusyError):
        return False  # Proven pre-execution refusal; leave the original queued.
