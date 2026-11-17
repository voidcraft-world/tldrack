"""The page the rack frames: the kit, a CSP, and a slot for the agent's fragment.

── WHY THE AGENT WRITES A FRAGMENT AND NEVER A PAGE ───────────────────────
Every token an agent spends on `<head>`, CSS and a script is a token not spent
on the content, and it is spent again on every revision. The kit (`assets/`)
is written once, here; the agent writes body markup against its ~10 classes.

── WHY AGENT MARKUP CANNOT RUN SCRIPT ─────────────────────────────────────
This page is served from the BRIDGE's origin, and a script on that origin can
call every bridge route — spawn, kill, type into any pane. A fragment is agent
output, and agent output routinely quotes things it fetched. So the CSP admits
exactly one script, the kit's, by a per-response nonce the fragment cannot
know; inline handlers (`onclick=`) are refused for the same reason. The meta
CSP comes before any agent byte, and a later CSP can only tighten, never widen.
"""

from __future__ import annotations

import html
import re
import secrets
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from agent_canvas.vendor import MERMAID_VERSION

ASSETS = Path(__file__).resolve().parent / "assets"

#: Where the kit loads Mermaid from: this plugin's own route (`vendor.py`). The
#: version rides in the query so a browser never runs an old bundle after an update.
MERMAID_URL = f"/plugin/agent-canvas/mermaid.js?v={MERMAID_VERSION}"

DEFAULT_PORTS = {"http": 80, "https": 443}


def framer_origins(configured: object) -> tuple[str, ...]:
    """The pages besides loopback allowed to FRAME the canvas: the operator's
    `allowed_origins` (plugins/README.md § What the context carries), each one
    kept only when it is a bare http(s) origin, in the form a browser reports it.
    None, or anything that is not a collection of strings, means loopback only.

    ── WHY THE PAGE CHECKS WHO FRAMES IT ──
    Its buttons TYPE INTO AN AGENT, and its POST is same-origin to the bridge no
    matter who embeds it, so any website could frame it under a decoy and
    borrow the click. A plugin cannot set a header (and the meta form of
    `frame-ancestors` is ignored). The tldrack host sends that directive on
    every response for it (bridge/tldrack/framing.py); the kit also reads
    `location.ancestorOrigins` and goes inert under any other parent, so the
    page stays safe under a host that does not. Loopback is always allowed and
    is checked by the kit itself, so it is never listed.
    """
    if configured is None or isinstance(configured, (str, bytes)) or not isinstance(configured, Iterable):
        return ()
    found: set[str] = set()
    for raw in configured:
        if not isinstance(raw, str) or raw != raw.strip() or not raw:
            continue
        try:
            parts = urlsplit(raw)
            port = parts.port
        except ValueError:
            continue
        if (
            parts.scheme not in ("http", "https")
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            continue
        host = parts.hostname.lower()  # an IPv6 literal arrives without its brackets
        if not re.fullmatch(r"[a-z0-9.\-]+|[0-9a-f:.]+", host):
            continue  # not ASCII: a browser reports the punycode form, so configure that
        if ":" in host:
            host = f"[{host}]"
        # A browser leaves the scheme's default port out of an origin.
        shown_port = "" if port in (None, DEFAULT_PORTS[parts.scheme]) else f":{port}"
        found.add(f"{parts.scheme}://{host}{shown_port}")
    return tuple(sorted(found))


def make_nonce() -> str:
    return secrets.token_urlsafe(18)


def content_security_policy(nonce: str, artifact_host: str = "127.0.0.1:7682") -> str:
    # Only the trusted viewer can be framed. 'self' would also let agent markup
    # embed unrelated, privileged bridge pages whose scripts have their own CSP.
    if not re.fullmatch(r"[A-Za-z0-9.\-:\[\]]+", artifact_host):
        raise ValueError("invalid artifact host")
    viewer = f"{artifact_host}/plugin/agent-canvas/artifact"
    return "; ".join(
        (
            "default-src 'none'",
            f"script-src 'nonce-{nonce}'",
            # Mermaid draws with inline styles, and an agent may add a style
            # attribute; style cannot reach the bridge, script can.
            "style-src 'self' 'unsafe-inline'",
            # No remote images: an <img src=https://…> is a beacon.
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            f"frame-src http://{viewer} https://{viewer}",
            "form-action 'none'",
            "base-uri 'none'",
        )
    )


def _asset(name: str) -> str:
    return (ASSETS / name).read_text(encoding="utf-8")


def build_page(session: str, nonce: str, artifact_host: str = "127.0.0.1:7682", framers: tuple[str, ...] = ()) -> str:
    """The whole document for one session. `session` is already validated;
    `framers` comes from `framer_origins`."""
    safe_session = html.escape(session, quote=True)
    safe_framers = html.escape(" ".join(framers), quote=True)
    return (
        '<!doctype html>\n<html lang="en"><head>\n'
        '<meta charset="utf-8">\n'
        f'<meta http-equiv="Content-Security-Policy" content="{content_security_policy(nonce, artifact_host)}">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>canvas · {safe_session}</title>\n"
        f"<style>{_asset('kit.css')}</style>\n"
        "</head>\n"
        f'<body data-session="{safe_session}" data-framers="{safe_framers}" '
        f'data-mermaid="{html.escape(MERMAID_URL, quote=True)}">\n'
        '<nav id="vc-views" class="vc-views" aria-label="Canvas or skills"></nav>\n'
        '<div id="vc-status" class="vc-status" aria-live="polite"></div>\n'
        '<main id="vc-canvas"></main>\n'
        '<section id="vc-artifacts" hidden aria-label="Generated artifacts"></section>\n'
        '<div id="vc-skills" hidden></div>\n'
        # ONE script, still: the skills face is defined first and kit.js mounts it.
        f'<script nonce="{nonce}">{_asset("skills.js")}\n{_asset("artifacts.js")}\n{_asset("kit.js")}</script>\n'
        "</body></html>\n"
    )
