"""agent_index.py — which of Claude Code's live-index entries are believed."""

import json

from tldrack.agent_index import (
    AgentIndexReader,
    IndexEntry,
    one_per_session,
    parse_index_entry,
    parse_ps_table,
    place_on_panes,
    tmux_pane_of,
    tmux_session_of,
    work_children,
)

SID = "eeeeeeee-0000-4000-8000-000000000005"
STARTED = "Thu Oct  1 09:00:00 2026"


def raw(**over):
    """An invented entry in the shape 2.1.294 writes, with overrides."""
    body = {
        "pid": 50001,
        "sessionId": SID,
        "cwd": "/work",
        "procStart": STARTED,
        "version": "2.1.294",
        "kind": "interactive",
        "tmux": "vc-spike:@2.%2",
        "status": "waiting",
        "statusUpdatedAt": 1790000000160,
        "waitingFor": "permission prompt",
    }
    body.update(over)
    return body


def test_an_entry_parses_into_the_bridge_vocabulary():
    entry = parse_index_entry(raw(), 50001)
    assert entry == IndexEntry(
        session="vc-spike",
        state="attention",
        status_at=1790000000.16,
        waiting_for="permission prompt",
        agent_session_id=SID,
        pid=50001,
        proc_start="Thu Oct 1 09:00:00 2026",  # whitespace-normalized
        pane="%2",
    )


def test_status_words_map_and_unknown_words_contribute_nothing():
    assert parse_index_entry(raw(status="busy"), 50001).state == "busy"
    assert parse_index_entry(raw(status="idle"), 50001).state == "free"
    assert parse_index_entry(raw(status="future-word"), 50001) is None


def test_shell_is_the_index_saying_background_work():
    # 2.1.294–2.1.299: idle + a live local_bash task (a Bash job or a Monitor).
    assert parse_index_entry(raw(status="shell"), 50001).state == "background"


def test_the_body_must_agree_with_the_filename_pid():
    # A copied or planted file must not borrow a live process's pid.
    assert parse_index_entry(raw(pid=1), 50001) is None


def test_an_entry_without_a_tmux_field_says_nothing():
    # Pre-2.1.294 Claudes write none: those rows keep hook-only behaviour.
    assert parse_index_entry(raw(tmux=None), 50001) is None
    assert parse_index_entry(raw(tmux="bad name:@1.%1"), 50001) is None


def test_missing_process_identity_or_stamp_is_refused():
    assert parse_index_entry(raw(procStart=None), 50001) is None
    assert parse_index_entry(raw(statusUpdatedAt=None, updatedAt=None), 50001) is None
    assert parse_index_entry(raw(statusUpdatedAt=True), 50001) is None


def test_a_non_uuid_session_id_is_dropped_not_trusted():
    entry = parse_index_entry(raw(sessionId="--dangerously-skip-permissions"), 50001)
    assert entry is not None and entry.agent_session_id is None


def test_tmux_session_of():
    assert tmux_session_of("deploy-fix:@0.%0") == "deploy-fix"
    assert tmux_session_of("deploy-fix") is None
    assert tmux_session_of(None) is None


def test_tmux_pane_of():
    assert tmux_pane_of("deploy-fix:@0.%0") == "%0"
    assert tmux_pane_of("vc-12:@3.%41") == "%41"
    assert tmux_pane_of("deploy-fix:@0") is None
    assert tmux_pane_of("deploy-fix:@0.%x") is None
    assert tmux_pane_of(None) is None


# An invented pane table: four sessions renamed through the rack's ✎, and
# Claude's index still naming each by the name it started under.
RENAMED = {"%0": "rack-one", "%1": "docs-review", "%2": "rack-two", "%3": "BAZ"}


def test_an_entry_lands_on_the_session_its_pane_is_in_now():
    born_as = IndexEntry("vc-2", "attention", 1.0, None, None, 50002, STARTED, pane="%1")
    [placed] = place_on_panes([born_as], RENAMED)
    assert placed.session == "docs-review"
    assert placed.state == "attention"


def test_an_entry_that_cannot_be_placed_says_nothing():
    gone = IndexEntry("vc-9", "busy", 1.0, None, None, 1, STARTED, pane="%9")
    paneless = IndexEntry("vc-1", "busy", 1.0, None, None, 2, STARTED)
    assert place_on_panes([gone, paneless], RENAMED) == []
    # No server, or tmux did not answer: never fall back to the born-with name.
    assert place_on_panes([IndexEntry("vc-2", "busy", 1.0, None, None, 3, STARTED, pane="%1")], {}) == []


def entry(session, pid, state="busy"):
    return IndexEntry(session, state, 1.0, None, None, pid, STARTED)


def panes(**by_pane):
    """A fixed pane → session table (`p2=` is `%2`), so no reader test asks
    this machine's tmux."""
    return lambda: {f"%{k[1:]}": v for k, v in by_pane.items()}


#: Where `raw()`'s pane (%2) lives in every reader test unless one says otherwise.
SPIKE_PANE = panes(p2="vc-spike")


def test_a_session_two_claudes_claim_is_dropped():
    picked = one_per_session([entry("a", 1), entry("b", 2), entry("a", 3)])
    assert list(picked) == ["b"]


def write_index(root, pid, body):
    directory = root / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{pid}.json").write_text(json.dumps(body))


def test_the_reader_skips_dead_pids_before_reading_and_checks_identity(tmp_path):
    write_index(tmp_path, 50001, raw())
    write_index(tmp_path, 777, raw(pid=777, tmux="dead:@1.%1"))
    (tmp_path / "sessions" / "8001.json").write_text("{not json")
    (tmp_path / "sessions" / "50001.0000.key").write_text("x")
    probed = []
    reader = AgentIndexReader(
        lambda: [str(tmp_path)],
        alive=lambda pid: pid in (50001, 8001),
        started=lambda pid: probed.append(pid) or STARTED,
        panes=SPIKE_PANE,
    )
    assert list(reader.read()) == ["vc-spike"]
    assert probed == [50001]


def test_a_recycled_pid_is_not_believed(tmp_path):
    write_index(tmp_path, 50001, raw())
    reader = AgentIndexReader(
        lambda: [str(tmp_path)], alive=lambda pid: True, started=lambda pid: "Fri Oct  9 01:00:00 2026", panes=SPIKE_PANE
    )
    assert reader.read() == {}


def test_identity_is_cached_not_probed_every_read(tmp_path):
    write_index(tmp_path, 50001, raw())
    probed = []
    now = [100.0]
    reader = AgentIndexReader(
        lambda: [str(tmp_path)],
        alive=lambda pid: True,
        started=lambda pid: probed.append(pid) or STARTED,
        panes=SPIKE_PANE,
        clock=lambda: now[0],
    )
    reader.read()
    reader.read()
    assert probed == [50001]
    now[0] += 61
    reader.read()
    assert probed == [50001, 50001]


def test_the_same_root_listed_twice_is_read_once(tmp_path):
    # Two profiles on one config dir must not make one Claude "contest" itself.
    write_index(tmp_path, 50001, raw())
    reader = AgentIndexReader(
        lambda: [str(tmp_path), str(tmp_path)], alive=lambda pid: True, started=lambda pid: STARTED, panes=SPIKE_PANE
    )
    assert list(reader.read()) == ["vc-spike"]


def test_the_reader_follows_a_rename(tmp_path):
    write_index(tmp_path, 50001, raw())  # born in `vc-spike`, pane %2
    reader = AgentIndexReader(
        lambda: [str(tmp_path)], alive=lambda pid: True, started=lambda pid: STARTED, panes=panes(p2="renamed")
    )
    assert list(reader.read()) == ["renamed"]


def test_two_claudes_are_contested_in_the_session_they_share_now(tmp_path):
    # Born in different sessions; one pane was moved in beside the other.
    write_index(tmp_path, 50001, raw())
    write_index(tmp_path, 56017, raw(pid=56017, tmux="other:@4.%4"))
    reader = AgentIndexReader(
        lambda: [str(tmp_path)], alive=lambda pid: True, started=lambda pid: STARTED, panes=panes(p2="shared", p4="shared")
    )
    assert reader.read() == {}


def test_no_index_entries_means_no_tmux_read(tmp_path):
    asked = []
    reader = AgentIndexReader(
        lambda: [str(tmp_path)], alive=lambda pid: True, started=lambda pid: STARTED, panes=lambda: asked.append(1) or {}
    )
    assert reader.read() == {}
    assert asked == []


# Invented children of two Claudes, in the shapes 2.1.299 spawns.
SNAPSHOT_JOB = (
    "/bin/zsh -c source /Users/u/.claude/shell-snapshots/snapshot-zsh-1700000000000-abc123.sh "
    "2>/dev/null || true && eval 'sleep 300' < /dev/null && pwd -P >| /tmp/claude-0000-cwd"
)
PS_TEXT = f"""\
  101     1 /Users/u/.local/bin/claude
  202   101 {SNAPSHOT_JOB}
  203   101 caffeinate -i -t 300
  204   101 /Users/u/.local/bin/uv tool uvx example-mcp-server==1.0.0 https://mcp.example.com/mcp
  205   101 bash ./.claude/hooks/check.sh
  206   101 /usr/bin/python3 /work/bridge/tldrack/statusline.py --status-port 7682
  207   202 sleep 300
  300     1 /Users/u/.local/bin/claude
  301   300 caffeinate -i -t 300
  302   300 {SNAPSHOT_JOB}
  303   300 {SNAPSHOT_JOB}
  bogus line
  12x   1 nope
"""


def test_parse_ps_table_keeps_spaces_and_skips_junk():
    rows = parse_ps_table(PS_TEXT)
    assert (207, 202, "sleep 300") in rows
    assert (202, 101, SNAPSHOT_JOB) in rows
    assert all(isinstance(pid, int) and isinstance(ppid, int) for pid, ppid, _ in rows)
    assert len(rows) == 11


def test_work_children_counts_only_the_job_wrapper():
    # caffeinate, an MCP server, a lingering hook and the statusline are all
    # children of an idle Claude and none of them is work; the job's own child
    # (`sleep`) is a grandchild and is not counted twice.
    counts = work_children(parse_ps_table(PS_TEXT), [101, 300])
    assert counts == {101: 1, 300: 2}


def test_caffeinate_alone_is_no_work():
    assert work_children(parse_ps_table("  9   1 claude\n 10   9 caffeinate -i -t 300\n"), [9]) == {9: 0}


def test_a_pid_not_in_the_table_reads_zero():
    assert work_children(parse_ps_table(PS_TEXT), [4242]) == {4242: 0}


def test_still_running_needs_positive_evidence_of_death():
    entry = IndexEntry("vc-2", "background", 1.0, None, None, 50001, "Thu Oct 1 09:00:00 2026")
    alive = AgentIndexReader(list, alive=lambda pid: True, started=lambda pid: STARTED)
    assert alive.still_running(entry)
    dead = AgentIndexReader(list, alive=lambda pid: False, started=lambda pid: STARTED)
    assert not dead.still_running(entry)
    recycled = AgentIndexReader(list, alive=lambda pid: True, started=lambda pid: "Fri Oct  9 01:00:00 2026")
    assert not recycled.still_running(entry)
