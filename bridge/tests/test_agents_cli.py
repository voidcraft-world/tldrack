from tldrack.agent_index import AgentIndexReader, IndexEntry
from tldrack.agents_cli import (
    DRIFT_AFTER_TICKS,
    PublicEntry,
    PublicStatus,
    compare_root,
    pane_for_pid,
    parse_pane_pids,
    parse_public_entries,
    public_env,
)

SID = "6f1c2a3b-0000-4000-8000-1234567890ab"
ROOT = "/Users/x/.claude-a"


# ── parsing ──────────────────────────────────────────────────────────────────


def test_parse_keeps_known_statuses_and_drops_the_rest():
    # The measured 2.1.312 shape, plus the junk a future version could send.
    raw = [
        {"pid": 10, "cwd": "/r", "kind": "interactive", "startedAt": 1, "sessionId": SID, "name": "vc-1", "status": "busy"},
        {"pid": 11, "status": "waiting", "waitingFor": "permission prompt", "sessionId": "not-a-uuid"},
        {"pid": 12, "status": "idle"},
        {"pid": 13, "status": "thinking"},  # a word we do not know
        {"pid": True, "status": "busy"},  # bool is not a pid
        {"pid": -1, "status": "busy"},
        "nope",
    ]
    assert parse_public_entries(raw) == [
        PublicEntry(10, "busy", None, SID),
        PublicEntry(11, "attention", "permission prompt", None),
        PublicEntry(12, "free", None, None),
    ]
    assert parse_public_entries({"pid": 1}) == []


def test_parse_pane_pids():
    assert parse_pane_pids("%1 100\n%22 2200\nbad line\n%x 5\n") == {100: "%1", 2200: "%22"}


def test_pane_for_pid_walks_up_to_the_panes_shell():
    parent_of = {500: 400, 400: 300, 300: 1, 900: 1}
    panes = {300: "%7"}
    assert pane_for_pid(500, parent_of, panes) == "%7"
    assert pane_for_pid(900, parent_of, panes) is None  # an iTerm claude: no pane above it
    assert pane_for_pid(5, {5: 6, 6: 5}, panes) is None  # a cycle never hangs


def test_public_env_never_pins_the_default_account():
    base = {"CLAUDE_CONFIG_DIR": "/stale", "PATH": "/bin"}
    assert "CLAUDE_CONFIG_DIR" not in public_env("/h/.claude", "/h/.claude", base)
    assert public_env("/h/.claude-c", "/h/.claude", base)["CLAUDE_CONFIG_DIR"] == "/h/.claude-c"


def test_compare_root_counts_and_turn_over_words_agree():
    public = [
        PublicEntry(1, "busy", None, None),
        PublicEntry(2, "free", None, None),
        PublicEntry(3, "busy", None, None),
        PublicEntry(4, "free", None, None),
        PublicEntry(5, "busy", None, None),
    ]
    private = {1: "busy", 2: "background", 3: "free"}
    panes = {1: "%1", 2: "%2", 3: "%3", 4: "%4"}
    # 1 agrees, 2 agrees (idle ≈ background), 3 disagrees, 4 the index lacks, 5 is in no pane.
    assert compare_root(public, private, panes.get) == (2, 1, 1, 1)


# ── the reconciler ───────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _status(reads, clock, panes=None, table="  500   300 claude\n  300     1 zsh\n"):
    """A PublicStatus over one root whose public read is `reads[0]`, popped per poll."""

    def read(_cli, _root, _default):
        return reads.pop(0) if len(reads) > 1 else reads[0]

    return PublicStatus(
        lambda: [ROOT],
        lambda: "/bin/claude",
        "/Users/x/.claude",
        read=read,
        pane_pids=lambda: panes if panes is not None else {300: "%7"},
        process_table=lambda: table,
        started=lambda pid: "Mon Jan 1 12:00:00 2024",
        clock=clock,
    )


def test_a_healthy_index_stays_on_the_index():
    clock = Clock()
    status = _status([[PublicEntry(500, "busy", None, SID)]], clock)
    status.observe({ROOT: [_private(500, "busy")]})
    for _ in range(DRIFT_AFTER_TICKS + 1):
        status.poll_once()
    assert status.drifted() == set()
    assert status.health()[ROOT]["agree"] == 1 and status.health()[ROOT]["mode"] == "index"


def test_an_unreadable_index_drifts_to_public_and_back():
    clock = Clock()
    status = _status([[PublicEntry(500, "busy", None, SID)]], clock)
    status.observe({ROOT: []})  # the files no longer parse
    status.poll_once()
    assert status.drifted() == set()  # one tick is not a drift
    clock.now += 5
    status.poll_once()
    assert status.drifted() == {ROOT}
    assert status.health()[ROOT]["index_missing"] == 1
    status.observe({ROOT: [_private(500, "busy")]})
    clock.now += 5
    status.poll_once()
    assert status.drifted() == set() and status.entries_for(ROOT) == []


def test_public_entries_need_two_agreeing_reads_and_carry_the_first_stamp():
    clock = Clock()
    reads = [[PublicEntry(500, "busy", None, SID)]] * 3 + [[PublicEntry(500, "free", None, SID)]] * 2
    status = _status(list(reads), clock)
    status.observe({ROOT: []})
    status.poll_once()  # t=1000 busy (pending)
    clock.now = 1005
    status.poll_once()  # t=1005 busy again → drifted; confirmed from 1000
    [entry] = status.entries_for(ROOT)
    assert (entry.state, entry.status_at, entry.pane, entry.agent_session_id) == ("busy", 1000, "%7", SID)
    clock.now = 1010
    status.poll_once()  # still busy
    clock.now = 1015
    status.poll_once()  # flips to free: unconfirmed, so nothing is handed over
    assert status.entries_for(ROOT) == []
    clock.now = 1020
    status.poll_once()
    [entry] = status.entries_for(ROOT)
    assert (entry.state, entry.status_at) == ("free", 1015)


def test_a_failed_cli_is_no_evidence():
    clock = Clock()
    status = _status([None], clock)
    status.observe({ROOT: []})
    for _ in range(DRIFT_AFTER_TICKS + 1):
        status.poll_once()
    assert status.drifted() == set()
    assert status.health()[ROOT]["cli_ok"] is False


def test_sessions_outside_tmux_never_cause_a_drift():
    clock = Clock()
    status = _status([[PublicEntry(900, "busy", None, None)]], clock)
    status.observe({ROOT: []})
    for _ in range(DRIFT_AFTER_TICKS + 1):
        status.poll_once()
    assert status.drifted() == set()
    assert status.health()[ROOT]["outside_rack"] == 1


def test_the_reader_places_a_drifted_roots_public_entries():
    clock = Clock()
    status = _status([[PublicEntry(500, "attention", "permission prompt", SID)]], clock)
    reader = AgentIndexReader(lambda: [ROOT], alive=lambda pid: False, panes=lambda: {"%7": "vc-3"}, public=status)
    assert reader.read() == {}  # observes the empty index
    status.poll_once()
    clock.now += 5
    status.poll_once()
    [(session, entry)] = reader.read().items()
    assert (session, entry.state, entry.waiting_for) == ("vc-3", "attention", "permission prompt")


def _private(pid, state):
    return IndexEntry(
        session="vc-3",
        state=state,
        status_at=900.0,
        waiting_for=None,
        agent_session_id=SID,
        pid=pid,
        proc_start="x",
        pane="%7",
        root=ROOT,
    )
