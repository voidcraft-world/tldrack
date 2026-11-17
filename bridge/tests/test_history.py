from tldrack.activity import KEEP_ACTIVITY
from tldrack.history import TransitionLog, transition_entry


def now_stub():
    return 1234.5678


# ── transition_entry ────────────────────────────────────────────────────


def test_state_change_yields_entry():
    previous = {"state": "busy", "activity": {"zone": "codebase"}}
    entry = transition_entry("s", "Stop", previous, "free", None, now=now_stub)
    assert entry == {
        "ts": 1234.568,
        "session": "s",
        "from_state": "busy",
        "to_state": "free",
        "event": "Stop",
        "activity": None,
    }


def test_first_report_comes_from_unknown():
    entry = transition_entry("s", "SessionStart", {}, "free", None, now=now_stub)
    assert entry["from_state"] == "unknown"
    assert entry["to_state"] == "free"


def test_same_state_with_keep_activity_is_none():
    # Notification keep-alives and legacy /status repeats don't spam the log.
    previous = {"state": "attention", "activity": None}
    assert transition_entry("s", "Notification", previous, "attention", KEEP_ACTIVITY) is None
    assert transition_entry("s", "status", previous, "attention", KEEP_ACTIVITY) is None


def test_pretooluse_activity_change_yields_entry_despite_same_state():
    previous = {"state": "busy", "activity": {"zone": "codebase", "tool": "Read", "detail": "read: a.py"}}
    activity = {"zone": "aws", "tool": "Bash", "detail": "bash: aws s3 ls"}
    entry = transition_entry("s", "PreToolUse", previous, "busy", activity, now=now_stub)
    assert entry["from_state"] == "busy"
    assert entry["to_state"] == "busy"
    assert entry["activity"] == activity


def test_pretooluse_identical_activity_is_none():
    activity = {"zone": "codebase", "tool": "Read", "detail": "read: a.py"}
    previous = {"state": "busy", "activity": dict(activity)}
    assert transition_entry("s", "PreToolUse", previous, "busy", activity) is None


def test_keep_activity_entry_carries_stored_activity():
    # attention entry keeps the tool the session was on when it asked.
    previous = {"state": "busy", "activity": {"zone": "aws", "tool": "Bash", "detail": "bash: cdk deploy"}}
    entry = transition_entry("s", "Notification", previous, "attention", KEEP_ACTIVITY, now=now_stub)
    assert entry["activity"] == {"zone": "aws", "tool": "Bash", "detail": "bash: cdk deploy"}


# ── TransitionLog ───────────────────────────────────────────────────────


def entry_stub(n):
    return {"ts": float(n), "session": "s", "from_state": "busy", "to_state": "free", "event": "Stop", "activity": None}


def test_log_empty():
    assert TransitionLog().latest() == []


def test_log_newest_first_and_limit():
    log = TransitionLog()
    for n in range(5):
        log.append(entry_stub(n))
    assert [entry["ts"] for entry in log.latest()] == [4.0, 3.0, 2.0, 1.0, 0.0]
    assert [entry["ts"] for entry in log.latest(2)] == [4.0, 3.0]
    assert len(log.latest(99)) == 5


def test_log_maxlen_evicts_oldest():
    log = TransitionLog(maxlen=3)
    for n in range(5):
        log.append(entry_stub(n))
    assert [entry["ts"] for entry in log.latest()] == [4.0, 3.0, 2.0]


def test_log_returns_copies():
    log = TransitionLog()
    log.append(entry_stub(1))
    log.latest()[0]["to_state"] = "mutated"
    assert log.latest()[0]["to_state"] == "free"
