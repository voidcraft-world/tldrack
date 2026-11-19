"""AUTO launch: which account a new agent should run on. Pure.

The rack's LAUNCH row offers `AUTO` beside the named profiles. A client never
scores anything — it sends `profile: "auto"` to `POST /spawn` (or the worktree
plugin's `/create`), and the bridge resolves it HERE, at click time, from the
same two inputs `/snapshot` already serves:

  - each account's remaining usage (`profiles[i].usage`: `usage.py` for Claude,
    `codex_usage.py` for Codex) and when each window resets, scaled by its
    subscription size (`capacity` in profiles.json — a half-size plan is 0.5);
  - how many agents already run on it (live rows' `profile` + `state`).

    rate     = left × window length / time to reset     (per window)
    headroom = capacity × min(5h rate, week rate)
    score    = headroom / (1 + load)

RATE is what is left PACED against its reset: how fast the account may burn
for the rest of the window, as a multiple of an even pace over a fresh one. A
full window with its whole length ahead is 1.0. Budget unspent at the reset is
gone, so what resets soon is worth more than the same amount resetting late: 20%
of a week left with a day to go (1.4, held to 1.0 by a fresh 5-hour window)
out-ranks 50% with four days (0.875). The 5h rate bounds the week's, so an account a day from its weekly reset is still
held to what its 5-hour window allows. A reset nearer than `PACE_FLOOR_S` counts
as that far, so a window minutes from its reset reads generous, not infinite.

The score is the share of that rate a new agent would get if it split it
evenly with the agents already there. Highest wins; a tie goes to the account
whose week has the most to burn (the higher week rate), then to the earlier
profile in profiles.json.

LOAD is per live session on the profile: an ENGAGED one counts 1, any other
`IDLE_SESSION_LOAD`. Engaged is working (busy/attention/background), or
touched within `ENGAGED_WITHIN_S` — its last state change, or its tmux birth.
The birth half is what spreads a burst: two quick AUTO clicks would otherwise
both see an idle account and land on it together, because a just-spawned
agent has not reported a turn yet. The state-change half is the agent whose
turn just ended: you are reading it and about to answer, so it is in progress
in every sense that matters for the account.

A window absent from a reading counts as FULL with its whole length ahead
(rate 1.0): `usage.py` drops a window once its `resets_at` has passed, and Codex
on this plan reports only a weekly one. An account with no reading at all is
likewise taken at full — it is unused. A window with no `resets_at` is not paced.

SPENT accounts are skipped: under `SPENT_BELOW` of a window left, or a live
session on it whose turn a rate limit cut off (`stop_failure`) with the reset
still ahead — the statusline reading can lag the cut-off by a whole turn. When
EVERY candidate is spent the pick is the one whose blocking window resets
first, flagged `spent`, so AUTO still says where the wait is shortest. SPENT
reads the window at face value, never paced: 3% left an hour from the reset is
still a cap a new agent would hit mid-task.

OUT accounts are never picked while any other can launch: an account whose plan lapsed or
whose login died (`account_standing.py`, the profile's `standing`) has no reset to wait for,
whatever its last usage reading says. Only when EVERY candidate is out does AUTO name one,
flagged `spent`, with a reason that says so.
"""

from __future__ import annotations

import math

from dataclasses import dataclass
from collections.abc import Iterable, Mapping

#: Kinds AUTO may choose. A shell runs no agent and spends no account.
#: `antigravity` is left out: no reading of its quota reaches the bridge yet,
#: and an account with no usage would score as the freest one.
AGENT_KINDS = ("claude", "codex")

#: States in which a session is drawing on its account right now.
ACTIVE_STATES = frozenset({"busy", "attention", "background"})

#: A session touched (state change or tmux birth) this recently counts as fully
#: loaded even when idle — see the module docstring.
ENGAGED_WITHIN_S = 600.0

#: An idle live session older than that: open, but drawing nothing. Enough to
#: break a tie toward the account with fewer agents parked on it.
IDLE_SESSION_LOAD = 0.25

#: Under this fraction of a window left, a new agent would hit the cap mid-task.
SPENT_BELOW = 0.05

#: The usage windows a reading may carry, as `usage.py` names them, and their length.
WINDOW_SECONDS = {"five_hour": 5 * 3600.0, "seven_day": 7 * 86400.0}
WINDOWS = tuple(WINDOW_SECONDS)

#: A reset nearer than this paces as if it were this far (module docstring) —
#: about a new agent's first stretch of work.
PACE_FLOOR_S = 3600.0

#: The weekly reset goes in AUTO's reason once pacing has at least doubled what
#: the week is worth — once the reset is the reason for the pick. Only the week:
#: a 5-hour reset is always hours away and moves no pick on its own.
PACE_NOTE_FACTOR = 2.0


@dataclass(frozen=True)
class Candidate:
    """One profile's standing. `left` is the fraction of its tightest window
    still unspent (0–1), at face value; `rate` is the binding window's left
    paced against its reset; `headroom` is that times its capacity."""

    id: str
    label: str
    capacity: float
    left: float
    rate: float
    #: The weekly rate alone — the tie-break toward the week with most to burn.
    week_rate: float
    headroom: float
    load: float
    engaged: int
    score: float
    spent: bool
    #: When the window that makes it spent resets (epoch s), or None.
    resets_at: float | None
    #: When the week resets, where that reset is why it scores well
    #: (`PACE_NOTE_FACTOR`); else None.
    paced_reset: float | None = None
    #: The account cannot run a turn at all (module docstring § OUT).
    out: bool = False


@dataclass(frozen=True)
class LaunchPick:
    id: str | None
    label: str | None
    reason: str
    spent: bool
    ranking: tuple[Candidate, ...]

    def as_wire(self) -> dict:
        """The `/snapshot` `launch_pick` block. Its PRESENCE is the client's
        capability signal for `profile: "auto"`; `id` is None when nothing is
        launchable."""
        return {
            "id": self.id,
            "label": self.label,
            "reason": self.reason,
            "spent": self.spent,
            "ranking": [
                {
                    "id": c.id,
                    "score": round(c.score, 4),
                    "left": round(c.left, 4),
                    "rate": round(c.rate, 4),
                    "headroom": round(c.headroom, 4),
                    "load": c.load,
                    "spent": c.spent,
                    "out": c.out,
                }
                for c in self.ranking
            ],
        }


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def window_left(usage: object, window: str) -> tuple[float, float | None]:
    """`(fraction left 0–1, resets_at)` for one window of a usage reading. An
    absent window or reading is a full one (module docstring)."""
    reading = usage.get(window) if isinstance(usage, Mapping) else None
    used = _number(reading.get("used_percentage")) if isinstance(reading, Mapping) else None
    if used is None:
        return 1.0, None
    resets_at = _number(reading.get("resets_at"))
    return max(0.0, min(1.0, 1.0 - used / 100.0)), resets_at


def window_rate(left: float, resets_at: float | None, window: str, now: float) -> float:
    """`left` paced against its reset — the module docstring's RATE."""
    if resets_at is None:
        return left
    length = WINDOW_SECONDS[window]
    remaining = resets_at - now
    if remaining <= 0:
        return 1.0  # past its reset: a fresh window, whatever the stale reading said
    return left * length / min(length, max(PACE_FLOOR_S, remaining))


def _cut_off_until(rows: Iterable[Mapping], now: float) -> float | None:
    """The latest future reset among these rows' rate-limit cut-offs, or None.
    A cut-off with no reset time is a limit of unknown length: spent for now."""
    until: float | None = None
    for row in rows:
        failure = row.get("stop_failure")
        if not isinstance(failure, Mapping) or not failure.get("limit"):
            continue  # an overload or a lapsed login is not the account's budget
        resets_at = _number(failure.get("resets_at"))
        if resets_at is None:
            until = math.inf
        elif resets_at > now:
            until = resets_at if until is None else max(until, resets_at)
    return until


def is_engaged(row: Mapping, now: float) -> bool:
    """Is this live session drawing on its account, or about to?"""
    if row.get("state") in ACTIVE_STATES:
        return True
    age = _number(row.get("age_seconds"))
    if age is not None and age < ENGAGED_WITHIN_S:
        return True
    created = _number(row.get("created_at"))
    return created is not None and now - created < ENGAGED_WITHIN_S


def session_load(rows: Iterable[Mapping], now: float) -> tuple[float, int]:
    """`(load, engaged count)` over one profile's LIVE rows."""
    load = 0.0
    engaged = 0
    for row in rows:
        if is_engaged(row, now):
            load += 1.0
            engaged += 1
        else:
            load += IDLE_SESSION_LOAD
    return load, engaged


def score_profile(summary: Mapping, rows: list[Mapping], capacity: float, now: float) -> Candidate:
    """One `/snapshot` profile entry + its live rows → its standing."""
    usage = summary.get("usage")
    readings = {window: window_left(usage, window) for window in WINDOWS}
    left, binding_reset = min(readings.values(), key=lambda reading: reading[0])
    rates = {window: window_rate(*readings[window], window, now) for window in WINDOWS}
    rate = min(rates.values())
    week_left, week_reset = readings["seven_day"]
    paced = week_left > 0 and rates["seven_day"] >= PACE_NOTE_FACTOR * week_left
    load, engaged = session_load(rows, now)
    cut_off = _cut_off_until(rows, now)
    standing = summary.get("standing")
    out = isinstance(standing, Mapping) and standing.get("state") == "out"
    spent = out or left < SPENT_BELOW or cut_off is not None
    resets_at = binding_reset if left < SPENT_BELOW else None
    if cut_off is not None:
        resets_at = cut_off if resets_at is None else max(resets_at, cut_off)
    headroom = capacity * rate
    return Candidate(
        id=str(summary["id"]),
        label=str(summary.get("label") or summary["id"]),
        capacity=capacity,
        left=left,
        rate=rate,
        week_rate=rates["seven_day"],
        headroom=headroom,
        load=load,
        engaged=engaged,
        score=headroom / (1.0 + load),
        spent=spent,
        resets_at=resets_at,
        paced_reset=week_reset if paced else None,
        out=out,
    )


def _until(seconds: float) -> str:
    """`40m`, `23h`, `3d` — the reason line's reset distance."""
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))}m"
    if seconds < 48 * 3600:
        return f"{round(seconds / 3600)}h"
    return f"{round(seconds / 86400)}d"


def reason_for(pick: Candidate, now: float) -> str:
    """The short mono line the clients print as AUTO's tooltip."""
    if pick.out:
        return f"{pick.label} · every account is out"
    if pick.spent:
        return f"{pick.label} · all spent · resets first"
    busy = f"{pick.engaged} busy" if pick.engaged else "idle"
    resets = f" · resets in {_until(pick.paced_reset - now)}" if pick.paced_reset is not None else ""
    return f"{pick.label} · {round(pick.left * 100)}% left{resets} · {busy}"


def pick_launch_profile(
    summaries: Iterable[Mapping], rows: Iterable[Mapping], now: float, capacities: Mapping[str, float] | None = None
) -> LaunchPick:
    """`/snapshot`'s `profiles` list + its session rows → where AUTO launches.

    Only LIVE rows count (a dead row runs nothing); a row's `profile` is the
    snapshot's own answer (transcript hint over the tmux stamp)."""
    by_profile: dict[str, list[Mapping]] = {}
    for row in rows:
        profile = row.get("profile")
        if row.get("live") and isinstance(profile, str):
            by_profile.setdefault(profile, []).append(row)
    candidates = [
        score_profile(summary, by_profile.get(str(summary["id"]), []), (capacities or {}).get(str(summary["id"]), 1.0), now)
        for summary in summaries
        if summary.get("kind") in AGENT_KINDS and summary.get("cli_found")
    ]
    # Stable sort: equal scores go to the week with most to burn, then keep
    # profiles.json order.
    ranking = tuple(sorted(candidates, key=lambda c: (-c.score, -c.week_rate)))
    open_ = [c for c in ranking if not c.spent]
    if open_:
        best = open_[0]
    elif ranking:
        # An out account has no reset to wait for: last, whatever its stale windows say.
        best = min(ranking, key=lambda c: (c.out, math.inf if c.resets_at is None else c.resets_at))
    else:
        return LaunchPick(None, None, "no agent CLI found", False, ())
    return LaunchPick(best.id, best.label, reason_for(best, now), best.spent, ranking)
