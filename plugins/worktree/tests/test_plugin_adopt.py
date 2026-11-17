"""`adopt.py` and the three routes it backs: `GET /adopt-plan`, `POST /adopt`
and `POST /catch-up`.

Two halves, deliberately.

The PURE half runs the parsers against bytes in the shape `git` really emits
(`MERGE_TREE_CONFLICTED`, `DELTA_Z`) rather than against hand-written strings.
A parser test whose fixture was typed by the same person who typed the parser
proves that two guesses agree; these came off `git merge-tree --write-tree -z`
and `git diff --raw -z` on the case that motivated the feature — a worktree
editing files that `main` had deleted.

The ROUTE half fences `plan_adopt`, because the refusal ladder is what those
tests are about. Every refusal asserts `rec.calls == []`: a route that refuses
must not have forked, which is the property that makes "refuses rather than
confirms" a real safety argument rather than a wording choice.
"""

import pytest

from test_branches import FIXTURE, INTENT, MANAGED, REPO
from test_plugin_harbor import Recorder, plugin  # noqa: F401  (the fixture)
from worktree import adopt as adopt_mod
from worktree import branches as branches_mod
from worktree.store import AdoptStore, parse_adopt_file

# Invented, in the shape of `git merge-tree --write-tree -z --name-only
# --merge-base <wt> <main> <snapshot>` where main DELETED both files the
# worktree modified. NUL-separated: tree, conflicted paths, an empty separator,
# then `<count>`, the paths, the type, and the message.
MERGED_TREE = "0f1e2d3c4b5a69788796a5b4c3d2e1f00f1e2d3c"
NOTES_A = "docs/notes/alpha-notes.html"
NOTES_B = "docs/notes/beta-notes.html"
NOTES_C_HTML = "docs/notes/gamma-notes.html"
NOTES_C_MD = "docs/notes/gamma-notes.md"

MERGE_TREE_CONFLICTED = "\0".join(
    [
        MERGED_TREE,
        NOTES_A,
        NOTES_B,
        "",
        "1",
        NOTES_A,
        "CONFLICT (modify/delete)",
        f"CONFLICT (modify/delete): {NOTES_A} deleted in a1b2c3d06 and modified in b0b0b0b01.",
        "1",
        NOTES_B,
        "CONFLICT (modify/delete)",
        f"CONFLICT (modify/delete): {NOTES_B} deleted in a1b2c3d06 and modified in b0b0b0b01.",
        "",
    ]
)

MERGE_TREE_CLEAN = f"{MERGED_TREE}\0"

DELTA_Z = "".join(f":000000 100644 000000000 a1b2c3d09 A\0{path}\0" for path in (NOTES_A, NOTES_B, NOTES_C_HTML, NOTES_C_MD))


class TestParseMergeTree:
    def test_clean_output_is_a_tree_and_no_conflicts(self):
        parsed = adopt_mod.parse_merge_tree(MERGE_TREE_CLEAN)
        assert parsed == {"tree": MERGED_TREE, "conflicts": {}}

    def test_conflicted_output_carries_the_TYPE_not_just_the_path(self):
        parsed = adopt_mod.parse_merge_tree(MERGE_TREE_CONFLICTED)
        assert parsed["tree"] == MERGED_TREE
        assert parsed["conflicts"] == {NOTES_A: "modify/delete", NOTES_B: "modify/delete"}

    def test_unparseable_info_section_still_reports_the_paths(self):
        """Degrading to `unknown` is the SAFE direction: the verdict table
        treats an unknown type as a real conflict, so a parser that loses its
        footing asks for a force rather than skipping one."""
        truncated = "\0".join([MERGED_TREE, NOTES_A, "", "not-a-number"])
        parsed = adopt_mod.parse_merge_tree(truncated)
        assert parsed["conflicts"] == {NOTES_A: "unknown"}

    @pytest.mark.parametrize(
        "text,expected",
        [
            ("CONFLICT (modify/delete)", "modify/delete"),
            ("CONFLICT (content)", "content"),
            ("CONFLICT (rename/rename)", "rename/rename"),
            ("no parens here", "unknown"),
            ("", "unknown"),
            (None, "unknown"),
        ],
    )
    def test_conflict_type_of(self, text, expected):
        assert adopt_mod.conflict_type_of(text) == expected

    def test_empty_input_is_not_an_exception(self):
        assert adopt_mod.parse_merge_tree("") == {"tree": None, "conflicts": {}}
        assert adopt_mod.parse_merge_tree(None) == {"tree": None, "conflicts": {}}


class TestParseDelta:
    def test_real_diff_raw_z(self):
        rows = adopt_mod.parse_delta(DELTA_Z)
        assert [row["path"] for row in rows] == [NOTES_A, NOTES_B, NOTES_C_HTML, NOTES_C_MD]
        assert {row["status"] for row in rows} == {"A"}
        assert rows[0]["mode"] == "100644"

    def test_garbage_records_are_skipped_not_guessed_at(self):
        rows = adopt_mod.parse_delta(f"not-a-record\0:000000 100644 0 1 M\0{NOTES_C_MD}\0")
        assert [row["path"] for row in rows] == [NOTES_C_MD]

    def test_empty(self):
        assert adopt_mod.parse_delta("") == []


class TestVerdicts:
    """The table the whole feature turns on."""

    def plan(self, delta=None, conflicts=None, dirty=()):
        return adopt_mod.build_plan(delta if delta is not None else adopt_mod.parse_delta(DELTA_Z), conflicts or {}, set(dirty))

    def verdicts(self, **kwargs):
        return {row["path"]: row["verdict"] for row in self.plan(**kwargs)["files"]}

    def test_modify_delete_is_READD_and_needs_no_force(self):
        """The measured case. git keeps the modified version and writes NO
        conflict markers, so calling this a conflict would put a `force` in
        front of the one outcome the user actually wants."""
        verdicts = self.verdicts(conflicts={NOTES_A: "modify/delete"})
        assert verdicts[NOTES_A] == "readd"
        assert self.plan(conflicts={NOTES_A: "modify/delete"})["clean"] is True

    def test_content_conflict_is_a_conflict(self):
        plan = self.plan(conflicts={NOTES_A: "content"})
        assert plan["conflicts"] == [NOTES_A]
        assert plan["clean"] is False
        assert plan["adoptable"] is True  # reachable, but only with force

    def test_an_UNKNOWN_conflict_type_is_treated_as_a_real_one(self):
        assert self.verdicts(conflicts={NOTES_A: "who-knows"})[NOTES_A] == "conflict"

    def test_blocked_outranks_every_other_verdict(self):
        """A path the target has uncommitted edits on must not be written
        whatever the merge thought of it — the target's copy has nowhere else
        to live, while the source's is still in its worktree."""
        verdicts = self.verdicts(conflicts={NOTES_A: "modify/delete"}, dirty=[NOTES_A, NOTES_C_MD])
        assert verdicts[NOTES_A] == "blocked"
        assert verdicts[NOTES_C_MD] == "blocked"

    def test_blocked_makes_the_plan_unadoptable(self):
        plan = self.plan(dirty=[NOTES_C_MD])
        assert plan["adoptable"] is False
        assert plan["blocked"] == [NOTES_C_MD]

    def test_status_letters_map_to_verdicts(self):
        delta = [
            {"path": "a", "status": "A", "mode": "100644"},
            {"path": "b", "status": "M", "mode": "100644"},
            {"path": "c", "status": "D", "mode": "100644"},
            {"path": "d", "status": "T", "mode": "120000"},
        ]
        assert self.verdicts(delta=delta) == {"a": "new", "b": "merge", "c": "delete", "d": "merge"}

    def test_files_are_sorted_so_the_list_does_not_reshuffle_between_polls(self):
        paths = [row["path"] for row in self.plan()["files"]]
        assert paths == sorted(paths)

    def test_an_empty_delta_is_not_adoptable(self):
        plan = self.plan(delta=[])
        assert plan["adoptable"] is False and plan["total"] == 0


class TestSummarize:
    def test_the_measured_case(self):
        plan = adopt_mod.build_plan(adopt_mod.parse_delta(DELTA_Z), {NOTES_A: "modify/delete", NOTES_B: "modify/delete"}, set())
        assert adopt_mod.summarize_plan(plan) == "4 FILES · 2 NEW · 2 RE-ADD"

    def test_an_ordinary_merge_gets_no_term_of_its_own(self):
        """`merge` is the expected case; naming it would print a word on every
        row that distinguishes nothing. The terms exist for what is UNUSUAL —
        a new file, a re-add, a conflict, a block."""
        plan = adopt_mod.build_plan([{"path": "a", "status": "M", "mode": "100644"}], {}, set())
        assert adopt_mod.summarize_plan(plan) == "1 FILE"

    def test_blocked_and_conflict_terms_appear(self):
        delta = [{"path": "a", "status": "M", "mode": "100644"}, {"path": "b", "status": "M", "mode": "100644"}]
        plan = adopt_mod.build_plan(delta, {"a": "content"}, {"b"})
        assert adopt_mod.summarize_plan(plan) == "2 FILES · 1 CONFLICT · 1 BLOCKED"

    def test_nothing(self):
        assert adopt_mod.summarize_plan(adopt_mod.build_plan([], {}, set())) == "NOTHING TO ADOPT"


class TestArgvBuilders:
    """Nothing goes through a shell, so these are the security surface."""

    def test_checkout_index_fences_the_path_list(self):
        command = adopt_mod.build_checkout_index_command(REPO, ["--not-an-option"])
        assert command[-2:] == ["--", "--not-an-option"]

    def test_merge_tree_asks_for_z_and_an_explicit_merge_base(self):
        command = adopt_mod.build_merge_tree_command(REPO, "base", "ours", "theirs")
        assert command == [
            "git",
            "-C",
            REPO,
            "merge-tree",
            "--write-tree",
            "-z",
            "--name-only",
            "--merge-base",
            "base",
            "ours",
            "theirs",
        ]

    def test_delta_disables_rename_detection(self):
        assert "--no-renames" in adopt_mod.build_delta_command(REPO, "a", "b")

    def test_ff_merge_is_ff_only_and_autostash_is_opt_in(self):
        assert adopt_mod.build_ff_merge_command(REPO, "main", autostash=False) == [
            "git",
            "-C",
            REPO,
            "merge",
            "--ff-only",
            "main",
        ]
        assert "--autostash" in adopt_mod.build_ff_merge_command(REPO, "main", autostash=True)

    def test_the_anchor_ref_namespace_cannot_collide_with_a_branch(self):
        assert adopt_mod.adopt_ref_for("feature/x") == "refs/tldrack/adopted/feature/x"
        assert not adopt_mod.adopt_ref_for("x").startswith("refs/heads/")


class TestDecideCatchUp:
    """A pure table, so the route and the row verdict cannot drift."""

    def decide(self, **kwargs):
        args = {
            "known": True,
            "is_base": False,
            "creating": False,
            "behind": 5,
            "ahead": 0,
            "base_behind_remote": 0,
            "base": "main",
        }
        args.update(kwargs)
        return adopt_mod.decide_catch_up(**args)

    def test_a_clean_fast_forward_is_allowed(self):
        assert self.decide() is None

    def test_bad_name(self):
        assert self.decide(name_error="branch names may not start with '-'")[0] == 400

    def test_unknown_branch(self):
        assert self.decide(known=False)[0] == 404

    def test_the_base_is_what_others_catch_up_TO(self):
        status, body = self.decide(is_base=True)
        assert status == 409 and "base" in body["error"]

    def test_mid_create(self):
        assert self.decide(creating=True)[0] == 409

    def test_a_stale_base_is_refused_before_anything_else_is_measured(self):
        status, body = self.decide(base_behind_remote=3)
        assert status == 409 and "FETCH first" in body["error"]

    def test_unknown_behind_is_refused_never_assumed_zero(self):
        assert self.decide(behind=None)[0] == 409

    def test_already_up_to_date(self):
        status, body = self.decide(behind=0)
        assert status == 409 and "already up to date" in body["error"]

    def test_commits_of_its_own_refuse_with_needs_rebase(self):
        """The safety argument: a fast-forward cannot lose or invent a commit,
        and refusing is what lets this verb fire without a confirmation."""
        status, body = self.decide(ahead=8)
        assert status == 409
        assert body["needs_rebase"] is True and body["ahead"] == 8
        assert "rebase by hand" in body["error"]

    def test_one_commit_reads_singular(self):
        assert "1 commit is not on main" in self.decide(ahead=1)[1]["error"]

    def test_dirty_conflicts_point_at_the_other_verb(self):
        status, body = adopt_mod.refuse_dirty_conflicts([NOTES_A], 49)
        assert status == 409
        assert body["needs_adopt"] is True
        assert "ADOPT them first" in body["error"]


class TestAdoptStore:
    def test_round_trip(self, tmp_path):
        store = AdoptStore(tmp_path / "adopted.json")
        store.set("wt", tree="t1", commit="c1", ref="refs/voidcraft/adopted/wt", files=["a", "b"])
        assert AdoptStore(tmp_path / "adopted.json").get("wt")["tree"] == "t1"

    def test_a_row_with_no_tree_is_dropped_because_it_can_answer_nothing(self):
        rows = parse_adopt_file('{"version": 1, "adopted": {"a": {"files": []}}}')
        assert rows == {}

    def test_one_bad_row_does_not_cost_the_others(self):
        rows = parse_adopt_file('{"version": 1, "adopted": {"a": {"tree": "t"}, "b": 5}}')
        assert list(rows) == ["a"]

    def test_a_wrong_envelope_raises_so_the_caller_can_warn(self):
        with pytest.raises(ValueError):
            parse_adopt_file('{"version": 1}')

    def test_drop(self, tmp_path):
        store = AdoptStore(tmp_path / "adopted.json")
        store.set("wt", tree="t", commit=None, ref=None, files=[])
        store.drop("wt")
        assert store.get("wt") is None


# ── Routes ─────────────────────────────────────────────────────────────────


def fence_plan(monkeypatch, plan=None, error=None):
    """Answer `plan_adopt` from a fixture. These tests are about the refusal
    ladder; the plan itself is covered above, against real git bytes."""
    monkeypatch.setattr("worktree.adopt.plan_adopt", lambda *_a, **_kw: (plan, error))


def canned(files, **extra):
    plan = adopt_mod.build_plan(files, {}, set())
    plan.update(
        {
            "merged_tree": MERGED_TREE,
            "snapshot_commit": "c0",
            "snapshot_tree": "t0",
            "worktree_head": "w0",
            "target_head": "m0",
            "conflict_paths": [],
            "summary": adopt_mod.summarize_plan(plan),
        }
    )
    plan.update(extra)
    return plan


ONE_NEW = [{"path": NOTES_C_MD, "status": "A", "mode": "100644"}]


class TestAdoptPlanRoute:
    def get(self, plugin, branch=None):
        query = {"branch": [branch]} if branch is not None else {}
        return plugin.handle_get("/adopt-plan", query)

    def test_a_managed_worktree_answers_a_plan(self, plugin, monkeypatch):
        fence_plan(monkeypatch, canned(ONE_NEW))
        status, body = self.get(plugin, "pr-demo")
        assert status == 200
        assert body["branch"] == "pr-demo" and body["target"] == "main"
        assert body["summary"] == "1 FILE · 1 NEW" and body["adoptable"] is True

    def test_an_UNMANAGED_worktree_may_also_be_read(self, plugin, monkeypatch):
        """Reading a tree and copying out of it destroys nothing, and the trees
        Claude Code owns hold work worth rescuing. The WRITE gate is unchanged."""
        fence_plan(monkeypatch, canned(ONE_NEW))
        status, body = self.get(plugin, "add-stopwatch-feed")
        assert status == 200 and body["managed"] is False

    def test_the_path_label_is_home_collapsed(self, plugin, monkeypatch):
        fence_plan(monkeypatch, canned(ONE_NEW))
        _status, body = self.get(plugin, "pr-demo")
        assert body["path_label"].startswith("~/")
        assert "/Users/" not in str(body)

    def test_missing_branch(self, plugin):
        assert self.get(plugin)[0] == 400

    def test_unknown_branch(self, plugin):
        assert self.get(plugin, "no-such-branch")[0] == 404

    def test_the_main_checkout_has_nothing_to_adopt_FROM(self, plugin):
        status, body = self.get(plugin, "main")
        assert status == 409 and "main checkout" in body["error"]

    def test_option_injection_is_refused_by_name_before_git_is_reached(self, plugin):
        status, body = self.get(plugin, "--upload-pack=pwn")
        assert status == 400 and "may not start with '-'" in body["error"]

    def test_a_failed_plan_is_a_502_not_a_traceback(self, plugin, monkeypatch):
        fence_plan(monkeypatch, None, "git exploded")
        assert self.get(plugin, "pr-demo")[0] == 502


class TestAdoptRoute:
    def post(self, plugin, monkeypatch, body, plan=None, error=None, git=None):
        fence_plan(monkeypatch, plan, error)
        recorder = Recorder(git or {})
        monkeypatch.setattr("worktree.create.run_bounded", recorder)
        return plugin.handle_post("/adopt", {}, body), recorder

    def test_nothing_uncommitted_is_a_refusal_not_a_no_op_success(self, plugin, monkeypatch):
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "pr-demo"}, plan=canned([]))
        assert status == 409 and "nothing uncommitted" in body["error"]
        assert rec.calls == []

    def test_a_blocked_path_refuses_and_names_it(self, plugin, monkeypatch):
        plan = canned(ONE_NEW)
        plan.update({"blocked": [NOTES_C_MD], "adoptable": False})
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "pr-demo"}, plan=plan)
        assert status == 409
        assert body["blocked"] == [NOTES_C_MD]
        assert "uncommitted edits here too" in body["error"]
        assert rec.calls == []

    def test_blocked_is_NOT_reachable_with_force(self, plugin, monkeypatch):
        """`force` is about conflict markers, never about overwriting work the
        target has not committed. There is no flag for that on purpose."""
        plan = canned(ONE_NEW)
        plan.update({"blocked": [NOTES_C_MD], "adoptable": False})
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "pr-demo", "force": True}, plan=plan)
        assert status == 409 and body["blocked"] == [NOTES_C_MD]
        assert "needs_force" not in body
        assert rec.calls == []

    def test_a_conflict_asks_for_force_in_the_shape_every_other_verb_uses(self, plugin, monkeypatch):
        plan = canned(ONE_NEW)
        plan["conflicts"] = [NOTES_A]
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "pr-demo"}, plan=plan)
        assert status == 409
        assert body["needs_force"] is True and body["conflicts"] == [NOTES_A]
        assert rec.calls == []

    def test_the_main_checkout_is_refused_as_a_SOURCE(self, plugin, monkeypatch):
        (status, _body), rec = self.post(plugin, monkeypatch, {"branch": "main"})
        assert status == 409 and rec.calls == []


class TestCatchUpRoute:
    def post(self, plugin, monkeypatch, body, git=None, dirty=False, plan=None):
        recorder = Recorder(git or {})
        monkeypatch.setattr("worktree.create.run_bounded", recorder)
        monkeypatch.setattr("worktree.adopt.worktree_is_dirty", lambda _p: dirty)
        if plan is not None:
            fence_plan(monkeypatch, plan)
        return plugin.handle_post("/catch-up", {}, body), recorder

    def test_commits_of_its_own_refuse_and_fork_NOTHING(self, plugin, monkeypatch):
        """`feat/cache-all-pages` is 8 ahead, 2 behind in the fixture."""
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "feat/cache-all-pages"})
        assert status == 409 and body["needs_rebase"] is True and body["ahead"] == 8
        assert rec.calls == []

    def test_the_base_is_refused(self, plugin, monkeypatch):
        (status, _body), rec = self.post(plugin, monkeypatch, {"branch": "main"})
        assert status == 409 and rec.calls == []

    def test_a_bad_name_never_reaches_git(self, plugin, monkeypatch):
        (status, _body), rec = self.post(plugin, monkeypatch, {"branch": "-x"})
        assert status == 400 and rec.calls == []

    def test_unknown_branch(self, plugin, monkeypatch):
        (status, _body), rec = self.post(plugin, monkeypatch, {"branch": "nope"})
        assert status == 404 and rec.calls == []

    def test_a_clean_worktree_fast_forwards_with_no_autostash(self, plugin, monkeypatch):
        (status, body), rec = self.post(
            plugin,
            monkeypatch,
            {"branch": "pr-demo"},
            git={("git", "-C", f"{MANAGED}/pr-demo", "merge", "--ff-only"): (0, "", "")},
        )
        assert status == 200
        assert body == {"branch": "pr-demo", "advanced": 22, "autostashed": False, "worktree": True}
        assert rec.calls[0][0] == ["git", "-C", f"{MANAGED}/pr-demo", "merge", "--ff-only", "main"]
        assert rec.calls[0][1] == adopt_mod.FF_TIMEOUT_SECONDS

    def test_a_dirty_worktree_autostashes_when_nothing_collides(self, plugin, monkeypatch):
        (status, body), rec = self.post(
            plugin,
            monkeypatch,
            {"branch": "pr-demo"},
            dirty=True,
            plan=canned(ONE_NEW),
            git={("git", "-C", f"{MANAGED}/pr-demo", "merge", "--ff-only"): (0, "", "")},
        )
        assert status == 200 and body["autostashed"] is True
        assert "--autostash" in rec.calls[0][0]

    def test_a_dirty_worktree_that_COLLIDES_refuses_and_names_ADOPT(self, plugin, monkeypatch):
        """`stash pop` stops on ANY conflict, so predicting is the only way a
        button can avoid leaving a worktree mid-conflict."""
        plan = canned(ONE_NEW)
        plan["conflict_paths"] = [NOTES_A, NOTES_B]
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "pr-demo"}, dirty=True, plan=plan)
        assert status == 409
        assert body["needs_adopt"] is True
        assert body["conflicts"] == [NOTES_A, NOTES_B]
        assert rec.calls == []

    def test_a_branch_with_no_checkout_advances_the_REF_alone(self, plugin, monkeypatch):
        """`test123` has no worktree. `fetch .` applies git's own
        fast-forward check to the ref update."""
        (status, body), rec = self.post(
            plugin, monkeypatch, {"branch": "test123"}, git={("git", "-C", REPO, "fetch"): (0, "", "")}
        )
        assert status == 200 and body["worktree"] is False and body["advanced"] == 217
        assert rec.calls[0][0] == ["git", "-C", REPO, "fetch", "--no-tags", ".", "main:test123"]
        assert rec.calls[0][1] == adopt_mod.FF_TIMEOUT_SECONDS

    def test_a_stale_base_refuses_before_touching_anything(self, plugin, monkeypatch):
        behind_base = FIXTURE.replace(
            "\x1forigin\x1frefs/heads/main\x1f\x1f0 0\x1f" + REPO, "\x1forigin\x1frefs/heads/main\x1f[behind 3]\x1f0 0\x1f" + REPO
        )
        monkeypatch.setattr(
            "worktree.branches.sweep_refs",
            lambda _root, base, **_kw: (
                branches_mod.parse_for_each_ref(behind_base, base, ahead_behind=True),
                {"ahead_behind": True},
                None,
            ),
        )
        (status, body), rec = self.post(plugin, monkeypatch, {"branch": "pr-demo"})
        assert status == 409 and "FETCH first" in body["error"]
        assert rec.calls == []

    def test_a_failed_fast_forward_is_a_500_with_gits_words_scrubbed(self, plugin, monkeypatch):
        (status, body), _rec = self.post(
            plugin,
            monkeypatch,
            {"branch": "pr-demo"},
            git={("git", "-C", f"{MANAGED}/pr-demo", "merge"): (1, "", f"fatal: refusing under {INTENT}/x")},
        )
        assert status == 500 and "/Users/" not in body["error"]
