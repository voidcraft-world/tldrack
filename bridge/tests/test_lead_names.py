"""A LEAD wears a topic name: its launch name counts as generated, the rack's own lines never name it,
the operator's first real prompt does, and the run's title names it outright (lead/names.py)."""

import pytest

from lead_fakes import build, ok

from tldrack.lead.brief import launch_prompt
from tldrack.lead.names import LEAD_NAME_RE, lead_name
from tldrack.session_names import is_generated


def test_a_built_lead_name_is_recognised_and_suffixed_when_taken():
    first = lead_name("claude-c", 1_900_000_000.0, set())
    assert LEAD_NAME_RE.fullmatch(first) and is_generated(first)
    second = lead_name("claude-c", 1_900_000_000.0, {first})
    assert second == f"{first}-2" and is_generated(second)


@pytest.mark.parametrize("name", ["wt-lead-r0101-abcd-s2", "wt-lead-r1127-5916-s011", "wt-lead-r0101-abcd-fix-rows"])
def test_a_lead_worker_name_is_generated(name):
    # The run and step ids say nothing about the work, so the step's label replaces them (session_names.py).
    assert is_generated(name)


@pytest.mark.parametrize(
    "name",
    [
        "wt-my-branch",  # a worktree the operator named
        "wt-lead-r0101-abcd",  # a run with no step: not a worker
        "wt-lead-r0101-ABCD-s2",
        "lead",
        "lead-one",
        "my-lead-0101-1200",
        "lead-One-0101-1200",
        "vc-3x",
    ],
)
def test_custom_names_are_not_generated(name):
    assert not is_generated(name)


def test_the_launch_line_never_labels_the_lead_but_the_operators_first_prompt_does(tmp_path):
    service, fake, _ = build(tmp_path)
    lead = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    assert fake.names.note_prompt(lead, fake.spawned[-1]["prompt"]) is None
    assert fake.names.note_prompt(lead, launch_prompt(None)) is None
    assert fake.names.get_all() == {}
    assert fake.names.note_prompt(lead, "Collect Laya training data from the ledger") is not None
    assert fake.names.get_all() == {lead: "Collect Laya training data from the ledger"}


def test_start_names_the_lead_from_its_title_over_a_topic_in_flight(tmp_path):
    service, fake, _ = build(tmp_path)
    lead = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    epoch = fake.names.note_prompt(lead, "hi, let's work on the Laya data")
    body = ok(service.handle("start", {"title": "Laya training data", "plan_text": "# plan"}, lead))
    assert body["label"] == "laya-training-data"
    assert fake.names.get_all() == {lead: "laya-training-data"}
    assert not fake.names.settle_topic(lead, epoch, "laya-data"), "the topic call in flight lands nothing"
    assert fake.names.note_prompt(lead, "now the next part") is None, "the title is final"


def test_a_title_never_overrides_an_explicit_rename(tmp_path):
    service, fake, _ = build(tmp_path)
    lead = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    fake.names.manual(lead, lead)  # ✎ kept the name as typed: the operator's choice
    body = ok(service.handle("start", {"title": "Laya training data", "plan_text": "# plan"}, lead))
    assert "label" not in body
    assert fake.names.get_all() == {}


def test_a_title_with_no_topic_keeps_the_label_there_is(tmp_path):
    service, fake, _ = build(tmp_path)
    lead = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    epoch = fake.names.note_prompt(lead, "Worker replays")
    assert fake.names.settle_topic(lead, epoch, "worker-replays")
    body = ok(service.handle("start", {"title": "Fix the code", "plan_text": "# plan"}, lead))
    assert "label" not in body
    assert fake.names.get_all() == {lead: "worker-replays"}


def test_a_resumed_or_adopting_lead_wears_the_runs_title(tmp_path):
    service, fake, _ = build(tmp_path)
    lead = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    run_id = ok(service.handle("start", {"title": "Rack names for leads", "plan_text": "# plan"}, lead))["run"]
    assert fake.names.get_all() == {lead: "rack-names-leads"}
    fake.drop(lead)
    assert fake.names.note_prompt(lead, launch_prompt(run_id)) is None, "a resume line is the rack's too"
    resumed = ok(service.handle("launch", {"profile": "a", "resume": run_id}, None))["session"]
    assert fake.names.get_all()[resumed] == "rack-names-leads", "the dead lead's label is not in the rack"

    fake.drop(resumed)
    fresh = ok(service.handle("launch", {"profile": "auto"}, None))["session"]
    fake.names.note_prompt(fresh, f"resume {run_id}")
    assert ok(service.handle("adopt", {"run": run_id}, fresh))["label"] == "rack-names-leads"
    assert fake.names.get_all()[fresh] == "rack-names-leads"
