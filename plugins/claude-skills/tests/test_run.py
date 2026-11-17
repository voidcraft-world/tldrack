"""POST /run: the operator's allow-list, the refusal table, idempotency, replace, and the rule
that nothing is ever typed. Every skill, prompt and session name here is invented."""

from __future__ import annotations

import pytest
from run_fakes import DescribingProfiles, FakeProfiles, FakeRun, has_tmux, no_tmux, project_with_skill, write_skill

from claude_skills.run import (
    MAX_PROMPT_CHARS,
    RunSpec,
    SkillRunner,
    decide_run,
    describe_runnable,
    parse_run_entry,
    runnable_from_settings,
)

SETTINGS = {
    "my-skill": {},
    "release-notes": {"prompt": "/release-notes draft the next entry", "session": "notes-draft"},
    # One skill, a second entry point: a fixed mode in its own session.
    "release-notes-ship": {"skill": "release-notes", "prompt": "/release-notes ship", "session": "notes-ship"},
}


def parsed(settings=SETTINGS) -> dict[str, RunSpec]:
    logged: list[str] = []
    table = runnable_from_settings(settings, logged.append)
    assert logged == []
    return table


RUNNABLE = parsed()


def runner(tmp_path, *, profiles=None, run=None, which=has_tmux, project=None, runnable=None):
    logged: list[str] = []
    fake_run = run if run is not None else FakeRun()
    fake_profiles = profiles if profiles is not None else FakeProfiles()
    built = SkillRunner(
        project if project is not None else project_with_skill(tmp_path),
        fake_profiles,
        logged.append,
        RUNNABLE if runnable is None else runnable,
        claude_dir=tmp_path / "home" / ".claude",
        run=fake_run,
        which=which,
    )
    return built, fake_run, fake_profiles, logged


# ── the allow-list comes from settings, and is empty by default ─────────────


def test_the_default_table_is_empty_and_says_nothing():
    logged: list[str] = []
    assert runnable_from_settings(None, logged.append) == {}
    assert runnable_from_settings({}, logged.append) == {}
    assert logged == []


def test_an_empty_entry_takes_every_default_from_its_key():
    assert RUNNABLE["my-skill"] == RunSpec(skill="my-skill", session="my-skill", prompt="/my-skill")


def test_an_entry_may_name_another_skill_its_session_and_a_fixed_mode():
    assert RUNNABLE["release-notes-ship"] == RunSpec(skill="release-notes", session="notes-ship", prompt="/release-notes ship")
    assert RUNNABLE["release-notes"].prompt == "/release-notes draft the next entry"


def test_unknown_fields_are_ignored():
    assert parse_run_entry("my-skill", {"colour": "blue"}) == RunSpec("my-skill", "my-skill", "/my-skill")


@pytest.mark.parametrize(
    "key, entry",
    [
        ("My-Skill", {}),
        ("", {}),
        ("-lead", {}),
        ("a" * 65, {}),
        ("../up", {}),
        (3, {}),
        ("my-skill", True),
        ("my-skill", "/my-skill"),
        ("my-skill", ["x"]),
        ("my-skill", None),
        ("my-skill", {"skill": "../other"}),
        ("my-skill", {"skill": "Other"}),
        ("my-skill", {"skill": 7}),
        ("my-skill", {"session": "two words"}),
        ("my-skill", {"session": "s" * 65}),
        ("my-skill", {"session": ""}),
        ("my-skill", {"session": "a:b"}),
        ("my-skill", {"session": ["x"]}),
        ("my-skill", {"prompt": "rm -rf /"}),
        ("my-skill", {"prompt": "/other-skill"}),
        ("my-skill", {"prompt": "/my-skillz"}),
        ("my-skill", {"prompt": "--dangerous /my-skill"}),
        ("my-skill", {"prompt": "/my-skill one\ntwo"}),
        ("my-skill", {"prompt": "/my-skill \x1b[2J"}),
        ("my-skill", {"prompt": "/my-skill " + "x" * MAX_PROMPT_CHARS}),
        ("my-skill", {"prompt": 5}),
    ],
)
def test_a_bad_entry_is_dropped_with_one_log_line_and_the_rest_survive(key, entry):
    logged: list[str] = []
    table = runnable_from_settings({key: entry, "release-notes": {}}, logged.append)
    assert list(table) == ["release-notes"]
    assert len(logged) == 1 and "dropped run entry" in logged[0]


def test_two_keys_never_share_a_session():
    logged: list[str] = []
    table = runnable_from_settings({"my-skill": {"session": "shared"}, "release-notes": {"session": "shared"}}, logged.append)
    assert list(table) == ["my-skill"]
    assert len(logged) == 1 and "already belongs to 'my-skill'" in logged[0]


@pytest.mark.parametrize("raw", [["my-skill"], "my-skill", 3, True])
def test_a_run_setting_that_is_not_an_object_is_ignored_once(raw):
    logged: list[str] = []
    assert runnable_from_settings(raw, logged.append) == {}
    assert len(logged) == 1


def test_state_describes_what_may_run_without_the_prompts():
    described = describe_runnable(RUNNABLE)
    assert [row["key"] for row in described] == sorted(SETTINGS)
    assert described[0] == {"key": "my-skill", "skill": "my-skill", "session": "my-skill"}
    assert all("prompt" not in row for row in described)


# ── the refusal table (pure) ─────────────────────────────────────────────────

OK = dict(tmux_ok=True, has_profiles=True, profile_id="a", cli_found=True, skill_file_exists=True)


def test_a_clear_request_is_not_refused():
    assert decide_run("my-skill", RUNNABLE, **OK) is None


@pytest.mark.parametrize("skill", [None, 3, "", "my_skill", "MY-SKILL", "../my-skill", "deploy", ["my-skill"]])
def test_only_an_allow_listed_key_is_runnable(skill):
    status, body = decide_run(skill, RUNNABLE, **OK)
    assert (status, body["code"]) == (400, "unknown_skill")
    assert "my-skill" in body["error"] and "release-notes-ship" in body["error"]


def test_with_no_settings_every_key_is_refused_with_a_hint():
    status, body = decide_run("my-skill", {}, **OK)
    assert (status, body["code"]) == (400, "unknown_skill")
    assert 'plugins["claude-skills"].run' in body["hint"] and "settings.json" in body["hint"]


@pytest.mark.parametrize("field", ["tmux_ok", "has_profiles", "cli_found", "skill_file_exists"])
def test_each_missing_capability_is_a_distinct_503(field):
    status, body = decide_run("my-skill", RUNNABLE, **{**OK, field: False})
    assert status == 503
    others = {
        f: decide_run("my-skill", RUNNABLE, **{**OK, f: False})[1]["error"]
        for f in ("tmux_ok", "has_profiles", "cli_found", "skill_file_exists")
        if f != field
    }
    assert body["error"] not in others.values()


def test_no_claude_profile_is_refused():
    assert decide_run("my-skill", RUNNABLE, **{**OK, "profile_id": None})[0] == 503


# ── refusals run nothing ─────────────────────────────────────────────────────


@pytest.mark.parametrize("case", ["unknown", "empty_table", "no_tmux", "no_profiles", "no_claude", "no_cli", "no_skill_file"])
def test_every_refusal_is_decided_before_anything_runs(tmp_path, case):
    kwargs = {}
    body = {"skill": "my-skill"}
    if case == "unknown":
        body = {"skill": "rm -rf"}
    elif case == "empty_table":
        kwargs["runnable"] = {}
    elif case == "no_tmux":
        kwargs["which"] = no_tmux
    elif case == "no_claude":
        kwargs["profiles"] = FakeProfiles(claude_id=None)
    elif case == "no_cli":
        kwargs["profiles"] = FakeProfiles(cli_found=False)
    elif case == "no_skill_file":
        kwargs["project"] = tmp_path / "empty"
    built, run, profiles, _ = runner(tmp_path, **kwargs)
    if case == "no_profiles":
        built = SkillRunner(
            project_with_skill(tmp_path / "bare-host"),
            None,
            lambda _: None,
            RUNNABLE,
            claude_dir=tmp_path / "home" / ".claude",
            run=run,
            which=has_tmux,
        )
    status, _ = built.start(body)
    assert status in (400, 503)
    assert run.calls == []
    assert profiles.spawned == []


# ── where the skill is found ─────────────────────────────────────────────────


def test_a_skill_in_the_default_accounts_skills_runs_without_a_project_copy(tmp_path):
    write_skill(tmp_path / "home" / ".claude" / "skills", "my-skill")
    built, _, profiles, _ = runner(tmp_path, project=tmp_path / "other-repo")
    status, body = built.start({"skill": "my-skill"})
    assert (status, body["created"]) == (200, True)
    assert profiles.spawned[-1][2] == str(tmp_path / "other-repo")


def test_a_skill_is_looked_for_in_the_account_the_run_starts_in(tmp_path):
    account = tmp_path / "home" / ".claude-b"
    write_skill(account / "skills", "my-skill")
    built, _, _, _ = runner(tmp_path, project=tmp_path / "other-repo", profiles=DescribingProfiles(account))
    assert built.start({"skill": "my-skill"})[0] == 200


def test_a_skill_held_only_by_another_account_is_missing(tmp_path):
    write_skill(tmp_path / "home" / ".claude" / "skills", "my-skill")
    elsewhere = tmp_path / "home" / ".claude-b"
    built, run, _, _ = runner(tmp_path, project=tmp_path / "other-repo", profiles=DescribingProfiles(elsewhere))
    status, body = built.start({"skill": "my-skill"})
    assert (status, body["code"]) == (503, "skill_missing")
    assert run.calls == []


def test_a_second_entry_point_checks_the_skill_it_names_not_its_key(tmp_path):
    built, _, profiles, _ = runner(tmp_path, project=project_with_skill(tmp_path, "release-notes"))
    status, body = built.start({"skill": "release-notes-ship"})
    assert (status, body["skill"], body["session"], body["created"]) == (200, "release-notes-ship", "notes-ship", True)
    assert profiles.spawned[-1][-1] == ("/release-notes ship",)


# ── the spawn ────────────────────────────────────────────────────────────────


def test_a_fresh_start_spawns_the_profile_argv_with_the_fixed_prompt(tmp_path):
    project = project_with_skill(tmp_path)
    built, run, profiles, logged = runner(tmp_path, project=project)
    status, body = built.start({"skill": "my-skill", "prompt": "place every order", "session": "evil"})
    assert (status, body) == (200, {"skill": "my-skill", "session": "my-skill", "created": True, "replaced": False})
    assert profiles.spawned == [("a", "my-skill", str(project), ("/my-skill",))]
    assert run.calls[-1][-1] == "/my-skill"
    # No request field reaches the argv.
    flat = " ".join(" ".join(call) for call in run.calls)
    assert "place every order" not in flat and "evil" not in flat
    assert len(logged) == 1


def test_a_prompt_with_words_is_one_argv_element(tmp_path):
    project = project_with_skill(tmp_path, "release-notes")
    built, run, profiles, _ = runner(tmp_path, project=project)
    status, body = built.start({"skill": "release-notes"})
    assert (status, body["session"], body["created"]) == (200, "notes-draft", True)
    # the whole prompt is ONE argv element: a slash command plus its words, never split, never typed
    assert profiles.spawned == [("a", "notes-draft", str(project), ("/release-notes draft the next entry",))]
    assert run.calls[-1][-1] == "/release-notes draft the next entry"


def test_a_live_session_answers_created_false_and_runs_nothing_else(tmp_path):
    built, run, profiles, _ = runner(tmp_path, run=FakeRun(live={"my-skill"}))
    status, body = built.start({"skill": "my-skill"})
    assert (status, body["created"], body["replaced"]) == (200, False, False)
    assert run.verbs() == ["has-session"]
    assert profiles.spawned == []


def test_replace_kills_then_spawns_in_that_order(tmp_path):
    built, run, profiles, _ = runner(tmp_path, run=FakeRun(live={"my-skill"}))
    status, body = built.start({"skill": "my-skill", "replace": True})
    assert (status, body["created"], body["replaced"]) == (200, True, True)
    assert run.verbs() == ["has-session", "kill-session", "new-session"]
    assert run.calls[1][3] == "=my-skill"


@pytest.mark.parametrize("replace", ["true", 1, "yes", None])
def test_only_a_literal_true_replaces(tmp_path, replace):
    built, run, _, _ = runner(tmp_path, run=FakeRun(live={"my-skill"}))
    assert built.start({"skill": "my-skill", "replace": replace})[1]["created"] is False
    assert "kill-session" not in run.verbs()


def test_replace_with_no_session_is_a_plain_start(tmp_path):
    built, run, _, _ = runner(tmp_path)
    body = built.start({"skill": "my-skill", "replace": True})[1]
    assert (body["created"], body["replaced"]) == (True, False)
    assert run.verbs() == ["has-session", "new-session"]


def test_a_duplicate_session_race_is_an_idempotent_success(tmp_path):
    run = FakeRun(new_session_code=1, new_session_stderr="duplicate session: my-skill")
    built, _, _, logged = runner(tmp_path, run=run)
    assert built.start({"skill": "my-skill"}) == (
        200,
        {"skill": "my-skill", "session": "my-skill", "created": False, "replaced": False},
    )
    assert logged == []


def test_a_tmux_failure_is_a_500(tmp_path):
    built, _, _, _ = runner(tmp_path, run=FakeRun(new_session_code=1, new_session_stderr="no server"))
    status, body = built.start({"skill": "my-skill"})
    assert status == 500 and body["code"] == "spawn_failed" and "no server" in body["error"]


class VanishingProfiles(FakeProfiles):
    """Resolved at refusal time, gone by spawn time (profiles.json edited mid-request)."""

    def spawn_argv(self, profile_id, session, cwd, extra_args=(), cli_bin=None) -> list[str]:
        raise KeyError(profile_id)


def test_a_vanished_profile_is_a_503_not_a_crash(tmp_path):
    built, run, _, _ = runner(tmp_path, profiles=VanishingProfiles())
    assert built.start({"skill": "my-skill"})[0] == 503
    assert "new-session" not in run.verbs()


# ── the invariant ────────────────────────────────────────────────────────────


def test_nothing_in_any_path_ever_sends_keys(tmp_path):
    for live, body in [
        (set(), {"skill": "my-skill"}),
        ({"my-skill"}, {"skill": "my-skill"}),
        ({"my-skill"}, {"skill": "my-skill", "replace": True}),
    ]:
        built, run, _, _ = runner(tmp_path / str(len(live)) / str(len(body)), run=FakeRun(live=live))
        built.start(body)
        assert all(call[1] in ("has-session", "kill-session", "new-session") for call in run.calls), run.calls


def test_the_module_source_never_spells_send_keys():
    from claude_skills import plugin, run

    for module in (run, plugin):
        text = open(module.__file__, encoding="utf-8").read()
        anchor = "def " if module is run else "class "
        assert text.find(anchor) != -1
        assert '"send-keys"' not in text and "'send-keys'" not in text, module.__name__
