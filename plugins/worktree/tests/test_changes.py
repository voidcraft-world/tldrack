"""The uncommitted-work read: both `-z` parsers, the XY collapse table, the
untracked counter, the truncation ceilings, and the route's path LOOKUP —
with fake runners, never a fork.

The fixture is invented in the shape of `git status` output (git 2.54) for a
worktree that `git worktree remove` refuses for its uncommitted work, plus two
more shapes: a rename and a binary.
"""

from pathlib import Path

import pytest

from worktree import changes as c

# ── fixtures ────────────────────────────────────────────────────────────────
#
# `status --porcelain=v1 -z`: `XY<space><path>` per NUL-terminated field, the
# rename carrying its source as the NEXT field.

STATUS = (
    "\0".join(
        [
            " M docs/notes/alpha-notes.html",
            " M docs/notes/beta-notes.html",
            "?? docs/notes/gamma-notes.html",
            "?? docs/notes/gamma-notes.md",
            "M  back-end/staged-only.ts",
            "R  worktree/renamed-to.py",
            "worktree/renamed-from.py",
            "D  demo-output/demo.mp4",
            "UU docs/conflict.md",
            "A  docs/brand-new.ts",
        ]
    )
    + "\0"
)

#: `diff --numstat -z HEAD`. The rename record's path field is EMPTY and its
#: two paths follow; the binary is `-\t-`.
NUMSTAT = (
    "\0".join(
        [
            "10\t0\tdocs/notes/alpha-notes.html",
            "11\t0\tdocs/notes/beta-notes.html",
            "3\t1\tback-end/staged-only.ts",
            "2\t2\t",
            "worktree/renamed-from.py",
            "worktree/renamed-to.py",
            "-\t-\tdemo-output/demo.mp4",
            "4\t4\tdocs/conflict.md",
            "9\t0\tdocs/brand-new.ts",
        ]
    )
    + "\0"
)


def rows_by_path(rows: list[dict]) -> dict[str, dict]:
    return {row["path"]: row for row in rows}


# ── builders ────────────────────────────────────────────────────────────────


def test_status_command_pins_v1_and_asks_for_every_untracked_file():
    command = c.build_status_command(Path("/wt/x"))
    assert command[:3] == ["git", "-C", "/wt/x"]
    # Pinned: a future git whose bare `--porcelain` means v2 must not silently
    # feed a v1 parser.
    assert "--porcelain=v1" in command
    assert "-z" in command
    assert "--untracked-files=all" in command


def test_numstat_command_measures_against_head_not_the_index():
    assert c.build_numstat_command("/wt/x") == ["git", "-C", "/wt/x", "diff", "--numstat", "-z", "HEAD"]


def test_patch_command_diffs_an_untracked_file_against_dev_null():
    tracked = c.build_patch_command("/wt/x", "a/b.ts", untracked=False)
    assert tracked[-3:] == ["HEAD", "--", "a/b.ts"]
    assert "--no-color" in tracked

    untracked = c.build_patch_command("/wt/x", "a/new.ts", untracked=True)
    assert "--no-index" in untracked
    assert untracked[-2:] == ["/dev/null", "a/new.ts"]


# ── the XY collapse ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "index,worktree,expected",
    [
        (" ", "M", "modified"),
        ("M", " ", "modified"),
        ("M", "M", "modified"),
        ("A", " ", "added"),
        ("D", " ", "deleted"),
        ("R", " ", "renamed"),
        ("C", " ", "copied"),
        ("T", " ", "typechange"),
        ("?", "?", "untracked"),
        # Every conflict pair is "unmerged" — neither letter is the answer alone.
        ("U", "U", "conflicted"),
        ("A", "A", "conflicted"),
        ("D", "D", "conflicted"),
        ("A", "U", "conflicted"),
        ("U", "D", "conflicted"),
    ],
)
def test_kind_for(index, worktree, expected):
    assert c.kind_for(index, worktree) == expected


def test_a_staged_add_later_modified_reads_as_added():
    """`AM` is a file no commit has: what a forced remove destroys is the whole
    file, not an edit to one."""
    assert c.kind_for("A", "M") == "added"


# ── parse_status ────────────────────────────────────────────────────────────


def test_parse_status_reads_every_shape():
    rows = c.parse_status(STATUS)
    assert len(rows) == 9  # the rename's source field is not a row
    by_path = rows_by_path(rows)

    assert by_path["docs/notes/alpha-notes.html"]["kind"] == "modified"
    assert by_path["docs/notes/gamma-notes.md"]["kind"] == "untracked"
    assert by_path["demo-output/demo.mp4"]["kind"] == "deleted"
    assert by_path["docs/conflict.md"]["kind"] == "conflicted"

    renamed = by_path["worktree/renamed-to.py"]
    assert renamed["kind"] == "renamed"
    assert renamed["orig_path"] == "worktree/renamed-from.py"


def test_parse_status_marks_only_the_index_column_as_staged():
    by_path = rows_by_path(c.parse_status(STATUS))
    assert by_path["back-end/staged-only.ts"]["staged"] is True
    assert by_path["docs/notes/alpha-notes.html"]["staged"] is False
    # `?` is not "unstaged", it is unknown to the index — and a row claiming
    # otherwise would arm the wrong verb.
    assert by_path["docs/notes/gamma-notes.md"]["staged"] is False


def test_parse_status_survives_a_path_with_a_space_in_it():
    rows = c.parse_status(" M docs/my notes.md\0")
    assert rows[0]["path"] == "docs/my notes.md"


@pytest.mark.parametrize("text", ["", None, 42, "\0\0", "garbage"])
def test_parse_status_never_raises(text):
    assert c.parse_status(text) == []


# ── parse_numstat ───────────────────────────────────────────────────────────


def test_parse_numstat_reads_counts_binaries_and_renames():
    stats = c.parse_numstat(NUMSTAT)

    assert stats["docs/notes/alpha-notes.html"] == {"added": 10, "removed": 0, "binary": False}

    binary = stats["demo-output/demo.mp4"]
    assert binary["binary"] is True
    # Not zero: "0 lines changed" and "not lines" are different facts.
    assert binary["added"] is None and binary["removed"] is None

    # A rename is keyed on the DESTINATION — the path the status list uses.
    assert stats["worktree/renamed-to.py"]["added"] == 2
    assert "worktree/renamed-from.py" not in stats


def test_parse_numstat_stops_cleanly_on_a_truncated_rename_record():
    stats = c.parse_numstat("2\t2\t\0only-one-path\0")
    assert stats == {}


@pytest.mark.parametrize("text", ["", None, [], "no tabs here\0"])
def test_parse_numstat_never_raises(text):
    assert c.parse_numstat(text) == {}


# ── merge + summary ─────────────────────────────────────────────────────────


def test_merge_rows_prices_the_tracked_half_and_leaves_untracked_null():
    merged = rows_by_path(c.merge_rows(c.parse_status(STATUS), c.parse_numstat(NUMSTAT)))
    assert merged["docs/notes/alpha-notes.html"]["added"] == 10
    assert merged["demo-output/demo.mp4"]["binary"] is True
    # Untracked files are absent from `diff HEAD` by construction; the caller
    # fills these from the filesystem.
    untracked = merged["docs/notes/gamma-notes.md"]
    assert untracked["added"] is None and untracked["bytes"] is None


def test_summarize_counts_kinds_by_name_and_excludes_binaries_from_the_totals():
    merged = c.merge_rows(c.parse_status(STATUS), c.parse_numstat(NUMSTAT))
    summary = c.summarize(merged)
    assert summary["files"] == 9
    assert summary["binary"] == 1
    assert summary["kinds"]["untracked"] == 2
    assert summary["kinds"]["renamed"] == 1
    # 10 + 11 + 3 + 2 + 4 + 9 — the binary contributes nothing to a line count.
    assert summary["added"] == 39
    assert summary["removed"] == 7


def test_summarize_of_nothing():
    assert c.summarize([]) == {"files": 0, "added": 0, "removed": 0, "binary": 0, "kinds": {}}


# ── the untracked counter (a read, never a fork) ────────────────────────────


def test_count_untracked_counts_lines_the_way_numstat_does(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n")
    counted = c.count_untracked(tmp_path, "a.txt")
    assert counted["added"] == 3  # a trailing newline TERMINATES a line
    assert counted["removed"] == 0
    assert counted["binary"] is False
    assert counted["bytes"] == 14


def test_count_untracked_counts_a_file_with_no_trailing_newline(tmp_path: Path):
    (tmp_path / "a.txt").write_text("one\ntwo")
    assert c.count_untracked(tmp_path, "a.txt")["added"] == 2


def test_count_untracked_calls_a_nul_byte_binary(tmp_path: Path):
    (tmp_path / "a.bin").write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0")
    counted = c.count_untracked(tmp_path, "a.bin")
    assert counted["binary"] is True
    assert counted["added"] is None


def test_count_untracked_skips_the_read_on_a_big_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(c, "UNTRACKED_COUNT_MAX_BYTES", 8)
    (tmp_path / "a.txt").write_text("more than eight bytes\n")
    counted = c.count_untracked(tmp_path, "a.txt")
    assert counted["bytes"] == 22
    assert counted["added"] is None  # unknown, and the size still tells a story


def test_count_untracked_of_a_vanished_file_is_unknown_not_an_error(tmp_path: Path):
    counted = c.count_untracked(tmp_path, "gone.txt")
    assert counted == {"added": None, "removed": 0, "binary": False, "bytes": None}


def test_count_untracked_of_an_empty_file(tmp_path: Path):
    (tmp_path / "a.txt").write_text("")
    assert c.count_untracked(tmp_path, "a.txt")["added"] == 0


# ── truncation ──────────────────────────────────────────────────────────────


def test_truncate_patch_cuts_on_a_line_boundary(monkeypatch):
    monkeypatch.setattr(c, "PATCH_MAX_LINES", 3)
    text, truncated = c.truncate_patch("a\nb\nc\nd\ne")
    assert text == "a\nb\nc"
    assert truncated is True


def test_truncate_patch_also_catches_few_enormous_lines(monkeypatch):
    monkeypatch.setattr(c, "PATCH_MAX_BYTES", 10)
    text, truncated = c.truncate_patch("x" * 50)
    assert len(text) == 10
    assert truncated is True


def test_truncate_patch_leaves_a_small_patch_alone():
    assert c.truncate_patch("@@ -1 +1 @@\n-a\n+b") == ("@@ -1 +1 @@\n-a\n+b", False)


# ── collect / patch_for (fake runner) ───────────────────────────────────────


class FakeRunner:
    """Answers by the command's git subcommand. Two forks are the contract —
    a third would mean someone started probing per file."""

    def __init__(self, status=STATUS, numstat=NUMSTAT, status_code=0):
        self.status = status
        self.numstat = numstat
        self.status_code = status_code
        self.commands: list[list[str]] = []

    def __call__(self, command, timeout, **kwargs):
        self.commands.append(command)
        if "status" in command:
            return self.status_code, self.status, "" if self.status_code == 0 else "fatal: not a git repository"
        if "--numstat" in command:
            return 0, self.numstat, ""
        return 0, "diff --git a/x b/x\n@@ -1 +1 @@\n-a\n+b", ""


@pytest.fixture
def fake_run(monkeypatch):
    runner = FakeRunner()
    monkeypatch.setattr(c.create_mod, "run_bounded", runner)
    return runner


def test_collect_runs_exactly_two_forks(fake_run, tmp_path):
    rows, summary, error = c.collect(tmp_path)
    assert error is None
    assert len(fake_run.commands) == 2
    assert summary["files"] == 9
    assert rows_by_path(rows)["back-end/staged-only.ts"]["added"] == 3


def test_collect_skips_the_diff_fork_when_everything_is_untracked(monkeypatch, tmp_path):
    runner = FakeRunner(status="?? new.md\0")
    monkeypatch.setattr(c.create_mod, "run_bounded", runner)
    (tmp_path / "new.md").write_text("hello\n")

    rows, summary, error = c.collect(tmp_path)
    assert [command[3] for command in runner.commands] == ["status"]
    assert rows[0]["added"] == 1
    assert summary["added"] == 1


def test_collect_reports_a_git_failure_rather_than_an_empty_worktree(monkeypatch, tmp_path):
    runner = FakeRunner(status="", status_code=128)
    monkeypatch.setattr(c.create_mod, "run_bounded", runner)
    rows, summary, error = c.collect(tmp_path)
    # An empty list and a failed probe must never look the same: one says
    # "nothing to lose", and acting on that reading is how work disappears.
    assert error == "fatal: not a git repository"
    assert rows == [] and summary["files"] == 0


def test_patch_for_accepts_the_no_index_exit_code(monkeypatch, tmp_path):
    def runner(command, timeout, **kwargs):
        return 1, "diff --git a/new.md b/new.md\n@@ -0,0 +1 @@\n+hi", ""

    monkeypatch.setattr(c.create_mod, "run_bounded", runner)

    patch, truncated, error = c.patch_for(tmp_path, {"path": "new.md", "kind": "untracked"})
    assert error is None and truncated is False
    assert patch.startswith("diff --git")


def test_patch_for_reports_a_real_failure(monkeypatch, tmp_path):
    def runner(command, timeout, **kwargs):
        return 128, "", "fatal: bad object HEAD"

    monkeypatch.setattr(c.create_mod, "run_bounded", runner)

    patch, _truncated, error = c.patch_for(tmp_path, {"path": "x.ts", "kind": "modified"})
    assert patch == "" and error == "fatal: bad object HEAD"
