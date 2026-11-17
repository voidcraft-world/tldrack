"""The evidence tier (`worktree/absorption.py`): the diff parser, the verdict,
and the tier end to end against a scripted runner — never a fork.

The shapes are invented to cover three cases: lanes absorbed through
`iteration` snapshots (nothing missing), a port that adds files main never
had, and a branch whose thousands of "missing" files are content-hashed bakes.
"""

from collections import Counter

from worktree import absorption as A


def oid(seed: str) -> str:
    return (seed + "0" * 40)[:40]


REPO = "/repo"
BASE_TIP = oid("ba5e")
TIP = oid("71b")
MB = oid("b0b0")

CODE = "const absorbed_work = measure_it(branch);"
OTHER = "export function another_long_line() {"


def diff(path: str, added=(), removed=(), new=False, deleted=False) -> str:
    old = "/dev/null" if new else f"a/{path}"
    new_path = "/dev/null" if deleted else f"b/{path}"
    body = [f"diff --git a/{path} b/{path}"]
    if new:
        body.append("new file mode 100644")
    if deleted:
        body.append("deleted file mode 100644")
    body += [f"--- {old}", f"+++ {new_path}", "@@ -0,0 +1 @@"]
    body += [f"-{line}" for line in removed] + [f"+{line}" for line in added]
    return "\n".join(body)


class TestParseDiff:
    def test_added_and_removed_lines_are_stripped_multisets(self):
        files = A.parse_diff(diff("src/a.ts", added=[f"  {CODE}", CODE], removed=[OTHER]))
        entry = files["src/a.ts"]
        assert entry["added"] == Counter({CODE: 2})
        assert entry["removed"] == Counter({OTHER: 1})
        assert entry["new"] is False and entry["deleted"] is False

    def test_new_and_deleted_files_are_flagged(self):
        text = diff("n.ts", added=[CODE], new=True) + "\n" + diff("d.ts", removed=[CODE], deleted=True)
        files = A.parse_diff(text)
        assert files["n.ts"]["new"] is True and files["d.ts"]["deleted"] is True

    def test_a_hunk_line_that_looks_like_a_header_is_content(self):
        # A Markdown rule added inside a hunk prints as `+---`; a `++i` as `+++i`.
        text = "\n".join(
            [
                "diff --git a/x.md b/x.md",
                "--- a/x.md",
                "+++ b/x.md",
                "@@ -1 +1,2 @@",
                "+--- a rule ---",
                "+++i; the counter moves",
            ]
        )
        entry = A.parse_diff(text)["x.md"]
        assert entry["added"] == Counter({"--- a rule ---": 1, "++i; the counter moves": 1})

    def test_binary_files_carry_flags_only(self):
        text = "\n".join(["diff --git a/i.webp b/i.webp", "new file mode 100644", "Binary files /dev/null and b/i.webp differ"])
        entry = A.parse_diff(text)["i.webp"]
        assert entry["new"] is True and not entry["added"]

    def test_quoted_paths_are_unquoted(self):
        assert A.unquote_path('"a\\tb"') == "a\tb"
        assert A.unquote_path('"caf\\303\\251.ts"') == "café.ts"
        assert A.unquote_path("plain.ts") == "plain.ts"

    def test_garbage_is_empty(self):
        assert A.parse_diff(None) == {} and A.parse_diff("nonsense\n@@ x") == {}


def parsed(**files) -> dict:
    return {
        path: {
            "added": Counter(spec.get("added", {})),
            "removed": Counter(spec.get("removed", {})),
            "new": spec.get("new", False),
            "deleted": spec.get("deleted", False),
        }
        for path, spec in files.items()
    }


class TestMeasure:
    def test_lines_the_base_lacks_are_absent_the_rest_present(self):
        branch = parsed(**{"a.ts": {"added": {CODE: 3, OTHER: 1}}})
        base = parsed(**{"a.ts": {"removed": {CODE: 1}}})
        result = A.measure(branch, base, set())
        assert (result["lines"], result["present"]) == (4, 3)
        assert result["absorbed"] is True  # 3/4 = the threshold exactly

    def test_short_lines_do_not_vote(self):
        result = A.measure(parsed(**{"a.ts": {"added": {"});": 9, CODE: 1}}}), {}, set())
        assert result["lines"] == 1

    def test_a_file_the_base_never_had_blocks_the_verdict(self):
        branch = parsed(**{"new.ts": {"added": {CODE: 1}, "new": True}, "a.ts": {"added": {OTHER: 99}}})
        base = parsed(**{"new.ts": {"removed": {CODE: 1}, "deleted": True}})
        result = A.measure(branch, base, set())
        assert result["missing_files"] == ["new.ts"] and result["missing_file_count"] == 1
        assert result["absorbed"] is False

    def test_a_file_the_base_took_then_deleted_is_not_missing_and_does_not_vote(self):
        branch = parsed(**{"gone.ts": {"added": {CODE: 5}, "new": True}, "a.ts": {"added": {OTHER: 1}}})
        base = parsed(**{"gone.ts": {"removed": {CODE: 5}, "deleted": True}})
        result = A.measure(branch, base, {"gone.ts"})
        assert result["missing_file_count"] == 0 and result["lines"] == 1 and result["absorbed"]

    def test_data_and_media_are_skipped_both_ways(self):
        branch = parsed(
            **{"n0e0.1024.abc.webp": {"new": True}, "blocks.json": {"added": {CODE: 50}}, "a.ts": {"added": {OTHER: 1}}}
        )
        base = parsed(**{"n0e0.1024.abc.webp": {"deleted": True}, "blocks.json": {"removed": {CODE: 50}}})
        result = A.measure(branch, base, set())
        assert result["missing_file_count"] == 0 and result["lines"] == 1 and result["absorbed"]

    def test_nothing_measured_is_never_absorbed(self):
        assert A.measure({}, {}, set())["absorbed"] is False

    def test_the_missing_list_is_capped_and_the_count_is_whole(self):
        branch = parsed(**{f"f{i}.ts": {"added": {CODE: 1}, "new": True} for i in range(9)})
        base = parsed(**{f"f{i}.ts": {"removed": {CODE: 1}, "deleted": True} for i in range(9)})
        result = A.measure(branch, base, set())
        assert len(result["missing_files"]) == A.MISSING_FILES_MAX and result["missing_file_count"] == 9


class Script:
    """A runner answering per argv prefix (after the fixed `git -C repo -c …`)."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, command, timeout):
        self.calls.append(command)
        verb = command[5:]
        for prefix, answer in self.answers:
            if verb[: len(prefix)] == list(prefix):
                return answer
        raise AssertionError(f"unscripted: {command}")


def rows(**overrides):
    base = {"branch": "main", "is_base": True, "oid": BASE_TIP, "ahead": 0}
    open_row = {
        "branch": "lane",
        "is_base": False,
        "oid": TIP,
        "ahead": 3,
        "landed": False,
        "contained_in": None,
        "content": "measured",
        **overrides,
    }
    return [base, open_row]


def tier_script(branch_diff=None, base_diff=None, deleted=""):
    return Script(
        [
            (("merge-base",), (0, MB + "\n", "")),
            (("diff",), (0, branch_diff if branch_diff is not None else diff("a.ts", added=[CODE, OTHER]), "")),
            (("--literal-pathspecs", "diff"), (0, base_diff if base_diff is not None else "", "")),
            (("--literal-pathspecs", "log"), (0, deleted, "")),
        ]
    )


class TestTier:
    def make(self, script):
        started = []
        return A.AbsorptionTier(REPO, runner=script, start_thread=started.append), started

    def test_judge_forks_nothing_marks_pending_and_schedules_one_pass(self):
        script = tier_script()
        tier, started = self.make(script)
        table = rows()
        tier.judge(table)
        assert script.calls == [] and len(started) == 1
        assert table[1]["absorption"] == {"status": "pending"} and table[0]["absorption"] is None

    def test_after_one_pass_the_row_carries_the_measurement(self):
        script = tier_script(base_diff=diff("a.ts", removed=[OTHER]))
        tier, started = self.make(script)
        tier.judge(rows())
        started.pop()()
        table = rows()
        calls = len(script.calls)
        tier.judge(table)
        assert len(script.calls) == calls, "judging stays fork-free"
        assert table[1]["absorption"] == {
            "status": "measured",
            "lines": 2,
            "present": 1,
            "missing_files": [],
            "missing_file_count": 0,
            "absorbed": False,
        }
        assert started == []

    def test_proofs_and_pending_content_are_never_measured(self):
        tier, started = self.make(tier_script())
        for override in ({"landed": True}, {"contained_in": "other"}, {"content": "pending"}, {"ahead": 0}):
            table = rows(**override)
            tier.judge(table)
            assert table[1]["absorption"] is None, override
        assert started == []

    def test_a_moved_base_reuses_the_branch_diff(self):
        script = tier_script()
        tier, started = self.make(script)
        tier.judge(rows())
        started.pop()()
        moved = rows()
        moved[0]["oid"] = oid("ba5f")
        tier.judge(moved)
        started.pop()()
        branch_diffs = [c for c in script.calls if c[5:6] == ["diff"]]
        assert len(branch_diffs) == 1, "the (merge-base, tip) diff is cached"

    def test_an_oversized_branch_reads_too_big(self, monkeypatch):
        monkeypatch.setattr(A, "MAX_DIFF_CHARS", 10)
        tier, started = self.make(tier_script())
        tier.judge(rows())
        started.pop()()
        table = rows()
        tier.judge(table)
        assert table[1]["absorption"] == {"status": "too_big"}

    def test_a_failed_pass_backs_off(self):
        clock = [100.0]
        started = []
        tier = A.AbsorptionTier(REPO, runner=lambda *_a: (128, "", "fatal"), start_thread=started.append, clock=lambda: clock[0])
        tier.judge(rows())
        started.pop()()
        assert tier.last_error
        tier.judge(rows())
        assert started == []
        clock[0] += A.ABSORPTION_BACKOFF_SECONDS
        tier.judge(rows())
        assert len(started) == 1

    def test_pathspecs_are_literal_and_chunked(self, monkeypatch):
        monkeypatch.setattr(A, "PATHSPEC_CHUNK", 2)
        many = "\n".join(diff(f":(glob)f{i}.ts", added=[CODE]) for i in range(5))
        script = tier_script(branch_diff=many)
        tier, started = self.make(script)
        tier.judge(rows())
        started.pop()()
        base_calls = [c for c in script.calls if c[5:7] == ["--literal-pathspecs", "diff"]]
        assert len(base_calls) == 3
        assert all(c[c.index("--") + 1].startswith(":(glob)") for c in base_calls)
