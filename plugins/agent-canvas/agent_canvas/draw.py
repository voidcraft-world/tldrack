"""The ◧ DRAW button's request: what the terminal page pastes into an agent's draft (pure).

── WHY A BUTTON, AND WHY THE PATH RIDES IN THE TEXT ───────────────────────
Why agents need asking at all: owned by README.md § Getting agents to draw at
all — see the note there. The button is the on-demand door, and costs nothing
until it is pressed.

It PASTES, without Enter, as + IMAGE does (bridge/tldrack/index_patch.py),
so the user can add a focus before sending. The exact canvas path rides in the text, so an agent
that never got the SessionStart line (it started before this plugin was up)
still knows where to write.

The agent-initiated half is `nudge.py`.
"""

from __future__ import annotations

from agent_canvas.paths import canvas_path


def draw_request(key: str) -> str:
    """The pasted line for one VALIDATED canvas key. Ends in a space: the
    user's focus, if any, is typed straight after it."""
    return (
        f"Draw what you just did on your agent canvas, {canvas_path(key, '.json')} "
        "(agent-canvas skill): the structure as a diagram (new dirs, processes, the flow), "
        "not a recap of your reply. "
    )
