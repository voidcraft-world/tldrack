"""The Python writers against THE shapes: schemas/src/decide.ts and schemas/src/laya-review.ts, run in node.

Every row and body here is built by the real Python code (never hand-typed), piped through node and
parsed by the Zod schemas the pages use. Node runs from `schemas/`, the package that owns them, and
strips the types itself. Skipped, with the reason, when node or the package's `zod` is absent
(`cd schemas && npm install`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from laya_bridge import envelope, files, lab_files, lab_jobs, landing, ledger, review_log, rows
from laya_bridge.ledger import Ledger
from laya_bridge.ops import ops_state
from laya_bridge.plugin import LayaPlugin
from laya_bridge.wire import clean_request

REPO = Path(__file__).resolve().parents[3]
SCHEMAS = REPO / "schemas"
SURFACE = "agent_open_work"
#: The surfaces the built ledger tags: the rack's two, and two a host names for itself (any name of the shape).
TAGGED = ("coordinator", "paste", SURFACE, "notes_triage")
NOW = datetime(2026, 10, 27, 12, 0, tzinfo=timezone.utc)
NOUL = {"type": "noul", "instructions": "Is work open?"}
CHOICE = {"type": "choice", "instructions": "Which?", "criteria": {"todo": "a task", "note": "prose"}}
SCORE = {"type": "score", "instructions": "How bad?", "criteria": ["fine", "bad"]}
EMOJI_STATE = "😀" * 5_000 + " phase D is next"  # 5,016 code points, 10,016 UTF-16 units
EMOJI_TEXT = "😀" * 2_500 + " phase 9 is not done yet"  # past the card's 4,000 only when counted as JS does
EMOJI_WHY = "😀" * 300  # laya-lab keeps 300 code points: 600 units
CONVERSATION = "019a5f3e-7b2c-7d41-9e8f-0123456789ab"  # a Codex thread id (UUIDv7); Claude's are v4
#: Candidates for the two copies of the conversation shape: decide.ts LAYA_LEDGER_CONVERSATION and ledger.py's twin.
CONVERSATION_PROBES = [
    CONVERSATION,
    CONVERSATION.upper(),
    "0b6d6c1e-2f3a-4b5c-8d9e-0123456789ab",
    "",
    "not-a-uuid",
    CONVERSATION + "\n",
    "\n" + CONVERSATION,
    CONVERSATION[:-1],
    CONVERSATION + "0",
    CONVERSATION.replace("-", ""),
    "../" + CONVERSATION,
    "019a5f3e-7b2c-7d41-9e8f-0123456789aG",
    "٠١٩a5f3e-7b2c-7d41-9e8f-0123456789ab",
]
#: Candidates for the two copies of the RECENT cursor: laya-review.ts REVIEW_RECENT_CURSOR and review_data.py's twin.
CURSOR_PROBES = [
    "n~2026-10-27T12:00:00Z~0123456789abcdef",
    "e~0.010000~2026-10-27T12:00:00Z~0123456789abcdef",
    "n~2026-10-27T12:00:00.123Z~golden:case-1",
    "e~1.000000~2026-10-27T12:00:00Z~0123456789abcdef",
    "",
    "n~",
    "n~2026-10-27T12:00:00Z",
    "n~2026-10-27T12:00:00Z~0123456789ABCDEF",
    "e~0.01~2026-10-27T12:00:00Z~0123456789abcdef",
    "e~2.000000~2026-10-27T12:00:00Z~0123456789abcdef",
    "n~2026-10-27T12:00:00Z~0123456789abcdef\n",
    "x~2026-10-27T12:00:00Z~0123456789abcdef",
    "n~2026-10-27 12:00:00Z~0123456789abcdef",
    "n~٢٠٢٦-10-27T12:00:00Z~0123456789abcdef",
]

#: A `laya-lab compare` report as it writes one (compare.py `run`): ids and numbers only.
COMPARE_REPORT = {
    "kind": "compare",
    "suite": "agent-open-work",
    "surface": SURFACE,
    "question_version": 2,
    "at": "2026-11-09T03:00:00Z",
    "run_id": "a1b2c3d4e5f6",
    "grid": [0.5],
    "eval_run": "0123456789ab",
    "device": "mps",
    "heldout_note": None,
    "voters": [
        {"name": "laya:english", "kind": "laya", "model": "english", "url": None, "live_cut": {"at": 0.7, "source": "code"}},
        {
            "name": "mica",
            "kind": "endpoint",
            "model": "mica",
            "url": "http://127.0.0.1:8010",
            "live_cut": {"at": 0.7, "source": "code"},
        },
    ],
    "sets": {
        "golden": {
            "rows": 4,
            "open": 2,
            "done": 2,
            "agreement": [],
            "p": {"case-a": {"laya:english": 0.9}},
            "voters": {
                name: {
                    "auc": 0.75,
                    "brier": 0.2,
                    "ece": 0.1,
                    "at_live": {"accuracy": 0.75, "kappa": 0.5, "threshold": 0.7},
                    "latency_ms": {"p50": 41.5, "max": 60.0} if name != "mean" else None,
                }
                for name in ("laya:english", "mica", "mean")
            },
        }
    },
}

#: A latency report as laya-lab's `latency.run` writes one (lab/tests/test_latency.py reads a real one through OPS).
LATENCY_REPORT = {
    "kind": "latency",
    "suite": "agent-open-work",
    "surface": SURFACE,
    "question_version": 2,
    "at": "2026-11-09T09:15:00Z",
    "window": {"days": 14, "since": "2026-10-26T09:15:00Z", "first": None, "last": None},
    "latency_ms": {"n": 40, "p50": 566.0, "p90": 1210.5, "p95": 1480.0, "max": 9020, "le_100": 0.025},
    "by_start": [
        {"bucket": "cold", "n": 3, "p50": 2100.0, "p90": 4000.0},
        {"bucket": "warm", "n": 37, "p50": 540.0, "p90": 900.0},
    ],
    "truncation": {"state": "ran", "states": 60, "overflowed": 1},
}

#: A `/landing` body as tldrack's `open_work.landing_body` builds one (lab/tests/test_landing_wire.py pins that).
LANDING_BODY = {
    "surface": SURFACE,
    "class": "held",
    "held": "children",
    "running": {"descendants": 0, "agents": 1, "workflows": 2, "shells": 0},
    "trace": {"ran_tests": True, "edited_files": 3, "opened_pr": False, "ended_on_question": True},
}

SCRIPT = r"""
import { readFileSync } from 'node:fs';
import * as decide from './src/decide.ts';
import * as review from './src/laya-review.ts';

const input = JSON.parse(readFileSync(0, 'utf8'));
const schemas = {
  ledger: decide.layaLedgerRowSchema, operator: review.operatorAnswerRowSchema, queue: review.reviewQueueSchema,
  item: review.reviewItemResultSchema, result: review.reviewResultSchema, ops: review.layaOpsStateSchema,
  recent: review.reviewRecentSchema,
  lab: review.layaLabResultSchema, restart: review.layaRestartResultSchema, landing: decide.layaLandingRowSchema,
  rack: review.reviewRackRequestSchema, retest_request: review.reviewRetestRequestSchema,
  retest: review.reviewRetestResultSchema,
};
const failures = [];
const refused = [];
const conversation = (input.probes?.conversation ?? []).map((value) => decide.LAYA_LEDGER_CONVERSATION.test(value));
const cursor = (input.probes?.cursor ?? []).map((value) => review.REVIEW_RECENT_CURSOR.test(value));
const wave = (input.probes?.wave ?? []).map((value) => review.REVIEW_WAVE_ID.test(value));
const surface = (input.probes?.surface ?? []).map((value) => decide.layaLedgerSurfaceSchema.safeParse(value).success);
for (const [kind, values] of Object.entries(input.accept)) {
  values.forEach((value, i) => {
    const parsed = schemas[kind].safeParse(value);
    if (!parsed.success) failures.push({ kind, i, issues: parsed.error.issues.slice(0, 3) });
  });
}
for (const [kind, values] of Object.entries(input.refuse)) {
  values.forEach((value) => refused.push(schemas[kind].safeParse(value).success === false));
}
console.log(JSON.stringify({
  failures, refused, conversation, cursor, wave, surface,
  enums: {
    tag_fields: Object.keys(decide.layaLedgerTagSchema.shape),
    untagged: decide.LAYA_LEDGER_UNTAGGED, actions: decide.layaReactionActionSchema.options,
    keep_text: decide.LAYA_LEDGER_KEEP_TEXT, rack: decide.LAYA_LEDGER_RACK_SURFACES,
    strata: review.REVIEW_STRATA, verbs: review.layaLabVerbSchema.options, job_states: review.LAYA_LAB_JOB_STATES,
    landing: { classes: decide.layaLandingClassSchema.options, held: decide.layaHeldReasonSchema.options,
               surfaces: decide.LAYA_LANDING_SURFACES, count_max: decide.LAYA_LANDING_COUNT_MAX,
               running: Object.keys(decide.layaLandingRunningSchema.shape),
               trace: Object.keys(decide.layaLandingTraceSchema.shape) },
    recent: { default: review.REVIEW_RECENT_DEFAULT, max: review.REVIEW_RECENT_MAX, orders: review.REVIEW_RECENT_ORDERS,
              offset_max: review.REVIEW_RECENT_OFFSET_MAX },
    wave_max: review.REVIEW_WAVE_MAX, rack_stratum: review.REVIEW_RACK_STRATUM,
    three_way: review.REVIEW_THREE_WAY, needs: review.REVIEW_NEEDS, retest_page_max: review.REVIEW_RETEST_PAGE_MAX,
    live: { split: review.REVIEW_LANDING_SPLIT, free: review.REVIEW_FREE_CLASSES, audiences: review.REVIEW_AUDIENCES },
  },
}));
"""


def run_node(payload: dict) -> dict:
    if shutil.which("node") is None:
        pytest.skip("node is not installed: the Zod contract cannot be checked here")
    if not (SCHEMAS / "node_modules" / "zod").is_dir():
        pytest.skip("schemas/node_modules has no zod: run `npm install` in schemas/ to check the Zod contract")
    result = subprocess.run(
        ["node", "--input-type=module", "-e", SCRIPT],
        input=json.dumps(payload),
        text=True,
        cwd=SCHEMAS,
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return json.loads(result.stdout.strip().splitlines()[-1])


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def built(tree, fake_runtime):
    """Real rows and bodies from the Python writers, over one synthetic tree."""
    checkpoint = tree.register(train_texts=["trained text"])
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    known = frozenset({"english", "voidcraft-agent-open-work"})
    questions = {"open": NOUL, "kind": CHOICE, "bad": SCORE}
    answers = {
        "answers": {
            "open": {"noul": 0.9},
            "kind": {"choice": "todo", "probabilities": {"todo": 0.7, "note": 0.3}},
            "bad": {"score": 1.0, "confidence": 0.6, "probabilities": {"0": 0.4, "1": 0.6}},
        }
    }
    ids = []
    for surface in TAGGED:
        tag = {
            "surface": surface,
            "subject": "wt-a:1",
            "question_version": 1,
            "threshold": 0.7,
            "done_threshold": 0.4,
            **({"conversation": CONVERSATION} if surface in ledger.RACK_SURFACES else {}),
        }
        ids.append(
            book.record_decision(
                ledger.clean_tag(tag, rack_ok=True),
                clean_request({"state": f"text for {surface}", "questions": questions}, known),
                answers,
                "voidcraft-agent-open-work",
                f"laya-local/voidcraft-agent-open-work@{checkpoint.name}",
                str(checkpoint),
                12.5,
            )
        )
    ids.append(
        book.record_decision(
            None,
            clean_request({"state": {"headline": "an object"}, "questions": questions}, known),
            {"answers": {}},
            "english",
            None,
            None,
            1,
            1,
        )
    )  # one abstain per type; `cold` recorded
    # Kept text past the cap in UTF-16 units (Zod's `.length`), though well under it in code points.
    ids.append(
        book.record_decision(
            {"surface": "agent_open_work"},
            clean_request({"state": EMOJI_STATE, "questions": {"open": NOUL}}, known),
            {"answers": {"open": {"noul": 0.8}}},
            "english",
            None,
            None,
            1,
        )
    )
    book.react(ids[0], "shown")
    ledger_rows = [
        row
        for surface in ledger.surfaces(tree.base)
        if (tree.base / "ledger" / f"{surface}.jsonl").exists()
        for row in read_rows(tree.base / "ledger" / f"{surface}.jsonl")
    ]

    keys = tree.unlabelled([f"review text {i}: phase {i} is next" for i in range(3)] + [EMOJI_TEXT])
    tree.write_jsonl(
        "data/agent-open-work-labels.jsonl",
        [  # an emoji `why` past 400 UTF-16 units
            {"key": key, "open": i % 2 == 0, "why": EMOJI_WHY if key == keys[3] else f"why {key[:4]}", "by": "claude-sonnet-5-5"}
            for i, key in enumerate(keys)
        ],
    )
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    headers = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173", "content-type": "application/json"}
    tree.path("data/x")

    def post(subpath, body):
        return plugin.handle_request("POST", subpath, {}, body, headers)[1]

    def get(subpath, **query):
        return plugin.handle_request("GET", subpath, {k: [v] for k, v in query.items()}, None, headers)[1]

    # Landing rows, through the in-process route as tldrack posts them: one held with its trace, one free
    # verdict that drew no ⚠️ and had no Claude transcript, and a repeat of the first, which writes nothing.
    open_work_id = ids[TAGGED.index(SURFACE)]
    landed = [
        plugin.handle_post("/landing", {}, body)
        for body in (
            {**LANDING_BODY, "decision_id": open_work_id},
            {**LANDING_BODY, "decision_id": ids[-1], "class": None, "held": None, "trace": None},
            {**LANDING_BODY, "decision_id": open_work_id},
        )
    ]
    assert [(status, body.get("duplicate")) for status, body in landed] == [(200, False), (200, False), (200, True)]
    landing_rows = read_rows(tree.base / "landing" / f"{SURFACE}.jsonl")
    queue = get("/review/queue", limit="50")
    recent = get("/review/recent", surface=SURFACE)
    item = get("/review/item", surface=SURFACE, key=keys[0])
    emoji_item = get("/review/item", surface=SURFACE, key=keys[3])
    assert emoji_item["card"]["truncated"] and emoji_item["card"]["sonnet"]["why"].endswith("…")
    agree = post(
        "/review/answer", {"surface": SURFACE, "key": keys[0], "answer": "agree", "reference_open": True, "needs": "you"}
    )
    skip = post("/review/answer", {"surface": SURFACE, "key": keys[1], "answer": "skip", "reference_open": False})
    again = post("/review/answer", {"surface": SURFACE, "key": keys[0], "answer": "flip", "reference_open": True})
    changed = post("/review/answer", {"surface": SURFACE, "key": keys[2], "answer": "flip", "reference_open": False})
    undo = post("/review/undo", {"surface": SURFACE, "answer_id": agree["answer"]["answer_id"]})
    operator_rows = read_rows(review_log.operator_path(tree.base, SURFACE))
    ops = ops_state(tree.base, fake_runtime.status(), lab_jobs.job_rows(None), lab_ok=False, now=NOW)
    # A lab queue (SCORE running, GOLDEN and COMPARE behind it) and a compare report beside the golden ones.
    lab_jobs.write_queue(
        tree.base,
        [
            {"verb": "golden", "surface": SURFACE, "at": "2026-11-09T03:00:00Z"},
            {"verb": "compare", "surface": SURFACE, "at": "2026-11-09T03:00:01Z"},
        ],
    )
    tree.path("evals/compare-agent-open-work-20261109-030000.json").write_text(json.dumps(COMPARE_REPORT))
    # A latency read and two checkpoints' stability scores: the row's LATENCY and STABLE lines.
    tree.path("evals/latency-agent-open-work-20261109-031500.json").write_text(json.dumps(LATENCY_REPORT))
    tree.write_jsonl(
        "stability/agent_open_work.jsonl",
        [
            lab_files.stability_row(
                key=f"{i:016x}",
                version=f"voidcraft-agent-open-work-2026110{day}-0900",
                digest="sha256:0123456789abcdef",
                device="mps",
                p=0.9 if (i + day) % 3 else 0.1,
                cut=0.7,
                scored_at="2026-11-09T03:20:00Z",
            )
            for day in (1, 2)
            for i in range(24)
        ],
    )
    running = {"score": {"state": "working", "exit_code": None, "session": "laya-score", "surface": SURFACE}}
    ops_queued = ops_state(
        tree.base, fake_runtime.status(), lab_jobs.job_rows(running, lab_jobs.read_queue(tree.base)), lab_ok=True, now=NOW
    )
    assert [(j["verb"], j["state"], j["position"]) for j in ops_queued["jobs"] if j["state"] != "idle"] == [
        ("score", "working", None),
        ("golden", "queued", 1),
        ("compare", "queued", 2),
    ]
    assert ops_queued["compare"][0]["sets"][0]["voters"][0]["name"] == "laya:english"
    assert (
        ops_queued["latency"][0]["cold"] == {"n": 3, "p50_ms": 2100.0}
        and ops_queued["latency"][0]["truncation"]["overflowed"] == 1
    )
    assert ops_queued["stability"][0]["pairs"][0]["n"] == 24 and ops_queued["stability"][0]["kappa_average"] is not None
    assert ops["latency"] == [] and ops["stability"] == []
    recent_after = get("/review/recent", surface=SURFACE, limit="200")
    recent_edge = get("/review/recent", surface=SURFACE, order="edge", todo="1")
    # ANSWERED recent rows, graded from the list as the page does: one OPEN with `needs`, one skip (`open` null).
    open_row, skip_row = recent["rows"]
    graded = [
        post(
            "/review/answer",
            {
                "surface": SURFACE,
                "key": open_row["key"],
                "answer": "agree",
                "reference_open": open_row["reference_open"],
                "needs": "running",
            },
        ),
        post(
            "/review/answer",
            {"surface": SURFACE, "key": skip_row["key"], "answer": "skip", "reference_open": skip_row["reference_open"]},
        ),
    ]
    recent_answered = get("/review/recent", surface=SURFACE)
    recent_older = get("/review/recent", surface=SURFACE, limit="1", offset="1")
    recent_first = get("/review/recent", surface=SURFACE, limit="1")
    recent_next = get(
        "/review/recent", surface=SURFACE, limit="1", after=recent_first["summary"]["next"]
    )  # the pager's next page
    return {
        "ledger": ledger_rows,
        "operator": operator_rows,
        "queue": [queue],
        "item": [item, emoji_item],
        "recent": [recent, recent_after, recent_edge, recent_answered, recent_older, recent_first, recent_next],
        "result": [agree, skip, again, changed, undo, post("/review/undo", {"surface": SURFACE, "answer_id": "nope"}), *graded],
        "ops": [ops, ops_queued, ops_state(None, None, [], lab_ok=True)],
        "lab": [
            envelope.ok_body("score started", session="laya-score", queued=None),
            envelope.ok_body("golden queued #1: one lab job at a time, behind laya-score", session=None, queued=1),
            envelope.error_response(409, "busy", "laya-score is still running", session=None, queued=None)[1],
        ],
        "restart": [post("/restart", {})],
        "landing": landing_rows,
    }


def test_every_python_shape_parses_as_the_schema_says(built):
    ledger_rows = built["ledger"]
    assert len(ledger_rows) == len(TAGGED) + 3, "a decision per surface, the abstain row, the emoji row, a reaction"
    emoji_row = next(row for row in ledger_rows if row.get("state_truncated"))
    assert len(EMOJI_STATE) < ledger.STATE_MAX < files.utf16_len(EMOJI_STATE), "only a UTF-16 count cuts this one"
    assert files.utf16_len(emoji_row["state"]) == ledger.STATE_MAX and EMOJI_STATE.endswith(emoji_row["state"])
    assert any(card["text"].startswith("😀") and card["truncated"] for card in built["queue"][0]["cards"])
    abstains = [d for row in ledger_rows if row["kind"] == "decision" for d in row["decisions"] if d.get("abstain")]
    assert sorted(a["type"] for a in abstains) == ["choice", "noul", "score"]
    assert {row["checkpoint_path"] for row in ledger_rows if row["kind"] == "decision"} == {
        "checkpoints/voidcraft-agent-open-work-20260102-0304",
        None,
    }
    assert [row["answer"] for row in built["operator"] if row["op"] == "answer"] == ["agree", "skip"]
    assert "open" not in built["operator"][1], "a skip has no open"
    assert built["operator"][0]["needs"] == "you" and built["result"][0]["answer"]["needs"] == "you"
    assert built["recent"][1]["summary"]["needs"] == {"you": 0, "running": 0}, "the needs answer was undone"
    # Every route names a live answer by ONE ref (review_log.answer_ref): a RECENT row's `operator` is exactly what
    # /review/answer returned for it, `key` included, or the page's safeParse refuses the whole list.
    answered = {row["key"]: row["operator"] for row in built["recent"][3]["rows"] if row["operator"] is not None}
    graded = [result["answer"] for result in built["result"][-2:]]
    assert answered == {ref["key"]: ref for ref in graded} and len(answered) == 2
    assert [(ref["open"], ref["needs"]) for ref in graded] == [(True, "running"), (None, None)]
    assert built["recent"][3]["summary"]["needs"] == {"you": 0, "running": 1} and built["recent"][3]["summary"]["skipped"] == 1
    assert built["recent"][0]["summary"]["offset"] == 0 and built["recent"][4]["summary"]["offset"] == 1
    assert [row["key"] for row in built["recent"][4]["rows"]] == [built["recent"][3]["rows"][1]["key"]]
    assert [row["key"] for row in built["recent"][6]["rows"]] == [built["recent"][3]["rows"][1]["key"]], "after = offset 1 here"
    assert built["recent"][5]["summary"]["next"] is not None and built["recent"][0]["summary"]["next"] is None

    plus = rows.utc_stamp(NOW).replace("Z", "+00:00")
    held_row, free_row = built["landing"]
    assert (held_row["class"], held_row["held"], held_row["trace"]["edited_files"]) == ("held", "children", 3)
    assert (free_row["class"], free_row["held"], free_row["trace"]) == (None, None, None)
    assert set(landing.landings_by_decision(None)) == set(), "no params tree, no rows"
    bad_reaction = {**[r for r in ledger_rows if r["kind"] == "reaction"][0], "at": NOW.isoformat()}
    paste = next(row for row in ledger_rows if row.get("surface") == "paste")
    largest = {**paste, "question_version": files.MAX_SAFE_INTEGER}  # what clean_tag still lets through
    out = run_node(
        {
            "accept": {**built, "ledger": [*ledger_rows, largest]},
            "refuse": {
                "ledger": [
                    bad_reaction,
                    {**paste, "state": "kept text on a hash-only surface"},
                    {**paste, "question_version": files.MAX_SAFE_INTEGER + 1},
                ],
                "operator": [{**built["operator"][0], "at": plus}, {**built["operator"][1], "open": None}],
                "landing": [
                    {**held_row, "held": None},
                    {**held_row, "class": "new"},
                    {**held_row, "trace": {"ran_tests": True}},
                    {**held_row, "surface": "paste"},
                    {**held_row, "running": {**held_row["running"], "agents": -1}},
                    {**held_row, "at": plus},
                ],
            },
        }
    )
    assert out["failures"] == []
    assert out["refused"] == [True] * 11, "isoformat()'s +00:00 is refused: utc_stamp is load-bearing"
    with pytest.raises(ledger.LedgerTagError):
        ledger.clean_tag({"surface": "paste", "question_version": files.MAX_SAFE_INTEGER + 1}, rack_ok=False)


def test_kappa_s_interval_laya_s_rates_and_the_operator_calibration_parse_as_the_schema_says(tree, fake_runtime):
    """P1-13's additive fields, from the real writers: `kappa_ci` and `at_cut` on the queue, `calibration` on OPS."""
    tree.register(train_texts=["trained text"])
    keys = tree.unlabelled([f"sampled turn end {i}: phase {i} is next" for i in range(24)])
    tree.labels(dict.fromkeys(keys, True))
    tree.path("data/x")
    for i, key in enumerate(keys):  # 12 open by the operator, Laya flags 10 of them and 2 of the 12 done
        tree.append_jsonl(
            "data/agent-open-work-operator.jsonl",
            review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at="2026-10-27T01:02:03Z",
                surface=SURFACE,
                key=key,
                stratum="sample",
                answer="agree" if i < 12 else "flip",
                against={"by": "claude-sonnet-5-5", "open": True},
                laya={
                    "p": 0.9 if i < 10 or i in (12, 13) else 0.1,
                    "threshold": 0.7,
                    "source": "scores",
                    "checkpoint_path": None,
                },
                decision_id=None,
                held_out=True,
            ),
        )
    report = {
        **lab_files.heldout_report_head(
            "2026-10-27T01:05:00Z", "agent-open-work", "voidcraft-agent-open-work", "operator", 44, 0.62, 0.55
        ),
        "calibration": {
            "rows": 44,
            "ece": 0.12,
            "teacher": {"rows": 40, "ece": 0.081},
            "temperature": 1.4,
            "ece_refit": 0.06,
            "reliability": [],
        },
    }
    name = lab_files.heldout_report_name("agent-open-work", "voidcraft-agent-open-work", "operator", "20261027-010500")
    tree.path(f"evals/{name}").write_text(json.dumps(report))
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    headers = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173"}
    queue = plugin.handle_request("GET", "/review/queue", {"limit": ["0"]}, None, headers)[1]
    ops = ops_state(tree.base, fake_runtime.status(), lab_jobs.job_rows(None), lab_ok=True, now=NOW)
    [stats] = queue["surfaces"]
    assert stats["operator_vs_sonnet"]["kappa_ci"] is not None and stats["operator_vs_laya"]["at_cut"]["precision"]["n"] == 12
    assert ops["sweeps"][0]["calibration"]["teacher_ece"] == 0.081
    assert run_node({"accept": {"queue": [queue], "ops": [ops]}, "refuse": {}})["failures"] == []


def test_live_sample_precision_per_landing_class_parses_as_the_schema_says(tree, fake_runtime):
    """P1-15's additive fields, from the real writers: the ledger, its `shown` reaction, the landing record and the
    operator's grades, through `/review/queue` and `/ops`."""
    texts, i = [], 0
    while len(texts) < 6:  # live_sample members, by the deck's own hash
        text = f"live turn end {i}: phase {i} is next"
        if rows.bucket(rows.state_key(text), "review-live") < 10:
            texts.append(text)
        i += 1
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    log = landing.LandingLog(lambda: tree.base, clock=lambda: NOW)
    tree.path("data/x")
    for i, text in enumerate(texts):
        decision_id = book.record_decision(
            {"surface": SURFACE, "threshold": 0.7, "question_version": 1},
            clean_request({"state": text, "questions": {"q": NOUL}}, frozenset({"english"})),
            {"answers": {"q": {"noul": 0.95}}},
            "english",
            None,
            None,
            5,
        )
        book.react(decision_id, "shown")
        log.record(
            landing.clean_landing(
                {
                    "decision_id": decision_id,
                    "surface": SURFACE,
                    "class": "new",
                    "held": None,
                    "running": {"descendants": 0, "agents": 0, "workflows": 0, "shells": 0},
                    "trace": None,
                }
            )
        )
        tree.append_jsonl(
            "data/agent-open-work-operator.jsonl",
            review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at="2026-10-27T01:02:03Z",
                surface=SURFACE,
                key=rows.state_key(text),
                stratum="live_sample",
                answer="agree" if i < 5 else "flip",
                against={"by": "english", "open": True},
                laya=None,
                decision_id=decision_id,
                held_out=True,
            ),
        )
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    headers = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173"}
    queue = plugin.handle_request("GET", "/review/queue", {"limit": ["0"]}, None, headers)[1]
    ops = ops_state(tree.base, fake_runtime.status(), lab_jobs.job_rows(None), lab_ok=True, now=NOW)
    [stats] = queue["surfaces"]
    free = stats["precision"]["free"]
    assert (free["pool"], free["n"], free["value"]) == (6, 6, 0.833) and free["lo"] is not None
    assert stats["recall_est"]["by_audience"]["free"]["n"] == 5
    assert queue["sources"]["landing"] == "ok"
    assert run_node({"accept": {"queue": [queue], "ops": [ops]}, "refuse": {}})["failures"] == []


def test_a_rack_verdict_carries_its_conversation_from_the_tag_to_the_recent_list(built):
    """agent_open_work's tag → its ledger row → every RECENT row on that row's text, as the page reads them."""
    tagged = {
        row["surface"]: row.get("conversation")
        for row in built["ledger"]
        if row["kind"] == "decision" and row.get("subject") == "wt-a:1"
    }  # one per tag surface
    assert tagged["agent_open_work"] == tagged["coordinator"] == CONVERSATION
    assert all(conversation is None for surface, conversation in tagged.items() if surface not in ledger.RACK_SURFACES)
    rows_out = [row for page in built["recent"] for row in page["rows"]]
    assert rows_out and all("conversation" in row for row in rows_out), "every row names it, null when absent"
    assert {row["conversation"] for row in rows_out if row["text"] == f"text for {SURFACE}"} == {CONVERSATION}
    untagged = {row["conversation"] for row in rows_out if row["text"].endswith("phase D is next")}
    assert untagged == {None}, "a decision tagged without one serves null"
    # Off the rack surfaces the plugin refuses the field, naming it, so the rack strips only that field.
    with pytest.raises(ledger.LedgerTagError, match="^ledger.conversation"):
        ledger.clean_tag({"surface": "paste", "conversation": CONVERSATION}, rack_ok=True)
    out = run_node(
        {
            "accept": {"ledger": [row for row in built["ledger"] if row.get("conversation")]},
            "refuse": {"ledger": [{**row, "conversation": "not-a-uuid"} for row in built["ledger"] if row.get("conversation")]},
        }
    )
    assert out["failures"] == [] and out["refused"] == [True, True]


def test_the_conversation_shape_matches_its_typescript_owner():
    out = run_node({"accept": {}, "refuse": {}, "probes": {"conversation": CONVERSATION_PROBES}})
    assert out["conversation"] == [ledger.CONVERSATION.fullmatch(value) is not None for value in CONVERSATION_PROBES]
    assert out["conversation"][:3] == [True, True, True] and not any(out["conversation"][3:])


#: Candidates for the two copies of the surface shape: decide.ts DECIDE_SURFACE_NAME and paths.py's twin.
SURFACE_PROBES = [
    "agent_open_work",
    "paste",
    "a",
    "a" * 64,
    "notes_triage_2",
    "",
    "a" * 65,
    "Paste",
    "2fast",
    "has-dash",
    "has space",
    "agent_open_work\n",
    "../ledger",
    "ａgent",
]


def test_the_surface_shape_matches_its_typescript_owner():
    out = run_node({"accept": {}, "refuse": {}, "probes": {"surface": SURFACE_PROBES}})
    assert out["surface"] == [ledger.is_surface(value) for value in SURFACE_PROBES]
    assert out["surface"][:5] == [True] * 5 and not any(out["surface"][5:])


def test_the_recent_cursor_shape_matches_its_typescript_owner():
    from laya_bridge import review_data

    out = run_node({"accept": {}, "refuse": {}, "probes": {"cursor": CURSOR_PROBES}})
    assert out["cursor"] == [review_data.RECENT_CURSOR.fullmatch(value) is not None for value in CURSOR_PROBES]
    assert out["cursor"][:4] == [True] * 4 and not any(out["cursor"][4:])


def test_the_python_enums_mirror_the_typescript_ones():
    out = run_node({"accept": {}, "refuse": {}})
    enums = out["enums"]
    assert list(ledger.TAG_FIELDS) == enums["tag_fields"], "one field list: clean_tag accepts and the row copies it"
    assert enums["untagged"] == ledger.UNTAGGED
    assert list(ledger.ACTIONS) == enums["actions"]
    assert list(ledger.KEEP_TEXT) == enums["keep_text"]
    assert list(ledger.RACK_SURFACES) == enums["rack"]
    assert list(lab_jobs.LAB_VERBS) == enums["verbs"]
    assert list(lab_jobs.JOB_STATES) == enums["job_states"]
    assert enums["landing"] == {
        "classes": list(landing.CLASSES),
        "held": list(landing.HELD_REASONS),
        "surfaces": list(landing.SURFACES),
        "count_max": landing.COUNT_MAX,
        "running": list(landing.RUNNING_KEYS),
        "trace": list(landing.TRACE_KEYS),
    }
    from laya_bridge import review, review_data

    assert list(review_data.STRATA) == enums["strata"]
    assert enums["recent"] == {
        "default": review_data.RECENT_DEFAULT,
        "max": review_data.RECENT_MAX,
        "orders": list(review_data.RECENT_ORDERS),
        "offset_max": review.RECENT_OFFSET_MAX,
    }
    assert enums["wave_max"] == review_data.WAVE_MAX and enums["rack_stratum"] == review_data.RACK_STRATUM
    assert enums["live"] == {
        "split": list(review_data.LANDING_SPLIT),
        "free": list(review_data.FREE_CLASSES),
        "audiences": list(review_data.AUDIENCES),
    }
    assert enums["three_way"] == list(review_data.THREE_WAY) and enums["needs"] == list(review_log.NEEDS)
    assert set(enums["three_way"]) == {"done", *enums["needs"]}, "DONE and the OPEN halves, nothing else"
    assert enums["retest_page_max"] == review.MAX_LIMIT // review_data.RETEST_DEAL_EVERY, (
        "the largest page's retests fit the wire"
    )


def test_a_blind_retest_its_card_its_row_and_its_undo_parse_as_the_schema_says(tree, fake_runtime):
    """P4-3's shapes, from the real writers: the queue's `retests`, `three_way` and `retest` stats, the `/review/retest`
    result and refusal, the `retest` row in the operator log, and the undo of one."""
    texts, i = [], 0
    while len(texts) < 6:  # the largest page deals five; the sixth waits
        text = f"retested turn end {i}: phase {i} is next"
        if rows.bucket(rows.state_key(text), "review-retest") < 10:
            texts.append(text)
        i += 1
    keys = tree.unlabelled(texts)
    tree.labels(dict.fromkeys(keys, True))
    tree.path("data/x")
    labels = []
    for n, key in enumerate(keys):  # a month old, written straight to the log: the routes stamp the real clock
        labels.append(
            review_log.answer_row(
                answer_id=review_log.new_answer_id(),
                at=f"2026-10-0{n + 1}T01:02:03Z",
                surface=SURFACE,
                key=key,
                stratum="rest",
                answer="agree" if n % 2 else "flip",
                against={"by": "claude-sonnet-5-5", "open": True},
                laya=None,
                decision_id=None,
                held_out=True,
                needs="you" if n % 2 else None,
            )
        )
        tree.append_jsonl("data/agent-open-work-operator.jsonl", labels[-1])
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    headers = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173", "content-type": "application/json"}

    def post(subpath, body):
        return plugin.handle_request("POST", subpath, {}, body, headers)[1]

    queue = plugin.handle_request("GET", "/review/queue", {"limit": ["50"]}, None, headers)[1]
    assert len(queue["retests"]) == 5 and queue["surfaces"][0]["retest"]["due"] == 6
    assert queue["surfaces"][0]["three_way"] == {
        "done": 3,
        "running": 0,
        "you": 3,
        "min": 20,
        "deficit": "running",
        "lean": "running",
    }
    requests = [
        {"surface": SURFACE, "key": card["key"], "of": card["of"], "open": said, **needs}
        for card, (said, needs) in zip(queue["retests"], [(True, {"needs": "you"}), (False, {}), (None, {})])
    ]
    done = [post("/review/retest", body) for body in requests]
    assert [(out["ok"], out["retest"]["open"], out["retest"]["needs"]) for out in done] == [
        (True, True, "you"),
        (True, False, None),
        (True, None, None),
    ]
    again = post("/review/retest", requests[0])
    assert (again["code"], again["retest"], again["stats"]) == ("already_retested", None, None)
    undone = post("/review/undo", {"surface": SURFACE, "answer_id": done[0]["retest"]["answer_id"]})
    after = plugin.handle_request("GET", "/review/queue", {"limit": ["50"]}, None, headers)[1]
    assert after["surfaces"][0]["retest"]["pairs"] == 1 and after["surfaces"][0]["reviewed"] == 6
    operator_rows = read_rows(review_log.operator_path(tree.base, SURFACE))
    assert [row["op"] for row in operator_rows[6:]] == ["retest", "retest", "retest", "undo"]
    said_open = operator_rows[6]
    out = run_node(
        {
            "accept": {
                "queue": [queue, after],
                "retest": [*done, again],
                "retest_request": requests,
                "result": [undone],
                "operator": operator_rows,
            },
            "refuse": {
                "operator": [
                    {**said_open, "open": False},
                    {**said_open, "of": "nope"},
                    {**said_open, "against": {"by": "sonnet", "open": True}},
                    {k: v for k, v in said_open.items() if k != "of"},
                ],
                "retest_request": [
                    {**requests[1], "needs": "you"},
                    {**requests[0], "answer": "agree"},
                    {k: v for k, v in requests[0].items() if k != "open"},
                ],
            },
        }
    )
    assert out["failures"] == []
    assert out["refused"] == [True] * 7, "needs beside DONE, a bad `of`, an answer's field, no `of`; then the request's own three"


WAVE_PROBES = [
    "w_0123456789abcdef",
    "w_0123456789ABCDEF",
    "w_0123456789abcde",
    "a_0123456789abcdef",
    "",
    "w_0123456789abcdef\n",
    "w_٠123456789abcdef",
]


def test_a_wave_card_its_answer_and_its_undo_parse_as_the_schema_says(tree, fake_runtime):
    """One session's three ⚠️ seconds apart: one card, one press, one undo, each through the Zod owner."""
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    texts, i = [], 0
    while len(texts) < 3:  # outside live_sample, which never rides a wave
        text = f"wave text {i}: waiting on the child agents"
        if rows.bucket(rows.state_key(text), "review-live") >= 10:
            texts.append(text)
        i += 1
    for text in texts:
        book.record_decision(
            ledger.clean_tag(
                {"surface": SURFACE, "subject": "wt-wave", "threshold": 0.7, "done_threshold": 0.4, "question_version": 1},
                rack_ok=True,
            ),
            clean_request({"state": text, "questions": {"open": NOUL}}, frozenset({"english"})),
            {"answers": {"open": {"noul": 0.75}}},
            "english",
            None,
            None,
            3,
        )
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    headers = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173", "content-type": "application/json"}
    tree.path("data/x")
    queue = plugin.handle_request("GET", "/review/queue", {"limit": ["50"]}, None, headers)[1]
    # The repo's own public suite deals its provisional cases too (`golden`, P1-13): the wave card is the one with a wave.
    [card] = [card for card in queue["cards"] if card["wave"]]
    assert card["wave"]["n"] == 3 and queue["surfaces"][0]["deficit"] == "open"
    riders = [m["key"] for m in card["wave"]["members"]]
    answered = plugin.handle_request(
        "POST",
        "/review/answer",
        {},
        {"surface": SURFACE, "key": card["key"], "answer": "agree", "reference_open": True, "needs": "running", "wave": riders},
        headers,
    )[1]
    undone = plugin.handle_request(
        "POST", "/review/undo", {}, {"surface": SURFACE, "answer_id": answered["answer"]["answer_id"], "wave": True}, headers
    )[1]
    operator_rows = read_rows(review_log.operator_path(tree.base, SURFACE))
    assert len(operator_rows) == 6 and len({row["wave_id"] for row in operator_rows if row["op"] == "answer"}) == 1
    out = run_node(
        {
            "accept": {"queue": [queue], "result": [answered, undone], "operator": operator_rows},
            "refuse": {"operator": [{**operator_rows[0], "wave_id": "w_nope"}]},
            "probes": {"wave": WAVE_PROBES},
        }
    )
    assert out["failures"] == [] and out["refused"] == [True]
    assert out["wave"] == [review_log.WAVE_ID.fullmatch(value) is not None for value in WAVE_PROBES]


def test_a_rack_grade_and_a_challenger_card_parse_as_the_schema_says(tree, fake_runtime):
    """The rack's in-process grade, a `challenger` card with its answer row and sides, and the door."""
    from test_rack_door import plain, shadow
    from test_registry_v2 import RIVAL, v2
    from conftest import FINE_TUNE

    v2(tree)
    book = Ledger(lambda: tree.base, clock=lambda: NOW)
    rival_text, rack_text = plain("contract rival", 1)[0], plain("contract rack", 1)[0]
    ids = [
        book.record_decision(
            ledger.clean_tag(
                {"surface": SURFACE, "subject": "vc-1", "threshold": 0.7, "done_threshold": 0.4, "question_version": 1},
                rack_ok=True,
            ),
            clean_request({"state": text, "questions": {"open": NOUL}}, frozenset({"english"})),
            {"answers": {"open": {"noul": 0.95}}},
            FINE_TUNE,
            None,
            None,
            3,
        )
        for text in (rival_text, rack_text)
    ]
    shadow(tree, ids[0], rival_text, 0.95, 0.2)
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    headers = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173", "content-type": "application/json"}
    tree.path("data/x")
    queue = plugin.handle_request("GET", "/review/queue", {"limit": ["50"], "stratum": ["challenger"]}, None, headers)[1]
    [card] = queue["cards"]
    assert card["challenger"] == {"name": RIVAL, "p": 0.2, "threshold": 0.7}
    answered = plugin.handle_request(
        "POST", "/review/answer", {}, {"surface": SURFACE, "key": card["key"], "answer": "flip", "reference_open": True}, headers
    )[1]
    assert answered["stats"]["challenger"]["with_challenger"] == 1 and answered["stats"]["door"]["min"] == 5
    grade = {"decision_id": ids[1], "open": False}
    graded = plugin.rack_grade(grade, headers)[1]
    again = plugin.rack_grade(grade, headers)[1]
    assert graded["ok"] and again["code"] == "already_answered"
    operator_rows = read_rows(review_log.operator_path(tree.base, SURFACE))
    assert [row["stratum"] for row in operator_rows] == ["challenger", "rack"]
    out = run_node(
        {
            "accept": {"queue": [queue], "result": [answered, graded, again], "operator": operator_rows, "rack": [grade]},
            "refuse": {
                "rack": [{**grade, "surface": SURFACE}, {"decision_id": "nope", "open": False}],
                "operator": [{**operator_rows[0], "challenger": {"name": RIVAL, "p": 2, "threshold": 0.7}}],
            },
        }
    )
    assert out["failures"] == [] and out["refused"] == [True, True, True]
