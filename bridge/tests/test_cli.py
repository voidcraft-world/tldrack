"""The CLI's doors: `hooks`, `decisions import`, and what a bad configuration file does at entry.
Each runs in the test's own empty directories (conftest.py); none starts a bridge."""

import json

from click.testing import CliRunner

from tldrack import main, paths


def run(*args):
    return CliRunner().invoke(main.main, list(args), catch_exceptions=False)


def write_config(name, body):
    path = paths.config_dir() / name
    path.write_text(body if isinstance(body, str) else json.dumps(body))
    return path


def test_hooks_prints_the_same_json_as_print_hooks():
    printed = run("hooks", "--status-port", "7791")
    assert printed.exit_code == 0
    block = json.loads(printed.output)
    assert "Stop" in block["hooks"] and block == json.loads(run("--status-port", "7791", "--print-hooks").output)
    assert "7791" in json.dumps(block)


def test_hooks_install_names_an_unknown_profile(monkeypatch):
    result = run("hooks", "--install", "--profile", "nope")
    assert result.exit_code == 1 and "no profile 'nope'" in result.output


def test_decisions_import_reads_nothing_until_globs_are_listed(tmp_path):
    result = run("decisions", "import", "--root", str(tmp_path))
    assert result.exit_code != 0 and "decision_docs" in result.output


def test_decisions_import_reads_the_listed_globs_under_the_root(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "a.md").write_text("Decision (user, 2000-01-02): keep the broad allow rules.\n")
    (tmp_path / "other.md").write_text("Decision (user, 2000-01-03): this file is not listed.\n")
    write_config("settings.json", {"decision_docs": ["docs/**/*.md"]})
    result = run("decisions", "import", "--root", str(tmp_path))
    assert result.exit_code == 0 and "1 new from 1 docs" in result.output
    assert (paths.store_dir("decisions") / "ledger.jsonl").is_file()


def test_a_bad_settings_file_stops_every_command_but_doctor():
    write_config("settings.json", {"sesion_namer": True})
    result = run("hooks")
    assert result.exit_code != 0 and "unknown key 'sesion_namer'" in result.output and "tldrack doctor" in result.output


def test_a_bad_standing_mission_stops_the_start():
    write_config("settings.json", {"standing_missions": [{"key": "Bad Key", "title": "x"}]})
    result = run("hooks")
    assert result.exit_code != 0 and "standing_missions[0]" in result.output


def test_doctor_reports_a_broken_settings_file_instead_of_refusing_to_run(monkeypatch):
    write_config("settings.json", "{not json")
    monkeypatch.setattr(main, "fetch_snapshot", lambda *a, **k: None)
    monkeypatch.setattr(main, "port_holder", lambda port: None)
    result = run("doctor", "--port", "7781", "--status-port", "7791")
    assert "✗ settings" in result.output and "cannot read" in result.output
    assert result.exit_code == 1 and "to fix before the rack starts" in result.output
