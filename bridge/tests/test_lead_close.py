"""The verified close (lead/close.py): the truth table, then the route doing it for real."""

from dataclasses import replace

import pytest
from lead_fakes import H1, H2, build, crew_appears, launch_and_start, ok, publish

from tldrack.lead.close import CloseFacts, decide_close

LEAD_ID = "11111111-1111-4111-8111-111111111111"
CREW_ID = "22222222-2222-4222-8222-222222222222"

PASSING = CloseFacts(
    run_open=True,
    step_known=True,
    kind="ship",
    crew_session="wt-x",
    recorded_lineage=CREW_ID,
    run_identity=LEAD_ID,
    live_lineage=CREW_ID,
    live_parent=LEAD_ID,
    live=True,
    state="free",
    open_questions=[],
    children_running=0,
    tree_clean=True,
    claim_head=H1,
    claim_published=True,
    head_published=True,
)


def test_all_checks_true_closes():
    assert decide_close(PASSING) == (None, "closed")


@pytest.mark.parametrize(
    "change, code",
    [
        ({"run_open": False}, "unknown_step"),
        ({"crew_session": None}, "unknown_step"),
        ({"live_lineage": "33333333-3333-4333-8333-333333333333"}, "not_the_crew"),
        ({"recorded_lineage": None}, "not_the_crew"),
        ({"live_parent": "44444444-4444-4444-8444-444444444444"}, "not_crew_of_run"),
        ({"state": "busy"}, "busy"),
        ({"state": "background"}, "background"),
        ({"state": "attention"}, "attention"),
        ({"state": None}, "unknown"),
        ({"open_questions": ["Q-1"]}, "open_question"),
        ({"children_running": 2}, "children_running"),
        ({"tree_clean": False}, "dirty"),
        ({"tree_clean": None}, "git_error"),
        ({"claim_head": None}, "no_claim"),
        ({"claim_published": False}, "claim_unpublished"),
        ({"claim_published": None}, "git_error"),
        ({"head_published": False}, "unpublished_commits"),
    ],
)
def test_each_check_alone_false_refuses_with_its_reason(change, code):
    assert decide_close(replace(PASSING, **change))[0] == code


def test_scout_closes_on_a_saved_report_and_never_needs_a_claim():
    scout = replace(PASSING, kind="scout", claim_head=None, claim_published=None, head_published=None)
    assert decide_close(replace(scout, report_ok=False))[0] == "no_report"
    assert decide_close(replace(scout, report_ok=True)) == (None, "closed")


def test_a_gone_crew_is_judged_on_its_work_alone():
    gone = replace(PASSING, live=False, live_lineage=None, live_parent=None, state=None, children_running=3)
    assert decide_close(gone) == (None, "closed")
    assert decide_close(replace(gone, open_questions=["D-1"]))[0] == "open_question"
    assert decide_close(replace(gone, tree_clean=False))[0] == "dirty"


# ── the route ───────────────────────────────────────────────────────────────


@pytest.fixture
def published(tmp_path):
    service, fake, clock = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    crew = crew_appears(service, fake, lead, "S1", run_id)
    branch = service.runs.step(run_id, "S1")["branch"]
    publish(fake, branch)
    service.tick()
    assert service.runs.step(run_id, "S1")["state"] == "published"
    return service, fake, lead, run_id, crew, branch


def test_the_route_kills_only_after_every_check_passes(published):
    service, fake, lead, run_id, crew, branch = published
    fake.git.clean = False
    status, body = service.handle("close", {"step": "S1"}, lead)
    assert status == 409 and body["code"] == "dirty" and fake.killed == []
    fake.git.clean = True
    fake.git.tips[branch] = H2  # the remote moved somewhere that does not contain the claim
    assert service.handle("close", {"step": "S1"}, lead)[1]["code"] == "claim_unpublished"
    fake.git.tips[branch] = H1
    body = ok(service.handle("close", {"step": "S1"}, lead))
    assert body["killed"] is True and fake.killed == [crew]
    step = service.runs.step(run_id, "S1")
    assert step["state"] == "closed" and step["close_facts"]["claim"] == H1


def test_an_open_question_or_a_busy_crew_blocks_the_close(published):
    service, fake, lead, run_id, crew, _ = published
    fake.row(
        crew, service.runs.step(run_id, "S1")["crew_lineage"], parent=service.runs.run(run_id)["lead_identity"], state="busy"
    )
    assert service.handle("close", {"step": "S1"}, lead)[1]["code"] == "busy"
    fake.row(crew, service.runs.step(run_id, "S1")["crew_lineage"], parent=service.runs.run(run_id)["lead_identity"])
    service.runs.ask(run_id, "S1", crew, "which?", blocking=True)
    assert service.handle("close", {"step": "S1"}, lead)[1]["code"] == "open_question"
    assert fake.killed == []


def test_a_name_reused_by_another_agent_is_never_killed(published):
    service, fake, lead, run_id, crew, _ = published
    fake.row(crew, "55555555-5555-4555-8555-555555555555", parent=service.runs.run(run_id)["lead_identity"])
    status, body = service.handle("close", {"step": "S1"}, lead)
    assert status == 409 and body["code"] == "not_the_crew" and fake.killed == []


def test_only_the_runs_lead_may_close(published):
    service, fake, _, _, crew, _ = published
    fake.row("vc-9", "66666666-6666-4666-8666-666666666666")
    assert service.handle("close", {"step": "S1"}, "vc-9")[0] == 409, "a session that leads nothing"
    assert service.handle("close", {"step": "S1"}, crew)[0] == 409, "the worker cannot close itself"
    assert fake.killed == []
