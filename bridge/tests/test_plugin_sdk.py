"""The host keeps SDK diagnostics, mount identities and cleanup registration."""

from types import SimpleNamespace

import pytest

import tldrack.main as main
from tldrack.icons import SessionIconStore
from tldrack.models import SessionModelStore
from voidcraft_world_bridge.plugins import PluginContext, PluginRegistry, load_plugins


@pytest.mark.parametrize("bind_fails", [True, False])
@pytest.mark.parametrize("watching", [True, False])
def test_status_server_owns_plugin_cleanup(tmp_path, monkeypatch, bind_fails, watching):
    calls, callbacks = [], []
    plugins = {"owned": SimpleNamespace(close=lambda: calls.append("plugin closed"))}
    watcher = SimpleNamespace(stop=lambda: calls.append("watcher stopped")) if watching else None
    expected = (["watcher stopped"] if watching else []) + ["plugin closed"]
    monkeypatch.setattr(main, "load_bridge_profiles", lambda: None)
    monkeypatch.setattr(main, "load_rack_plugins", lambda *a: (plugins, []))
    monkeypatch.setattr(main, "start_freshness", lambda *a, **kw: watcher)
    monkeypatch.setattr(main.atexit, "register", lambda *a: callbacks.append(a))

    def start(*a, **kw):
        if bind_fails:
            raise OSError("port taken")

    monkeypatch.setattr(main, "start_status_server", start)
    if bind_fails:
        with pytest.raises(OSError, match="port taken"):
            main.serve_status(7681, 7682, "test", str(tmp_path), {}, main.ShutdownControl("status-only"))
        assert calls == expected
        assert not callbacks
    else:
        main.serve_status(7681, 7682, "test", str(tmp_path), {}, main.ShutdownControl("status-only"))
        assert not calls
        for callback, *args in reversed(callbacks):
            callback(*args)
        assert calls == expected


def test_failed_factory_does_not_claim_freshness_watch_for_later_valid_plugin(tmp_path):
    rejected, valid = tmp_path / "rejected", tmp_path / "valid"
    rejected.mkdir()
    valid.mkdir()
    (rejected / "bridge_plugin.py").write_text('PLUGIN_NAME = "demo"\ndef create_plugin(context): return None\n')
    (valid / "bridge_plugin.py").write_text(
        "from voidcraft_world_bridge.routing import RoutedPlugin\n"
        'PLUGIN_NAME = "demo"\ndef create_plugin(context): return RoutedPlugin("demo", [])\n'
    )
    registry = load_plugins([rejected, valid], PluginContext(status_port=7682, log=lambda message: None))
    assert main.mounted_plugin_dirs([rejected, valid], registry, claimed=set()) == [valid]


def test_host_snapshot_preserves_failed_plugin_diagnostics(tmp_path, monkeypatch):
    monkeypatch.setattr(main, "list_tmux_sessions", dict)
    monkeypatch.setattr(main, "list_tmux_clients", dict)
    registry = PluginRegistry({})
    registry.diagnostics.append({"entry": "broken", "state": "failed", "error": "ValueError"})
    handler = main.build_status_handler(
        store=main.StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="test",
        bridge_info={"pid": 1234, "started_at": 0.0},
        control=main.ShutdownControl("status-only"),
        plugins=registry,
    )
    assert object.__new__(handler).snapshot_payload()["plugin_diagnostics"] == registry.diagnostics
