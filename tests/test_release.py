"""scripts/pack.py and scripts/release.py: the version rule, the stamp, the tarballs, a dry run."""

from __future__ import annotations

import json
import random
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import check_archive  # noqa: E402
import pack  # noqa: E402
import release  # noqa: E402

NPM = shutil.which("npm")
NOREPLY = "someone" + release.NOREPLY_SUFFIX  # invented
UI_BUILD = (
    "node -e \"require('fs').mkdirSync('dist',{recursive:true});require('fs').writeFileSync('dist/index.html','<p>rack</p>')\""
)


def make_tree(root: Path, version: str = "0.1.0", ui_files: list[str] | None = None) -> None:
    """A tree with the three version files: invented packages, nothing installed."""
    (root / "bridge").mkdir(parents=True)
    (root / "LICENSE").write_text("MIT License\n")
    (root / "NOTICE").write_text("Third-party notices\n")
    (root / "bridge" / "pyproject.toml").write_text(
        f'[project]\nname = "tldrack"\nversion = "{version}"\n\n[tool.other]\nversion = "9.9.9"\n'
    )
    (root / "schemas" / "src").mkdir(parents=True)
    (root / "schemas" / "src" / "leaf.ts").write_text("export const LEAF = 1;\n")
    (root / "schemas" / "package.json").write_text(
        json.dumps({"name": "@tldrack/schemas", "version": version, "dependencies": {"zod": "^4.0.0"}}, indent=2) + "\n"
    )
    (root / "ui" / "src").mkdir(parents=True)
    (root / "ui" / "src" / "main.ts").write_text("export const UI = 1;\n")
    (root / "ui" / "package.json").write_text(
        json.dumps(
            {
                "name": "@tldrack/ui",
                "version": version,
                "files": ui_files or ["dist", "src"],
                "scripts": {"build": UI_BUILD, "prepack": "exit 1"},
                "dependencies": {"@tldrack/schemas": "file:../schemas", "vue": "^3.5.0"},
                "devDependencies": {"vite": "^8.0.0"},
            },
            indent=2,
        )
        + "\n"
    )


@pytest.fixture
def tree(tmp_path, monkeypatch):
    root = tmp_path / "tree"
    make_tree(root)
    monkeypatch.setattr(pack, "ROOT", root)
    return root


def tarball_manifest(path: Path) -> dict:
    with tarfile.open(path) as tar:
        return json.load(tar.extractfile("package/package.json"))


def tarball_names(path: Path) -> set[str]:
    with tarfile.open(path) as tar:
        return set(tar.getnames())


# --- versions ------------------------------------------------------------------------------------


def test_version_precedence_follows_semver():
    ordered = [
        "0.1.0-alpha",
        "0.1.0-alpha.1",
        "0.1.0-alpha.beta",
        "0.1.0-beta",
        "0.1.0-beta.2",
        "0.1.0-beta.11",
        "0.1.0-rc.1",
        "0.1.0",
        "0.1.1",
        "0.2.0",
        "0.10.0",
        "1.0.0",
    ]
    shuffled = ordered[:]
    random.Random(7).shuffle(shuffled)
    assert sorted(shuffled, key=pack.version_key) == ordered


@pytest.mark.parametrize("text", ["1.0", "v1.0.0", "01.0.0", "1.0.0-", "1.0.0+build", "1.0.0-rc..1", "1.0.0-01"])
def test_malformed_versions_are_refused(text):
    with pytest.raises(pack.PackError):
        pack.parse_version(text)


def test_a_tag_is_the_trees_number_plus_an_optional_prerelease(tree):
    assert pack.base_version("0.1.0-rc.1") == "0.1.0"
    pack.check_release_version("0.1.0")
    pack.check_release_version("0.1.0-rc.1")
    with pytest.raises(pack.PackError, match="the tree is at 0.1.0"):
        pack.check_release_version("0.2.0")


def test_the_pyproject_version_is_the_project_tables():
    text = '[tool.x]\nversion = "9.9.9"\n\n[project]\nname = "x"\nversion = "1.2.3"\n\n[tool.y]\nversion = "8.8.8"\n'
    assert pack.pyproject_version(text) == "1.2.3"
    assert pack.pyproject_version('[project]\nname = "x"\n\n[tool.y]\nversion = "8.8.8"\n') is None


def test_the_tree_holds_one_number(tree):
    assert pack.tree_version() == "0.1.0"
    manifest = json.loads((tree / "ui" / "package.json").read_text())
    (tree / "ui" / "package.json").write_text(json.dumps({**manifest, "version": "0.2.0"}))
    with pytest.raises(pack.PackError, match="ui/package.json 0.2.0"):
        pack.tree_version()
    (tree / "schemas" / "package.json").unlink()
    with pytest.raises(pack.PackError, match="schemas/package.json says nothing"):
        pack.tree_version()


def test_the_tree_never_holds_a_prerelease(tmp_path, monkeypatch):
    make_tree(tmp_path / "t", version="0.1.0-rc.1")
    monkeypatch.setattr(pack, "ROOT", tmp_path / "t")
    with pytest.raises(pack.PackError, match="no prerelease"):
        pack.tree_version()


# --- the stamp -----------------------------------------------------------------------------------


def test_the_stamp_pins_siblings_to_this_release_and_nothing_else():
    manifest = {
        "name": "@tldrack/ui",
        "version": "0.1.0",
        "dependencies": {"@tldrack/schemas": "file:../schemas", "vue": "^3.5.0"},
        "devDependencies": {"@tldrack/schemas": "file:../schemas"},
    }
    stamped = pack.stamp_manifest(manifest, "0.1.0-rc.1")
    assert stamped["version"] == "0.1.0-rc.1"
    assert stamped["dependencies"] == {
        "@tldrack/schemas": "https://github.com/voidcraft-world/tldrack/releases/download/v0.1.0-rc.1/"
        "tldrack-schemas-0.1.0-rc.1.tgz",
        "vue": "^3.5.0",
    }
    assert stamped["devDependencies"] == {"@tldrack/schemas": "file:../schemas"}
    assert manifest["version"] == "0.1.0" and manifest["dependencies"]["@tldrack/schemas"] == "file:../schemas"


# --- packing -------------------------------------------------------------------------------------


def test_ui_without_a_build_script_is_refused(tree):
    manifest = json.loads((tree / "ui" / "package.json").read_text())
    del manifest["scripts"]["build"]
    (tree / "ui" / "package.json").write_text(json.dumps(manifest))
    with pytest.raises(pack.PackError, match="no `build` script"):
        pack.pack(out=tree / "out", skip_install=True)


def test_an_install_needs_a_lockfile(tree):
    with pytest.raises(pack.PackError, match="no package-lock.json"):
        pack.pack(out=tree / "out")


def test_a_misnamed_package_is_refused_before_any_build(tree):
    (tree / "schemas" / "package.json").write_text(json.dumps({"name": "schemas", "version": "0.1.0"}))
    with pytest.raises(pack.PackError, match="named 'schemas'"):
        pack.pack(out=tree / "out", skip_install=True)
    assert not (tree / "ui" / "dist").exists()


@pytest.mark.skipif(NPM is None, reason="npm is not installed")
def test_pack_builds_both_tarballs_and_leaves_the_tree_alone(tree):
    out = tree / "out"
    out.mkdir()
    (out / "tldrack-ui-0.0.9.tgz").write_text("stale")
    before = {p: p.read_bytes() for p in tree.rglob("*") if p.is_file() and out not in p.parents}

    packed = pack.pack("0.1.0-rc.1", out, skip_install=True)

    assert sorted(p.name for p in out.iterdir()) == [
        pack.SUMS_FILE,
        "tldrack-schemas-0.1.0-rc.1.tgz",
        "tldrack-ui-0.1.0-rc.1.tgz",
    ]
    assert [item.path.name for item in packed] == ["tldrack-schemas-0.1.0-rc.1.tgz", "tldrack-ui-0.1.0-rc.1.tgz"]
    ui = tarball_manifest(out / "tldrack-ui-0.1.0-rc.1.tgz")
    assert ui["version"] == "0.1.0-rc.1"
    assert ui["dependencies"]["@tldrack/schemas"] == pack.asset_url("schemas", "0.1.0-rc.1")
    assert "package/dist/index.html" in tarball_names(out / "tldrack-ui-0.1.0-rc.1.tgz")
    assert tarball_manifest(out / "tldrack-schemas-0.1.0-rc.1.tgz")["version"] == "0.1.0-rc.1"
    for item in packed:  # the root's legal files ride in both, and the sums name what was checked
        assert {"package/LICENSE", "package/NOTICE"} <= tarball_names(item.path)
    assert (out / pack.SUMS_FILE).read_text() == "".join(f"{i.sha256}  {i.path.name}\n" for i in packed)
    # The tracked files are as they were; the only new file is the gitignored build output.
    after = {p: p.read_bytes() for p in tree.rglob("*") if p.is_file() and out not in p.parents}
    assert {p for p in after if p not in before} == {tree / "ui" / "dist" / "index.html"}
    assert all(after[p] == data for p, data in before.items())


def test_a_root_without_its_notice_is_refused_before_any_build(tree):
    (tree / "NOTICE").unlink()
    with pytest.raises(pack.PackError, match="the root has no NOTICE"):
        pack.pack(out=tree / "out", skip_install=True)
    assert not (tree / "ui" / "dist").exists()


def test_the_legal_files_join_a_files_allow_list_once():
    assert pack.with_legal_files({"files": ["src", "LICENSE"]})["files"] == ["src", "LICENSE", "NOTICE"]
    assert pack.with_legal_files({"name": "x"}) == {"name": "x"}


@pytest.mark.skipif(NPM is None, reason="npm is not installed")
def test_a_secret_in_the_build_output_refuses_the_release_and_deletes_the_tarballs(tree):
    (tree / "ui" / "planted.txt").write_text("const k = '-----BEGIN RSA PRIVATE " + "KEY-----'\n")
    build = (
        "node -e \"const fs=require('fs');fs.mkdirSync('dist',{recursive:true});fs.copyFileSync('planted.txt','dist/index.js')\""
    )
    manifest = json.loads((tree / "ui" / "package.json").read_text())
    manifest["scripts"]["build"] = build
    (tree / "ui" / "package.json").write_text(json.dumps(manifest))
    with pytest.raises(pack.PackError, match="dist/index.js:1: a private key"):
        pack.pack(out=tree / "out", skip_install=True)
    assert not list((tree / "out").glob("*.tgz")) and not (tree / "out" / pack.SUMS_FILE).exists()


def _tarball(path: Path, members: dict[str, bytes]) -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            tar.addfile(info, __import__("io").BytesIO(data))
    return path


def test_the_archive_check_reads_compressed_members_and_names_forbidden_files(tmp_path, monkeypatch):
    import gzip

    monkeypatch.setenv("TLDRACK_PURITY_WORDS_TEXT", "zanzibar\n")  # an invented operator list
    clean = _tarball(tmp_path / "clean.tgz", {"LICENSE": b"MIT\n", "NOTICE": b"n\n", "src/a.ts": b"x\n"})
    assert check_archive.check(clean) == []
    dirty = _tarball(
        tmp_path / "dirty.tgz",
        {
            "LICENSE": b"MIT\n",
            "vendor/lib.js.gz": gzip.compress(("see /Us" + "ers/jsmith/x").encode()),
            "data/store.sqlite": b"\0rows",
            "notes.md": b"the zanzibar said",
        },
    )
    problems = check_archive.check(dirty)
    assert any(p.startswith("vendor/lib.js.gz:1: a home directory path") and "jsmith" not in p for p in problems)
    assert "data/store.sqlite: a data, key or credential file" in problems
    assert any(p.startswith("notes.md:1: an operator word") and "zanzibar" not in p for p in problems)
    assert "NOTICE: missing at the package root" in problems


@pytest.mark.skipif(NPM is None, reason="npm is not installed")
def test_a_ui_tarball_without_its_build_output_is_refused(tmp_path, monkeypatch):
    make_tree(tmp_path / "t", ui_files=["src"])
    monkeypatch.setattr(pack, "ROOT", tmp_path / "t")
    with pytest.raises(pack.PackError, match="carries nothing under dist/"):
        pack.pack(out=tmp_path / "out", skip_install=True)


# --- release: pure -------------------------------------------------------------------------------


def test_remote_versions_skip_peeled_refs_and_odd_tags():
    listing = (
        "a1\trefs/tags/v0.1.0\nb2\trefs/tags/v0.1.0^{}\nc3\trefs/tags/v0.1.0-rc.1\n"
        "d4\trefs/tags/nightly\ne5\trefs/tags/v0.10.0\nf6\trefs/tags/vX\n"
    )
    assert release.remote_versions(listing) == ["0.1.0-rc.1", "0.1.0", "0.10.0"]


def test_a_consumer_manifest_moves_only_the_released_packages():
    text = (
        '{\n    "name": "host",\n    "dependencies": {\n        "@tldrack/ui": "file:../tldrack/ui",\n'
        '        "three": "^0.187.0"\n    },\n    "devDependencies": {\n        "@tldrack/schemas": "x"\n    }\n}\n'
    )
    out = release.rewrite_manifest(text, "0.2.0")
    assert out.startswith('{\n    "name": "host",')  # indentation and key order kept
    data = json.loads(out)
    assert data["dependencies"] == {"@tldrack/ui": pack.asset_url("ui", "0.2.0"), "three": "^0.187.0"}
    assert data["devDependencies"] == {"@tldrack/schemas": pack.asset_url("schemas", "0.2.0")}


def test_the_lock_check_names_what_is_stale():
    wanted = {"@tldrack/ui": pack.asset_url("ui", "0.2.0")}
    good = {"packages": {"node_modules/@tldrack/ui": {"version": "0.2.0", "resolved": wanted["@tldrack/ui"]}}}
    old = {"packages": {"node_modules/@tldrack/ui": {"version": "0.1.0", "resolved": pack.asset_url("ui", "0.1.0")}}}
    assert release.lock_mismatches(good, wanted, "0.2.0") == []
    assert release.lock_mismatches(old, wanted, "0.2.0") == [f"@tldrack/ui locks 0.1.0 from {pack.asset_url('ui', '0.1.0')}"]
    assert release.lock_mismatches({}, wanted, "0.2.0") == ["@tldrack/ui is not in package-lock.json"]


def test_a_consumer_dry_run_changes_nothing(tmp_path):
    consumer = tmp_path / "host"
    consumer.mkdir()
    text = json.dumps({"name": "host", "dependencies": {"@tldrack/ui": pack.asset_url("ui", "0.1.0")}}, indent=2) + "\n"
    (consumer / "package.json").write_text(text)
    line = release.sync_one(consumer, "0.2.0", dry_run=True)
    assert line.startswith(f"  would move {consumer}") and "then `npm install`" in line
    assert (consumer / "package.json").read_text() == text
    assert not (consumer / "package-lock.json").exists()
    (consumer / "package.json").write_text('{"name": "host", "dependencies": {"vue": "^3"}}')
    with pytest.raises(release.ReleaseError, match="depends on none of"):
        release.sync_one(consumer, "0.2.0", dry_run=True)


# --- release: `tag --dry-run` in a scratch repository ---------------------------------------------


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "commit.gpgsign=false", "-c", "tag.gpgsign=false", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def snapshot(work: Path, origin: Path) -> list[str]:
    """Every ref of both repositories, and the clone's working tree, ignored files included."""
    return [git(work, "for-each-ref"), git(work, "status", "--porcelain", "--ignored"), git(origin, "for-each-ref")]


@pytest.fixture
def scratch(tmp_path, monkeypatch):
    """A clone of an invented tree, `main` pushed to a bare origin; GitHub answered by a stub."""
    origin, work = tmp_path / "origin.git", tmp_path / "work"
    git(tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))
    make_tree(work)
    git(work, "init", "--quiet", "-b", "main")
    git(work, "config", "user.email", NOREPLY)
    git(work, "config", "user.name", "Someone")
    git(work, "add", ".")
    git(work, "commit", "--quiet", "-m", "tree")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "--quiet", "origin", "main")
    git(work, "tag", "-a", "v0.0.9", "-m", "old")  # a release that already went out
    git(work, "push", "--quiet", "origin", "v0.0.9")

    calls: list[tuple[str, ...]] = []

    def fake_gh(*args: str, capture: bool = True) -> str:
        calls.append(args)
        if args[:2] == ("run", "list"):
            return json.dumps([{"databaseId": 1, "status": "completed", "conclusion": "success"}])
        raise AssertionError(f"a dry run asked GitHub for {args}")

    monkeypatch.setattr(pack, "ROOT", work)
    monkeypatch.setattr(release, "PUSH_URLS", (str(origin),))
    monkeypatch.setattr(release, "gh", fake_gh)
    monkeypatch.delenv(release.CONSUMERS_ENV, raising=False)
    return work, origin, calls


def test_tag_dry_run_prints_the_plan_and_changes_nothing(scratch, capsys):
    work, origin, calls = scratch
    before = snapshot(work, origin)

    assert release.main(["tag", "0.1.0-rc.1", "--dry-run"]) == 0

    printed = capsys.readouterr().out
    head = git(work, "rev-parse", "--short=7", "HEAD")
    assert f"Release v0.1.0-rc.1 from {head}" in printed
    assert printed.count("  ✓ ") == 6 and "✗" not in printed
    assert f'git tag -a v0.1.0-rc.1 {head} -m "tldrack 0.1.0-rc.1"' in printed
    assert "git push origin v0.1.0-rc.1" in printed
    assert "prerelease v0.1.0-rc.1 carries tldrack-schemas-0.1.0-rc.1.tgz and tldrack-ui-0.1.0-rc.1.tgz" in printed
    assert "no checks would refuse this tag" in printed
    assert snapshot(work, origin) == before
    assert calls == [
        (
            "run",
            "list",
            "--commit",
            git(work, "rev-parse", "HEAD"),
            "--workflow",
            "ci.yml",
            "--json",
            "databaseId,status,conclusion",
        )
    ]


def test_tag_dry_run_lists_every_refusal_and_still_prints_the_plan(scratch, capsys):
    work, origin, _ = scratch
    (work / "notes.txt").write_text("unsaved\n")
    git(work, "config", "user.email", "someone@example.com")
    before = snapshot(work, origin)

    assert release.main(["tag", "0.0.8", "--dry-run"]) == 1

    printed = capsys.readouterr().out
    assert "✗ git user.email is not a GitHub noreply address" in printed
    assert "✗ the working tree is not clean (1 paths)" in printed
    assert "✗ v0.0.8 is not newer than every release on origin (v0.0.9)" in printed
    assert "✗ version: the tree is at 0.1.0" in printed
    assert "\nPlan\n" in printed and "git push origin v0.0.8" in printed
    assert "4 checks would refuse this tag" in printed
    assert printed.rstrip().endswith("✗ v0.0.8 would be refused today")
    assert snapshot(work, origin) == before


def test_a_push_url_that_is_not_the_public_repository_refuses(scratch, capsys):
    work, _, _ = scratch
    git(work, "remote", "set-url", "--push", "origin", "DISABLED")
    assert release.main(["tag", "0.1.0", "--dry-run"]) == 1
    assert "✗ origin's push URL is 'DISABLED': this checkout cannot publish" in capsys.readouterr().out
