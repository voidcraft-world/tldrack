"""The content proofs (`worktree/landing.py`): its parsers, the proof ladder,
containment, the ledger, the outside-remove table, and the tier end to end
against a scripted runner — never a fork.

The shapes are invented to cover the three proofs: one post-merge commit that
landed as `… (#12)` through another branch, `agent-*` branches each
carrying one commit another branch also carries, and squash-merged branches
whose commits match a base commit's patch-id.
"""

import json

import pytest

from worktree import landing as L
from worktree.branches import FIELD_SEP


def oid(seed: str) -> str:
    """A full lowercase object id starting with `seed` (hex only)."""
    return (seed + "0" * 40)[:40]


def record(commit: str, subject: str, parents=(), committed_at=1790000000) -> str:
    return FIELD_SEP.join((commit, commit[:9], " ".join(parents), str(committed_at), subject))


BASE = "main"
BASE_TIP = oid("ba5e")
MB = oid("b0b0")


class TestParsers:
    def test_patch_ids_map_commit_to_patch(self):
        text = f"{oid('aa')} {oid('c1')}\n{oid('bb')} {oid('c2')}\nnoise\n"
        assert L.parse_patch_ids(text) == {oid("c1"): oid("aa"), oid("c2"): oid("bb")}
        assert L.parse_patch_ids(None) == {}

    def test_a_single_diffs_patch_id_ignores_the_zero_commit(self):
        assert L.parse_single_patch_id(f"{oid('aa')} {'0' * 40}\n") == oid("aa")
        assert L.parse_single_patch_id("") is None
        assert L.parse_single_patch_id(None) is None

    def test_log_records_are_total(self):
        text = "\n".join(
            [
                record(oid("c1"), "one", [oid("f1")]),
                record(oid("c2"), "a merge", [oid("f1"), oid("f2")]),
                "garbage",
                FIELD_SEP.join(("notanoid", "x", "", "1", "s")),
            ]
        )
        records = L.parse_log_records(text)
        assert [r["oid"] for r in records] == [oid("c1"), oid("c2")]
        assert records[1]["parents"] == [oid("f1"), oid("f2")]
        assert records[0]["subject"] == "one" and records[0]["committed_at"] == 1790000000

    def test_missing_objects_reads_batch_check(self):
        text = f"{oid('a1')} missing\n{oid('a2')} commit 240\n"
        assert L.parse_missing_objects(text) == {oid("a1")}

    def test_lsof_pairs_each_path_with_its_command(self):
        text = "p1\nczsh\nfcwd\nn/a/b\np2\ncnode\nfcwd\nn/a/b/c\np3\nn/orphan\n"
        assert L.parse_lsof_cwds(text) == [("zsh", "/a/b"), ("node", "/a/b/c")]

    def test_claude_codes_version_named_binary_reads_as_claude(self):
        text = "p1\nc2.1.289\nfcwd\nn/w/a\np2\nc3.2\nfcwd\nn/w/a\n"
        assert L.parse_lsof_cwds(text) == [("claude", "/w/a"), ("3.2", "/w/a")]

    def test_squash_titles_read_the_trailing_pr_number_only(self):
        records = L.parse_log_records(
            "\n".join(
                [
                    record(oid("c1"), "Add the export page (#12)"),
                    record(oid("c2"), "mentions (#3) in the middle"),
                    record(oid("c3"), "plain"),
                ]
            )
        )
        assert L.squash_titles(records) == {"Add the export page": 12}


class TestOccupants:
    def test_a_segment_boundary_match_and_the_longest_tree_wins(self):
        paths = ["/w/a", "/w/a/nested", "/w/ab"]
        cwds = [("zsh", "/w/a"), ("node", "/w/a/nested/src"), ("vim", "/w/abc")]
        assert L.occupants_by_path(paths, cwds) == {"/w/a": ["zsh"], "/w/a/nested": ["node"], "/w/ab": []}

    def test_names_are_unique_and_capped(self):
        cwds = [(name, "/w/a") for name in ("bash", "bash", "node", "sed", "grep")]
        assert L.occupants_by_path(["/w/a"], cwds) == {"/w/a": ["bash", "node", "sed"]}

    def test_an_lsof_that_cannot_answer_reads_as_unknown_never_as_empty(self):
        out = L.sweep_occupants(["/w/a"], runner=lambda *_a, **_k: (-1, "", "timed out"))
        assert out == {"/w/a": None}

    def test_an_lsof_exit_1_with_output_is_still_the_answer(self, tmp_path):
        tree = tmp_path / "tree"
        tree.mkdir()
        real = str(tree.resolve())
        out = L.sweep_occupants([str(tree)], runner=lambda *_a, **_k: (1, f"p1\nczsh\nfcwd\nn{real}\n", "lsof: WARNING"))
        assert out == {str(tree): ["zsh"]}


class TestMergedHeadsAndReach:
    PRS = {
        "a": {"number": 1, "state": "merged", "base_ref": "main", "head_oid": oid("a1"), "merged_at": "2026-10-01T00:00:00Z"},
        "stacked": {"number": 2, "state": "merged", "base_ref": "a", "head_oid": oid("b1"), "merged_at": "2026-10-01T00:00:00Z"},
        "open": {"number": 3, "state": "open", "base_ref": "main", "head_oid": oid("c1"), "merged_at": None},
        "nostamp": {"number": 4, "state": "merged", "base_ref": "main", "head_oid": oid("d1"), "merged_at": None},
    }

    def test_only_prs_merged_into_the_base_count(self):
        assert set(L.merged_heads(self.PRS, "main")) == {oid("a1")}

    def test_reach_walks_through_merges(self):
        records = L.parse_log_records(
            "\n".join(
                [
                    record(oid("a1"), "head", [oid("dd1")]),
                    record(oid("dd1"), "merge main in", [oid("ab1"), oid("ba5e")]),
                    record(oid("ab1"), "work", [oid("ba5e")]),
                ]
            )
        )
        reach = L.pr_reach(records, {oid("a1"): {"number": 1}})
        assert set(reach) == {oid("a1"), oid("dd1"), oid("ab1")}
        assert reach[oid("ab1")] == [1]

    def test_shipped_by_needs_the_clock_and_takes_the_earliest_merge(self):
        prs = {1: {"merged_at": "2026-10-05T00:00:00Z"}, 2: {"merged_at": "2026-10-02T00:00:00Z"}}
        reach = {oid("7a1"): [1, 2]}
        before = {"oid": oid("7a1"), "committed_at": 1790000000}  # 2026-09-21
        after = {"oid": oid("7a1"), "committed_at": 1800000000}  # 2027
        assert L.shipped_by(before, reach, prs) == 2
        assert L.shipped_by(after, reach, prs) is None
        assert L.shipped_by({"oid": "short", "committed_at": 1}, reach, prs) is None


def commits(*pairs):
    return [{"oid": o, "sha": o[:9], "subject": s, "parents": [MB], "committed_at": 1} for o, s in pairs]


class TestDecideContent:
    def base_args(self, **over):
        args = dict(
            commits=commits((oid("c1"), "one"), (oid("c2"), "two")),
            commit_pids={oid("c1"): oid("f1"), oid("c2"): oid("f2")},
            cumulative_pid=oid("c0de"),
            cumulative_empty=False,
            base_pids=set(),
            pr_pids={},
            titles={},
        )
        args.update(over)
        return args

    def test_squash_is_the_cumulative_patch_on_the_base(self):
        landing, _ledger = L.decide_content(**self.base_args(base_pids={oid("c0de")}))
        assert landing == {"how": "squash", "pr": None}

    def test_commits_is_every_commit_found_and_names_one_pr(self):
        landing, ledger = L.decide_content(**self.base_args(base_pids={oid("f1")}, pr_pids={oid("f2"): 87}))
        assert landing == {"how": "commits", "pr": None}
        landing, _ = L.decide_content(**self.base_args(pr_pids={oid("f1"): 87, oid("f2"): 87}))
        assert landing == {"how": "commits", "pr": 87}
        assert ledger["found"] == 2 and ledger["missing"] == 0

    def test_a_missing_commit_is_the_ledger_with_missing_first(self):
        landing, ledger = L.decide_content(**self.base_args(base_pids={oid("f2")}, titles={"one": 12}))
        assert landing is None
        assert ledger["found"] == 1 and ledger["missing"] == 1
        assert ledger["commits"][0] == {
            "sha": oid("c1")[:9],
            "subject": "one",
            "found": False,
            "hint": {"pr": 12, "why": "title"},
        }
        assert ledger["commits"][1]["found"] is True and ledger["commits"][1]["hint"] is None

    def test_a_title_hint_never_makes_a_commit_found(self):
        landing, ledger = L.decide_content(**self.base_args(titles={"one": 12, "two": 13}))
        assert landing is None and ledger["missing"] == 2

    def test_the_ledger_is_capped(self):
        many = commits(*[(oid(f"c{i:02x}"), f"s{i}") for i in range(10)])
        _landing, ledger = L.decide_content(**self.base_args(commits=many, commit_pids={}))
        assert ledger["missing"] == 10 and len(ledger["commits"]) == L.LEDGER_MAX_COMMITS

    def test_unhashable_matches_nothing_not_even_itself(self):
        landing, ledger = L.decide_content(
            **self.base_args(
                commit_pids={oid("c1"): L.UNHASHABLE, oid("c2"): L.UNHASHABLE},
                cumulative_pid=L.UNHASHABLE,
                base_pids={L.UNHASHABLE},
            )
        )
        assert landing is None and ledger["missing"] == 2

    def test_an_empty_cumulative_diff_has_nothing_to_lose(self):
        landing, _ = L.decide_content(**self.base_args(cumulative_pid=None, cumulative_empty=True))
        assert landing == {"how": "commits", "pr": None}

    def test_an_unmeasured_commit_is_missing(self):
        landing, ledger = L.decide_content(**self.base_args(commit_pids={oid("c1"): oid("f1")}, base_pids={oid("f1")}))
        assert landing is None and ledger["missing"] == 1

    def test_attach_squash_pr_reads_the_matched_commits_subject(self):
        landing = L.attach_squash_pr(
            {"how": "squash", "pr": None}, oid("c0de"), {oid("c0de"): oid("b1")}, {oid("b1"): "Fix the retry loop (#34)"}
        )
        assert landing == {"how": "squash", "pr": 34}


def cand(branch, tip, committed_at=1, has_worktree=False):
    return {"branch": branch, "oid": tip, "committed_at": committed_at, "has_worktree": has_worktree}


class TestContainment:
    def test_agent_branches_inside_one_integration_branch(self):
        rows = [cand("feat/basic-auth-hardening", oid("b7")), *[cand(f"agent-{i}", oid(f"a{i}")) for i in range(4)]]
        holders = {f"agent-{i}": {f"agent-{i}", "feat/basic-auth-hardening"} for i in range(4)}
        holders["feat/basic-auth-hardening"] = {"feat/basic-auth-hardening"}
        assert L.containment(rows, holders) == {f"agent-{i}": "feat/basic-auth-hardening" for i in range(4)}

    def test_a_chain_names_the_top_and_keeps_it(self):
        rows = [cand("a", oid("a1")), cand("b", oid("b1")), cand("c", oid("c1"))]
        holders = {"a": {"a", "b", "c"}, "b": {"b", "c"}, "c": {"c"}}
        assert L.containment(rows, holders) == {"a": "c", "b": "c"}

    def test_equal_tips_keep_exactly_one(self):
        rows = [cand("x", oid("e1"), has_worktree=False), cand("y", oid("e1"), has_worktree=True)]
        holders = {"x": {"x", "y"}, "y": {"x", "y"}}
        assert L.containment(rows, holders) == {"x": "y"}

    def test_a_holder_that_is_not_a_candidate_holds_nothing(self):
        # A landed branch (or the base) is not a candidate: being inside it is
        # the PR / ancestor rungs' question, not containment's.
        rows = [cand("x", oid("e1"))]
        assert L.containment(rows, {"x": {"x", "landed-branch", "main"}}) == {}

    def test_no_holders_contains_nothing(self):
        assert L.containment([cand("x", oid("e1"))], {}) == {}


class TestDecideOutsideRemove:
    CLEAR = dict(
        name_error=None,
        held_path="/private/tmp/t",
        is_main=False,
        managed=False,
        creating=False,
        locked=False,
        landed=True,
        contained=False,
        dirty=False,
        occupants=[],
    )

    def decide(self, **over):
        return L.decide_outside_remove(**{**self.CLEAR, **over})

    def test_clear(self):
        assert self.decide() is None
        assert self.decide(landed=False, contained=True) is None

    @pytest.mark.parametrize(
        "over, status, words",
        [
            ({"name_error": "bad"}, 400, "bad"),
            ({"held_path": None}, 404, "no worktree"),
            ({"is_main": True}, 409, "main checkout"),
            ({"managed": True}, 409, "REMOVE WORKTREE"),
            ({"creating": True}, 409, "being created"),
            ({"locked": True}, 409, "locked"),
            ({"landed": False}, 409, "not in main"),
            ({"dirty": None}, 409, "could not read"),
            ({"dirty": True}, 409, "uncommitted"),
            ({"occupants": None}, 409, "could not tell"),
            ({"occupants": ["zsh"]}, 409, "zsh is running"),
        ],
    )
    def test_every_refusal(self, over, status, words):
        got = self.decide(**over)
        assert got is not None and got[0] == status and words in got[1]["error"]

    def test_the_main_checkout_refuses_before_anything_else_about_it(self):
        got = self.decide(is_main=True, landed=False, dirty=True)
        assert "main checkout" in got[1]["error"]


class TestPatchIdStore:
    def test_round_trip_and_the_empty_diff_is_an_answer(self, tmp_path):
        path = tmp_path / "pids.json"
        L.PatchIdStore(path).add(commits={oid("c1"): oid("f1")}, diffs={f"{MB}:{oid('7a1')}": ""})
        again = L.PatchIdStore(path)
        assert again.commits() == {oid("c1"): oid("f1")}
        assert again.diff(MB, oid("7a1")) == ""
        assert again.diff(MB, oid("7a2")) is None

    def test_a_wrong_version_is_ignored(self, tmp_path):
        path = tmp_path / "pids.json"
        path.write_text(json.dumps({"version": 99, "commits": {oid("c1"): oid("f1")}}))
        assert L.PatchIdStore(path).commits() == {}


class Script:
    """A `run_bounded` stand-in: the first prefix that matches answers."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, command, timeout, **kwargs):
        self.calls.append((command, kwargs.get("input")))
        for prefix, answer in self.answers:
            if tuple(command[: len(prefix)]) == prefix:
                return answer(command, kwargs.get("input")) if callable(answer) else answer
        raise AssertionError(f"unscripted subprocess: {command}")


REPO = "/repo"
SQUASH_TIP, LIVE_TIP, SHIPPED_TIP = oid("51"), oid("11"), oid("5e")


def rows():
    return [
        {
            "branch": "main",
            "is_base": True,
            "oid": BASE_TIP,
            "ahead": 0,
            "behind": 0,
            "landed": False,
            "merged": True,
            "committed_at": 1,
        },
        {
            "branch": "squashed",
            "is_base": False,
            "oid": SQUASH_TIP,
            "ahead": 2,
            "behind": 9,
            "landed": False,
            "merged": False,
            "committed_at": 1790000000,
        },
        {
            "branch": "live",
            "is_base": False,
            "oid": LIVE_TIP,
            "ahead": 1,
            "behind": 3,
            "landed": False,
            "merged": False,
            "committed_at": 1790000000,
        },
        {
            "branch": "shipped",
            "is_base": False,
            "oid": SHIPPED_TIP,
            "ahead": 1,
            "behind": 3,
            "landed": False,
            "merged": False,
            "committed_at": 1790000000,
        },
        {
            "branch": "ancestor",
            "is_base": False,
            "oid": oid("a7"),
            "ahead": 0,
            "behind": 5,
            "landed": True,
            "merged": True,
            "committed_at": 1,
        },
    ]


PRS = {
    "other": {"number": 34, "state": "merged", "base_ref": "main", "head_oid": oid("4ead"), "merged_at": "2026-10-07T00:00:00Z"}
}


def tier_script():
    base_log = "\n".join(
        [record(oid("bc1"), "Squashed work (#90)", [oid("bc2")]), record(oid("bc2"), "The live one's title (#12)", [MB])]
    )
    pr_log = "\n".join([record(oid("4ead"), "pr head", [SHIPPED_TIP]), record(SHIPPED_TIP, "shipped", [MB])])

    def log(command, _input):
        if "--no-merges" in command and command[-4] == SQUASH_TIP:
            return 0, "\n".join([record(oid("5c1"), "s1", [MB]), record(oid("5c2"), "s2", [MB])]), ""
        if "--no-merges" in command and command[-4] == LIVE_TIP:
            return 0, record(oid("1c1"), "The live one's title", [MB]), ""
        if "--no-merges" in command and command[-4] == SHIPPED_TIP:
            return 0, record(SHIPPED_TIP, "shipped", [MB]), ""
        if command[-4:] == [BASE_TIP, "--not", MB, "--"]:
            return 0, base_log, ""
        if command[-4:] == [oid("4ead"), "--not", BASE_TIP, "--"]:
            return 0, pr_log, ""
        raise AssertionError(f"unexpected log {command}")

    def patch_ids(_command, given):
        # Each "patch" is the list of shas fed to `log -p`; hash them by rule:
        # the squash's cumulative diff equals base commit bc1's patch.
        if given.startswith("DIFF:"):
            return 0, f"{oid('cafe') if 'squashed' in given else oid('dead')} {'0' * 40}\n", ""
        lines = [line.split()[1] for line in given.splitlines() if line.startswith("commit ")]
        table = {
            oid("bc1"): oid("cafe"),
            oid("bc2"): oid("beef"),
            oid("5c1"): oid("e1"),
            oid("5c2"): oid("e2"),
            oid("1c1"): oid("e3"),
            SHIPPED_TIP: oid("e4"),
        }
        return 0, "".join(f"{table[sha]} {sha}\n" for sha in lines if sha in table), ""

    def patch_log(_command, given):
        return 0, "".join(f"commit {sha}\n" for sha in given.split()), ""

    def diff(command, _input):
        return 0, "DIFF:" + ("squashed" if command[-2] == SQUASH_TIP else "other"), ""

    return Script(
        [
            # The fixture's one ancestor ("ancestor") is on the base's line: no merge brought it in.
            (("git", "-C", REPO, "rev-list"), (0, "", "")),
            (("git", "-C", REPO, "merge-base"), (0, MB + "\n", "")),
            (("git", "-C", REPO, "cat-file", "--batch-check"), (0, "", "")),
            (("git", "-C", REPO, "log", "-p"), patch_log),
            (("git", "-C", REPO, "log"), log),
            (("git", "-C", REPO, "patch-id"), patch_ids),
            (("git", "-C", REPO, "diff"), diff),
        ]
    )


class TestContentTier:
    def tier(self, tmp_path, script):
        started = []
        tier = L.ContentTier(REPO, L.PatchIdStore(tmp_path / "pids.json"), runner=script, start_thread=started.append)
        return tier, started

    def test_judge_forks_nothing_marks_pending_and_schedules_one_pass(self, tmp_path):
        script = tier_script()
        tier, started = self.tier(tmp_path, script)
        table = rows()
        tier.judge(table, base=BASE, prs=PRS)
        assert script.calls == [] and len(started) == 1
        by = {r["branch"]: r for r in table}
        assert by["squashed"]["content"] == "pending" and by["squashed"]["landed"] is False
        assert by["ancestor"]["content"] == "skipped"
        assert by["ancestor"]["landing"] == {"how": "ancestor", "pr": None}
        assert by["main"]["content"] == "skipped"
        tier.judge(rows(), base=BASE, prs=PRS)
        assert len(started) == 1  # single flight

    @pytest.mark.parametrize("head, number", [(oid("a7"), 88), (oid("b8"), None)])
    def test_an_ancestor_names_only_the_pr_that_shipped_its_tip(self, tmp_path, head, number):
        # A branch cut from main and never committed to is an ancestor too; the
        # PR number is what tells it from one whose PR merged (`work_landed`).
        script = tier_script()
        tier, _started = self.tier(tmp_path, script)
        table = rows()
        by = {r["branch"]: r for r in table}
        by["ancestor"]["pr"] = {
            "number": 88,
            "state": "merged",
            "base_ref": BASE,
            "head_oid": head,
            "merged_at": "2026-10-07T00:00:00Z",
        }
        tier.judge(table, base=BASE, prs=PRS)
        assert by["ancestor"]["landing"] == {"how": "ancestor", "pr": number}
        assert script.calls == [], "exact head only — never a fork"

    def test_work_landed_needs_the_pr_on_an_ancestor_only(self):
        assert L.work_landed({"how": "ancestor", "pr": 88}) is True
        assert L.work_landed({"how": "ancestor", "pr": None}) is False
        for how in ("pr", "squash", "commits"):
            assert L.work_landed({"how": how, "pr": None}) is True
        assert L.work_landed(None) is False

    def test_after_one_pass_every_proof_lands(self, tmp_path):
        script = tier_script()
        tier, started = self.tier(tmp_path, script)
        tier.judge(rows(), base=BASE, prs=PRS)
        started.pop()()
        assert tier.last_error is None

        table = rows()
        calls = len(script.calls)
        tier.judge(table, base=BASE, prs=PRS)
        assert len(script.calls) == calls  # judging is still fork-free
        by = {r["branch"]: r for r in table}
        assert by["squashed"]["landed"] is True
        assert by["squashed"]["landing"] == {"how": "squash", "pr": 90}
        assert by["shipped"]["landing"] == {"how": "pr", "pr": 34}
        live = by["live"]
        assert live["landed"] is False and live["content"] == "measured"
        assert live["ledger"]["missing"] == 1
        assert live["ledger"]["commits"][0]["hint"] == {"pr": 12, "why": "title"}
        assert started == []  # nothing missing → nothing scheduled

    def test_the_cache_survives_a_restart(self, tmp_path):
        script = tier_script()
        tier, started = self.tier(tmp_path, script)
        tier.judge(rows(), base=BASE, prs=PRS)
        started.pop()()
        again = tier_script()
        fresh, started2 = self.tier(tmp_path, again)
        fresh.judge(rows(), base=BASE, prs=PRS)
        started2.pop()()
        # The second process re-reads refs, never re-hashes a commit or a diff.
        assert not [c for c, _i in again.calls if c[3:5] in (["log", "-p"], ["diff", "--no-renames"])]

    def test_a_failed_pass_backs_off(self, tmp_path):
        clock = [100.0]
        started = []
        tier = L.ContentTier(
            REPO,
            L.PatchIdStore(tmp_path / "p.json"),
            runner=lambda *_a, **_k: (128, "", "fatal"),
            start_thread=started.append,
            clock=lambda: clock[0],
        )
        tier.judge(rows(), base=BASE, prs=PRS)
        started.pop()()
        assert tier.last_error
        tier.judge(rows(), base=BASE, prs=PRS)
        assert started == []
        clock[0] += L.CONTENT_BACKOFF_SECONDS
        tier.judge(rows(), base=BASE, prs=PRS)
        assert len(started) == 1

    def test_an_orphan_branch_does_not_stall_the_others(self, tmp_path):
        script = tier_script()
        script.answers.insert(0, (("git", "-C", REPO, "merge-base", BASE_TIP, LIVE_TIP), (1, "", "")))
        tier, started = self.tier(tmp_path, script)
        tier.judge(rows(), base=BASE, prs=PRS)
        started.pop()()
        table = rows()
        tier.judge(table, base=BASE, prs=PRS)
        by = {r["branch"]: r for r in table}
        assert by["squashed"]["landed"] is True
        assert by["live"]["content"] == "measured" and by["live"]["ledger"]["missing"] == 1


class TestIntroducedBy:
    """Which parent carried a tip into the base's first-parent line. Shapes: `C` the base tip, `M` a merge on
    the line, `B` the base before it, `H` a PR head, `T` the tip."""

    T, H, M, B, C, D = oid("7"), oid("4"), oid("3e"), oid("b1"), oid("c1"), oid("d1")

    def test_a_tip_merged_inside_another_branchs_pr_names_that_prs_head(self):
        # T → H (a combined branch took T), merged into the base as M = (B, H), then C on top.
        graph = {self.C: [self.M], self.M: [self.B, self.H], self.H: [self.T]}
        assert L.introduced_by(self.T, self.C, graph) == self.H

    def test_a_tip_merged_by_its_own_merge_commit_names_itself(self):
        assert L.introduced_by(self.T, self.C, {self.C: [self.M], self.M: [self.B, self.T]}) == self.T

    def test_a_tip_on_the_line_names_nothing(self):
        # A branch cut from the base and never committed to: C → D → T on the first-parent line.
        assert L.introduced_by(self.T, self.C, {self.C: [self.D], self.D: [self.T]}) == ""
        # ... even when a merge sits above it: its first parent still descends from T.
        graph = {self.C: [self.M], self.M: [self.D, self.H], self.D: [self.T]}
        assert L.introduced_by(self.T, self.C, graph) == ""

    def test_the_base_tip_itself_and_an_unreadable_graph_name_nothing(self):
        assert L.introduced_by(self.C, self.C, {}) == ""
        assert L.introduced_by(self.T, self.C, {}) == ""

    def test_the_parser_keeps_first_parents_first_and_drops_junk(self):
        text = f"{self.C} {self.M}\n{self.M} {self.B} {self.H}\nnot an oid\n\n"
        assert L.parse_parent_lines(text) == {self.C: [self.M], self.M: [self.B, self.H]}
        assert L.parse_parent_lines(None) == {}

    def test_the_command_walks_from_the_tip_to_the_base_tip(self):
        command = L.build_ancestry_path_command(REPO, self.T, self.C)
        assert command[3:6] == ["rev-list", "--parents", "--ancestry-path"] and f"{self.T}..{self.C}" in command


class TestAncestorNamedByItsMerge:
    """The rule of 2026-11-14, over invented shapes: lanes merged into a combined branch whose PR merged
    with a merge commit. Pulling that merge made every lane an ancestor, and nothing named the PR any more."""

    LANE, FRESH, OLD = oid("1a"), oid("f5"), oid("01d")
    HEAD, MERGE, BEFORE = oid("4ead"), oid("3e"), oid("b1")
    BUILD = {"number": 41, "state": "merged", "base_ref": BASE, "head_oid": HEAD, "merged_at": "2026-11-14T00:00:00Z"}

    def rows(self, **lane):
        def ancestor(branch, tip, behind, **extra):
            return {
                "branch": branch,
                "is_base": False,
                "oid": tip,
                "ahead": 0,
                "behind": behind,
                "landed": True,
                "merged": True,
                "committed_at": 1790000000,
                **extra,
            }

        return [
            {
                "branch": "main",
                "is_base": True,
                "oid": BASE_TIP,
                "ahead": 0,
                "behind": 0,
                "landed": False,
                "merged": True,
                "committed_at": 1,
            },
            ancestor("lane", self.LANE, 4, **lane),
            ancestor("fresh", self.FRESH, 2),
            ancestor("old", self.OLD, L.ANCESTRY_PATH_MAX_BEHIND + 1),
        ]

    def script(self):
        graphs = {
            self.LANE: f"{BASE_TIP} {self.MERGE}\n{self.MERGE} {self.BEFORE} {self.HEAD}\n{self.HEAD} {self.LANE}\n",
            self.FRESH: f"{BASE_TIP} {self.FRESH}\n",
        }

        def rev_list(command, _input):
            return 0, graphs[command[-2].split("..")[0]], ""

        return Script([(("git", "-C", REPO, "rev-list"), rev_list)])

    def judged(self, tmp_path, prs, **lane):
        script, started = self.script(), []
        tier = L.ContentTier(REPO, L.PatchIdStore(tmp_path / "pids.json"), runner=script, start_thread=started.append)
        tier.judge(self.rows(**lane), base=BASE, prs=prs)
        assert script.calls == [] and len(started) == 1, "judging never forks; one pass walks"
        started.pop()()
        assert tier.last_error is None
        table = self.rows(**lane)
        tier.judge(table, base=BASE, prs=prs)
        assert started == [], "every walk is cached by tip"
        self.tier = tier
        return {r["branch"]: r for r in table}, script

    def test_the_lane_names_the_pr_that_merged_it_and_a_fresh_branch_names_none(self, tmp_path):
        by, script = self.judged(tmp_path, {"feat/build": self.BUILD})
        assert by["lane"]["landing"] == {"how": "ancestor", "pr": 41}
        assert L.work_landed(by["lane"]["landing"]) is True
        assert by["fresh"]["landing"] == {"how": "ancestor", "pr": None}
        assert L.work_landed(by["fresh"]["landing"]) is False
        assert by["old"]["landing"] == {"how": "ancestor", "pr": None}
        walked = [command[-2].split("..")[0] for command, _input in script.calls]
        assert sorted(walked) == sorted([self.LANE, self.FRESH]), "too far behind is never walked"

    def test_its_own_pr_still_wins(self, tmp_path):
        own = {"number": 12, "state": "merged", "base_ref": BASE, "head_oid": self.LANE, "merged_at": "2026-11-01T00:00:00Z"}
        script, started = self.script(), []
        tier = L.ContentTier(REPO, L.PatchIdStore(tmp_path / "pids.json"), runner=script, start_thread=started.append)
        table = self.rows(pr=own)
        tier.judge(table, base=BASE, prs={"feat/build": self.BUILD})
        assert {r["branch"]: r for r in table}["lane"]["landing"] == {"how": "ancestor", "pr": 12}

    @pytest.mark.parametrize(
        "pr",
        [
            {"base_ref": "release"},  # merged into another branch, not the base
            {"merged_at": "2026-09-01T00:00:00Z"},  # the tip was committed after that merge
            {"state": "closed"},
            {"head_oid": oid("0ther")},  # the merge's other parent is no PR's head
        ],
    )
    def test_anything_short_of_a_pr_merged_into_the_base_after_the_tip_names_nothing(self, tmp_path, pr):
        by, _script = self.judged(tmp_path, {"feat/build": {**self.BUILD, **pr}})
        assert by["lane"]["landing"] == {"how": "ancestor", "pr": None}

    def test_a_fresh_pr_index_names_it_without_walking_again(self, tmp_path):
        by, script = self.judged(tmp_path, {})
        assert by["lane"]["landing"] == {"how": "ancestor", "pr": None}
        calls = len(script.calls)
        table = self.rows()
        self.tier.judge(table, base=BASE, prs={"feat/build": self.BUILD})
        assert {r["branch"]: r for r in table}["lane"]["landing"] == {"how": "ancestor", "pr": 41}
        assert len(script.calls) == calls


class TestBuilders:
    def test_the_fetch_never_writes_fetch_head(self):
        command = L.build_fetch_heads_command(REPO, "origin", [oid("a1")])
        assert "--no-write-fetch-head" in command and command[-2:] == ["origin", oid("a1")]

    def test_both_sides_of_a_patch_id_use_the_same_diff_options(self):
        log = L.build_patch_log_command(REPO)
        diff = L.build_diff_command(REPO, MB, oid("7a1"))
        for option in L.DIFF_OPTIONS:
            assert option in log and option in diff
