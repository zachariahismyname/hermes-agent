"""Config health must not prescribe mapping values for nullable scalar settings."""

from tui_gateway.server import _probe_config_health


def test_null_session_cap_is_valid_and_not_rewritten(tmp_path, monkeypatch):
    from pathlib import Path
    from hermes_cli.config_effective import load_user_config_effective
    from hermes_cli.active_sessions import coerce_max_concurrent_sessions

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = tmp_path / "config.yaml"
    original = "max_concurrent_sessions: null\nagent:\n  max_turns: 10\n"
    config.write_text(original, encoding="utf-8")
    cfg = load_user_config_effective()
    assert cfg["max_concurrent_sessions"] is None
    assert coerce_max_concurrent_sessions(cfg["max_concurrent_sessions"]) is None
    assert _probe_config_health(cfg) == ""
    assert config.read_text(encoding="utf-8") == original


def test_only_null_mapping_sections_receive_mapping_advice():
    warning = _probe_config_health({"agent": None, "display": None, "max_concurrent_sessions": None})
    assert "`agent`" in warning and "`display`" in warning
    assert "max_concurrent_sessions" not in warning
    assert "{}" in warning


def test_bare_sections_missing_from_defaults_still_warn():
    """mcp_servers is not in DEFAULT_CONFIG; a bare `mcp_servers:` silently drops every server."""
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert "mcp_servers" not in DEFAULT_CONFIG
    warning = _probe_config_health({"mcp_servers": None, "fallback_providers": None})
    assert "`mcp_servers`" in warning and "`mcp_servers: {}`" in warning
    # A list-valued setting is never told to become a mapping.
    assert "`fallback_providers: []`" in warning


def test_null_scalar_settings_are_never_told_to_become_mappings():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    scalars = [k for k, v in DEFAULT_CONFIG.items() if isinstance(v, (str, int, float, bool))]
    assert scalars
    assert _probe_config_health({k: None for k in scalars}) == ""


def test_optional_scalar_aliases_and_unknown_roots_do_not_get_mapping_advice():
    from gateway.config import GatewayConfig
    assert GatewayConfig.from_dict({"multiplex_profiles": None}).multiplex_profiles is None
    assert _probe_config_health({"multiplex_profiles": None, "group_sessions_per_user": None,
                                 "unknown_future_setting": None}) == ""


def test_optional_container_aliases_get_type_correct_advice():
    warning = _probe_config_health({"custom_providers": None, "reset_triggers": None,
                                  "profile_routes": None, "platforms": None})
    for key in ("custom_providers", "reset_triggers", "profile_routes"):
        assert f"`{key}: []`" in warning
    assert "`platforms: {}`" in warning


def test_every_optional_root_has_a_null_classification():
    """A new optional root must be classified: as a container it warns when bare, otherwise null is valid.

    Contract between the known-root registry and the health probe, so a new container root
    (platform_toolsets was missed once) cannot silently lose its empty-section warning.
    """
    from hermes_cli.config import _EXTRA_KNOWN_ROOT_KEYS
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from tui_gateway.server import _OPTIONAL_ROOT_CONTAINERS

    nullable = {"fallback_model", "group_sessions_per_user", "thread_sessions_per_user", "stt_echo_transcripts",
                "always_log_local", "filter_silence_narration", "multiplex_profiles", "require_mention",
                "unauthorized_dm_behavior", "allow_all_users"}
    unclassified = set(_EXTRA_KNOWN_ROOT_KEYS) - set(DEFAULT_CONFIG) - set(_OPTIONAL_ROOT_CONTAINERS) - nullable
    assert not unclassified
    assert _probe_config_health({k: None for k in nullable}) == ""
    warning = _probe_config_health({"platform_toolsets": None, "tool_gateway_declined_tools": None})
    assert "`platform_toolsets: {}`" in warning and "`tool_gateway_declined_tools: []`" in warning
