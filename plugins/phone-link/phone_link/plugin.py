"""The phone-link plugin: a token-gated door from the Android app to seven bridge routes, its keeper, and the
daily grade door (door.py).

    GET /plugin/phone-link/state   adb, the listener, each phone and the request count — never the token

Everything else this plugin does happens off the bridge's port: the link's listener on 127.0.0.1:7683
(link_server.py), the adb keeper (keeper.py) and the daily grade door (door.py), all on daemon threads started here. The contract, and why
the bridge's own port is never tunnelled to a phone: owned by plugins/phone-link/README.md — see the
note there.

`/state` is served through `handle_request` only, because its caller policy reads the request's headers. The
`handle_get` / `handle_post` pair an older host would call instead answers 403 `host_too_old`: a caller the
plugin cannot see is a caller it cannot check (laya fails closed the same way).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from phone_link import guard, paths
from phone_link.adb import Runner, find_adb, known_hosts
from phone_link.config import LinkConfig, load_config
from phone_link.door import Door
from phone_link.keeper import Keeper
from phone_link.link_server import LinkApp, LinkServer
from phone_link.logs import guarded
from phone_link.paths import LOCK_FILE, PHONE_LINK_PORT, STOCK_BRIDGE_PORTS
from phone_link.state import LinkState
from phone_link.token_store import TokenStore

PLUGIN_NAME = "phone-link"
ROUTE = "/state"


def bridge_ports(context: object) -> set[int]:
    """Every port that is the bridge: the stock pair plus the ports this bridge was given."""
    ports = set(STOCK_BRIDGE_PORTS)
    for name in ("status_port", "port"):
        value = getattr(context, name, 0)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            ports.add(value)
    return ports


def _error(status: int, error: str, **extra: Any) -> tuple[int, dict]:
    return status, {"ok": False, "plugin": PLUGIN_NAME, "error": error, **extra}


def host_too_old() -> tuple[int, dict]:
    return _error(403, "host_too_old", message="This terminal bridge does not pass request headers to plugins: update tldrack")


class PhoneLinkPlugin:
    name = PLUGIN_NAME

    def __init__(
        self,
        context,
        *,
        start: bool = True,
        state_dir: Path | None = None,
        link_port: int = PHONE_LINK_PORT,
        runner: Runner | None = None,
        find: Callable[[], str | None] = find_adb,
        known: Callable[[], frozenset[str]] = known_hosts,
        config: LinkConfig | None = None,
        keeper_options: Mapping[str, Any] | None = None,
        door_options: Mapping[str, Any] | None = None,
    ) -> None:
        log = guarded(getattr(context, "log", None) or print)
        directory = state_dir or paths.state_dir()
        config = config or load_config()
        if config.error:
            log(f"phone-link: {config.error}")
        elif config.source != "default":
            log(f"phone-link: the token goes to {config.app_package} (set by {config.source})")
        self._state = LinkState(PHONE_LINK_PORT, app=config.to_json())
        self._tokens = TokenStore(directory)
        if self._tokens.value() is None:
            log(f"phone-link: cannot keep a token in {self._tokens.path.parent} ({self._tokens.error})")
        elif self._tokens.minted:
            log(f"phone-link: minted the link token in {self._tokens.path.parent}")
        self._server = LinkServer(
            LinkApp(self._tokens, self._state, int(context.status_port)),
            self._state,
            log=log,
            lock_path=directory / LOCK_FILE,
            port=link_port,
        )
        self._keeper = Keeper(
            state=self._state,
            tokens=self._tokens,
            bridge_ports=bridge_ports(context),
            log=log,
            runner=runner,
            app_package=config.app_package,
            find=find,
            known=known,
            **dict(keeper_options or {}),
        )
        self._door = Door(
            state=self._state, bridge_port=int(context.status_port), directory=directory, log=log, **dict(door_options or {})
        )
        if start:
            self._server.start()
            self._keeper.start()
            self._door.start()

    @property
    def link_port(self) -> int | None:
        return self._server.bound_port

    def routes(self) -> list[str]:
        return [f"/plugin/{self.name}{ROUTE}"]

    def busy_reasons(self) -> list[str]:
        return []

    def close(self) -> None:
        self._door.close()
        self._keeper.close()
        self._server.close()

    def state(self) -> dict:
        return self._state.to_json()

    # -- routes ------------------------------------------------------------------------------------
    def handle_request(
        self, method: str, subpath: str, query: dict[str, list[str]] | None, body: Any, headers: Mapping[str, str] | None
    ) -> tuple:
        if subpath not in _STATE_PATHS:
            return _unknown(subpath)
        if headers is None:
            return host_too_old()
        refusal = guard.request_refusal(headers)
        if refusal is not None:
            origins = {"allowed_origins": list(refusal.allowed_origins)} if refusal.allowed_origins else {}
            return _error(403, refusal.code, message=refusal.message, **origins)
        if method != "GET":
            return _error(405, f"use GET {ROUTE}")
        return 200, self.state()

    # -- the pair an older host calls: fail closed ---------------------------------------------------
    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        return host_too_old() if subpath in _STATE_PATHS else _unknown(subpath)

    def handle_post(self, subpath: str, query: dict[str, list[str]], body: Any) -> tuple:
        return host_too_old() if subpath in _STATE_PATHS else _unknown(subpath)


_STATE_PATHS = ("", "/", ROUTE)


def _unknown(subpath: str) -> tuple[int, dict]:
    return _error(404, f"unknown route {subpath}")
