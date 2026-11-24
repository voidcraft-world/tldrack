"""doctor.py: every line `tldrack doctor` can print, from invented facts. Nothing here reads the machine."""

from pathlib import Path

from tldrack import doctor
from tldrack.doctor import FAIL, OFF, OK, WARN, Facts

ALL_TOOLS = {
    "tmux": "/bin/tmux",
    "ttyd": "/bin/ttyd",
    "node": "/bin/node",
    "uv": "/bin/uv",
    "claude": "/bin/claude",
    "codex": None,
    "npm": "/bin/npm",
}
PATHS = {
    "config": {"dir": "/h/.config/tldrack", "source": "default"},
    "state": {"dir": "/h/.local/state/tldrack", "source": "default"},
    "data": {"dir": "/big/tldrack", "source": "paths.json"},
    "stores": {
        "ledger": {"dir": "/archive/agent-ledger", "source": "paths.json"},
        "rack": {"dir": "/big/tldrack/agent-rack", "source": "data"},
    },
}
PARTS = {"session namer": False, "live marks": False, "supervisor": False, "extra origins": False}


def facts(**over) -> Facts:
    base = dict(
        tools=ALL_TOOLS,
        paths=PATHS,
        settings=PARTS,
        profiles=[("claude", "claude", True, True), ("shell", "shell", True, None)],
        ports={7681: None, 7682: None},
        status_port=7682,
        snapshot=None,
        ui_dist=Path("/repo/ui/dist"),
    )
    return Facts(**{**base, **over})


def levels(sections) -> dict[str, str]:
    return {check.name: check.level for _, checks in sections for check in checks}


def test_a_ready_machine_with_nothing_running():
    sections = doctor.run_checks(facts())
    got = levels(sections)
    assert got["tmux"] == got["ttyd"] == got["agent CLI"] == got["port 7682"] == got["hooks claude"] == OK
    assert got["bridge"] == OFF and got["session namer"] == OFF and got["phone pushes"] == OFF
    assert "hooks shell" not in got, "a plain shell has no hooks"
    assert got["store ledger"] == OK and "store rack" not in got, "only a mapped store is worth a line"
    assert doctor.verdict(sections) == (0, "ready")


def test_missing_tmux_or_ttyd_stops_the_rack_and_names_the_fix():
    sections = doctor.run_checks(facts(tools={**ALL_TOOLS, "tmux": None, "ttyd": None}))
    assert levels(sections)["tmux"] == FAIL
    code, closing = doctor.verdict(sections)
    assert code == 1 and closing.startswith("2 problems")
    line = next(check.line() for _, checks in sections for check in checks if check.name == "ttyd")
    assert "✗ ttyd" in line and "→ brew install ttyd" in line


def test_no_agent_cli_and_no_node_are_warnings_not_failures():
    sections = doctor.run_checks(facts(tools={**ALL_TOOLS, "claude": None, "node": None}, ui_dist=None))
    got = levels(sections)
    assert got["agent CLI"] == WARN and got["node"] == WARN and got["page"] == WARN
    assert doctor.verdict(sections) == (0, "ready, with 3 parts missing")


def test_hooks_not_installed_names_the_command():
    checks = doctor.hook_checks([("claude", "claude", True, False), ("codex", "codex", False, False)])
    assert checks[0].level == WARN and checks[0].fix == "tldrack hooks --install"
    assert checks[1].level == OFF, "a CLI that is not installed has nothing to install hooks into"


def test_a_port_held_by_another_program_is_named():
    held = {7681: (55, "/usr/bin/python3 -m http.server 7681"), 7682: None}
    check = doctor.port_checks(held, 7682, None)[0]
    assert check.level == FAIL and "pid 55" in check.detail and "--port" in check.fix


def test_a_running_rack_holding_its_own_ports_is_ok():
    held = {7681: (10, "ttyd -p 7681"), 7682: (11, "python -m tldrack.main")}
    sections = doctor.run_checks(facts(ports=held, snapshot={"plugins": {"worktree": {}, "laya": {}}}))
    got = levels(sections)
    assert got["port 7681"] == got["port 7682"] == got["bridge"] == got["plugins"] == OK
    detail = next(c.detail for _, cs in sections for c in cs if c.name == "plugins")
    assert detail == "laya, worktree"


def test_a_status_port_that_is_held_but_silent_is_a_failure():
    check = doctor.port_checks({7682: (11, "python -m tldrack.main")}, 7682, None)[0]
    assert check.level == FAIL and "tldrack kill && tldrack up" in check.fix


def test_a_broken_configuration_file_is_reported_not_raised():
    sections = doctor.run_checks(
        facts(paths={"error": "paths.json: unknown key 'cache'"}, settings="settings.json: unknown key 'x'")
    )
    got = levels(sections)
    assert got["paths"] == FAIL and got["settings"] == FAIL and doctor.verdict(sections)[0] == 1


def test_optional_parts_say_how_to_turn_each_on():
    checks = doctor.optional_checks(facts(settings={**PARTS, "session namer": True}, next_step_mode="shadow"))
    by_name = {check.name: check for check in checks}
    assert by_name["session namer"].level == OK and by_name["next step"].level == OK
    assert "tldrack notify --init" in by_name["phone pushes"].detail


def test_plugin_settings_name_what_is_configured_and_what_did_not_mount():
    def check(**over):
        return {c.name: c for c in doctor.optional_checks(facts(**over))}["plugin settings"]

    assert check().level == OFF and '"plugins"' in check().detail
    assert check(plugin_settings=("laya", "coordinator")).level == OK, "bridge down: nothing to compare"
    up = {"plugins": {"laya": {}, "coordinator": {}}}
    assert check(plugin_settings=("laya", "coordinator"), snapshot=up).level == OK
    stray = check(plugin_settings=("laya", "devcies"), snapshot=up)
    assert stray.level == WARN and "not mounted: devcies" in stray.detail


def test_what_is_switched_off_is_listed_not_warned_about():
    def check(**over):
        return {c.name: c for c in doctor.optional_checks(facts(**over))}["switched off"]

    assert check().level == OK and "every part runs" in check().detail
    off = check(features_off=("code_watch", "plugin:laya"))
    assert off.level == OFF and "code_watch, plugin:laya" in off.detail and "Settings page" in off.detail
    broken = check(features_error="features.json could not be read, so nothing is switched off: x")
    assert broken.level == WARN and "features.json" in broken.fix
    assert doctor.verdict(doctor.run_checks(facts(features_off=("code_watch",)))) == (0, "ready"), "off is a choice"


def test_the_page_reads_the_same_checks_as_json():
    sections = doctor.run_checks(facts(profiles=[("claude", "claude", True, False)], tools={**ALL_TOOLS, "ttyd": None}))
    body = doctor.wire(sections)
    assert [section["title"] for section in body["sections"]] == [title for title, _ in sections]
    assert (body["exit"], body["verdict"]) == doctor.verdict(sections)
    checks = {check["name"]: check for section in body["sections"] for check in section["checks"]}
    assert checks["ttyd"] == {
        "name": "ttyd",
        "level": FAIL,
        "detail": "not found",
        "fix": doctor.REQUIRED_TOOLS["ttyd"],
        "action": None,
    }
    # The one check a page can fix with a request names the route's kind and its argument.
    assert checks["hooks claude"]["action"] == {"kind": doctor.ACTION_INSTALL_HOOKS, "profile": "claude"}
    assert checks["hooks claude"]["fix"] == "tldrack hooks --install"
    # An OK line carries no fix and no action, whatever the check was built with.
    assert checks["tmux"]["fix"] == "" and checks["tmux"]["action"] is None
    done = doctor.Check("hooks claude", OK, "installed", "tldrack hooks --install", {"kind": doctor.ACTION_INSTALL_HOOKS})
    assert done.wire()["fix"] == "" and done.wire()["action"] is None
