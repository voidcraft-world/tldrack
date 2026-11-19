"""Work personas: the store, what each prompt carries, and the routes that move it."""

import json

import pytest

from tldrack.icons import SessionIconStore
from tldrack.main import ShutdownControl, StatusStore, build_status_handler
from tldrack.models import SessionModelStore
from tldrack.personas import (
    CLEARED_TEXT,
    PERSONAS,
    PERSONAS_BY_ID,
    SessionPersonaStore,
    parse_persona_file,
    prompt_hook_output,
)


def test_catalog_ids_are_unique_and_cards_never_carry_the_instruction_text():
    assert len(PERSONAS_BY_ID) == len(PERSONAS)
    for persona in PERSONAS:
        assert "text" not in persona.card()


def test_a_persona_rides_every_prompt_until_cleared_then_one_cleared_note(tmp_path):
    store = SessionPersonaStore(tmp_path / "p.json")
    assert store.context_for_prompt("a") is None
    store.set("a", "sprawl")
    first, second = store.context_for_prompt("a"), store.context_for_prompt("a")
    assert first == second and "SPRAWL" in first and PERSONAS_BY_ID["sprawl"].text in first
    store.set("a", None)
    assert store.context_for_prompt("a") == CLEARED_TEXT
    assert store.context_for_prompt("a") is None


def test_picks_persist_but_what_was_heard_does_not(tmp_path):
    path = tmp_path / "p.json"
    SessionPersonaStore(path).set("a", "ship")
    reloaded = SessionPersonaStore(path)
    assert reloaded.get("a") == "ship"
    reloaded.set("a", None)
    assert reloaded.context_for_prompt("a") is None  # a restart owes no cleared note


def test_rename_carries_the_pick_and_forget_owes_no_note(tmp_path):
    store = SessionPersonaStore(tmp_path / "p.json")
    store.set("a", "steady")
    store.context_for_prompt("a")
    store.rename("a", "b")
    assert store.get("b") == "steady" and store.get("a") is None
    store.forget("b")
    assert store.context_for_prompt("b") is None


def test_unknown_ids_are_refused_and_dropped_on_load(tmp_path):
    store = SessionPersonaStore(tmp_path / "p.json")
    with pytest.raises(ValueError):
        store.set("a", "yolo")
    text = json.dumps({"version": 1, "personas": {"a": "ship", "b": "retired", "bad name": "ship"}})
    assert parse_persona_file(text) == {"a": "ship"}


def test_hook_output_shape():
    assert prompt_hook_output(None) == {}
    assert prompt_hook_output("x") == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": "x"}}


class Recorder:
    status = None
    body = None


@pytest.fixture
def handler(tmp_path):
    cls = build_status_handler(
        store=StatusStore(),
        icon_store=SessionIconStore(tmp_path / "icons.json"),
        model_store=SessionModelStore(tmp_path / "models.json"),
        default_session="tldrack",
        bridge_info={"pid": 1, "started_at": 0},
        control=ShutdownControl("status-only"),
        personas=SessionPersonaStore(tmp_path / "personas.json"),
    )
    h = object.__new__(cls)
    rec = Recorder()

    def _send_json(status, body):
        rec.status, rec.body = status, body

    h._send_json = _send_json
    h.recorded = rec
    h.headers = {"Origin": "http://127.0.0.1:5173"}
    return h


def _post(h, path, body):
    raw = json.dumps(body).encode()
    h.path = path
    h.headers = {"Origin": "http://127.0.0.1:5173", "Content-Length": str(len(raw))}
    h.rfile = __import__("io").BytesIO(raw)
    getattr(h, h.POST_ROUTES[path.split("?")[0]])()
    return h.recorded


def test_routes_pick_then_the_prompt_hook_answers_with_it(handler):
    assert _post(handler, "/persona", {"session": "codex-row", "persona": "ship"}).status == 200
    handler.path = "/persona?session=codex-row"
    handler._handle_get_persona()
    assert handler.recorded.body["persona"] == "ship"
    assert [card["id"] for card in handler.recorded.body["personas"]] == [p.id for p in PERSONAS]
    answer = _post(handler, "/hook?session=codex-row&event=UserPromptSubmit", {"prompt": "go"}).body
    assert "SHIP" in answer["hookSpecificOutput"]["additionalContext"]
    assert "hookSpecificOutput" not in _post(handler, "/hook?session=codex-row&event=PreToolUse", {}).body


@pytest.mark.parametrize("body", [{"session": "codex-row", "persona": "yolo"}, {"session": "a b", "persona": None}])
def test_set_refuses_bad_input(handler, body):
    assert _post(handler, "/persona", body).status == 400
