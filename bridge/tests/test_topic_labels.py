"""The name-label log (topic_labels.py) and its routes: what the NAME card reads and answers."""

import io
import json
import subprocess

import pytest

import tldrack.main as main_module
from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.session_names import SessionNameStore
from tldrack.session_topics import TopicResult
from tldrack.topic_labels import TopicLabels, parse_rows, resolve_topic_labels_path, view


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        self.t += 1
        return self.t


@pytest.fixture
def labels(tmp_path):
    return TopicLabels(tmp_path / "agent-rack" / "topic-labels.jsonl", clock=Clock())


# ── the log ──────────────────────────────────────────────────────────────────


def test_the_path_lives_in_the_rack_store_unless_overridden(monkeypatch, tmp_path):
    monkeypatch.delenv("TLDRACK_TOPIC_LABELS_PATH", raising=False)
    monkeypatch.setenv("TLDRACK_DATA_DIR", str(tmp_path))
    assert resolve_topic_labels_path() == tmp_path / "agent-rack" / "topic-labels.jsonl"
    monkeypatch.setenv("TLDRACK_TOPIC_LABELS_PATH", str(tmp_path / "x.jsonl"))
    assert resolve_topic_labels_path() == tmp_path / "x.jsonl"


def test_a_naming_and_its_newest_label_make_the_view(labels):
    labels.named("vc-3", "rack-names", "rename the rack rows by topic", model="haiku", profile="claude")
    labels.label("vc-3", "rack-names", "wrong", via="card", better="topic-names")
    labels.label("vc-3", "rack-names", "fits", via="card")
    got = labels.view("vc-3")
    assert got["named"]["name"] == "rack-names" and got["named"]["request"] == "rename the rack rows by topic"
    assert got["label"]["verdict"] == "fits" and got["label"]["better"] is None and got["aliases"] == []


def test_the_file_is_private(labels):
    labels.renamed("vc-1", "x")
    assert labels.path.stat().st_mode & 0o777 == 0o600
    assert labels.path.parent.stat().st_mode & 0o777 == 0o700


def test_since_hides_an_older_occupant_of_a_reused_name(labels):
    labels.named("vc-3", "old-topic", "an older session's prompt")
    labels.label("vc-3", "old-topic", "wrong", via="card")
    cut = labels._clock.t + 0.5
    assert labels.view("vc-3", since=cut)["named"] is None
    labels.named("vc-3", "new-topic", "this one")
    got = labels.view("vc-3", since=cut)
    assert got["named"]["name"] == "new-topic" and got["label"] is None


def test_a_rename_walks_back_to_the_naming_and_counts_as_a_verdict(labels):
    labels.named("vc-3", "rack-names", "prompt")
    labels.on_rename("vc-3", "docs-voice", "rack-names")
    labels.on_rename("docs-voice", "docs-voice-2", None)  # the operator's own name: no verdict on it
    got = labels.view("docs-voice-2")
    assert got["aliases"] == ["docs-voice", "vc-3"]
    assert got["named"]["session"] == "vc-3"
    assert got["label"] == {"verdict": "wrong", "better": "docs-voice", "via": "rename", "at": got["label"]["at"]}


def test_renaming_to_the_auto_name_itself_keeps_it(labels):
    labels.named("vc-4", "rack-names", "prompt")
    labels.on_rename("vc-4", "Rack-Names", "rack-names")
    assert labels.view("Rack-Names")["label"]["verdict"] == "fits"


def test_a_rename_of_a_placeholder_or_a_foreign_label_records_no_verdict(labels):
    labels.named("vc-5", "landed", "prompt")
    labels.on_rename("vc-5", "mine", None)  # still a placeholder: nothing landed to judge
    labels.on_rename("vc-6", "other", "adopted")  # no naming on record for this label
    rows = labels.rows()
    assert [row["op"] for row in rows] == ["named", "renamed", "renamed"]


def test_torn_and_foreign_lines_are_skipped():
    text = "\n".join(
        [
            '{"op":"named","session":"vc-1","name":"a","at":1}',
            '{"op":"named","session":"vc-1"',  # torn
            '{"op":"label","session":"vc-1","name":"a","verdict":"maybe","via":"card","at":2}',
            '["not","a","row"]',
            '{"op":"renamed","old":"vc-1","new":"b","at":"yesterday"}',
        ]
    )
    assert [row["at"] for row in parse_rows(text)] == [1]


def test_a_rename_loop_stops():
    rows = [{"op": "renamed", "old": "a", "new": "b", "at": 1}, {"op": "renamed", "old": "b", "new": "a", "at": 2}]
    assert view(rows, "a", None)["aliases"] == ["b"]


def test_a_failed_append_is_reported_not_raised(tmp_path):
    said = []
    blocked = tmp_path / "file"
    blocked.write_text("")
    store = TopicLabels(blocked / "under-a-file.jsonl", log=said.append)
    assert store.renamed("a", "b") is False and said


# ── the routes ───────────────────────────────────────────────────────────────


class Recorder:
    status = None
    body = None


@pytest.fixture
def bridge(tmp_path, monkeypatch, labels):
    names = SessionNameStore(tmp_path / "names.json")

    def topic_ask(prompt, taken):
        return TopicResult(label="rack-names")

    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "started_at": 0},
        control=ShutdownControl("status-only"),
        auto_names=names,
        topic_ask=topic_ask,
        topic_labels=labels,
    )
    monkeypatch.setattr(main_module, "list_tmux_sessions", lambda: {"vc-2": {}, "taken-name": {}})
    h = object.__new__(cls)
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    h.recorded = Recorder()

    def send(status, body):
        h.recorded.status, h.recorded.body = status, body

    h._send_json = send
    h.names = names
    return h


def _post(h, path, payload):
    raw = json.dumps(payload).encode()
    h.path = path
    h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    getattr(h, h.POST_ROUTES[path])()
    return h.recorded


def _get(h, query):
    h.path = f"/topic-labels?{query}"
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    h._handle_get_topic_labels()
    return h.recorded


def _hook(h, prompt):
    raw = json.dumps({"prompt": prompt}).encode()
    h.path = "/hook?session=vc-2&event=UserPromptSubmit"
    h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
    h.rfile = io.BytesIO(raw)
    h._handle_hook()


def test_a_landed_topic_is_logged_with_the_request_it_was_named_from(bridge, labels):
    _hook(bridge, "rename the rack rows by what they work on")
    assert bridge.topics.drain() == 1
    got = _get(bridge, "session=vc-2")
    assert got.status == 200 and got.body["shown"] == "rack-names"
    assert got.body["named"]["request"] == "rename the rack rows by what they work on"
    assert got.body["named"]["model"] and got.body["label"] is None


def test_the_card_labels_and_a_better_name_relabels_the_row(bridge):
    _hook(bridge, "rename the rack rows by what they work on")
    bridge.topics.drain()
    got = _post(bridge, "/topic-labels", {"session": "vc-2", "verdict": "wrong", "better": "topic-names"})
    assert got.status == 200 and got.body["relabeled"] is True and got.body["shown"] == "topic-names"
    assert got.body["label"]["better"] == "topic-names" and got.body["label"]["via"] == "card"
    assert bridge.names.get_all() == {"vc-2": "topic-names"}
    assert _post(bridge, "/topic-labels", {"session": "vc-2", "verdict": "fits"}).body["label"]["verdict"] == "fits"


def test_the_card_refuses_what_it_cannot_record(bridge):
    assert _post(bridge, "/topic-labels", {"session": "vc-2", "verdict": "fits"}).status == 409  # nothing named yet
    _hook(bridge, "rename the rack rows by what they work on")
    bridge.topics.drain()
    for payload in (
        {"session": "vc 2", "verdict": "fits"},
        {"session": "vc-2", "verdict": "maybe"},
        {"session": "vc-2", "verdict": "fits", "better": "x"},
        {"session": "vc-2", "verdict": "wrong", "x": 1},
        {"session": "vc-2", "verdict": "wrong", "since": 17},
    ):
        assert _post(bridge, "/topic-labels", payload).status == 400, payload
    assert _post(bridge, "/topic-labels", {"session": "vc-2", "verdict": "wrong", "better": "TAKEN-name"}).status == 409
    assert bridge.names.get_all() == {"vc-2": "rack-names"}


def test_a_post_needs_an_allowed_origin(bridge):
    raw = json.dumps({"session": "vc-2", "verdict": "fits"}).encode()
    bridge.path = "/topic-labels"
    bridge.headers = {"Origin": "https://evil.example", "Content-Length": str(len(raw))}
    bridge.rfile = io.BytesIO(raw)
    bridge._handle_post_topic_labels()
    assert bridge.recorded.status == 403


def test_a_rename_of_an_auto_named_row_is_a_verdict(bridge, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "", ""))
    _hook(bridge, "rename the rack rows by what they work on")
    bridge.topics.drain()
    monkeypatch.setattr(main_module, "list_tmux_sessions", lambda: {"vc-2": {}})
    assert _post(bridge, "/rename-session", {"session": "vc-2", "name": "docs-voice"}).status == 200
    got = _get(bridge, "session=docs-voice")
    assert got.body["aliases"] == ["vc-2"]
    assert got.body["label"]["verdict"] == "wrong" and got.body["label"]["better"] == "docs-voice"


def test_no_store_means_503(tmp_path):
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1234, "started_at": 0},
        control=ShutdownControl("status-only"),
    )
    h = object.__new__(cls)
    h.recorded = Recorder()
    h._send_json = lambda status, body: setattr(h.recorded, "status", status)
    assert _get(h, "session=vc-2").status == 503


# ── the contract: what the Python writes, parsed by the Zod schema the page uses ─────────

CONTRACT_SCRIPT = r"""
import { readFileSync } from 'node:fs';
import * as wire from '../schemas/src/topic-labels.ts';

const input = JSON.parse(readFileSync(0, 'utf8'));
const schemas = { view: wire.topicLabelsViewSchema, result: wire.topicLabelResultSchema };
const failures = [];
for (const [kind, values] of Object.entries(input)) {
  values.forEach((value, i) => {
    const parsed = schemas[kind].safeParse(value);
    if (!parsed.success) failures.push({ kind, i, issues: parsed.error.issues.slice(0, 3) });
  });
}
console.log(JSON.stringify(failures));
"""


def test_the_python_views_parse_with_the_page_schema(bridge, labels, tmp_path):
    import shutil
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    ui = Path(__file__).resolve().parents[2] / "ui"
    if not (ui / "node_modules" / "zod").exists():
        pytest.skip("the ui dependencies are not installed (npm install in ui/)")
    loader = "./tests/register.mjs"  # the page's own test preload: owned by ui/tests/register.mjs — see the note there.
    empty = _get(bridge, "session=vc-9").body
    _hook(bridge, "rename the rack rows by what they work on " * 40)  # past the preview: request_chars says so
    bridge.topics.drain()
    named = _get(bridge, "session=vc-2").body
    result = _post(bridge, "/topic-labels", {"session": "vc-2", "verdict": "wrong", "better": "topic-names"}).body
    fits = _post(bridge, "/topic-labels", {"session": "vc-2", "verdict": "fits", "since": 1_700_000_000}).body
    assert named["named"]["request_chars"] > len(named["named"]["request"])
    script = tmp_path / "contract.mjs"
    script.write_text(CONTRACT_SCRIPT.replace("../schemas/", f"{ui.parent.as_posix()}/schemas/"))
    run = subprocess.run(
        [node, "--import", loader, str(script)],
        cwd=ui,
        capture_output=True,
        text=True,
        input=json.dumps({"view": [empty, named], "result": [result, fits]}),
        timeout=60,
        check=False,
    )
    assert run.returncode == 0, run.stderr[-2000:]
    assert json.loads(run.stdout) == []
