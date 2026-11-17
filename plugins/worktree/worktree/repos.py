"""The object the bridge mounts: one `WorktreePlugin` per repository the
operator configured, picked per request by `repo`.

── Which repositories ─────────────────────────────────────────────────────
`plugins.worktree.repos` in the operator's `settings.json` (the settings seam is
owned by plugins/README.md): a list of paths, or `{"path", "label"}` objects.
Unset, it is the one repository the bridge starts in. Each entry resolves to
its MAIN checkout, so naming a worktree of a repository names that repository.
Refused, with the reason on `GET /repos`: a path that is not a git checkout, a
second entry for the same repository, and a second repository with the same
directory name (both would want `~/worktrees/<name>`).

── Picking one ────────────────────────────────────────────────────────────
Every route takes `repo`, in the query or the JSON body: a key from `/repos`,
or a path inside the repository or inside one of its worktrees. Absent, the
first repository answers, so a client written before this (a frozen copy of
the rack UI) sees exactly the single-repository plugin it was written against.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from worktree.listing import list_worktrees, main_worktree_path
from worktree.names import display_path
from worktree.plugin import PLUGIN_NAME, WorktreePlugin, gh_found
from worktree.repo import RepoSpec, repo_dir_name, repo_spec, start_dir_spec
from worktree.store import adopt_unkeyed_state

#: A rack serves a handful of repositories; past this the list is a mistake.
REPOS_MAX = 16
LABEL_MAX = 40
#: The longest `repo` a request may name, key or path.
REPO_PARAM_MAX = 4096


def plugin_settings(context: object) -> dict:
    """This plugin's object of the operator's config: plugins/README.md owns the seam."""
    reader = getattr(context, "plugin_settings", None)
    if not callable(reader):
        return {}
    try:
        value = reader(PLUGIN_NAME)
    except Exception:  # noqa: BLE001 — a host's broken reader must not stop the plugin
        return {}
    return dict(value) if isinstance(value, dict) else {}


def main_checkout_of(path: Path) -> Path | None:
    """The main checkout of the repository `path` is in, or None (not a git checkout)."""
    if not path.is_dir():
        return None
    main = main_worktree_path(list_worktrees(path))
    return Path(main).resolve() if main else None


def repo_entries(raw: object, start_dir: str) -> tuple[list[tuple[str, str | None]], list[dict]]:
    """`repos` as `[(path, label)]` plus the entries refused for their SHAPE.
    Pure: nothing here touches the disk."""
    if raw is None:
        return [(start_dir, None)], []
    if not isinstance(raw, list):
        return [(start_dir, None)], [{"entry": str(raw)[:80], "reason": "`repos` is not a list"}]
    entries: list[tuple[str, str | None]] = []
    refused: list[dict] = []
    for item in raw:
        if isinstance(item, str):
            path, label = item, None
        elif isinstance(item, dict) and isinstance(item.get("path"), str):
            path = item["path"]
            label = item.get("label") if isinstance(item.get("label"), str) else None
        else:
            refused.append({"entry": str(item)[:80], "reason": "not a path or a {path, label} object"})
            continue
        label = " ".join(label.split())[:LABEL_MAX] or None if label else None
        if not path.strip():
            refused.append({"entry": "", "reason": "empty path"})
            continue
        if len(entries) == REPOS_MAX:
            refused.append({"entry": path[:80], "reason": f"more than {REPOS_MAX} repositories"})
            continue
        entries.append((path, label))
    return entries, refused


def resolve_repos(
    raw: object,
    start_dir: str,
    *,
    find_main: Callable[[Path], Path | None] = main_checkout_of,
    build_spec: Callable[..., RepoSpec] = repo_spec,
) -> tuple[list[RepoSpec], list[dict]]:
    """The configured repositories as specs, in order, and every entry refused."""
    entries, refused = repo_entries(raw, start_dir)
    specs: list[RepoSpec] = []
    names: dict[str, Path] = {}
    for path, label in entries:
        main = find_main(Path(path).expanduser())
        if main is None:
            refused.append({"entry": display_path(path), "reason": "not a git checkout"})
            continue
        if any(spec.root == main for spec in specs):
            refused.append({"entry": display_path(path), "reason": "the same repository twice"})
            continue
        name = repo_dir_name(main)
        if name in names:
            refused.append(
                {
                    "entry": display_path(path),
                    "reason": f"another repository is also named '{name}' ({display_path(names[name])})"
                    f" and both would use ~/worktrees/{name}",
                }
            )
            continue
        names[name] = main
        specs.append(build_spec(main, label=label))
    return specs, refused


class RepoRouter:
    name = PLUGIN_NAME
    max_body_bytes = WorktreePlugin.max_body_bytes

    def __init__(
        self,
        context,
        *,
        build_unit: Callable[..., WorktreePlugin] = WorktreePlugin,
        find_main: Callable[[Path], Path | None] = main_checkout_of,
        build_spec: Callable[..., RepoSpec] = repo_spec,
    ) -> None:
        self._context = context
        start_dir = str(getattr(context, "start_dir", None) or os.getcwd())
        specs, self._refused = resolve_repos(
            plugin_settings(context).get("repos"), start_dir, find_main=find_main, build_spec=build_spec
        )
        for refusal in self._refused:
            context.log(f"  worktree: skipped repository {refusal['entry']!r}: {refusal['reason']}")
        if not specs:
            # Not a repository, and none configured: the single-repository plugin
            # on the start directory, whose `/state` already says what is wrong.
            specs = [start_dir_spec(context)]
        else:
            # The state an unkeyed plugin kept belongs to the repository it started in.
            start_main = find_main(Path(start_dir).expanduser())
            for spec in specs:
                if spec.root == start_main:
                    copied = adopt_unkeyed_state(spec.store_path)
                    if copied:
                        context.log(
                            f"  worktree: {spec.label}: kept {len(copied)} state files from before repositories were keyed"
                        )
        self._units = [(spec, build_unit(context, spec)) for spec in specs]
        context.log(
            "  worktree: " + ", ".join(f"{spec.label} → {display_path(spec.worktree_root)}" for spec, _unit in self._units)
        )

    def routes(self) -> list[str]:
        return [*self._units[0][1].routes(), f"/plugin/{self.name}/repos"]

    def busy_reasons(self) -> list[str]:
        many = len(self._units) > 1
        return [f"{spec.label}: {reason}" if many else reason for spec, unit in self._units for reason in unit.busy_reasons()]

    def close(self) -> None:
        for _spec, unit in self._units:
            close = getattr(unit, "close", None)
            if callable(close):
                close()

    def handle_get(self, subpath: str, query: dict[str, list[str]]) -> tuple:
        if subpath == "/repos":
            return 200, self._repos_view()
        unit, refusal = self._pick(query, None)
        return refusal if unit is None else unit.handle_get(subpath, query)

    def handle_post(self, subpath: str, query: dict[str, list[str]], body: dict | None) -> tuple:
        unit, refusal = self._pick(query, body)
        return refusal if unit is None else unit.handle_post(subpath, query, body)

    def _repos_view(self) -> dict:
        found = gh_found()
        return {
            "repos": [{**spec.view(), "default": index == 0, "gh": found} for index, (spec, _unit) in enumerate(self._units)],
            "default": self._units[0][0].key,
            "skipped": self._refused,
        }

    def _pick(self, query: dict[str, list[str]], body: dict | None) -> tuple[WorktreePlugin | None, tuple | None]:
        raw = body.get("repo") if isinstance(body, dict) and "repo" in body else (query.get("repo") or [None])[0]
        if raw is None or raw == "":
            return self._units[0][1], None
        if not isinstance(raw, str) or len(raw) > REPO_PARAM_MAX:
            return None, (400, {"error": "`repo` is a repository key or a path"})
        for spec, unit in self._units:
            if raw == spec.key:
                return unit, None
        target = _resolved(raw)
        if target is not None:
            for spec, unit in self._units:
                if any(
                    target == base or base in target.parents
                    for base in (_resolved(spec.root), _resolved(spec.worktree_root))
                    if base is not None
                ):
                    return unit, None
        return None, (
            404,
            {"error": "unknown repository — GET /repos lists them", "repos": [spec.key for spec, _unit in self._units]},
        )


def _resolved(path: str | os.PathLike[str]) -> Path | None:
    try:
        candidate = Path(path).expanduser()
        return candidate.resolve() if candidate.is_absolute() else None
    except (OSError, RuntimeError, ValueError):
        return None
