"""tldrack entry module (see plugins/README.md for the contract).

The bridge sys.path-inserts this directory before importing, so the sibling
agent_canvas/ package resolves without any install step.
"""

from agent_canvas.plugin import AgentCanvasPlugin

PLUGIN_NAME = "agent-canvas"


def create_plugin(context):
    return AgentCanvasPlugin(context)
