"""Parsing `git worktree list --porcelain`."""

from worktree.listing import parse_worktree_list

SAMPLE = """worktree /Users/x/code/demo-app
HEAD abc123
branch refs/heads/main

worktree /Users/x/worktrees/demo-app/fix-nav
HEAD def456
branch refs/heads/fix/nav

worktree /Users/x/worktrees/demo-app/spike
HEAD 999aaa
detached
"""


class TestParseWorktreeList:
    def test_reads_every_stanza(self):
        assert len(parse_worktree_list(SAMPLE)) == 3

    def test_strips_the_refs_heads_prefix(self):
        rows = parse_worktree_list(SAMPLE)
        assert rows[1]["branch"] == "fix/nav"
        assert rows[1]["path"] == "/Users/x/worktrees/demo-app/fix-nav"

    def test_marks_detached_heads(self):
        rows = parse_worktree_list(SAMPLE)
        assert rows[2]["detached"] is True and rows[2]["branch"] is None

    def test_ignores_unknown_keys(self):
        # git has added lines to this format before (locked, prunable) and will
        # again — an unknown key must not drop the stanza.
        rows = parse_worktree_list("worktree /a\nHEAD abc\nbranch refs/heads/x\nlocked\nprunable gitdir\n")
        assert len(rows) == 1 and rows[0]["branch"] == "x"

    def test_is_total(self):
        for value in (None, 42, ""):
            assert parse_worktree_list(value) == []

    def test_ignores_stray_lines_before_any_stanza(self):
        assert parse_worktree_list("garbage\nHEAD abc\n") == []
