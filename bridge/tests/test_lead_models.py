"""The LEAD's model pair (lead/models.py): the standing default, a run that keeps the pair it started
with, a worker launched on the worker model, and a capped lead's fork denied."""

import json

import pytest

from lead_fakes import build, worker_appears, launch_and_start, ok

from tldrack.lead.models import DEFAULT_PAIR, LeadModels, clean_pair, pair_error

FORK = {"tool_name": "Agent", "tool_input": {"subagent_type": "fork", "prompt": "do it"}}
EXPLORE = {"tool_name": "Agent", "tool_input": {"subagent_type": "Explore", "prompt": "find it"}}


def decision(answer):
    return (answer or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def test_the_default_is_fable_over_opus_and_a_bad_file_is_the_default(tmp_path):
    models = LeadModels(tmp_path / "lead-models.json")
    assert models.pair() == DEFAULT_PAIR == {"model": "fable", "worker_model": "opus"}
    for junk in (
        "not json",
        "[]",
        '{"model": "fable"}',
        '{"model": "opus", "worker_model": "opus"}',
        '{"model": "fable", "worker_model": "gpt-5-codex"}',
    ):
        (tmp_path / "lead-models.json").write_text(junk)
        assert models.pair() == DEFAULT_PAIR, junk
    assert models.view()["families"] == ["haiku", "sonnet", "opus", "fable"]


def test_the_operator_can_change_the_pair_or_turn_the_split_off(tmp_path):
    models = LeadModels(tmp_path / "lead-models.json")
    assert models.set({"worker_model": "sonnet"}) == {"model": "fable", "worker_model": "sonnet"}
    assert models.set({"worker_model": None}) == {"model": "fable", "worker_model": None}, "off: all on the lead's model"
    assert json.loads((tmp_path / "lead-models.json").read_text()) == {"model": "fable", "worker_model": None}
    assert models.set({"model": None, "worker_model": "opus"}) == {"model": None, "worker_model": "opus"}
    for bad in (
        {"worker_model": "fable", "model": "fable"},
        {"model": "opus", "worker_model": "fable"},
        {"worker_model": "best"},
        {"model": 3},
    ):
        with pytest.raises(ValueError):
            models.set(bad)
    assert models.pair() == {"model": None, "worker_model": "opus"}, "a refused change stores nothing"
    assert pair_error("fable", "opus") is None and "weaker" in pair_error("opus", "opus")
    assert clean_pair({"model": "fable"}) is None


def test_a_pair_file_or_a_request_one_release_behind_still_names_the_worker_model(tmp_path):
    # Legacy (step 6.8): the key was `crew_model` before 0.1.0.
    (tmp_path / "lead-models.json").write_text('{"model": "fable", "crew_model": "sonnet"}')
    models = LeadModels(tmp_path / "lead-models.json")
    assert models.pair() == {"model": "fable", "worker_model": "sonnet"}
    assert models.set({"crew_model": "haiku"}) == {"model": "fable", "worker_model": "haiku"}
    assert json.loads((tmp_path / "lead-models.json").read_text()) == {"model": "fable", "worker_model": "haiku"}
    assert clean_pair({"model": "fable", "crew_model": "opus", "worker_model": "sonnet"})["worker_model"] == "sonnet"


def test_a_lead_launches_capped_and_its_workers_run_the_worker_model(tmp_path):
    service, fake, _ = build(tmp_path, models=LeadModels(tmp_path / "lead-models.json"))
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    launched = fake.spawned[0]
    assert (launched["model"], launched["subagent_model"]) == ("fable", "opus")
    run = service.runs.run(run_id)
    assert (run["lead_model"], run["worker_model"]) == ("fable", "opus")

    ok(service.handle("step", {"id": "A1", "title": "art", "brief": "b", "lane": "codex"}, lead))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    ok(service.handle("spawn", {"step": "A1"}, lead))
    claude_worker, codex_worker = fake.worktree.creates
    assert claude_worker["model"] == "opus" and claude_worker["branch"] != codex_worker["branch"], "one worktree each"
    assert "model" not in codex_worker, "a Codex worker keeps its profile's model"

    view = service.state_view()
    assert view["models"]["worker_model"] == "opus" and view["runs"][0]["worker_model"] == "opus"


def test_a_capped_lead_cannot_fork_and_an_uncapped_one_can(tmp_path):
    service, fake, _ = build(tmp_path, models=LeadModels(tmp_path / "lead-models.json"))
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker = worker_appears(service, fake, lead, "S1", run_id)
    service.tick()
    answer = service.hook_tool(lead, FORK)
    assert decision(answer) == "deny" and "worktree" in answer["hookSpecificOutput"]["permissionDecisionReason"]
    assert decision(service.hook_tool(lead, {**FORK, "tool_name": "Task"})) == "deny"
    assert service.hook_tool(lead, EXPLORE) is None, "a typed subagent runs the forced model"
    assert service.hook_tool(worker, FORK) is None, "a worker's fork runs the worker's own model"

    plain, fake2, _ = build(tmp_path / "plain")
    lead2, _ = launch_and_start(plain, fake2, steps=("S1",))
    assert "model" not in fake2.spawned[0] and plain.hook_tool(lead2, FORK) is None
    assert "models" not in plain.state_view()


def test_a_resume_keeps_the_runs_pair_whatever_the_standing_one_became(tmp_path):
    models = LeadModels(tmp_path / "lead-models.json")
    service, fake, _ = build(tmp_path, models=models)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    models.set({"worker_model": "haiku"})
    fake.drop(lead)
    ok(service.handle("launch", {"profile": "auto", "resume": run_id}, None))
    resumed = fake.spawned[-1]
    assert (resumed["model"], resumed["subagent_model"]) == ("fable", "opus")
    ok(service.handle("launch", {"profile": "auto"}, None))
    assert fake.spawned[-1]["subagent_model"] == "haiku", "a NEW lead takes the standing pair"
    ok(service.handle("launch", {"profile": "auto", "worker_model": None}, None))
    assert "subagent_model" not in fake.spawned[-1], "the request's key wins, and null is off"


def test_only_the_operator_sets_the_pair_and_a_refusal_reaches_the_caller(tmp_path):
    models = LeadModels(tmp_path / "lead-models.json")
    service, fake, _ = build(tmp_path, models=models)
    lead, _ = launch_and_start(service, fake, steps=("S1",))
    assert service.handle("models", {"worker_model": None}, lead)[0] == 403
    assert models.pair() == DEFAULT_PAIR
    assert ok(service.handle("models", {"worker_model": "sonnet"}, None))["worker_model"] == "sonnet"
    status, body = service.handle("models", {"worker_model": "fable"}, None)
    assert status == 400 and "weaker" in body["error"]
    status, body = service.handle("launch", {"profile": "auto", "model": "opus", "worker_model": "opus"}, None)
    assert status == 400 and "weaker" in body["error"]


def test_a_worktree_plugin_that_would_drop_the_model_stops_the_spawn(tmp_path):
    service, fake, _ = build(tmp_path, models=LeadModels(tmp_path / "lead-models.json"))
    lead, _ = launch_and_start(service, fake, steps=("S1",))
    fake.worktree.launch_models = False
    status, body = service.handle("spawn", {"step": "S1"}, lead)
    assert status == 503 and "too old" in body["error"] and not fake.worktree.creates
