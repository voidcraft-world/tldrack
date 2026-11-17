"""Count-only promotion preflight shared by OPS and the lab writer. No model loads.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".
"""

from __future__ import annotations
import copy
import hashlib
import json
from pathlib import Path
from laya_bridge import files, lab_files, paths, review_data
from laya_bridge.review_surfaces import REVIEW_SURFACES

CHECKS = ("heldout", "warning_cost", "done_cost", "private_golden", "floor")


def read_report(base: Path, name: str | None) -> dict:
    if not name or not lab_files.is_basename(name):
        return {}
    try:
        raw = files.read_capped(base / paths.EVALS_DIRNAME / name, lab_files.REPORT_CAP_BYTES)
        value = files.loads(raw) if raw else {}
        return value if isinstance(value, dict) else {}
    except (files.SourceError, ValueError):
        return {}


def cached_digest(version: dict | None) -> str | None:
    if version is None:
        return None
    try:
        folder = Path(version["path"])
        stamp = (folder / "model.safetensors").stat()
        raw = files.loads(files.read_capped(folder / "laya-lab-digest.json", 4096) or b"{}")
        digest = raw.get("sha256")
        if (
            raw.get("stamp") != {"size": stamp.st_size, "mtime_ns": stamp.st_mtime_ns}
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            return None
        return "sha256:" + digest[:16]
    except (OSError, ValueError, files.SourceError):
        return None


def view(base: Path, surface: str) -> dict | None:
    from laya_bridge.ops import shadow_summary

    spec = REVIEW_SURFACES[surface]
    versions = paths.registry_versions(base)
    aliases = versions["aliases"].get(surface) or {}
    name = aliases.get("challenger")
    version = versions["versions"].get(name or "")
    if version is None:
        return None
    tags = version.get("tags") or {}
    report = read_report(base, tags.get("gate_report"))
    champion = versions["versions"].get(aliases.get("champion") or "")
    champion_cut = (champion or {}).get("cut") or {"at": spec.default_threshold, "done_at": spec.default_done_threshold}
    if champion_cut.get("done_at") is None:
        champion_cut = {**champion_cut, "done_at": spec.default_done_threshold}
    candidate_digest = cached_digest(version)
    champion_digest = cached_digest(champion)
    served = report.get("served") or {}
    bound = (
        report.get("evidence", {}).get("question_version") == spec.question_version
        and served.get("checkpoint") == (champion["name"] if champion else None)
        and served.get("name") == (spec.fine_tune if champion else "english")
        and served.get("at") == champion_cut["at"]
        and served.get("done_at") == champion_cut["done_at"]
        and candidate_digest is not None
        and report.get("candidate", {}).get("stamp", {}).get("digest") == candidate_digest
        and (champion is None or (champion_digest is not None and served.get("stamp", {}).get("digest") == champion_digest))
    )
    checks = report.get("checks") or {}
    gate_ok = (
        bound
        and tags.get("gate") == "PASS"
        and report.get("promote") is True
        and report.get("candidate", {}).get("checkpoint") == name
        and all(checks.get(c, {}).get("state") == "pass" for c in CHECKS)
    )
    shadow_rows, _ = lab_files.read_shadow(base, surface)
    current_shadow = [
        r
        for r in shadow_rows
        if r["question_version"] == spec.question_version
        and r["challenger"] == name
        and r.get("challenger_digest") == candidate_digest
    ]
    shadow = shadow_summary(current_shadow, name)
    shadow["days"] = len({r["scored_at"][:10] for r in current_shadow})
    sides = review_data.challenger_sides(review_data.snapshot(base, surface)) or {"graded": 0, "with_challenger": 0}
    cut = version.get("cut")
    swept = read_report(base, (cut or {}).get("report"))
    reserve = swept.get("reserve") or {}
    applied = swept.get("applied") or {}
    gate_ok = gate_ok and bool(
        cut
        and report.get("candidate", {}).get("at") == cut["at"]
        and report.get("candidate", {}).get("done_at") == cut["done_at"]
    )
    reserve_ok = bool(
        cut
        and cut.get("swept_at")
        and cut.get("done_at") is not None
        and swept.get("kind") == "heldout"
        and swept.get("suite") == spec.golden_suite
        and swept.get("checkpoint", {}).get("name") == name
        and swept.get("checkpoint", {}).get("digest") == candidate_digest
        and swept.get("labels") == "operator"
        and swept.get("question_version") == spec.question_version
        and isinstance(applied, dict)
        and applied.get("at") == cut["at"]
        and applied.get("done_at") == cut["done_at"]
        and swept.get("reserve_only") is True
        and reserve.get("rows", 0) >= 40
        and min(reserve.get("open", 0), reserve.get("done", 0)) >= 10
    )
    requirements = {
        "gate": gate_ok,
        "shadow": shadow["days"] >= 3,
        "operator": sides["graded"] >= 10 and sides["with_challenger"] / max(1, sides["graded"]) >= 0.7,
        "reserve": reserve_ok,
    }
    projected = copy.deepcopy(versions)
    projected["aliases"][surface]["challenger"] = None
    prune = lab_files.retention(base, surface, projected)["prunable"]
    numbers = [
        {"name": c, "state": checks.get(c, {}).get("state", "unmeasured"), "p": checks.get(c, {}).get("p")} for c in CHECKS
    ]
    prospective = report.get("prospective") or {}
    numbers.append({"name": "prospective", "state": prospective.get("state", "pending"), "p": prospective.get("p")})
    out = {
        "name": name,
        "ready": all(requirements.values()),
        "requirements": requirements,
        "checks": numbers,
        "shadow_days": shadow["days"],
        "shadow_agreement": shadow["agree"],
        "graded": sides["graded"],
        "with_challenger": sides["with_challenger"],
        "cut": cut["at"] if cut else None,
        "done_cut": cut["done_at"] if cut else None,
        "reserve_rows": reserve.get("rows", 0),
        "prune_paths": [p for item in prune for p in (item["path"], item["path"] + ".data")],
        "prune_bytes": sum(item["bytes"] + lab_files.folder_bytes(Path(item["path"] + ".data")) for item in prune),
    }
    identity = {"view": out, "gate": report, "reserve": swept, "champion": champion, "candidate_digest": candidate_digest}
    out["token"] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    return out
