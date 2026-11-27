"""Pane buttons: a mounted plugin may put a button in every terminal pane.

The pane's own tools (+ IMAGE, ◧ DRAW, ◇ SKILLS) are written into the patched terminal page, so a
plugin from the operator's own plugin list had no way to add one: its button meant an edit to this
bridge. A plugin now DECLARES its buttons and the page draws them after + IMAGE.

── THE CONTRACT ───────────────────────────────────────────────────────────────
A plugin may define `pane_buttons()`, returning a list of `{"label", "title", "route"}`:

- `label` is the button's text, `title` its tooltip (optional).
- `route` is one of the plugin's OWN `routes()`. A press is `POST <route>` with
  `{"session": <the pane's tmux session, or null>}`.
- The plugin answers `{"paste"?: str, "notice"?: str, "cancelled"?: true}`, or a refusal with
  `error`. The page puts `paste` in the draft and shows `notice` beside the buttons.

`GET /pane-buttons` serves what every mounted plugin declared: `{"buttons": [...], "refused": [...]}`.

── WHAT A PLUGIN CANNOT DO WITH ONE ───────────────────────────────────────────
- **Press Enter.** The page refuses a `paste` holding any control character, so a button fills the
  draft and the operator sends it. (index_patch.py `press_plugin_button` holds that check: the
  answer never passes through this module.)
- **Reach another plugin or a bridge route.** A `route` outside the plugin's own is refused here.
- **Draw markup.** The page sets a label and a title as text; they are bounded and printable.
- **Crowd the pane.** PER_PLUGIN and TOTAL cap the row.

A malformed declaration is never an error and never a log line: this is asked on every pane load,
and one bad plugin must not cost the others their buttons. It is listed under `refused` with the
reason, so whoever wrote the plugin reads why with one `curl`.
"""

from __future__ import annotations

from collections.abc import Mapping

from voidcraft_world_bridge.shape import typed

LABEL_MAX = 24
TITLE_MAX = 200
PER_PLUGIN = 4
TOTAL = 8


def text_field(value: object, limit: int) -> str | None:
    """`value` as a button's text: a printable string of at most `limit` characters, else None."""
    if not isinstance(value, str) or len(value) > limit or not value.isprintable():
        return None
    return value.strip()


def parse_button(entry: object, routes: frozenset[str]) -> dict | str:
    """One declared button as the page reads it, or the reason it is refused."""
    data: dict = typed(entry, dict, {})
    label = text_field(data.get("label"), LABEL_MAX)
    if not label:
        return f"label must be 1 to {LABEL_MAX} printable characters"
    title = text_field(data.get("title", ""), TITLE_MAX)
    if title is None:
        return f"title must be at most {TITLE_MAX} printable characters"
    route = data.get("route")
    if not isinstance(route, str) or route not in routes:
        return "route must be one of the plugin's own routes"
    return {"label": label, "title": title, "route": route}


def declared(plugin: object) -> tuple[list, frozenset[str]] | str | None:
    """A plugin's raw declaration and its routes; None when it declares nothing; the reason when
    asking it failed."""
    pane_buttons = getattr(plugin, "pane_buttons", None)
    if not callable(pane_buttons):
        return None
    try:
        entries = pane_buttons()
        routes = frozenset(str(route) for route in plugin.routes())  # type: ignore[attr-defined]
    except Exception as error:  # noqa: BLE001 — a plugin bug costs that plugin's buttons, never the route
        return f"pane_buttons() raised {type(error).__name__}"
    if not isinstance(entries, list):
        return "pane_buttons() must return a list"
    return entries, routes


def collect(plugins: Mapping[str, object] | None) -> dict:
    """Every mounted plugin's pane buttons, in mount order: the body of `GET /pane-buttons`."""
    buttons: list[dict] = []
    refused: list[dict] = []
    for name, plugin in (plugins or {}).items():
        found = declared(plugin)
        if found is None:
            continue
        if isinstance(found, str):
            refused.append({"plugin": name, "reason": found})
            continue
        entries, routes = found
        kept = 0
        for entry in entries:
            if kept == PER_PLUGIN:
                refused.append({"plugin": name, "reason": f"a plugin draws at most {PER_PLUGIN} buttons"})
                break
            if len(buttons) == TOTAL:
                refused.append({"plugin": name, "reason": f"a pane draws at most {TOTAL} plugin buttons"})
                break
            button = parse_button(entry, routes)
            if isinstance(button, str):
                refused.append({"plugin": name, "reason": button})
                continue
            buttons.append({"plugin": name, **button})
            kept += 1
    return {"buttons": buttons, "refused": refused}
