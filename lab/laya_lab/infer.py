"""Asking Laya, the way laya-serve asks it: one `Router`, one `predict` per state. torch loads lazily.

`golden`, `sweep` and `review-open-work` all predict through here, so a golden report, a held-out
sweep and the deck's scores are the same numbers for the same text. `open_router` registers the
fine-tunes in `models.json` exactly as the bridge serves them, plus any unregistered checkpoint a
command names (`--model <checkpoint folder>`, `golden --all-checkpoints`).

It also takes the machine's resource lease before a model loads, when one is configured (`hold_slot`): the gpu slot,
one `ROUTER_TARGET_GB` per target the command loads, so a lab run never lands beside another large model. A scheduled
command steps aside between predictions while live work or load wants the machine (`yield_point`). With no lease the
command just runs: owned by `laya_lab/lease.py` — see the note there.
"""

from __future__ import annotations

import atexit
import hashlib
import ipaddress
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from laya_lab import siblings  # noqa: F401  (puts laya_bridge on sys.path)
from laya_lab import lease as lease_seam
from laya_bridge import env_names
from laya_bridge.files import clip
from laya_bridge.paths import laya_local_model_id, registry_view

#: A refused lease exits 75 (sysexits' EX_TEMPFAIL): a busy machine, never a failed run.
LEASE_REFUSED_EXIT = 75
#: How long a SCHEDULED command (the lease's `LEASE_SCHEDULED_ENV` is "1", set by its scheduler) queues for its slot; by
#: hand it never waits.
SCHEDULED_WAIT_S = 600.0
#: A torch run on `--device cpu` claims this many cores of the cpu slot.
LAB_CPU_CORES = 4


WHY_MAX = 200


def why_line(error: BaseException) -> str:
    """A step's failure as one line, `$HOME` folded to `~`: a report never carries a home path."""
    text = str(error) or type(error).__name__
    return clip(text.replace(str(Path.home()), "~").splitlines()[0] if text else type(error).__name__, WHY_MAX)


def lease_module() -> Any:
    """The lease in force, or its no-lease answer: owned by `laya_lab/lease.py` — see the note there."""
    return lease_seam.module()


#: The leases this command holds, by slot (`hold_slot`): `yield_point` asks them between units of work.
HELD: dict[str, Any] = {}


def hold_slot(slot: str, kind: str, units: float, models: Sequence[str] = ()) -> Any:
    """Takes `units` of `slot` for this command until the process ends, or exits LEASE_REFUSED_EXIT saying who holds
    it. The holder is the command (`laya-lab golden`); a second take in the same command is re-entrant."""
    lease = lease_module()
    holder = "laya-lab " + (sys.argv[1] if len(sys.argv) > 1 else "")
    scheduled = env_names.read(os.environ, lease.LEASE_SCHEDULED_ENV) == "1"
    try:
        held = lease.take(slot, units, holder, kind=kind, models=list(models), wait_s=SCHEDULED_WAIT_S if scheduled else 0.0)
    except lease.LeaseRefused as refused:
        print(f"[laya-lab] {refused} (exit {LEASE_REFUSED_EXIT}: try again once it is free)", file=sys.stderr)
        raise SystemExit(LEASE_REFUSED_EXIT) from None
    if not held.reentered and HELD.get(slot) is not held:
        HELD[slot] = held
        atexit.register(held.release)
    return held


def yield_point(sleep: Callable[[float], None] | None = None) -> None:
    """Between units of a SCHEDULED command's work: wait while live work (a rack verdict, the next-step chip) or load
    wants the machine back (the held slot's `should_yield`), then carry on. Never aborts; a hand-started command never waits here."""
    for held in list(HELD.values()):
        told = False
        while (reason := held.should_yield()) is not None:
            if not told:
                print(f"[laya-lab] paused: {reason[1]}", file=sys.stderr, flush=True)
                told = True
            (sleep or time.sleep)(lease_module().WAIT_POLL_S)


def hold_device(device: str, *, targets: int = 1, gb: float | None = None, models: Sequence[str] = ()) -> Any:
    """The slot a torch command on `device` holds: the gpu slot (`gb`, else `targets` router targets) on mps or
    auto, the cpu slot on cpu."""
    if device == "cpu":
        return hold_slot("cpu", "cpu", LAB_CPU_CORES, models)
    return hold_slot("gpu", "mps", gb if gb is not None else lease_module().router_gb(targets), models)


#: Per decision: a small local decoder can take seconds on a long state.
ENDPOINT_TIMEOUT_SECONDS = 120.0
LOOPBACK_NAMES = ("localhost",)


def is_loopback_url(url: str) -> bool:
    """`http(s)://` on this machine only. A golden or held-out text is the operator's own closing message,
    so an endpoint voter never sends one off the machine."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    if parts.scheme not in ("http", "https") or not host:
        return False
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class EndpointRouter:
    """Any `/v1/systemone` server (another local model, a future fine-tune), shaped like `laya.Router`
    so `predict_noul` — and every command built on it — scores it unchanged. Loopback only.

    `predict`'s `model` is the caller's NAME for this voter and is not forwarded; `model` here is what
    the server is asked for, when it serves more than one."""

    def __init__(self, url: str, *, model: str | None = None, timeout: float = ENDPOINT_TIMEOUT_SECONDS) -> None:
        if not is_loopback_url(url):
            raise ValueError(f"{url} is not a loopback URL: an endpoint voter never sends a text off this machine")
        base = url.rstrip("/")
        self.url = base if base.endswith("/v1/systemone") else f"{base}/v1/systemone"
        self.model = model
        self.timeout = timeout
        self.loaded_revisions: dict[str, str | None] = {}

    def predict(self, state: Any, questions: Mapping[str, Any], model: str | None = None) -> dict:
        body: dict[str, Any] = {"state": state, "questions": dict(questions)}
        if self.model:
            body["model"] = self.model
        request = urllib.request.Request(self.url, json.dumps(body).encode("utf-8"), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as reply:
                payload = json.loads(reply.read())
        except urllib.error.HTTPError as error:
            raise ValueError(f"{self.url} answered HTTP {error.code}") from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise ValueError(f"{self.url} is unreachable ({error}); is the server up?") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("answers"), dict):
            raise ValueError(f"{self.url} answered without an `answers` object")
        return payload


def open_router(base: Path, device: str, extra: Mapping[str, Path | None] | None = None) -> Any:
    """A Router over the registered fine-tunes and `extra`, with the lease held for the targets the command will load."""
    import laya

    hold_device(device, targets=len(extra or {}) or 1, models=list(extra or {}))
    models = {name: entry["path"] for name, entry in registry_view(base)["models"].items()}
    models.update({name: str(path) for name, path in (extra or {}).items() if path is not None})
    return laya.Router(models=models, device=device, preload=False)


def predict_noul(router: Any, model: str, states: Sequence[Any], question: Mapping[str, Any]) -> list[float]:
    """`p(yes)` per state, in order. A missing or non-probability answer stops the run (ValueError)."""
    out: list[float] = []
    for state in states:
        yield_point()
        answer = router.predict(state, {"q": dict(question)}, model=model)["answers"]["q"]
        p = answer.get("noul") if isinstance(answer, Mapping) else None
        if isinstance(p, bool) or not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 <= p <= 1:
            raise ValueError(f"{model} answered {p!r}, not a probability")
        out.append(float(p))
    return out


#: Beside a checkpoint's weights: their sha256, keyed by the weights' size and mtime, so a 1.6 GB file is hashed once.
DIGEST_SIDECAR = "laya-lab-digest.json"
WEIGHTS_FILE = "model.safetensors"
HASH_CHUNK = 8 * 1024 * 1024


def laya_version() -> str | None:
    from importlib import metadata

    try:
        return metadata.version("laya")
    except metadata.PackageNotFoundError:
        return None


def checkpoint_digest(name: str, path: Path | None) -> str | None:
    """THE identity a report, a score and a shadow row are keyed by (with the device and the case): what would make
    the same name answer differently is in it. A fine-tune is `sha256:<16 hex>` of its weights (cached beside them in
    DIGEST_SIDECAR, re-hashed when their size or mtime moves); a built-in is pinned by the laya release that serves it
    (`english.laya-<version>`). None when it cannot be read: such a row is never reused."""
    if path is None:
        version = laya_version()
        return f"{name}.laya-{version}" if version else None
    weights = Path(path) / WEIGHTS_FILE
    sidecar = Path(path) / DIGEST_SIDECAR
    try:
        stat = weights.stat()
    except OSError:
        return None
    stamp = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    try:
        held = json.loads(sidecar.read_text(encoding="utf-8"))
        if isinstance(held, dict) and held.get("stamp") == stamp and isinstance(held.get("sha256"), str):
            return f"sha256:{held['sha256'][:16]}"
    except (OSError, ValueError):
        pass
    digest = hashlib.sha256()
    try:
        with weights.open("rb") as handle:
            while chunk := handle.read(HASH_CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    from laya_lab.data import atomic_write_text

    try:
        atomic_write_text(sidecar, json.dumps({"stamp": stamp, "sha256": digest.hexdigest()}) + "\n")
    except OSError:
        pass  # a read-only folder still gets its digest, just not cached
    return f"sha256:{digest.hexdigest()[:16]}"


def checkpoint_stamp(base: Path, router: Any, name: str, path: Path | None) -> dict[str, Any]:
    """`{name, path, revision, model_id, registered, digest}` of what answered: `name` is the checkpoint
    FOLDER (None for a built-in), `path` relative to `base`. Read after that model has predicted,
    so `revision` is the Hub commit it actually loaded (None for a local folder). `digest`: `checkpoint_digest`."""
    revision = (getattr(router, "loaded_revisions", None) or {}).get(name)
    revision = revision if isinstance(revision, str) and revision else None
    relative = None
    if path is not None:
        try:
            relative = Path(path).resolve().relative_to(base.resolve()).as_posix()
        except ValueError:
            relative = None
    entry = registry_view(base)["models"].get(name)
    return {
        "name": Path(path).name if path is not None else None,
        "path": relative,
        "revision": revision,
        "model_id": laya_local_model_id(name, revision=revision, path=str(path) if path is not None else None),
        "registered": entry is not None and path is not None and entry["path"] == str(Path(path).resolve()),
        "digest": checkpoint_digest(name, path),
    }
