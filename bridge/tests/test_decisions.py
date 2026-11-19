"""The decision ledger: capture, doc import, precedent lookup, the shadow judge and the /hook wiring."""

import io
import json
from pathlib import Path

from tldrack.decisions import (
    ANSWER_HOOK,
    DecisionLedger,
    ShadowJudge,
    capture_blocked,
    doc_decisions,
    judge_request,
    nearest,
    parse_answers,
    parse_questions,
    read_pick,
)
from tldrack.icons import SessionIconStore
from tldrack.main import (
    CLAUDE_MATCHED_HOOKS,
    HOOK_EVENTS_BY_KIND,
    ShutdownControl,
    StatusStore,
    build_hooks_settings,
    build_status_handler,
)
from tldrack.models import SessionModelStore
from tldrack.profiles import Profile, ProfileRegistry

ASK = {
    "questions": [
        {
            "question": "Which database table should client errors use?",
            "header": "Table",
            "multiSelect": False,
            "options": [
                {"label": "page_cache (Recommended)", "description": "no new infra"},
                {"label": "A new table", "description": "a schema change"},
            ],
        }
    ]
}
ANSWER_INPUT = {**ASK, "answers": {"Which database table should client errors use?": "page_cache (Recommended)"}}


def test_parse_questions_and_answers():
    (q,) = parse_questions(ASK)
    assert q["options"] == ["page_cache (Recommended)", "A new table"] and q["header"] == "Table"
    assert parse_questions({"questions": [{"question": "only one option", "options": [{"label": "a"}]}]}) == []
    assert parse_questions("garbage") == []
    assert parse_answers(ANSWER_INPUT, None) == {"Which database table should client errors use?": "page_cache (Recommended)"}
    assert parse_answers({}, {"answers": {"Q?": "A"}}) == {"Q?": "A"}


def test_capture_writes_one_row_per_answered_question(tmp_path: Path):
    ledger = DecisionLedger(tmp_path, clock=lambda: 1_900_000_000.0)
    ledger.asked("vc-1", ASK)
    rows = ledger.answered("vc-1", ANSWER_INPUT, None, mission="m1", cwd="/Users/x/worktrees/project/feat-a")
    assert len(rows) == 1 and rows[0]["picked_offered"] and rows[0]["cwd"] == "feat-a"
    (stored,) = ledger.read()
    assert stored["answer"] == "page_cache (Recommended)" and stored["mission"] == "m1"
    assert ledger.answered("vc-1", {}, {}) == [], "no pending question, no answer, no row"


def test_disabled_ledger_writes_nothing(tmp_path: Path):
    ledger = DecisionLedger(None)
    ledger.asked("vc-1", ASK)
    assert ledger.answered("vc-1", ANSWER_INPUT, None) and not ledger.enabled
    assert ledger.read() == [] and ledger.summary()["decisions"] == 0


def test_doc_import_is_idempotent(tmp_path: Path):
    doc = tmp_path / "x.md"
    doc.write_text("intro\nDecision (user, 2000-01-02): keep the broad allow rules.\nmore\n")
    assert doc_decisions(doc, doc.read_text())[0]["date"] == "2000-01-02"
    ledger = DecisionLedger(tmp_path / "d")
    assert ledger.import_docs([doc]) == 1
    assert ledger.import_docs([doc]) == 0
    assert ledger.summary()["from_docs"] == 1


def test_nearest_ranks_by_shared_words():
    rows = [
        {"question": "Which table for errors?", "answer": "a"},
        {"question": "What colour is the lamp?", "answer": "b"},
        {"question": "Which database table for client errors?", "answer": "c"},
    ]
    found = nearest(rows, "Which database table should client errors use?", k=2)
    assert [r["answer"] for r in found] == ["c", "a"]
    assert nearest(rows, "") == []


def test_judge_request_and_pick():
    (q,) = parse_questions(ASK)
    body = judge_request(q, [{"question": "q", "answer": "a"}])
    assert body["questions"]["pick"]["type"] == "choice"
    assert body["questions"]["pick"]["criteria"] == {"o0": "page_cache (Recommended)", "o1": "A new table"}
    assert read_pick({"answers": {"pick": {"choice": "o1", "confidence": 0.8}}}, q["options"]) == ("A new table", 0.8)
    assert read_pick({"answers": {"pick": {"choice": "o9"}}}, q["options"]) is None
    assert read_pick({}, q["options"]) is None


def _laya(choice: str):
    calls = []

    def call(path, body):
        calls.append((path, body))
        return 200, {"answers": {"pick": {"choice": choice, "confidence": 0.9}}}

    return call, calls


def test_shadow_judge_scores_in_either_order(tmp_path: Path):
    ledger = DecisionLedger(tmp_path, clock=lambda: 1.0)
    call, calls = _laya("o0")
    judge = ShadowJudge(ledger, lambda: call, worker=False)
    questions = ledger.asked("vc-1", ASK)
    judge.asked("vc-1", questions)
    judge.predict_now("vc-1", questions)  # prediction first
    judge.answered("vc-1", ledger.answered("vc-1", ANSWER_INPUT, None))
    (scored,) = ledger.read("judge.jsonl")
    assert scored["agreed"] is True and calls[0][0] == "/decide"

    call2, _ = _laya("o1")
    judge2 = ShadowJudge(ledger, lambda: call2, worker=False)
    questions = ledger.asked("vc-2", ASK)
    judge2.asked("vc-2", questions)
    judge2.answered("vc-2", ledger.answered("vc-2", ANSWER_INPUT, None))  # answer first
    judge2.predict_now("vc-2", questions)
    assert ledger.read("judge.jsonl")[-1]["agreed"] is False
    summary = ledger.summary()
    assert summary["judged"] == 2 and summary["agreement"] == 0.5


def test_judge_without_laya_does_nothing(tmp_path: Path):
    ledger = DecisionLedger(tmp_path)
    judge = ShadowJudge(ledger, lambda: None, worker=False)
    judge.asked("vc-1", parse_questions(ASK))
    judge.predict_now("vc-1", parse_questions(ASK))
    assert ledger.read("judge.jsonl") == []


def test_hook_relay_captures_ask_user_question(tmp_path: Path):
    ledger = DecisionLedger(tmp_path / "decisions")
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        decisions=ledger,
    )
    h = object.__new__(cls)
    sent = {}
    h._send_json = lambda status, body: sent.update(status=status, body=body)

    def hook(event, payload):
        raw = json.dumps(payload).encode()
        h.path = f"/hook?session=vc-1&event={event}"
        h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
        h.rfile = io.BytesIO(raw)
        h._handle_hook()

    hook("PreToolUse", {"tool_name": "AskUserQuestion", "tool_input": ASK})
    hook("PostToolUse", {"tool_name": "AskUserQuestion", "tool_input": ANSWER_INPUT, "tool_response": {}})
    hook("PostToolUse", {"tool_name": "AskUserQuestion", "tool_input": ANSWER_INPUT, "agent_id": "sub-1"})
    assert len(ledger.read()) == 1, "one row, and a subagent's hook never writes one"
    h.path = "/decisions"
    h._handle_get_decisions()
    assert sent["status"] == 200 and sent["body"]["from_agents"] == 1


def test_since_start_tells_a_missing_answer_hook_from_an_unread_answer(tmp_path: Path):
    # Measured 2026-11-09: every account relayed PreToolUse and none PostToolUse, so questions were
    # held, no answer ever came, and the ledger stayed empty without one error.
    ledger = DecisionLedger(tmp_path)
    ledger.asked("vc-1", ASK)
    ledger.asked("vc-2", ASK)
    ledger.asked("vc-3", {"questions": []})
    assert ledger.summary()["since_start"] == {"asked": 2, "answered": 0, "rows": 0}
    ledger.answered("vc-1", ANSWER_INPUT, None)
    ledger.answered("vc-2", ASK, {"unexpected": "shape"})  # an answer that arrives and does not parse
    assert ledger.summary()["since_start"] == {"asked": 2, "answered": 2, "rows": 1}


def test_capture_blocked_names_claude_accounts_without_the_answer_hook():
    profiles = [
        {"id": "claude-a", "label": "A", "kind": "claude", "cli_found": True, "hooks_missing": ["PostToolUse"]},
        {"id": "claude-b", "label": "B", "kind": "claude", "cli_found": True, "hooks_missing": []},
        {"id": "claude-new", "label": "NEW", "kind": "claude", "cli_found": True, "hooks_missing": ["file"]},
        {"id": "claude-gone", "label": "GONE", "kind": "claude", "cli_found": False, "hooks_missing": ["PostToolUse"]},
        {"id": "claude-stat", "label": "STAT", "kind": "claude", "hooks_missing": ["statusLine"]},
        {"id": "codex", "label": "CODEX", "kind": "codex", "hooks_missing": ["PostToolUse"]},
        {"id": "shell", "label": "SH", "kind": "shell", "hooks_missing": None},
    ]
    assert capture_blocked(profiles) == ["A", "NEW"]
    assert {ANSWER_HOOK[0]: ANSWER_HOOK[1]} == CLAUDE_MATCHED_HOOKS, "the installer relays the hook this checks"


def test_decisions_route_names_the_accounts_that_drop_answers(tmp_path: Path):
    for name, hooks in ((".claude-a", ("Stop",)), (".claude-b", None)):
        (tmp_path / name).mkdir()
        block = build_hooks_settings(7682, hooks)
        (tmp_path / name / "settings.json").write_text(json.dumps(block))
    registry = ProfileRegistry(
        [
            Profile("claude-a", "A", "claude", str(tmp_path / ".claude-a"), True),
            Profile("claude-b", "B", "claude", str(tmp_path / ".claude-b"), False),
        ],
        "claude-a",
        home=tmp_path,
        source="file",
        which=lambda name: f"/usr/bin/{name}",
    )
    assert "PostToolUse" not in registry.hooks_missing(registry.get("claude-b"), 7682, HOOK_EVENTS_BY_KIND)

    def route(bridge_info):
        cls = build_status_handler(
            store=StatusStore(),
            icon_store=SessionIconStore(tmp_path / "icons.json"),
            model_store=SessionModelStore(tmp_path / "models.json"),
            default_session="tldrack",
            bridge_info=bridge_info,
            control=ShutdownControl("status-only"),
            decisions=DecisionLedger(tmp_path / "decisions"),
            profiles=registry,
        )
        h = object.__new__(cls)
        sent = {}
        h._send_json = lambda status, body: sent.update(status=status, body=body)
        h.path = "/decisions"
        h._handle_get_decisions()
        return sent["body"]

    assert route({"pid": 1, "status_port": 7682, "started_at": 0})["capture_blocked"] == ["A"]
    assert "capture_blocked" not in route({"pid": 1, "started_at": 0}), "no port: no verdict, never a false one"


def test_doc_files_stay_inside_the_root(tmp_path):
    from tldrack.decisions import doc_files

    root = tmp_path / "repo"
    (root / "docs" / "deep").mkdir(parents=True)
    (root / "docs" / "a.md").write_text("a")
    (root / "docs" / "deep" / "b.md").write_text("b")
    (tmp_path / "outside.md").write_text("x")
    assert doc_files(root, []) == [], "nothing is read until the operator lists globs"
    assert [p.name for p in doc_files(root, ["docs/**/*.md", "docs/*.md"])] == ["a.md", "b.md"]
    assert doc_files(root, ["../outside.md", str(tmp_path / "outside.md")]) == []
    assert doc_files(tmp_path / "missing", ["**/*.md"]) == []
