"""What one audit actually walks.

The regression these pin is the one that makes the whole feature quiet: a walk
of "the N most recently touched files" truncates the list deep inside the 21-day
window it claims to honour and drops whatever sorts past N — severity-1 findings
included. The window is the bound; the file cap is only a backstop against
unbounded growth.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from coordinator.sources import (
    DEFAULT_MAX_AGE_DAYS,
    LIVE_STATUSES,
    display_path,
    live_agent_rows,
    recent_transcripts,
    scan_all,
    session_timestamp_seconds,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
PROJECT = "-Users-me-repo"


def write_transcript(root: Path, session_id: str, *, age_hours: float, mtime_age_hours: float | None = None) -> Path:
    """One transcript whose closing message carries a soft ask (signal E), with
    its record timestamp and its mtime set independently — a transcript's mtime
    can be hours to days off its last activity."""
    directory = root / "projects" / PROJECT
    directory.mkdir(parents=True, exist_ok=True)
    stamp = (NOW - timedelta(hours=age_hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path = directory / f"{session_id}.jsonl"
    path.write_text(
        '{"type": "assistant", "timestamp": "%s", "message": {"content": '
        '[{"type": "text", "text": "Shipped it. Want me to keep going?"}]}}\n' % stamp
    )
    touch = NOW - timedelta(hours=mtime_age_hours if mtime_age_hours is not None else age_hours)
    os.utime(path, (touch.timestamp(), touch.timestamp()))
    return path


@pytest.fixture()
def claude_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    (tmp_path / "sessions").mkdir()
    return tmp_path


def test_the_scan_covers_the_whole_age_window_not_the_mtime_top_sixty(claude_home):
    # 80 live sessions, all well inside the window. The old top-60 slice would
    # have thrown 20 of them away without opening the files.
    for index in range(80):
        write_transcript(claude_home, f"{index:08d}-live", age_hours=index + 1)
    scan = scan_all(now=NOW)
    assert scan["scanned"] == 80
    assert len(scan["findings"]) == 80


def test_a_file_outside_the_window_is_never_opened(claude_home):
    write_transcript(claude_home, "aaaaaaaa-live", age_hours=5)
    write_transcript(claude_home, "bbbbbbbb-old", age_hours=DEFAULT_MAX_AGE_DAYS * 24 + 200)
    scan = scan_all(now=NOW)
    # mtime can only ever over-report freshness, so "untouched since the cutoff"
    # is a sound reason to skip a file unread — and the cheapest one there is.
    assert scan["scanned"] == 1
    assert [f["key"] for f in scan["findings"]] == ["aaaaaaaa:open-offer"]


def test_a_bulk_touched_file_is_still_judged_by_its_last_record(claude_home):
    # Anything that bulk-touches the directory moves mtime. A fresh mtime must
    # not make an ancient session look live.
    write_transcript(claude_home, "cccccccc-stale", age_hours=40 * 24, mtime_age_hours=1)
    scan = scan_all(now=NOW)
    assert scan["scanned"] == 1  # opened, because mtime said it might be live
    assert scan["findings"] == []  # dropped, because the record said otherwise


def test_the_scan_limit_still_caps_a_runaway_directory(claude_home):
    for index in range(30):
        write_transcript(claude_home, f"{index:08d}-live", age_hours=index + 1)
    assert scan_all(now=NOW, scan_limit=10)["scanned"] == 10


def test_recent_transcripts_orders_by_mtime_and_honours_the_floor(claude_home):
    old = write_transcript(claude_home, "aaaaaaaa-old", age_hours=1000)
    mid = write_transcript(claude_home, "bbbbbbbb-mid", age_hours=100)
    new = write_transcript(claude_home, "cccccccc-new", age_hours=1)
    directory = claude_home / "projects"
    assert recent_transcripts(directory, 10) == [new, mid, old]
    floor = (NOW - timedelta(hours=500)).timestamp()
    assert recent_transcripts(directory, 10, floor) == [new, mid]
    assert recent_transcripts(directory, 1, floor) == [new]


# ── GET /live's pure half ────────────────────────────────────────────────────
# The rows a client places its agents with. Liveness is injected, so every
# case here is a handful of dicts and a set of "alive" pids. Every value is
# invented.

LIVE_ROW_KEYS = {"pid", "session_id", "cwd", "status", "name", "kind", "updated_at"}
UPDATED_MS = 1900000000123  # the index writes epoch milliseconds
UPDATED_S = 1900000000  # 2030-03-17T17:46:40Z


def alive_in(*pids):
    return lambda pid: pid in pids


def entry(pid=1, session_id="12345678-1234-4321-8765-123456789abc", **extra) -> dict:
    return {"pid": pid, "sessionId": session_id, **extra}


@pytest.fixture()
def home(tmp_path, monkeypatch):
    """`Path.home()` reads HOME on POSIX, so this is enough to fence the collapse."""
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def test_live_agent_rows_drops_dead_pids():
    rows = live_agent_rows([entry(pid=1, session_id="a"), entry(pid=999, session_id="b")], alive_in(1))
    assert [row["session_id"] for row in rows] == ["a"]


def test_live_agent_rows_needs_a_session_id_and_an_int_pid():
    entries = [
        entry(pid=1, session_id=""),  # unusable id
        {"pid": 2, "status": "busy"},  # no id at all
        entry(pid=3, session_id=7),  # wrong type
        entry(pid="4", session_id="d"),  # a permissive `alive` must not let this through
        entry(pid=True, session_id="e"),  # bool is an int in Python; not on the wire
        entry(pid=0, session_id="f"),
        "not even a dict",
        entry(pid=5, session_id="ok"),
    ]
    rows = live_agent_rows(entries, lambda _pid: True)
    assert [(row["pid"], row["session_id"]) for row in rows] == [(5, "ok")]


def test_live_agent_rows_collapses_cwd_and_never_ships_the_home_dir(home):
    inside = str(home / "code" / "repo")
    rows = live_agent_rows(
        [
            entry(pid=1, session_id="a", cwd=inside),
            entry(pid=2, session_id="b", cwd=str(home)),
            entry(pid=3, session_id="c", cwd="/opt/elsewhere"),
        ],
        lambda _pid: True,
    )
    assert [row["cwd"] for row in rows] == ["~/code/repo", "~", "/opt/elsewhere"]
    assert str(home) not in repr(rows[:2])


def test_live_agent_rows_normalises_status_to_the_index_vocabulary():
    entries = [
        entry(pid=1, session_id="a", status="busy"),
        entry(pid=2, session_id="b", status="waiting"),
        entry(pid=3, session_id="c", status="idle"),
        entry(pid=4, session_id="d", status="thinking-hard"),  # a future value
        entry(pid=5, session_id="e", status=None),
        entry(pid=6, session_id="f"),
        entry(pid=7, session_id="g", status="shell"),
    ]  # idle + background work
    rows = live_agent_rows(entries, lambda _pid: True)
    assert [row["status"] for row in rows] == ["busy", "waiting", "idle", "idle", "idle", "idle", "shell"]
    assert all(row["status"] in LIVE_STATUSES for row in rows)


def test_live_agent_rows_reads_updated_at_in_both_forms_as_unix_seconds():
    entries = [
        entry(pid=1, session_id="a", updatedAt=UPDATED_MS),
        entry(pid=2, session_id="b", updatedAt="2030-03-17T17:46:40.123Z"),
        entry(pid=3, session_id="c", updatedAt="garbage"),
        entry(pid=4, session_id="d", updatedAt=True),
        entry(pid=5, session_id="e"),
    ]
    rows = live_agent_rows(entries, lambda _pid: True)
    assert [row["updated_at"] for row in rows] == [UPDATED_S, UPDATED_S, None, None, None]
    assert all(isinstance(row["updated_at"], int) for row in rows[:2])


def test_live_agent_rows_tolerates_a_bare_entry_and_leaks_nothing_else():
    bare = live_agent_rows([entry(pid=1, session_id="a")], lambda _pid: True)
    assert bare == [{"pid": 1, "session_id": "a", "cwd": None, "status": "idle", "name": None, "kind": None, "updated_at": None}]

    full = live_agent_rows(
        [
            entry(
                pid=1,
                session_id="a",
                cwd="/x",
                status="busy",
                name="my-app-7a",
                kind="interactive",
                updatedAt=UPDATED_MS,
                waitingFor="permission",
                entrypoint="cli",
                version="2.1.261",
                messagingSocketPath="/tmp/cc-socks/1.sock",
                bridgeSessionId="session_01",
            )
        ],
        lambda _pid: True,
    )
    assert set(full[0]) == LIVE_ROW_KEYS
    assert (full[0]["name"], full[0]["kind"]) == ("my-app-7a", "interactive")
    # Empty strings are absence, not a name.
    blank = live_agent_rows([entry(pid=1, session_id="a", name="", kind="", cwd="")], lambda _pid: True)
    assert (blank[0]["name"], blank[0]["kind"], blank[0]["cwd"]) == (None, None, None)


def test_live_agent_rows_orders_by_pid_not_by_filename():
    # `read_live_sessions` sorts filenames lexically, so 1200.json precedes
    # 300.json; the wire order must not depend on that.
    rows = live_agent_rows([entry(pid=1200, session_id="a"), entry(pid=300, session_id="b")], lambda _pid: True)
    assert [row["pid"] for row in rows] == [300, 1200]


@pytest.mark.parametrize(
    "value, expected",
    [
        (UPDATED_MS, UPDATED_S),
        (float(UPDATED_MS), UPDATED_S),
        ("2030-03-17T17:46:40.123Z", UPDATED_S),
        ("2030-03-17T17:46:40+00:00", UPDATED_S),
        ("2030-03-17T19:46:40+02:00", UPDATED_S),
        ("", None),
        ("soon", None),
        (None, None),
        (True, None),
        ([UPDATED_MS], None),
    ],
)
def test_session_timestamp_seconds_accepts_epoch_millis_and_iso(value, expected):
    assert session_timestamp_seconds(value) == expected


def test_display_path_collapses_home_and_nothing_else(home):
    assert display_path(None) is None
    assert display_path(str(home)) == "~"
    assert display_path(home / "a" / "b") == "~/a/b"
    assert display_path(str(home) + "x/y") == str(home) + "x/y"  # a sibling dir, not home
    assert display_path("/opt/thing") == "/opt/thing"


# ── more than one Claude account ──────────────────────────────────────────


class _Profiles:
    def __init__(self, *roots):
        self._roots = [str(r) for r in roots]

    def claude_roots(self):
        return self._roots


def test_claude_homes_adds_every_profile_root_once(claude_home, tmp_path):
    from coordinator.sources import claude_homes

    second = tmp_path.parent / f"{tmp_path.name}-primary"
    second.mkdir(exist_ok=True)
    homes = claude_homes(_Profiles(claude_home, second))
    assert homes == [claude_home, second]
    assert claude_homes(None) == [claude_home]  # a host without profiles


def test_both_accounts_are_scanned_and_merged_before_the_cap(claude_home, tmp_path):
    second = tmp_path.parent / f"{tmp_path.name}-second"
    (second / "sessions").mkdir(parents=True, exist_ok=True)
    write_transcript(claude_home, "aaaaaaaa-one", age_hours=5)
    write_transcript(second, "bbbbbbbb-two", age_hours=1)
    scan = scan_all(now=NOW, homes=[claude_home, second])
    assert scan["scanned"] == 2
    # The cap applies to the MERGED, mtime-ordered list: the newer file wins
    # whichever account it is in.
    assert scan_all(now=NOW, homes=[claude_home, second], scan_limit=1)["findings"][0]["session_id"] == "bbbbbbbb-two"


def test_owning_transcript_finds_the_account_that_holds_it(claude_home, tmp_path):
    from coordinator.sources import owning_transcript

    second = tmp_path.parent / f"{tmp_path.name}-owner"
    path = write_transcript(second, "cccccccc-mine", age_hours=1)
    assert owning_transcript("cccccccc-mine", [claude_home, second]) == path
    assert owning_transcript("dddddddd-nowhere", [claude_home, second]) is None
