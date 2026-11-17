from tldrack.activity import MAX_DETAIL_LEN
from tldrack.main import StatusStore
from tldrack.snapshot import merge_snapshot
from tldrack.tasks import fold_task_event, task_progress


def _tool(name, tool_input):
    return {"tool_name": name, "tool_input": tool_input}


def _fold(calls, state=None):
    for event, payload in calls:
        state = fold_task_event(event, payload, state)
    return state


def _todos(*pairs):
    return _tool("TodoWrite", {"todos": [{"content": c, "status": s, "activeForm": c} for c, s in pairs]})


def _create(subject):
    return ("PreToolUse", _tool("TaskCreate", {"subject": subject, "description": "…"}))


def _update(task_id, **fields):
    return ("PreToolUse", _tool("TaskUpdate", {"taskId": task_id, **fields}))


# ── TodoWrite ──────────────────────────────────────────────────────────────


def test_todowrite_summarises_the_list():
    state = _fold([("PreToolUse", _todos(("Read", "completed"), ("Wire it", "in_progress"), ("Test", "pending")))])
    assert task_progress(state) == {"done": 1, "total": 3, "current": "Wire it"}


def test_todowrite_replaces_the_whole_list():
    state = _fold(
        [
            ("PreToolUse", _todos(("A", "pending"), ("B", "pending"), ("C", "pending"))),
            ("PreToolUse", _todos(("A", "completed"), ("B", "in_progress"))),
        ]
    )
    assert task_progress(state) == {"done": 1, "total": 2, "current": "B"}


def test_current_falls_back_to_first_pending_then_none():
    state = _fold([("PreToolUse", _todos(("A", "completed"), ("B", "pending"), ("C", "pending")))])
    assert task_progress(state)["current"] == "B"
    state = _fold([("PreToolUse", _todos(("A", "completed"), ("B", "completed")))])
    assert task_progress(state) == {"done": 2, "total": 2, "current": None}


def test_current_is_squashed_and_clipped():
    long = "word  \n" * 40
    state = _fold([("PreToolUse", _todos((long, "in_progress")))])
    current = task_progress(state)["current"]
    assert len(current) == MAX_DETAIL_LEN and current.endswith("…") and "\n" not in current


def test_empty_todowrite_clears():
    state = _fold([("PreToolUse", _todos(("A", "pending"))), ("PreToolUse", _todos())])
    assert state is None and task_progress(state) is None


# ── TaskCreate / TaskUpdate ────────────────────────────────────────────────


def test_creates_and_updates_fold_by_creation_order():
    state = _fold(
        [
            _create("Plan"),
            _create("Build"),
            _create("Ship"),
            _update("1", status="completed"),
            _update("2", status="in_progress"),
        ]
    )
    assert task_progress(state) == {"done": 1, "total": 3, "current": "Build"}


def test_deleted_removes_but_still_spends_its_id():
    state = _fold(
        [
            _create("Plan"),
            _create("Scrap"),
            _update("2", status="deleted"),
            _create("Ship"),  # task "3", not "2"
            _update("3", status="in_progress"),
        ]
    )
    assert task_progress(state) == {"done": 0, "total": 2, "current": "Ship"}


def test_update_can_rename_and_accepts_an_int_id():
    state = _fold([_create("Plan"), _update(1, subject="Plan v2", status="in_progress")])
    assert task_progress(state)["current"] == "Plan v2"


def test_unknown_ids_and_statuses_are_ignored():
    state = _fold([_create("Plan")])
    for call in (_update("9", status="completed"), _update("1", status="exploded"), _update(None)):
        assert fold_task_event(*call, state)["items"] == state["items"]
    assert fold_task_event(*_update("1", status="completed"), None) is None


def test_create_without_subject_spends_no_id():
    state = _fold([("PreToolUse", _tool("TaskCreate", {})), _create("Real"), _update("1", status="completed")])
    assert task_progress(state) == {"done": 1, "total": 1, "current": None}


# ── TaskCreated / TaskCompleted (the real ids) ─────────────────────────────


def _created(task_id, subject):
    # The measured 2.1.312 shape (scratch `claude -p` with a cat-to-file hook).
    return (
        "TaskCreated",
        {"hook_event_name": "TaskCreated", "task_id": task_id, "task_subject": subject, "task_description": "…"},
    )


def _completed(task_id, subject="…"):
    return ("TaskCompleted", {"hook_event_name": "TaskCompleted", "task_id": task_id, "task_subject": subject})


def test_task_created_keys_the_guess_onto_the_real_id():
    # A resumed conversation: the fold guesses "1", Claude says it is "7".
    state = _fold([_create("Plan"), _created("7", "Plan"), _update("7", status="in_progress")])
    assert [item["id"] for item in state["items"]] == ["7"]
    assert task_progress(state) == {"done": 0, "total": 1, "current": "Plan"}


def test_once_keyed_pretooluse_creates_are_ignored():
    # Parallel creates fire PreToolUse in any order; only TaskCreated counts.
    state = _fold(
        [
            _create("A"),
            _created("1", "A"),
            _create("C"),
            _create("B"),
            _created("2", "B"),
            _created("3", "C"),
            _completed("2"),
        ]
    )
    assert [(item["id"], item["subject"], item["status"]) for item in state["items"]] == [
        ("1", "A", "pending"),
        ("2", "B", "completed"),
        ("3", "C", "pending"),
    ]


def test_task_completed_without_a_list_or_id_is_a_no_op():
    assert fold_task_event(*_completed("1"), None) is None
    state = _fold([_created("1", "A")])
    assert fold_task_event("TaskCompleted", {"task_id": None}, state) == state
    assert fold_task_event("TaskCreated", {"task_id": "2"}, state) == state  # no subject


def test_todowrite_keeps_the_keyed_flag():
    state = _fold([_created("1", "A"), ("PreToolUse", _todos(("B", "pending")))])
    assert state["keyed"] is True


# ── lifecycle ──────────────────────────────────────────────────────────────


def test_survives_turn_boundaries_and_other_tools():
    state = _fold([_create("Plan")])
    for event, payload in (
        ("Stop", {}),
        ("UserPromptSubmit", {"prompt": "go on"}),
        ("Notification", {"message": "needs you"}),
        ("Interrupt", {}),
        ("PostToolUse", _tool("TaskCreate", {"subject": "x"})),  # Codex relays it; folding twice would double
        ("PreToolUse", _tool("Bash", {"command": "ls"})),
        ("SessionStart", {"source": "compact"}),
    ):
        assert fold_task_event(event, payload, state) is state


def test_session_start_and_end_clear():
    state = _fold([_create("Plan")])
    for source in ("startup", "clear", "resume", None):
        assert fold_task_event("SessionStart", {"source": source}, state) is None
    assert fold_task_event("SessionEnd", {"reason": "other"}, state) is None


def test_malformed_input_never_raises():
    state = _fold([_create("Plan")])
    for payload in (
        {},
        {"tool_name": "TodoWrite"},
        _tool("TodoWrite", "not a dict"),
        _tool("TodoWrite", {"todos": "nope"}),
        _tool("TodoWrite", {"todos": [None, 3, {"content": 5, "status": "weird"}]}),
        _tool("TaskCreate", {"subject": ["x"]}),
        _tool("TaskUpdate", {"taskId": {"x": 1}, "status": 7}),
        _tool(None, None),
    ):
        fold_task_event("PreToolUse", payload, state)
        fold_task_event("PreToolUse", payload, None)
    coerced = fold_task_event("PreToolUse", _tool("TodoWrite", {"todos": [None, {"content": 5, "status": "weird"}]}), None)
    assert task_progress(coerced) == {"done": 0, "total": 1, "current": None}


# ── StatusStore + snapshot ─────────────────────────────────────────────────


def _hook(store, event, payload, activity=None):
    store.set("vc-1", "busy", None, activity=activity, fold_tasks=lambda tasks: fold_task_event(event, payload, tasks))


def test_store_row_carries_tasks_and_they_survive_the_activity_clear_on_stop():
    store = StatusStore()
    assert store.get("vc-1")["tasks"] is None
    _hook(
        store,
        "PreToolUse",
        _todos(("A", "in_progress"), ("B", "pending")),
        activity={"zone": "computer", "tool": "TodoWrite", "detail": "todowrite"},
    )
    _hook(store, "Stop", {}, activity=None)
    row = store.get("vc-1")
    assert row["activity"] is None
    assert row["tasks"] == {"done": 0, "total": 2, "current": "A"}
    store.set("vc-1", "free", None)  # legacy POST /status: no fold
    assert store.get("vc-1")["tasks"] == {"done": 0, "total": 2, "current": "A"}
    _hook(store, "SessionStart", {"source": "startup"})
    assert store.get("vc-1")["tasks"] is None


def test_snapshot_row_carries_tasks_or_null():
    store = StatusStore()
    _hook(store, "PreToolUse", _tool("TaskCreate", {"subject": "Plan"}))
    store.set("vc-2", "free", None)
    rows = merge_snapshot({"vc-3": {"title": "zsh", "attached": False}}, store.list())
    assert {row["session"]: row["tasks"] for row in rows} == {
        "vc-1": {"done": 0, "total": 1, "current": "Plan"},
        "vc-2": None,
        "vc-3": None,
    }
