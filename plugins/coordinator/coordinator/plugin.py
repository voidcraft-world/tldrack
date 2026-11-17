"""The plugin object the bridge mounts: GET /state, /live; POST /audit,
/dismiss, /restore, /resume.

Route handlers return `(status, dict)` per the host contract
(plugins/README.md). The host already applied the Origin guard to POSTs
and parsed any JSON body — `/resume` is the one route here that ACTS on the
machine, and it inherits that guard rather than carrying a weaker one of its own.

── Off until the operator turns it on ─────────────────────────────────────
Every route reads the operator's own Claude transcripts, so nothing happens until
`plugins.coordinator.enabled` is `true` (README § Settings). Off, the plugin stays
mounted — a `create_plugin` that returned nothing would be logged as a load failure —
but it builds nothing: no store read, no thread, no transcript or index scan. It
advertises no routes and answers every request 404 `{"error": "off"}`, which is
exactly what a bridge without the plugin answers, so a client needs no second case.

── The shape of the interaction ───────────────────────────────────────────
`GET /state` is CHEAP and is what a client polls: it reads the store and
does no scanning whatsoever. `POST /audit` is the expensive half — tens of MB of
transcript tails, plus an optional classification pass — so it runs on a
background thread and the route answers `status: "scanning"` immediately.
Plugin handlers execute on the bridge's request threads; blocking one for a
minute would stall the status listener every client depends on. `POST /resume`
is synchronous by contrast, because `tmux new-session -d` returns as soon as the
pane is forked — the agent's own startup happens inside the session, not here.

Dismissals are applied on the way OUT of the store rather than baked into the
scan, so `POST /dismiss` and `POST /restore` take effect on the very next
`GET /state` without re-reading a single transcript.

`GET /live` is the odd one out: it is not about loose ends at all. It serves the
pid-checked live-session index — every Claude process on this machine, in tmux or
not — because this plugin is the one reader of `~/.claude/sessions` and plugins
cannot import each other. The rationale is on `_live`.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping

from coordinator.claude_cli import resolve_claude_bin
from coordinator.laya_rank import loopback_poster, run_laya_rank
from coordinator.rank import apply_ranking, rank_candidates, run_rank
from coordinator.resume import (
    decide_resume,
    inline_env,
    resolve_cwd,
    run_resume_spawn,
    session_exists,
    session_name_for,
    spawn_resume_session,
    tmux_found,
)
from coordinator.sources import (
    DEFAULT_MAX_AGE_DAYS,
    DEFAULT_SCAN_LIMIT,
    claude_homes,
    live_agent_rows,
    live_session_ids,
    owning_transcript,
    pid_alive,
    read_live_sessions_all,
    scan_all,
)
from coordinator.store import CoordinatorStore, is_finding_key, resolve_store_path

PLUGIN_NAME = "coordinator"
#: How to turn the plugin on, in the load log line and in every answer while it is off.
ENABLE_HINT = f"set plugins.{PLUGIN_NAME}.enabled in settings.json"
#: Every route's answer while the plugin is off (module header).
OFF_ANSWER = {"error": "off", "enabled": False, "plugin": PLUGIN_NAME, "hint": ENABLE_HINT}


def read_settings(context: object) -> Mapping:
    """`plugins.coordinator` of the operator's settings.json, or `{}`.

    The seam and its rules: owned by plugins/README.md — see the note there. A host
    without it, a reader that raises, or a non-object value all read as "every setting
    at its default", which here means off.
    """
    reader = getattr(context, "plugin_settings", None)
    if not callable(reader):
        return {}
    try:
        settings = reader(PLUGIN_NAME)
    except Exception:  # noqa: BLE001 — a broken seam costs the settings, never the plugin
        return {}
    return settings if isinstance(settings, Mapping) else {}


def setting_enabled(settings: Mapping, log) -> bool:
    """`enabled`, validated: only a JSON `true` turns the plugin on. Anything else is off,
    and a value that is present but not a boolean says so in the log."""
    enabled = settings.get("enabled", False)
    if isinstance(enabled, bool):
        return enabled
    log(f"  {PLUGIN_NAME}: plugins.{PLUGIN_NAME}.enabled must be true or false, not {type(enabled).__name__}; staying off")
    return False


class CoordinatorPlugin:
    name = PLUGIN_NAME

    def __init__(self, context) -> None:
        self._context = context
        self._lock = threading.Lock()
        # Its own lock, NOT `_lock`: that one guards the audit flag and is taken
        # around cheap bookkeeping, while this one is held across a `tmux`
        # round-trip. Sharing them would make a RESUME click block `/audit`.
        self._resume_lock = threading.Lock()
        self._scanning = False
        self.enabled = setting_enabled(read_settings(context), self._log)
        if not self.enabled:
            # Off builds nothing (module header): the store is not even read.
            self._store = None
            self._log(f"  {PLUGIN_NAME}: off ({ENABLE_HINT})")
            return
        self._store = CoordinatorStore(resolve_store_path())

    def _log(self, line: str) -> None:
        log = getattr(self._context, "log", None)
        if callable(log):
            log(line)

    def routes(self) -> list[str]:
        if not self.enabled:
            return []
        base = f"/plugin/{self.name}"
        return [f"{base}/state", f"{base}/audit", f"{base}/dismiss", f"{base}/restore", f"{base}/resume", f"{base}/live"]

    def busy_reasons(self) -> list[str]:
        """The host's busy seam: owned by bridge/tldrack/freshness.py — see the note there."""
        with self._lock:
            return ["coordinator audit running"] if self._scanning else []

    # -- GET ----------------------------------------------------------------
    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        if not self.enabled:
            return 404, dict(OFF_ANSWER)
        if subpath == "/state":
            return 200, self._state()
        if subpath == "/live":
            return 200, self._live()
        return 404, {"error": "unknown route — GET /state or /live"}

    # -- POST ---------------------------------------------------------------
    def handle_post(self, subpath: str, query: dict[str, list[str]], body: dict | None) -> tuple:
        if not self.enabled:
            return 404, dict(OFF_ANSWER)
        payload = body if isinstance(body, dict) else {}
        if subpath == "/audit":
            return self._handle_audit(query, payload)
        if subpath == "/dismiss":
            return self._handle_dismiss(query, payload)
        if subpath == "/restore":
            self._store.restore()
            return 200, self._state()
        if subpath == "/resume":
            return self._handle_resume(query, payload)
        return 404, {"error": "unknown route — POST /audit, /dismiss, /restore or /resume"}

    def _handle_audit(self, query: dict[str, list[str]], body: dict) -> tuple:
        deep = _flag(query, body, "deep")
        scan_limit = _positive_int(query, body, "scan_limit", DEFAULT_SCAN_LIMIT)
        max_age_days = _positive_int(query, body, "max_age_days", DEFAULT_MAX_AGE_DAYS)
        # The caller may name the session it is speaking for so its own
        # half-finished transcript is not audited back at it. `scan_all` also
        # excludes every session Claude is currently busy in, which covers the
        # common case where nobody passed anything.
        exclude = {value for value in [_text(query, body, "session_id")] if value}

        with self._lock:
            if self._scanning:
                # Already running: answering the current state is the honest,
                # idempotent response — a second worker would just duplicate work.
                return 200, self._state()
            self._scanning = True

        # Snapshotted BEFORE the worker starts, so the answer is deterministic:
        # the previous scan's findings plus `status: "scanning"`. Reading it
        # afterwards would report `idle` whenever the worker happened to finish
        # first, which is exactly the "re-fetch after a delay" cue the
        # client keys off.
        state = self._state()
        worker = threading.Thread(
            target=self._audit_worker,
            kwargs={"deep": deep, "scan_limit": scan_limit, "max_age_days": max_age_days, "exclude": exclude},
            daemon=True,
            name="coordinator-audit",
        )
        try:
            worker.start()
        except Exception as exc:  # noqa: BLE001 — thread/FD exhaustion, mostly
            # `_scanning` is set BEFORE the start (so a second POST cannot race
            # a second worker in), which means a start that never happens wedges
            # the plugin for the life of the bridge: `/state` reports `scanning`
            # forever, every later `/audit` takes the early return, and the
            # client burns its whole rescan ladder on a scan that will never
            # land. Only the flag has to be undone — no worker ever ran.
            with self._lock:
                self._scanning = False
            return 500, {"error": f"could not start the audit: {type(exc).__name__}: {exc}"[:300]}
        return 200, state

    def _handle_dismiss(self, query: dict[str, list[str]], body: dict) -> tuple:
        key = _text(query, body, "key")
        if not is_finding_key(key):
            return 400, {"error": "expected a finding key like '<session prefix>:<kind>'"}
        self._store.dismiss(key)
        return 200, self._state()

    def _handle_resume(self, query: dict[str, list[str]], body: dict) -> tuple:
        """Reopen a finding's conversation in a detached tmux session.

        The input is the finding KEY, never a session id: the store already
        holds the findings keyed by exactly that string, so the id the spawn
        actually runs never has to cross the bus (the panel forwards
        `{action, key}` and nothing more), and the route can only ever resume a
        conversation the last audit actually surfaced.
        """
        key = _text(query, body, "key")
        finding = self._finding_for(key)
        session_id = (finding or {}).get("session_id")
        claude_bin = resolve_claude_bin()
        # Read the live index at CLICK time, never off the finding: a scan is
        # minutes old by the time anyone acts on it, and this is the check that
        # keeps a second agent out of a transcript some other pane still owns.
        homes = self._homes()
        session_live = isinstance(session_id, str) and session_id in live_session_ids(read_live_sessions_all(homes))
        refusal = decide_resume(key, session_id, finding is not None, claude_bin is not None, tmux_found(), session_live)
        if refusal is not None:
            return refusal

        session = session_name_for(finding.get("label"), session_id)
        # The probe and the spawn are ONE critical section. Apart they are a
        # check-then-act, and two overlapping clicks on the same row (a
        # double-click racing the arm timeout, or two rack windows) both see
        # "not there" and both spawn. tmux backstops the dangerous half — it
        # refuses the duplicate name, so no second agent is ever forked into one
        # transcript — but only this lock keeps the loser from being reported as
        # a spawn failure for a session that in fact came up.
        with self._resume_lock:
            if session_exists(session):
                # IDEMPOTENT, and deliberately a success rather than a 409: a
                # second RESUME on the same finding must select the pane that is
                # already there (hence the deterministic name in
                # `session_name_for`).
                return 200, self._resumed(key, session, created=False)

            cwd = resolve_cwd(finding.get("cwd"), getattr(self._context, "start_dir", ""))
            argv = self._resume_argv(session_id, session, cwd, claude_bin, homes)
            if argv is not None:
                code, payload = run_resume_spawn(argv, session)
            else:
                code, payload = spawn_resume_session(claude_bin, session_id, session, cwd, inline_env())
        if code != 200:
            return code, payload
        # False when tmux itself rejected the name (a race this process did not
        # serialize — a second bridge, or an operator's own `tmux new-session`).
        created = payload.get("created") is True
        if created:
            self._log(f"  coordinator: resumed session {session_id[:8]} in tmux session '{session}'")
        return 200, self._resumed(key, session, created=created)

    def _homes(self):
        """Every Claude config dir to read — see `sources.claude_homes`."""
        return claude_homes(getattr(self._context, "profiles", None))

    def _resume_argv(self, session_id: str, session: str, cwd, claude_bin: str, homes) -> list[str] | None:
        """The spawn argv for a RESUME, under the account that OWNS the
        transcript, or None on a host without launch profiles (the caller then
        takes the legacy bare-`claude` path).

        Decided at click time from where the transcript file actually is
        (`sources.owning_transcript`), so findings persisted before a second
        account existed need no migration. A conversation found nowhere falls
        back to the default CLAUDE profile, which is what the legacy path did —
        never to a Codex default, which has no `--resume=<claude id>`.
        The resume args come from the host's `resume_args` (tldrack
        profiles.py owns the `=`-attached fence — see the note there); a host
        that predates it gets the same literal shape from here.
        """
        profiles = getattr(self._context, "profiles", None)
        if profiles is None:
            return None
        transcript = owning_transcript(session_id, homes)
        owner = (
            profiles.profile_for_path(str(transcript), kind="claude", first_on_tie=True) if transcript is not None else None
        ) or profiles.default_of_kind("claude")
        if owner is None:
            return None  # no Claude profile at all — the legacy path still can
        host_resume_args = getattr(profiles, "resume_args", None)
        resume = host_resume_args("claude", session_id) if callable(host_resume_args) else None
        return profiles.spawn_argv(owner, session, str(cwd), extra_args=resume or [f"--resume={session_id}"], cli_bin=claude_bin)

    def _finding_for(self, key: str) -> dict | None:
        """The finding behind a key, read from the RAW scan rather than the
        dismissal-filtered view: a row dismissed between the click and the POST
        should still resume, not 400."""
        scan, _dismissed = self._store.snapshot()
        for finding in scan.get("findings") or []:
            if finding.get("key") == key:
                return finding
        return None

    # -- the background audit -----------------------------------------------
    def _audit_worker(self, *, deep: bool, scan_limit: int, max_age_days: int, exclude: set[str]) -> None:
        try:
            scan = scan_all(scan_limit=scan_limit, max_age_days=max_age_days, exclude_session_ids=exclude, homes=self._homes())
            # Persisted BEFORE the deep pass: if ranking then fails or times out,
            # the user still gets the heuristic findings. The heuristic layer
            # stands alone by design.
            self._store.set_scan(scan)
            self._log(f"  coordinator: audited {scan['scanned']} transcripts — {len(scan['findings'])} findings")
            if deep:
                self._store.set_scan(
                    _rank_scan(scan, getattr(self._context, "profiles", None), getattr(self._context, "status_port", None))
                )
        except Exception as exc:  # noqa: BLE001 — a worker crash must not wedge the plugin
            scan, _dismissed = self._store.snapshot()
            scan["error"] = f"audit failed: {type(exc).__name__}: {exc}"[:300]
            self._store.set_scan(scan)
        finally:
            with self._lock:
                self._scanning = False

    def _state(self) -> dict:
        """The CoordinatorState wire shape. Dismissals are subtracted here, and
        `dismissed` reports how many of THIS scan's findings they suppressed —
        which is what a "3 hidden · RESTORE" control needs to render."""
        scan, dismissed = self._store.snapshot()
        raw = scan.get("findings") or []
        kept = [finding for finding in raw if finding.get("key") not in dismissed]
        return {
            "status": "scanning" if self._scanning else "idle",
            "scanned_at": scan.get("scanned_at"),
            "scanned": scan.get("scanned", 0),
            "ranked": bool(scan.get("ranked")),
            "ranked_by": scan.get("ranked_by"),
            "dismissed": len(raw) - len(kept),
            "error": scan.get("error"),
            "findings": kept,
        }

    def _live(self) -> dict:
        """`{agents, count}` — every Claude process running on this machine now.

        Read fresh on every GET, deliberately. The whole read is one glob of
        small files (one per running Claude process) plus a `kill(pid, 0)` each;
        a cache would cost a lock and a TTL to save less than the request itself
        costs, and a client polling this every few seconds to place agents on
        their branches would show an agent where it no longer is. Reading the
        index at request time is also exactly what `/resume` does at click
        time, for the same reason. Every Claude account is read (`_homes`), so
        an agent on a second account shows up like any other.

        Every `cwd` is `~`-collapsed: the host's Origin guard covers POST only,
        so what this GET returns is the boundary — `sources.display_path` names
        the owner of that argument.
        """
        rows = live_agent_rows(read_live_sessions_all(self._homes()), pid_alive)
        return {"agents": rows, "count": len(rows)}

    def _resumed(self, key: str, session: str, *, created: bool) -> dict:
        """The usual CoordinatorState plus a top-level `resumed` block.

        Additive, per the host's JSON discipline — every caller already knows how
        to read a CoordinatorState, so RESUME answers with a fresh one and hangs
        the outcome off the side rather than inventing a second wire shape.
        `created` is False when the session was already open (the idempotent
        path), and the caller needs it to tell "spawned" from "selected".
        """
        state = self._state()
        state["resumed"] = {"key": key, "session": session, "created": created}
        return state


def _rank_scan(scan: dict, profiles: object | None, status_port: int | None = None, laya: object | None = None) -> dict:
    """Deep pass over one heuristic scan → the scan to persist.

    Laya on this machine first (`laya_rank.py`: free, private, no Claude quota), the
    `claude -p` pass when Laya cannot answer. `ranked_by` says which one sorted the list, and a fallback
    keeps Laya's reason in `error` so a silently-skipped local model is visible.

    A failed ranking is reported in `error` but never empties `findings`: the
    caller asked for a better-sorted list, not for nothing.
    """
    candidates = rank_candidates(scan["findings"])
    ranked_by = None
    laya_note = None
    poster = laya or (loopback_poster(status_port) if isinstance(status_port, int) else None)
    if poster is not None:
        ranking, laya_note = run_laya_rank(candidates, poster)
        if ranking:
            ranked_by = "laya"
    else:
        ranking = {}
    error = None
    if not ranking:
        ranking, error = run_rank(candidates, profiles=profiles)
        if ranking:
            ranked_by = "claude"
        if laya_note:
            error = f"{laya_note} — used Claude instead" if ranking else f"{laya_note}; {error}"
    ranked = dict(scan)
    if ranking:
        ranked["findings"] = apply_ranking(scan["findings"], ranking)
        ranked["ranked"] = True
    ranked["ranked_by"] = ranked_by
    ranked["error"] = error
    return ranked


# ── request-value helpers (query string wins; body is the fallback) ─────────


def _raw(query: dict[str, list[str]], body: dict, name: str):
    values = query.get(name)
    if values:
        return values[0]
    return body.get(name)


def _flag(query: dict[str, list[str]], body: dict, name: str) -> bool:
    # The host parses the query with a plain `parse_qs`, which DROPS valueless
    # keys — a bare `?deep` never reaches here, so the URL form must be
    # `?deep=1`, and `?<name>=1` for every flag in every plugin. The body
    # (`{"deep": true}`) is the contract's primary path.
    if name in query:
        return (query.get(name) or [""])[0].strip().lower() not in ("0", "false", "no")
    value = body.get(name)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes")
    return value is True


def _positive_int(query: dict[str, list[str]], body: dict, name: str, default: int) -> int:
    raw = _raw(query, body, name)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _text(query: dict[str, list[str]], body: dict, name: str) -> str:
    raw = _raw(query, body, name)
    return raw.strip() if isinstance(raw, str) else ""
