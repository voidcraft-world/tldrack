"""The operator's settings: only what validates is used, and every rejection is said once and does less."""

import os

import pytest

from devices_plugin.settings import DevicesSettings, parse_settings, read_settings


@pytest.fixture()
def log():
    lines = []
    return lines


def parse(raw, lines):
    return parse_settings(raw, lines.append)


def executable(tmp_path, name="jobs-tool"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_off_reads_nothing_past_enabled(log):
    # An invalid lease and jobs command say nothing while the plugin is off: they are never read.
    assert parse({"enabled": False, "lease": 7, "jobs": "nope", "daemons": 3}, log) == DevicesSettings()
    assert log == []


def test_on_with_nothing_else_is_the_plain_readout(log):
    assert parse({"enabled": True, "unknown": "ignored"}, log) == DevicesSettings(enabled=True)
    assert log == []


def test_a_seam_that_is_missing_raises_or_answers_a_non_object_reads_as_off(log):
    class Raising:
        def plugin_settings(self, _name):
            raise RuntimeError("broken seam")

    class Listy:
        def plugin_settings(self, _name):
            return ["enabled"]

    for context in (None, object(), Raising(), Listy()):
        assert read_settings(context, log.append) == DevicesSettings()
    assert log == []


def test_a_valid_lease_is_an_absolute_py_file(tmp_path, log):
    lease = tmp_path / "lease.py"
    lease.write_text("def status():\n    return {}\n")
    assert parse({"enabled": True, "lease": str(lease)}, log).lease == lease
    assert log == []


def test_a_lease_under_home_may_start_with_a_tilde(tmp_path, monkeypatch, log):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "lease.py").write_text("")
    assert parse({"enabled": True, "lease": "~/lease.py"}, log).lease == tmp_path / "lease.py"


@pytest.mark.parametrize(
    "value, why",
    [
        (7, "must be a path to a .py file"),
        ("", "must be a path to a .py file"),
        ("lease.py", "must be an absolute path"),
        ("/no/such/lease.py", "names no .py file"),
    ],
)
def test_a_lease_that_does_not_validate_is_reported_and_dropped(value, why, log):
    settings = parse({"enabled": True, "lease": value}, log)
    assert settings.enabled and settings.lease is None
    assert len(log) == 1 and why in log[0] and log[0].endswith("no lease readout")


def test_a_lease_that_is_not_python_is_dropped(tmp_path, log):
    other = tmp_path / "lease.json"
    other.write_text("{}")
    assert parse({"enabled": True, "lease": str(other)}, log).lease is None
    assert "names no .py file" in log[0]


def test_a_jobs_command_resolves_its_program_and_keeps_its_placeholders(tmp_path, log):
    tool = executable(tmp_path)
    command = [str(tool), "--json", "--every", "{seconds}", "--host", "{host}"]
    assert parse({"enabled": True, "jobs": command}, log).jobs == tuple(command)
    assert log == []


def test_a_jobs_program_on_path_is_resolved_to_where_it_is(tmp_path, monkeypatch, log):
    tool = executable(tmp_path)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    jobs = parse({"enabled": True, "jobs": ["jobs-tool", "--host", "{host}"]}, log).jobs
    assert jobs == (str(tool), "--host", "{host}")


@pytest.mark.parametrize(
    "value, why",
    [
        ("jobs-tool --host {host}", "must be a list"),
        ([], "must be a list"),
        (["/bin/sh", ""], "must be a list"),
        (["/bin/sh", 3], "must be a list"),
        (["/bin/sh", "--all"], "must name the host with {host}"),
        (["no-such-tool-anywhere", "{host}"], "not an executable on the bridge's PATH"),
        (["relative/tool", "{host}"], "not an executable on the bridge's PATH"),
    ],
)
def test_a_jobs_command_that_does_not_validate_is_reported_and_dropped(value, why, log):
    settings = parse({"enabled": True, "jobs": value}, log)
    assert settings.enabled and settings.jobs is None
    assert len(log) == 1 and why in log[0]


def test_a_jobs_program_that_is_not_executable_is_dropped(tmp_path, log):
    plain = tmp_path / "jobs-tool"
    plain.write_text("")
    assert parse({"enabled": True, "jobs": [str(plain), "{host}"]}, log).jobs is None


def test_too_long_a_jobs_command_is_dropped(tmp_path, log):
    tool = executable(tmp_path)
    assert parse({"enabled": True, "jobs": [str(tool), "{host}", *["x"] * 40]}, log).jobs is None


def test_daemons_are_a_list_of_strings(log):
    assert parse({"enabled": True, "daemons": ["my-mcp-service", "other-daemon"]}, log).daemons == (
        "my-mcp-service",
        "other-daemon",
    )
    assert parse({"enabled": True, "daemons": "my-mcp-service"}, log).daemons == ()
    assert len(log) == 1 and "daemons must be a list" in log[0]
