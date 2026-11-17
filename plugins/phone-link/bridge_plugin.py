"""tldrack entry module for phone-link: the host contract is owned by plugins/README.md."""

from phone_link.plugin import PLUGIN_NAME, PhoneLinkPlugin

__all__ = ["PLUGIN_NAME", "create_plugin"]


def create_plugin(context):
    return PhoneLinkPlugin(context)
