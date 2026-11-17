"""Build the two npm tarballs a release carries: `@tldrack/schemas` and `@tldrack/ui`.

    python3 scripts/pack.py                          # the tree's version, into dist/release/
    python3 scripts/pack.py --version 0.1.0-rc.1     # what release.yml runs for the tag v0.1.0-rc.1
    python3 scripts/pack.py --skip-install           # reuse the node_modules already installed

One script for both places. `.github/workflows/release.yml` runs it on a `v*` tag, and `release.py tag`
runs it on this machine before it tags, so a release that would not build fails before anything is public.

The version. The tree holds one number, X.Y.Z, written in every file of `VERSION_FILES`; this script
refuses when they disagree. A tag may add a prerelease on top of it (`v0.1.0-rc.1` over a tree at 0.1.0),
so the candidate that passed and the release that follows are the same commit. The bump itself is an
ordinary commit: unlike a PyPI version, a GitHub tag can be deleted and its release made again.

Nothing in the tree changes. A package is built in place (its build output is gitignored), copied to a
staging directory, stamped there, and packed from there with npm's lifecycle scripts off: the build has
already run, and a `prepack` in the copy would run it again with no dependencies installed. The stamp:

- `version` becomes the release's, so the tarball is `tldrack-<directory>-<version>.tgz`.
- A dependency on another package of this release (ui on schemas) becomes that release's tarball URL. A
  `file:../schemas` link installs for no one but this checkout, and no `@tldrack/*` is on the npm
  registry, so a range would not resolve either.

Installs are `npm ci`: a release builds from a committed `package-lock.json`, never from whatever a
range resolves to on the day. Stdlib only; it drives `npm`, which a release needs anyway.

What a tarball carries beyond its directory: the root's `LICENSE` and `NOTICE` (`LEGAL_FILES`), copied
into the staging directory, so a consumer of either package receives the licence text, not only a
`"license"` field. The root owns both; the copy is made fresh on every pack. Then every finished tarball
is opened and checked (`scripts/check_archive.py`: the purity rules over what it really holds, build
output included, and the legal files present), and its digest goes into `SHA256SUMS` beside it, so the
release job can prove the files it uploaded are the files that were checked.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_archive  # noqa: E402  (a sibling script, stdlib only)

ROOT = Path(__file__).resolve().parents[1]
REPO_SLUG = "voidcraft-world/tldrack"
REPO_URL = f"https://github.com/{REPO_SLUG}"
SCOPE = "@tldrack"
DEFAULT_OUT = Path("dist") / "release"


@dataclass(frozen=True)
class Package:
    directory: str  # below the root; also the npm name (`@tldrack/<directory>`) and the asset's
    # A path prefix the tarball must carry once built, for a package whose release IS its build
    # output. None: the package ships its sources, and is built only when it declares `build`.
    build_output: str | None = None

    @property
    def name(self) -> str:
        return f"{SCOPE}/{self.directory}"


# Order is build order: ui may import schemas.
PACKAGES = (Package("schemas"), Package("ui", build_output="dist/"))
RELEASED = {package.name: package for package in PACKAGES}
# Where the tree's X.Y.Z is written. The `tldrack` Python package is installed from a checkout of the
# tag (`uv tool install ./bridge`), so its `--version` must agree with the tarballs.
VERSION_FILES = ("bridge/pyproject.toml", *(f"{p.directory}/package.json" for p in PACKAGES))
# Never copied into the staging directory: installed dependencies and caches.
STAGE_SKIP = ("node_modules", ".vite", "coverage", ".DS_Store")
# Copied from the root into every tarball (module docstring).
LEGAL_FILES = ("LICENSE", "NOTICE")
SUMS_FILE = "SHA256SUMS"

# SemVer 2.0.0 without build metadata: X.Y.Z, then an optional `-` prerelease.
_IDENT = r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
VERSION_RE = re.compile(rf"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-({_IDENT}(?:\.{_IDENT})*))?$")
PROJECT_HEADER_RE = re.compile(r"^\[project\]\s*$", re.MULTILINE)
TABLE_HEADER_RE = re.compile(r"^\[", re.MULTILINE)
PYPROJECT_VERSION_RE = re.compile(r'^version\s*=\s*"([^"]*)"', re.MULTILINE)


class PackError(Exception):
    """The tree cannot be packed as asked; the message says what to change."""


# --- Pure: versions, names, manifests ---------------------------------------------------------


def parse_version(text: str) -> tuple[int, int, int, str | None]:
    match = VERSION_RE.match(text.strip())
    if not match:
        raise PackError(f"not a release version (X.Y.Z or X.Y.Z-pre): {text!r}")
    major, minor, patch, pre = match.groups()
    return int(major), int(minor), int(patch), pre


def version_key(text: str) -> tuple:
    """Sorts by SemVer precedence: a release above its prereleases, numeric identifiers below
    alphanumeric ones and compared as numbers, a longer prerelease above its own prefix."""
    major, minor, patch, pre = parse_version(text)
    if pre is None:
        return (major, minor, patch, (1,))
    idents = tuple((0, int(i), "") if i.isdigit() else (1, 0, i) for i in pre.split("."))
    return (major, minor, patch, (0, idents))


def base_version(text: str) -> str:
    """`0.1.0-rc.1` → `0.1.0`: the number the tree must hold for this release."""
    major, minor, patch, _ = parse_version(text)
    return f"{major}.{minor}.{patch}"


def asset_name(directory: str, version: str) -> str:
    """The file `npm pack` writes for `@tldrack/<directory>` at `version`."""
    return f"tldrack-{directory}-{version}.tgz"


def asset_url(directory: str, version: str) -> str:
    """Where the GitHub release for `v<version>` serves that tarball. A consumer pins this."""
    return f"{REPO_URL}/releases/download/v{version}/{asset_name(directory, version)}"


def pyproject_version(text: str) -> str | None:
    """The `version` of the `[project]` table; a regex, so it runs where tomllib does not (3.10)."""
    header = PROJECT_HEADER_RE.search(text)
    if not header:
        return None
    nxt = TABLE_HEADER_RE.search(text, header.end())
    match = PYPROJECT_VERSION_RE.search(text[header.end() : nxt.start() if nxt else len(text)])
    return match.group(1) if match else None


def stamp_manifest(manifest: dict, version: str) -> dict:
    """The manifest a release tarball carries: its version, and sibling packages pinned to this
    release's tarballs. Returns a copy; development-only fields stay as they are."""
    out = dict(manifest)
    out["version"] = version
    for field in ("dependencies", "optionalDependencies"):
        deps = out.get(field)
        if isinstance(deps, dict):
            out[field] = {
                name: asset_url(RELEASED[name].directory, version) if name in RELEASED else spec for name, spec in deps.items()
            }
    return out


# --- The tree ---------------------------------------------------------------------------------


def read_manifest(package: Package) -> dict:
    path = ROOT / package.directory / "package.json"
    try:
        manifest = json.loads(path.read_text())
    except FileNotFoundError:
        raise PackError(f"{package.directory}/package.json is missing") from None
    except ValueError as err:
        raise PackError(f"{package.directory}/package.json is not JSON: {err}") from None
    if not isinstance(manifest, dict):
        raise PackError(f"{package.directory}/package.json is not an object")
    if manifest.get("name") != package.name:
        raise PackError(f"{package.directory}/package.json is named {manifest.get('name')!r}, not {package.name!r}")
    return manifest


def tree_versions() -> dict[str, str | None]:
    """What each of `VERSION_FILES` says, None where it says nothing (or does not exist)."""
    found: dict[str, str | None] = {}
    for rel in VERSION_FILES:
        path = ROOT / rel
        if not path.is_file():
            found[rel] = None
        elif rel.endswith(".toml"):
            found[rel] = pyproject_version(path.read_text())
        else:
            try:
                value = json.loads(path.read_text()).get("version")
            except (ValueError, AttributeError):
                value = None
            found[rel] = value if isinstance(value, str) else None
    return found


def tree_version() -> str:
    """The one X.Y.Z every file of `VERSION_FILES` agrees on; raises naming each one when not."""
    found = tree_versions()
    values = set(found.values())
    if len(values) == 1 and None not in values:
        (version,) = values
        if parse_version(version)[3] is None:  # raises on a malformed one
            return version
    listing = ", ".join(f"{rel} {value or 'says nothing'}" for rel, value in found.items())
    raise PackError(f"the tree must hold one X.Y.Z, with no prerelease, in every version file: {listing}")


def check_release_version(version: str) -> None:
    tree = tree_version()
    if base_version(version) != tree:
        raise PackError(
            f"the tree is at {tree}: v{version} cannot release it (a tag is the tree's X.Y.Z, "
            "plus an optional -prerelease). Bump the version files in a commit first"
        )


# --- Impure: npm --------------------------------------------------------------------------------


def say(message: str) -> None:
    print(message, flush=True)


def npm(args: list[str], cwd: Path) -> str:
    proc = subprocess.run(["npm", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise PackError(
            f"`npm {' '.join(args)}` in {cwd.name}/ failed ({proc.returncode})" + (f":\n{detail[-2000:]}" if detail else "")
        )
    return proc.stdout


@dataclass(frozen=True)
class Packed:
    package: Package
    path: Path
    size: int
    entries: int
    sha256: str


def build(package: Package, manifest: dict, skip_install: bool) -> None:
    scripts = manifest.get("scripts") if isinstance(manifest.get("scripts"), dict) else {}
    if "build" not in scripts:
        if package.build_output:
            raise PackError(
                f"{package.directory}/package.json has no `build` script, and its release is "
                f"its build output ({package.build_output})"
            )
        return
    directory = ROOT / package.directory
    if not skip_install:
        if not (directory / "package-lock.json").is_file():
            raise PackError(
                f"{package.directory}/ has no package-lock.json: a release builds from locked "
                f"dependencies. Write one (`npm install --package-lock-only` there) and commit it"
            )
        say(f"→ {package.directory}: npm ci")
        npm(["ci", "--no-audit", "--no-fund"], directory)
    say(f"→ {package.directory}: npm run build")
    npm(["run", "build"], directory)


def with_legal_files(manifest: dict) -> dict:
    """A `files` allow-list gains the legal files; npm adds LICENSE by itself, never NOTICE."""
    if not isinstance(manifest.get("files"), list):
        return manifest
    return {**manifest, "files": [*manifest["files"], *(n for n in LEGAL_FILES if n not in manifest["files"])]}


def pack_one(package: Package, manifest: dict, version: str, out: Path, stage_root: Path) -> Packed:
    stage = stage_root / package.directory
    shutil.copytree(ROOT / package.directory, stage, symlinks=True, ignore=shutil.ignore_patterns(*STAGE_SKIP))
    for name in LEGAL_FILES:
        shutil.copyfile(ROOT / name, stage / name)
    stamped = with_legal_files(stamp_manifest(manifest, version))
    (stage / "package.json").write_text(json.dumps(stamped, indent=2) + "\n")
    report = json.loads(npm(["pack", "--ignore-scripts", "--json", "--pack-destination", str(out)], stage))
    entry = report[0] if isinstance(report, list) and report else {}
    expected = asset_name(package.directory, version)
    if entry.get("filename") != expected or entry.get("version") != version:
        raise PackError(
            f"{package.directory}: npm wrote {entry.get('filename')!r} at {entry.get('version')!r}, expected {expected!r}"
        )
    paths = [f.get("path", "") for f in entry.get("files", []) if isinstance(f, dict)]
    if package.build_output and not any(p.startswith(package.build_output) for p in paths):
        raise PackError(
            f"the {package.directory} tarball carries nothing under {package.build_output}: "
            "its package.json `files` must include the build output"
        )
    missing = [name for name in LEGAL_FILES if name not in paths]
    if missing:
        raise PackError(f"the {package.directory} tarball carries no {', '.join(missing)}")
    path = out / expected
    return Packed(package, path, path.stat().st_size, len(paths), hashlib.sha256(path.read_bytes()).hexdigest())


def pack(version: str | None = None, out: Path | None = None, skip_install: bool = False) -> list[Packed]:
    """Build and pack every package of `PACKAGES` into `out`. Returns what was written."""
    release = version or tree_version()
    check_release_version(release)
    out = (out or ROOT / DEFAULT_OUT).resolve()
    out.mkdir(parents=True, exist_ok=True)
    for stale in [*out.glob("tldrack-*.tgz"), out / SUMS_FILE]:  # `out` then holds exactly this run's
        stale.unlink(missing_ok=True)
    manifests = {package: read_manifest(package) for package in PACKAGES}  # every refusal before any work
    absent = [name for name in LEGAL_FILES if not (ROOT / name).is_file()]
    if absent:
        raise PackError(f"the root has no {', '.join(absent)}: every tarball carries the root's legal files")
    packed = []
    with tempfile.TemporaryDirectory(prefix="tldrack-pack-") as stage_root:
        for package in PACKAGES:
            build(package, manifests[package], skip_install)
            packed.append(pack_one(package, manifests[package], release, out, Path(stage_root)))
    problems = [f"{item.path.name}: {problem}" for item in packed for problem in check_archive.check(item.path, LEGAL_FILES)]
    if problems:
        for item in packed:
            item.path.unlink()  # a refused tarball must not be the one a later step uploads
        raise PackError("a tarball failed its content check:\n  " + "\n  ".join(problems))
    (out / SUMS_FILE).write_text("".join(f"{item.sha256}  {item.path.name}\n" for item in packed))
    for item in packed:
        say(f"✓ {item.path.name}  {item.size / 1024:.1f} KiB  {item.entries} files  sha256 {item.sha256[:16]}  checked")
    return packed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="pack.py", description=__doc__.splitlines()[0])
    parser.add_argument("--version", help="the release version (default: the tree's X.Y.Z)")
    parser.add_argument("--out", type=Path, help=f"where the tarballs go (default: {DEFAULT_OUT}/)")
    parser.add_argument("--skip-install", action="store_true", help="build with the node_modules already here")
    args = parser.parse_args(argv)
    try:
        pack(args.version, args.out, args.skip_install)
    except PackError as err:
        say(f"✗ {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
