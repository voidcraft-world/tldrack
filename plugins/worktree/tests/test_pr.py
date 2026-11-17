"""The PR refusal table and its argv builders."""

from worktree.pr import (
    build_commit_commands,
    build_pr_create_command,
    build_pr_view_command,
    build_push_command,
    commit_message_for,
    decide_pr,
)

CLEAR = dict(known=True, gh_found=True, gh_authed=True, has_commits=True, dirty=False, in_flight=False)


class TestDecidePr:
    def test_clear_when_there_are_commits_and_gh_works(self):
        assert decide_pr(**CLEAR) is None

    def test_unknown_branch_is_404(self):
        status, _ = decide_pr(**{**CLEAR, "known": False})
        assert status == 404

    def test_nothing_to_open_is_400(self):
        status, _ = decide_pr(**{**CLEAR, "has_commits": False, "dirty": False})
        assert status == 400

    def test_dirty_alone_is_enough_to_proceed(self):
        # The sweep-up commit is the point: a branch whose work is uncommitted
        # still has work.
        assert decide_pr(**{**CLEAR, "has_commits": False, "dirty": True}) is None

    def test_missing_gh_is_503(self):
        status, body = decide_pr(**{**CLEAR, "gh_found": False})
        assert status == 503 and "brew install gh" in body["error"]

    def test_unauthenticated_gh_is_503_with_the_fix(self):
        status, body = decide_pr(**{**CLEAR, "gh_authed": False})
        assert status == 503 and "gh auth login" in body["error"]

    def test_in_flight_is_a_success_not_a_conflict(self):
        # The job they asked for is already happening.
        status, body = decide_pr(**{**CLEAR, "in_flight": True})
        assert status == 202 and body["status"] == "opening"

    def test_nothing_to_do_is_reported_before_missing_tools(self):
        status, _ = decide_pr(**{**CLEAR, "has_commits": False, "dirty": False, "gh_found": False})
        assert status == 400


class TestArgv:
    def test_commit_stages_everything_first(self):
        add, commit = build_commit_commands("msg")
        assert add == ["git", "add", "-A"]
        assert commit == ["git", "commit", "-m", "msg"]

    def test_push_sets_upstream(self):
        # gh pr create needs one, and would otherwise PROMPT — a hang on a
        # detached background thread.
        assert build_push_command("feat") == ["git", "push", "-u", "origin", "feat"]

    def test_pr_create_fills_from_commits_when_untitled(self):
        assert build_pr_create_command(None) == ["gh", "pr", "create", "--fill"]

    def test_pr_create_keeps_fill_for_the_body_when_titled(self):
        argv = build_pr_create_command("My title")
        assert "--title" in argv and "My title" in argv and "--fill" in argv

    def test_pr_view_asks_only_for_the_url(self):
        assert build_pr_view_command() == ["gh", "pr", "view", "--json", "url", "-q", ".url"]


class TestCommitMessage:
    def test_defaults_to_the_branch_name(self):
        assert commit_message_for("fix-nav", None) == "fix-nav"

    def test_prefers_an_explicit_title(self):
        assert commit_message_for("fix-nav", "  Fix the nav  ") == "Fix the nav"

    def test_blank_title_falls_back(self):
        assert commit_message_for("fix-nav", "   ") == "fix-nav"
