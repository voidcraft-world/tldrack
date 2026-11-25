"""Create-job progress: the claim, the step ladder, the TTL, and the merge.

`/create` answers 202 and finishes in a worker, so everything the user sees
during those seconds comes from `CreateStore` and `_merge_creates`. Both are
tested without spawning anything — the store takes an injected clock, and the
merge takes a row list.
"""

import os
import tempfile

import pytest

from worktree.store import (
    CREATE_ERROR_TTL_SECONDS,
    CREATE_STEPS,
    CreateStore,
)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestClaim:
    def test_first_claim_wins_and_starts_at_the_first_step(self):
        store = CreateStore(FakeClock())
        assert store.claim("feature") is True
        row = store.get("feature")
        assert row["status"] == "creating"
        assert row["step"] == CREATE_STEPS[0]

    def test_second_claim_loses_while_the_first_is_running(self):
        store = CreateStore(FakeClock())
        store.claim("feature")
        assert store.claim("feature") is False

    def test_a_finished_branch_can_be_claimed_again(self):
        store = CreateStore(FakeClock())
        store.claim("feature")
        store.finish("feature")
        assert store.get("feature") is None
        assert store.claim("feature") is True

    def test_a_FAILED_branch_can_be_retried_without_waiting_out_the_ttl(self):
        # This is what makes "press it again" work. Blocking a retry behind the
        # 5-minute expiry would be indistinguishable from the button being dead.
        store = CreateStore(FakeClock())
        store.claim("feature")
        store.fail("feature", "git worktree add failed")
        assert store.claim("feature") is True
        assert store.get("feature")["status"] == "creating"

    def test_two_branches_do_not_block_each_other(self):
        store = CreateStore(FakeClock())
        assert store.claim("one") is True
        assert store.claim("two") is True


class TestStepLadder:
    def test_the_worker_walks_the_declared_steps_in_order(self):
        # Pins the tuple the front end re-declares. A reordering here without a
        # matching one in `rack-view.ts::create_row_view` would draw the meter
        # backwards, and nothing else would catch it.
        assert CREATE_STEPS == ("worktree", "bootstrap", "session", "prompt")

    def test_advance_moves_the_step(self):
        store = CreateStore(FakeClock())
        store.claim("feature")
        store.advance("feature", "bootstrap")
        assert store.get("feature")["step"] == "bootstrap"

    def test_advance_is_a_no_op_for_a_branch_that_is_not_creating(self):
        # A worker that outlived a `/remove` must not resurrect the row.
        store = CreateStore(FakeClock())
        store.advance("never-claimed", "session")
        assert store.get("never-claimed") is None

    def test_advance_does_not_revive_a_failed_branch(self):
        store = CreateStore(FakeClock())
        store.claim("feature")
        store.fail("feature", "boom")
        store.advance("feature", "session")
        assert store.get("feature")["status"] == "error"

    def test_a_failure_keeps_the_step_it_died_on(self):
        store = CreateStore(FakeClock())
        store.claim("feature")
        store.advance("feature", "bootstrap")
        store.fail("feature", "bootstrap blew up")
        row = store.get("feature")
        assert row["step"] == "bootstrap"
        assert row["error"] == "bootstrap blew up"


class TestErrorTtl:
    def test_an_error_survives_just_under_the_ttl(self):
        clock = FakeClock()
        store = CreateStore(clock)
        store.claim("feature")
        store.fail("feature", "boom")
        clock.advance(CREATE_ERROR_TTL_SECONDS - 1)
        assert store.get("feature") is not None

    def test_an_error_expires_at_the_ttl(self):
        clock = FakeClock()
        store = CreateStore(clock)
        store.claim("feature")
        store.fail("feature", "boom")
        clock.advance(CREATE_ERROR_TTL_SECONDS)
        assert store.get("feature") is None

    def test_a_CREATING_row_never_expires(self):
        # Bootstrap's own budget is 300s, so a long create must not have its
        # own progress row swept out from under it.
        clock = FakeClock()
        store = CreateStore(clock)
        store.claim("feature")
        clock.advance(CREATE_ERROR_TTL_SECONDS * 10)
        assert store.get("feature")["status"] == "creating"

    def test_snapshot_expires_stale_errors_on_the_way_out(self):
        clock = FakeClock()
        store = CreateStore(clock)
        store.claim("old")
        store.fail("old", "boom")
        store.claim("live")
        clock.advance(CREATE_ERROR_TTL_SECONDS)
        assert set(store.snapshot()) == {"live"}

    def test_a_long_error_message_is_truncated(self):
        store = CreateStore(FakeClock())
        store.claim("feature")
        store.fail("feature", "x" * 5000)
        assert len(store.get("feature")["error"]) == 300


class FakeContext:
    start_dir = "/repo"

    def __init__(self) -> None:
        self.lines: list[str] = []

    def log(self, line: str) -> None:
        self.lines.append(line)


@pytest.fixture
def plugin(monkeypatch):
    """A plugin that CANNOT spawn.

    `run_bounded` is fenced rather than each caller mocked: this suite's whole
    value is that it never touches the machine's real git or tmux, and a fence
    keeps that true for tests written later. Anything that needs a specific
    probe answer overrides the narrower function on top.
    """
    from worktree.plugin import WorktreePlugin

    def no_spawn(command, timeout, **_kwargs):  # noqa: ARG001 — env=/cwd= ride through
        raise AssertionError(f"test spawned a subprocess: {command}")

    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setenv("TLDRACK_WORKTREE_PATH", os.path.join(tmp, "prs.json"))
        monkeypatch.setenv("TLDRACK_PR_INDEX_PATH", os.path.join(tmp, "pr-index.json"))
        monkeypatch.setenv("WORKTREE_ROOT", os.path.join(tmp, "worktrees"))
        monkeypatch.setattr("worktree.create.run_bounded", no_spawn)
        monkeypatch.setattr("worktree.pr.run_bounded", no_spawn, raising=False)
        # `listing.list_worktrees` forks with `subprocess.run` directly, not
        # through `run_bounded`, so the fence above does not reach it — and
        # `/state` now lists every worktree for `worktree_paths` before it
        # calls the `_managed()` these tests patch. Empty, never the machine's.
        monkeypatch.setattr("worktree.plugin.list_worktrees", lambda _root: [])
        monkeypatch.setattr("worktree.create.list_worktrees", lambda _root: [])
        # `/state`'s ahead/behind is one `for-each-ref` and its `dirty` a
        # background sweep (dirty.py) — both answer nothing here.
        monkeypatch.setattr("worktree.branches.sweep_refs", lambda _root, _base, **_kw: ([], {"ahead_behind": True}, None))
        monkeypatch.setattr("worktree.branches.dirty_sweep", lambda paths, _budget, **_kw: {})
        yield WorktreePlugin(FakeContext())


def real_row(branch: str) -> dict:
    return {
        "branch": branch,
        "path": f"/wt/{branch}",
        "session": f"wt-{branch}",
        "dirty": False,
        "ahead": 0,
        "behind": 0,
        "pr_status": None,
        "pr_url": None,
        "pr_error": None,
    }


class TestMergeCreates:
    def test_no_jobs_returns_THE_SAME_LIST(self, plugin):
        # The overwhelmingly common case. Identity, not equality: `/state` runs
        # on every poll from up to four surfaces, and rebuilding the list to
        # change nothing is pure garbage.
        rows = [real_row("feature")]
        assert plugin._merge_creates(rows) is rows

    def test_a_job_git_already_knows_about_is_MERGED_not_duplicated(self, plugin):
        # From `bootstrap` onward the worktree is real and `git worktree list`
        # reports it. Appending would show the branch twice, once mid-create.
        plugin._creates.claim("feature")
        plugin._creates.advance("feature", "bootstrap")
        rows = plugin._merge_creates([real_row("feature")])
        assert len(rows) == 1
        assert rows[0]["create_status"] == "creating"
        assert rows[0]["create_step"] == "bootstrap"
        assert rows[0]["path"] == "/wt/feature"  # git's answer, not the derived one

    def test_a_job_git_has_never_heard_of_is_SYNTHESIZED(self, plugin):
        plugin._creates.claim("feature")
        rows = plugin._merge_creates([])
        assert len(rows) == 1
        assert rows[0]["branch"] == "feature"
        assert rows[0]["create_status"] == "creating"

    def test_a_synthesized_row_carries_the_FULL_shape(self, plugin):
        # A half-row would make every consumer of `ahead` / `dirty` special-case
        # this one case forever.
        plugin._creates.claim("feature")
        row = plugin._merge_creates([])[0]
        for key in ("branch", "path", "session", "dirty", "ahead", "behind", "pr_status", "pr_url", "pr_error"):
            assert key in row, key

    def test_untouched_rows_gain_no_create_fields(self, plugin):
        # An absent field is what tells the client "no create here" — writing
        # nulls onto every row would make the creating treatment fire on all.
        plugin._creates.claim("feature")
        rows = plugin._merge_creates([real_row("other")])
        other = next(r for r in rows if r["branch"] == "other")
        assert "create_status" not in other

    def test_a_failed_create_still_reports_its_error(self, plugin):
        plugin._creates.claim("feature")
        plugin._creates.fail("feature", "git worktree add failed")
        row = plugin._merge_creates([])[0]
        assert row["create_status"] == "error"
        assert row["create_error"] == "git worktree add failed"

    def test_a_finished_create_leaves_an_ordinary_row(self, plugin):
        plugin._creates.claim("feature")
        plugin._creates.finish("feature")
        rows = plugin._merge_creates([real_row("feature")])
        assert rows == [real_row("feature")]


class TestSecondClickWhileBuilding:
    """A double-click on `+ WORKTREE` must not read as an error.

    The ordering trap: `git worktree add` is step one, so from `bootstrap`
    onward the path exists on disk and `decide_create`'s conflict table would
    refuse the second click with a 409 about a directory the first click is
    still creating.
    """

    def test_an_in_flight_branch_answers_202(self, plugin, monkeypatch):
        # The step-1 window, before the path exists — reachable via the claim
        # as well. The `_on_disk` case below is the one that needs the ordering.
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        plugin._creates.claim("feature")

        status, body = plugin._handle_create({}, {"branch": "feature"})

        assert status == 202
        assert body["status"] == "creating"
        assert body["created"] is False

    def test_it_answers_202_even_when_the_path_is_on_disk(self, plugin, monkeypatch):
        # The exact 409 this ordering exists to avoid.
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
        from worktree.names import worktree_path_for

        worktree_path_for("feature", plugin._root).mkdir(parents=True, exist_ok=True)
        plugin._creates.claim("feature")

        status, _body = plugin._handle_create({}, {"branch": "feature"})
        assert status == 202

    def test_a_bad_branch_name_is_still_400_while_another_build_runs(self, plugin):
        # The in-flight check must not swallow validation.
        plugin._creates.claim("feature")
        status, _body = plugin._handle_create({}, {"branch": "bad name!"})
        assert status == 400

    def test_an_unrelated_branch_is_not_short_circuited(self, plugin, monkeypatch):
        monkeypatch.setattr("worktree.create.tool_found", lambda _n: False)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: False)
        plugin._creates.claim("feature")
        # Tools missing → 503 from the refusal table, proving we reached it.
        status, _body = plugin._handle_create({}, {"branch": "other"})
        assert status == 503


class TestStateCacheHoldsGitRowsOnly:
    """The TTL cache must not sit in front of the step ladder.

    `_state` caches the GIT rows — one `git worktree list` and one
    `for-each-ref`, for up to four polling surfaces — and merges create progress
    AFTER the cache is read. Caching the merged list instead would put a 4 s cache in
    front of a four-step ladder, which is most of the latency this route exists
    to remove.
    """

    def test_a_step_advances_through_a_WARM_cache(self, plugin, monkeypatch):
        monkeypatch.setattr(plugin, "_managed", lambda *_a: [])
        plugin.handle_get("/state", {})  # warm it

        plugin._creates.claim("feature")
        _s, body = plugin.handle_get("/state", {})  # no _invalidate()
        assert body["worktrees"][0]["create_step"] == "worktree"

        plugin._creates.advance("feature", "session")
        _s, body = plugin.handle_get("/state", {})
        assert body["worktrees"][0]["create_step"] == "session"

    def test_the_cached_rows_are_never_mutated(self, plugin, monkeypatch):
        # `_merge_creates` may be handed the cached list, which is read again on
        # every poll for the next four seconds. A mutation would poison it.
        row = {
            "branch": "feature",
            "path": "/wt/feature",
            "session": "wt-feature",
            "dirty": False,
            "ahead": 0,
            "behind": 0,
            "pr_status": None,
            "pr_url": None,
            "pr_error": None,
        }
        monkeypatch.setattr(plugin, "_managed", lambda *_a: [{"branch": "feature", "path": "/wt/feature"}])
        monkeypatch.setattr("worktree.pr.ahead_behind", lambda *_a: (0, 0))
        monkeypatch.setattr("worktree.pr.is_dirty", lambda *_a: False)
        monkeypatch.setattr("worktree.names.session_name_for", lambda b: f"wt-{b}")

        plugin.handle_get("/state", {})  # warm
        plugin._creates.claim("feature")
        plugin.handle_get("/state", {})  # merge over the cached row
        plugin._creates.finish("feature")
        _s, body = plugin.handle_get("/state", {})  # still the same cached row

        assert "create_status" not in body["worktrees"][0], "the cached row kept a create field from an earlier merge"
        assert body["worktrees"][0]["branch"] == row["branch"]


class FakeProfiles:
    """Stands in for the bridge's ProfileRegistry — the plain-value methods
    the plugin may call, and a record of what it asked for."""

    def __init__(self) -> None:
        self.profiles = {
            "claude-a": {"id": "claude-a", "kind": "claude"},
            "codex-b": {"id": "codex-b", "kind": "codex"},
        }
        self.default_id = "claude-a"
        self.spawned: list = []

    def describe(self, profile_id=None):
        return self.profiles.get(profile_id or self.default_id)

    def cli_path(self, profile_id):
        return f"/usr/bin/{self.profiles[profile_id]['kind']}"

    def child_env(self, profile_id, base=None):  # noqa: ARG002
        return {"CLAUDE_CONFIG_DIR": f"/cfg/{profile_id}"}

    def spawn_argv(self, profile_id, session, cwd, extra_args=(), cli_bin=None):
        self.spawned.append((profile_id, session, cwd, tuple(extra_args), cli_bin))
        return ["tmux", "new-session", "-s", session, "PROFILE", profile_id]


@pytest.fixture
def profiled(plugin, monkeypatch):
    """The fenced plugin, on a host WITH launch profiles."""
    profiles = FakeProfiles()
    plugin._context.profiles = profiles
    monkeypatch.setattr("worktree.create.tool_found", lambda _n: True)
    monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: False)
    monkeypatch.setattr("worktree.create.session_exists", lambda _n: False)
    monkeypatch.setattr("worktree.plugin._resolve_claude_bin", lambda: "/bin/claude")
    started = []

    class InlineThread:
        def __init__(self, target=None, args=(), daemon=None, name=None):  # noqa: ARG002
            self.target, self.args = target, args

        def start(self):
            started.append(self)

    monkeypatch.setattr("worktree.plugin.threading.Thread", InlineThread)
    plugin.started = started
    return plugin, profiles


class TestLaunchProfiles:
    def test_paused_profile_refuses_before_creating_a_worktree(self, profiled, monkeypatch):
        plugin, profiles = profiled
        monkeypatch.setattr(profiles, "launch_refusal", lambda _profile: "Profile is paused", raising=False)
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": "claude-a"})
        assert status == 400 and "paused" in body["error"]
        assert not plugin.started

    def test_parent_identity_is_captured_before_the_worker_starts(self, profiled, monkeypatch):
        plugin, profiles = profiled
        lineage = {"id": "child-id", "parent_id": "parent-id"}
        parents = []
        monkeypatch.setattr(profiles, "prepare_lineage", lambda name: parents.append(name) or lineage, raising=False)
        status, _body = plugin._handle_create({}, {"branch": "feature", "parent_session": "parent"})
        assert status == 202
        assert parents == ["parent"]
        worker = plugin.started[-1]
        assert worker.target.keywords["lineage"] == lineage
        monkeypatch.setattr("worktree.create.add_worktree", lambda *_a, **_k: None)
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, **_k: None)
        seen = []
        monkeypatch.setattr(profiles, "spawn_argv", lambda *a, **kw: seen.append(kw["lineage"]) or ["tmux"])
        monkeypatch.setattr("worktree.create.run_spawn", lambda *_a: (200, {"created": True}))
        worker.target(*worker.args)
        assert seen == [lineage]

    def test_parent_request_on_an_old_host_refuses_before_creating(self, profiled):
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "parent_session": "parent"})
        assert status == 400 and "restart" in body["error"]
        assert not plugin.started

    def test_an_unknown_profile_refuses_rather_than_starting_the_default(self, profiled):
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": "typo"})
        assert status == 400 and "typo" in body["error"]

    def test_a_host_without_profiles_refuses_a_named_one(self, plugin):
        # Ignoring the request would start the WRONG account silently.
        status, _body = plugin._handle_create({}, {"branch": "feature", "profile": "codex-b"})
        assert status == 400

    def test_no_profile_named_is_the_registry_default(self, profiled):
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature"})
        assert status == 202 and body["profile"] == "claude-a"

    def test_auto_starts_the_profile_the_host_picks_and_echoes_it(self, profiled, monkeypatch):
        plugin, profiles = profiled
        monkeypatch.setattr(profiles, "pick_launch", lambda: "codex-b", raising=False)
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": "auto"})
        assert status == 202 and body["profile"] == "codex-b"
        assert plugin.started[-1].target.keywords["profile"] == "codex-b"

    def test_auto_with_nothing_launchable_is_503_and_creates_nothing(self, profiled, monkeypatch):
        plugin, profiles = profiled
        monkeypatch.setattr(profiles, "pick_launch", lambda: None, raising=False)
        status, _body = plugin._handle_create({}, {"branch": "feature", "profile": "auto"})
        assert status == 503
        assert not plugin.started

    def test_auto_on_a_host_that_cannot_pick_refuses(self, profiled):
        # FakeProfiles has no pick_launch: an older bridge.
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": "auto"})
        assert status == 400 and "restart" in body["error"]
        assert not plugin.started

    def test_codex_never_has_a_prompt_typed_and_says_why(self, profiled):
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": "codex-b", "prompt": "fix the nav"})
        assert status == 202
        assert body["prompt_queued"] is False
        assert "codex" in body["prompt_reason"]
        assert plugin.started[-1].args[4] == ""  # the worker never gets the prompt

    def test_the_worker_spawns_and_bootstraps_under_the_profile(self, profiled, monkeypatch):
        plugin, profiles = profiled
        plugin._handle_create({}, {"branch": "feature", "profile": "codex-b"})
        worker = plugin.started[-1]
        seen = {}
        monkeypatch.setattr("worktree.create.add_worktree", lambda *_a, **_k: None)
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, env=None: seen.update(env=env))
        monkeypatch.setattr("worktree.create.run_spawn", lambda argv, session: seen.update(argv=argv) or (200, {"created": True}))
        worker.target(*worker.args)
        assert seen["env"] == {"CLAUDE_CONFIG_DIR": "/cfg/codex-b"}
        assert seen["argv"][-2:] == ["PROFILE", "codex-b"]
        assert profiles.spawned[-1][0] == "codex-b"
        assert plugin._creates.get("feature") is None  # finished cleanly


class TestPermissionMode:
    """`POST /create {permission_mode}` — a worker starting in the CLI's automatic-approval mode.
    The mode → CLI args mapping is the host's (tldrack `profiles.permission_args`)."""

    @staticmethod
    def host_names_modes(profiles, monkeypatch, modes=("auto",), refusals=None):
        """The host's two methods; `refusals` maps a profile id to the host's reason."""
        asked = []
        monkeypatch.setattr(profiles, "permission_modes", lambda: list(modes), raising=False)
        monkeypatch.setattr(
            profiles,
            "permission_refusal",
            lambda profile_id, mode, cli_bin=None: asked.append((profile_id, mode, cli_bin)) or (refusals or {}).get(profile_id),
            raising=False,
        )
        return asked

    @staticmethod
    def run_worker(plugin, profiles, monkeypatch) -> dict:
        seen = {}
        monkeypatch.setattr("worktree.create.add_worktree", lambda *_a, **_k: None)
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, **_k: None)
        monkeypatch.setattr(profiles, "spawn_argv", lambda *a, **kw: seen.update(args=a, kw=kw) or ["tmux"])
        monkeypatch.setattr("worktree.create.run_spawn", lambda *_a: (200, {"created": True}))
        monkeypatch.setattr("worktree.create.send_prompt", lambda *_a: True)
        worker = plugin.started[-1]
        worker.target(*worker.args)
        return seen

    @pytest.mark.parametrize("profile", ["claude-a", "codex-b"])
    def test_a_valid_mode_reaches_the_hosts_spawn_and_is_echoed(self, profiled, monkeypatch, profile):
        plugin, profiles = profiled
        asked = self.host_names_modes(profiles, monkeypatch)
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": profile, "permission_mode": "auto"})
        assert status == 202 and body["permission_mode"] == "auto"
        # The host is asked about the binary this plugin will run (`WORKTREE_CLAUDE_BIN` for Claude).
        assert asked == [(profile, "auto", "/bin/claude" if profile == "claude-a" else "/usr/bin/codex")]
        assert plugin.started[-1].target.keywords["permission_mode"] == "auto"
        assert self.run_worker(plugin, profiles, monkeypatch)["kw"]["permission_mode"] == "auto"

    def test_absent_is_todays_launch_and_never_reaches_the_host(self, profiled, monkeypatch):
        # The FakeProfiles signature has no `permission_mode`: an absent mode must not pass one.
        plugin, profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "permission_mode": ""})
        assert status == 202 and "permission_mode" not in body
        monkeypatch.setattr("worktree.create.add_worktree", lambda *_a, **_k: None)
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, **_k: None)
        monkeypatch.setattr("worktree.create.run_spawn", lambda *_a: (200, {"created": True}))
        worker = plugin.started[-1]
        worker.target(*worker.args)
        assert profiles.spawned[-1][0] == "claude-a"
        assert plugin._creates.get("feature") is None  # finished cleanly

    @pytest.mark.parametrize("mode", ["bypassPermissions", "AUTO", 1, ["auto"]])
    def test_an_unknown_mode_refuses_before_anything_starts(self, profiled, monkeypatch, mode):
        plugin, profiles = profiled
        self.host_names_modes(profiles, monkeypatch)
        status, body = plugin._handle_create({}, {"branch": "feature", "permission_mode": mode})
        assert status == 400 and "permission_mode" in body["error"] and "auto" in body["error"]
        assert not plugin.started

    @pytest.mark.parametrize(
        "reason",
        [
            "a shell profile has no 'auto' mode",
            "profile 'plan' pins its own approvals (--permission-mode in its args)",
            "this codex CLI does not offer --approve-for-me — upgrade it",
        ],
    )
    def test_the_hosts_per_profile_refusal_is_the_answer(self, profiled, monkeypatch, reason):
        # A shell, a pinned profile or an older CLI: the request is refused with the host's reason,
        # never started in the default mode, and never echoed as if it were in `auto`.
        plugin, profiles = profiled
        self.host_names_modes(profiles, monkeypatch, refusals={"codex-b": reason})
        status, body = plugin._handle_create({}, {"branch": "feature", "profile": "codex-b", "permission_mode": "auto"})
        assert status == 400 and reason in body["error"] and "auto" in body["error"]
        assert not plugin.started

    def test_a_live_session_is_not_described_as_starting_in_the_mode(self, profiled, monkeypatch):
        # The idempotent 200 selects a session someone else started; it may not be in `auto`.
        plugin, profiles = profiled
        self.host_names_modes(profiles, monkeypatch)
        monkeypatch.setattr("worktree.create.session_exists", lambda _n: True)
        monkeypatch.setattr("worktree.create.session_home", lambda _n: None)
        status, body = plugin._handle_create({}, {"branch": "feature", "permission_mode": "auto"})
        assert status == 200 and body["created"] is False and "permission_mode" not in body
        assert not plugin.started

    def test_a_host_that_names_no_modes_refuses_rather_than_launching_the_default(self, profiled):
        # FakeProfiles has no `permission_modes`: an older bridge.
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "permission_mode": "auto"})
        assert status == 400 and "restart" in body["error"]
        assert not plugin.started

    def test_a_host_without_profiles_refuses_too(self, plugin):
        status, body = plugin._handle_create({}, {"branch": "feature", "permission_mode": "auto"})
        assert status == 400 and "restart" in body["error"]

    def test_an_existing_tree_whose_session_died_respawns_in_the_mode(self, profiled, monkeypatch):
        # A LEAD follow-up (`existing: True`) re-enters its tree; it must come back in `auto` too.
        plugin, profiles = profiled
        self.host_names_modes(profiles, monkeypatch)
        held = plugin._root / "feature"
        held.mkdir(parents=True)
        monkeypatch.setattr("worktree.create.branch_exists", lambda *_a: True)
        monkeypatch.setattr("worktree.create.checkout_path_for", lambda *_a: held)
        monkeypatch.setattr("worktree.create.tree_present", lambda _p: True)
        status, body = plugin._handle_create({}, {"branch": "feature", "existing": True, "permission_mode": "auto"})
        assert status == 202 and body["respawn"] is True and body["permission_mode"] == "auto"
        monkeypatch.setattr("worktree.create.add_worktree", lambda *_a, **_k: pytest.fail("the tree is built"))
        seen = self.run_worker(plugin, profiles, monkeypatch)
        assert seen["kw"]["permission_mode"] == "auto" and seen["args"][2] == str(held)


SHA = "0123456789abcdef0123456789abcdef01234567"


class TestBaseRef:
    """`POST /create {base}` — cut the new branch from a chosen ref (`launch.py --base`)."""

    def resolves(self, monkeypatch, answer=(0, SHA + "\n", "")):
        calls = []
        monkeypatch.setattr("worktree.create.run_bounded", lambda argv, _t, **_k: (calls.append(argv), answer)[1])
        return calls

    def test_the_worker_cuts_from_the_RESOLVED_sha(self, profiled, monkeypatch):
        plugin, _profiles = profiled
        calls = self.resolves(monkeypatch)
        status, body = plugin._handle_create({}, {"branch": "feature", "base": "origin/main"})
        assert status == 202 and body["base"] == "origin/main" and body["base_sha"] == SHA
        assert calls[0][-2:] == ["--end-of-options", "origin/main^{commit}"]
        seen = {}
        monkeypatch.setattr("worktree.create.add_worktree", lambda _root, _path, _branch, base, **_k: seen.update(base=base))
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, env=None: None)
        monkeypatch.setattr("worktree.create.run_spawn", lambda argv, session: (200, {}))
        worker = plugin.started[-1]
        worker.target(*worker.args)
        assert seen["base"] == SHA

    def test_no_base_keeps_the_configured_branch(self, profiled, monkeypatch):
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature"})
        assert status == 202 and "base" not in body
        seen = {}
        monkeypatch.setattr("worktree.create.add_worktree", lambda _root, _path, _branch, base, **_k: seen.update(base=base))
        monkeypatch.setattr("worktree.create.run_bootstrap", lambda *_a, env=None: None)
        monkeypatch.setattr("worktree.create.run_spawn", lambda argv, session: (200, {}))
        worker = plugin.started[-1]
        worker.target(*worker.args)
        assert seen["base"] == plugin._base

    def test_an_unknown_ref_refuses_before_anything_starts(self, profiled, monkeypatch):
        plugin, _profiles = profiled
        self.resolves(monkeypatch, answer=(1, "", ""))
        status, body = plugin._handle_create({}, {"branch": "feature", "base": "nope"})
        assert status == 400 and "unknown base ref 'nope'" in body["error"]
        assert plugin.started == []

    def test_a_flag_shaped_ref_never_reaches_git(self, profiled, monkeypatch):
        plugin, _profiles = profiled
        calls = self.resolves(monkeypatch)
        for ref in ("--upload-pack=x", "main extra", "x" * 201):
            status, _body = plugin._handle_create({}, {"branch": "feature", "base": ref})
            assert status == 400
        assert calls == []

    def test_base_with_existing_is_refused(self, profiled):
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "base": "main", "existing": True})
        assert status == 400 and "NEW branch" in body["error"]


class TestLaunchModel:
    """`POST /create {model}` — a LEAD worker launched on the run's worker model. What a launch model is,
    and whether a profile can take one, is the host's (tldrack `profiles.model_refusal`)."""

    @staticmethod
    def host_takes_models(profiles, monkeypatch, refusal=None):
        asked = []
        monkeypatch.setattr(
            profiles, "model_refusal", lambda profile_id, model: asked.append((profile_id, model)) or refusal, raising=False
        )
        return asked

    def test_a_model_reaches_the_hosts_spawn_and_is_echoed(self, profiled, monkeypatch):
        plugin, profiles = profiled
        asked = self.host_takes_models(profiles, monkeypatch)
        status, body = plugin._handle_create({}, {"branch": "feature", "model": "opus"})
        assert status == 202 and body["model"] == "opus"
        assert asked == [("claude-a", "opus")]
        assert TestPermissionMode.run_worker(plugin, profiles, monkeypatch)["kw"]["model"] == "opus"
        assert plugin.handle_get("/state", {})[1]["capabilities"]["launch_models"] is True

    def test_absent_names_no_model(self, profiled, monkeypatch):
        plugin, profiles = profiled
        asked = self.host_takes_models(profiles, monkeypatch)
        status, body = plugin._handle_create({}, {"branch": "feature"})
        assert status == 202 and "model" not in body and not asked
        assert "model" not in TestPermissionMode.run_worker(plugin, profiles, monkeypatch)["kw"]

    def test_a_refusal_never_launches_on_the_profiles_own_model(self, profiled, monkeypatch):
        plugin, profiles = profiled
        self.host_takes_models(profiles, monkeypatch, refusal="profile 'claude-a' pins its own model")
        status, body = plugin._handle_create({}, {"branch": "feature", "model": "opus"})
        assert status == 400 and "pins its own model" in body["error"] and not plugin.started

    def test_an_older_host_refuses_and_says_so_in_its_capabilities(self, profiled):
        # FakeProfiles has no `model_refusal`: a bridge from before launch models.
        plugin, _profiles = profiled
        status, body = plugin._handle_create({}, {"branch": "feature", "model": "opus"})
        assert status == 400 and "restart" in body["error"] and not plugin.started
        assert plugin.handle_get("/state", {})[1]["capabilities"]["launch_models"] is False
