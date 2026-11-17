"""The host context the route tests hand the plugin: the operator turned it on.

The off case builds its own (`test_plugin.py`); everything else needs an enabled plugin,
so the default is the settings object an operator writes to turn it on (README § Settings).
"""

from __future__ import annotations


class FakeContext:
    port = 7681
    status_port = 7682
    default_session = "main"
    start_dir = "/repo"

    def __init__(self, settings: object = None) -> None:
        self.lines: list[str] = []
        self.settings = {"enabled": True} if settings is None else settings

    def log(self, line: str) -> None:
        self.lines.append(line)

    def plugin_settings(self, name: str) -> object:
        assert name == "coordinator"
        return self.settings
