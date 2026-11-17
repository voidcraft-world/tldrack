"""Install Laya: the runtime venv, then the base checkpoint, so the first decision is not a download.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

TWO STEPS, ONE ORDER, TWO DOORS.
1. `sync_argv`: `uv sync` for `plugins/laya/runtime` (torch and `laya[serve]`, pinned by its lock).
2. `fetch_argv`: the venv's own python downloads the base checkpoint (`BASE_REPO`, about 800 MB) into the
   Hugging Face cache, at the revision laya-serve will ask for. Nothing is trained and nothing of the operator's
   is read: a first run serves the public weights.

`POST /install` runs both in the background (`runtime.py`); `main` runs both in the foreground with their output,
then asks a running bridge to load the model and prints what it serves. `main` is `tldrack laya install`: the
bridge's CLI hands the words after `laya` to `plugins/laya/cli.py`.

The download is a head start, never a requirement: laya-serve fetches whatever is missing when it starts, so a
failed or skipped fetch costs the first start its wait and nothing else (`run` reports it, and goes on).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import IO, Any

from laya_bridge.paths import RUNTIME_DIR

#: The public checkpoint a first run serves (laya-serve's `english`), and what every fine-tune here starts from.
BASE_REPO = "convaiinnovations/laya"
#: laya-serve is started with this revision word (`runtime.py`): the commit the pinned `laya` release reviewed.
REVISION = "reviewed"
#: What laya's own loader takes from the repository; the rest of it is sibling checkpoints nobody loads here.
FETCH_PATTERNS = ("rl_agent_config.json", "model.safetensors", "tokenizer/*", "encoder/*")
SYNC_TIMEOUT_S = 30 * 60
FETCH_TIMEOUT_S = 30 * 60
DEFAULT_STATUS_PORT = 7682
STATUS_PORT_ENV = "TLDRACK_STATUS_PORT"
HTTP_TIMEOUT_S = 5.0
UV_CANDIDATES = ("~/.local/bin/uv", "/opt/homebrew/bin/uv", "/usr/local/bin/uv")

#: Run by the runtime venv's python. `resolve_revision` is laya's own rule, so the commit fetched here is the one
#: laya-serve resolves at start.
FETCH_SCRIPT = (
    "import sys\n"
    "from huggingface_hub import snapshot_download\n"
    "from laya.revisions import resolve_revision\n"
    "repo, revision, patterns = sys.argv[1], sys.argv[2], sys.argv[3:]\n"
    "print(snapshot_download(repo, revision=resolve_revision(repo, revision), allow_patterns=patterns))\n"
)


def find_uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    for candidate in UV_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    return None


def venv_python(runtime_dir: Path = RUNTIME_DIR) -> Path:
    return runtime_dir / ".venv" / "bin" / "python"


def sync_argv(uv: str, runtime_dir: Path = RUNTIME_DIR) -> list[str]:
    """`uv sync`, `--locked` while `runtime/uv.lock` exists: a drifted lock is refused, never quietly rewritten."""
    argv = [uv, "sync", "--project", str(runtime_dir)]
    if (runtime_dir / "uv.lock").is_file():
        argv.append("--locked")
    return argv


def fetch_argv(runtime_dir: Path = RUNTIME_DIR) -> list[str]:
    return [str(venv_python(runtime_dir)), "-c", FETCH_SCRIPT, BASE_REPO, REVISION, *FETCH_PATTERNS]


def _call(argv: Sequence[str], out: IO[Any] | int | None, timeout: float) -> int:
    try:
        return subprocess.call(list(argv), stdout=out, stderr=subprocess.STDOUT, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124
    except OSError:
        return 127


def run(
    uv: str,
    runtime_dir: Path = RUNTIME_DIR,
    out: IO[Any] | int | None = None,
    say: Callable[[str], None] = lambda _line: None,
    fetch: bool = True,
) -> tuple[int, str]:
    """Both steps, in order. `(exit code, the step it ended on)`: `sync` failing is the install failing; `fetch`
    failing is reported and still 0, because the module note's last paragraph holds."""
    say(f"[laya] installing the runtime: {' '.join(sync_argv(uv, runtime_dir))}")
    code = _call(sync_argv(uv, runtime_dir), out, SYNC_TIMEOUT_S)
    if code != 0:
        return code, "sync"
    if fetch:
        say(f"[laya] downloading the base model {BASE_REPO} (about 800 MB the first time)")
        if _call(fetch_argv(runtime_dir), out, FETCH_TIMEOUT_S) != 0:
            say("[laya] the base model was not downloaded now: the first start will fetch it")
    return 0, "fetch"


# ── the CLI ───────────────────────────────────────────────────────────────────


def _bridge(method: str, port: int, subpath: str) -> dict | None:
    """One loopback call to this plugin on a running bridge; None when no bridge answers there."""
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/plugin/laya{subpath}", method=method, data=b"" if method == "POST" else None
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_S) as reply:  # noqa: S310 — loopback, fixed scheme
            body = json.loads(reply.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            body = json.loads(error.read().decode("utf-8"))
        except ValueError:
            return None
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def status_lines(status: dict | None) -> list[str]:
    """`/status` as a first run reads it: what is serving, and how far a first fine-tune is (pure)."""
    if status is None:
        return ["no bridge answers on that port: start one (tldrack up), then: tldrack laya status"]
    serving = status.get("serving") if isinstance(status.get("serving"), dict) else {}
    state = status.get("state")
    lines = [f"state: {state}"]
    if serving.get("base_only"):
        lines.append(
            f"serving: the base model {serving.get('base')}"
            if serving.get("ready")
            else f"will serve: the base model {serving.get('base')} (state {state})"
        )
        for surface, road in sorted((serving.get("first_fine_tune") or {}).items()):
            if isinstance(road, dict):
                lines.append(
                    f"first fine-tune of {surface}: {road.get('labels')} of {road.get('need')} labels "
                    f"({road.get('open')} open, {road.get('done')} done; {road.get('need_per_class')} of each needed)"
                )
    elif serving:
        lines.append(f"serving: {', '.join(serving.get('fine_tunes') or [])} over the base model {serving.get('base')}")
    if isinstance(status.get("error"), str):
        lines.append(f"error: {status['error']}")
    return lines


def _port(value: str | None) -> int:
    raw = value or os.environ.get(STATUS_PORT_ENV) or str(DEFAULT_STATUS_PORT)
    port = int(raw) if raw.isdigit() else 0
    if not 0 < port < 65536:
        raise SystemExit(f"not a port: {raw}")
    return port


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tldrack laya", description="Install and inspect Laya, the rack's local model.")
    verbs = parser.add_subparsers(dest="verb", required=True)
    install = verbs.add_parser("install", help="install the runtime, download the base model, and serve it")
    install.add_argument("--no-fetch", action="store_true", help="skip the download; the first start fetches it")
    status = verbs.add_parser("status", help="what a running bridge is serving")
    for verb in (install, status):
        verb.add_argument("--status-port", help=f"the bridge's status port (default ${STATUS_PORT_ENV} or {DEFAULT_STATUS_PORT})")
    args = parser.parse_args(argv)
    port = _port(args.status_port)
    if args.verb == "install":
        uv = find_uv()
        if uv is None:
            print("uv was not found on PATH: install it (https://docs.astral.sh/uv/), then run this again", file=sys.stderr)
            return 1
        code, step = run(uv, say=lambda line: print(line, flush=True), fetch=not args.no_fetch)
        if code != 0:
            print(f"[laya] the {step} step exited {code}", file=sys.stderr)
            return code
        started = _bridge("POST", port, "/start")
        print(
            "[laya] installed." + ("" if started is not None else " No bridge is up: it loads the model on its first decision.")
        )
        if started is None:
            return 0
    for line in status_lines(_bridge("GET", port, "/status")):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
