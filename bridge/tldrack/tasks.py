"""Fold an agent's task-list tool calls into step progress for its snapshot row.

`POST /hook` (main.py) hands every hook payload to `fold_task_event`, which
returns the session's next task STATE. Snapshot rows then carry the summary:

    "tasks": {"done": 37, "total": 90, "current": "Wire the rack map"} | null

so a long-running agent shows how far through its plan it is. Since 2.1.268 the
task tools exist only where CLAUDE_CODE_ENABLE_TODO_TOOLS=1 (Opus 4.7 and older
have them by default) — profiles.py `CLAUDE_SETTINGS_ENV` sets it per account.
Three sources reach the fold:

    TaskCreated   hook event {task_id, task_subject, task_description} — the
                  REAL id (measured 2.1.312). The first one makes the session
                  `keyed`: from then on PreToolUse TaskCreate is ignored, and
                  the task it guessed is re-keyed onto the real id.
    TaskCompleted hook event {task_id, task_subject} — marks it completed.
    PreToolUse    TodoWrite  tool_input.todos = [{content, status, activeForm?}]
                             — the WHOLE list on every call, so it replaces.
                  TaskCreate {subject, …} — appends under a GUESSED id: Claude
                             numbers a session's tasks "1", "2", … in creation
                             order, so the Nth create is task N. Only until the
                             session is `keyed` (a CLI without TaskCreated).
                  TaskUpdate {taskId, status?, subject?} — edits task `taskId`;
                             status "deleted" removes it; an unseen id is ignored.

The guess drifts (parallel creates swap ids; a RESUMED conversation numbers past
tasks this fold never saw) — which is exactly what TaskCreated ends.

The state lives in StatusStore beside activity, in memory only. It is cleared on
SessionEnd and on a SessionStart that begins a new conversation — never on
Stop or UserPromptSubmit, because the plan outlives the turn — and kept on
SessionStart `compact`, which is the same conversation continuing.

Pure functions only (no I/O); malformed input never raises
(tests/test_tasks.py).
"""

from __future__ import annotations

from tldrack.activity import clip, squash

STATUSES = ("pending", "in_progress", "completed")
DELETED = "deleted"

#: SessionStart `source` values that continue the SAME conversation.
CONTINUING_SOURCES = frozenset({"compact"})


def _text(value: object) -> str:
    return squash(value) if isinstance(value, str) else ""


def _replace_todos(state: dict | None, todos: object) -> dict | None:
    if not isinstance(todos, list):
        return state
    items = [
        {
            "id": None,
            "subject": _text(todo.get("content")),
            "status": todo["status"] if todo.get("status") in STATUSES else "pending",
        }
        for todo in todos
        if isinstance(todo, dict)
    ]
    created = state["created"] if state else 0
    keyed = {"keyed": True} if state and state.get("keyed") else {}
    return {"items": items, "created": created, **keyed} if items or created else None


def _create_task(state: dict | None, tool_input: dict) -> dict | None:
    if state is not None and state.get("keyed"):
        return state  # TaskCreated carries the real id — see the module docstring
    subject = _text(tool_input.get("subject")) or _text(tool_input.get("description"))
    if not subject:
        return state  # Claude rejects a create with no subject, so no id is spent
    created = (state["created"] if state else 0) + 1
    items = [*(state["items"] if state else ()), {"id": str(created), "subject": subject, "status": "pending"}]
    return {"items": items, "created": created}


def _task_id(value: object) -> str | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    return value if isinstance(value, str) and value else None


def _task_created(state: dict | None, payload: dict) -> dict | None:
    """A TaskCreated event: upsert the real id. The PreToolUse create that came
    just before it guessed an id; that guess is dropped by subject, newest first,
    so a wrong guess never leaves a phantom task behind."""
    task_id = _task_id(payload.get("task_id"))
    subject = _text(payload.get("task_subject")) or _text(payload.get("task_description"))
    if task_id is None or not subject:
        return state
    items = list(state["items"]) if state else []
    if not (state and state.get("keyed")):
        for index in range(len(items) - 1, -1, -1):
            if items[index]["subject"] == subject and items[index]["id"] != task_id:
                del items[index]
                break
    existing = next((i for i, item in enumerate(items) if item["id"] == task_id), None)
    if existing is None:
        items.append({"id": task_id, "subject": subject, "status": "pending"})
    else:
        items[existing] = {**items[existing], "subject": subject}
    created = max(state["created"] if state else 0, int(task_id) if task_id.isdigit() else 0)
    return {"items": items, "created": created, "keyed": True}


def _task_completed(state: dict | None, payload: dict) -> dict | None:
    task_id = _task_id(payload.get("task_id"))
    if state is None or task_id is None:
        return state
    items = [{**item, "status": "completed"} if item["id"] == task_id else item for item in state["items"]]
    return {**state, "items": items}


def _update_task(state: dict | None, tool_input: dict) -> dict | None:
    task_id = _task_id(tool_input.get("taskId"))
    if state is None or task_id is None:
        return state
    status = tool_input.get("status")
    subject = _text(tool_input.get("subject"))
    items = []
    for item in state["items"]:
        if item["id"] == task_id:
            if status == DELETED:
                continue
            item = {
                **item,
                "status": status if status in STATUSES else item["status"],
                "subject": subject or item["subject"],
            }
        items.append(item)
    return {**state, "items": items}


def fold_task_event(event: str, payload: dict, state: dict | None) -> dict | None:
    """The session's task state after this hook event (`state` itself when the
    event says nothing about tasks). None means no tasks."""
    if event == "SessionEnd" or (event == "SessionStart" and payload.get("source") not in CONTINUING_SOURCES):
        return None
    if event == "TaskCreated":
        return _task_created(state, payload)
    if event == "TaskCompleted":
        return _task_completed(state, payload)
    if event != "PreToolUse":
        return state
    tool_input = payload.get("tool_input")
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    tool_name = payload.get("tool_name")
    if tool_name == "TodoWrite":
        return _replace_todos(state, tool_input.get("todos"))
    if tool_name == "TaskCreate":
        return _create_task(state, tool_input)
    if tool_name == "TaskUpdate":
        return _update_task(state, tool_input)
    return state


def task_progress(state: dict | None) -> dict | None:
    """The snapshot row's `tasks` field: {done, total, current}, or None with no
    tasks. `current` is the first in_progress subject, else the first pending
    one, clipped like an activity detail; None when nothing is open."""
    items = state["items"] if state else []
    if not items:
        return None
    open_items = [item for item in items if item["status"] == "in_progress"] or [
        item for item in items if item["status"] == "pending"
    ]
    current = next((item["subject"] for item in open_items if item["subject"]), None)
    return {
        "done": sum(item["status"] == "completed" for item in items),
        "total": len(items),
        "current": clip(current) if current else None,
    }
