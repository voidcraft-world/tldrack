"""The built rack page, served by the bridge itself at `/`.

A stranger needs no hosting and no origin setup: the page comes from the same loopback port as
the API it calls, so the browser sends it as a loopback origin, which every bridge serves.

WHERE THE BUILD IS (`find_ui_dist`), first that exists: `TLDRACK_UI_DIST`, settings.py `ui_dist`,
the checkout's `ui/dist` beside this package, then a copy shipped inside the package (`_ui/`).
None found is not an error: `/` then answers a page that says how to build it.

WHAT IS SERVED (`resolve`). Only regular files that resolve inside the build directory: `..`, an
absolute segment or a symlink that leaves it names nothing. `/` is `index.html`. A path with no
file is None, and the caller answers its ordinary 404, so the API's routes are never shadowed:
the handler asks its own routes first.

CACHING. Vite names a built asset by its content hash under `assets/`, so those are immutable;
every HTML page is `no-cache`, so a rebuild is picked up on the next load.
"""

from __future__ import annotations

import mimetypes
import os
from pathlib import Path
from urllib.parse import unquote

UI_DIST_ENV = "TLDRACK_UI_DIST"
INDEX = "index.html"
PACKAGED_DIRNAME = "_ui"
#: A page for a bridge with no build. Plain text in HTML: it runs no script and loads nothing.
NOT_BUILT = (
    "<!doctype html><meta charset=utf-8><title>tldrack</title>"
    '<body style="font:16px/1.5 ui-monospace,monospace;max-width:40rem;margin:4rem auto;padding:0 1rem">'
    '<h1 style="font-size:1.2rem">tldrack: the bridge is up, the page is not built</h1>'
    "<p>Run <code>tldrack up</code> from a checkout: it builds <code>ui/dist</code> when it is missing "
    "(it needs <code>node</code> and <code>npm</code>). <code>tldrack doctor</code> says what is missing.</p>"
    '<p>The API is here: <a href="/snapshot">/snapshot</a>.</p>'
)
_TYPES = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".html": "text/html",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".wasm": "application/wasm",
    ".woff2": "font/woff2",
    ".woff": "font/woff",
    ".map": "application/json",
    ".ico": "image/x-icon",
}
_TEXT = ("text/", "application/json", "image/svg+xml")


def checkout_ui_dir() -> Path:
    """`ui/` of the checkout this package runs from (it may not exist: an installed wheel)."""
    return Path(__file__).resolve().parents[2] / "ui"


def find_ui_dist(configured: Path | None = None, env: dict[str, str] | None = None) -> Path | None:
    """The build directory to serve, or None. A directory counts only when it holds `index.html`."""
    env = os.environ if env is None else env
    override = env.get(UI_DIST_ENV)
    candidates = [Path(override).expanduser()] if override else []
    if configured is not None:
        candidates.append(configured)
    candidates += [checkout_ui_dir() / "dist", Path(__file__).resolve().parent / PACKAGED_DIRNAME]
    return next((path for path in candidates if (path / INDEX).is_file()), None)


def resolve(dist: Path, url_path: str) -> Path | None:
    """The file `url_path` names inside `dist`, or None. Pure apart from the filesystem reads."""
    relative = unquote(url_path).lstrip("/") or INDEX
    if "\x00" in relative or relative.startswith(("/", "\\")):
        return None
    parts = Path(relative).parts
    if any(part in ("..", "") or part.startswith(("/", "\\")) for part in parts):
        return None
    try:
        root = dist.resolve()
        target = (root / relative).resolve()
    except OSError:
        return None
    if not target.is_relative_to(root):
        return None
    if target.is_dir():
        target = target / INDEX
    return target if target.is_file() else None


def content_type(path: Path) -> str:
    kind = _TYPES.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return f"{kind}; charset=utf-8" if kind.startswith(_TEXT) else kind


def cache_control(dist: Path, path: Path) -> str:
    try:
        hashed = path.resolve().relative_to(dist.resolve()).parts[:1] == ("assets",)
    except (OSError, ValueError):
        hashed = False
    return "public, max-age=31536000, immutable" if hashed else "no-cache"
