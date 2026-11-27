"""A Codex worker gets its brief (pane_typist.py § WHEN IT IS SAFE), pinned to the worker it was meant for,
and a stuck one shows on `lead status`. Also `lead cap`, the run's live-worker cap changed mid-run."""

import uuid

from lead_fakes import build, launch_and_start, ok

from tldrack.lead.runs import MAX_PARALLEL
from tldrack.pane_typist import COMPOSER

IDLE_CODEX = f"  >_ OpenAI Codex\n\n› {COMPOSER}\n  GPT-5-Codex high · lead/r1 · No changes\n  ? for shortcuts\n"
WORKING_CODEX = f"• Working (4s • esc to interrupt)\n\n› {COMPOSER}\n  ? for shortcuts\n"


def _codex_step(service, fake, lead, step_id="A1"):
    ok(service.handle("step", {"id": step_id, "title": "art", "brief": "b", "lane": "codex"}, lead))
    return ok(service.handle("spawn", {"step": step_id}, lead))["session"]


def _codex_worker(fake, session, parent, *, identity=None, pane=IDLE_CODEX, state="unknown"):
    """A fresh Codex: no hook yet, so the row reads `unknown`."""
    fake.panes[session] = pane
    return fake.row(session, identity or str(uuid.uuid4()), parent=parent, state=state, profile="codex")


def test_a_fresh_codex_worker_on_unknown_gets_its_brief_once(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    names = []
    service.bridge.label_worker = lambda session, prompt: names.append((session, prompt))
    session = _codex_step(service, fake, lead)
    service.tick()
    assert names == []
    assert fake.typed == [], "no worker yet: nothing to type into"
    _codex_worker(fake, session, service.runs.run(run_id)["lead_identity"])
    service.tick()
    assert len(fake.typed) == 1 and fake.typed[0][0] == session
    assert "lead brief" in fake.typed[0][1] and fake.typed[0][1].startswith("<lead-message")
    service.tick()
    assert len(fake.typed) == 1 and service.typist.pending() == []
    assert names == [(session, "art\nb")]


def test_a_working_codex_holds_the_brief_and_status_names_why(tmp_path):
    service, fake, clock = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    session = _codex_step(service, fake, lead)
    _codex_worker(fake, session, service.runs.run(run_id)["lead_identity"], pane=WORKING_CODEX)
    service.tick()
    assert fake.typed == []
    text = ok(service.handle("status", {}, lead))["text"]
    assert "waiting" not in text, "under a minute is not news"
    clock.now += 90
    service.tick()
    text = ok(service.handle("status", {}, lead))["text"]
    (line,) = [row for row in text.splitlines() if row.strip().startswith("A1")]
    assert "brief waiting 1m: a turn is running in the pane" in line


def test_a_name_worn_by_another_agent_never_gets_the_brief(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    session = _codex_step(service, fake, lead)
    _codex_worker(fake, session, parent="someone-elses-lead")
    service.tick()
    assert fake.typed == [], "not parented to this run: never proven, never typed"
    (item,) = service.typist.pending(session)
    assert item["lineage"] is None and item["hold"] == "the worker is not proven yet"


def test_a_pinned_brief_is_dropped_when_the_name_comes_back_on_another_agent(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    session = _codex_step(service, fake, lead)
    _codex_worker(fake, session, service.runs.run(run_id)["lead_identity"], pane=WORKING_CODEX)
    service.tick()
    assert service.typist.pending(session)[0]["lineage"] == service.runs.step(run_id, "A1")["worker_lineage"]
    _codex_worker(fake, session, parent="someone-elses-lead", pane=IDLE_CODEX)
    service.tick()
    assert [t for t in fake.typed if t[0] == session] == [] and service.typist.pending() == []


def test_a_respawn_supersedes_the_brief_the_last_attempt_never_delivered(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    session = _codex_step(service, fake, lead)
    service.runs.update_step(run_id, "A1", state="failed")
    ok(service.handle("spawn", {"step": "A1"}, lead))
    assert len(service.typist.pending(session)) == 1, "one brief, not two"


def test_a_brief_for_a_step_no_longer_open_is_dropped(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    session = _codex_step(service, fake, lead)
    service.runs.update_step(run_id, "A1", state="abandoned")
    service.tick()
    assert service.typist.pending(session) == []


def test_a_tell_to_a_codex_worker_still_waits_for_free(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=())
    session = _codex_step(service, fake, lead)
    _codex_worker(fake, session, service.runs.run(run_id)["lead_identity"])
    service.tick()
    ok(service.handle("tell", {"step": "A1", "text": "use 410"}, lead))
    service.tick()
    assert len(fake.typed) == 1, "the brief went; the tell holds on unknown"
    assert service.typist.pending(session)[0]["hold"] == "the agent is unknown"
    fake.row(
        session, fake.rows[-1]["lineage"]["id"], parent=service.runs.run(run_id)["lead_identity"], state="free", profile="codex"
    )
    service.tick()
    assert fake.typed[-1][1].endswith("use 410")


# ── lead cap ─────────────────────────────────────────────────────────────────


def test_cap_is_the_leads_raises_and_lowers_and_kills_nobody(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1", "S2", "S3"), parallel=2)
    ok(service.handle("spawn", {"step": "S1"}, lead))
    ok(service.handle("spawn", {"step": "S2"}, lead))
    assert service.handle("spawn", {"step": "S3"}, lead)[0] == 429
    assert ok(service.handle("cap", {"parallel": 3}, lead))["parallel"] == 3
    ok(service.handle("spawn", {"step": "S3"}, lead))
    body = ok(service.handle("cap", {"parallel": 1}, lead))
    assert body["worker_live"] == 3 and fake.killed == [], "lowering kills nobody"
    text = ok(service.handle("status", {}, lead))["text"]
    assert "caps: 3/1 workers live (over the cap: no new spawn until 3 close)" in text
    kinds = [e["kind"] for e in service.runs.history(run_id, 50)]
    assert kinds.count("cap") == 2


def test_cap_is_bounded_and_lead_only(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    for bad in (0, MAX_PARALLEL + 1, "4", True, None):
        assert service.handle("cap", {"parallel": bad}, lead)[0] == 400, bad
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker = service.runs.step(run_id, "S1")["worker_session"]
    fake.row(worker, str(uuid.uuid4()), parent=service.runs.run(run_id)["lead_identity"])
    service.tick()
    status, body = service.handle("cap", {"parallel": 5}, worker)
    assert status == 409 and "leads no open run" in body["error"]
    assert service.runs.run(run_id)["parallel_max"] == 3
