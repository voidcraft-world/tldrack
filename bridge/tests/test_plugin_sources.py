"""Which plugins a bridge mounts: the env var, then an existing plugins.json, then the checkout's defaults."""

import json
import subprocess
import sys

from click.testing import CliRunner

import tldrack.main as main
from tldrack import doctor, plugin_sources
from tldrack.plugin_sources import DEFAULT_PLUGINS, PLUGINS_ENV, resolve_plugin_source

#: plugins/README.md's off-by-default plugins.
OFF_BY_DEFAULT = {"coordinator", "devices"}


def fake_checkout(root, names):
    for name in names:
        (root / name).mkdir(parents=True)
        (root / name / "bridge_plugin.py").write_text("PLUGIN_NAME = 'x'\n")
    return root


def test_every_default_plugin_is_in_the_checkout():
    """The list and the tree cannot drift: each default has an entry module under plugins/."""
    root = plugin_sources.checkout_plugins_dir()
    missing = [name for name in DEFAULT_PLUGINS if not (root / name / "bridge_plugin.py").is_file()]
    assert not missing, f"DEFAULT_PLUGINS names plugins the checkout lacks: {missing}"
    assert not OFF_BY_DEFAULT & set(DEFAULT_PLUGINS)
    shipped = {path.parent.name for path in root.glob("*/bridge_plugin.py")}
    assert shipped - set(DEFAULT_PLUGINS) == OFF_BY_DEFAULT, "a new plugin needs a default: add it or list it off"


def test_nothing_configured_mounts_the_checkout_defaults(tmp_path):
    root = fake_checkout(tmp_path / "plugins", ["laya", "worktree", "coordinator"])
    source = resolve_plugin_source({}, tmp_path / "plugins.json", checkout=root)
    assert source.kind == plugin_sources.DEFAULTS
    assert source.dirs == [root / "worktree", root / "laya"], "defaults only, in DEFAULT_PLUGINS order"
    assert "checkout defaults" in source.describe()


def test_no_checkout_mounts_none(tmp_path):
    source = resolve_plugin_source({}, tmp_path / "plugins.json", checkout=tmp_path / "missing")
    assert source.kind == plugin_sources.NONE and source.dirs == []


def test_an_existing_plugins_json_wins_even_when_empty(tmp_path):
    root = fake_checkout(tmp_path / "plugins", DEFAULT_PLUGINS)
    config = tmp_path / "plugins.json"
    config.write_text(json.dumps({"version": 1, "plugins": []}))
    source = resolve_plugin_source({}, config, checkout=root)
    assert source.kind == plugin_sources.FILE and source.dirs == []
    config.write_text("{not json")
    assert resolve_plugin_source({}, config, checkout=root).dirs == [], "a corrupt file is still the operator's"
    config.write_text(json.dumps({"plugins": [str(tmp_path / "mine")]}))
    assert resolve_plugin_source({}, config, checkout=root).dirs == [tmp_path / "mine"]


def test_the_env_var_wins_over_everything(tmp_path):
    config = tmp_path / "plugins.json"
    config.write_text(json.dumps({"plugins": [str(tmp_path / "file")]}))
    source = resolve_plugin_source(
        {PLUGINS_ENV: f"{tmp_path / 'a'}:{tmp_path / 'b'}"}, config, checkout=fake_checkout(tmp_path / "plugins", ["laya"])
    )
    assert source.kind == plugin_sources.ENV and source.dirs == [tmp_path / "a", tmp_path / "b"]
    assert source.describe() == f"env {PLUGINS_ENV}"


def test_doctor_says_where_the_list_came_from():
    base = dict(tools={}, paths={}, settings={}, ports={}, status_port=7682, ui_dist=None)
    running = doctor.Facts(**base, snapshot={"plugins": {"laya": {}}}, plugin_source="checkout defaults (/x)")
    assert doctor.plugins_check(running).detail == "laya  (from checkout defaults (/x))"
    down = doctor.Facts(**base, snapshot=None, plugin_source="plugins.json (/c)")
    assert doctor.plugins_check(down).detail.endswith("(from plugins.json (/c))")


def test_laya_passes_every_word_through(monkeypatch, tmp_path):
    cli = tmp_path / "laya" / "cli.py"
    cli.parent.mkdir()
    cli.write_text("import sys\nprint(' '.join(sys.argv[1:]))\nraise SystemExit(3)\n")
    monkeypatch.setattr(plugin_sources, "checkout_plugins_dir", lambda: tmp_path)
    calls = []
    real_run = subprocess.run

    def run(argv, **kwargs):
        calls.append(argv)
        return real_run(argv, **kwargs)

    monkeypatch.setattr(main.subprocess, "run", run)
    result = CliRunner().invoke(main.main, ["laya", "install", "--status-port", "7791", "--help"])
    assert result.exit_code == 3
    assert calls == [[sys.executable, str(cli), "install", "--status-port", "7791", "--help"]]


def test_laya_without_a_checkout_says_so(monkeypatch, tmp_path):
    monkeypatch.setattr(plugin_sources, "checkout_plugins_dir", lambda: tmp_path)
    result = CliRunner().invoke(main.main, ["laya", "status"])
    assert result.exit_code == 1 and "runs from a checkout" in result.output
