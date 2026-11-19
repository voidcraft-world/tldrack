"""routing.json — which profile a piece of agent work runs on (ladder rung 7, the worker-dispatch half).

`~/.config/tldrack/routing.json` (env `TLDRACK_ROUTING_PATH`) maps a task kind and a
lane to an ordered list of profiles and a quota floor, in the shape of firstmate's dispatch file (docs/INSPIRATIONS.md):

    {"ladder_step": {"claude": {"profiles": ["a", "b"], "min_quota_pct": 15},
                     "codex":  {"profiles": ["codex"], "min_quota_pct": 10}},
     "report":      {"any":    {"profiles": ["b", "a"]}}}

- The first listed profile whose 5-hour AND weekly windows both have at least `min_quota_pct` left
  wins, read off the usage the bridge already keeps per account (usage.py, codex_usage.py).
- **A worker's model is never downgraded quietly.** When no profile qualifies, `resolve` answers
  None with the reason; the caller waits and says why (an inbox row), it never picks a lesser one.
- No file, or no entry for the kind/lane: the Claude lane is every Claude profile, the Codex lane
  every Codex profile, `any` both, in `launch_pick` order with no floor — today's AUTO.
- Tolerant per entry, like profiles.json: a malformed entry is dropped with a log line, the rest kept.
- **No `model` key.** A route picks an ACCOUNT. Nothing reads a model from a route, so one here could
  only be ignored, and an ignored pin is exactly the quiet downgrade this file refuses: an entry
  carrying one is dropped with a log line. A profile may pin its own (`args`, `["--model", "sonnet"]`);
  the model a LEAD's workers run is the run's pair: owned by lead/models.py — see the note there.

Left for the rest of rung 7: coordinator `rank.py` and worktree `brief.py` reading their model from
here, which needs `PluginContext` (the public core) to carry it.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

from tldrack import paths
from tldrack.launch_pick import window_left

ROUTING_FILE = "routing.json"
KINDS = ("ladder_step", "report", "rank", "brief", "judge", "review")
#: Legacy (step 6.8): a kind's name in a routing.json written before 0.1.0, read as the new one for one release.
LEGACY_KINDS = {"scout": "report"}
LANES = ("claude", "codex", "any")
LANE_KINDS = {"claude": ("claude",), "codex": ("codex",), "any": ("claude", "codex")}
_PROFILE = re.compile(r"[a-z0-9][a-z0-9_-]{0,39}")


@dataclass(frozen=True)
class Route:
    profiles: tuple[str, ...]
    min_quota_pct: float = 0.0


def resolve_routing_path() -> Path:
    env = os.environ.get("TLDRACK_ROUTING_PATH")
    return Path(env) if env else paths.config_file(ROUTING_FILE)


def parse_route(raw: object) -> Route | None:
    if not isinstance(raw, Mapping):
        return None
    profiles = raw.get("profiles")
    if not isinstance(profiles, list) or not profiles or not all(isinstance(p, str) and _PROFILE.fullmatch(p) for p in profiles):
        return None
    floor = raw.get("min_quota_pct", 0)
    if isinstance(floor, bool) or not isinstance(floor, (int, float)) or not 0 <= floor <= 100:
        return None
    if "model" in raw:
        return None
    return Route(tuple(profiles), float(floor))


def parse_routing(text: str, log: Callable[[str], None] = lambda _m: None) -> dict[str, dict[str, Route]]:
    try:
        data = json.loads(text)
    except ValueError:
        log("routing.json: not JSON, using the defaults")
        return {}
    if not isinstance(data, Mapping):
        return {}
    table: dict[str, dict[str, Route]] = {}
    for kind, lanes in data.items():
        kind = LEGACY_KINDS.get(kind, kind)
        if kind not in KINDS or not isinstance(lanes, Mapping):
            log(f"routing.json: dropped unknown kind {kind!r}")
            continue
        for lane, raw in lanes.items():
            route = parse_route(raw) if lane in LANES else None
            if route is None:
                pin = isinstance(raw, Mapping) and "model" in raw
                log(
                    f"routing.json: dropped {kind}.{lane}"
                    + (" (a route takes no model: a profile's args pin one, lead/models.py sets a worker's)" if pin else "")
                )
                continue
            table.setdefault(kind, {})[lane] = route
    return table


def load_routing(path: Path | None = None, log: Callable[[str], None] = lambda _m: None) -> dict[str, dict[str, Route]]:
    target = path or resolve_routing_path()
    try:
        return parse_routing(target.read_text(encoding="utf-8"), log)
    except OSError:
        return {}


def quota_left(summary: Mapping) -> float:
    """The tighter of the two windows, in percent left (an unreported window is full)."""
    usage = summary.get("usage")
    return 100.0 * min(window_left(usage, "five_hour")[0], window_left(usage, "seven_day")[0])


def resolve(
    table: Mapping[str, Mapping[str, Route]], kind: str, lane: str, summaries: Iterable[Mapping], ranked: Iterable[str] = ()
) -> tuple[str | None, str]:
    """(profile id, why). The profile is None when nothing qualifies: the caller waits.

    `summaries` is `/snapshot`'s `profiles` list (id, kind, cli_found, usage); `ranked` is launch_pick's
    order, used when routing.json says nothing for this kind and lane."""
    by_id = {str(s.get("id")): s for s in summaries}
    route = (table.get(kind) or {}).get(lane)
    if route is None:
        kinds = LANE_KINDS.get(lane, ())
        order = [p for p in ranked if p in by_id] + [p for p in by_id if p not in set(ranked)]
        route = Route(tuple(p for p in order if by_id[p].get("kind") in kinds))
        source = "default"
    else:
        source = "routing.json"
    skipped: list[str] = []
    for profile in route.profiles:
        summary = by_id.get(profile)
        if summary is None or not summary.get("cli_found"):
            skipped.append(f"{profile} not installed")
            continue
        if lane != "any" and summary.get("kind") not in LANE_KINDS.get(lane, ()):
            skipped.append(f"{profile} is not a {lane} profile")
            continue
        left = quota_left(summary)
        if left < route.min_quota_pct:
            skipped.append(f"{profile} has {left:.0f}% left, under the {route.min_quota_pct:.0f}% floor")
            continue
        return profile, f"{source}: {profile}"
    why = "; ".join(skipped) or f"no {lane} profile is configured"
    return None, f"{source}: nothing qualifies ({why})"
