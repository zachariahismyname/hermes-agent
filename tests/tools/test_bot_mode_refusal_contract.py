import json
from pathlib import Path
import sys
from types import SimpleNamespace

from tools import bot_mode_dm


def test_delivery_uses_refusal_code_before_human_wording(tmp_path, capsys):
    for code, message, busy in (
        ("SESSION_NOT_OWNED", "Ce chat est occupé.", True),
        ("SESSION_COORDINATION_UNAVAILABLE", "Cannot verify whether session already has a live owner", False),
        ("SESSION_NOT_OWNED_EXTRA", "Different failure", False),
    ):
        dm = tmp_path / "message.txt"
        dm.write_text("isolated probe", encoding="utf-8")
        child = tmp_path / "child.py"
        child.write_text(f"import sys\nprint({f'hermes-refusal-reason: {code}'!r}, file=sys.stderr)\nprint({message!r}, file=sys.stderr)\nraise SystemExit(1)\n", encoding="utf-8")
        assert bot_mode_dm._run_delivery([sys.executable, str(child)], str(dm), stdin_file=False) == 1
        output = capsys.readouterr()
        assert (json.loads(output.out)["reason"] == "target_busy") if busy else not output.out
        # A2A admission and recovery: an unconsumed payload is retained as
        # evidence, never deleted on refusal or failure; a busy refusal names it.
        assert dm.read_text(encoding="utf-8") == "isolated probe"
        pending = Path(str(dm) + ".pending.json")
        if busy:
            assert json.loads(output.out)["evidence_file"] == str(dm)
            # Proven pre-execution refusal: no reconciliation marker blocks a deliberate retry.
            assert not pending.exists()
        else:
            assert pending.exists()  # Outcome not proven: the attempt stays reserved for reconciliation.
        dm.unlink()
        pending.unlink(missing_ok=True)


def test_one_shot_cli_preserves_refusal_reason(monkeypatch, capsys):
    from cli import HermesCLI
    from hermes_cli import active_sessions
    refusal = active_sessions.ActiveSessionRefusal("Ce chat est occupé.", reason=active_sessions.SESSION_NOT_OWNED)
    monkeypatch.setattr(active_sessions, "try_acquire_active_session", lambda **kwargs: (None, refusal))
    cli = SimpleNamespace(_active_session_lease=None, session_id="isolated", config={})
    assert not HermesCLI._claim_active_session(cli, stderr=True)
    assert "hermes-refusal-reason: SESSION_NOT_OWNED\n" in capsys.readouterr().err


def test_retry_refused_after_an_executed_attempt_is_unknown_not_busy(tmp_path, capsys, monkeypatch):
    """Attempt one runs (and has an effect) then fails retryably; the policy retry is refused because
    another turn took the chat. The payload may have executed: it must not be reported as 'no turn
    executed', must keep its reconciliation reservation, and must never be handed to a live owner."""
    monkeypatch.setattr(bot_mode_dm, "_admit_live_dm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("handed to another owner")))
    dm = tmp_path / "message.txt"
    dm.write_text("isolated probe", encoding="utf-8")
    effects = tmp_path / "effects.txt"
    child = tmp_path / "child.py"
    child.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"effects = Path({str(effects)!r})\n"
        "if not effects.exists():\n"
        "    effects.write_text('one effect\\n')\n"
        "    print('Error: rate limit exceeded', file=sys.stderr)\n"
        "else:\n"
        "    print('hermes-refusal-reason: SESSION_NOT_OWNED', file=sys.stderr)\n"
        "raise SystemExit(1)\n", encoding="utf-8")
    assert bot_mode_dm._run_delivery([sys.executable, str(child)], str(dm), stdin_file=False) == 1
    outcome = json.loads(capsys.readouterr().out)
    assert outcome["status"] == "ambiguous" and outcome["evidence_file"] == str(dm)
    assert Path(str(dm) + ".pending.json").exists() and dm.exists()
    assert effects.read_text() == "one effect\n"
