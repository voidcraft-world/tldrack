"""The plugin object the bridge mounts.

GET  /health                  → {ok, canvas_dir}
GET  /session-start?session=<name> → what `hooks/session-start.sh` prints to the agent: its canvas
                                paths (text/plain, "" when the session has no canvas; `session_start.py`)
GET  /page?session=<name>     → the canvas page the rack frames (text/html)
GET  /frag?session=<name>     → {rev, kind, html | doc+errors+questions, question, answered, queued, …}
GET  /check?key=<key>         → {ok, kind, drawn, errors, questions}; `&format=text` → the issues
                                as the agent reads them ("" when the canvas is clean)
POST /check <PostToolUse hook stdin> → the same text for the canvas that Write/Edit touched, ""
                                for any other file (`hooks/check.sh`); native callers only
GET  /draw?session=<name>     → {key, paste} — what the terminal's ◧ DRAW button pastes (`draw.py`)
POST /view {session, view: "canvas"|"skills"|"toggle"} → {session, view} — the pane's face; the
                                terminal's ◇ SKILLS and the page's own chips (`desk.py` `set_view`)
POST /nudge?session=<name> <PostToolUse hook stdin> → the hook's stdout: a canvas reminder, or
                                nothing (`nudge.py`); native callers only
GET  /mermaid.js              → the vendored Mermaid bundle, checked (`vendor.py`)
POST /reply {session, rev, answers: {id: value}} → 200 delivered · 202 queued
POST /reply {session, rev, answer, question?}    → the same, for one value

Feature write-up: README.md beside this package.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from urllib.parse import urlparse

from agent_canvas.artifact_page import build_artifact_page
from agent_canvas.artifacts import ArtifactStore
from agent_canvas.desk import CanvasDesk
from agent_canvas.host import HostClient
from agent_canvas.nudge import Nudger, hook_output
from agent_canvas.page import build_page, framer_origins, make_nonce
from agent_canvas.paths import SESSION_NAME_RE, canvas_dir, canvas_path, ensure_canvas_dir, normalize_key
from agent_canvas.session_start import session_start_text
from agent_canvas.tmux import workspace_for_session
from agent_canvas.vendor import VendorError, mermaid_js

PLUGIN_NAME = "agent-canvas"

#: The watcher's beat: flush queued answers, raise unanswered questions. One
#: directory listing per beat while idle — no tmux call, no HTTP.
TICK_SECONDS = 1.5

#: POST /nudge and /check carry the Write's whole `tool_input`, file content
#: included — the same reason the bridge's own /hook takes 512 KiB. Past this
#: the hook stays quiet.
NUDGE_MAX_BODY_BYTES = 2 * 1024 * 1024


def same_origin(headers: dict) -> bool:
    """Is this POST from the canvas page itself (or a native client)?

    The host already refuses foreign origins; this narrows /reply further to
    the bridge's OWN origin, because /reply types into an agent and any other
    loopback page — a dev server, a docs preview — has no business doing that.
    Under rebinding, Host is already pinned by the host's guard.
    """
    origin = headers.get("origin")
    if origin is None:
        # curl / a native client — unless a browser left it off (no-cors).
        return headers.get("sec-fetch-site") is None
    host = headers.get("host")
    return bool(host) and urlparse(origin).netloc == host


def check_text(key: str, view: dict) -> str:
    """The post-write hook's message to the agent; empty when there is nothing to fix."""
    if view.get("ok"):
        return ""
    errors = view.get("errors") or []
    head = "the rest was drawn" if view.get("drawn") else "NOTHING was drawn"
    lines = [
        f"agent canvas: {len(errors)} issue{'s' if len(errors) != 1 else ''} in {key}.json — "
        f"those blocks were cut, {head}. Fix and rewrite the file:"
    ]
    lines += [f"  {error['path']}  {error['code']}: {error['message']}" for error in errors]
    lines.append("Block reference: reference.md in the agent-canvas skill")
    return "\n".join(lines) + "\n"


def native_caller(headers: dict) -> bool:
    """curl, not a browser: no Origin and no Sec-Fetch-Site. The hook routes take a
    hook's stdin, and /nudge answers whether a directory holds one file, which no
    web page needs to learn."""
    return headers.get("origin") is None and headers.get("sec-fetch-site") is None


class AgentCanvasPlugin:
    name = PLUGIN_NAME
    max_body_bytes = NUDGE_MAX_BODY_BYTES

    def __init__(
        self, context, *, desk: CanvasDesk | None = None, nudger: Nudger | None = None, start_watcher: bool = True
    ) -> None:
        self._context = context
        # Read once: the operator's origins, besides loopback, that may frame the page.
        self._framers = framer_origins(getattr(context, "allowed_origins", None))
        self._desk = desk or CanvasDesk(HostClient(context.status_port))
        self._nudger = nudger or Nudger()
        self._artifacts = ArtifactStore()
        self._stop = threading.Event()
        if start_watcher:
            threading.Thread(target=self._watch, name="agent-canvas-watcher", daemon=True).start()

    def routes(self) -> list[str]:
        base = f"/plugin/{self.name}"
        return [
            f"{base}/health",
            f"{base}/session-start",
            f"{base}/page",
            f"{base}/frag",
            f"{base}/check",
            f"{base}/draw",
            f"{base}/mermaid.js",
            f"{base}/reply",
            f"{base}/nudge",
            f"{base}/view",
            f"{base}/artifacts/register",
            f"{base}/artifact",
        ]

    def busy_reasons(self) -> list[str]:
        """A restart drops queued answers, so say so before one."""
        return [f"answer queued for {name}" for name in self._desk.queued_sessions()]

    def stop(self) -> None:
        self._stop.set()

    def _watch(self) -> None:
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._desk.tick()
            except Exception as exc:  # noqa: BLE001 — a bad beat must not end the watcher
                self._context.log(f"agent-canvas: watcher beat failed: {type(exc).__name__}: {exc}")

    # -- dispatch -----------------------------------------------------------
    def handle_request(self, method, subpath, query, body, headers) -> tuple:
        if method == "POST":
            if subpath == "/reply" and not same_origin(headers or {}):
                return 403, {"error": "replies come from the canvas page only"}
            if subpath in ("/nudge", "/check", "/artifacts/register") and not native_caller(headers or {}):
                return 403, {"error": "hook relays and artifact registration require a native caller"}
            return self.handle_post(subpath, query, body)
        return self.handle_get(subpath, query, artifact_host=(headers or {}).get("host"))

    def handle_get(self, subpath: str, query: dict[str, list[str]], *, artifact_host: str | None = None) -> tuple:
        if subpath == "/artifact":
            key = normalize_key((query.get("key") or [""])[0])
            if key is None:
                return 400, {"error": "invalid artifact session key"}
            try:
                row, data = self._artifacts.read(key, (query.get("id") or [""])[0])
                return 200, build_artifact_page(row, data).encode(), "text/html; charset=utf-8"
            except FileNotFoundError:
                return 404, {"error": "saved artifact is missing; register the file again"}
            except (ValueError, OSError) as exc:
                return 400, {"error": str(exc)}
        if subpath == "/health":
            return 200, {"ok": True, "plugin": self.name, "canvas_dir": str(canvas_dir())}
        if subpath == "/mermaid.js":
            return self._mermaid()
        if subpath == "/session-start":
            return self._session_start(query)
        if subpath == "/check":
            key = normalize_key((query.get("key") or [""])[0])
            if key is None:
                return 400, {"error": "invalid canvas key"}
            view = self._desk.check_view(key)
            if (query.get("format") or [""])[0] == "text":
                return 200, check_text(key, view).encode("utf-8"), "text/plain; charset=utf-8"
            return 200, view
        if subpath in ("/page", "/frag", "/draw"):
            session = (query.get("session") or [""])[0]
            if not SESSION_NAME_RE.fullmatch(session):
                return 400, {"error": "invalid session name"}
            if subpath == "/page":
                host = artifact_host or f"127.0.0.1:{self._context.status_port}"
                page = build_page(session, make_nonce(), host, self._framers)
                return 200, page.encode("utf-8"), "text/html; charset=utf-8"
            if subpath == "/draw":
                return self._desk.draw_view(session)
            view = self._desk.fragment_view(session)
            try:
                view["artifacts"] = self._artifacts.list(view["key"]) if view.get("key") else []
            except (ValueError, OSError) as exc:
                view.update(artifacts=[], artifact_error=f"Cannot read artifacts: {exc}")
            return 200, view
        return 404, {
            "error": "unknown route — GET /health, /session-start, /page, /frag, /check, /draw, /mermaid.js; POST /reply, /view"
        }

    def handle_post(self, subpath: str, query: dict[str, list[str]], body: dict | None) -> tuple:
        if subpath == "/artifacts/register":
            return self._register_artifact(body)
        if subpath == "/nudge":
            return self._nudge(query, body)
        if subpath == "/check":
            return self._check_written(body)
        if subpath not in ("/reply", "/view"):
            return 404, {"error": "unknown route — POST /reply, /view, /nudge, /check"}
        if not isinstance(body, dict):
            return 400, {"error": "JSON body required"}
        session = body.get("session")
        if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
            return 400, {"error": "invalid session name"}
        if subpath == "/view":
            # Not narrowed like /reply: it flips which face the pane shows and types nothing,
            # and the ◇ SKILLS button that calls it lives on the ttyd page's own origin.
            return self._desk.set_view(session, body.get("view"))
        if "answers" in body:
            return self._desk.reply_answers(session, body["answers"], body.get("rev"))
        answer = body.get("answer")
        if not isinstance(answer, str) or not answer.strip():
            return 400, {"error": "answer required"}
        return self._desk.reply(session, answer, body.get("rev"), body.get("question"))

    def _nudge(self, query: dict[str, list[str]], body: dict | None) -> tuple:
        """200 with the hook's stdout, which is EMPTY whenever there is nothing to say —
        the hook prints the body as-is, so an error must never be a 2xx."""
        session = (query.get("session") or [""])[0]
        if not SESSION_NAME_RE.fullmatch(session):
            return 400, {"error": "invalid session name"}
        key = self._desk.key_for(session)
        artifact = None
        if isinstance(body, dict) and body.get("tool_name") == "Write":
            inputs = body.get("tool_input")
            path = inputs.get("file_path") if isinstance(inputs, dict) else None
            if isinstance(path, str) and Path(path).suffix.lower() in (".html", ".htm"):
                status, result = self._register_artifact({"session": session, "path": path})
                if status == 200:
                    artifact = f"Artifact saved to this session's Canvas shelf: {result['artifact']['name']}."
        output = self._nudger.consider(key, body) if key else None
        if artifact:
            text = output["hookSpecificOutput"]["additionalContext"] if output else ""
            output = hook_output(f"{artifact}\n{text}".strip())
        data = json.dumps(output).encode("utf-8") if output else b""
        return 200, data, "application/json"

    def _register_artifact(self, body: dict | None) -> tuple:
        if not isinstance(body, dict):
            return 400, {"error": "JSON body required"}
        session, path = body.get("session"), body.get("path")
        if not isinstance(session, str) or not SESSION_NAME_RE.fullmatch(session):
            return 400, {"error": "invalid session name"}
        if not isinstance(path, str) or not path or len(path) > 4096:
            return 400, {"error": "artifact path required"}
        key = self._desk.key_for(session)
        workspace = workspace_for_session(session) if key else None
        if key is None or workspace is None:
            return 404, {"error": "session workspace unavailable"}
        try:
            row = self._artifacts.register(key, workspace, path)
        except (OSError, ValueError) as exc:
            return 400, {"error": str(exc)}
        return 200, {"key": key, "artifact": row}

    def _mermaid(self) -> tuple:
        try:
            return 200, mermaid_js(), "application/javascript; charset=utf-8"
        except VendorError as exc:
            self._context.log(f"agent-canvas: {exc}")
            return 500, {"error": str(exc)}

    def _session_start(self, query: dict[str, list[str]]) -> tuple:
        """200 with the text the hook prints, EMPTY when this session has no canvas:
        the hook prints the body as-is, so an error must never be a 2xx."""
        session = (query.get("session") or [""])[0]
        if not SESSION_NAME_RE.fullmatch(session):
            return 400, {"error": "invalid session name"}
        key = self._desk.key_for(session)
        if key is None:
            return 200, b"", "text/plain; charset=utf-8"
        try:
            ensure_canvas_dir()
        except OSError as exc:
            return 500, {"error": f"cannot create the canvas directory: {exc}"}
        return 200, session_start_text(key).encode("utf-8"), "text/plain; charset=utf-8"

    def _check_written(self, body: dict | None) -> tuple:
        """The issues in the typed canvas a Write or Edit just touched, as the hook
        prints them; empty for every other file. The canvas directory is this
        process's to know, so the hook relays every key-shaped `.json` here."""
        empty = (200, b"", "text/plain; charset=utf-8")
        inputs = body.get("tool_input") if isinstance(body, dict) else None
        raw = inputs.get("file_path") if isinstance(inputs, dict) else None
        if not isinstance(raw, str) or not raw.endswith(".json"):
            return empty
        key = normalize_key(Path(raw).stem)
        if key is None or Path(raw).stem != key:
            return empty
        try:
            if Path(raw).resolve() != canvas_path(key, ".json").resolve():
                return empty
        except OSError:
            return empty
        return 200, check_text(key, self._desk.check_view(key)).encode("utf-8"), "text/plain; charset=utf-8"
