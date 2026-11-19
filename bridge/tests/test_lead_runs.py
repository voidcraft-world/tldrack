"""Runs and steps (lead/runs.py) through the service: NEXT, dependencies, the caps, the refusals,
a contradicted assumption reopening work, and routing that waits instead of downgrading."""

import sqlite3

import pytest
from lead_fakes import build, worker_appears, launch_and_start, ok, publish

from tldrack import rack_db
from tldrack.lead.runs import LeadError, deps_met, kind_token, next_actions, spawn_cap


def test_a_lead_must_be_launched_as_one(tmp_path):
    service, fake, _ = build(tmp_path)
    fake.row("vc-1", "77777777-7777-4777-8777-777777777777")
    status, body = service.handle("start", {"title": "T", "plan_text": "x"}, "vc-1")
    assert status == 403 and "LEAD" in body["error"]


def test_steps_validate_and_deps_must_exist(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    assert service.handle("step", {"id": "S2", "title": "t", "brief": "b", "after": ["S9"]}, lead)[0] == 400
    assert service.handle("step", {"id": "S1", "title": "t", "brief": "b"}, lead)[0] == 409
    ok(service.handle("step", {"id": "S2", "title": "t", "brief": "b", "after": ["S1"]}, lead))
    assert service.runs.run(run_id)["spawn_cap"] == spawn_cap(2)


def test_a_step_kind_is_change_or_report_and_the_old_names_still_read(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("step", {"id": "S2", "title": "t", "brief": "b", "kind": "report"}, lead))
    # Legacy (step 6.8): `scout` and `ship` were the kinds' names before 0.1.0.
    ok(service.handle("step", {"id": "S3", "title": "t", "brief": "b", "kind": "scout"}, lead))
    ok(service.handle("step", {"id": "S4", "title": "t", "brief": "b"}, lead))
    stored = {sid: service.runs.step(run_id, sid)["kind"] for sid in ("S2", "S3", "S4")}
    assert stored == {"S2": "report", "S3": "report", "S4": "change"}
    status, body = service.handle("step", {"id": "S5", "title": "t", "brief": "b", "kind": "epic"}, lead)
    assert status == 400 and body["error"] == "kind must be change or report"
    assert [kind_token(t) for t in ("change", "ship", "report", "scout", None)] == ["change", "change", "report", "report", None]


def test_a_rack_db_from_before_the_rename_is_migrated_in_place(tmp_path):
    """Legacy (step 6.8): a V2 file keeps its runs, steps and questions under the new names, in one transaction."""
    path = tmp_path / "rack.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    for script in rack_db.MIGRATIONS[:2]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 2")
    conn.execute(
        "INSERT INTO lead_runs (id, title, lead_identity, parallel_max, spawn_cap, state, created_at, crew_model)"
        " VALUES ('R0001-abcd', 't', 'x', 2, 4, 'open', 0, 'opus')"
    )
    # step 6.8: the rows as a V2 file holds them
    for step_id, kind in (("S1", "ship"), ("S2", "scout")):
        conn.execute(
            "INSERT INTO lead_steps (run_id, step_id, ord, title, brief, kind, lane, after, state, crew_session, observed)"
            " VALUES ('R0001-abcd', ?, 1, 't', 'b', ?, 'claude', '[]', 'working', 'w-1', '{\"crew_live\": true}')",
            (step_id, kind),
        )
    conn.execute(  # step 6.8: the questions' table before the rename
        "INSERT INTO crew_questions VALUES ('q1', 'R0001-abcd', 'S1', 'w-1', 'why?', 1, NULL, 0, 'open', NULL, NULL, NULL)"
    )
    conn.close()
    db = rack_db.RackDb(path)
    with db.read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == rack_db.SCHEMA_VERSION
        steps = [dict(r) for r in conn.execute("SELECT step_id, kind, worker_session, observed FROM lead_steps ORDER BY step_id")]
        assert steps == [
            {"step_id": "S1", "kind": "change", "worker_session": "w-1", "observed": '{"worker_live": true}'},
            {"step_id": "S2", "kind": "report", "worker_session": "w-1", "observed": '{"worker_live": true}'},
        ]
        assert conn.execute("SELECT worker_model FROM lead_runs").fetchone()[0] == "opus"
        assert conn.execute("SELECT text FROM worker_questions").fetchone()[0] == "why?"


def test_a_change_dependency_waits_for_the_merge(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("step", {"id": "S2", "title": "t", "brief": "b", "after": ["S1"]}, lead))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    status, body = service.handle("spawn", {"step": "S2"}, lead)
    assert status == 409 and "waits for S1" in body["error"]
    worker_appears(service, fake, lead, "S1", run_id)
    branch = service.runs.step(run_id, "S1")["branch"]
    publish(fake, branch)
    fake.worktree.worker(branch)["pr_state"] = "merged"
    service.tick()
    ok(service.handle("spawn", {"step": "S2"}, lead))
    assert fake.worktree.creates[-1]["base"] is not None, "a fresh worker branches from origin/main"


def test_parallel_cap_parked_steps_and_live_worker_are_refused(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1", "S2", "S3"), parallel=2)
    ok(service.handle("spawn", {"step": "S1"}, lead))
    ok(service.handle("spawn", {"step": "S2"}, lead))
    assert service.handle("spawn", {"step": "S3"}, lead)[0] == 429
    service.inbox.ask(source="lead", run_id=run_id, title="Which?", detail="", blocking=True, steps=["S3"])
    view = ok(service.handle("status", {}, lead))
    assert not any(a["step"] == "S3" and a["verb"] == "spawn" for a in view["next"])
    assert "PARKED" in view["text"]


def test_spawn_routes_lanes_and_never_downgrades(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    ok(service.handle("step", {"id": "A1", "title": "art", "brief": "b", "lane": "codex"}, lead))
    body = ok(service.handle("spawn", {"step": "A1"}, lead))
    assert body["profile"] == "codex"
    assert "prompt" not in fake.worktree.creates[-1], "Codex gets its brief from the typist, not the trust list"
    assert service.typist.pending(body["session"])[0]["kind"] == "brief"
    ok(service.handle("step", {"id": "C1", "title": "logic", "brief": "b"}, lead))
    for summary in fake.summaries:
        if summary["kind"] == "claude":
            summary["cli_found"] = False
    status, body = service.handle("spawn", {"step": "C1"}, lead)
    assert status == 503 and body["code"] == "route_wait"
    assert service.runs.step(run_id, "C1")["state"] == "pending", "it waits; nothing lesser was picked"
    (row,) = [d for d in service.inbox.open_rows(run_id) if d["source"] == "routing"]
    assert row["steps"] == ["C1"]


def test_a_contradicted_assumption_reopens_and_next_says_re_brief(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker = worker_appears(service, fake, lead, "S1", run_id)
    key = ok(
        service.handle(
            "escalate", {"title": "Which table?", "detail": "d", "steps": ["S1"], "blocking": False, "assume": "page_cache"}, lead
        )
    )["key"]
    assert (
        service.handle(
            "escalate", {"title": "Which table?", "detail": "d", "steps": ["S1"], "blocking": False, "assume": "page_cache"}, lead
        )[1]["outcome"]
        == "existing"
    )
    result = service.inbox.answer(key, "text", "a new table")
    assert result["reopened"] == ["S1"]
    view = ok(service.handle("status", {}, lead))
    assert ("tell", "S1") in [(a["verb"], a["step"]) for a in view["next"]]
    ok(service.handle("tell", {"step": "S1", "text": "Use a new table (D answer).", "from": key}, lead))
    assert service.runs.step(run_id, "S1")["state"] == "working"
    assert fake.typed[-1][0] == worker and fake.typed[-1][1].startswith("<lead-message")


def test_worker_questions_reach_the_lead_and_escalate_once(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker = worker_appears(service, fake, lead, "S1", run_id)
    assert service.handle("ask", {"text": "404 or 410?"}, worker)[0] == 400, "blocking or an assumption"
    qid = ok(service.handle("ask", {"text": "404 or 410?", "blocking": True}, worker))["question"]
    assert ("answer", "S1") in [(a["verb"], a["step"]) for a in ok(service.handle("status", {}, lead))["next"]]
    first = ok(service.handle("escalate", {"question": qid, "blocking": True}, lead))
    assert first["outcome"] == "opened" and service.runs.question(qid)["state"] == "escalated"
    assert "S1" in service.inbox.parked_steps(run_id)
    assert service.handle("ask", {"text": "x", "blocking": True}, lead)[0] == 403, "a lead is not a worker"


def test_end_needs_every_step_finished(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    assert service.handle("end", {}, lead)[0] == 409
    ok(service.handle("abandon", {"step": "S1", "reason": "superseded"}, lead))
    assert ("end", None) in [(a["verb"], a["step"]) for a in ok(service.handle("status", {}, lead))["next"]]
    ok(service.handle("end", {"report": "done"}, lead))


def test_spawn_cap_stops_a_looping_lead(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    for _ in range(spawn_cap(1)):
        ok(service.handle("spawn", {"step": "S1"}, lead))
        service.runs.update_step(run_id, "S1", state="failed")
    assert service.handle("spawn", {"step": "S1"}, lead)[0] == 429


def test_next_actions_is_pure():
    run = {"parallel_max": 1, "spawn_cap": 10, "spawns": 0}
    steps = [
        {"step_id": "S1", "state": "pending", "kind": "ship", "after": [], "branch": None},
        {"step_id": "S2", "state": "pending", "kind": "ship", "after": [], "branch": None},
    ]
    acts = next_actions(run, steps, live={}, workers={}, parked=set(), questions=[])
    assert [(a["verb"], a["step"]) for a in acts] == [("spawn", "S1")], "one slot, one spawn"
    assert deps_met({"after": ["X"]}, {}, set()) is False
    with pytest.raises(LeadError):
        raise LeadError("x", 409)


def test_every_worker_starts_in_auto_mode_claude_codex_and_follow_up(tmp_path):
    # The operator no longer switches each worker by hand: /create asks for `auto` on every door the run
    # spawns through — a fresh Claude worker, a Codex worker, and a follow-up into an existing tree.
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    assert fake.worktree.creates[-1]["permission_mode"] == "auto"
    session = worker_appears(service, fake, lead, "S1", run_id)
    ok(service.handle("step", {"id": "A1", "title": "art", "brief": "b", "lane": "codex"}, lead))
    assert ok(service.handle("spawn", {"step": "A1"}, lead))["profile"] == "codex"
    assert fake.worktree.creates[-1]["permission_mode"] == "auto"
    fake.drop(session)
    ok(service.handle("spawn", {"step": "S1", "follow_up": True}, lead))
    follow_up = fake.worktree.creates[-1]
    assert follow_up["existing"] is True and follow_up["permission_mode"] == "auto"


def test_a_worktree_plugin_that_cannot_start_auto_is_a_restart_not_a_default_mode_worker(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    real = fake.worktree.handle_get

    def old_plugin(path, query):
        status, body = real(path, query)
        if path == "/state":
            body["capabilities"].pop("permission_modes")
        return status, body

    fake.worktree.handle_get = old_plugin
    status, body = service.handle("spawn", {"step": "S1"}, lead)
    assert status == 503 and "restart the bridge" in body["error"]
    assert fake.worktree.creates == [], "nothing was created in the default mode"
