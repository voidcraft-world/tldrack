"""Session-owned output snapshots. Native registration is the only writer.

Sources are opened component-by-component below tmux's starting workspace with
O_NOFOLLOW; neither a traversal nor a swapped symlink can escape that boundary.
The shelf stores copies, so a rename, source deletion or bridge restart loses no output.
Raw HTML is never served on the bridge origin: artifact_page.py owns the preview.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path

from agent_canvas.paths import canvas_dir, ensure_canvas_dir, normalize_key

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_ARTIFACTS = 32
MAX_INDEX_BYTES = 128 * 1024
ARTIFACT_ID = re.compile(r"[a-f0-9]{24}")
REVISION = re.compile(r"[a-f0-9]{64}")
FILE_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".md": "text/plain",
    ".txt": "text/plain",
    ".json": "text/plain",
    ".csv": "text/plain",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".pdf": "application/pdf",
}


def read_bounded(path: Path, limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise ValueError("saved artifact must be a regular file")
        data = source.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"artifact exceeds {limit // 1024} KiB")
    return data


def read_source(workspace: Path, raw: str) -> tuple[Path, bytes]:
    root = workspace.resolve(strict=True)
    path = Path(raw)
    if not path.is_absolute():
        path = root / path
    try:
        relative = path.relative_to(root)
    except ValueError:
        raise ValueError("artifact must be inside this session workspace") from None
    if not relative.parts or any(part.startswith(".") for part in relative.parts):
        raise ValueError("hidden paths and traversal are not artifacts")
    if relative.suffix.lower() not in FILE_TYPES:
        raise ValueError("supported artifacts: HTML, Markdown, text, JSON, CSV, PNG, JPEG, WebP, PDF")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        file_fd = os.open(relative.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(file_fd, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("artifact source must be a regular file")
            data = source.read(MAX_FILE_BYTES + 1)
        if len(data) > MAX_FILE_BYTES:
            raise ValueError("artifact exceeds 8 MiB")
        return relative, data
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(prefix=".artifact-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as target:
            target.write(data)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


class ArtifactStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()

    def directory(self, key: str, *, create: bool = False) -> Path:
        if normalize_key(key) != key:
            raise ValueError("invalid artifact session key")
        root = ensure_canvas_dir() if create else canvas_dir()
        for part in ("artifacts", key):
            root = root / part
            if root.is_symlink():
                raise ValueError("artifact directories cannot be symlinks")
            if create:
                root.mkdir(exist_ok=True)
        return root

    def _index(self, key: str) -> list[dict]:
        try:
            rows = json.loads(read_bounded(self.directory(key) / "index.json", MAX_INDEX_BYTES))
        except FileNotFoundError:
            return []
        if not isinstance(rows, list) or len(rows) > MAX_ARTIFACTS:
            raise ValueError("invalid artifact index")
        for row in rows:
            if (
                not isinstance(row, dict)
                or not ARTIFACT_ID.fullmatch(str(row.get("id", "")))
                or not REVISION.fullmatch(str(row.get("revision", "")))
                or row.get("mime") not in FILE_TYPES.values()
                or not isinstance(row.get("name"), str)
                or not isinstance(row.get("path"), str)
            ):
                raise ValueError("invalid artifact entry")
        return rows

    def list(self, key: str) -> list[dict]:
        with self._lock:
            rows = self._index(key)
            result = []
            for row in rows:
                item = dict(row)
                path = self.directory(key) / f"{row['id']}-{row['revision']}.bin"
                if path.is_symlink() or not path.is_file():
                    item["error"] = "Saved artifact is missing; register the file again."
                result.append(item)
            return result

    def register(self, key: str, workspace: Path, raw: str) -> dict:
        relative, data = read_source(workspace, raw)
        if len(str(relative)) > 2048:
            raise ValueError("artifact path is too long")
        artifact_id = hashlib.sha256(str(relative).encode()).hexdigest()[:24]
        revision = hashlib.sha256(data).hexdigest()
        row = {
            "id": artifact_id,
            "revision": revision,
            "name": relative.name,
            "path": relative.as_posix(),
            "mime": FILE_TYPES[relative.suffix.lower()],
            "bytes": len(data),
            "updated": int(time.time()),
        }
        with self._lock:
            rows = self._index(key)
            previous = next((r for r in rows if r["id"] == artifact_id), None)
            if previous and previous["revision"] == revision:
                try:
                    if self.read(key, artifact_id)[1] == data:
                        return previous
                except (OSError, ValueError):
                    pass  # Re-registering also repairs a lost or damaged snapshot.
            rows = [r for r in rows if r["id"] != artifact_id]
            if len(rows) >= MAX_ARTIFACTS:
                raise ValueError("artifact shelf is full (32 files)")
            directory = self.directory(key, create=True)
            atomic_write(directory / f"{artifact_id}-{revision}.bin", data)
            rows.insert(0, row)
            atomic_write(directory / "index.json", json.dumps(rows).encode())
            if previous and previous["revision"] != revision:
                (directory / f"{artifact_id}-{previous['revision']}.bin").unlink(missing_ok=True)
        return row

    def read(self, key: str, artifact_id: str) -> tuple[dict, bytes]:
        if not ARTIFACT_ID.fullmatch(artifact_id):
            raise ValueError("invalid artifact id")
        with self._lock:
            row = next((r for r in self._index(key) if r["id"] == artifact_id), None)
            if row is None:
                raise FileNotFoundError("artifact not registered for this session")
            data = read_bounded(self.directory(key) / f"{artifact_id}-{row['revision']}.bin", MAX_FILE_BYTES)
            if hashlib.sha256(data).hexdigest() != row["revision"]:
                raise ValueError("saved artifact failed its content check; register it again")
            return row, data
