"""tldrack entry module for devices: the host contract is owned by plugins/README.md."""

from devices_plugin.plugin import DevicesPlugin
from devices_plugin.settings import PLUGIN_NAME

__all__ = ["PLUGIN_NAME", "create_plugin"]


def create_plugin(context):
    # Off unless the operator's settings say otherwise; what that means: devices_plugin/plugin.py.
    return DevicesPlugin(context)
