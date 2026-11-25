"""Cut a release: a `vX.Y.Z` tag on `main`, and a GitHub release carrying the two npm tarballs.

    python3 scripts/release.py tag 0.1.0              # on main, pushed and green: tag, publish, verify
    python3 scripts/release.py tag 0.1.0-rc.1         # a candidate; the same commit can become 0.1.0
    python3 scripts/release.py sync-consumers         # move dependents to the latest release

Every subcommand takes `--dry-run`: every check and the plan, nothing changed. A dry run reports all
the checks that would refuse, not just the first, and exits 1 when there is one.

Ported from the bridge core's `scripts/release.py`, without its `prepare`. The core needs that step
because a PyPI version can never be uploaded twice, so its bump is reviewed before the irreversible tag.
A GitHub tag can be deleted and its release made again, so here the version bump is an ordinary commit
(`scripts/pack.py` owns which files hold the version and why a tag may add a prerelease), and `tag` is
told the version.

`tag` checks that `main` is clean, even with GitHub and green in CI, that the tag is new and newer than
every release, that a final tag passes the gate `release.yml` holds for one (`check_cited_commits`), and
that the tarballs build HERE (`scripts/pack.py`). Then it asks, tags, pushes the tag,
waits for `release.yml` (which runs the same `pack.py` on GitHub and attaches both tarballs), checks that
the release carries them, and syncs the consumers. It is re-runnable: a tag already on GitHub skips to
the wait and the verification.

Consumers. A project that installs the tarballs (a host that embeds the rack UI) follows releases: list
its directories in `TLDRACK_CONSUMERS` (`os.pathsep`-separated) or pass `--consumer`. Each one's
`package.json` gets the new tarball URLs, then `npm install` moves its lockfile. The change is left
uncommitted: the consumer owns its own history.

Stdlib only. It drives `git`, `gh` and `npm`, which a maintainer already has.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pack  # noqa: E402  (scripts/ is not a package)

CONSUMERS_ENV = "TLDRACK_CONSUMERS"
RELEASE_WORKFLOW = "release.yml"
CI_WORKFLOW = "ci.yml"
# The gate `release.yml` runs for a final tag: this test, switched on by this variable.
CITED_COMMITS_TEST = "ui/tests/unit/bench-commits.test.ts"
CITED_COMMITS_ENV = "TLDRACK_REQUIRE_CITED_COMMITS"
CITED_FILE_RE = re.compile(r"'([^'/]+\.json)'")
# The repository is public; a commit or tag with a personal address publishes it for good.
NOREPLY_SUFFIX = "@users.noreply.github.com"
# The push URLs that reach the public repository. Anything else (a fork, a disabled URL) cannot publish.
PUSH_URLS = (
    pack.REPO_URL,
    f"{pack.REPO_URL}.git",
    f"git@github.com:{pack.REPO_SLUG}.git",
    f"ssh://git@github.com/{pack.REPO_SLUG}.git",
)
TAG_REF_RE = re.compile(r"^[0-9a-f]+\trefs/tags/v([^\s^]+)$", re.MULTILINE)


class ReleaseError(Exception):
    """A preflight or verification failed; the message says what to do."""


# --- Pure ----------------------------------------------------------------------------------------


def tag_name(version: str) -> str:
    return f"v{version}"


def remote_versions(ls_remote: str) -> list[str]:
    """The release versions among `git ls-remote --tags` output; peeled refs and odd tags skipped."""
    found = []
    for version in TAG_REF_RE.findall(ls_remote):
        try:
            pack.parse_version(version)
        except pack.PackError:
            continue
        found.append(version)
    return sorted(set(found), key=pack.version_key)


def tag_commit(ls_remote: str, ref: str) -> str | None:
    """The commit `ref` names in `git ls-remote --tags` output. An annotated tag lists its own object
    and then, peeled (`^{}`), the commit it points at; a lightweight tag lists the commit alone."""
    found: dict[str, str] = {}
    for line in ls_remote.splitlines():
        sha, _, name = line.partition("\t")
        found[name] = sha
    return found.get(f"refs/tags/{ref}^{{}}") or found.get(f"refs/tags/{ref}")


def parse_consumers(env_value: str, extra: list[str] | None = None) -> list[Path]:
    """Consumer directories from the env var plus `--consumer`, expanded, deduplicated, in order."""
    seen: dict[Path, None] = {}
    for part in [*env_value.split(os.pathsep), *(extra or [])]:
        if part.strip():
            seen.setdefault(Path(part.strip()).expanduser().resolve(), None)
    return list(seen)


def released_specs(manifest: dict) -> dict[str, str]:
    """What a consumer's manifest asks for each released package, from any dependency field."""
    specs: dict[str, str] = {}
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = manifest.get(field)
        if isinstance(deps, dict):
            specs.update({name: spec for name, spec in deps.items() if name in pack.RELEASED and isinstance(spec, str)})
    return specs


def wanted_specs(manifest: dict, version: str) -> dict[str, str]:
    """The URL each released package the consumer uses should point at for `version`."""
    return {name: pack.asset_url(pack.RELEASED[name].directory, version) for name in released_specs(manifest)}


def rewrite_manifest(text: str, version: str) -> str:
    """The consumer's `package.json` with every released package moved to `version`'s tarball.
    Key order and indentation are kept; npm writes the same two-space JSON."""
    manifest = json.loads(text)
    wanted = wanted_specs(manifest, version)
    for field in ("dependencies", "devDependencies", "optionalDependencies"):
        deps = manifest.get(field)
        if isinstance(deps, dict):
            for name in deps.keys() & wanted.keys():
                deps[name] = wanted[name]
    indent = re.search(r"^( +|\t)\S", text, re.MULTILINE)
    return json.dumps(manifest, indent=indent.group(1) if indent else 2, ensure_ascii=False) + "\n"


def lock_mismatches(lock: dict, wanted: dict[str, str], version: str) -> list[str]:
    """What a consumer's `package-lock.json` (v2 or v3) records that is not `wanted`. Empty: in sync."""
    packages = lock.get("packages") if isinstance(lock.get("packages"), dict) else {}
    problems = []
    for name, url in wanted.items():
        entry = packages.get(f"node_modules/{name}")
        if not isinstance(entry, dict):
            problems.append(f"{name} is not in package-lock.json")
        elif entry.get("resolved") != url or entry.get("version") != version:
            problems.append(f"{name} locks {entry.get('version')} from {entry.get('resolved')}")
    return problems


# --- Impure: processes ---------------------------------------------------------------------------


def say(message: str) -> None:
    print(message, flush=True)


def run(
    cmd: list[str],
    cwd: Path | None = None,
    *,
    capture: bool = True,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> str:
    """Run a command. Captured output is returned; on failure, it is the error. `env` adds to this
    process's environment."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd or pack.ROOT,
            text=True,
            capture_output=capture,
            check=False,
            env={**os.environ, **env} if env else None,
        )
    except FileNotFoundError:
        raise ReleaseError(f"`{cmd[0]}` is not installed") from None
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() if capture else ""
        raise ReleaseError(f"`{' '.join(cmd)}` failed ({proc.returncode})" + (f":\n{detail}" if detail else ""))
    return (proc.stdout or "").strip() if capture else ""


def git(*args: str) -> str:
    return run(["git", *args])


def gh(*args: str, capture: bool = True) -> str:
    """Every GitHub call names the public repository, so a fork or a worktree cannot redirect it."""
    return run(["gh", *args, "--repo", pack.REPO_SLUG], capture=capture)


def act(dry_run: bool, description: str, cmd: list[str], cwd: Path | None = None) -> None:
    """A step that changes something: printed under --dry-run, run otherwise."""
    if dry_run:
        say(f"  would {description}: {' '.join(cmd)}")
        return
    say(f"→ {description}")
    run(cmd, cwd)


def confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


# --- tag: checks ---------------------------------------------------------------------------------

OK, WAIT, FAIL = "✓", "…", "✗"


@dataclass(frozen=True)
class Check:
    state: str  # OK | WAIT (CI still running: a real tag waits for it) | FAIL
    line: str


def safely(label: str, probe) -> Check:
    """Run one check; a tool that fails becomes that check's ✗, never the end of the report. A failed
    command's error ends with what the tool said, and that last line is the part worth reading."""
    try:
        return probe()
    except (ReleaseError, pack.PackError) as err:
        lines = [line for line in str(err).splitlines() if line.strip()]
        return Check(FAIL, f"{label}: {lines[-1] if lines else type(err).__name__}")


def check_identity() -> Check:
    email = run(["git", "config", "user.email"], check=False)
    if email.endswith(NOREPLY_SUFFIX):
        return Check(OK, "git identity is a GitHub noreply address")
    return Check(
        FAIL,
        f"git user.email is not a GitHub noreply address (…{NOREPLY_SUFFIX}): the history is "
        "public. Set it for this repository: `git config user.email …`",
    )


def check_push_url() -> Check:
    url = git("remote", "get-url", "--push", "origin")
    if url in PUSH_URLS:
        return Check(OK, f"origin pushes to {pack.REPO_SLUG}")
    return Check(FAIL, f"origin's push URL is {url!r}: this checkout cannot publish to {pack.REPO_URL}")


def check_main(head: str) -> Check:
    branch = git("branch", "--show-current")
    dirty = git("status", "--porcelain")
    if branch != "main":
        return Check(FAIL, f"on `{branch or 'a detached HEAD'}`, not `main`")
    if dirty:
        return Check(FAIL, f"the working tree is not clean ({len(dirty.splitlines())} paths): commit first")
    remote = git("ls-remote", "origin", "refs/heads/main").split("\t")[0]
    if remote != head:
        return Check(FAIL, f"`main` is {head[:7]} here and {remote[:7] or 'absent'} on origin: push or pull first")
    return Check(OK, f"on `main` at {head[:7]}, clean and even with origin")


def check_tag(version: str, released: list[str]) -> Check:
    ref = tag_name(version)
    if git("tag", "--list", ref):
        return Check(FAIL, f"{ref} exists here and not on origin: inspect it, then push it or `git tag -d {ref}`")
    newer = [v for v in released if pack.version_key(v) >= pack.version_key(version)]
    if newer:
        return Check(FAIL, f"{ref} is not newer than every release on origin (v{newer[-1]})")
    return Check(OK, f"{ref} is free, and newer than {f'v{released[-1]}' if released else 'any tag (the first release)'}")


def check_tree(version: str) -> Check:
    pack.check_release_version(version)
    return Check(OK, f"the tree is at {pack.tree_version()} ({', '.join(pack.VERSION_FILES)})")


def ci_run(head: str) -> dict | None:
    runs = json.loads(
        gh("run", "list", "--commit", head, "--workflow", CI_WORKFLOW, "--json", "databaseId,status,conclusion") or "[]"
    )
    return runs[0] if runs else None


def check_ci(head: str) -> Check:
    latest = ci_run(head)
    if latest is None:
        return Check(FAIL, f"no `ci` run for {head[:7]}: push `main`, or wait for GitHub to start one")
    if latest.get("status") != "completed":
        return Check(WAIT, f"ci run {latest.get('databaseId')} on {head[:7]} is still running: a tag waits for it")
    if latest.get("conclusion") != "success":
        return Check(FAIL, f"ci on {head[:7]} concluded `{latest.get('conclusion')}`")
    return Check(OK, f"ci is green on {head[:7]}")


def cited_commits_gate() -> None:
    """Run the release job's gate for a final tag on this checkout; a refusal is the error."""
    run(["node", "--test", CITED_COMMITS_TEST], env={CITED_COMMITS_ENV: "1"})


def check_cited_commits() -> Check:
    """`release.yml` refuses a final tag whose bench results cite a commit outside `main`'s history, and
    it runs after the tag is pushed: the refusal leaves a public tag with no release, to be deleted and
    cut again. The same gate run here refuses before anything is public. The test it runs owns the rule,
    why a release candidate is exempt, and the fix."""
    try:
        cited_commits_gate()
    except ReleaseError as err:
        files = sorted(set(CITED_FILE_RE.findall(str(err))))
        if not files:
            raise  # node is missing or the test broke another way: `safely` reports what it said
        return Check(
            FAIL,
            f"bench/results/ cites a commit outside `main`'s history ({', '.join(files)}): {RELEASE_WORKFLOW} "
            f"would refuse this tag after it is public. {CITED_COMMITS_TEST} owns the fix",
        )
    return Check(OK, "every bench results file cites a commit in `main`'s history")


def preflight(version: str, head: str, released: list[str]) -> list[Check]:
    final = pack.parse_version(version)[3] is None
    return [
        safely("identity", check_identity),
        safely("push URL", check_push_url),
        safely("main", lambda: check_main(head)),
        safely("tag", lambda: check_tag(version, released)),
        safely("version", lambda: check_tree(version)),
        safely("ci", lambda: check_ci(head)),
        *([safely("bench commits", check_cited_commits)] if final else []),
    ]


# --- tag: the plan and the act -------------------------------------------------------------------


def plan_lines(version: str, head: str, consumers: list[Path]) -> list[str]:
    ref = tag_name(version)
    assets = " and ".join(pack.asset_name(p.directory, version) for p in pack.PACKAGES)
    return [
        f"build both tarballs here: python3 scripts/pack.py --version {version} --out <a temporary directory>",
        "ask, unless --yes",
        f'tag: git tag -a {ref} {head[:7]} -m "tldrack {version}"',
        f"push the tag: git push origin {ref}",
        f"wait for {RELEASE_WORKFLOW}, then check that the {'pre' if pack.parse_version(version)[3] else ''}release "
        f"{ref} carries {assets}",
        f"sync consumers: {', '.join(map(str, consumers)) if consumers else f'none (set {CONSUMERS_ENV} or pass --consumer)'}",
    ]


def watch_release_run(ref: str, commit: str) -> None:
    """Wait for the release run of `ref` AT `commit`. A tag that was deleted and cut again leaves its
    first run behind under the same name, and until GitHub starts the new one that old run is the only
    answer to the name alone: the release would be judged by a run of another commit."""
    for _ in range(30):
        runs = json.loads(
            gh("run", "list", "--workflow", RELEASE_WORKFLOW, "--branch", ref, "--commit", commit, "--json", "databaseId") or "[]"
        )
        if runs:
            say(f"→ watching release run {runs[0]['databaseId']}")
            gh("run", "watch", str(runs[0]["databaseId"]), "--exit-status", capture=False)
            return
        time.sleep(5)
    raise ReleaseError(f"no {RELEASE_WORKFLOW} run started for {ref} at {commit[:7]}: check the Actions tab")


def verify_release(version: str) -> None:
    ref = tag_name(version)
    info = json.loads(gh("release", "view", ref, "--json", "assets,isPrerelease"))
    names = {asset.get("name") for asset in info.get("assets", [])}
    missing = [pack.asset_name(p.directory, version) for p in pack.PACKAGES if pack.asset_name(p.directory, version) not in names]
    if missing:
        raise ReleaseError(f"the GitHub release {ref} lacks {', '.join(missing)}")
    if bool(info.get("isPrerelease")) != (pack.parse_version(version)[3] is not None):
        raise ReleaseError(f"the GitHub release {ref} is marked prerelease={info.get('isPrerelease')}")
    say(f"✓ {ref}: the GitHub release carries both tarballs")


def tag(version: str, yes: bool, dry_run: bool, consumers: list[str]) -> None:
    pack.parse_version(version)  # a malformed version ends here, before any probe
    ref = tag_name(version)
    head = git("rev-parse", "HEAD")
    try:
        remote_tags, unreachable = git("ls-remote", "--tags", "origin"), None
    except ReleaseError as err:
        remote_tags, unreachable = "", Check(FAIL, f"origin's tags: {err}")
    targets = parse_consumers(os.environ.get(CONSUMERS_ENV, ""), consumers)

    if f"refs/tags/{ref}" in remote_tags.split():
        say(f"{ref} is already on origin: waiting for and verifying what it published")
        if dry_run:
            say(f"  would watch {RELEASE_WORKFLOW}, verify the release, then sync consumers")
            return
        watch_release_run(ref, tag_commit(remote_tags, ref) or head)
        verify_release(version)
        sync_consumers(version, consumers, dry_run)
        return

    checks = preflight(version, head, remote_versions(remote_tags)) + ([unreachable] if unreachable else [])
    say(f"Release {ref} from {head[:7]}\n\nChecks")
    for check in checks:
        say(f"  {check.state} {check.line}")
    say("\nPlan")
    for number, line in enumerate(plan_lines(version, head, targets), start=1):
        say(f"  {number}. {line}")
    failures = [c for c in checks if c.state == FAIL]

    if dry_run:
        say(
            f"\n(dry run: nothing changed; {len(failures) or 'no'} check{'s' if len(failures) != 1 else ''} "
            f"would refuse this tag)"
        )
        if targets:
            say("")
            sync_consumers(version, consumers, dry_run=True, require_release=False)
        if failures:
            raise ReleaseError(f"{ref} would be refused today")
        return

    if failures:
        raise ReleaseError("refused:\n" + "\n".join(f"  {c.line}" for c in failures))
    if any(c.state == WAIT for c in checks):
        latest = ci_run(head)
        say(f"→ waiting for ci run {latest and latest.get('databaseId')}")
        if latest:
            gh("run", "watch", str(latest["databaseId"]), "--exit-status", capture=False)
    with tempfile.TemporaryDirectory(prefix="tldrack-release-") as out:
        say("→ building both tarballs here before anything is public")
        pack.pack(version, Path(out))
    if not yes and not confirm(f"Tag {ref} at {head[:7]} and publish it?"):
        raise ReleaseError("not confirmed")
    act(False, f"tag {ref}", ["git", "tag", "-a", ref, head, "-m", f"tldrack {version}"])
    act(False, f"push {ref}", ["git", "push", "--quiet", "origin", ref])
    watch_release_run(ref, head)
    verify_release(version)
    sync_consumers(version, consumers, dry_run)


# --- sync-consumers ------------------------------------------------------------------------------


def latest_release() -> str:
    """The newest full release on GitHub (prereleases are never `latest`)."""
    tag_ref = json.loads(gh("release", "view", "--json", "tagName")).get("tagName", "")
    if not tag_ref.startswith("v"):
        raise ReleaseError(f"{pack.REPO_SLUG} has no release yet")
    return tag_ref[1:]


def sync_one(consumer: Path, version: str, dry_run: bool) -> str:
    """Move one consumer to `version`. Returns its line for the report; raises on a failure."""
    manifest_path, lock_path = consumer / "package.json", consumer / "package-lock.json"
    if not manifest_path.is_file():
        raise ReleaseError(f"{consumer}: no package.json")
    text = manifest_path.read_text()
    manifest = json.loads(text)
    current = released_specs(manifest)
    if not current:
        raise ReleaseError(f"{consumer}: depends on none of {', '.join(pack.RELEASED)}")
    wanted = wanted_specs(manifest, version)
    lock = json.loads(lock_path.read_text()) if lock_path.is_file() else {}
    if current == wanted and not lock_mismatches(lock, wanted, version):
        return f"✓ {consumer}: already on {version}"
    if dry_run:
        moves = (
            "; ".join(f"{name}: {current[name]} → {wanted[name]}" for name in sorted(wanted) if current[name] != wanted[name])
            or "package.json already there"
        )
        return f"  would move {consumer} ({moves}), then `npm install` there"
    manifest_path.write_text(rewrite_manifest(text, version))
    say(f"→ {consumer}: npm install")
    run(["npm", "install", "--no-audit", "--no-fund"], consumer)
    problems = lock_mismatches(json.loads(lock_path.read_text()) if lock_path.is_file() else {}, wanted, version)
    if problems:
        raise ReleaseError(f"{consumer}: after `npm install`, " + "; ".join(problems))
    return f"✓ {consumer}: moved to {version} (package.json and package-lock.json changed, not committed)"


def sync_consumers(version: str | None, extra: list[str], dry_run: bool, require_release: bool = True) -> None:
    consumers = parse_consumers(os.environ.get(CONSUMERS_ENV, ""), extra)
    if not consumers:
        say(f"no consumers to update (set {CONSUMERS_ENV} or pass --consumer)")
        return
    target = version or latest_release()
    if require_release:
        verify_release(target)  # never point a consumer at a tarball that is not there
    failures = []
    for consumer in consumers:
        try:
            say(sync_one(consumer, target, dry_run))
        except (ReleaseError, ValueError, OSError) as err:
            failures.append(str(err) if isinstance(err, ReleaseError) else f"{consumer}: {err}")
    if failures:
        raise ReleaseError("\n".join(failures))


# --- CLI -----------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="release.py", description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p_tag = sub.add_parser("tag", help="check, tag main, wait for the release, verify, sync consumers")
    p_tag.add_argument("version", help="X.Y.Z (the tree's) or X.Y.Z-prerelease")
    p_tag.add_argument("-y", "--yes", action="store_true", help="skip the confirmation")
    p_sync = sub.add_parser("sync-consumers", help="move each consumer to a release's tarballs")
    p_sync.add_argument("--version", help="the release to move to (default: the latest on GitHub)")
    for p in (p_tag, p_sync):
        p.add_argument(
            "--consumer",
            action="append",
            default=[],
            metavar="DIR",
            help=f"a project to move to the release, on top of ${CONSUMERS_ENV}",
        )
        p.add_argument("--dry-run", action="store_true", help="every check and the plan; change nothing")
    args = parser.parse_args(argv)
    try:
        if args.command == "tag":
            tag(args.version, args.yes, args.dry_run, args.consumer)
        else:
            sync_consumers(args.version, args.consumer, args.dry_run)
    except (ReleaseError, pack.PackError) as err:
        say(f"✗ {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
