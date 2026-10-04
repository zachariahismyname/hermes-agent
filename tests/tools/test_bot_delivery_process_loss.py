"""A real process can die after an effect without making its input replayable."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

from hermes_cli.active_sessions import try_acquire_active_session
from hermes_cli.quiet_mailbox import QuietMailbox, DELIVERY_ID_ENV
from tests.hermes_cli.test_quiet_mailbox import seed
from tools import bot_live_delivery as mailbox
from tools.bot_delivery_outbox import register_outgoing, poll_outgoing


def test_process_death_after_effect_preserves_claim_and_allows_later_work(tmp_path):
    a, b = tmp_path / "profiles/a", tmp_path / "profiles/b"
    dba, la, ca = seed(a)
    dbb, lb, cb = seed(b)
    try:
        wa = QuietMailbox(ca, None)
        QuietMailbox(cb, None)
        old = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "original task")
        register_outgoing(SimpleNamespace(_session_db=dba, session_id="chat"), old, [], tmp_path / "unused", None)
        lb.release()
        script = tmp_path / "interrupted.py"
        script.write_text('''
import os
from pathlib import Path
from types import SimpleNamespace
from hermes_cli.active_sessions import try_acquire_active_session
from hermes_cli.quiet_mailbox import QuietMailbox
home = Path(os.environ['HERMES_HOME'])
lease, refusal = try_acquire_active_session(session_id='chat', surface='cli', config={}, registry_home=home,
    metadata={'live_session_id':'original-worker'})
assert refusal is None
worker = QuietMailbox(SimpleNamespace(_active_session_lease=lease, session_id='chat'), None)
def effect_then_die(text, author):
    with (home/'effect.txt').open('a', encoding='utf-8') as stream:
        stream.write(text+'\\n'); stream.flush(); os.fsync(stream.fileno())
    os._exit(91)
worker.drain_one(effect_then_die)
raise AssertionError('The task did not start')
''', encoding="utf-8")
        env = dict(os.environ, HERMES_HOME=str(b), HERMES_RUNTIME_DIR=str(tmp_path / "runtime"),
                   HERMES_TEST_ISOLATION="1", PYTHONPATH=str(Path(__file__).resolve().parents[2]))
        env[DELIVERY_ID_ENV] = old["id"]
        child = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
        assert child.returncode == 91, child.stderr
        interrupted = mailbox.read_delivery_result(b, old["id"])
        assert interrupted["status"] == "claimed"
        # A new writer can process new work, but must never replay the original effect.
        lb, refusal = try_acquire_active_session(session_id="chat", surface="cli", config={}, registry_home=b,
            metadata={"live_session_id": "replacement"})
        assert refusal is None
        wb = QuietMailbox(SimpleNamespace(_active_session_lease=lb, session_id="chat"), None)
        later = mailbox.deliver_to_live_owner(b, mailbox.find_canonical_live_owner(b), "later task")
        seen = []
        assert wb.drain_one(lambda text, author: seen.append(text) or {"final_response": "later result"})
        assert not wb.drain_one(lambda *_: seen.append("REPLAY"))
        assert seen == ["later task"]
        assert (b / "effect.txt").read_text() == "original task\n"
        assert mailbox.read_delivery_result(b, later["id"])["status"] == "settled"
        assert mailbox.read_delivery_result(b, old["id"])["status"] == "claimed"
        for _ in range(2):
            poll_outgoing(a, "chat")
        notices = []
        assert wa.drain_one(lambda text, author: notices.append(text) or {"final_response": "NO_REPLY"})
        assert not wa.drain_one(lambda *_: notices.append("DUPLICATE"))
        assert len(notices) == 1 and "requires_reconciliation" in notices[0]
        assert old["id"] in notices[0]
    finally:
        la.release(); lb.release(); dba.close(); dbb.close()
