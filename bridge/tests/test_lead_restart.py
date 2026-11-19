"""Restart: end the lead mid-run, start a fresh one on the same run, and it resumes without
re-spawning a finished step — because the run lives in rack.sqlite, not in the lead's conversation."""

from lead_fakes import build, worker_appears, launch_and_start, ok, publish


def test_a_fresh_lead_resumes_without_respawning_finished_steps(tmp_path):
    service, fake, clock = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1", "S2", "S3", "S4", "S5"), parallel=3)
    identity = service.runs.run(run_id)["lead_identity"]

    # S1 and S2 ship and close; S3 is working; S4 and S5 have not started.
    for sid in ("S1", "S2"):
        ok(service.handle("spawn", {"step": sid}, lead))
        worker_appears(service, fake, lead, sid, run_id)
        publish(fake, service.runs.step(run_id, sid)["branch"])
        service.tick()
        ok(service.handle("close", {"step": sid}, lead))
    ok(service.handle("spawn", {"step": "S3"}, lead))
    s3_worker = worker_appears(service, fake, lead, "S3", run_id)
    service.handle("status", {}, lead)  # the old lead read everything up to here
    spawns_before = len(fake.worktree.creates)
    publish(fake, service.runs.step(run_id, "S3")["branch"], ci="pending")  # S3 reports ready while nobody leads
    fake.worktree.worker(service.runs.step(run_id, "S3")["branch"]).update(pr_url=None, pr_head=None)

    # The lead dies mid-run.
    typed_before = len(fake.typed)
    fake.drop(lead)
    service.tick()
    view = service.state_view()["runs"][0]
    assert view["lead_live"] is False, "AGENT RACK › LEAD offers RESUME"
    assert len(fake.typed) == typed_before, "nobody to wake: the events wait in the store"

    # RESUME: a fresh lead, wearing the run's identity, so the workers are still its children.
    resumed = ok(service.handle("launch", {"profile": "a", "resume": run_id}, None))
    assert resumed["run"] == run_id and resumed["session"] != lead
    assert fake.spawned[-1]["lineage"]["id"] == identity
    assert "Resume LEAD run" in fake.spawned[-1]["prompt"]
    status, body = service.handle("launch", {"profile": "a", "resume": run_id}, None)
    assert status == 409, "a second live lead for one run is refused"

    new_lead = resumed["session"]
    status_body = ok(service.handle("status", {}, new_lead))
    nxt = [(a["verb"], a["step"]) for a in status_body["next"]]
    assert ("spawn", "S4") in nxt and ("spawn", "S5") in nxt
    assert not any(step in ("S1", "S2", "S3") and verb.startswith("spawn") for verb, step in nxt)
    assert "S3 ready" in status_body["text"], "what happened while nobody led is waiting in status"
    assert "S3 ready" not in ok(service.handle("status", {}, new_lead))["text"], "and is printed once"

    for sid in ("S1", "S2", "S3"):
        status, body = service.handle("spawn", {"step": sid}, new_lead)
        assert status == 409, (sid, body)
    assert len(fake.worktree.creates) == spawns_before, "nothing finished or live was spawned again"
    ok(service.handle("spawn", {"step": "S4"}, new_lead))
    ok(service.handle("spawn", {"step": "S5"}, new_lead))
    status, body = service.handle("spawn", {"step": "S4"}, new_lead)
    assert status == 409, "a spawn in flight cannot be claimed twice"
    assert service.role_of(s3_worker) == ("worker", run_id, "S3")


def test_adopt_lets_a_fresh_lead_take_an_orphaned_run(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    fake.drop(lead)
    fresh = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    status = ok(service.handle("status", {}, fresh))
    assert status["run"] is None and status["orphans"] == [run_id]
    ok(service.handle("adopt", {"run": run_id}, fresh))
    assert ok(service.handle("status", {}, fresh))["run"] == run_id
    other = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    assert service.handle("adopt", {"run": run_id}, other)[0] == 409, "the run has a live lead again"
