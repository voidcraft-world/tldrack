"""tldrack entry module for laya: the host contract is owned by plugins/README.md."""

from laya_bridge.plugin import PLUGIN_NAME, LayaPlugin

__all__ = ["PLUGIN_NAME", "create_plugin"]


def create_plugin(context):
    return LayaPlugin(context)
