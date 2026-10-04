"""Two Hermes processes holding the same ``hermes auth add openai-codex`` pool row must spend its
single-use refresh token once, and must never fall back to a pair that was already consumed.

A ``manual:device_code`` row rotates in the credential-pool store only; it never writes back to the
``providers.openai-codex`` singleton (#39236). The in-lock re-sync consulted only the singleton, so a
second process that loaded the row before a peer's rotation POSTed the consumed token and lost the
login ("refresh token was already consumed", S4 soak-03, 10 bots sharing one login). Each process
is modelled as its own ``CredentialPool`` over the same on-disk auth store, with a token endpoint
that honours each refresh token exactly once.
"""

import base64
import itertools
import json

import pytest

from agent.credential_pool import AUTH_TYPE_OAUTH, CredentialPool, PooledCredential
from hermes_cli import auth as A
import hermes_cli.auth_codex as auth_codex

PROVIDER = "openai-codex"


def _jwt(generation: int) -> str:
    """Unsigned Codex-shaped access token: one principal, distinct per generation, no expiry."""
    claims = {"sub": "user-1", "https://api.openai.com/auth": {"chatgpt_account_id": "acct-1"}, "gen": generation}
    body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"e30.{body}.sig"


def _row(generation: int) -> PooledCredential:
    return PooledCredential(provider=PROVIDER, id="codex-1", label="soak", auth_type=AUTH_TYPE_OAUTH,
                            priority=0, source="manual:device_code", access_token=_jwt(generation),
                            refresh_token=f"refresh-{generation}")


@pytest.fixture
def token_endpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(A, "_auth_file_path", lambda: tmp_path / "auth.json")
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: None)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    state = {"live": {"refresh-0"}, "presented": []}
    generation = itertools.count(1)

    def refresh(access_token, refresh_token, **_kwargs):
        state["presented"].append(refresh_token)
        if refresh_token not in state["live"]:
            raise RuntimeError("refresh_token_reused")
        state["live"].discard(refresh_token)
        n = next(generation)
        state["live"].add(f"refresh-{n}")
        return {"access_token": _jwt(n), "refresh_token": f"refresh-{n}", "last_refresh": f"2026-10-04T00:00:0{n}Z"}

    monkeypatch.setattr(A, "refresh_codex_oauth_pure", refresh)
    monkeypatch.setattr(auth_codex, "refresh_codex_oauth_pure", refresh)
    return state


@pytest.mark.parametrize("stale_unstamped_singleton", [False, True])
def test_second_process_adopts_a_peer_rotation_instead_of_replaying_the_consumed_token(
        token_endpoint, stale_unstamped_singleton):
    if stale_unstamped_singleton:
        # The same principal's login pair, never advanced by pool-row rotations and without a
        # last_refresh stamp: adopt-on-difference must not let it supersede the peer's rotation.
        with A._auth_store_lock():
            store = A._load_auth_store()
            A._save_provider_state(store, PROVIDER, {"tokens": {"access_token": _jwt(0), "refresh_token": "refresh-0"}})
            A._save_auth_store(store)
    A.write_credential_pool(PROVIDER, [_row(0).to_dict()])

    # Both processes loaded the row before either refreshed; both then see a 401 on generation 0.
    first, second = CredentialPool(PROVIDER, [_row(0)]), CredentialPool(PROVIDER, [_row(0)])
    rotated = first.try_refresh_matching(credential_id="codex-1")
    adopted = second.try_refresh_matching(credential_id="codex-1")

    live, presented = token_endpoint["live"], token_endpoint["presented"]
    assert rotated is not None and adopted is not None
    assert presented.count("refresh-0") == 1, f"the consumed refresh token was replayed: {presented}"
    assert adopted.refresh_token in live and adopted.access_token == rotated.access_token
    on_disk = next(r for r in A.read_credential_pool(PROVIDER) if r.get("id") == "codex-1")
    assert on_disk["refresh_token"] in live
