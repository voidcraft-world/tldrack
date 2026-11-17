"""tldrack entry module: the host contract is owned by plugins/README.md.

The bridge appends this directory to sys.path before importing, so the
sibling worktree/ package resolves without any install step.
"""

from worktree.repos import RepoRouter

PLUGIN_NAME = "worktree"


def create_plugin(context):
    return RepoRouter(context)
