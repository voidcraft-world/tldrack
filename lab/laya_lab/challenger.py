"""Armed challenger decisions and the next-night rollback guard. Private files stay local."""

from __future__ import annotations
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from laya_lab import clock, registry
from laya_lab.data import open_private
from laya_bridge import challenger as preflight, rows

HISTORY = "challenger-decisions.json"
RESTART_PENDING = "challenger-restart.json"


def pending_restart(base: Path, surface: str, name: str, event: dict | None = None) -> None:
    from laya_lab.data import atomic_write_text

    atomic_write_text(base / RESTART_PENDING, json.dumps({"surface": surface, "name": name, "event": event}) + "\n")


def finish_restart(base: Path) -> None:
    intent = json.loads((base / RESTART_PENDING).read_text())
    event = intent.get("event")
    if event and not any(r.get("at") == event["at"] and r.get("evidence") == event["evidence"] for r in history(base)):
        record(base, event)
    restart()
    (base / RESTART_PENDING).unlink(missing_ok=True)


def history(base: Path) -> list[dict]:
    path = base / HISTORY
    if not path.exists():
        return []
    value = json.loads(path.read_text())
    if not isinstance(value, list):
        raise ValueError("challenger decision history is corrupt")
    return value


def record(base: Path, event: dict) -> None:
    from laya_lab.data import atomic_write_text

    atomic_write_text(base / HISTORY, json.dumps([*history(base), {"at": rows.utc_stamp(), **event}], indent=2) + "\n")


def automatic(base: Path, surface: str) -> bool:
    calls = [r for r in history(base) if r.get("surface") == surface and r.get("action") in ("promote", "reject")]
    return (
        sum(r["action"] == "promote" and not r.get("automatic") for r in calls) >= 3
        and len(calls) >= 3
        and all(r.get("agrees") is True for r in calls[-3:])
    )


def act(base: Path, surface: str, action: str, token: str, *, auto: bool = False) -> dict:
    with open_private(base / "challenger.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        registry.read_registry(base)
        state = preflight.view(base, surface)
        if state is None or state["token"] != token:
            raise ValueError("challenger evidence changed; reload before arming again")
        if action not in ("promote", "reject") or (action == "promote" and not state["ready"]):
            raise ValueError("promotion needs gate PASS, 3 shadow days, 70% of 10 grades and an applied reserve cut")
        if auto and not automatic(base, surface):
            raise ValueError("the first three promotions and three agreeing calls require operator presses")
        previous = registry.alias_target(base, surface, "champion")
        event = {
            "at": rows.utc_stamp(),
            "surface": surface,
            "action": action,
            "name": state["name"],
            "previous": previous[0] if previous and action == "promote" else None,
            "automatic": auto,
            "agrees": (action == "promote") == state["requirements"]["gate"],
            "evidence": state["token"],
        }
        if action == "promote":
            pending_restart(base, surface, state["name"], event)
            name, previous = registry.promote_challenger(base, surface, "armed promotion" if not auto else "automatic promotion")
            out = {"name": name, "previous": previous, "pruned": []}
        else:
            name = registry.reject_challenger(base, surface, "armed rejection")
            out = {"name": name, "previous": None, "pruned": registry.prune_rejects(base, surface)}
        record(base, event)
        return out


def restart() -> None:
    port = int(os.environ.get("TLDRACK_STATUS_PORT", str(int(os.environ.get("TLDRACK_PORT", "7681")) + 1)))
    req = Request(
        f"http://127.0.0.1:{port}/plugin/laya/restart", data=b"{}", headers={"Content-Type": "application/json"}, method="POST"
    )
    with urlopen(req, timeout=15) as reply:
        if not json.load(reply).get("ok"):
            raise RuntimeError("Laya restart refused after alias change")


def nightly(name: str, base: Path, *, device: str, router=None, now: datetime | None = None) -> dict:
    from laya_lab import gate

    surface = gate.SUITES[name].surface
    pending = base / RESTART_PENDING
    if pending.exists():
        wanted = json.loads(pending.read_text())
        current = registry.alias_target(base, wanted["surface"], "champion")
        if current and current[0] == wanted["name"]:
            finish_restart(base)
        else:
            pending.unlink()
    events = history(base)
    promotion = next((r for r in reversed(events) if r.get("surface") == surface and r.get("action") == "promote"), None)
    checked = {r.get("promotion_at") for r in events if r.get("action") == "night-check"}
    current = registry.alias_target(base, surface, "champion")
    if promotion and promotion["at"] not in checked and current and current[0] == promotion["name"] and promotion.get("previous"):
        promoted = registry.instant(promotion["at"])
        today = clock.local(now or datetime.now(timezone.utc)).date()  # the night after: a local day, `clock` owns which
        if promoted is None or clock.local(promoted).date() >= today:
            return {"state": "skipped", "why": "rollback guard waits for the next local night"}
        report, _ = gate.judge(name, base, candidate=current[0], served=promotion["previous"], device=device, router=router)
        # Missing evidence never rolls back. An explicit measured regression does.
        if any(c.get("state") in ("unmeasured", "pending") for c in report.get("checks", {}).values()):
            return {"state": "skipped", "why": "rollback evidence is not yet measurable"}
        regression = any(c.get("state") == "fail" for c in report.get("checks", {}).values())
        if regression:
            pending_restart(base, surface, promotion["previous"])
            registry.rollback(base, surface, "next-night regression")
            finish_restart(base)
        record(base, {"surface": surface, "action": "night-check", "promotion_at": promotion["at"], "rollback": regression})
        return {"state": "ran", "rollback": regression, "gate": report["verdict"]}
    candidate = registry.alias_target(base, surface, "challenger")
    if candidate:
        from laya_bridge import lab_files

        report, path = gate.judge(name, base, candidate=candidate[0], device=device, router=router)
        payload = registry.read_registry(base)
        payload["versions"][candidate[0]]["tags"] = lab_files.gate_tags(
            "PASS" if report["promote"] else "FORCED",
            report["run_id"],
            path.name,
            {key: value["state"] for key, value in report["checks"].items()},
        )
        registry.write_registry(base, payload)
    state = preflight.view(base, surface)
    if state and state["ready"] and automatic(base, surface):
        act(base, surface, "promote", state["token"], auto=True)
        finish_restart(base)
        return {"state": "ran", "promoted": state["name"]}
    return {"state": "skipped", "why": "no next-night guard or ready automatic promotion"}


def cmd(args, base: Path) -> int:
    out = act(base, args.surface, args.action, args.token)
    print(json.dumps(out))
    return 0
