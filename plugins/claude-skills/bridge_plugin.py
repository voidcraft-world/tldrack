"""tldrack entry module (see plugins/README.md for the contract).

The bridge sys.path-inserts this directory before importing, so the sibling
claude_skills/ package resolves without any install step.
"""

from claude_skills.plugin import ClaudeSkillsPlugin

PLUGIN_NAME = "claude-skills"


def create_plugin(context):
    return ClaudeSkillsPlugin(context)
