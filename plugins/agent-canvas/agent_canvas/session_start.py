"""What an agent is told when its session starts: where its canvas is (pure).

── WHY THE PLUGIN WRITES THIS, AND THE HOOK ONLY RELAYS IT ─────────────────
The agent learns its canvas path ONCE, from a SessionStart hook, and the
path depends on things only this process knows: the canvas directory (the
bridge's data directory, `paths.py`) and the key tmux gives the session. A
hook that worked the path out for itself would be a second copy of both, and
the agent runs without the bridge's environment, so that copy would be wrong
whenever the operator moved the data directory. `hooks/session-start.sh`
therefore asks `GET /session-start` and prints the answer as it is.

The triggers are a compressed copy of the agent-canvas skill's § When to draw
(`.claude/skills/agent-canvas/SKILL.md`, the owner). The smallest doc below is
held valid by `tests/test_session_start.py`.
"""

from __future__ import annotations

from pathlib import Path

from agent_canvas.paths import canvas_path

#: The artifact shelf's native client, by absolute path: the agent's working
#: directory is its own repository, not this one.
REGISTER_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "register-artifact.py"

#: The smallest doc that draws something; the agent can copy it without loading the skill.
SMALLEST_DOC = '{"blocks":[{"type":"mermaid","source":"flowchart TD; a-->b"}]}'


def session_start_text(key: str) -> str:
    """The SessionStart context for one VALIDATED canvas key: two lines."""
    return (
        "Agent canvas: a pane beside your terminal in the agent rack; the user takes in structure faster "
        f"there than as prose. Typed JSON doc -> {canvas_path(key, '.json')} (checked on every write; blocks "
        f"in the agent-canvas skill), or body markup -> {canvas_path(key, '.html')}. DRAW instead of "
        "describing when you create directories or a module layout (mermaid flowchart TD), add a process, "
        "service, hook or plugin (flowchart LR of who starts what), trace a request (sequenceDiagram), show "
        f"a before/after (diff block) or weigh options (table). Smallest valid doc: {SMALLEST_DOC}. "
        "Overwrite the whole file each time; your reply still carries the answer. Not for routine replies.\n"
        "Generated deliverables: register HTML, reports or images on this session's canvas artifact shelf "
        f"with python3 {REGISTER_SCRIPT} <path>. A Write of an .html file registers itself; shell and "
        "apply_patch outputs need that command. See the agent-canvas skill.\n"
    )
