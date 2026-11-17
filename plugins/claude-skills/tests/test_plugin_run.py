"""POST /run through the plugin: the settings seam, dispatch, the caller check, and the older-host
fail-closed. Every origin, skill and setting here is invented."""

from __future__ import annotations

from run_fakes import FakeProfiles, FakeRun, has_tmux, project_with_skill

from claude_skills import plugin as plugin_module
from claude_skills.plugin import ClaudeSkillsPlugin
from claude_skills.roots import SkillRoots
from claude_skills.run import SkillRunner

OPERATOR = "https://rack.example.test"
OK_HEADERS = {"host": "127.0.0.1:7682", "origin": OPERATOR, "sec-fetch-site": "cross-site"}
SETTINGS = {"run": {"my-skill": {}}}


class FakeContext:
    """A host context; `settings` / `allowed_origins` exist only when given, like an older host."""

    def __init__(self, start_dir, profiles=None, *, settings=None, allowed_origins=None, port=0) -> None:
        self.start_dir = str(start_dir)
        self.profiles = profiles
        self.port = port
        self.logged: list[str] = []
        self.asked: list[str] = []
        if settings is not None:

            def plugin_settings(name: str):
                self.asked.append(name)
                if isinstance(settings, Exception):
                    raise settings
                return settings

            self.plugin_settings = plugin_settings
        if allowed_origins is not None:
            self.allowed_origins = allowed_origins

    def log(self, message: str) -> None:
        self.logged.append(message)


def plugin_with_fakes(tmp_path, *, settings=SETTINGS, allowed_origins=(OPERATOR,)):
    project = project_with_skill(tmp_path)
    context = FakeContext(project, FakeProfiles(), settings=settings, allowed_origins=allowed_origins)
    plugin = ClaudeSkillsPlugin(context)
    run = FakeRun()
    plugin._runner = SkillRunner(
        project,
        context.profiles,
        context.log,
        plugin._runner.runnable,
        claude_dir=tmp_path / "home" / ".claude",
        run=run,
        which=has_tmux,
    )
    return plugin, run, context


# ── the settings seam ────────────────────────────────────────────────────────


def test_run_is_advertised():
    plugin = ClaudeSkillsPlugin(FakeContext("/nonexistent"))
    assert "/plugin/claude-skills/run" in plugin.routes()


def test_a_host_without_settings_runs_nothing_and_says_nothing(tmp_path):
    context = FakeContext(tmp_path)
    plugin = ClaudeSkillsPlugin(context)
    assert plugin._runner.runnable == {}
    assert context.logged == []


def test_the_plugin_asks_for_its_own_settings_once(tmp_path):
    context = FakeContext(tmp_path, settings=SETTINGS)
    plugin = ClaudeSkillsPlugin(context)
    assert context.asked == ["claude-skills"]
    assert list(plugin._runner.runnable) == ["my-skill"]


def test_unknown_settings_keys_are_ignored(tmp_path):
    context = FakeContext(tmp_path, settings={"run": {"my-skill": {}}, "colour": "blue"})
    assert list(ClaudeSkillsPlugin(context)._runner.runnable) == ["my-skill"]
    assert context.logged == []


def test_settings_that_fail_or_are_not_an_object_leave_the_defaults(tmp_path):
    for settings in (RuntimeError("boom"), ["run"], "run"):
        context = FakeContext(tmp_path, settings=settings)
        assert ClaudeSkillsPlugin(context)._runner.runnable == {}
        assert len(context.logged) == 1, (settings, context.logged)


def test_a_bad_run_entry_costs_that_entry_only(tmp_path):
    context = FakeContext(tmp_path, settings={"run": {"my-skill": {}, "bad": {"prompt": "rm -rf /"}}})
    assert list(ClaudeSkillsPlugin(context)._runner.runnable) == ["my-skill"]
    assert len(context.logged) == 1 and "'bad'" in context.logged[0]


def test_state_lists_what_may_run(tmp_path):
    plugin, _, _ = plugin_with_fakes(tmp_path)
    plugin._roots = SkillRoots(home=tmp_path / "home", project=tmp_path / "repo")
    plugin._builtins = type("NoBuiltins", (), {"get": lambda self: {"status": "unavailable", "skills": []}})()
    status, body = plugin.handle_get("/state", {})
    assert status == 200
    assert body["runnable"] == [{"key": "my-skill", "skill": "my-skill", "session": "my-skill"}]
    assert [row["name"] for row in body["tiers"]["project"]] == ["my-skill"]


# ── dispatch and the caller check ────────────────────────────────────────────


def test_an_allowed_caller_starts_the_skill(tmp_path):
    plugin, run, _ = plugin_with_fakes(tmp_path)
    status, body = plugin.handle_request("POST", "/run", {}, {"skill": "my-skill"}, OK_HEADERS)
    assert (status, body["session"], body["created"]) == (200, "my-skill", True)
    assert run.verbs() == ["has-session", "new-session"]


def test_with_no_settings_an_allowed_caller_is_refused_every_key(tmp_path):
    plugin, run, _ = plugin_with_fakes(tmp_path, settings=None)
    status, body = plugin.handle_request("POST", "/run", {}, {"skill": "my-skill"}, OK_HEADERS)
    assert (status, body["plugin"], body["code"]) == (400, "claude-skills", "unknown_skill")
    assert run.calls == []


def test_a_refused_caller_runs_nothing_and_is_logged(tmp_path):
    plugin, run, context = plugin_with_fakes(tmp_path)
    status, body = plugin.handle_request(
        "POST", "/run", {}, {"skill": "my-skill"}, {**OK_HEADERS, "x-forwarded-for": "100.64.0.9"}
    )
    assert (status, body["code"]) == (403, "host_refused")
    assert run.calls == []
    assert any("refused run" in line for line in context.logged)


def test_an_origin_refusal_lists_the_allowed_origins(tmp_path):
    plugin, run, _ = plugin_with_fakes(tmp_path)
    status, body = plugin.handle_request(
        "POST", "/run", {}, {"skill": "my-skill"}, {"host": "127.0.0.1:7682", "origin": "https://evil.example.test"}
    )
    assert (status, body["code"]) == (403, "origin_refused")
    assert OPERATOR in body["allowed_origins"]
    assert run.calls == []


def test_without_operator_origins_a_hosted_page_is_refused(tmp_path):
    plugin, run, _ = plugin_with_fakes(tmp_path, allowed_origins=None)
    status, body = plugin.handle_request("POST", "/run", {}, {"skill": "my-skill"}, OK_HEADERS)
    assert (status, body["code"]) == (403, "origin_refused")
    assert OPERATOR not in body["allowed_origins"]
    assert run.calls == []


def test_a_bad_operator_origin_is_logged_and_the_good_one_kept(tmp_path):
    plugin, _, context = plugin_with_fakes(tmp_path, allowed_origins=["*", OPERATOR])
    assert plugin.handle_request("POST", "/run", {}, {"skill": "my-skill"}, OK_HEADERS)[0] == 200
    assert len([line for line in context.logged if "allowed origin" in line]) == 1


def test_an_older_host_without_headers_fails_closed(tmp_path):
    plugin, run, _ = plugin_with_fakes(tmp_path)
    status, _ = plugin.handle_post("/run", {}, {"skill": "my-skill"})
    assert status == 426
    assert run.calls == []


def test_state_and_open_still_route_through_handle_request(tmp_path):
    plugin, _, _ = plugin_with_fakes(tmp_path)
    assert plugin.handle_request("GET", "/nope", {}, None, OK_HEADERS)[0] == 404
    assert plugin.handle_request("POST", "/nope", {}, None, OK_HEADERS)[0] == 404
    assert plugin.handle_request("DELETE", "/run", {}, None, OK_HEADERS)[0] == 405


def test_every_run_body_names_the_plugin_and_every_refusal_a_code(tmp_path):
    plugin, _, _ = plugin_with_fakes(tmp_path)
    cases = [
        ({"skill": "my-skill"}, OK_HEADERS),
        ({"skill": "nope"}, OK_HEADERS),
        ({"skill": "my-skill"}, {**OK_HEADERS, "origin": "https://evil.example.test"}),
    ]
    for body, headers in cases:
        status, payload = plugin.handle_request("POST", "/run", {}, body, headers)
        assert payload["plugin"] == "claude-skills"
        assert status == 200 or payload["code"], payload


def test_a_host_without_a_start_dir_scans_the_working_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    context = FakeContext("")
    assert ClaudeSkillsPlugin(context)._roots.project == tmp_path
    del context.start_dir
    assert ClaudeSkillsPlugin(context)._roots.project == tmp_path


def test_the_entry_module_and_the_plugin_agree_on_the_name():
    import bridge_plugin

    assert bridge_plugin.PLUGIN_NAME == plugin_module.PLUGIN_NAME == ClaudeSkillsPlugin.name == "claude-skills"
