"""The plugin object the bridge mounts. Routes table: plugins/laya/README.md; shapes: schemas/src/decide.ts, laya-review.ts.

`POST /decide` is the one hot route. It answers SYNCHRONOUSLY when Laya is up — a warm decision is
tens of milliseconds, well inside a request thread — and with `503 warming` otherwise, after
kicking a start in the background. Callers (the SPA's paste router, the coordinator, the agent
rack) treat `warming` as "fall back to the rules this time"; nothing ever waits a cold start out.
Every 200 is also recorded in the decision ledger (`ledger.py`), the one seam: callers only tag.

Two guards. The host applies the bridge's Host and Origin guards before anything reaches here
(plugins/README.md); `/status`, `/decide` and `/react` rely on those alone. Every route that
carries the operator's text or acts on this machine is a POLICY route and also passes the plugin's
own caller policy (`guard.policy_for`, built once at mount from what the host's context says).

`handle_request` is what the host calls: it is the only entry that sees headers, so it is the only
one that can judge a caller. `handle_get`/`handle_post` stay for tldrack's in-process caller
(`open_work.plugin_caller`): they answer `/status /decide /react /landing /start /stop /install` as before,
and every other policy route with 403 `host_too_old` — a caller the plugin cannot see is a caller
it cannot check. Request bodies are re-checked by `wire.clean_request`, so only the documented
`/v1/systemone` fields ever reach laya-serve (the `ledger` tag never does).

`/review/rack` is the one route with NO HTTP door (`RACK_GRADE`): it writes an operator label from a decision id
alone, so it is not mounted, and `handle_request` refuses it should a host ever route it. Its one way in is
`rack_grade(body, headers)`, called in process by tldrack for the operator's press of the ⚠️'s own menu
(`/open-work/dismiss`, `/open-work/confirm`). The bridge's own guards admit any loopback origin and any content
type, which is right for clearing a mark and too weak for an operator label: a page served from another localhost
port could post `{"session"}` as a simple request. So the bridge hands over the headers of the HTTP request that
CAUSED the grade, and the press passes the same checks as `/review/answer` (the caller policy, a JSON body) before a
row is written. `handle_post` carries no headers, so it answers the route `host_too_old` like every policy route.
"""

from __future__ import annotations

import os
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from laya_bridge import card_files, files, guard, lab_jobs, review
from laya_bridge.landing import LandingError, LandingLog, clean_landing
from laya_bridge.envelope import Refusal, error_response, ok_body, refusal_response
from laya_bridge.ledger import Ledger, LedgerTagError, clean_react, clean_tag
from laya_bridge.ops import ops_state
from laya_bridge.paths import PathsError, ensure_laya_dir, laya_dir, registry_view, served_name
from laya_bridge.runtime import LayaRuntime
from laya_bridge.wire import BUILTIN_MODELS, WireError, clean_request

PLUGIN_NAME = "laya"

#: What a `warming` / `not_installed` caller is told to wait before asking again.
WARMING_RETRY_MS = 1_500

INSTALL_HINT = "tldrack laya install (or POST /plugin/laya/install): uv sync --project plugins/laya/runtime"

#: A `/decide` state may be a long hand-off; the host's 4 KB default refused every one past it.
MAX_BODY_BYTES = 256 * 1024

POLICY_GETS = ("/card", "/ops", "/review/queue", "/review/item", "/review/recent")
POLICY_POSTS = (
    "/card/publish",
    "/challenger",
    "/lab",
    "/restart",
    "/review/answer",
    "/review/retest",
    "/review/undo",
    "/install",
    "/start",
    "/stop",
)
JSON_POSTS = ("/card/publish", "/challenger", "/lab", "/review/answer", "/review/retest", "/review/undo")
#: The agent rack's grade (`review.rack`): in process only, through `rack_grade` (the module note owns why).
RACK_GRADE = "/review/rack"
LIFECYCLE_POSTS = ("/start", "/stop", "/install")

Response = tuple[int, dict]

#: How often the drain thread looks for the lab queue's next turn while anything waits (one `tmux list-panes`).
DRAIN_POLL_S = 15.0


def start_daemon(work: Callable[[], None]) -> None:
    """The drain's thread. Looked up at each kick, so tests/conftest.py can replace it: no test may start a real
    lab job."""
    threading.Thread(target=work, name="laya-lab-drain", daemon=True).start()


class LabDrain:
    """Starts the lab queue's next job when the running one ends, with nobody watching: lab_jobs.py owns the queue.

    One daemon thread, alive only while something waits. It is kicked by a press that queued, by an OPS read that
    finds a queue, and at mount when a queue outlived a bridge restart; it exits when a pass leaves nothing waiting
    and on `close`. `start` replaces `start_daemon` (tests run the loop on their own thread).

    A kick that finds the loop alive is STICKY (`_again`): the pass that just read an empty queue has already let go
    of the queue lock, so a press queued after that read would otherwise have no thread left to start it, and
    the queue would wait for an OPS read, another press or a restart. The loop exits only through `_retire`, which
    gives it one more pass when a kick landed after the current pass began.
    """

    def __init__(
        self,
        advance: Callable[[], lab_jobs.Advance],
        log: Callable[[str], None],
        interval: float = DRAIN_POLL_S,
        start: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        self._advance, self._log, self._interval = advance, log, interval
        self._start = start
        self._lock = threading.Lock()
        self._alive = False
        self._again = False
        self._stop = threading.Event()

    def kick(self) -> None:
        with self._lock:
            if self._stop.is_set():
                return
            if self._alive:
                self._again = True
                return
            self._alive, self._again = True, False
        try:
            (self._start or start_daemon)(self._loop)
        except BaseException:  # a thread that never started must not leave `_alive` set and every later kick a no-op
            with self._lock:
                self._alive = False
            raise

    def _retire(self) -> bool:
        """End the loop unless a kick landed since this pass began (that kick's press may sit behind the pass's read)."""
        with self._lock:
            if self._again:
                self._again = False
                return False
            self._alive = False
            return True

    def _loop(self) -> None:
        retired = False
        try:
            while not self._stop.wait(self._interval):
                with self._lock:
                    self._again = False  # an earlier kick's press wrote the queue before kicking: this pass reads it
                try:
                    done = self._advance()
                except Exception as error:  # noqa: BLE001 — a pass that dies must not leave the queue undrained forever
                    self._log(f"[laya] lab queue pass failed: {type(error).__name__}")
                    continue
                log_advance(done, self._log)
                if done.held and done.waiting == 0 and self._retire():
                    retired = True
                    return
        finally:
            if not retired:  # `close`, or an error past the pass guard; `_retire` already cleared it otherwise
                with self._lock:
                    self._alive = False

    def close(self) -> None:
        self._stop.set()


def log_advance(done: lab_jobs.Advance | None, log: Callable[[str], None]) -> None:
    """One line per job a pass started or dropped (a drop is a queued press whose gate no longer held)."""
    if done is None:
        return
    if done.started is not None:
        verb, _surface, session = done.started
        log(f"[laya] lab queue: {verb} started in {session}")
    for verb, _surface, refusal in done.dropped:
        log(f"[laya] lab queue: {verb} dropped — {refusal.code}: {refusal.message}")


def host_too_old() -> Response:
    return error_response(403, "host_too_old", "This terminal bridge does not pass request headers to plugins: update tldrack")


def not_found() -> Response:
    """404 keeps its repo-wide meaning: the route is missing (the string is unchanged)."""
    return 404, {"error": "not found"}


def no_ledger() -> tuple[int, dict]:
    """The 503 every ledger route answers while the laya store is not there."""
    return 503, {"error": "no_ledger", "detail": "the decision ledger is off: the laya data folder is not there"}


def is_json(headers: Mapping[str, str] | None) -> bool:
    media = str((headers or {}).get("content-type", "")).split(";", 1)[0].strip().lower()
    return media == "application/json"


def caller_refused(headers: Mapping[str, str] | None, *, json_body: bool, policy: Any) -> Response | None:
    """The policy posture as a response, or None when the caller passes: `policy` (`guard.policy_for`), then (for a
    route that takes a JSON body) the content type. `headers` are lowercased names, as the host hands them over."""
    refusal = policy.refusal(headers)
    if refusal is not None:
        origins = {"allowed_origins": list(refusal.allowed_origins)} if refusal.allowed_origins else {}
        return error_response(403, refusal.code, refusal.message, **origins)
    if json_body and not is_json(headers):
        return error_response(415, "unsupported_media_type", "Send the body as application/json")
    return None


def plugin_settings(context: Any) -> Mapping[str, Any]:
    """This plugin's object of the operator's settings, as the host hands it over (plugins/README.md § What the
    context carries); `{}` on a host that carries none. Keys: README.md § Settings."""
    read = getattr(context, "plugin_settings", None)
    try:
        found = read(PLUGIN_NAME) if callable(read) else None
    except Exception:  # noqa: BLE001 — a host's settings reader must never keep the plugin unmounted
        found = None
    return found if isinstance(found, Mapping) else {}


class LayaPlugin:
    name = PLUGIN_NAME
    max_body_bytes = MAX_BODY_BYTES

    def __init__(
        self,
        context: Any,
        runtime: LayaRuntime | None = None,
        ledger: Ledger | None = None,
        params_dir: Callable[[], Path | None] | None = None,
        drain: LabDrain | None = None,
        landing: LandingLog | None = None,
    ) -> None:
        log = getattr(context, "log", None)
        self._log: Callable[[str], None] = log if callable(log) else (lambda _m: None)
        self._policy = guard.policy_for(
            getattr(context, "status_port", None), guard.operator_origins(getattr(context, "allowed_origins", None), self._log)
        )
        self._runner = lab_jobs.runner_dir(plugin_settings(context).get("evals_dir"), self._log)
        self._card_repo = card_files.card_repo(plugin_settings(context))
        self._params_dir = params_dir or self._store
        if params_dir is None:
            self._make_store()
        self._runtime = runtime or LayaRuntime(
            extra_models=self._extra_models,
            log=self._log,
            registry=lambda: registry_view(self._params_dir()),
            first_fine_tune=self._first_fine_tune,
        )
        self._ledger = ledger or Ledger(self._params_dir, log=self._log)
        self._landing = landing or LandingLog(self._params_dir, log=self._log)
        self._drain = drain or LabDrain(self._advance_lab, self._log)
        self._tighten()
        if lab_jobs.read_queue(self._params_dir()):
            self._drain.kick()  # a queue that outlived a bridge restart

    def _store(self) -> Path | None:
        """The laya store (paths.py owns where). None only while its mapping is unusable, which every route then
        answers as it answers a missing folder; the bridge refuses to start on the same file, so that is a mapping
        edited under a running bridge."""
        try:
            return laya_dir()
        except PathsError:
            return None

    def _make_store(self) -> None:
        """Create the store once at mount, so a first run has a ledger with nothing configured. Never fails a
        mount: without it the ledger is off and `/decide` still answers."""
        try:
            ensure_laya_dir()
        except (OSError, PathsError) as error:
            self._log(f"[laya] the laya data folder is not there ({type(error).__name__}): the ledger is off")

    def _first_fine_tune(self) -> dict[str, Any]:
        """What `/status` says about the road to a first fine-tune (`review.first_fine_tune` owns the count)."""
        return review.first_fine_tune(self._params_dir())

    def _tighten(self) -> None:
        """Repair operator file modes once at mount (files.tighten_params owns the rule). Never fails a mount."""
        try:
            base = self._params_dir()
            if base is not None and files.is_real_dir(base):
                for relative in files.tighten_params(base):
                    self._log(f"[laya] tightened {relative}")
        except Exception:  # noqa: BLE001 — a mode repair must never keep the plugin unmounted
            pass

    def _extra_models(self) -> dict[str, str]:
        """What laya-serve loads: the serving view, one @champion per surface (a @challenger never reaches it)."""
        return {name: entry["path"] for name, entry in registry_view(self._params_dir())["models"].items()}

    def _advance_lab(self) -> lab_jobs.Advance:
        return lab_jobs.advance(self._params_dir(), loaded=self._runtime.loaded(), runner=self._runner)

    def close(self) -> None:
        self._drain.close()
        self._runtime.close()

    def routes(self) -> list[str]:
        base = f"/plugin/{self.name}"
        subpaths = (
            "/status",
            "/decide",
            "/react",
            "/landing",
            "/start",
            "/stop",
            "/install",
            "/restart",
            "/ops",
            "/card",
            "/card/publish",
            "/lab",
            "/challenger",
            "/review/queue",
            "/review/item",
            "/review/recent",
            "/review/answer",
            "/review/retest",
            "/review/undo",
        )
        return [f"{base}{subpath}" for subpath in subpaths]

    # -- the host's entry ---------------------------------------------------
    def handle_request(
        self, method: str, subpath: str, query: dict[str, list[str]] | None, body: Any, headers: Mapping[str, str] | None
    ) -> tuple:
        if subpath in POLICY_GETS or subpath in POLICY_POSTS:
            posts = method == "POST" and subpath in POLICY_POSTS
            refused = caller_refused(headers, json_body=posts and subpath in JSON_POSTS, policy=self._policy)
            if refused is not None:
                return refused
            if method == "GET" and subpath in POLICY_GETS:
                return self._policy_get(subpath, query or {})
            if posts:
                return self._policy_post(subpath, body)
            return not_found()
        if subpath == RACK_GRADE:
            return 403, {"error": "forbidden", "detail": "only the agent rack, in process, records a rack grade"}
        if method == "POST" and subpath == "/decide":
            return self._decide(body, rack_ok=guard.is_native(headers))
        if method == "POST" and subpath == "/landing" and not guard.is_native(headers):
            return 403, {"error": "forbidden", "detail": "only the agent rack records where a verdict landed"}
        if method == "GET":
            return self.handle_get(subpath, query or {})
        return self.handle_post(subpath, query or {}, body)

    # -- the in-process pair ------------------------------------------------
    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        if subpath == "/status":
            return 200, self._runtime.status()
        if subpath in POLICY_GETS:
            return host_too_old()
        return not_found()

    def handle_post(self, subpath: str, query: dict[str, list[str]], body: Any) -> tuple:
        if subpath == "/decide":
            return self._decide(body, rack_ok=True)  # in process is the rack itself
        if subpath == "/react":
            return self._react(body)
        if subpath == "/landing":
            return self._record_landing(body)  # in process is the rack itself
        if subpath in LIFECYCLE_POSTS:
            return self._lifecycle(subpath)
        if subpath in POLICY_POSTS or subpath == RACK_GRADE:
            return host_too_old()
        return not_found()

    def rack_grade(self, body: Any, headers: Mapping[str, str] | None) -> tuple:
        """The agent rack's grade, in process, with the headers of the HTTP request that caused it (the module note
        owns why). No headers is a caller this plugin cannot see, so it is refused like one it could not check."""
        if headers is None:
            return host_too_old()
        refused = caller_refused(
            {str(name).lower(): value for name, value in headers.items()}, json_body=True, policy=self._policy
        )
        if refused is not None:
            return refused
        return review.rack(self._params_dir(), body, loaded=self._runtime.loaded())

    def judged_before(self, conversation: object, state_hash: object) -> bool | None:
        """In process only, never a route: has the ledger already judged this agent-rack turn end? (`Ledger.judged`)"""
        return self._ledger.judged("agent_open_work", conversation, state_hash)

    # -- host routes --------------------------------------------------------
    def _lifecycle(self, subpath: str) -> tuple:
        if subpath == "/start":
            self._runtime.ensure_started()
            return 200, self._runtime.status()
        if subpath == "/stop":
            self._runtime.stop(reason="requested")
            return 200, self._runtime.status()
        self._runtime.install()
        return 202, self._runtime.status()

    def _row_model(self, payload: Mapping[str, Any], cleaned: Mapping[str, Any]) -> str:
        """The name a ledger row records: the payload's when it is served, else the pin, else `english`."""
        served = set(BUILTIN_MODELS) | set(self._runtime.loaded() or {})
        named = payload.get("model")
        named = served_name(named) if isinstance(named, str) else named
        if isinstance(named, str) and named in served:
            return named
        pinned = cleaned.get("model")
        return pinned if isinstance(pinned, str) else "english"

    def _decide(self, body: Any, *, rack_ok: bool) -> tuple:
        known = frozenset(BUILTIN_MODELS) | frozenset(self._extra_models())
        if isinstance(body, dict) and isinstance(body.get("model"), str):
            body = {**body, "model": served_name(body["model"])}  # a caller that pins a legacy name (paths.py)
        try:
            cleaned = clean_request(body, known)
            tag = clean_tag(body.get("ledger"), rack_ok=rack_ok)
        except (WireError, LedgerTagError) as error:
            return 400, {"error": "bad_request", "detail": str(error)}
        if tag is not None and not self._ledger.admits(tag["surface"]):
            return 400, {"error": "bad_request", "detail": "ledger.surface: this ledger already holds every surface it may"}

        state = self._runtime.ensure_started()
        if state == "not_installed":
            return 503, {"error": "not_installed", "state": state, "hint": INSTALL_HINT}
        if state != "ready":
            return 503, {"error": "warming", "state": state, "retry_after_ms": WARMING_RETRY_MS}
        spawn = self._runtime.spawn()
        started = time.monotonic()
        status, payload = self._runtime.decide(cleaned)
        if status != 200:
            return status, payload
        ms = (time.monotonic() - started) * 1000
        name = self._row_model(payload, cleaned)
        model_id, checkpoint = self._runtime.served_identity(name)
        decision_id = self._ledger.record_decision(tag, cleaned, payload, name, model_id, checkpoint, ms, spawn)
        return 200, {
            **payload,
            "decision_id": decision_id,
            "model_id": model_id,
            "checkpoint": Path(checkpoint).name if checkpoint else None,
        }

    def _react(self, body: Any) -> tuple:
        try:
            decision_id, action = clean_react(body)
        except LedgerTagError as error:
            return 400, {"error": "bad_request", "detail": str(error)}
        outcome, duplicate = self._ledger.react(decision_id, action)
        if outcome == "off":
            return no_ledger()
        if outcome == "unknown":
            return 409, {"error": "unknown_decision", "detail": "no decision has that id"}
        return 200, {"ok": True, "decision_id": decision_id, "action": action, "duplicate": duplicate}

    def _record_landing(self, body: Any) -> tuple:
        """`POST /landing`: one landing row for a decision this ledger holds (landing.py owns why)."""
        try:
            cleaned = clean_landing(body)
        except LandingError as error:
            return 400, {"error": "bad_request", "detail": str(error)}
        known, surface = self._ledger.surface_of(cleaned["decision_id"])
        if known == "off":
            return no_ledger()
        if known == "unknown" or surface != cleaned["surface"]:
            return 409, {"error": "unknown_decision", "detail": "no decision on that surface has that id"}
        outcome, duplicate = self._landing.record(cleaned)
        if outcome == "off":
            return 503, {"error": "no_landing", "detail": "the landing record cannot be written"}
        return 200, {"ok": True, "decision_id": cleaned["decision_id"], "duplicate": duplicate}

    # -- policy routes ------------------------------------------------------
    def _policy_get(self, subpath: str, query: Mapping[str, Any]) -> tuple:
        base, loaded = self._params_dir(), self._runtime.loaded()
        if subpath == "/card":
            return 200, ok_body("Verifier preview", card=card_files.read(base, self._card_repo))
        if subpath == "/ops":
            queue = lab_jobs.read_queue(base)
            if queue:
                self._drain.kick()  # no start on a read thread: the drain takes the next turn
            jobs = lab_jobs.job_rows(lab_jobs.parse_lab_panes(lab_jobs.list_panes() or ""), queue, self._runner)
            return 200, ops_state(base, self._runtime.status(), jobs, loaded=loaded, runner=self._runner)
        if subpath == "/review/queue":
            return review.queue(base, query, loaded=loaded)
        if subpath == "/review/recent":
            return review.recent(base, query, loaded=loaded)
        return review.item(base, query, loaded=loaded)

    def _policy_post(self, subpath: str, body: Any) -> tuple:
        if subpath in LIFECYCLE_POSTS:
            return self._lifecycle(subpath)
        if subpath == "/restart":
            return self._restart(body)
        base, loaded = self._params_dir(), self._runtime.loaded()
        if subpath == "/card/publish":
            return self._card_publish(body, base)
        if subpath == "/challenger":
            return self._challenger(body, base)
        if subpath == "/lab":
            return self._lab(body, base, loaded)
        if subpath == "/review/answer":
            return review.answer(base, body, loaded=loaded)
        if subpath == "/review/retest":
            return review.retest(base, body, loaded=loaded)
        return review.undo(base, body, loaded=loaded)

    def _restart(self, body: Any) -> tuple:
        if body not in (None, {}):
            return error_response(400, "bad_body", "restart takes an empty body", status=None)
        if not self._runtime.installed:
            return error_response(503, "not_installed", "Laya is not installed", hint=INSTALL_HINT, status=None)
        self._runtime.stop(reason="restart")
        self._runtime.ensure_started()
        return 200, ok_body("restarting", status=self._runtime.status())

    def _card_publish(self, body: Any, base: Path | None) -> tuple:
        """Write the reviewed verifier card into this checkout's `docs/` (laya-lab `card.py` owns the files and the
        digest gate). `url` stays in the answer, empty: nothing is published anywhere by this route."""
        import json
        import subprocess
        from laya_bridge.paths import REPO_ROOT

        card = card_files.read(base, self._card_repo)
        if not isinstance(body, dict) or set(body) != {"digest"}:
            return error_response(400, "bad_body", "The reviewed card digest is required")
        if not card or body["digest"] != card["digest"] or not card["ready"]:
            return error_response(409, "card_held", "Review a current card with at least 20 measurable kappa pairs")
        try:
            result = subprocess.run(
                [str(REPO_ROOT / lab_jobs.LAB_BIN), "card", "--publish", "--digest", card["digest"]],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=180,
                env={**os.environ, **lab_jobs.store_env()},
            )
            reply = json.loads(result.stdout.strip().splitlines()[-1]) if result.returncode == 0 and result.stdout.strip() else {}
            written = reply.get("written") if isinstance(reply, dict) else None
            if not isinstance(written, list) or not written or not all(isinstance(name, str) for name in written):
                raise ValueError("the card command wrote nothing")
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired):
            return error_response(409, "publish_refused", "The card was not written; reload the preview and retry")
        return 200, ok_body("Verifier card written to docs/", url="", repository=card["repository"], written=written)

    def _challenger(self, body: Any, base: Path | None) -> tuple:
        import subprocess
        from laya_bridge.challenger import view
        from laya_bridge.paths import REPO_ROOT

        if base is None:
            return error_response(503, "params_unavailable", "The laya data folder is not there")
        if (
            not isinstance(body, dict)
            or set(body) != {"action", "surface", "token"}
            or body.get("action") not in ("promote", "reject")
            or body.get("surface") != "agent_open_work"
        ):
            return error_response(400, "bad_body", "A challenger action, surface and evidence token are required")
        state = view(base, body["surface"])
        if state is None or body["token"] != state["token"]:
            return error_response(409, "evidence_changed", "Challenger evidence changed; reload and arm again")
        output = lab_jobs.list_panes()
        if output is None or lab_jobs.running(lab_jobs.parse_lab_panes(output)) is not None:
            return error_response(409, "busy", "A lab job is running; wait before changing the challenger")
        try:
            result = subprocess.run(
                [
                    str(REPO_ROOT / lab_jobs.LAB_BIN),
                    "challenger",
                    body["action"],
                    "--surface",
                    body["surface"],
                    "--token",
                    body["token"],
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired):
            return error_response(503, "lab_unavailable", "Challenger command did not complete; reload its state")
        if result.returncode:
            return error_response(409, "not_ready", "Challenger decision refused; reload the evidence")
        if body["action"] == "promote":
            self._runtime.stop(reason="challenger promoted")
            self._runtime.ensure_started()
            (base / "challenger-restart.json").unlink(missing_ok=True)
        return 200, ok_body(
            "Challenger promoted; restarting Laya" if body["action"] == "promote" else "Challenger rejected; retention applied"
        )

    def _lab(self, body: Any, base: Path | None, loaded: Mapping[str, str] | None) -> tuple:
        """A lab button: started now (202 with its session) or queued behind the running job (202 with `queued`)."""
        try:
            verb, surface = lab_jobs.clean_lab_request(body)
            reply, done = lab_jobs.press(verb, surface, base, loaded=loaded, runner=self._runner)
        except Refusal as refusal:
            return refusal_response(refusal, session=None, queued=None)
        log_advance(done, self._log)
        if reply["queued"] is not None:
            self._log(f"[laya] lab {verb} queued #{reply['queued']}")
        if reply["queued"] is not None or (done is not None and done.waiting):
            self._drain.kick()
        return 202, reply
