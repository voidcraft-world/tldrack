"""The LEAD's hooks (lead/service.py): a lead's edit is refused, a worker's AskUserQuestion goes to its
lead, both roles are restated on every prompt, and nobody else is touched."""

from lead_fakes import build, crew_appears, launch_and_start, ok

ASK = {
    "tool_name": "AskUserQuestion",
    "tool_input": {
        "questions": [{"question": "404 or 410 for a deleted world?", "options": [{"label": "404"}, {"label": "410"}]}]
    },
}


def decision(answer):
    return (answer or {}).get("hookSpecificOutput", {}).get("permissionDecision")


def test_lead_edits_are_denied_and_crew_questions_go_to_the_lead(tmp_path):
    service, fake, _ = build(tmp_path)
    lead, run_id = launch_and_start(service, fake, steps=("S1",))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    crew = crew_appears(service, fake, lead, "S1", run_id)
    fake.row("vc-7", "88888888-8888-4888-8888-888888888888")
    service.tick()

    assert decision(service.hook_tool(lead, {"tool_name": "Edit"})) == "deny"
    assert decision(service.hook_tool(lead, {"tool_name": "Write"})) == "deny"
    assert service.hook_tool(lead, {"tool_name": "Bash"}) is None
    assert service.hook_tool(crew, {"tool_name": "Edit"}) is None, "a worker does the work"
    assert service.hook_tool("vc-7", {"tool_name": "Edit"}) is None and service.hook_tool("vc-7", ASK) is None

    answer = service.hook_tool(crew, ASK)
    assert decision(answer) == "deny"
    (question,) = service.runs.questions(run_id)
    assert question["step_id"] == "S1" and question["blocking"] == 1 and "410" in question["text"]
    assert question["id"] in answer["hookSpecificOutput"]["permissionDecisionReason"]

    assert "LEAD" in service.hook_prompt(lead) and run_id in service.hook_prompt(lead)
    assert "worker" in service.hook_prompt(crew) and service.hook_prompt("vc-7") is None
    assert service.row_fields({"session": lead}) == {"lead": {"run": run_id, "open_decisions": 0}}
    assert service.row_fields({"session": crew}) == {"crew": {"run": run_id, "step": "S1"}}
    assert service.row_fields({"session": "vc-7"}) == {}
