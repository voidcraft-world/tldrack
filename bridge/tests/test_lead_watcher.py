"""The zero-token watcher (lead/watcher.py) and the wake: events on transitions only, nothing for a
busy worker or a pending check, persisted facts across a restart, one coalesced line per wake."""

from lead_fakes import CLAUDE_BOX, build, worker_appears, launch_and_start, ok, publish

from tldrack.lead.watcher import SPAWN_GRACE_SECONDS, worker_facts, observe

ID = "11111111-1111-4111-8111-111111111111"
WORKER = "22222222-2222-4222-8222-222222222222"


def step(state="working", observed=None, **kw):
    return {"state": state, "observed": observed or {}, "worker_lineage": WORKER, **kw}


def facts(row_state="busy", **worker):
    row = {"live": True, "state": row_state, "lineage": {"id": WORKER, "parent_id": ID}}
    return worker_facts(row, worker)


def run_observe(s, f, **kw):
    return observe(s, f, run_identity=ID, now=100.0, spawned_at=0.0, create_error=None, open_question=False, **kw)


def test_spawning_becomes_working_when_its_worker_appears():
    updates, events = observe(
        step("spawning", worker_lineage=None),
        facts(),
        run_identity=ID,
        now=1.0,
        spawned_at=0.0,
        create_error=None,
        open_question=False,
    )
    assert updates["state"] == "working" and updates["worker_lineage"] == WORKER and events == []


def test_a_create_error_or_a_worker_that_never_appears_fails_the_step():
    gone = worker_facts(None, {})
    assert (
        observe(
            step("spawning"), gone, run_identity=ID, now=1.0, spawned_at=0.0, create_error="bootstrap failed", open_question=False
        )[1][0][0]
        == "failed"
    )
    assert (
        observe(
            step("spawning"),
            gone,
            run_identity=ID,
            now=SPAWN_GRACE_SECONDS + 1,
            spawned_at=0.0,
            create_error=None,
            open_question=False,
        )[1][0][0]
        == "failed"
    )
    assert observe(step("spawning"), gone, run_identity=ID, now=10.0, spawned_at=0.0, create_error=None, open_question=False) == (
        {},
        [],
    )


def test_events_fire_on_transitions_only():
    f1 = facts(stage="implement")
    updates, events = run_observe(step(), f1)
    assert events == [] and updates["observed"]["stage"] == "implement"
    again = run_observe(step(observed=updates["observed"]), f1)
    assert again == ({}, []), "nothing changed: no event, no write"
    f2 = facts(stage="ready", head="a" * 40)
    updates, events = run_observe(step(observed=f1), f2)
    assert updates["state"] == "ready" and [k for k, _ in events] == ["ready"]
    f3 = facts(stage="ready", head="a" * 40, pr_url="https://github.com/o/r/pull/1", ci="pending")
    updates, events = run_observe(step("ready", observed=f2), f3)
    assert updates["state"] == "published" and [k for k, _ in events] == ["pr"], "pending CI wakes nobody"
    f4 = {**f3, "ci": "passing"}
    assert [k for k, _ in run_observe(step("published", observed=f3), f4)[1]] == ["ci"]
    f5 = {**f4, "pr_state": "merged"}
    assert [k for k, _ in run_observe(step("published", observed=f4), f5)[1]] == ["merged"]


def test_stopped_and_gone():
    busy = facts("busy", stage="implement")
    free = facts("free", stage="implement")
    assert [k for k, _ in run_observe(step(observed=busy), free)[1]] == ["stopped"]
    assert (
        run_observe(
            step(observed=busy),
            free,
        )[1]
        and not observe(step(observed=busy), free, run_identity=ID, now=1, spawned_at=0, create_error=None, open_question=True)[1]
    ), "a worker that asked and ended its turn did what it was told"
    gone = worker_facts(None, {"stage": "implement"})
    assert [k for k, _ in run_observe(step(observed=free), gone)[1]] == ["gone"]


def test_a_stranger_wearing_the_name_reads_as_gone():
    stranger = worker_facts({"live": True, "state": "free", "lineage": {"id": "x", "parent_id": ID}}, {})
    updates, events = run_observe(step(observed=facts("free")), stranger)
    assert [k for k, _ in events] == ["gone"] and updates["observed"]["worker_live"] is False


# ── the wake ────────────────────────────────────────────────────────────────


def test_one_coalesced_wake_only_when_the_lead_pane_is_safe(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1", "S2"))
    for sid in ("S1", "S2"):
        ok(service.handle("spawn", {"step": sid}, lead))
        worker_appears(service, fake, lead, sid, run_id)
    ok(service.handle("status", {}, lead))
    fake.typed.clear()
    lead_row = next(r for r in fake.rows if r["session"] == lead)
    lead_row["state"] = "busy"
    for sid in ("S1", "S2"):
        publish(fake, service.runs.step(run_id, sid)["branch"])
    service.tick()
    assert fake.typed == [], "the lead is working: nothing is typed"
    lead_row["state"] = "free"
    fake.panes[lead] = CLAUDE_BOX.replace("❯  ", "❯ half a sentence")
    service.tick()
    assert fake.typed == [], "a draft in the box holds the wake"
    fake.panes[lead] = CLAUDE_BOX
    service.tick()
    ((session, line),) = fake.typed
    assert session == lead and line.startswith("<rack-wake") and "S1 ready" in line and "S2 ready" in line
    service.tick()
    assert len(fake.typed) == 1, "each event wakes once"


def test_no_wake_for_events_the_lead_already_read(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker_appears(service, fake, lead, "S1", run_id)
    fake.typed.clear()
    next(r for r in fake.rows if r["session"] == lead)["state"] = "busy"
    publish(fake, service.runs.step(run_id, "S1")["branch"])
    service.tick()
    ok(service.handle("status", {}, lead))  # it ran status mid-turn and saw them
    next(r for r in fake.rows if r["session"] == lead)["state"] = "free"
    service.tick()
    assert fake.typed == []


def test_observed_facts_survive_a_restart(tmp_path):
    service, fake, clock = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker_appears(service, fake, lead, "S1", run_id)
    publish(fake, service.runs.step(run_id, "S1")["branch"])
    service.tick()
    before = len(service.runs.history(run_id, 500))
    rebuilt, _, _ = build(tmp_path, clock)  # same rack.sqlite, a new service
    rebuilt.bridge = service.bridge
    rebuilt.tick()
    assert len(rebuilt.runs.history(run_id, 500)) == before, "no event replayed after a restart"
