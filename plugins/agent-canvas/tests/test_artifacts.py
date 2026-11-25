"""Output ownership, snapshots and preview isolation, without a browser or tmux."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agent_canvas.artifacts import ArtifactStore, MAX_FILE_BYTES, MAX_ARTIFACTS, read_bounded
from agent_canvas.artifact_page import build_artifact_page, ARTIFACT_CSP

KEY = "7-1760000000"
OTHER = "8-1760000000"


def test_nonregular_reads_identify_source_or_saved_artifact(canvas_root, tmp_path):
    source = tmp_path / "pipe.txt"
    os.mkfifo(source)
    with pytest.raises(ValueError, match="artifact source must be a regular file"):
        ArtifactStore().register(KEY, tmp_path, str(source))
    with pytest.raises(ValueError, match="saved artifact must be a regular file"):
        read_bounded(source, MAX_FILE_BYTES)


def test_snapshot_survives_source_deletion_and_restart(canvas_root, tmp_path):
    source = tmp_path / "proposal.html"
    source.write_text("<h1>First</h1>")
    store = ArtifactStore()
    row = store.register(KEY, tmp_path, str(source))
    source.unlink()
    assert ArtifactStore().read(KEY, row["id"])[1] == b"<h1>First</h1>"
    assert store.list(OTHER) == []
    with pytest.raises(FileNotFoundError):
        store.read(OTHER, row["id"])


def test_revisions_replace_one_file_and_unchanged_write_is_idempotent(canvas_root, tmp_path):
    source = tmp_path / "out.html"
    source.write_text("one")
    store = ArtifactStore()
    first = store.register(KEY, tmp_path, str(source))
    assert store.register(KEY, tmp_path, str(source)) == first
    source.write_text("two")
    second = store.register(KEY, tmp_path, str(source))
    assert first["id"] == second["id"] and first["revision"] != second["revision"]
    assert store.list(KEY) == [second]
    assert store.read(KEY, first["id"])[1] == b"two"
    assert len(list(store.directory(KEY).glob("*.bin"))) == 1


def test_source_boundary_rejects_traversal_hidden_and_symlinks(canvas_root, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "private.html"
    outside.write_text("private")
    (workspace / "linked.html").symlink_to(outside)
    (workspace / "linked-dir").symlink_to(tmp_path, target_is_directory=True)
    (workspace / ".hidden.html").write_text("secret")
    store = ArtifactStore()
    for path in [str(outside), "../private.html", "linked.html", "linked-dir/private.html", ".hidden.html"]:
        with pytest.raises((ValueError, OSError)):
            store.register(KEY, workspace, path)
    assert store.list(KEY) == []


def test_output_types_and_limits(canvas_root, tmp_path):
    source = tmp_path / "too-big.html"
    with source.open("wb") as stream:
        stream.truncate(MAX_FILE_BYTES + 1)
    store = ArtifactStore()
    with pytest.raises(ValueError, match="8 MiB"):
        store.register(KEY, tmp_path, str(source))
    source = tmp_path / "script.py"
    source.write_text("print(1)")
    with pytest.raises(ValueError, match="supported artifacts"):
        store.register(KEY, tmp_path, str(source))
    for number in range(MAX_ARTIFACTS):
        source = tmp_path / f"{number}.txt"
        source.write_text("output")
        store.register(KEY, tmp_path, str(source))
    source = tmp_path / "overflow.txt"
    source.write_text("output")
    with pytest.raises(ValueError, match="full"):
        store.register(KEY, tmp_path, str(source))


def test_missing_or_modified_snapshot_is_not_served(canvas_root, tmp_path):
    source = tmp_path / "out.html"
    source.write_text("good")
    store = ArtifactStore()
    row = store.register(KEY, tmp_path, str(source))
    saved = store.directory(KEY) / f"{row['id']}-{row['revision']}.bin"
    saved.write_text("tampered")
    with pytest.raises(ValueError, match="content check"):
        store.read(KEY, row["id"])
    saved.unlink()
    assert "error" in store.list(KEY)[0]
    saved.symlink_to(source)
    with pytest.raises(OSError):
        store.read(KEY, row["id"])
    store.register(KEY, tmp_path, str(source))
    assert store.read(KEY, row["id"])[1] == b"good"


def test_store_symlink_and_bad_identifiers_are_refused(canvas_root, tmp_path):
    (canvas_root / "artifacts").symlink_to(tmp_path, target_is_directory=True)
    store = ArtifactStore()
    for key in ["../outside", KEY]:
        with pytest.raises(ValueError):
            store.list(key)
    with pytest.raises(ValueError, match="invalid artifact id"):
        store.read(KEY, "../private")


def test_viewer_never_interpolates_untrusted_markup_or_names():
    evil = "</script><script>parent.evil()</script>"
    page = build_artifact_page({"name": evil, "mime": "text/html"}, evil.encode())
    assert evil not in page
    assert page.count("<script>") == 1
    script = (Path(__file__).parents[1] / "agent_canvas/assets/artifact-viewer.js").read_text()
    assert "setAttribute('sandbox', 'allow-scripts')" in script
    assert "allow-same-origin" not in script
    assert "connect-src 'none'" in ARTIFACT_CSP
    assert "frame-src 'none'" in ARTIFACT_CSP
    assert "form-action 'none'" in ARTIFACT_CSP
    assert "base-uri 'none'" in ARTIFACT_CSP


def test_viewer_saves_only_clicked_images_from_its_own_sandbox():
    viewer = Path(__file__).parents[1] / "agent_canvas/assets/artifact-viewer.js"
    assert "allow-downloads" not in viewer.read_text()
    node = shutil.which("node")
    if not node:
        pytest.skip("needs node on PATH")
    harness = Path(__file__).parent / "fixtures/viewer-save-harness.cjs"
    result = subprocess.run([node, str(harness), str(viewer)], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr.strip()
    played = json.loads(result.stdout)
    batch = [f"batch-{i}.png" for i in range(31)]
    assert played["saved"] == ["My Chart_.png", "sheet-web.webp", "image.jpg", "slow.png", *batch]
    replies = {reply["id"]: reply for reply in played["replies"]}
    assert "stranger" not in replies
    assert [replies[id]["ok"] for id in ("png", "webp", "jpeg")] == [True, True, True]
    assert replies["html"]["error"] == "only PNG, JPEG and WebP images can be saved"
    assert replies["text"]["error"] == "no image bytes were sent"
    assert replies["huge"]["error"] == "the image is larger than 16 MiB"
    for refused in ("no-click", "stranger-click", "past-budget", "past-minute"):
        assert replies[refused]["error"] == "saving needs a click: press the button again"
