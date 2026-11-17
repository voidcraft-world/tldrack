"""The branch → path → session derivation. All pure."""

from pathlib import Path

from worktree.names import (
    is_managed_path,
    session_name_for,
    slugify,
    validate_branch,
    validate_existing_branch,
    worktree_path_for,
)


class TestValidateBranch:
    def test_accepts_ordinary_names(self):
        for name in ("fix-tooltip", "feat/nav-sort", "v2.1-spike", "a"):
            assert validate_branch(name) is None, name

    def test_rejects_empty_and_non_strings(self):
        for value in ("", "   ", None, 42, []):
            assert validate_branch(value) is not None

    def test_rejects_a_leading_dash(self):
        # Not merely a git rule: this string becomes a shell argument and a
        # directory name downstream.
        assert validate_branch("-rf") is not None

    def test_rejects_git_specific_shapes(self):
        for name in ("a..b", "a//b", "trailing/", "trailing.", "thing.lock"):
            assert validate_branch(name) is not None, name

    def test_rejects_path_traversal(self):
        assert validate_branch("../../etc/passwd") is not None

    def test_rejects_overlong(self):
        assert validate_branch("a" * 81) is not None

    def test_rejects_a_name_that_slugs_to_nothing(self):
        # Passes BRANCH_RE only via a leading alnum; guard the session name too.
        assert validate_branch("...") is not None

    def test_still_refuses_a_plus_because_minting_is_narrower(self):
        # The create path DERIVES a directory and a session from this string,
        # so its alphabet stays narrow on purpose. The delete path does not —
        # see TestValidateExistingBranch.
        assert validate_branch("fix-+-retry") is not None


class TestValidateExistingBranch:
    """The rule for a ref git ALREADY has: git's own, plus a leading-dash ban.

    Every "accepts" case below was probed against `git check-ref-format
    --branch` on a real git before it was written down.
    """

    def test_accepts_what_git_accepts(self):
        for name in ("fix-tooltip", "feat/nav-sort", "v2.1-spike", "a", "fix-+-retry", "a%b", "a{b", "a!b", "a#b"):
            assert validate_existing_branch(name) is None, name

    def test_accepts_the_branch_the_harbor_stranded(self):
        # The regression this function exists for: git lists it, `git branch
        # -d` takes it, and the harbor refused it over a single `+`.
        assert validate_existing_branch("fix-+-retry") is None

    def test_rejects_empty_and_non_strings(self):
        for value in ("", "   ", None, 42, []):
            assert validate_existing_branch(value) is not None

    def test_rejects_a_leading_dash(self):
        # OURS, not git's: `git branch -d -D` is an option, not a branch.
        for name in ("-rf", "-D", "--force"):
            assert validate_existing_branch(name) is not None, name

    def test_rejects_every_character_git_rejects(self):
        for name in ("a b", "a~b", "a^b", "a:b", "a?b", "a*b", "a[b", "a\\b", "a\nb", "a\tb", "a\x00b", "a\x7fb"):
            assert validate_existing_branch(name) is not None, repr(name)

    def test_rejects_git_specific_shapes(self):
        for name in (
            "a..b",
            "a//b",
            "/leading",
            "trailing/",
            "trailing.",
            "thing.lock",
            "feat/thing.lock",
            ".hidden",
            "feat/.hidden",
            "a@{b",
            "@",
        ):
            assert validate_existing_branch(name) is not None, name

    def test_rejects_path_traversal(self):
        assert validate_existing_branch("../../etc/passwd") is not None

    def test_rejects_overlong(self):
        assert validate_existing_branch("a" * 256) is not None
        assert validate_existing_branch("a" * 255) is None

    def test_permits_shell_metacharacters(self):
        # NOT an oversight. `create.run_bounded` hands `subprocess.run` a LIST
        # and never a shell string, so these are one argv element each. Git
        # accepts them, so refusing them would strand a branch for no gain.
        for name in ("a;b", "a|b", "a$b", "a&b", "a>b", "a'b", 'a"b', "a(b"):
            assert validate_existing_branch(name) is None, name


class TestSlugAndSessionName:
    def test_session_name_is_tmux_safe(self):
        import re

        # The bridge's own SESSION_NAME_RE.
        pattern = re.compile(r"^[A-Za-z0-9_-]+$")
        for branch in ("feat/nav-sort", "v2.1", "UPPER/Case", "a_b"):
            assert pattern.fullmatch(session_name_for(branch)), branch

    def test_session_name_is_deterministic(self):
        # The idempotency guard: a second CREATE must derive the same name so it
        # can short-circuit instead of forking a second agent.
        assert session_name_for("feat/x") == session_name_for("feat/x")

    def test_prefix_is_not_eaten_by_the_racks_worktree_suffix_rule(self):
        # multi-terminal-layout.ts strips a TRAILING -wt/-worktree marker when it
        # derives a cohort label. A prefix survives that; a suffix would not.
        assert session_name_for("thing").startswith("wt-")

    def test_slug_collapses_separators(self):
        assert slugify("Feat/Nav Sort_v2") == "feat-nav-sort-v2"

    def test_slug_is_total(self):
        assert slugify(None) == ""
        assert slugify("!!!") == ""


class TestWorktreePath:
    def test_path_is_flat_not_nested(self):
        # A branch with slashes must not create nested directories, or
        # `git worktree remove` leaves empty parents behind forever.
        path = worktree_path_for("fix/nav/tooltip", Path("/tmp/wt"))
        assert path == Path("/tmp/wt/fix-nav-tooltip")


class TestIsManagedPath:
    ROOT = Path("/tmp/wt-root")

    def test_accepts_a_child(self):
        assert is_managed_path("/tmp/wt-root/feature", self.ROOT)

    def test_rejects_the_root_itself(self):
        assert not is_managed_path("/tmp/wt-root", self.ROOT)

    def test_rejects_outside_paths(self):
        # The guard that keeps Claude Code's own ~/other-tool/worktrees worktrees
        # out of this plugin's list — and out of POST /remove's reach.
        assert not is_managed_path("/Users/x/other-tool/worktrees/foo/demo-app", self.ROOT)
        assert not is_managed_path("/", self.ROOT)

    def test_rejects_traversal_out_of_the_root(self):
        assert not is_managed_path("/tmp/wt-root/../../etc", self.ROOT)

    def test_is_total(self):
        for value in (None, "", 42):
            assert not is_managed_path(value, self.ROOT)
