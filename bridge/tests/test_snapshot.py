import pytest

from tldrack.main import StatusStore
from tldrack.statusline_models import resolve_fallback_models
from tldrack.snapshot import (
    apply_hook_cwds,
    DEFAULT_CAPTURE_LINES,
    MAX_CAPTURE_LINES,
    TMUX_FIELD_SEP,
    TMUX_PANE_FORMAT,
    RepoResolver,
    capture_lines,
    merge_snapshot,
    repo_name_from_common_dir,
    parse_pane_sessions,
    parse_tmux_clients,
    parse_tmux_panes,
    tail_visible,
)


def _pane(session, attached, window_active, pane_active, title, cwd="/repo", profile=""):
    return TMUX_FIELD_SEP.join((session, attached, window_active, pane_active, cwd, profile, title))


def _client(session, tty):
    return TMUX_FIELD_SEP.join((session, tty))


def test_parse_multi_session():
    output = "\n".join(
        [
            _pane("voidcraft", "1", "1", "1", "claude"),
            _pane("research", "0", "1", "1", "vim"),
        ]
    )
    parsed = parse_tmux_panes(output)
    assert parsed == {
        "voidcraft": {"title": "claude", "attached": True, "cwd": "/repo", "profile": None},
        "research": {"title": "vim", "attached": False, "cwd": "/repo", "profile": None},
    }


def test_parse_prefers_active_pane_over_inactive():
    output = "\n".join(
        [
            _pane("work", "1", "0", "1", "background window"),
            _pane("work", "1", "1", "1", "active window"),
            _pane("work", "1", "1", "0", "sibling pane"),
        ]
    )
    assert parse_tmux_panes(output)["work"]["title"] == "active window"


def test_parse_first_pane_kept_when_no_active():
    output = "\n".join(
        [
            _pane("work", "1", "0", "1", "first"),
            _pane("work", "1", "1", "0", "second"),
        ]
    )
    assert parse_tmux_panes(output)["work"]["title"] == "first"


def test_parse_braille_prefixed_title_preserved():
    # Claude Code writes spinner-prefixed progress into the pane title verbatim.
    output = _pane("voidcraft", "1", "1", "1", "⠹ refactoring")
    assert parse_tmux_panes(output)["voidcraft"]["title"] == "⠹ refactoring"


def test_parse_attached_count_to_bool():
    assert parse_tmux_panes(_pane("a", "0", "1", "1", "x"))["a"]["attached"] is False
    assert parse_tmux_panes(_pane("b", "2", "1", "1", "x"))["b"]["attached"] is True


def test_parse_malformed_line_skipped():
    output = "\n".join(
        [
            "not enough fields",
            _pane("good", "1", "1", "1", "kept"),
        ]
    )
    assert parse_tmux_panes(output) == {"good": {"title": "kept", "attached": True, "cwd": "/repo", "profile": None}}


def test_parse_invalid_session_name_skipped():
    output = _pane("bad.name", "1", "1", "1", "x")
    assert parse_tmux_panes(output) == {}


def test_parse_empty_string():
    assert parse_tmux_panes("") == {}


def test_merge_tmux_only_reads_unknown():
    merged = merge_snapshot({"solo": {"title": "vim", "attached": True}}, [])
    assert merged == [
        {
            "session": "solo",
            "state": "unknown",
            "age_seconds": None,
            "message": None,
            "activity": None,
            "tasks": None,
            "model": None,
            "usage": None,
            "state_source": None,
            "open_work": None,
            "work_done": None,
            "open_work_p": None,
            "answer_to_read": None,
            "title": "vim",
            "attached": True,
            "cwd": None,
            "live": True,
            "icon": None,
            "repo": None,
            "client_ttys": [],
            "profile": None,
        }
    ]


def test_merge_store_only_has_null_tmux_fields():
    store_entries = [{"session": "ghost", "state": "busy", "age_seconds": 3.0, "message": "working"}]
    merged = merge_snapshot({}, store_entries)
    assert merged == [
        {
            "session": "ghost",
            "state": "busy",
            "age_seconds": 3.0,
            "message": "working",
            "activity": None,
            "tasks": None,
            "model": None,
            "usage": None,
            "state_source": None,
            "open_work": None,
            "work_done": None,
            "open_work_p": None,
            "answer_to_read": None,
            "title": None,
            "attached": False,
            "cwd": None,
            "live": False,
            "icon": None,
            "repo": None,
            "client_ttys": [],
            "profile": None,
        }
    ]


def test_merge_both_sources_combined():
    tmux = {"voidcraft": {"title": "claude", "attached": True, "cwd": "/repo"}}
    store_entries = [{"session": "voidcraft", "state": "busy", "age_seconds": 1.2, "message": None}]
    merged = merge_snapshot(tmux, store_entries, None, {"voidcraft": ["/dev/ttys013"]})
    assert merged == [
        {
            "session": "voidcraft",
            "state": "busy",
            "age_seconds": 1.2,
            "message": None,
            "activity": None,
            "tasks": None,
            "model": None,
            "usage": None,
            "state_source": None,
            "open_work": None,
            "work_done": None,
            "open_work_p": None,
            "answer_to_read": None,
            "title": "claude",
            "attached": True,
            "cwd": "/repo",
            "live": True,
            "icon": None,
            "repo": None,
            "client_ttys": ["/dev/ttys013"],
            "profile": None,
        }
    ]


def test_merge_attaches_icons_to_all_row_kinds():
    tmux = {
        "live-only": {"title": "vim", "attached": False},
        "both": {"title": "claude", "attached": True},
    }
    store_entries = [
        {"session": "both", "state": "busy", "age_seconds": 1.0, "message": None},
        {"session": "store-only", "state": "free", "age_seconds": 9.0, "message": None},
    ]
    icons = {
        "live-only": "flame",
        "both": "wrench.and.screwdriver",
        "store-only": "leaf",
        "no-such-session": "star",  # a pick with no row must NOT synthesize one
    }
    merged = {row["session"]: row["icon"] for row in merge_snapshot(tmux, store_entries, icons)}
    assert merged == {
        "live-only": "flame",
        "both": "wrench.and.screwdriver",
        "store-only": "leaf",
    }


def test_merge_carries_model_and_usage_from_store():
    usage = {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 5}
    store_entries = [
        {
            "session": "voidcraft",
            "state": "busy",
            "age_seconds": 1.0,
            "message": None,
            "activity": None,
            "model": "claude-opus-4-8",
            "usage": usage,
        }
    ]
    row = merge_snapshot({"voidcraft": {"title": "claude", "attached": True}}, store_entries)[0]
    assert row["model"] == "claude-opus-4-8"
    assert row["usage"] == usage


def test_merge_carries_laya_verdict_from_store():
    """The rack draws ⚠️ / ✅ off `/snapshot` alone; until 2026-11-05 the merge dropped all three."""
    store_entries = [
        {
            "session": "vc-1",
            "state": "free",
            "age_seconds": 2.0,
            "message": None,
            "open_work": False,
            "work_done": True,
            "open_work_p": 0.246,
        }
    ]
    tmux = {"vc-1": {"title": "claude", "attached": True}, "vc-2": {"title": "zsh", "attached": False}}
    rows = {row["session"]: row for row in merge_snapshot(tmux, store_entries)}
    assert (rows["vc-1"]["open_work"], rows["vc-1"]["work_done"], rows["vc-1"]["open_work_p"]) == (False, True, 0.246)
    assert (rows["vc-2"]["open_work"], rows["vc-2"]["work_done"], rows["vc-2"]["open_work_p"]) == (None, None, None)


def test_merge_sorted_by_session_name():
    tmux = {"zeta": {"title": None, "attached": False}, "alpha": {"title": None, "attached": False}}
    store_entries = [{"session": "mid", "state": "free", "age_seconds": 0.0, "message": None}]
    assert [row["session"] for row in merge_snapshot(tmux, store_entries)] == ["alpha", "mid", "zeta"]


def test_merge_empty_inputs():
    assert merge_snapshot({}, []) == []


def test_parse_clients_groups_ttys_by_session():
    output = "\n".join([_client("work", "/dev/ttys013"), _client("other", "/dev/ttys002")])
    assert parse_tmux_clients(output) == {
        "work": ["/dev/ttys013"],
        "other": ["/dev/ttys002"],
    }


def test_parse_clients_same_session_attached_twice():
    # One session open in two windows: both ttys are focusable targets.
    output = "\n".join([_client("work", "/dev/ttys013"), _client("work", "/dev/ttys021")])
    assert parse_tmux_clients(output) == {"work": ["/dev/ttys013", "/dev/ttys021"]}


def test_parse_clients_deduplicates():
    output = "\n".join([_client("work", "/dev/ttys013"), _client("work", "/dev/ttys013")])
    assert parse_tmux_clients(output) == {"work": ["/dev/ttys013"]}


def test_parse_clients_skips_malformed_and_invalid():
    output = "\n".join(
        [
            "no separator here",
            _client("bad.name", "/dev/ttys009"),
            _client("good", ""),
            _client("good", "/dev/ttys013"),
        ]
    )
    assert parse_tmux_clients(output) == {"good": ["/dev/ttys013"]}


def test_parse_clients_empty_string():
    # The overwhelmingly common case: sessions running, nobody attached.
    assert parse_tmux_clients("") == {}


def test_parse_pane_sessions_maps_each_pane_to_its_current_session():
    output = "\n".join(
        (
            TMUX_FIELD_SEP.join(("%0", "rack-one")),
            TMUX_FIELD_SEP.join(("%1", "docs-review")),
            TMUX_FIELD_SEP.join(("%2", "bad name")),  # not a session name
            TMUX_FIELD_SEP.join(("@2", "vc-3")),  # a window id, not a pane
            "%3",  # no separator
        )
    )
    assert parse_pane_sessions(output) == {"%0": "rack-one", "%1": "docs-review"}
    assert parse_pane_sessions("") == {}


def test_capture_lines_clamps_and_defaults():
    assert capture_lines(None) == DEFAULT_CAPTURE_LINES
    assert capture_lines("") == DEFAULT_CAPTURE_LINES
    assert capture_lines("not a number") == DEFAULT_CAPTURE_LINES
    assert capture_lines("5") == 5
    assert capture_lines("0") == 1
    assert capture_lines("-40") == 1
    assert capture_lines("99999") == MAX_CAPTURE_LINES


def test_tail_visible_drops_trailing_blank_padding():
    # capture-pane pads to the full pane height; a mostly-idle pane is a few real
    # rows plus a lot of nothing.
    output = "line one\nline two\n\n   \n\n"
    assert tail_visible(output, 20) == ["line one", "line two"]


def test_tail_visible_keeps_interior_blanks():
    output = "top\n\nbottom\n\n"
    assert tail_visible(output, 20) == ["top", "", "bottom"]


def test_tail_visible_takes_the_last_n_rows():
    output = "\n".join(str(i) for i in range(1, 11))
    assert tail_visible(output, 3) == ["8", "9", "10"]


def test_tail_visible_empty_pane():
    assert tail_visible("", 20) == []
    assert tail_visible("\n\n\n", 20) == []


def test_merge_dead_session_never_carries_a_tty():
    # A store-only (killed) session must not hand a caller a tty to focus, even if
    # a stale client entry names it.
    store_entries = [{"session": "ghost", "state": "free", "age_seconds": 1.0, "message": None}]
    row = merge_snapshot({}, store_entries, None, {"ghost": ["/dev/ttys013"]})[0]
    assert row["live"] is False
    assert row["client_ttys"] == []


def test_merge_detached_live_session_has_empty_ttys():
    # Busy agent nobody is watching — attached False, no clients, still live.
    tmux = {"solo": {"title": "claude", "attached": False, "cwd": "/repo"}}
    row = merge_snapshot(tmux, [], None, {})[0]
    assert row["live"] is True
    assert row["attached"] is False
    assert row["client_ttys"] == []


def test_merge_cwd_from_tmux_pane():
    tmux = {"solo": {"title": "vim", "attached": False, "cwd": "/Users/x/code/project"}}
    assert merge_snapshot(tmux, [])[0]["cwd"] == "/Users/x/code/project"


def test_parse_pane_cwd_with_spaces_and_title_last():
    # A path may contain spaces; the title is still the final field and survives
    # intact because maxsplit stops before it.
    output = _pane("work", "1", "1", "1", "⠹ a title", cwd="/Users/x/My Repos/my project")
    parsed = parse_tmux_panes(output)["work"]
    assert parsed["cwd"] == "/Users/x/My Repos/my project"
    assert parsed["title"] == "⠹ a title"


def test_parse_reads_the_profile_stamp_before_the_title():
    # `#{@vc_profile}` sits BEFORE the title; a title holding the separator's
    # neighbours must still survive, and the stamp must land on its own key.
    output = _pane("vc-1", "0", "1", "1", "⠹ a title", profile="claude-a")
    parsed = parse_tmux_panes(output)["vc-1"]
    assert parsed["profile"] == "claude-a"
    assert parsed["title"] == "⠹ a title"


def test_parse_an_unset_stamp_reads_none():
    assert parse_tmux_panes(_pane("vc-1", "0", "1", "1", "x"))["vc-1"]["profile"] is None


def test_parse_reads_the_full_format_with_the_session_birth_leading():
    # The live format leads with `#{session_created}` then the lineage stamp;
    # the title, last, still survives the separator's neighbours.
    line = TMUX_FIELD_SEP.join(("1700000000", "", _pane("vc-1", "0", "1", "1", "⠹ a title")))
    parsed = parse_tmux_panes(line)["vc-1"]
    assert parsed["created_at"] == 1700000000
    assert parsed["title"] == "⠹ a title"
    assert "exited" not in parsed


def test_parse_reads_the_full_live_format_with_the_dead_field_leading():
    # The live format leads with the dead-pane field; a running pane leaves it
    # empty and carries no `exited` key at all.
    line = TMUX_FIELD_SEP.join(("", "1700000000", "", _pane("vc-1", "0", "1", "1", "⠹ a title")))
    parsed = parse_tmux_panes(line)["vc-1"]
    assert parsed["created_at"] == 1700000000
    assert parsed["title"] == "⠹ a title"
    assert "exited" not in parsed and "exit_code" not in parsed


def test_parse_reads_the_full_live_format_with_the_permission_mode_leading():
    # The live format now leads with the launch permission mode stamp, ahead of the
    # dead-pane field; an unset stamp is "" and leaves no key.
    line = TMUX_FIELD_SEP.join(("auto", "", "1700000000", "", _pane("vc-1", "0", "1", "1", "⠹ a title")))
    parsed = parse_tmux_panes(line)["vc-1"]
    assert parsed["launch_permission_mode"] == "auto"
    assert parsed["created_at"] == 1700000000
    assert parsed["title"] == "⠹ a title"
    # One more leading field since the launch models (tests/test_launch_models.py).
    assert TMUX_PANE_FORMAT.count(TMUX_FIELD_SEP) == 11
    assert (TMUX_FIELD_SEP + "#{@vc_permission_mode}" + TMUX_FIELD_SEP) in TMUX_PANE_FORMAT
    unset = TMUX_FIELD_SEP.join(("", "", "1700000000", "", _pane("vc-1", "0", "1", "1", "x")))
    assert "launch_permission_mode" not in parse_tmux_panes(unset)["vc-1"]


@pytest.mark.parametrize("stamp", ["bypassPermissions", "AUTO", "auto ", "--dangerously-skip-permissions"])
def test_parse_drops_a_permission_stamp_outside_the_closed_set(stamp):
    # Any process on the machine can `tmux set-option`; only a known mode is believed.
    line = TMUX_FIELD_SEP.join((stamp, "", "1700000000", "", _pane("vc-1", "0", "1", "1", "x")))
    assert "launch_permission_mode" not in parse_tmux_panes(line)["vc-1"]


def test_merge_carries_the_launch_mode_on_a_live_row_only():
    rows = merge_snapshot(
        {
            "wt-lead-a": {"title": None, "attached": False, "cwd": "/r", "launch_permission_mode": "auto"},
            "vc-1": {"title": None, "attached": False, "cwd": "/r"},
        },
        [{"session": "gone", "state": "free", "age_seconds": 1, "message": None}],
    )
    by_name = {row["session"]: row for row in rows}
    assert by_name["wt-lead-a"]["launch_permission_mode"] == "auto"
    assert "launch_permission_mode" not in by_name["vc-1"]
    assert "launch_permission_mode" not in by_name["gone"]
    assert all("permission_mode" not in row for row in rows), "the row never claims the CURRENT mode"


@pytest.mark.parametrize(("raw", "code"), [("D1", 1), ("D0", 0), ("S15", 143), ("D", None)])
def test_parse_a_dead_pane_carries_its_exit(raw, code):
    line = TMUX_FIELD_SEP.join((raw, "1700000000", "", _pane("batch-queue", "0", "1", "1", "x")))
    parsed = parse_tmux_panes(line)["batch-queue"]
    assert parsed["exited"] is True
    assert parsed["exit_code"] == code


def test_parse_a_garbled_dead_field_reads_running():
    line = TMUX_FIELD_SEP.join(("X9", "1700000000", "", _pane("vc-1", "0", "1", "1", "x")))
    assert "exited" not in parse_tmux_panes(line)["vc-1"]


def test_the_live_format_falls_back_to_the_start_dir_for_a_dead_pane():
    # A dead pane has no pane_current_path; without the fallback it had no cwd
    # and the rack filed it under `(no path)`.
    assert "#{?pane_current_path,#{pane_current_path},#{session_path}}" in TMUX_PANE_FORMAT


def test_merge_carries_the_exit_on_a_live_row_only():
    rows = merge_snapshot(
        {
            "batch-queue": {"title": None, "attached": False, "cwd": "/r", "exited": True, "exit_code": 1},
            "vc-1": {"title": None, "attached": False, "cwd": "/r"},
        },
        [{"session": "gone", "state": "free", "age_seconds": 1, "message": None}],
    )
    by_name = {row["session"]: row for row in rows}
    assert by_name["batch-queue"]["exited"] is True
    assert by_name["batch-queue"]["exit_code"] == 1
    assert "exited" not in by_name["vc-1"]
    assert "exited" not in by_name["gone"]


def test_parse_a_malformed_birth_is_dropped_not_the_row():
    line = TMUX_FIELD_SEP.join(("soon", "", _pane("vc-1", "0", "1", "1", "x")))
    assert "created_at" not in parse_tmux_panes(line)["vc-1"]


def test_merge_carries_the_birth_on_a_live_row_only():
    rows = merge_snapshot(
        {"vc-1": {"title": None, "attached": False, "cwd": "/r", "created_at": 5}},
        [{"session": "gone", "state": "free", "age_seconds": 1, "message": None}],
    )
    by_name = {row["session"]: row for row in rows}
    assert by_name["vc-1"]["created_at"] == 5
    assert "created_at" not in by_name["gone"]


def test_parse_a_malformed_stamp_reads_none():
    # Anything can `tmux set-option` — a value that is not a profile id is dropped.
    output = _pane("vc-1", "0", "1", "1", "x", profile="Bad Id;rm")
    assert parse_tmux_panes(output)["vc-1"]["profile"] is None


def test_merge_carries_the_stamp_on_a_live_row():
    rows = merge_snapshot({"vc-1": {"title": None, "attached": False, "cwd": "/r", "profile": "codex"}}, [])
    assert rows[0]["profile"] == "codex"


def test_merge_the_transcript_hint_beats_the_stamp():
    # The stamp says what was SPAWNED; the hint says whose transcript the
    # running agent writes. After the spawned CLI exits, the shell can run
    # anything — the hint is the live answer.
    tmux = {"vc-1": {"title": None, "attached": False, "cwd": "/r", "profile": "claude-b"}}
    store = [{"session": "vc-1", "state": "busy", "age_seconds": 0.0, "message": None, "profile": "claude-a"}]
    assert merge_snapshot(tmux, store)[0]["profile"] == "claude-a"


def test_merge_a_dead_row_has_no_profile():
    store = [{"session": "gone", "state": "free", "age_seconds": 9.0, "message": None, "profile": "claude-a"}]
    assert merge_snapshot({}, store)[0]["profile"] is None


def test_status_store_list_empty():
    assert StatusStore().list() == []


def test_status_store_list_after_set():
    store = StatusStore()
    store.set("voidcraft", "busy", "compiling")
    rows = store.list()
    assert len(rows) == 1
    row = rows[0]
    assert row["session"] == "voidcraft"
    assert row["state"] == "busy"
    assert row["message"] == "compiling"
    assert isinstance(row["age_seconds"], float) and row["age_seconds"] >= 0


def test_status_store_rename_moves_the_entry():
    store = StatusStore()
    store.set("vc-1", "attention", "needs you")
    assert store.rename("vc-1", "backend") is True
    rows = store.list()
    assert [row["session"] for row in rows] == ["backend"]
    # State AND message travel — the row is the same report, renamed.
    assert rows[0]["state"] == "attention"
    assert rows[0]["message"] == "needs you"


def test_status_store_rename_unknown_session_is_a_noop():
    store = StatusStore()
    store.set("vc-1", "busy", None)
    # A tmux session that never reported has no store row; renaming it must not
    # fabricate one (that would BE the ghost row this method exists to prevent).
    assert store.rename("ghost", "backend") is False
    assert [row["session"] for row in store.list()] == ["vc-1"]


def test_renamed_session_leaves_no_ghost_row_in_the_snapshot():
    """The whole reason StatusStore.rename exists.

    merge_snapshot union-merges tmux with the store, so a store row with no live
    tmux session behind it comes back `live: False` — which the agent rack draws
    as a dimmed "— session gone —" row. Skip the rename and `vc-1` haunts the
    rack forever, next to the session it actually became.
    """
    store = StatusStore()
    store.set("vc-1", "busy", "working")
    store.rename("vc-1", "backend")
    rows = merge_snapshot(
        {"backend": {"title": None, "attached": True, "cwd": "/repo"}},
        store.list(),
    )
    assert [row["session"] for row in rows] == ["backend"]
    assert rows[0]["live"] is True
    assert rows[0]["state"] == "busy"


def test_snapshot_without_the_rename_would_show_the_ghost():
    """Pins the failure mode itself, so a future 'simplification' that drops the
    store.rename call fails here instead of in the user's rack."""
    store = StatusStore()
    store.set("vc-1", "busy", "working")
    # tmux renamed, store did NOT — exactly what skipping store.rename produces.
    rows = merge_snapshot({"backend": {"title": None, "attached": True, "cwd": "/repo"}}, store.list())
    ghost = [row for row in rows if not row["live"]]
    assert [row["session"] for row in ghost] == ["vc-1"]


# ── repo identity ──────────────────────────────────────────────────────────
#
# The failure these exist to prevent: with checkouts at ~/code/project,
# ~/workspaces/*/project and ~/worktrees/project/*, a path-prefix
# heuristic groups by ancestor directory (code / workspaces / worktrees) — three
# groups for ONE repo. `--git-common-dir` collapses all of them to `project`.


def test_repo_name_from_a_plain_checkouts_relative_common_dir():
    assert repo_name_from_common_dir(".git", "/Users/me/code/project") == "project"


def test_repo_name_from_a_worktrees_absolute_common_dir_folds_onto_the_parent():
    # This is the whole point: run from a linked worktree, git names the MAIN repo.
    assert (
        repo_name_from_common_dir(
            "/Users/me/code/project/.git",
            "/Users/me/workspaces/side-quest/project",
        )
        == "project"
    )


def test_every_worktree_of_one_repo_resolves_to_the_same_name():
    common = "/Users/me/code/project/.git"
    cwds = [
        "/Users/me/code/project",
        "/Users/me/workspaces/side-quest/project",
        "/Users/me/worktrees/project/feat-x",
    ]
    assert {repo_name_from_common_dir(common, cwd) for cwd in cwds} == {"project"}


def test_a_bare_repo_keeps_its_own_name():
    assert repo_name_from_common_dir("/srv/thing.git", "/srv") == "thing"


def test_a_nested_common_dir_is_normalised():
    assert repo_name_from_common_dir("../.git", "/repo/front-end") == "repo"


def test_empty_common_dir_is_no_repo():
    assert repo_name_from_common_dir("", "/repo") is None


def test_resolver_caches_so_polling_does_not_fork_git_every_tick():
    calls = []

    class Counting(RepoResolver):
        def _read(self, cwd):
            calls.append(cwd)
            return "project"

    resolver = Counting()
    for _ in range(5):
        assert resolver.resolve("/repo") == "project"
    assert calls == ["/repo"]


def test_resolver_caches_negatives_too():
    calls = []

    class Counting(RepoResolver):
        def _read(self, cwd):
            calls.append(cwd)
            return

    resolver = Counting()
    assert resolver.resolve("/tmp") is None
    assert resolver.resolve("/tmp") is None
    assert calls == ["/tmp"]  # a plain shell in ~ must not re-fork git every poll


def test_resolver_ignores_sessions_with_no_cwd():
    class Fixed(RepoResolver):
        def _read(self, cwd):
            return "project"

    resolved = Fixed().resolve_all(
        {"a": {"cwd": "/repo"}, "b": {"cwd": None}, "c": {}},
    )
    assert resolved == {"a": "project"}


def test_merge_snapshot_carries_repo_and_defaults_it_to_none():
    rows = merge_snapshot(
        {"a": {"title": None, "attached": False, "cwd": "/repo"}},
        [],
        repos={"a": "project"},
    )
    assert rows[0]["repo"] == "project"
    # Absent for a session git knew nothing about — never a fabricated name.
    rows = merge_snapshot({"b": {"title": None, "attached": False, "cwd": "/tmp"}}, [])
    assert rows[0]["repo"] is None


# ── remembered model (models.py) ─────────────────────────────────────────────
#
# The bug: StatusStore is in-memory and lost on restart, and `model` is written
# from exactly one place — POST /hook. An IDLE Claude session (Stop already
# fired before the restart) emits no further hooks, so the live store never
# learns its model and every surface rendered "claude disconnected" for a
# session that was plainly connected.

REMEMBERED = {"vc-1": {"model": "claude-opus-5", "usage": {"output_tokens": 20}}}


def test_remembered_model_fills_a_session_that_has_not_reported_this_run():
    rows = merge_snapshot(
        {"vc-1": {"title": None, "attached": True, "cwd": "/repo"}},
        [],  # StatusStore wiped by the restart
        models=REMEMBERED,
    )
    assert rows[0]["model"] == "claude-opus-5"
    # The row is still honestly "unknown" state with no age — only the model,
    # which is a fact about what ran, is restored.
    assert rows[0]["state"] == "unknown"
    assert rows[0]["age_seconds"] is None


def test_a_live_reading_always_beats_the_remembered_one():
    """Fallback, never override — a hook that fired this run is more current."""
    store = StatusStore()
    store.set("vc-1", "busy", None, model="claude-sonnet-5")
    rows = merge_snapshot(
        {"vc-1": {"title": None, "attached": True, "cwd": "/repo"}},
        store.list(),
        models=REMEMBERED,
    )
    assert rows[0]["model"] == "claude-sonnet-5"


def test_remembered_models_never_synthesize_a_row():
    """A reading whose session no longer exists stays dormant, exactly like an
    orphaned icon pick — it must not conjure a session out of the file."""
    rows = merge_snapshot({}, [], models=REMEMBERED)
    assert rows == []


# ── statusline fallback tier (statusline_models.py) ──────────────────────────
#
# merge_snapshot is unchanged: it still takes ONE `models` dict. The statusline
# tier is folded into it by resolve_fallback_models before the call, which is
# why there is no second parameter here.


def test_a_statusline_model_reaches_the_row_through_the_same_fallback():
    """The reported bug end to end: a brand-new tmux name, no disk entry, no
    assistant message yet — the row must still name its model."""
    fallback = resolve_fallback_models({}, {"/t/a.jsonl": "claude-opus-5"}, {"vc-9": "/t/a.jsonl"})
    rows = merge_snapshot(
        {"vc-9": {"title": None, "attached": True, "cwd": "/repo"}},
        [],
        models=fallback,
    )
    assert rows[0]["model"] == "claude-opus-5"
    # No usage came with it, so nothing is priced against the wrong model.
    assert rows[0]["usage"] is None


def test_a_live_reading_still_beats_the_statusline_tier():
    store = StatusStore()
    store.set("vc-9", "busy", None, model="claude-sonnet-5")
    fallback = resolve_fallback_models({}, {"/t/a.jsonl": "claude-opus-5"}, {"vc-9": "/t/a.jsonl"})
    rows = merge_snapshot(
        {"vc-9": {"title": None, "attached": True, "cwd": "/repo"}},
        store.list(),
        models=fallback,
    )
    assert rows[0]["model"] == "claude-sonnet-5"


def test_omitting_models_leaves_the_snapshot_byte_identical():
    """Additive: every existing caller (and every test above) passes no models."""
    tmux = {"vc-1": {"title": None, "attached": True, "cwd": "/repo"}}
    assert merge_snapshot(tmux, []) == merge_snapshot(tmux, [], models=None)


# ── hook-reported cwd overlay (apply_hook_cwds) ─────────────────────────────


def test_apply_hook_cwds_hook_cwd_wins_for_a_live_session():
    tmux = {"a": {"title": "claude", "attached": False, "cwd": "/Users/x/code/project"}}
    store = [
        {"session": "a", "state": "working", "age_seconds": 1.0, "message": None, "cwd": "/Users/x/worktrees/project/feat-foo"}
    ]
    out = apply_hook_cwds(tmux, store)
    assert out["a"]["cwd"] == "/Users/x/worktrees/project/feat-foo"
    assert out["a"]["title"] == "claude"  # everything else carried
    assert tmux["a"]["cwd"] == "/Users/x/code/project"  # input not mutated


def test_apply_hook_cwds_pane_cwd_stands_when_the_store_has_none():
    tmux = {"a": {"title": "zsh", "attached": True, "cwd": "/repo"}}
    for store in (
        [],
        [{"session": "a", "state": "free", "age_seconds": 0.0, "message": None}],
        [{"session": "a", "state": "free", "age_seconds": 0.0, "message": None, "cwd": None}],
    ):
        assert apply_hook_cwds(tmux, store)["a"]["cwd"] == "/repo", store


def test_apply_hook_cwds_never_resurrects_a_dead_session():
    tmux = {"live": {"title": "zsh", "attached": False, "cwd": "/repo"}}
    store = [{"session": "gone", "state": "working", "age_seconds": 2.0, "message": None, "cwd": "/somewhere"}]
    out = apply_hook_cwds(tmux, store)
    assert set(out) == {"live"}
    # …and merge_snapshot still reports the dead row with cwd None.
    rows = {row["session"]: row for row in merge_snapshot(out, store)}
    assert rows["gone"]["live"] is False and rows["gone"]["cwd"] is None


def test_apply_hook_cwds_feeds_the_repo_resolver_the_same_dict():
    # The overlay is applied once and handed to BOTH consumers, so the cwd a
    # row shows and the repo it is grouped under cannot disagree.
    tmux = {"a": {"title": "claude", "attached": False, "cwd": "/main"}}
    store = [{"session": "a", "state": "working", "age_seconds": 1.0, "message": None, "cwd": "/wt/feat"}]
    out = apply_hook_cwds(tmux, store)
    seen = []

    class Resolver:
        def resolve_all(self, sessions):
            seen.append({s: p["cwd"] for s, p in sessions.items()})
            return {}

    Resolver().resolve_all(out)
    assert seen == [{"a": "/wt/feat"}]
    assert merge_snapshot(out, store)[0]["cwd"] == "/wt/feat"


def test_status_store_cwd_keeps_forward_and_clears_on_none():
    store = StatusStore()
    store.set("a", "working", None, cwd="/wt/feat")
    assert store.get("a")["cwd"] == "/wt/feat"
    store.set("a", "waiting", None)  # a report that says nothing about cwd
    assert store.get("a")["cwd"] == "/wt/feat"  # …keeps the last one (KEEP default)
    store.set("a", "free", None, cwd=None)  # SessionEnd: the shell is the truth again
    assert store.get("a")["cwd"] is None
    assert store.list()[0]["cwd"] is None and "cwd" in store.list()[0]


# ── PaneSessionCache (the http hook relay's pane → session) ──────────────────


def test_pane_cache_serves_within_ttl_refreshes_on_a_miss_and_after_rename():
    from tldrack.snapshot import PaneSessionCache

    reads = []
    maps = [{"%1": "vc-1"}, {"%1": "vc-1", "%2": "vc-2"}, {"%1": "renamed", "%2": "vc-2"}]
    now = [0.0]
    cache = PaneSessionCache(read=lambda: (reads.append(1), maps[min(len(reads) - 1, 2)])[1], clock=lambda: now[0])
    assert cache.session_of("%1") == "vc-1" and len(reads) == 1
    assert cache.session_of("%1") == "vc-1" and len(reads) == 1  # within TTL: no tmux call
    assert cache.session_of("%2") is None and len(reads) == 1  # a miss inside MISS_SECONDS waits
    now[0] = 0.6
    assert cache.session_of("%2") == "vc-2" and len(reads) == 2
    cache.invalidate()
    assert cache.session_of("%1") == "renamed" and len(reads) == 3
    assert cache.session_of("") is None and cache.session_of("vc-1") is None and len(reads) == 3
