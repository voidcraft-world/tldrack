"""Push an alert OFF this machine when an agent is blocked and nobody is looking.

The bridge has always known when a Claude session needs a human: `hook_state()`
turns a `Notification` hook into the `attention` state, with the
`IDLE_NOTIFICATION_RE` carve-out that separates a real permission prompt from
Claude's "waiting for your input" nag. What it has never had is a way to say so
when you are not at the machine — that signal reaches an SSE bus (needs a live
browser tab) and the macOS notifier (needs you at the desk), and stops there.
Walk away and the agent stalls in silence.

This module is the missing half. The transport lives in `sinks/`; what lives
here is the part that decides whether a human should be interrupted at all.

=== THE POLICY IS THE FEATURE ===

The naive version of this is one `curl` in the Claude Code `Notification` hook.
It takes five minutes and it is unusable, because it fires every time an agent
asks anything — including while you are staring straight at it. What makes an
alert worth receiving is the filter, not the transport, so all five properties
below are enforced in code rather than left to the caller's good judgement:

 1. **Edge-triggered, never level-triggered.** A session is notified once per
    blocked episode, not once per tick while it stays blocked. That is what
    `notified` + `cooldown_seconds` buy.
 2. **Presence suppression — the single biggest lever, and it was already
    built.** `PresenceStore` (TTL 15s) knows whether a rack surface is on
    screen. If one is, this stays silent: the in-app toast and the macOS
    notifier already have you covered, and a phone buzzing about something you
    are looking at is the exact definition of annoying.
 3. **A grace window.** Most blocks resolve in seconds when you are watching.
    Waiting `NOTIFY_GRACE_SECONDS` before saying anything converts "an agent
    asked a question" (noise) into "an agent has been stuck for a minute and a
    half and nobody noticed" (signal). It costs nothing to measure — an
    `attention` row's `age_seconds` IS its blocked duration, because
    `StatusStore` restamps only on a new report.
 4. **Coalescing.** Three agents blocking at once is ONE notification, never
    three. A burst of pushes reads as a malfunction even when each is correct.
 5. **A hard daily budget.** `NOTIFY_DAILY_BUDGET` is a ceiling enforced by a
    counter, not by care. It makes a runaway loop structurally impossible rather
    than merely unlikely, which matters for a thing that vibrates your pocket.
 6. **Once per wait, never on done** (2026-11-04): one ping when an agent starts
    waiting on a permission prompt or a question, none when it finishes. A
    wait is an EPISODE: the ticks a session spends in `attention` (or flagged
    stuck, stuck.py) without leaving it. One ping per episode, however long it
    lasts — the cooldown alone used to re-ping a two-hour wait every 15 minutes.
    The cooldown stays, as the guard between episodes. A finished turn is never
    a ping: it is an optional review, and those go in the digest (7).
 7. **Optional reviews are a digest, `digest_hours` a day.** Open work (Laya's ⚠️,
    attention.py's new or repeat loops, never a held ⏳), parked loops and turns
    that finished since the last digest, as ONE low-priority push at each hour in
    `digest_hours` (default `DEFAULT_DIGEST_HOURS`; `[]` turns it off). Skipped
    when empty or when a surface is on screen, since the rack IS the digest then.

Every ping sent and every one held back is reported to `on_signal` (the rack
metrics' attention signals, rack_metrics.py), once per episode, so a policy
change here is measured rather than asserted.

Deliberately NOT a reimplementation of the frontend's `advance_tracker`
(`ui/src/engine/terminal/terminal-status-logic.ts`). That function
debounces state DOWNGRADES to stop a UI badge flickering; this one adds a grace
delay, presence suppression and a budget. Same source signal, different consumer,
genuinely different rules — not duplication to be collapsed later.

=== WHAT MAY GO IN A MESSAGE ===

Session names and counts. That is the whole list. A name is the one the rack shows: the row's
`label` (its auto title, a few words) when it has one, else the tmux name, because `vc-3` on a
phone names nothing. See `sinks/ntfy.py` for why
(short version: on hosted ntfy.sh the topic name is the only secret, so anyone
who guesses it reads everything sent). In particular **never** include the
`message` field of an `attention` row — that is Claude's own text, and it
routinely names the file path or command it wants permission for.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path

from tldrack import paths
from tldrack.atomic_store import atomic_write
from tldrack.sinks import PRIORITY_DEFAULT, PRIORITY_HIGH, PRIORITY_LOW
from tldrack.sinks.ntfy import DEFAULT_NTFY_SERVER, NtfySink, valid_topic

#: A tool event's text (`NotifyDaemon.send_event`) is cut to these: room for a pointer
#: ("run 3 of 12 passed · seed 42"), never for a payload.
EVENT_TITLE_MAX = 60
EVENT_BODY_MAX = 160
#: ntfy tags are emoji short codes; anything else falls back to the default flag.
EVENT_TAGS_RE = re.compile(r"[a-z0-9_]{1,32}(,[a-z0-9_]{1,32}){0,2}")
#: Where a tap on an event may land instead of the configured click URL: a NAME from this table, never a URL of the
#: caller's. A push is readable by anyone who guesses the topic (`sinks/ntfy.py`), and a tap opens whatever it
#: carries, so the only targets are ones written here. `laya_recent`: the Android app's LAYA screen (the RECENT list
#: over phone-link; the deep link is owned by `android/` `LayaRules.kt` `LINK_SCHEME`).
EVENT_DOORS: dict[str, str] = {"laya_recent": "tldrack://review"}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


def _event_text(text: str, limit: int) -> str:
    """One line, no control characters, at most `limit` characters (an ellipsis marks a cut)."""
    flat = _CONTROL_RE.sub(" ", text).strip()
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


#: How long a session must sit in `attention` before it is worth interrupting a
#: human over. Tuned to be longer than "I glanced over and answered it" and
#: shorter than "I have wandered off for the afternoon".
NOTIFY_GRACE_SECONDS = 90.0

#: One alert per session per this window. A session that blocks, is answered,
#: and blocks again two minutes later is one episode as far as your phone is
#: concerned.
NOTIFY_SESSION_COOLDOWN_SECONDS = 900.0

#: Hard ceiling on notifications per local day. Not a quota (ntfy.sh allows ~250
#: anonymously) — a runaway guard. If this is ever hit in normal use, the policy
#: above is wrong and should be fixed rather than the number raised.
NOTIFY_DAILY_BUDGET = 20

#: Tick interval for the daemon. A timer is REQUIRED rather than reacting to the
#: next hook: the case this feature exists for is "the agent is blocked and
#: nothing further happens", which by definition produces no further events.
NOTIFY_TICK_SECONDS = 10.0

NOTIFY_FILE_VERSION = 1

#: The local hours a digest goes out (7). Late morning, mid-afternoon, early evening: three looks
#: a day, never at night. Editable in notify.json (`digest_hours`); `[]` turns the digest off.
DEFAULT_DIGEST_HOURS: tuple[int, ...] = (11, 15, 19)

#: Names per digest line before the rest fold into "+N" — pointers, never a payload.
DIGEST_NAMES_MAX = 4

NOTIFY_FILE = "notify.json"


def resolve_notify_path() -> Path:
    env_path = os.environ.get("TLDRACK_NOTIFY_PATH")
    return Path(env_path) if env_path else paths.config_file(NOTIFY_FILE)


def generate_topic(prefix: str = "tldrack") -> str:
    """A topic name strong enough to be the only secret protecting it.

    `secrets`, not `random`: on hosted ntfy.sh the topic IS the password, so a
    predictable one hands a stranger both your alerts and the ability to spam
    your phone. `token_urlsafe` emits exactly ntfy's allowed alphabet
    (`[A-Za-z0-9_-]`), so no filtering is needed and none is done — filtering
    would silently shrink the keyspace.
    """
    return f"{prefix}-{secrets.token_urlsafe(24)}"


# ── configuration ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class NotifyConfig:
    """Resolved notification settings. `enabled` already accounts for the topic.

    Precedence follows plugins.py: the environment wins over the file. The one
    subtlety is `enabled`, which is tri-state on disk — absent means "the user
    configured a topic and never said otherwise", which resolves to ON. Setting
    a topic IS the opt-in; the explicit flag exists so the macOS toggle can turn
    it back off without destroying the topic the user pasted in.
    """

    topic: str | None
    server: str = DEFAULT_NTFY_SERVER
    click: str | None = None
    explicit_enabled: bool | None = None
    digest_hours: tuple[int, ...] = DEFAULT_DIGEST_HOURS

    @property
    def configured(self) -> bool:
        return valid_topic(self.topic)

    @property
    def enabled(self) -> bool:
        return self.configured and self.explicit_enabled is not False


def parse_notify_file(text: str) -> NotifyConfig:
    """Parse notify.json. Raises ValueError on a corrupt envelope.

    Tolerant about individual fields in the same spirit as `parse_icon_file`: a
    malformed `click` costs you the click-through, not the whole feature. The
    topic is the exception — a malformed one is dropped entirely rather than
    passed through, because a wrong topic publishes your alerts to a stranger.
    """
    payload = json.loads(text)  # ValueError on non-JSON
    if not isinstance(payload, dict):
        raise ValueError('expected {"version": ..., "topic": ...}')

    topic = payload.get("topic")
    server = payload.get("server")
    click = payload.get("click")
    enabled = payload.get("enabled")

    return NotifyConfig(
        topic=topic if valid_topic(topic) else None,
        server=server if isinstance(server, str) and server.startswith("https://") else DEFAULT_NTFY_SERVER,
        click=click if isinstance(click, str) and click.startswith("https://") else None,
        explicit_enabled=enabled if isinstance(enabled, bool) else None,
        digest_hours=parse_digest_hours(payload.get("digest_hours", DEFAULT_DIGEST_HOURS)),
    )


def parse_digest_hours(raw: object) -> tuple[int, ...]:
    """Sorted distinct local hours 0–23. A malformed value falls back to the default rather than
    silencing the digest: an empty list is how the digest is turned off, on purpose."""
    if not isinstance(raw, (list, tuple)):
        return DEFAULT_DIGEST_HOURS
    hours = [hour for hour in raw if type(hour) is int and 0 <= hour <= 23]
    if len(hours) != len(raw):
        return DEFAULT_DIGEST_HOURS
    return tuple(sorted(set(hours)))


def serialize_notify_file(config: NotifyConfig) -> str:
    """Stable, indented file body — this is a file the user hand-edits."""
    document = {
        "version": NOTIFY_FILE_VERSION,
        "enabled": config.enabled,
        "topic": config.topic,
        "server": config.server,
        "click": config.click,
        "digest_hours": list(config.digest_hours),
    }
    return json.dumps(document, indent=2) + "\n"


class NotifyConfigStore:
    """The notify.json file, loaded tolerantly and written atomically.

    Same shape as `SessionIconStore`, and for the same reasons — handler threads
    race, and a crash mid-write must never leave a half-file that the tolerant
    loader then discards along with the user's topic.
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._config = NotifyConfig(topic=None)

        try:
            text = path.read_text()
        except FileNotFoundError:
            text = None
        except OSError as err:
            print(f"tldrack: could not read {path} ({err}) — phone notifications off", file=sys.stderr)
            text = None

        if text is not None:
            try:
                self._config = parse_notify_file(text)
            except ValueError as err:
                # Not re-persisted: an empty config must not clobber a file the
                # user might want to hand-repair (same rule as icons.py).
                print(f"tldrack: corrupt notify config {path} ({err}) — ignoring it", file=sys.stderr)

        # The environment wins over the file, exactly as it does for plugins.
        env_topic = os.environ.get("TLDRACK_NTFY_TOPIC")
        if env_topic:
            if valid_topic(env_topic):
                self._config = replace(self._config, topic=env_topic)
            else:
                print(
                    "tldrack: TLDRACK_NTFY_TOPIC is too short or has bad characters "
                    "— a guessable ntfy topic is readable by strangers, so it was ignored",
                    file=sys.stderr,
                )

    def get(self) -> NotifyConfig:
        with self._lock:
            return self._config

    def set_enabled(self, enabled: bool) -> NotifyConfig:
        with self._lock:
            self._config = replace(self._config, explicit_enabled=enabled)
            self._persist_locked()
            return self._config

    def set_topic(self, topic: str) -> NotifyConfig:
        """Write a new topic. CLI only — there is deliberately no HTTP route for
        this, so an allowlisted page cannot redirect where a user's alerts go."""
        if not valid_topic(topic):
            raise ValueError("topic must be 24-64 chars of [A-Za-z0-9_-] — a short one is guessable")
        with self._lock:
            self._config = replace(self._config, topic=topic)
            self._persist_locked()
            return self._config

    def _persist_locked(self) -> None:
        """Mechanism owned by atomic_store.py — see the note there."""
        atomic_write(self._path, serialize_notify_file(self._config), "change")


# ── the policy (pure) ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Notice:
    """One notification, already coalesced across every session it covers."""

    sessions: tuple[str, ...]
    title: str
    body: str
    priority: int
    tags: str
    #: Per session, parallel to `sessions`: `wait` (a prompt or question) or `stuck` (stuck.py).
    kinds: tuple[str, ...] = ()


@dataclass(frozen=True)
class Decision:
    """What the policy decided, and why.

    The reason is not decoration: "nothing was pending" and "something was
    pending and we refused to send it" are very different states to be in when
    a user reports that their phone stayed quiet, and only one of them is worth
    a log line.
    """

    notice: Notice | None
    reason: str

    @property
    def should_send(self) -> bool:
        return self.notice is not None


def format_duration(seconds: float) -> str:
    """Terse human duration for a notification body. `95.0` → `1m`."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    hours = seconds / 3600
    return f"{hours:.0f}h" if hours >= 2 else f"{int(seconds // 60)}m"


def blocked_sessions(
    sessions: Sequence[Mapping],
    grace_seconds: float = NOTIFY_GRACE_SECONDS,
) -> list[tuple[str, float]]:
    """`(session, blocked_seconds)` for every session past the grace window.

    `age_seconds` is the blocked duration rather than merely "time since we last
    heard from it": `StatusStore.set` restamps only when a report arrives, and a
    session sitting at a permission prompt emits nothing further. A row with
    `age_seconds: None` has never reported and is skipped — `unknown` is not
    `attention`.
    """
    found = []
    for row in sessions:
        if row.get("state") != "attention":
            continue
        age = row.get("age_seconds")
        session = row.get("session")
        if not isinstance(session, str) or not isinstance(age, (int, float)):
            continue
        if age >= grace_seconds:
            found.append((session, float(age)))
    return sorted(found, key=lambda item: item[0])


def stuck_sessions(sessions: Sequence[Mapping]) -> list[tuple[str, float]]:
    """`(session, seconds)` for every row stuck.py flagged. No grace: a flag already took four
    failures or twenty silent minutes to earn (stuck.py owns both cuts)."""
    found = []
    for row in sessions:
        stuck = row.get("stuck")
        session = row.get("session")
        if isinstance(stuck, Mapping) and isinstance(session, str):
            seconds = stuck.get("seconds")
            found.append((session, float(seconds) if isinstance(seconds, (int, float)) else 0.0))
    return sorted(found, key=lambda item: item[0])


def waiting_now(sessions: Sequence[Mapping]) -> set[str]:
    """Every session inside a wait episode right now — at a prompt (any age) or flagged stuck.
    What `NotifyDaemon` diffs tick to tick to know when an episode ENDED (property 6)."""
    return {
        row["session"]
        for row in sessions
        if isinstance(row.get("session"), str) and (row.get("state") == "attention" or isinstance(row.get("stuck"), Mapping))
    }


def shown_names(sessions: Sequence[Mapping]) -> dict[str, str]:
    """session → the name a message prints for it: the row's `label` when it carries one
    (§ WHAT MAY GO IN A MESSAGE). The ledger, the cooldown and the episode stay keyed by session."""
    return {
        row["session"]: row["label"].strip()
        for row in sessions
        if isinstance(row.get("session"), str) and isinstance(row.get("label"), str) and row["label"].strip()
    }


def pending_notification(
    *,
    sessions: Sequence[Mapping],
    presence_count: int,
    notified: Mapping[str, float],
    sent_today: int,
    now: float,
    grace_seconds: float = NOTIFY_GRACE_SECONDS,
    cooldown_seconds: float = NOTIFY_SESSION_COOLDOWN_SECONDS,
    daily_budget: int = NOTIFY_DAILY_BUDGET,
    pinged: Collection[str] = (),
) -> Decision:
    """Should we interrupt a human right now, and with what?

    Pure — no clock, no store, no socket — so pytest covers the entire policy
    directly, the same way `hook_state` and `activity.py` are covered. Every
    input that could vary is a parameter, including `now`. `pinged` is the set of
    sessions already pinged in their CURRENT wait episode (property 6).

    Order matters for the reason code, not the outcome: presence is checked
    before the budget so a quiet day at the desk does not report itself as
    "budget exhausted".
    """
    kinds: dict[str, str] = {session: "wait" for session, _ in blocked_sessions(sessions, grace_seconds)}
    ages: dict[str, float] = {session: age for session, age in blocked_sessions(sessions, grace_seconds)}
    for session, seconds in stuck_sessions(sessions):
        kinds.setdefault(session, "stuck")
        ages.setdefault(session, seconds)
    if not kinds:
        return Decision(None, "nothing-blocked")

    # 2. Presence suppression. Checked before anything else that could consume a
    # resource, because "the human is right there" is the cheapest and most
    # decisive answer available.
    if presence_count > 0:
        return Decision(None, "presence")

    fresh = [session for session in sorted(kinds) if now - notified.get(session, float("-inf")) >= cooldown_seconds]
    if not fresh:
        return Decision(None, "cooldown")
    fresh = [session for session in fresh if session not in pinged]
    if not fresh:
        return Decision(None, "episode")

    if sent_today >= daily_budget:
        return Decision(None, "budget")

    names = tuple(fresh)
    longest = max(ages[session] for session in names)
    duration = format_duration(longest)
    stuck = [session for session in names if kinds[session] == "stuck"]
    shown = shown_names(sessions)

    if len(names) == 1:
        if stuck:
            title = f"{shown.get(names[0], names[0])} looks stuck"
            body = "Same failure again, or no progress. Nothing on screen."
        else:
            title = f"{shown.get(names[0], names[0])} needs you"
            body = f"Blocked {duration}. Nothing on screen."
    else:
        title = f"{len(names)} agents need you"
        listed = " · ".join(
            f"{shown.get(session, session)} (stuck)" if session in stuck else shown.get(session, session) for session in names
        )
        body = listed + f"\nBlocked up to {duration}. Nothing on screen."

    return Decision(
        Notice(
            sessions=names,
            title=title,
            body=body,
            priority=PRIORITY_HIGH,
            tags="bell",
            kinds=tuple(kinds[session] for session in names),
        ),
        "send",
    )


# ── the digest (property 7, pure) ────────────────────────────────────────────


def digest_slot(now_wall: float, hours: Sequence[int]) -> tuple[int, int] | None:
    """The newest digest slot at or before `now_wall`: `(local day ordinal, hour)`, or None when
    the digest is off. A slot is due once, the first tick it is the newest — so a Mac asleep at
    15:00 sends that digest when it wakes, and never twice."""
    if not hours:
        return None
    moment = datetime.fromtimestamp(now_wall)
    today = moment.date().toordinal()
    earlier = [hour for hour in hours if hour <= moment.hour]
    return (today, max(earlier)) if earlier else (today - 1, max(hours))


def _names(names: Sequence[str]) -> str:
    shown = " · ".join(names[:DIGEST_NAMES_MAX])
    return shown + (f" +{len(names) - DIGEST_NAMES_MAX}" if len(names) > DIGEST_NAMES_MAX else "")


def digest_notice(*, open_work: Sequence[str], done: Sequence[str], parked: int) -> Notice | None:
    """One low-priority push for the optional reviews, or None when there is nothing to review.
    Names and counts only (§ WHAT MAY GO IN A MESSAGE): never a closing message."""
    open_work, done = sorted(set(open_work)), sorted(set(done))
    items = len(open_work) + len(done) + parked
    if not items:
        return None
    lines = []
    if open_work:
        lines.append(f"Open work: {_names(open_work)}")
    if done:
        lines.append(f"Done: {_names(done)}")
    if parked:
        lines.append(f"{parked} parked (seen, not now)")
    title = f"Digest · {items} to review"
    return Notice(
        sessions=tuple(open_work) + tuple(done), title=title, body="\n".join(lines), priority=PRIORITY_LOW, tags="inbox_tray"
    )


# ── bookkeeping ──────────────────────────────────────────────────────────────


def local_day(now: float | None = None) -> int:
    """Today as an ordinal, for the daily budget's rollover.

    Local rather than UTC: the budget is a comfort guarantee to a human in a
    timezone, and "20 a day" resetting at 7pm would be surprising.
    """
    stamp = time.time() if now is None else now
    return date.fromtimestamp(stamp).toordinal()


class NotifyLedger:
    """What we have already sent: per-session cooldowns and the daily count.

    In-memory only, deliberately. This mirrors `StatusStore` rather than
    `SessionIconStore`: a cooldown restored from disk after a restart would
    suppress a genuinely new alert, and the daily budget is a runaway guard
    whose worst case on restart is that it forgives a few sends. Neither is
    worth a file.
    """

    def __init__(self, daily_budget: int = NOTIFY_DAILY_BUDGET) -> None:
        self._lock = threading.Lock()
        self._budget = daily_budget
        self._notified: dict[str, float] = {}
        self._sent_today = 0
        self._day = local_day()

    def snapshot(self, day: int | None = None) -> tuple[dict[str, float], int]:
        """`(notified, sent_today)` for the policy, rolling the day over first."""
        with self._lock:
            self._roll_locked(local_day() if day is None else day)
            return dict(self._notified), self._sent_today

    def record(self, sessions: Sequence[str], now: float, day: int | None = None) -> None:
        """Mark a notice as sent: one budget unit, a cooldown stamp per session."""
        with self._lock:
            self._roll_locked(local_day() if day is None else day)
            self._sent_today += 1
            for session in sessions:
                self._notified[session] = now

    @property
    def daily_budget(self) -> int:
        return self._budget

    def stats(self, day: int | None = None) -> dict:
        with self._lock:
            self._roll_locked(local_day() if day is None else day)
            return {"sent_today": self._sent_today, "daily_budget": self._budget}

    def forget(self, session: str) -> None:
        """Drop a session's cooldown — used when it is renamed or killed, so a
        recreated name is not silently muted by its predecessor's stamp."""
        with self._lock:
            self._notified.pop(session, None)

    def _roll_locked(self, day: int) -> None:
        if day != self._day:
            self._day = day
            self._sent_today = 0


# ── the daemon ───────────────────────────────────────────────────────────────


class NotifyDaemon:
    """Ticks the policy and hands anything it produces to the configured sink.

    Readers are injected as callables rather than the stores themselves so the
    whole thing is testable without a bridge: `tick()` takes `now`, does one
    round of policy, and returns the `Decision` it acted on.
    """

    def __init__(
        self,
        *,
        config_store: NotifyConfigStore,
        read_sessions: Callable[[], Sequence[Mapping]],
        read_presence_count: Callable[[], int],
        ledger: NotifyLedger | None = None,
        sink_factory: Callable[[NotifyConfig], object] | None = None,
        interval: float = NOTIFY_TICK_SECONDS,
        on_push: Callable[[str, tuple[str, ...], str, bool], None] | None = None,
        read_digest: Callable[[float], Mapping] | None = None,
        on_signal: Callable[[str, str | None, str | None, int], None] | None = None,
        wall: Callable[[], float] = time.time,
    ) -> None:
        self._config_store = config_store
        # `since (wall s) → {open_work, done, parked}` for the digest (property 7); None = no digest.
        self._read_digest = read_digest
        # `(signal, session, reason, count)` into the rack metrics (rack_metrics.py § Attention
        # signals). Never raises into a send.
        self._on_signal = on_signal
        self._wall = wall
        # Sessions pinged in their CURRENT wait episode, and those whose held ping was reported
        # (property 6). Both shrink to the sessions still waiting on every tick.
        self._pinged: set[str] = set()
        self._held_reported: set[str] = set()
        self._digest_slot: tuple[int, int] | None = None
        self._digest_since = wall()
        # (kind, sessions, title, sent) after every publish — the session ledger's record of each
        # time the operator's phone was pinged (activity_log.py). Never raises into a send.
        self._on_push = on_push
        self._read_sessions = read_sessions
        self._read_presence_count = read_presence_count
        self._ledger = ledger or NotifyLedger()
        self._sink_factory = sink_factory or _default_sink_factory
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle --

    def start(self) -> bool:
        """Start ticking. False (and no thread) when nothing is configured.

        The thread is started even when `enabled` is currently False, as long as
        a topic exists — the macOS toggle flips `enabled` at runtime and must not
        require a bridge restart to take effect.
        """
        if not self._config_store.get().configured:
            return False
        if self._thread is not None:
            return True
        self._thread = threading.Thread(target=self._run, name="notify", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.tick()
            except Exception as err:  # noqa: BLE001 - a notifier may never kill its thread
                print(f"tldrack: notify tick failed ({err}) — continuing", file=sys.stderr)

    # -- one round --

    def tick(self, now: float | None = None) -> Decision:
        """One policy round. Sends at most one notification — a wait before a digest. Never raises."""
        config = self._config_store.get()
        if not config.enabled:
            return Decision(None, "disabled")

        stamp = time.monotonic() if now is None else now
        if self._digest_slot is None:
            # Armed on the first tick whatever else it does: a (re)start counts from now, so the
            # NEXT slot is the first digest, and a wait sent this tick cannot delay the arming.
            self._digest_slot = digest_slot(self._wall(), config.digest_hours)
        rows = self._read_sessions()
        presence = self._read_presence_count()
        # An episode that ended forgets its ping (property 6): the next wait is news again.
        waiting = waiting_now(rows)
        self._pinged &= waiting
        self._held_reported &= waiting
        notified, sent_today = self._ledger.snapshot()
        decision = pending_notification(
            sessions=rows,
            presence_count=presence,
            notified=notified,
            sent_today=sent_today,
            now=stamp,
            # From the ledger, not the module default: a caller that constructed
            # its own ledger with a different ceiling would otherwise be silently
            # policed against 20 anyway.
            daily_budget=self._ledger.daily_budget,
            pinged=self._pinged,
        )
        if decision.notice is None:
            if decision.reason in ("presence", "cooldown", "budget"):
                self._report_held(rows, decision.reason)
            if decision.reason == "budget":
                print(
                    f"tldrack: daily notification budget ({self._ledger.daily_budget}) reached — dropping",
                    file=sys.stderr,
                )
            digest = self._tick_digest(config, presence, stamp)
            return digest if digest is not None else decision

        sink = self._sink_factory(config)
        if sink is None:
            return Decision(None, "no-sink")

        # The cooldown is stamped BEFORE the send, and whether or not the
        # transport accepts it. A failing sink must not turn into a retry storm
        # against the same session — the next episode will notify normally.
        self._ledger.record(decision.notice.sessions, stamp)
        try:
            sent = sink.publish(
                title=decision.notice.title,
                body=decision.notice.body,
                priority=decision.notice.priority,
                click=config.click,
                tags=decision.notice.tags,
            )
        except Exception as err:  # noqa: BLE001 - a sink may never break its caller
            # The contract in sinks/__init__.py says publish() returns False
            # rather than raising, so reaching here means a sink is buggy. Say
            # so plainly instead of letting it surface as a mystery tick failure.
            print(f"tldrack: {getattr(sink, 'name', 'sink')} raised ({err}) — notification dropped", file=sys.stderr)
            return Decision(None, "sink-failed")
        finally:
            # Spent whether or not the transport took it, like the cooldown: one try per episode.
            self._pinged.update(decision.notice.sessions)
        for session, kind in zip(decision.notice.sessions, decision.notice.kinds):
            self._signal("ping_sent", session, kind)
        self._pushed("agents", decision.notice.sessions, decision.notice.title, sent)
        return decision

    def _report_held(self, rows: Sequence[Mapping], reason: str) -> None:
        """A ping the policy held back, once per episode per session (the telemetry's `ping_held`)."""
        candidates = {session for session, _ in blocked_sessions(rows)} | {session for session, _ in stuck_sessions(rows)}
        for session in sorted(candidates - self._pinged - self._held_reported):
            self._held_reported.add(session)
            self._signal("ping_held", session, reason)

    def _tick_digest(self, config: NotifyConfig, presence: int, stamp: float) -> Decision | None:
        """Property 7: at most one digest per slot. None when no slot came due this tick."""
        wall = self._wall()
        slot = digest_slot(wall, config.digest_hours)
        if slot is None or self._read_digest is None or slot == self._digest_slot:
            return None
        if self._digest_slot is None:  # the digest was off at the first tick and just came on
            self._digest_slot = slot
            return None
        self._digest_slot = slot
        since, self._digest_since = self._digest_since, wall
        if presence > 0:
            return Decision(None, "digest-presence")
        try:
            items = self._read_digest(since)
            open_work, done = set(items.get("open_work") or ()), set(items.get("done") or ())
            parked = int(items.get("parked") or 0)
            notice = digest_notice(open_work=sorted(open_work), done=sorted(done), parked=parked)
        except Exception as err:  # noqa: BLE001 - a digest reader may never kill the tick
            print(f"tldrack: digest read failed ({err}) — skipping this slot", file=sys.stderr)
            return Decision(None, "digest-failed")
        if notice is None:
            return Decision(None, "digest-empty")
        _, sent_today = self._ledger.snapshot()
        if sent_today >= self._ledger.daily_budget:
            return Decision(None, "budget")
        sink = self._sink_factory(config)
        if sink is None:
            return Decision(None, "no-sink")
        self._ledger.record((), stamp)
        try:
            sent = sink.publish(
                title=notice.title, body=notice.body, priority=notice.priority, click=config.click, tags=notice.tags
            )
        except Exception as err:  # noqa: BLE001 - a sink may never break its caller
            print(f"tldrack: {getattr(sink, 'name', 'sink')} raised ({err}) — digest dropped", file=sys.stderr)
            return Decision(None, "sink-failed")
        self._signal("digest_sent", None, "digest", len(open_work) + len(done) + parked)
        self._pushed("digest", notice.sessions, notice.title, sent)
        return Decision(notice, "digest")

    def _signal(self, signal: str, session: str | None, reason: str | None, count: int = 1) -> None:
        if self._on_signal is None:
            return
        try:
            self._on_signal(signal, session, reason, count)
        except Exception:  # noqa: BLE001 - telemetry may never break a send
            pass

    def _pushed(self, kind: str, sessions: tuple[str, ...], title: str, sent: object) -> None:
        if self._on_push is None:
            return
        try:
            self._on_push(kind, tuple(sessions), title, bool(sent))
        except Exception:  # noqa: BLE001 - a record may never break a send
            pass

    # -- control surface (GET/POST /notify) --

    def send_test(self) -> bool:
        """Publish a test notification, ignoring policy. Powers the UI button.

        Deliberately bypasses presence, grace and cooldown — the user pressed a
        button and is owed an answer about whether the transport works. It still
        spends a budget unit, so the button cannot be used to bypass the ceiling.
        """
        config = self._config_store.get()
        sink = self._sink_factory(config) if config.configured else None
        if sink is None:
            return False
        self._ledger.record((), time.monotonic())
        sent = sink.publish(
            title="tldrack test",
            body="Phone notifications are working.",
            priority=PRIORITY_LOW,
            click=config.click,
            tags="white_check_mark",
        )
        self._signal("ping_sent", None, "test")
        self._pushed("test", (), "tldrack test", sent)
        return sent

    def send_event(self, title: str, body: str, tags: str | None = None, door: str | None = None) -> tuple[bool, str]:
        """Publish a one-off event a LOCAL TOOL reports finished (`POST /notify/send`:
        a batch caller's step, a long run). `(sent, reason)`; never raises. `door` names where a
        tap lands (`EVENT_DOORS`); an unknown name is refused before anything is spent.

        Not the agent policy above: an event happens once, so there is no grace,
        cooldown or coalescing to apply, and presence is NOT checked — the run being
        reported is a terminal process no rack surface shows. What still binds:
        `enabled`, the daily budget (a looping caller cannot spam the phone), and
        pointer-only text — the caller's strings are cut to `EVENT_TITLE_MAX` /
        `EVENT_BODY_MAX` and stripped of control characters here, not trusted to be.
        """
        if door is not None and door not in EVENT_DOORS:
            return False, "bad-door"
        config = self._config_store.get()
        if not config.configured:
            return False, "unconfigured"
        if not config.enabled:
            return False, "disabled"
        _, sent_today = self._ledger.snapshot()
        if sent_today >= self._ledger.daily_budget:
            return False, "budget"
        sink = self._sink_factory(config)
        if sink is None:
            return False, "no-sink"
        self._ledger.record((), time.monotonic())
        try:
            sent = sink.publish(
                title=_event_text(title, EVENT_TITLE_MAX),
                body=_event_text(body, EVENT_BODY_MAX),
                priority=PRIORITY_DEFAULT,
                click=EVENT_DOORS[door] if door is not None else config.click,
                tags=tags if tags and EVENT_TAGS_RE.fullmatch(tags) else "checkered_flag",
            )
        except Exception as err:  # noqa: BLE001 - a sink may never break its caller
            print(f"tldrack: {getattr(sink, 'name', 'sink')} raised ({err}) — event dropped", file=sys.stderr)
            return False, "sink-failed"
        self._signal("ping_sent", None, "event")
        self._pushed("event", (), _event_text(title, EVENT_TITLE_MAX), sent)
        return bool(sent), "sent" if sent else "transport"

    def state(self) -> dict:
        """Everything a client needs to render the toggle. No topic — that is a
        secret, and a UI has no use for it beyond confirming it is set."""
        config = self._config_store.get()
        return {
            "configured": config.configured,
            "enabled": config.enabled,
            "server": config.server,
            "running": self._thread is not None,
            "digest_hours": list(config.digest_hours),
            **self._ledger.stats(),
        }

    def set_enabled(self, enabled: bool) -> dict:
        self._config_store.set_enabled(enabled)
        if enabled:
            self.start()
        return self.state()

    def forget(self, session: str) -> None:
        self._ledger.forget(session)


def _default_sink_factory(config: NotifyConfig) -> object | None:
    if not config.configured or config.topic is None:
        return None
    try:
        return NtfySink(config.topic, config.server)
    except ValueError as err:
        print(f"tldrack: bad ntfy config ({err}) — notifications off", file=sys.stderr)
        return None
