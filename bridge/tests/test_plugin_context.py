"""plugin_context.py: the host-side context carries the operator's plugin objects and origins, in the
shape the plugins read them (plugins/README.md § What the context carries)."""

import json
import sys
from pathlib import Path

import tldrack.main as main
from tldrack.plugin_context import RackPluginContext, unclaimed_settings
from tldrack.settings import Settings, load_settings, parse_settings

PLUGIN = """
PLUGIN_NAME = "probe"
SEEN = {}

class Probe:
    name = PLUGIN_NAME
    def routes(self): return []
    def handle_get(self, subpath, query): return 404, {}
    def handle_post(self, subpath, query, body): return 404, {}

def create_plugin(context):
    reader = getattr(context, "plugin_settings", None)
    SEEN["settings"] = reader(PLUGIN_NAME) if callable(reader) else None
    SEEN["origins"] = getattr(context, "allowed_origins", None)
    return Probe()
"""


def settings(tmp_path: Path) -> Settings:
    return parse_settings(
        {"allowed_origins": ["https://rack.example"], "plugins": {"probe": {"enabled": True}, "ghost": {}}},
        tmp_path / "settings.json",
    )


def test_a_bare_context_reads_as_every_default():
    context = RackPluginContext(status_port=7682, log=lambda _m: None)
    assert context.plugin_settings("probe") == {} and context.allowed_origins == frozenset()


def test_a_mounted_plugin_reads_its_own_object_and_the_operator_origins(tmp_path, monkeypatch, capsys):
    plugin_dir = tmp_path / "probe"
    plugin_dir.mkdir()
    (plugin_dir / "bridge_plugin.py").write_text(PLUGIN)
    monkeypatch.setenv(main.PLUGINS_ENV, str(plugin_dir))
    registry, _dirs = main.load_rack_plugins(7681, 7682, "test", str(tmp_path), None, settings(tmp_path))
    assert "probe" in registry
    seen = sys.modules[type(registry["probe"]).__module__].SEEN
    assert seen["settings"] == {"enabled": True}
    assert seen["origins"] == frozenset({"https://rack.example"}), "loopback is never added"
    err = capsys.readouterr().err
    assert "plugins.ghost" in err and "plugins.probe" not in err, "only the unclaimed name is echoed"


def test_unclaimed_names_are_the_ones_no_plugin_answers_to(tmp_path):
    assert unclaimed_settings(settings(tmp_path), {"probe": object()}) == ["ghost"]
    assert unclaimed_settings(Settings(), {"probe": object()}) == []


def test_settings_json_round_trip_reaches_the_context(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"plugins": {"claude-skills": {"run": {"ci-sweep": True}}}}))

    context = RackPluginContext(status_port=7682, log=lambda _m: None, operator_settings=load_settings(path))
    assert context.plugin_settings("claude-skills") == {"run": {"ci-sweep": True}}
