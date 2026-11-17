"""tldrack entry module (see plugins/README.md for the contract).

The bridge sys.path-inserts this directory before importing, so the sibling
coordinator/ package resolves without any install step.
"""

from coordinator.plugin import CoordinatorPlugin

PLUGIN_NAME = "coordinator"


def create_plugin(context):
    return CoordinatorPlugin(context)
