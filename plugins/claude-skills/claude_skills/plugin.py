"""The plugin object the bridge mounts: GET /state, GET /skill, POST /open, POST /run.

Route handlers return `(status, dict)` per the host contract
(plugins/README.md). The host has already applied its Origin guard and
parsed any JSON body. `POST /run` starts an agent, so it also passes a
narrower caller check (`guard.py`), which needs the request's headers — hence
`handle_request`, which the host calls instead of the pair. `GET /skill` is one
skill's files and text (`files.py`).

The operator's settings and origins are read once, here, at construction
(`settings.py`, `guard.py`); a bridge restart picks up a change.

`GET /state` is a full re-scan every call. That is affordable — a directory walk
over the skill roots plus a few small JSON documents — and it means adding a
skill shows up on the next REFRESH with no invalidation logic to get wrong. The
one genuinely expensive input, the built-ins scrape, is cached per CLI build and
runs on its own thread (see builtins.py).
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from claude_skills.builtins import BuiltinsScanner
from claude_skills.files import handle_skill
from claude_skills.guard import operator_origins, run_refusal_for
from claude_skills.roots import default_roots
from claude_skills.run import SkillRunner, describe_runnable, runnable_from_settings
from claude_skills.scan import (
    mark_superseded,
    scan_available,
    scan_global_skills,
    scan_installed_plugin_skills,
    scan_skills_dir,
    scan_synced_skills,
)
from claude_skills.settings import read_settings

PLUGIN_NAME = "claude-skills"


class ClaudeSkillsPlugin:
    name = PLUGIN_NAME

    def __init__(self, context) -> None:
        self._context = context
        profiles = getattr(context, "profiles", None)
        # A host without a start directory reports "": the project is then where the bridge runs.
        self._roots = default_roots(getattr(context, "start_dir", "") or os.getcwd(), profiles)
        self._builtins = BuiltinsScanner()
        settings = read_settings(context, PLUGIN_NAME, context.log)
        runnable = runnable_from_settings(settings.get("run"), context.log)
        self._runner = SkillRunner(self._roots.project, profiles, context.log, runnable, claude_dir=self._roots.claude_dir)
        allowed = operator_origins(getattr(context, "allowed_origins", None), context.log)
        self._run_refusal = run_refusal_for(getattr(context, "port", None), allowed)

    def routes(self) -> list[str]:
        base = f"/plugin/{self.name}"
        return [f"{base}/state", f"{base}/skill", f"{base}/open", f"{base}/run"]

    # -- the one entry point on a current host --------------------------------
    def handle_request(
        self, method: str, subpath: str, query: dict[str, list[str]], body: dict | None, headers: Mapping[str, str] | None
    ) -> tuple:
        if method == "GET":
            return self.handle_get(subpath, query)
        if method == "POST" and subpath == "/run":
            return self._handle_run(body, headers)
        if method == "POST":
            return self.handle_post(subpath, query, body)
        return 405, {"error": "use GET /state, GET /skill, POST /open or POST /run"}

    # -- GET ----------------------------------------------------------------
    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        if subpath == "/state":
            return self._handle_state(query)
        if subpath == "/skill":
            return handle_skill(query, self._roots.reveal_allowlist())
        return 404, {"error": "unknown route — GET /state or GET /skill?path=…"}

    def _handle_state(self, query: dict[str, list[str]]) -> tuple:
        vendored_only = (query.get("all") or ["0"])[0] not in ("1", "true")
        global_skills = scan_global_skills(self._roots)
        synced_skills = scan_synced_skills(self._roots)
        project_skills = scan_skills_dir(self._roots.project_skills, "project")
        plugin_skills = scan_installed_plugin_skills(self._roots)
        mark_superseded(plugin_skills, global_skills + project_skills)
        available, catalog_total = scan_available(self._roots, vendored_only=vendored_only)
        builtins = self._builtins.get()
        return 200, {
            "version": 1,
            "plugin": self.name,
            "project": str(self._roots.project),
            "tiers": {
                "global": global_skills,
                "synced": synced_skills,
                "project": project_skills,
                "plugin": plugin_skills,
                "available": available,
                "builtin": builtins["skills"],
            },
            "counts": {
                "global": len(global_skills),
                "synced": len(synced_skills),
                "project": len(project_skills),
                "plugin": len(plugin_skills),
                "available": len(available),
                "builtin": len(builtins["skills"]),
                # The full catalog, so the wall can show "N shown of M" and
                # never imply the vendored filter is the whole story.
                "available_catalog_total": catalog_total,
            },
            "builtin_status": builtins["status"],
            # What POST /run accepts, from the operator's settings: a page shows a skill's
            # button only when its key is here.
            "runnable": describe_runnable(self._runner.runnable),
        }

    # -- POST ---------------------------------------------------------------
    def handle_post(self, subpath: str, query: dict[str, list[str]], body: dict | None) -> tuple:
        if subpath == "/run":
            # Reached only from a host without `handle_request`, which never hands over the
            # headers the caller policy reads: fail closed rather than start an agent unguarded.
            return 426, {
                "plugin": self.name,
                "code": "host_too_old",
                "error": "POST /run needs a bridge that passes request headers",
                "hint": "update tldrack",
            }
        if subpath != "/open":
            return 404, {"error": 'unknown route — POST /open {"path": …} or POST /run {"skill": …}'}
        # Imported lazily so a non-macOS host still serves /state normally.
        from claude_skills.reveal import handle_open

        status, payload = handle_open(body, self._roots.reveal_allowlist())
        if status == 403:
            self._context.log(f"  claude-skills: refused reveal — {payload.get('error')}")
        return status, payload

    def _handle_run(self, body: dict | None, headers: Mapping[str, str] | None) -> tuple:
        """Every /run body names the plugin, so a caller tells this plugin's answer from the
        host's own 404 (a bridge that never mounted it)."""
        refusal = self._run_refusal(headers)
        if refusal is not None:
            self._context.log(f"  claude-skills: refused run — {refusal.code}")
            extra = {"allowed_origins": list(refusal.allowed_origins)} if refusal.allowed_origins else {}
            return 403, {"plugin": self.name, "code": refusal.code, "error": refusal.message, **extra}
        status, payload = self._runner.start(body)
        return status, {"plugin": self.name, **payload}
