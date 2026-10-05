"""Plugin reloads must tolerate unrelated imports without losing profile scope."""

import importlib
import sys
import threading
import types
from pathlib import Path

import pytest

from hermes_cli import plugins_loader


def test_eviction_survives_module_map_growth(monkeypatch):
    modules = {"hermes_plugins.example": object()}

    class ImportingName(str):
        def startswith(self, prefix):
            # A deterministic scheduling boundary: another import publishes a module
            # while the eviction predicate is inspecting a key.
            modules["unrelated.new_import"] = object()
            return super().startswith(prefix)

    modules[ImportingName("hermes_plugins.example.child")] = object()
    monkeypatch.setattr(plugins_loader, "sys", types.SimpleNamespace(modules=modules))
    plugins_loader._evict_modules("hermes_plugins.example")
    assert set(modules) == {"unrelated.new_import"}


def test_eviction_keeps_replacement_and_tolerates_concurrent_removal(monkeypatch):
    replacement = object()
    modules = {}

    class ReplacingName(str):
        def startswith(self, prefix):
            modules[str(self)] = replacement
            modules.pop("hermes_plugins.example.removed", None)
            return super().startswith(prefix)

    modules[ReplacingName("hermes_plugins.example.child")] = object()
    modules["hermes_plugins.example.removed"] = object()
    modules["hermes_plugins.example_sibling"] = object()
    monkeypatch.setattr(plugins_loader, "sys", types.SimpleNamespace(modules=modules))
    plugins_loader._evict_modules("hermes_plugins.example")
    assert modules["hermes_plugins.example.child"] is replacement
    assert "hermes_plugins.example.removed" not in modules
    assert "hermes_plugins.example_sibling" in modules


@pytest.mark.parametrize("load_timeout", ["0", "10"])  # inline, and the production deadline worker
def test_profile_reload_registers_real_tools_during_unrelated_imports(tmp_path, monkeypatch, load_timeout):
    from hermes_cli.plugins import PluginManager, PluginManifest
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools.registry import registry

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "a"))
    managers = []
    manifests = []
    for marker in ("a", "b"):
        home = tmp_path / marker
        plugin = home / "plugins" / "eviction-probe"
        plugin.mkdir(parents=True)
        (home / "config.yaml").write_text(f"plugins:\n  load_timeout_seconds: {load_timeout}\n", encoding="utf-8")
        (plugin / "state.py").write_text(f"MARKER = {marker!r}\n", encoding="utf-8")
        (plugin / "__init__.py").write_text(
            "from .state import MARKER\n"
            "def register(ctx):\n"
            "    ctx.register_tool('eviction_probe', 'eviction_probe', "
            "{'name': 'eviction_probe', 'parameters': {'type': 'object', 'properties': {}}}, "
            "lambda *args, **kwargs: MARKER)\n", encoding="utf-8",
        )
        token = set_hermes_home_override(home)
        try:
            managers.append(PluginManager())
        finally:
            reset_hermes_home_override(token)
        manifests.append(PluginManifest(name="eviction-probe", path=plugin, source="user"))

    import_dir = tmp_path / "imports"
    import_dir.mkdir()
    module_name = "hermes_eviction_unrelated_probe"
    (import_dir / f"{module_name}.py").write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(import_dir))
    stop = threading.Event()
    started = threading.Event()
    failures = []

    def import_loop():
        try:
            while not stop.is_set():
                importlib.import_module(module_name)
                started.set()
                sys.modules.pop(module_name, None)
        except BaseException as exc:
            failures.append(exc)
            started.set()

    worker = threading.Thread(target=import_loop)
    worker.start()
    try:
        assert started.wait(5)
        for index in (0, 1, 0) * 5:
            manager = managers[index]
            manager._load_plugin(manifests[index])
            loaded = manager._plugins["eviction-probe"]
            assert not loaded.error
            assert loaded.module.MARKER == ("a", "b")[index]
            entry = registry.get_entry("eviction_probe", scope=manager.scope_key)
            assert entry is not None
            assert entry.handler() == ("a", "b")[index]
        assert not failures
        assert managers[0]._plugins["eviction-probe"].module.__name__ != managers[1]._plugins["eviction-probe"].module.__name__
    finally:
        stop.set()
        worker.join(5)
        assert not worker.is_alive()
        sys.modules.pop(module_name, None)
        for manager in managers:
            manager.unload()


def test_failed_abandoned_load_does_not_evict_its_successor(monkeypatch):
    """A deadline-abandoned loader that fails late cleans up only its own generation."""
    stale, successor = types.ModuleType("hermes_plugins.example"), types.ModuleType("hermes_plugins.example")
    child = types.ModuleType("hermes_plugins.example.child")
    modules = {"hermes_plugins.example": successor, "hermes_plugins.example.child": child}
    monkeypatch.setattr(plugins_loader, "sys", types.SimpleNamespace(modules=modules))
    plugins_loader._evict_modules("hermes_plugins.example", owner=stale)
    assert modules == {"hermes_plugins.example": successor, "hermes_plugins.example.child": child}
    plugins_loader._evict_modules("hermes_plugins.example", owner=successor)
    assert modules == {}


def test_real_deadline_abandonment_then_late_failure_keeps_successor(tmp_path, monkeypatch):
    """Exercise timeout -> successful reload -> stale import failure with real loader threads."""
    from hermes_cli.plugins import PluginManager, PluginManifest
    from tools.registry import registry

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("plugins:\n  load_timeout_seconds: 0.2\n", encoding="utf-8")
    control_name = "hermes_deadline_eviction_test_control"
    control = types.ModuleType(control_name)
    control.entered, control.release = threading.Event(), threading.Event()
    control.attempt = 0
    monkeypatch.setitem(sys.modules, control_name, control)
    plugin = tmp_path / "plugins/deadline-eviction-probe"
    plugin.mkdir(parents=True)
    (plugin / "state.py").write_text("MARKER = object()\n", encoding="utf-8")
    (plugin / "__init__.py").write_text(
        f"import {control_name} as control\n"
        "from . import state\n"
        "control.attempt += 1\n"
        "if control.attempt == 1:\n"
        "    control.entered.set()\n"
        "    if not control.release.wait(10): raise RuntimeError('test release missing')\n"
        "    raise RuntimeError('late stale import failure')\n"
        "def register(ctx):\n"
        "    ctx.register_tool('deadline_eviction_probe', 'deadline_eviction_probe', "
        "{'name': 'deadline_eviction_probe', 'parameters': {'type': 'object', 'properties': {}}}, "
        "lambda *args, **kwargs: state.MARKER)\n", encoding="utf-8")
    manager = PluginManager()
    manifest = PluginManifest(name="deadline-eviction-probe", path=plugin, source="user")
    abandoned_before = set(plugins_loader._ABANDONED_LOADERS)
    workers = []
    try:
        manager._load_plugin(manifest)
        assert control.entered.wait(3)
        assert "timed out" in manager._plugins[manifest.name].error
        workers = [t for t in plugins_loader._ABANDONED_LOADERS if t not in abandoned_before]
        assert len(workers) == 1 and workers[0].is_alive()
        # Give the successor a generous real deadline; it is never blocked by the test events.
        (tmp_path / "config.yaml").write_text("plugins:\n  load_timeout_seconds: 10\n", encoding="utf-8")
        manager._load_plugin(manifest)
        loaded = manager._plugins[manifest.name]
        assert not loaded.error
        successor = loaded.module
        child = sys.modules[successor.__name__ + ".state"]
        handler = registry.get_entry("deadline_eviction_probe", scope=manager.scope_key)
        assert handler.handler() is child.MARKER
        control.release.set()
        workers[0].join(5)
        assert not workers[0].is_alive()
        assert sys.modules[successor.__name__] is successor
        assert sys.modules[successor.__name__ + ".state"] is child
        assert registry.get_entry("deadline_eviction_probe", scope=manager.scope_key).handler() is child.MARKER
        assert manager._plugins[manifest.name] is loaded
    finally:
        control.release.set()
        for worker in workers:
            worker.join(5)
        manager.unload()
        with plugins_loader._ABANDONED_LOADERS_LOCK:
            plugins_loader._ABANDONED_LOADERS[:] = [
                t for t in plugins_loader._ABANDONED_LOADERS if t not in workers or t.is_alive()]
