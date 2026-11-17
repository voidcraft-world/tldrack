"""The raw archive: a lossless, compressed copy of every conversation file, kept after the source
is reaped. Why: `ledger/__init__.py` (Claude deletes its transcripts after 30 days).

── APPEND-ONLY FRAMES ──────────────────────────────────────────────────────────
A transcript only ever grows, so its archive does too: each ingest compresses the bytes past
`archived_bytes`, up to the last newline (a half-written line waits for the next run), and APPENDS
them as one more compressed frame. zstd and gzip both decode concatenated frames as one stream
(`read_archive`), so a 200 MB conversation is compressed once, in pieces, never again whole.

A file that SHRANK, or whose first 4 KiB changed, was rewritten; its archive so far is kept as a
numbered generation (`<name>.g1.zst`) and a new one starts. Nothing in the archive is ever deleted.

Sidecars (`*.meta.json`, workflow journals, `tool-results/*`) are small and rewritten in place by
their writer, so each change replaces their archive copy whole, atomically.

Codec: zstd from the 3.14 stdlib (`compression.zstd`) where present, else gzip. The extension says
which, and `read_archive` follows the extension, so a ledger moved between interpreters still reads.
"""

from __future__ import annotations

import gzip
import hashlib
import os
from pathlib import Path

try:
    from compression import zstd as _zstd  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover — interpreters before 3.14
    _zstd = None

CODEC = "zst" if _zstd is not None else "gz"
ZSTD_LEVEL = 9
GZIP_LEVEL = 6
HEAD_BYTES = 4096
#: One frame holds at most this much source; a bigger backlog becomes several frames, so a first
#: backfill never holds a whole 200 MB file in memory twice.
FRAME_MAX = 32 * 1024 * 1024


def _require_zstd():
    if _zstd is None:
        raise RuntimeError("zstd is not available in this interpreter")
    return _zstd


def compress(data: bytes, codec: str = CODEC) -> bytes:
    if codec == "zst":
        return _require_zstd().compress(data, level=ZSTD_LEVEL)
    return gzip.compress(data, compresslevel=GZIP_LEVEL, mtime=0)


def decompress(data: bytes, codec: str) -> bytes:
    if codec == "zst":
        return _require_zstd().decompress(data)
    return gzip.decompress(data)


def codec_of(path: Path) -> str:
    return "zst" if path.suffix == ".zst" else "gz"


def read_archive(path: Path) -> bytes:
    """The archived bytes, every frame joined — byte-identical to the source prefix it covers."""
    return decompress(path.read_bytes(), codec_of(path))


def head_hash(path: Path, size: int | None = None) -> str:
    with path.open("rb") as handle:
        data = handle.read(HEAD_BYTES if size is None else min(HEAD_BYTES, size))
    return hashlib.sha256(data).hexdigest()[:32]


def archive_path_for(raw_root: Path, source: str, profile: str, rel: str) -> Path:
    return raw_root / source / profile / f"{rel}.{CODEC}"


def generation_path(path: Path, generation: int) -> Path:
    suffix = f".{codec_of(path)}"
    return path.with_name(path.name[: -len(suffix)] + f".g{generation}{suffix}")


def _private_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _open_append(path: Path):
    _private_parent(path)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return os.fdopen(fd, "ab")


def append_lines(source: Path, dest: Path, start: int, size: int) -> int:
    """Append source[start:size], cut back to the last newline, as frames. Returns bytes consumed."""
    if size <= start:
        return 0
    consumed = 0
    with source.open("rb") as handle, _open_append(dest) as out:
        handle.seek(start)
        remaining = size - start
        while remaining > 0:
            chunk = handle.read(min(FRAME_MAX, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            cut = chunk.rfind(b"\n")
            if cut < 0:
                break
            whole = chunk[: cut + 1]
            out.write(compress(whole))
            consumed += len(whole)
            if cut + 1 < len(chunk):
                break  # the tail of a half-written line: the next run starts there
    return consumed


def rotate_generation(dest: Path, generation: int) -> None:
    """Keep the archive of a file that was rewritten as generation `generation`."""
    if dest.exists():
        dest.replace(generation_path(dest, generation))


def replace_whole(source: Path, dest: Path) -> int:
    """Archive a sidecar whole, atomically. Returns its size."""
    data = source.read_bytes()
    _private_parent(dest)
    tmp = dest.with_name(dest.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        out.write(compress(data))
    tmp.replace(dest)
    return len(data)
