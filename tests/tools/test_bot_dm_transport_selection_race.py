"""Two runners for one A2A payload never execute it through both transports.

Runner B passes the transport checks and is about to admit the payload to the live mailbox when
runner A, finding no live owner, reserves the payload and runs a direct CLI turn whose quiet CLI
advertises a mailbox. If B then admits the same payload to that mailbox, the CLI executes it a
second time. Transport selection (reconciliation check, live admission, CLI reservation) is one
atomic step per payload.
"""
import contextlib
import json
import threading
import time

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_state import SessionDB
from tools import bot_live_delivery as mailbox
from tools import bot_mode_dm as dm


def test_concurrent_runners_never_use_both_transports_for_one_payload(tmp_path, monkeypatch, capsys):
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    payload = tmp_path / "message.txt"
    payload.write_text("do the task once", encoding="utf-8")
    leases, cli_turns = [], []
    b_admitted = threading.Event()

    def cli_turn(*args, **kwargs):
        # The direct turn's quiet CLI advertises its mailbox while it runs.
        lease, refusal = try_acquire_active_session(
            session_id="chat", surface="cli", config={}, registry_home=tmp_path,
            metadata={"live_session_id": "cli-live", "bot_live_delivery_consumer": True})
        assert refusal is None
        leases.append(lease)
        cli_turns.append(1)
        b_admitted.wait(timeout=5)  # still running (mailbox advertised) while the other runner decides
        return 0

    real_admit = dm._admit_live_dm
    runner_a = []

    def admit(*args, **kwargs):
        if not runner_a:  # runner B: let runner A race in before B's admission lands
            thread = threading.Thread(target=lambda: dm._run_delivery(
                [], str(payload), stdin_file=False, profile_home=tmp_path), daemon=True)
            runner_a.append(thread)
            thread.start()
            time.sleep(0.5)
            try:
                return real_admit(*args, **kwargs)
            finally:
                b_admitted.set()
        return real_admit(*args, **kwargs)

    monkeypatch.setattr(dm, "_run_local_turn", cli_turn)
    monkeypatch.setattr(dm, "_admit_live_dm", admit)
    monkeypatch.setattr(dm, "_delivery_lock", lambda *a, **k: contextlib.nullcontext())
    monkeypatch.setattr(dm, "_LIVE_WAIT_SECONDS", 0)
    monkeypatch.setattr(dm, "_LIVE_WAIT_MAX_SECONDS", 0)
    try:
        dm._run_delivery([], str(payload), stdin_file=False, profile_home=tmp_path)
        runner_a[0].join(timeout=20)
        assert not runner_a[0].is_alive()

        queued = [json.loads(p.read_text(encoding="utf-8"))
                  for p in (tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME).glob("*.json")] \
            if (tmp_path / "runtime" / mailbox.DELIVERY_DIR_NAME).is_dir() else []
        executions = len(cli_turns) + len([r for r in queued if r.get("message") == "do the task once"])
        assert executions == 1, (cli_turns, queued)
    finally:
        for lease in leases:
            lease.release()
        db.close()
