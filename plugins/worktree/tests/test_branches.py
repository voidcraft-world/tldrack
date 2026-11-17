"""The branch sweep: its format, its parsers, the delete table, the builders,
and the I/O ladder — with fake runners, never a fork.

The fixture is invented in the exact shape of `for-each-ref` output (git 2.54),
one row per distinct shape, under this machine's home directory so the
`~`-collapse assertions hold anywhere.
"""

import json
from pathlib import Path

import pytest

from worktree import branches as b
from worktree.branches import FIELD_SEP

HOME = str(Path.home())
REPO = f"{HOME}/code/demo-app"
INTENT = f"{HOME}/other-tool/worktrees"
MANAGED = f"{HOME}/worktrees/demo-app"
PRUNABLE_DIR = f"{MANAGED}/stale-wt"


def row(*fields: str) -> str:
    return FIELD_SEP.join(fields)


def full(short: str) -> str:
    """A full object id that starts with `short` — what `%(objectname)` prints
    beside `%(objectname:short)`. Zero-padded: real ids are never needed."""
    return short + "0" * (40 - len(short))


AUTHOR = "Test Author"

# 12 fields: refname:short, objectname:short, objectname, upstream:short,
# upstream:remotename, upstream:remoteref, upstream:track, ahead-behind:main,
# worktreepath, committerdate:unix, authorname, subject.
FIXTURE = "\n".join(
    [
        # own upstream, [ahead 1], 1 commit main lacks, a Claude-Code worktree
        row(
            "add-stopwatch-feed",
            "0a1b2c3d4",
            full("0a1b2c3d4"),
            "origin/add-stopwatch-feed",
            "origin",
            "refs/heads/add-stopwatch-feed",
            "[ahead 1]",
            "1 140",
            f"{INTENT}/spike-one/demo-app",
            "1770000000",
            AUTHOR,
            "feat(stopwatch): keep the lap label apart from the display name",
        ),
        # upstream IS origin/main — its upstream numbers repeat the vs-main reading
        row(
            "add-entity-local-search",
            "a1b2c3d07",
            full("a1b2c3d07"),
            "origin/main",
            "origin",
            "refs/heads/main",
            "[behind 215]",
            "0 215",
            "",
            "1771450000",
            AUTHOR,
            "fix: address review comments — focus order, a race, null guards, cleanup",
        ),
        # tracks gitlab/main: the wrong forge for a PR, and both numbers
        row(
            "fill-docstring-gaps-now",
            "a1b2c3d05",
            full("a1b2c3d05"),
            "gitlab/main",
            "gitlab",
            "refs/heads/main",
            "[ahead 2, behind 213]",
            "2 232",
            "",
            "1770590000",
            AUTHOR,
            "Create the helpers README.md",
        ),
        # gitlab-tracked, own name
        row(
            "add-bookcase-folder-entity",
            "a1b2c3d08",
            full("a1b2c3d08"),
            "gitlab/add-bookcase-folder-entity",
            "gitlab",
            "refs/heads/add-bookcase-folder-entity",
            "",
            "0 227",
            "",
            "1770830000",
            AUTHOR,
            "Fix the folder's editable fields: use a list field type instead of textarea",
        ),
        # no upstream at all
        row(
            "test123",
            "a1b2c3d0a",
            full("a1b2c3d0a"),
            "",
            "",
            "",
            "",
            "0 217",
            "",
            "1771440000",
            AUTHOR,
            "feat: gate the arrow keys on pointer lock and expose focus_and_select_item",
        ),
        # the base, checked out in the main checkout
        row(
            "main",
            "a1b2c3d01",
            full("a1b2c3d01"),
            "origin/main",
            "origin",
            "refs/heads/main",
            "",
            "0 0",
            REPO,
            "1788620000",
            AUTHOR,
            "tidy up before the next release",
        ),
        # synthetic: upstream deleted on the remote
        row(
            "old-feature",
            "0123456ab",
            full("0123456ab"),
            "origin/old-feature",
            "origin",
            "refs/heads/old-feature",
            "[gone]",
            "0 300",
            "",
            "1760000000",
            AUTHOR,
            "old work",
        ),
        # synthetic: checked out in a managed worktree whose directory is gone
        row("stale-wt", "89abcdef0", full("89abcdef0"), "", "", "", "", "0 40", PRUNABLE_DIR, "1785000000", AUTHOR, "stale"),
        # a managed worktree, merged
        row(
            "pr-demo",
            "a1b2c3d02",
            full("a1b2c3d02"),
            "origin/pr-demo",
            "origin",
            "refs/heads/pr-demo",
            "",
            "0 22",
            f"{MANAGED}/pr-demo",
            "1788130000",
            AUTHOR,
            "worktree list: show create progress instead of nothing",
        ),
        # a managed worktree, 8 unlanded commits, no upstream
        row(
            "feat/cache-all-pages",
            "a1b2c3d03",
            full("a1b2c3d03"),
            "",
            "",
            "",
            "",
            "8 2",
            f"{MANAGED}/cache-all-pages",
            "1788560000",
            AUTHOR,
            "cache: clear the revalidation state when a page leaves the cache",
        ),
        "warning: refname 'x' is ambiguous",  # garbage — skipped, never guessed at
        "",
    ]
)

# 11 fields — what a git older than 2.41 would have printed (no ahead-behind).
OLD_GIT_FIXTURE = "\n".join(
    [
        row(
            "main",
            "a1b2c3d01",
            full("a1b2c3d01"),
            "origin/main",
            "origin",
            "refs/heads/main",
            "",
            REPO,
            "1788620000",
            AUTHOR,
            "tidy up before the next release",
        ),
        row(
            "add-stopwatch-feed",
            "0a1b2c3d4",
            full("0a1b2c3d4"),
            "origin/add-stopwatch-feed",
            "origin",
            "refs/heads/add-stopwatch-feed",
            "[ahead 1]",
            f"{INTENT}/spike-one/demo-app",
            "1770000000",
            AUTHOR,
            "feat(stopwatch)",
        ),
    ]
)

MISSING_BASE_STDERR = "fatal: failed to find 'main'\n"
OLD_GIT_STDERR = "fatal: unknown field name: ahead-behind:main\n"

PRUNE_OUTPUT = """Removing worktrees/stale-wt: gitdir file points to non-existent location
Removing worktrees/old: gitdir file points to non-existent location
"""

# The shape `gh pr list --state all --json …` prints, with invented values.
PR_LIST_JSON = json.dumps(
    [
        {
            "headRefName": "feat/end-to-end-validation",
            "isDraft": False,
            "mergedAt": "2026-09-05T14:50:00Z",
            "number": 32,
            "state": "MERGED",
            "updatedAt": "2026-09-05T14:50:00Z",
            "headRefOid": full("a1b2c3d04"),
            "baseRefName": "main",
            "url": "https://github.com/o/r/pull/32",
        },
        {
            "headRefName": "pr-demo",
            "isDraft": False,
            "mergedAt": "2026-08-31T02:00:00Z",
            "number": 31,
            "state": "MERGED",
            "updatedAt": "2026-08-31T02:00:00Z",
            "headRefOid": full("a1b2c3d02"),
            "baseRefName": "main",
            "url": "https://github.com/o/r/pull/31",
        },
        {
            "headRefName": "add-stopwatch-feed",
            "isDraft": True,
            "mergedAt": None,
            "number": 40,
            "state": "OPEN",
            "updatedAt": "2026-09-01T00:00:00Z",
            "url": "https://github.com/o/r/pull/40",
        },
        # the same head branch again, older and closed — must lose to the open one
        {
            "headRefName": "add-stopwatch-feed",
            "isDraft": False,
            "mergedAt": None,
            "number": 12,
            "state": "CLOSED",
            "updatedAt": "2026-03-01T00:00:00Z",
            "url": "https://github.com/o/r/pull/12",
        },
        # two merged PRs for one head: the NEWER updatedAt wins
        {
            "headRefName": "twice",
            "isDraft": False,
            "mergedAt": "2026-01-01T00:00:00Z",
            "number": 2,
            "state": "MERGED",
            "updatedAt": "2026-01-01T00:00:00Z",
            "url": "u2",
        },
        {
            "headRefName": "twice",
            "isDraft": False,
            "mergedAt": "2026-02-01T00:00:00Z",
            "number": 3,
            "state": "MERGED",
            "updatedAt": "2026-02-01T00:00:00Z",
            "url": "u3",
        },
        {"headRefName": "", "number": 99, "state": "OPEN"},  # bad row: no head
        {"headRefName": "weird", "number": "7", "state": "OPEN"},  # bad row: number
        "not a row",
    ]
)


def parsed() -> dict[str, dict]:
    return {r["branch"]: r for r in b.parse_for_each_ref(FIXTURE, "main", ahead_behind=True)}


class TestFormat:
    def test_the_field_list_is_pinned_in_wire_order(self):
        assert b.REF_FIELDS == (
            "refname:short",
            "objectname:short",
            "objectname",
            "upstream:short",
            "upstream:remotename",
            "upstream:remoteref",
            "upstream:track",
            "ahead-behind:{base}",
            "worktreepath",
            "committerdate:unix",
            "authorname",
            "subject",
        )

    def test_the_remote_field_list_is_pinned_in_wire_order(self):
        assert b.REMOTE_REF_FIELDS == (
            "refname:short",
            "symref",
            "objectname:short",
            "objectname",
            "ahead-behind:{base}",
            "committerdate:unix",
            "authorname",
            "subject",
        )

    def test_argv_with_ahead_behind(self):
        argv = b.build_for_each_ref_command("/repo", "main", ahead_behind=True)
        assert argv[:6] == ["git", "-C", "/repo", "for-each-ref", "refs/heads/", "--format"]
        fmt = argv[6]
        assert "%(ahead-behind:main)" in fmt
        assert fmt.count("%1F") == len(b.REF_FIELDS) - 1

    def test_argv_without_ahead_behind_drops_exactly_that_field(self):
        fmt = b.build_for_each_ref_command("/repo", "main", ahead_behind=False)[6]
        assert "ahead-behind" not in fmt
        assert fmt.count("%1F") == len(b.REF_FIELDS) - 2

    def test_the_separator_is_unit_separator(self):
        assert FIELD_SEP == "\x1f"


class TestParseUpstreamTrack:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("", (0, 0, False)),
            ("[ahead 1]", (1, 0, False)),
            ("[behind 215]", (0, 215, False)),
            ("[ahead 2, behind 213]", (2, 213, False)),
            ("[gone]", (0, 0, True)),
        ],
    )
    def test_every_form_git_prints(self, text, expected):
        assert b.parse_upstream_track(text) == expected

    def test_is_total(self):
        for value in (None, 42, "garbage", "[ahead lots]"):
            assert b.parse_upstream_track(value) == (0, 0, False), value


class TestParseAheadBehind:
    def test_two_integers(self):
        assert b.parse_ahead_behind("1 140") == (1, 140)

    def test_anything_else_is_none_never_zero_zero(self):
        # (0, 0) would read as merged. `pr.ahead_behind`'s degrade is the trap.
        for value in ("", "x y", "1", None, "1 2 3"):
            assert b.parse_ahead_behind(value) is None, value


class TestParseForEachRef:
    def test_reads_every_real_row_and_skips_garbage(self):
        rows = b.parse_for_each_ref(FIXTURE, "main", ahead_behind=True)
        assert len(rows) == 10

    def test_field_mapping(self):
        r = parsed()["add-stopwatch-feed"]
        assert r["sha"] == "0a1b2c3d4"
        assert r["oid"] == full("0a1b2c3d4")
        assert r["committed_at"] == 1770000000
        assert r["author"] == AUTHOR
        assert r["subject"].startswith("feat(stopwatch)")
        assert r["ahead"] == 1 and r["behind"] == 140
        assert r["worktree_path"] == f"{INTENT}/spike-one/demo-app"  # ABSOLUTE here

    def test_merged_is_exactly_ahead_equals_zero(self):
        rows = parsed()
        assert rows["main"]["merged"] is True
        assert rows["test123"]["merged"] is True
        assert rows["add-stopwatch-feed"]["merged"] is False
        assert rows["feat/cache-all-pages"]["merged"] is False

    def test_is_base(self):
        rows = parsed()
        assert rows["main"]["is_base"] is True
        assert all(not r["is_base"] for name, r in rows.items() if name != "main")

    def test_upstream_own(self):
        up = parsed()["add-stopwatch-feed"]["upstream"]
        assert up == {
            "ref": "origin/add-stopwatch-feed",
            "remote": "origin",
            "branch": "add-stopwatch-feed",
            "kind": "own",
            "ahead": 1,
            "behind": 0,
            "gone": False,
        }

    def test_upstream_remoteref_has_refs_heads_stripped(self):
        assert parsed()["add-entity-local-search"]["upstream"]["branch"] == "main"

    def test_upstream_kind_base_when_tracking_the_remote_base(self):
        rows = parsed()
        assert rows["add-entity-local-search"]["upstream"]["kind"] == "base"
        assert rows["fill-docstring-gaps-now"]["upstream"]["kind"] == "base"
        assert rows["fill-docstring-gaps-now"]["upstream"]["remote"] == "gitlab"
        assert rows["fill-docstring-gaps-now"]["upstream"]["ahead"] == 2
        assert rows["fill-docstring-gaps-now"]["upstream"]["behind"] == 213

    def test_the_base_branch_itself_is_own_not_base(self):
        # Its `behind` is the "↓N behind origin/main" the main island shows;
        # classifying it as `base` would make the client hide it.
        assert parsed()["main"]["upstream"]["kind"] == "own"

    def test_gitlab_tracked_own_name(self):
        up = parsed()["add-bookcase-folder-entity"]["upstream"]
        assert up["remote"] == "gitlab" and up["kind"] == "own"

    def test_no_upstream_is_none(self):
        assert parsed()["test123"]["upstream"] is None

    def test_gone(self):
        assert parsed()["old-feature"]["upstream"]["gone"] is True

    def test_no_worktree_is_none_not_empty_string(self):
        assert parsed()["test123"]["worktree_path"] is None

    def test_an_oid_that_is_not_a_full_id_reads_as_none_never_as_a_prefix(self):
        # `landed` compares whole ids; a short or garbled one must not pass.
        text = row("x", "abc", "abc", "", "", "", "", "0 1", "", "1", AUTHOR, "s")
        assert b.parse_for_each_ref(text, "main", ahead_behind=True)[0]["oid"] is None

    def test_old_git_blob_parses_with_every_ahead_behind_none(self):
        rows = b.parse_for_each_ref(OLD_GIT_FIXTURE, "main", ahead_behind=False)
        assert len(rows) == 2
        for r in rows:
            assert r["ahead"] is None and r["behind"] is None and r["merged"] is None
        assert rows[1]["upstream"]["ahead"] == 1  # the track field still parses

    def test_old_git_blob_read_with_the_wrong_flag_yields_nothing(self):
        # Field count is the contract; a mismatch is skipped, never shifted.
        assert b.parse_for_each_ref(OLD_GIT_FIXTURE, "main", ahead_behind=True) == []

    def test_is_total(self):
        for value in (None, 42, "", "\n\n"):
            assert b.parse_for_each_ref(value, "main", ahead_behind=True) == []


class TestMergedFlag:
    def test_tri_state(self):
        assert b.merged_flag(0) is True
        assert b.merged_flag(3) is False
        assert b.merged_flag(None) is None


class TestParsePruneOutput:
    def test_names_from_the_verbose_lines(self):
        assert b.parse_prune_output(PRUNE_OUTPUT) == ["stale-wt", "old"]

    def test_nothing_pruned_and_garbage(self):
        assert b.parse_prune_output("") == []
        assert b.parse_prune_output("something else\n") == []
        assert b.parse_prune_output(None) == []


class TestParsePrList:
    def test_keyed_by_head_ref_with_lowercase_state(self):
        index = b.parse_pr_list(PR_LIST_JSON)
        assert index["pr-demo"] == {
            "number": 31,
            "state": "merged",
            "url": "https://github.com/o/r/pull/31",
            "draft": False,
            "merged_at": "2026-08-31T02:00:00Z",
            "updated_at": "2026-08-31T02:00:00Z",
            "head_oid": full("a1b2c3d02"),
            "base_ref": "main",
        }

    def test_a_missing_or_empty_base_ref_is_none(self):
        for value in (None, "", 7):
            index = b.parse_pr_list(json.dumps([{"headRefName": "x", "number": 1, "state": "MERGED", "baseRefName": value}]))
            assert index["x"]["base_ref"] is None, value

    def test_a_missing_or_malformed_head_oid_is_none(self):
        for value in (None, "", "abc", full("ABC"), 7, full("a") + "0"):
            index = b.parse_pr_list(json.dumps([{"headRefName": "x", "number": 1, "state": "MERGED", "headRefOid": value}]))
            assert index["x"]["head_oid"] is None, value

    def test_open_beats_closed_for_the_same_head(self):
        pr = b.parse_pr_list(PR_LIST_JSON)["add-stopwatch-feed"]
        assert pr["number"] == 40 and pr["state"] == "open" and pr["draft"] is True

    def test_newest_wins_within_a_state(self):
        assert b.parse_pr_list(PR_LIST_JSON)["twice"]["number"] == 3

    def test_bad_rows_are_dropped_not_fatal(self):
        index = b.parse_pr_list(PR_LIST_JSON)
        assert "" not in index and "weird" not in index
        assert len(index) == 4

    def test_a_list_that_fills_the_ceiling_reads_as_cut(self):
        # Counted RAW: two PRs on one head fold to one index row, but the
        # ceiling is what gh was allowed to return, so both count.
        rows = json.dumps([{"headRefName": "same", "number": n, "state": "CLOSED"} for n in range(3)])
        assert b.pr_list_truncated(rows, ceiling=3) is True
        assert b.pr_list_truncated(rows, ceiling=4) is False
        assert len(b.parse_pr_list(rows)) == 1

    def test_the_real_ceiling_covers_a_busy_repository(self):
        # A ceiling of 200 once cut the oldest PRs off the index of a repository that had outgrown it.
        assert b.pr_list_truncated(PR_LIST_JSON) is False
        assert b.PR_LIST_CEILING >= 1000

    def test_truncation_refuses_a_body_that_is_not_a_list(self):
        for text in ("{not json", '{"a": 1}', None):
            with pytest.raises(ValueError):
                b.pr_list_truncated(text)

    def test_a_non_list_body_raises_so_a_good_index_is_never_replaced(self):
        for body in ("", "not json", "{}", None):
            with pytest.raises(ValueError):
                b.parse_pr_list(body)


class TestBuilders:
    def test_delete_argv(self):
        assert b.build_branch_delete_command("/repo", "x", False) == ["git", "-C", "/repo", "branch", "-d", "x"]
        assert b.build_branch_delete_command("/repo", "x", True) == ["git", "-C", "/repo", "branch", "-D", "x"]

    def test_prune_argv_is_verbose(self):
        assert b.build_prune_command("/repo") == ["git", "-C", "/repo", "worktree", "prune", "-v"]

    def test_fetch_argv_prunes_remote_refs(self):
        assert b.build_fetch_command("/repo", "origin") == ["git", "-C", "/repo", "fetch", "--prune", "origin"]

    def test_fetch_env_never_prompts(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin")
        env = b.fetch_env()
        assert env["GIT_TERMINAL_PROMPT"] == "0"
        assert env["GIT_SSH_COMMAND"] == "ssh -oBatchMode=yes"
        assert env["PATH"] == "/usr/bin"  # the rest of the environment rides along

    def test_pr_list_argv(self):
        assert b.build_pr_list_command() == [
            "gh",
            "pr",
            "list",
            "--state",
            "all",
            "--limit",
            "5000",
            "--json",
            "number,headRefName,headRefOid,baseRefName,state,url,isDraft,mergedAt,updatedAt",
        ]

    def test_is_ancestor_argv(self):
        assert b.build_is_ancestor_command("/repo", "a" * 40, "b" * 40) == [
            "git",
            "-C",
            "/repo",
            "merge-base",
            "--is-ancestor",
            "a" * 40,
            "b" * 40,
        ]


CLEAR = dict(
    known=True, is_base=False, creating=False, checked_out_at=None, managed=False, merged=True, ahead=0, force=False, base="main"
)


def decide(**overrides):
    return b.decide_delete(**{**CLEAR, **overrides})


class TestDecideDelete:
    def test_clear_for_a_merged_unheld_branch(self):
        assert decide() is None

    def test_an_invalid_name_is_400_with_the_validator_reason(self):
        status, body = decide(name_error="branch names may not contain '..'")
        assert status == 400 and body["error"] == "branch names may not contain '..'"

    def test_the_name_row_is_first_even_over_unknown(self):
        assert decide(name_error="bad", known=False)[0] == 400

    def test_force_never_bypasses_the_name_row(self):
        assert decide(name_error="bad", force=True)[0] == 400

    def test_unknown_is_404(self):
        status, body = decide(known=False)
        assert status == 404 and "no local branch" in body["error"]

    def test_base_is_409(self):
        status, body = decide(is_base=True)
        assert status == 409 and "base branch" in body["error"]

    def test_creating_is_409(self):
        status, body = decide(creating=True)
        assert status == 409 and body["error"] == b.CREATING_REASON

    def test_checked_out_in_a_managed_worktree_says_remove_it_first(self):
        status, body = decide(checked_out_at=f"{MANAGED}/pr-demo", managed=True)
        assert status == 409
        assert body["error"] == "checked out at ~/worktrees/demo-app/pr-demo — remove the worktree first"
        assert body["worktree_removable"] is True

    def test_checked_out_elsewhere_is_not_ours(self):
        status, body = decide(checked_out_at=f"{INTENT}/x/demo-app", managed=False)
        assert status == 409
        assert body["error"] == "checked out at ~/other-tool/worktrees/x/demo-app — that worktree is not ours to remove"
        assert "worktree_removable" not in body

    def test_the_path_in_the_message_is_collapsed(self):
        _s, body = decide(checked_out_at=f"{INTENT}/x/demo-app")
        assert HOME not in body["error"]

    def test_merged_unknown_is_refused(self):
        status, body = decide(merged=None, ahead=None)
        assert status == 409 and body["error"] == b.UNKNOWN_MERGED_REASON
        assert "needs_force" not in body

    def test_unmerged_needs_force_and_carries_ahead(self):
        status, body = decide(merged=False, ahead=3)
        assert status == 409
        assert body["needs_force"] is True and body["ahead"] == 3
        assert body["error"] == "3 commits are not on main"

    def test_one_unmerged_commit_reads_singular(self):
        _s, body = decide(merged=False, ahead=1)
        assert body["error"] == "1 commit is not on main"

    def test_force_clears_only_the_unmerged_row(self):
        assert decide(merged=False, ahead=3, force=True) is None

    def test_force_never_bypasses_base(self):
        status, _ = decide(is_base=True, force=True)
        assert status == 409

    def test_force_never_bypasses_creating(self):
        status, _ = decide(creating=True, force=True)
        assert status == 409

    def test_force_never_bypasses_a_checkout(self):
        status, _ = decide(checked_out_at="/wt/x", managed=True, force=True)
        assert status == 409

    def test_force_never_bypasses_unknown(self):
        # The safe-looking default must never become permission.
        status, body = decide(merged=None, ahead=None, force=True)
        assert status == 409 and body["error"] == b.UNKNOWN_MERGED_REASON

    def test_order_each_earlier_refusal_wins_with_every_later_input_set(self):
        # All later inputs "true" at once; the earliest row must answer.
        everything = dict(is_base=True, creating=True, checked_out_at="/wt/x", managed=True, merged=False, ahead=5)
        assert decide(name_error="bad", known=False, **everything)[0] == 400
        assert decide(known=False, **everything)[0] == 404
        assert "base branch" in decide(**everything)[1]["error"]
        assert decide(**{**everything, "is_base": False})[1]["error"] == b.CREATING_REASON
        rest = {**everything, "is_base": False, "creating": False}
        assert "remove the worktree first" in decide(**rest)[1]["error"]
        rest = {**rest, "checked_out_at": None, "merged": None, "ahead": None}
        assert decide(**rest)[1]["error"] == b.UNKNOWN_MERGED_REASON
        rest = {**rest, "merged": False, "ahead": 5}
        assert decide(**rest)[1]["needs_force"] is True

    def test_landed_clears_an_unmerged_branch_without_force(self):
        # The squash-merged branch: `merged` is False, and it shipped anyway.
        assert decide(merged=False, ahead=5, landed=True) is None

    def test_landed_answers_where_merged_cannot_be_measured(self):
        assert decide(merged=None, ahead=None, landed=True) is None

    def test_landed_never_bypasses_an_earlier_row(self):
        # It sits after the checkout row: git refuses a checked-out branch
        # whatever it holds, and the useful answer is still WHICH tree.
        assert decide(name_error="bad", landed=True)[0] == 400
        assert decide(known=False, landed=True)[0] == 404
        assert "base branch" in decide(is_base=True, landed=True)[1]["error"]
        assert decide(creating=True, landed=True)[1]["error"] == b.CREATING_REASON
        status, body = decide(checked_out_at="/wt/x", managed=True, landed=True)
        assert status == 409 and body["worktree_removable"] is True

    def test_landed_defaults_to_false_so_a_caller_that_never_joined_refuses(self):
        import inspect

        assert inspect.signature(b.decide_delete).parameters["landed"].default is False
        assert decide(merged=False, ahead=5)[1]["needs_force"] is True


class TestNeedsDashDUpper:
    def test_an_ancestor_takes_plain_dash_d(self):
        assert b.needs_dash_d_upper(force=False, merged=True, landed=True) is False

    def test_a_squash_landed_branch_takes_dash_D_because_git_cannot_see_it(self):
        assert b.needs_dash_d_upper(force=False, merged=False, landed=True) is True
        assert b.needs_dash_d_upper(force=False, merged=None, landed=True) is True

    def test_force_is_dash_D_and_nothing_else_is(self):
        assert b.needs_dash_d_upper(force=True, merged=False, landed=False) is True
        assert b.needs_dash_d_upper(force=False, merged=False, landed=False) is False


def assembled(**overrides) -> dict:
    return {"branch": "x", "is_base": False, "merged": True, "ahead": 0, "worktree": None, **overrides}


class TestDeleteVerdict:
    def test_ok(self):
        assert b.delete_verdict(assembled(), base="main") == ("ok", None, False)

    def test_needs_force(self):
        verdict, reason, removable = b.delete_verdict(assembled(merged=False, ahead=2), base="main")
        assert verdict == "needs_force" and reason == "2 commits are not on main"
        assert removable is False

    def test_refused_with_a_removable_worktree(self):
        verdict, reason, removable = b.delete_verdict(
            assembled(worktree={"path": "~/worktrees/demo-app/x", "managed": True}), base="main"
        )
        assert verdict == "refused"
        assert reason == "checked out at ~/worktrees/demo-app/x — remove the worktree first"
        assert removable is True

    def test_refused_not_ours(self):
        verdict, reason, removable = b.delete_verdict(
            assembled(worktree={"path": "~/other-tool/worktrees/x/demo-app", "managed": False}), base="main"
        )
        assert verdict == "refused" and "not ours" in reason and removable is False

    def test_refused_while_creating(self):
        verdict, reason, _ = b.delete_verdict(assembled(), base="main", creating=True)
        assert verdict == "refused" and reason == b.CREATING_REASON

    def test_refused_when_merged_unknown(self):
        verdict, reason, _ = b.delete_verdict(assembled(merged=None, ahead=None), base="main")
        assert verdict == "refused" and reason == b.UNKNOWN_MERGED_REASON

    def test_base_is_refused(self):
        verdict, reason, _ = b.delete_verdict(assembled(is_base=True), base="main")
        assert verdict == "refused" and "base branch" in reason

    def test_a_name_the_validator_rejects_is_refused_not_ok(self):
        # Merged and unheld, so every OTHER row of the table says ok — and the
        # route would still answer 400, so this one must never promise `ok`.
        from worktree.names import validate_existing_branch

        verdict, reason, removable = b.delete_verdict(assembled(branch="bad name", merged=True, ahead=0), base="main")
        assert verdict == "refused"
        assert reason == validate_existing_branch("bad name")
        assert removable is False

    def test_a_plus_in_the_name_is_no_longer_a_refusal(self):
        # The regression this split exists for. git lists `fix-+-retry` and
        # `git branch -d` takes it, but a row judged by the CREATE alphabet
        # would leave it stranded as an undeletable atoll.
        assert b.delete_verdict(assembled(branch="fix-+-retry", merged=True, ahead=0), base="main") == ("ok", None, False)

    def test_the_name_row_wins_over_every_later_refusal(self):
        verdict, reason, removable = b.delete_verdict(
            assembled(
                branch="x..y", is_base=True, merged=False, ahead=3, worktree={"path": "~/worktrees/demo-app/x", "managed": True}
            ),
            base="main",
        )
        assert verdict == "refused" and reason == "branch names may not contain '..'"
        assert removable is False

    @pytest.mark.parametrize("name", ["feat/x", "a.b", "v1_2-3", "release-1.0"])
    def test_ordinary_names_are_still_ok(self, name):
        assert b.delete_verdict(assembled(branch=name), base="main")[0] == "ok"

    def test_the_measured_trap_pr_merged_but_ahead_is_needs_force_never_ok(self):
        # add-shadow-renderer / add-stopwatch-feed / youtube-playback-entity on
        # the measured machine: a MERGED pull request and a commit main lacks.
        verdict, _r, _ = b.delete_verdict(assembled(merged=False, ahead=1, pr={"state": "merged"}), base="main")
        assert verdict == "needs_force"

    def test_a_squash_landed_row_is_ok(self):
        assert b.delete_verdict(assembled(merged=False, ahead=5, landed=True), base="main") == ("ok", None, False)

    def test_landed_must_be_exactly_true(self):
        # A truthy non-bool off a hand-built row is not a proof.
        for value in ("yes", 1, None):
            verdict, _r, _ = b.delete_verdict(assembled(merged=False, ahead=5, landed=value), base="main")
            assert verdict == "needs_force", value

    def test_a_landed_branch_in_a_managed_tree_still_says_remove_it_first(self):
        verdict, _r, removable = b.delete_verdict(
            assembled(merged=False, ahead=5, landed=True, worktree={"path": "~/worktrees/demo-app/x", "managed": True}),
            base="main",
        )
        assert verdict == "refused" and removable is True


class FakeRunner:
    """Answers scripted `(code, out, err)` tuples in order and records argv."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls: list[tuple] = []

    def __call__(self, command, timeout, **kwargs):
        self.calls.append((command, timeout, kwargs))
        if not self.answers:
            raise AssertionError(f"unexpected subprocess: {command}")
        return self.answers.pop(0)


class TestSweepRefsLadder:
    def test_rung_one_succeeds(self):
        runner = FakeRunner([(0, FIXTURE, "")])
        rows, caps, err = b.sweep_refs("/repo", "main", runner=runner)
        assert len(rows) == 10 and caps == {"ahead_behind": True} and err is None
        assert len(runner.calls) == 1

    def test_old_git_retries_without_the_field(self):
        runner = FakeRunner([(128, "", OLD_GIT_STDERR), (0, OLD_GIT_FIXTURE, "")])
        rows, caps, err = b.sweep_refs("/repo", "main", runner=runner)
        assert caps == {"ahead_behind": False} and err is None
        assert len(rows) == 2 and all(r["merged"] is None for r in rows)
        assert "ahead-behind" not in runner.calls[1][0][6]

    def test_a_missing_base_is_reported_as_that_and_not_retried(self):
        runner = FakeRunner([(128, "", MISSING_BASE_STDERR)])
        rows, caps, err = b.sweep_refs("/repo", "main", runner=runner)
        assert rows == [] and err == "base branch main not found"
        assert len(runner.calls) == 1

    def test_any_other_failure_is_final(self):
        runner = FakeRunner([(-1, "", "`git` timed out after 10s")])
        rows, _caps, err = b.sweep_refs("/repo", "main", runner=runner)
        assert rows == [] and "timed out" in err
        assert len(runner.calls) == 1

    def test_both_rungs_failing_reports_the_second_error(self):
        runner = FakeRunner([(128, "", OLD_GIT_STDERR), (128, "", "fatal: not a git repository")])
        rows, caps, err = b.sweep_refs("/repo", "main", runner=runner)
        assert rows == [] and caps == {"ahead_behind": False}
        assert "not a git repository" in err

    def test_uses_the_local_timeout(self):
        runner = FakeRunner([(0, "", "")])
        b.sweep_refs("/repo", "main", runner=runner)
        assert runner.calls[0][1] == b.GIT_LOCAL_TIMEOUT_SECONDS


# 8 fields: refname:short, symref, objectname:short, objectname,
# ahead-behind:<remote>/main, committerdate:unix, authorname, subject. Invented,
# one row per shape a remote sweep produces.
REMOTE_FIXTURE = (
    "\n".join(
        [
            # the symref git keeps for the remote's default branch. Its short name is
            # the BARE remote name, which reads as a branch called `origin` until you
            # look at the second field.
            row(
                "origin",
                "refs/remotes/origin/main",
                "a1b2c3d01",
                full("a1b2c3d01"),
                "0 0",
                "1788620000",
                AUTHOR,
                "tidy up before the next release",
            ),
            # the remote base itself — measured against, never cleanup
            row(
                "origin/main", "", "a1b2c3d01", full("a1b2c3d01"), "0 0", "1788620000", AUTHOR, "tidy up before the next release"
            ),
            # landed on origin/main: the cleanup signal
            row(
                "origin/rss-item-entity",
                "",
                "aaaa11111",
                full("aaaa11111"),
                "0 240",
                "1769000000",
                AUTHOR,
                "feat(rss): add feed entity",
            ),
            # a slash in the branch name — the split is on the FIRST separator only
            row(
                "origin/claude/feature/web-components",
                "",
                "bbbb22222",
                full("bbbb22222"),
                "0 36",
                "1780000000",
                AUTHOR,
                "feat: web extension",
            ),
            # NOT landed: two commits origin/main lacks
            row("origin/unlanded-thing", "", "cccc33333", full("cccc33333"), "2 10", "1781000000", AUTHOR, "wip"),
            # merged as PR #32, branch never deleted on the remote and never pulled
            # back down locally: the exact shape this tier exists to surface
            row(
                "origin/feat/end-to-end-validation",
                "",
                "a1b2c3d04",
                full("a1b2c3d04"),
                "0 2",
                "1788500000",
                AUTHOR,
                "fix: retry the upload once",
            ),
            # already a local island (`test123` is in FIXTURE) — must not double up
            row(
                "origin/test123",
                "",
                "a1b2c3d0a",
                full("a1b2c3d0a"),
                "0 217",
                "1771440000",
                AUTHOR,
                "feat: pause the timer on blur",
            ),
        ]
    )
    + "\n"
)


class TestParseRemoteRefs:
    def rows(self, text=REMOTE_FIXTURE, remote="origin", base="main"):
        return b.parse_remote_refs(text, remote, base, ahead_behind=True)

    def test_drops_the_head_symref(self):
        # The regression that named this test: without the symref field the
        # bare `origin` row reads as a branch NAMED origin, and it was read
        # that way once.
        assert not any(r["branch"] == "" or r["ref"] == "origin" for r in self.rows())

    def test_drops_the_remote_base(self):
        assert not any(r["branch"] == "main" for r in self.rows())

    def test_splits_on_the_first_separator_only(self):
        row_ = next(r for r in self.rows() if r["sha"] == "bbbb22222")
        assert row_["remote"] == "origin"
        assert row_["branch"] == "claude/feature/web-components"
        assert row_["ref"] == "origin/claude/feature/web-components"

    def test_merged_is_ahead_zero_against_the_remote_base(self):
        by_name = {r["branch"]: r for r in self.rows()}
        assert by_name["rss-item-entity"]["merged"] is True
        assert by_name["rss-item-entity"]["behind"] == 240
        assert by_name["unlanded-thing"]["merged"] is False
        assert by_name["unlanded-thing"]["ahead"] == 2

    def test_without_the_field_every_merge_state_is_unknown(self):
        text = row("origin/x", "", "abc", full("abc"), "1770000000", AUTHOR, "s") + "\n"
        rows = b.parse_remote_refs(text, "origin", "main", ahead_behind=False)
        assert len(rows) == 1
        assert rows[0]["merged"] is None and rows[0]["ahead"] is None

    def test_ignores_refs_from_another_remote(self):
        # Each sweep is scoped to one remote; a stray line is not adopted.
        text = row("gitlab/thing", "", "abc", full("abc"), "0 1", "1770000000", AUTHOR, "s") + "\n"
        assert b.parse_remote_refs(text, "origin", "main", ahead_behind=True) == []

    def test_is_total(self):
        assert b.parse_remote_refs(None, "origin", "main", ahead_behind=True) == []
        assert b.parse_remote_refs("too\x1ffew\x1ffields\n", "origin", "main", ahead_behind=True) == []


class TestRemoteOnly:
    def test_hides_branches_that_are_already_islands(self):
        rows = b.parse_remote_refs(REMOTE_FIXTURE, "origin", "main", ahead_behind=True)
        names = {r["branch"] for r in b.remote_only(rows, {"test123"})}
        assert "test123" not in names
        assert "rss-item-entity" in names

    def test_matches_across_remotes_by_name(self):
        # A branch pushed to two remotes and checked out locally is hidden on
        # BOTH — the local island's upstream line already names one of them.
        rows = [{"branch": "shared", "remote": "origin"}, {"branch": "shared", "remote": "gitlab"}]
        assert b.remote_only(rows, {"shared"}) == []

    def test_is_total_on_no_local_branches(self):
        rows = b.parse_remote_refs(REMOTE_FIXTURE, "origin", "main", ahead_behind=True)
        assert len(b.remote_only(rows, None)) == len(rows)


class TestParseIso8601:
    def test_the_z_suffix_gh_actually_ships(self):
        # Exact, not approx: a default relative tolerance is ~30 minutes here
        # and would hide a whole timezone's worth of error.
        assert b.parse_iso8601("2030-01-02T03:04:05Z") == 1893553445.0

    def test_an_explicit_offset_is_read_too(self):
        assert b.parse_iso8601("2030-01-02T03:04:05+00:00") == 1893553445.0

    def test_a_naive_stamp_refuses_rather_than_reading_as_local_time(self):
        # `.timestamp()` on a naive datetime uses the machine's zone, and half
        # of those shifts move a branch toward "landed".
        assert b.parse_iso8601("2030-01-02T03:04:05") is None

    def test_the_reading_does_not_depend_on_the_machines_timezone(self, monkeypatch):
        import time as _time

        for zone in ("UTC", "America/New_York", "Pacific/Kiritimati"):
            monkeypatch.setenv("TZ", zone)
            _time.tzset()
            assert b.parse_iso8601("2030-01-02T03:04:05Z") == 1893553445.0
        monkeypatch.delenv("TZ", raising=False)
        _time.tzset()

    def test_anything_unparseable_is_none_never_zero(self):
        # Zero would read as 1970 and make every branch look landed.
        for bad in (None, "", "yesterday", 17, "2026-13-45T99:99:99Z", {}):
            assert b.parse_iso8601(bad) is None


#: The tip every `remote_row` carries, and by default the head its merged PR shipped.
TIP = full("abc")
#: A head that is NOT the tip — the PR took commits this checkout never saw,
#: or the tip took commits the PR never shipped.
OTHER_HEAD = full("def")


def remote_row(**over):
    row = {
        "ref": "origin/x",
        "remote": "origin",
        "branch": "x",
        "sha": "abc",
        "oid": TIP,
        "committed_at": 1000,
        "author": "a",
        "subject": "s",
        "ahead": None,
        "behind": None,
        "merged": None,
        "pr": None,
    }
    row.update(over)
    return row


def pr(state="merged", merged_at="1970-01-01T00:30:00Z", head_oid=TIP):
    return {"number": 1, "state": state, "url": "u", "draft": False, "merged_at": merged_at, "head_oid": head_oid}


class Asked:
    """A `contains` that answers one fixed value and records every question."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, head, tip):
        self.calls.append((head, tip))
        return self.answer


class TestLanded:
    def test_an_ancestor_of_the_base_needs_no_pr(self):
        assert b.landed(remote_row(merged=True, ahead=0)) is True

    def test_a_squash_merge_lands_without_ever_being_an_ancestor(self):
        # The case the ancestor test structurally cannot see: the commits were
        # rewritten, so `merged` is False and the work is on the base anyway.
        row = remote_row(merged=False, ahead=3, committed_at=1000, pr=pr())
        assert b.landed(row) is True

    def test_a_squash_merged_branch_whose_tip_is_the_squashed_head(self):
        # 5 ahead, PR #57 squash-merged, local tip == the head GitHub squashed.
        row = remote_row(merged=False, ahead=5, committed_at=1000, pr=pr(merged_at="2026-10-01T15:14:46Z"))
        assert b.landed(row) is True

    def test_a_commit_after_the_pr_head_is_not_landed_whatever_its_date(self):
        # THE hole a clock test alone leaves: committed BEFORE the merge,
        # pushed AFTER it. The date passes; the id does not.
        row = remote_row(merged=False, ahead=4, committed_at=1000, pr=pr(head_oid=OTHER_HEAD))
        assert b.landed(row, Asked(False)) is False

    def test_a_tip_behind_the_pr_head_is_landed(self):
        # The PR took more commits from another checkout and squashed them
        # all; this tip is one of them.
        row = remote_row(merged=False, ahead=3, committed_at=1000, pr=pr(head_oid=OTHER_HEAD))
        asked = Asked(True)
        assert b.landed(row, asked) is True
        assert asked.calls == [(OTHER_HEAD, TIP)]

    def test_a_head_git_cannot_place_refuses(self):
        # Never fetched here: `merge-base` answers neither yes nor no.
        row = remote_row(merged=False, ahead=3, committed_at=1000, pr=pr(head_oid=OTHER_HEAD))
        assert b.landed(row, Asked(None)) is False
        assert b.landed(row) is False, "no probe at all is the same refusal"

    def test_the_probe_is_never_asked_when_a_cheaper_test_already_answered(self):
        asked = Asked(True)
        b.landed(remote_row(merged=False, ahead=1, committed_at=1000, pr=pr()), asked)
        b.landed(remote_row(merged=False, ahead=1, committed_at=9999, pr=pr(head_oid=OTHER_HEAD)), asked)
        b.landed(remote_row(merged=False, ahead=1, pr=pr("open", head_oid=OTHER_HEAD)), asked)
        assert asked.calls == []

    def test_a_merged_pr_pushed_to_afterwards_is_not_landed_even_at_the_same_id(self):
        # Belt for the day a forge moves a merged PR's head: a commit newer
        # than the merge is new work, whatever the index says the head is.
        row = remote_row(merged=False, ahead=3, committed_at=9999, pr=pr())
        assert b.landed(row) is False

    def test_a_commit_exactly_at_the_merge_stamp_is_landed(self):
        row = remote_row(merged=False, ahead=1, committed_at=1800, pr=pr())
        assert b.landed(row) is True

    def test_an_open_or_closed_pr_never_lands_a_branch(self):
        for state in ("open", "closed"):
            row = remote_row(merged=False, ahead=1, committed_at=1, pr=pr(state))
            assert b.landed(row) is False

    def test_a_merged_pr_with_no_stamp_cannot_be_trusted(self):
        row = remote_row(merged=False, ahead=1, committed_at=1, pr=pr(merged_at=None))
        assert b.landed(row) is False

    def test_an_index_from_before_head_oid_existed_refuses_until_the_next_fetch(self):
        row = remote_row(merged=False, ahead=1, committed_at=1, pr=pr(head_oid=None))
        assert b.landed(row, Asked(True)) is False

    def test_abbreviations_never_compare(self):
        # Equal SHORT ids prove nothing about objects this repo never had.
        row = remote_row(merged=False, ahead=1, committed_at=1, oid="abc", pr=pr(head_oid="abc"))
        assert b.landed(row) is False

    def test_no_pr_and_not_an_ancestor_is_not_landed(self):
        assert b.landed(remote_row(merged=False, ahead=2)) is False

    def test_an_unmeasured_row_is_not_landed_on_the_ancestor_half(self):
        assert b.landed(remote_row(merged=None)) is False

    def test_a_missing_commit_stamp_refuses_rather_than_reads_as_1970(self):
        for stamp in (0, None, "1000", True):
            row = remote_row(merged=False, ahead=1, committed_at=stamp, pr=pr())
            assert b.landed(row) is False


class TestJoinLanding:
    def test_attaches_the_pr_and_decides_landed_in_place(self):
        rows = [remote_row(branch="shipped", merged=False, ahead=2), remote_row(branch="live", merged=False, ahead=2)]
        index = {"shipped": pr()}
        out = b.join_landing(rows, index.get)
        assert out is rows
        assert rows[0]["pr"] == pr() and rows[0]["landed"] is True
        assert rows[1]["pr"] is None and rows[1]["landed"] is False

    def test_the_probe_reaches_landed(self):
        rows = [remote_row(merged=False, ahead=2, pr=None)]
        asked = Asked(True)
        b.join_landing(rows, lambda _b: pr(head_oid=OTHER_HEAD), asked)
        assert rows[0]["landed"] is True and asked.calls == [(OTHER_HEAD, TIP)]


def judged(pr_row=None, **over) -> dict:
    """A remote row that went through the one door, as the plugin's do."""
    return b.join_landing([remote_row(**over)], lambda _b: pr_row)[0]


class TestScuttleVerdict:
    def test_landed_is_clear_with_no_reason(self):
        assert b.scuttle_verdict(judged(merged=True, ahead=0)) == ("clear", None)

    def test_a_squash_merge_is_clear_too(self):
        row = judged(pr(), merged=False, ahead=3, committed_at=1000)
        assert b.scuttle_verdict(row) == ("clear", None)

    def test_a_row_that_skipped_the_join_refuses(self):
        # Even an ancestor: no `landed` decided means nothing was proven.
        row = remote_row(merged=False, ahead=3, committed_at=1000, pr=pr())
        assert b.scuttle_verdict(row)[0] == "refused"

    def test_measured_and_unlanded_names_the_branch_not_the_sweep(self):
        verdict, reason = b.scuttle_verdict(judged(merged=False, ahead=2))
        assert verdict == "refused" and reason == b.REMOTE_UNLANDED_REASON

    def test_unmeasurable_names_the_sweep_not_the_branch(self):
        verdict, reason = b.scuttle_verdict(judged(merged=None))
        assert verdict == "refused" and reason == b.REMOTE_UNKNOWN_MERGED_REASON

    def test_an_unmeasured_row_with_a_merged_pr_is_still_clear(self):
        # An old git drops `%(ahead-behind:…)` entirely; the PR still answers.
        row = judged(pr(), merged=None, committed_at=1000)
        assert b.scuttle_verdict(row) == ("clear", None)

    def test_a_closed_unmerged_pr_is_refused(self):
        row = judged(pr("closed"), merged=False, ahead=2, committed_at=1)
        assert b.scuttle_verdict(row)[0] == "refused"

    def test_a_merged_pr_that_did_not_ship_this_tip_is_refused(self):
        row = judged(pr(head_oid=OTHER_HEAD), merged=False, ahead=2, committed_at=1)
        assert b.scuttle_verdict(row) == ("refused", b.REMOTE_UNLANDED_REASON)

    def test_there_is_no_force_to_pass(self):
        # A remote delete is a network write others see: "merged only" is a
        # PREDICATE gate, so no argument exists to unlock it.
        import inspect

        assert list(inspect.signature(b.scuttle_verdict).parameters) == ["row"]


class TestBuildPushDeleteCommand:
    def test_it_is_the_plain_porcelain_so_it_works_off_github(self):
        assert b.build_push_delete_command("/repo", "gitlab", "x") == ["git", "-C", "/repo", "push", "gitlab", "--delete", "x"]


class TestSweepRemoteRefsLadder:
    def test_one_sweep_per_remote_each_against_its_own_base(self):
        runner = FakeRunner([(0, REMOTE_FIXTURE, ""), (0, "", "")])
        rows, err = b.sweep_remote_refs("/repo", "main", remotes=["origin", "gitlab"], runner=runner)
        assert err is None and len(runner.calls) == 2
        assert all(r["remote"] == "origin" for r in rows)
        assert "ahead-behind:origin/main" in runner.calls[0][0][-1]
        assert "ahead-behind:gitlab/main" in runner.calls[1][0][-1]
        assert runner.calls[0][0][4] == "refs/remotes/origin/"

    def test_old_git_retries_without_the_field(self):
        text = row("origin/x", "", "abc", full("abc"), "1770000000", AUTHOR, "s") + "\n"
        runner = FakeRunner([(128, "", OLD_GIT_STDERR), (0, text, "")])
        rows, err = b.sweep_remote_refs("/repo", "main", remotes=["origin"], runner=runner)
        assert err is None and len(rows) == 1 and rows[0]["merged"] is None

    def test_a_missing_remote_base_still_yields_rows(self):
        # Softer than the local sweep on purpose: a remote with no
        # `<remote>/main` still has branches worth NAMING, only their merge
        # state is unknown.
        text = row("origin/x", "", "abc", full("abc"), "1770000000", AUTHOR, "s") + "\n"
        runner = FakeRunner([(128, "", MISSING_BASE_STDERR), (0, text, "")])
        rows, err = b.sweep_remote_refs("/repo", "main", remotes=["origin"], runner=runner)
        assert err is None and len(rows) == 1 and rows[0]["branch"] == "x"

    def test_one_bad_remote_does_not_empty_the_others(self):
        # The additive rule: this tier arrived after the harbor was already
        # complete, so it must never take the document down with it.
        expected = b.parse_remote_refs(REMOTE_FIXTURE, "origin", "main", ahead_behind=True)
        runner = FakeRunner([(0, REMOTE_FIXTURE, ""), (-1, "", "`git` timed out after 10s")])
        rows, err = b.sweep_remote_refs("/repo", "main", remotes=["origin", "gitlab"], runner=runner)
        assert rows == expected and expected  # origin's rows all survive
        assert err is not None
        assert err.startswith("gitlab: ") and "timed out" in err

    def test_no_remotes_is_no_rows_and_no_error(self):
        runner = FakeRunner([])
        assert b.sweep_remote_refs("/repo", "main", remotes=[], runner=runner) == ([], None)

    def test_discovers_remotes_when_none_are_passed(self):
        runner = FakeRunner([(0, "origin\ngitlab\norigin\n", ""), (0, "", ""), (0, "", "")])
        b.sweep_remote_refs("/repo", "main", runner=runner)
        assert runner.calls[0][0] == ["git", "-C", "/repo", "remote"]
        assert len(runner.calls) == 3  # deduped: two remotes, not three

    def test_uses_the_local_timeout(self):
        runner = FakeRunner([(0, "", "")])
        b.sweep_remote_refs("/repo", "main", remotes=["origin"], runner=runner)
        assert runner.calls[0][1] == b.GIT_LOCAL_TIMEOUT_SECONDS


class TestAncestryCache:
    HEAD, TIP_ = full("aaa"), full("bbb")

    def cache(self, answers, clock=None):
        runner = FakeRunner(answers)
        return b.AncestryCache("/repo", runner=runner, clock=clock or FakeClock()), runner

    def test_exit_codes_map_to_yes_no_unknown(self):
        for code, expected in ((0, True), (1, False), (128, None), (-1, None)):
            cache, _r = self.cache([(code, "", "")])
            assert cache.contains(self.HEAD, self.TIP_) is expected, code

    def test_asks_whether_the_tip_is_an_ancestor_of_the_head(self):
        cache, runner = self.cache([(0, "", "")])
        cache.contains(self.HEAD, self.TIP_)
        assert runner.calls[0][0] == b.build_is_ancestor_command("/repo", self.TIP_, self.HEAD)
        assert runner.calls[0][1] == b.GIT_LOCAL_TIMEOUT_SECONDS

    def test_a_definite_answer_is_asked_once_ever(self):
        clock = FakeClock()
        cache, runner = self.cache([(1, "", "")], clock)
        assert cache.contains(self.HEAD, self.TIP_) is False
        clock.now += 10 * b.ANCESTRY_UNKNOWN_TTL_SECONDS
        assert cache.contains(self.HEAD, self.TIP_) is False
        assert len(runner.calls) == 1

    def test_an_unknown_is_retried_only_after_its_ttl(self):
        # The head may arrive with the next fetch, but not every 4 s sweep.
        clock = FakeClock()
        cache, runner = self.cache([(128, "", "bad object"), (0, "", "")], clock)
        assert cache.contains(self.HEAD, self.TIP_) is None
        clock.now += b.ANCESTRY_UNKNOWN_TTL_SECONDS - 1
        assert cache.contains(self.HEAD, self.TIP_) is None
        assert len(runner.calls) == 1
        clock.now += 2
        assert cache.contains(self.HEAD, self.TIP_) is True
        assert len(runner.calls) == 2

    def test_a_full_cache_clears_rather_than_grows(self, monkeypatch):
        monkeypatch.setattr(b, "ANCESTRY_CACHE_LIMIT", 2)
        cache, runner = self.cache([(0, "", "")] * 4)
        for tip in (full("1"), full("2"), full("3")):
            cache.contains(self.HEAD, tip)
        assert len(cache._known) == 1
        cache.contains(self.HEAD, full("1"))  # evicted, so asked again
        assert len(runner.calls) == 4


class TestListRemotes:
    def test_parses_and_dedupes(self):
        runner = FakeRunner([(0, "origin\ngitlab\n\norigin\n", "")])
        assert b.list_remotes("/repo", runner=runner) == ["origin", "gitlab"]

    def test_empty_on_failure(self):
        assert b.list_remotes("/repo", runner=FakeRunner([(128, "", "fatal")])) == []


class TestStashCount:
    def test_counts_lines(self):
        runner = FakeRunner([(0, "stash@{0}: WIP on main: abc\nstash@{1}: x\n", "")])
        assert b.stash_count("/repo", runner=runner) == 2

    def test_zero_on_failure(self):
        assert b.stash_count("/repo", runner=FakeRunner([(128, "", "fatal")])) == 0


class FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class TestDirtySweep:
    def test_dirty_clean_and_failed(self):
        runner = FakeRunner([(0, " M file.ts\n", ""), (0, "", ""), (128, "", "fatal")])
        result = b.dirty_sweep(["/a", "/b", "/c"], 5.0, runner=runner)
        assert result == {"/a": True, "/b": False, "/c": None}
        assert runner.calls[0][0] == ["git", "-C", "/a", "status", "--porcelain"]

    def test_budget_exhaustion_leaves_the_tail_none(self):
        clock = FakeClock()
        runner = FakeRunner([(0, "", ""), (0, "", "")])

        def probe(command, timeout, **kw):
            clock.now += 3.0  # each probe costs 3 s against a 5 s budget
            return runner(command, timeout, **kw)

        result = b.dirty_sweep(["/a", "/b", "/c", "/d"], 5.0, clock=clock, runner=probe)
        assert result == {"/a": False, "/b": False, "/c": None, "/d": None}

    def test_each_probe_gets_only_the_remaining_budget(self):
        clock = FakeClock()
        runner = FakeRunner([(0, "", ""), (0, "", "")])

        def probe(command, timeout, **kw):
            clock.now += 3.5
            return runner(command, timeout, **kw)

        b.dirty_sweep(["/a", "/b"], 5.0, clock=clock, runner=probe)
        assert runner.calls[0][1] == 5  # the whole budget
        assert runner.calls[1][1] == 2  # ceil(1.5) — never the full 10 s local timeout

    def test_untracked_files_count_as_dirty(self):
        result = b.dirty_sweep(["/a"], 5.0, runner=FakeRunner([(0, "?? new.ts\n", "")]))
        assert result == {"/a": True}


class TestFetchHeadMtime:
    def test_reads_the_file_and_tolerates_its_absence(self, tmp_path):
        assert b.fetch_head_mtime(tmp_path) is None
        (tmp_path / ".git").mkdir()
        (tmp_path / ".git" / "FETCH_HEAD").write_text("x")
        assert isinstance(b.fetch_head_mtime(tmp_path), float)


# ── the pure helpers the sweep and the document lean on ───────────────────

from worktree.listing import main_worktree_path, parse_worktree_list, worktree_by_branch  # noqa: E402
from worktree.names import display_path, is_primary_checkout  # noqa: E402

PORCELAIN = f"""worktree {REPO}
HEAD a1b2c3d010000000000000000000000000000000
branch refs/heads/main

worktree {MANAGED}/pr-demo
HEAD a1b2c3d020000000000000000000000000000000
branch refs/heads/pr-demo
locked

worktree {PRUNABLE_DIR}
HEAD 89abcdef00000000000000000000000000000000
branch refs/heads/stale-wt
prunable gitdir file points to non-existent location

worktree {INTENT}/spike/demo-app
HEAD 999aaa0000000000000000000000000000000000
detached
locked agent still running
"""


class TestDisplayPath:
    def test_collapses_home(self):
        assert display_path(f"{HOME}/other-tool/worktrees/x/demo-app") == "~/other-tool/worktrees/x/demo-app"

    def test_home_itself_is_tilde(self):
        assert display_path(HOME) == "~"

    def test_a_sibling_prefix_is_not_home(self):
        # `/Users/someone2/…` starts with the home STRING but is not under it.
        assert display_path(f"{HOME}2/x") == f"{HOME}2/x"

    def test_leaves_foreign_paths_and_none(self):
        assert display_path("/opt/elsewhere") == "/opt/elsewhere"
        assert display_path(None) is None

    def test_is_idempotent_on_a_collapsed_path(self):
        assert display_path("~/worktrees/demo-app/x") == "~/worktrees/demo-app/x"

    def test_accepts_a_path_object(self):
        assert display_path(Path(HOME) / "a") == "~/a"


class TestScrubHome:
    """`names.scrub_home`: the free-text twin of `display_path`."""

    def test_a_git_stderr_with_a_home_path_is_collapsed(self):
        from worktree.names import scrub_home

        text = f"fatal: cannot change to '{HOME}/no-such-repo': No such file or directory"
        assert scrub_home(text) == "fatal: cannot change to '~/no-such-repo': No such file or directory"

    def test_every_occurrence_goes(self):
        from worktree.names import scrub_home

        assert scrub_home(f"{HOME}/a and {HOME}/b") == "~/a and ~/b"

    def test_a_bare_home_at_a_boundary_is_tilde(self):
        from worktree.names import scrub_home

        assert scrub_home(f"in '{HOME}'") == "in '~'"
        assert scrub_home(HOME) == "~"

    def test_a_sibling_prefix_is_not_home(self):
        from worktree.names import scrub_home

        assert scrub_home(f"{HOME}2/x") == f"{HOME}2/x"
        assert scrub_home(f"{HOME}.bak") == f"{HOME}.bak"

    def test_is_idempotent_and_total(self):
        from worktree.names import scrub_home

        once = scrub_home(f"{HOME}/x")
        assert scrub_home(once) == once == "~/x"
        assert scrub_home(None) is None
        assert scrub_home("") == ""
        assert scrub_home("/opt/other/path") == "/opt/other/path"


class TestIsPrimaryCheckout:
    def test_equal_paths(self, tmp_path):
        assert is_primary_checkout(tmp_path, tmp_path) is True
        assert is_primary_checkout(str(tmp_path), tmp_path) is True

    def test_compares_resolved_paths(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real)
        assert is_primary_checkout(link, real) is True

    def test_a_child_is_not_the_main_checkout(self, tmp_path):
        assert is_primary_checkout(tmp_path / "child", tmp_path) is False

    def test_is_total(self):
        for value in (None, "", 42):
            assert is_primary_checkout(value, "/x") is False
            assert is_primary_checkout("/x", value) is False


class TestWorktreeListAdditions:
    def test_prunable_with_its_reason(self):
        rows = {r["branch"]: r for r in parse_worktree_list(PORCELAIN)}
        stale = rows["stale-wt"]
        assert stale["prunable"] is True
        assert stale["prunable_reason"] == "gitdir file points to non-existent location"
        assert stale["locked"] is False

    def test_locked_bare_and_with_a_reason(self):
        rows = parse_worktree_list(PORCELAIN)
        assert rows[1]["locked"] is True and rows[1]["prunable"] is False
        assert rows[3]["locked"] is True and rows[3]["detached"] is True

    def test_an_ordinary_stanza_defaults_both_off(self):
        rows = parse_worktree_list(PORCELAIN)
        assert rows[0]["prunable"] is False and rows[0]["locked"] is False
        assert rows[0]["prunable_reason"] is None

    def test_the_old_keys_are_unchanged(self):
        rows = parse_worktree_list(PORCELAIN)
        assert rows[0] == {
            "path": REPO,
            "branch": "main",
            "head": "a1b2c3d010000000000000000000000000000000",
            "detached": False,
            "prunable": False,
            "prunable_reason": None,
            "locked": False,
        }

    def test_main_worktree_path_is_the_first_stanza(self):
        assert main_worktree_path(parse_worktree_list(PORCELAIN)) == REPO
        assert main_worktree_path([]) is None

    def test_worktree_by_branch_skips_detached_and_keeps_the_first(self):
        by_branch = worktree_by_branch(parse_worktree_list(PORCELAIN))
        assert set(by_branch) == {"main", "pr-demo", "stale-wt"}
        assert by_branch["stale-wt"]["path"] == PRUNABLE_DIR
        twice = [{"path": "/a", "branch": "x"}, {"path": "/b", "branch": "x"}]
        assert worktree_by_branch(twice)["x"]["path"] == "/a"


class TestContainedAndBase:
    """2026-10-08: `contained` clears like `landed`; a PR must be merged INTO the base."""

    ARGS = dict(
        known=True,
        is_base=False,
        creating=False,
        checked_out_at=None,
        managed=False,
        merged=False,
        ahead=3,
        force=False,
        base="main",
    )

    def test_contained_clears_where_unmerged_would_need_force(self):
        assert b.decide_delete(**self.ARGS)[1]["needs_force"] is True
        assert b.decide_delete(**self.ARGS, contained=True) is None

    def test_contained_never_clears_a_checked_out_branch(self):
        refusal = b.decide_delete(**{**self.ARGS, "checked_out_at": f"{MANAGED}/x", "managed": True}, contained=True)
        assert refusal[0] == 409 and refusal[1]["worktree_removable"] is True

    def test_a_contained_delete_runs_dash_D(self):
        assert b.needs_dash_d_upper(force=False, merged=False, landed=False, contained=True)
        assert not b.needs_dash_d_upper(force=False, merged=True, landed=True, contained=True)

    def test_landed_with_a_base_requires_the_pr_to_target_it(self):
        row = {
            "merged": False,
            "committed_at": 1,
            "oid": full("ab"),
            "pr": {"state": "merged", "merged_at": "2026-10-01T00:00:00Z", "head_oid": full("ab"), "base_ref": "feat/parent"},
        }
        assert b.landed(row) is True  # no base asked: the old answer
        assert b.landed(row, base="main") is False
        row["pr"]["base_ref"] = "main"
        assert b.landed(row, base="main") is True
        row["pr"]["base_ref"] = None  # an index from before the field
        assert b.landed(row, base="main") is False
