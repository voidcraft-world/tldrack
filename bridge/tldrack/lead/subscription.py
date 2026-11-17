"""Subscription logins only: a lead and every worker session run on a plan the operator already pays for.

The rule: agent calls are subscription-only. No API key, no Bedrock, no overage
path. A Claude spawn already sheds the API-key env (profiles.py `SHED_ENV`), but a settings-file
`apiKeyHelper` survives that, so the gate asks the CLI itself:

- Claude: `ProfileRegistry.auth_info` (`claude auth status`, cached by the registry) must say
  `billing: subscription`.
- Codex: `codex login status` under the profile's own `CODEX_HOME` must say ChatGPT. Run as the
  binary with a clean environment, so a shell function wrapping `codex` cannot answer for
  it; the output is matched, never echoed (an API-key login prints part of the key).

Unknown is a refusal. A gate that waves through what it could not read is no gate.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from collections.abc import Callable

CACHE_SECONDS = 600
PROBE_TIMEOUT = 10


class SubscriptionGate:
    def __init__(
        self,
        profiles: object,
        *,
        runner: Callable[[list[str], dict], tuple[int, str]] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._profiles = profiles
        self._runner = runner or _run
        self._clock = clock
        self._lock = threading.Lock()
        self._codex: dict[str, tuple[float, str | None]] = {}

    def refusal(self, profile_id: str) -> str | None:
        """None when `profile_id` is a subscription login, else why not."""
        kind = self._profiles.kind_of(profile_id)
        if kind == "claude":
            info = self._profiles.auth_info(profile_id) or {}
            if not info.get("signed_in"):
                return f"{profile_id} is not signed in"
            if info.get("billing") != "subscription":
                return f"{profile_id} is not a subscription login (billing: {info.get('billing') or 'unknown'})"
            return None
        if kind == "codex":
            return self._codex_refusal(profile_id)
        return f"{profile_id} is not an agent profile"

    def _codex_refusal(self, profile_id: str) -> str | None:
        now = self._clock()
        with self._lock:
            cached = self._codex.get(profile_id)
        if cached is not None and now - cached[0] < CACHE_SECONDS:
            return cached[1]
        cli = self._profiles.cli_path(profile_id)
        described = self._profiles.describe(profile_id) or {}
        home = described.get("config_dir")
        if cli is None or not home:
            verdict: str | None = f"{profile_id}: the codex CLI or its CODEX_HOME was not found"
        else:
            env = {
                "CODEX_HOME": os.path.expanduser(str(home)),
                "HOME": str(os.path.expanduser("~")),
                "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin",
            }
            code, out = self._runner([cli, "login", "status"], env)
            if code == 0 and "chatgpt" in out.lower():
                verdict = None
            elif code == 0 and "api key" in out.lower():
                verdict = f"{profile_id} is logged in with an API key, not a ChatGPT subscription"
            else:
                verdict = f"{profile_id}: could not confirm a ChatGPT login"
        with self._lock:
            self._codex[profile_id] = (now, verdict)
        return verdict


def _run(argv: list[str], env: dict) -> tuple[int, str]:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=PROBE_TIMEOUT, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return done.returncode, f"{done.stdout}\n{done.stderr}"
