"""Capped, link-refusing reads of the operator's Laya files, the two parse caches, and their modes.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".

Every file under the laya store (`paths.laya_dir`) is written by another process (laya-lab) or by this
plugin, and holds the operator's own text. So every read is capped, refuses a link in its last
component, and refuses anything but a regular file; every append goes through `append_line`.

- `StampedCache`: a file parsed once per (st_dev, st_ino, st_size, st_mtime_ns). For files that
  are rewritten whole (labels, scores, reports, golden suites, rotated ledger segments).
- `JsonlTail`: an append-only file (the active ledger segment, the operator log) parses only the
  bytes past the last offset, and re-reads from zero when it shrinks or its inode changes.

These are the only parse caches in the plugin; every source read goes through one of them.
"""

from __future__ import annotations

import errno
import json
import math
import os
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard


OK, MISSING, TOO_LARGE, REFUSED = "ok", "missing", "too_large", "refused"

Stamp = tuple[int, int, int, int]

#: `Number.MAX_SAFE_INTEGER`: Zod's `z.number().int()` accepts no larger integer, and JSON.parse
#: cannot read one exactly.
MAX_SAFE_INTEGER = 2**53 - 1


class SourceError(Exception):
    """A file this module will not read: `code` is `too_large` or `refused` (`missing` from the caches)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def open_regular(path: Path, flags: int) -> int:
    """An fd on a regular file, never through a link, and never blocking on a FIFO.

    O_NONBLOCK is always added: on a regular file it changes nothing, and a FIFO then fails or opens
    at once instead of waiting for a peer — a writer holding a lock must never wait on one.
    FileNotFoundError when absent; SourceError(refused) for a link or anything but a regular file;
    any other OSError (a permission, a full disk, a FIFO with no reader) as itself, so a writer can
    tell "unwritable" apart.
    """
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR):
            raise FileNotFoundError(str(path)) from None
        if exc.errno == errno.ELOOP:
            raise SourceError(REFUSED) from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SourceError(REFUSED)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _read_fd(fd: int, wanted: int) -> bytes:
    chunks: list[bytes] = []
    while wanted > 0 and (chunk := os.read(fd, wanted)):
        chunks.append(chunk)
        wanted -= len(chunk)
    return b"".join(chunks)


def read_capped(path: Path, cap: int) -> bytes | None:
    """The whole file, or None when it is absent; SourceError(too_large | refused) otherwise."""
    try:
        fd = open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError:
        raise SourceError(REFUSED) from None
    try:
        data = _read_fd(fd, cap + 1)
    except OSError:
        raise SourceError(REFUSED) from None
    finally:
        os.close(fd)
    if len(data) > cap:
        raise SourceError(TOO_LARGE)
    return data


def _refuse_constant(token: str) -> object:
    raise ValueError(f"{token} is not JSON")


def _finite_float(token: str) -> float:
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("an infinite float is not JSON")
    return value


def loads(text: str | bytes) -> Any:
    """`json.loads` as JSON.parse reads it: NaN, ±Infinity and overflowing floats are refused.

    The bridge refuses to SEND them (`allow_nan=False` in dispatch_plugin), so a value read here can
    always go out in a response.
    """
    if isinstance(text, (bytes, bytearray)):
        text = bytes(text).decode("utf-8")
    return json.loads(text, parse_constant=_refuse_constant, parse_float=_finite_float)


def is_number(value: object) -> TypeGuard[float]:
    """A finite JSON number. A bool is never a number."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def is_p(value: object) -> TypeGuard[float]:
    return is_number(value) and 0 <= value <= 1


def is_count(value: object) -> TypeGuard[int]:
    """A whole number ≥ 0 that JavaScript reads exactly (≤ `MAX_SAFE_INTEGER`). A bool is never one."""
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SAFE_INTEGER


# ── lengths as JavaScript counts them ─────────────────────────────────────────
# Zod's `.max(n)` on a string reads `.length`: UTF-16 code units, so an emoji counts 2. Every cap
# a schema checks is applied with these, never with `len()` (code points).


def utf16_len(text: str) -> int:
    """`text.length` in JavaScript. A lone surrogate counts 1, as it does there."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _units(ch: str) -> int:
    return 2 if ord(ch) > 0xFFFF else 1


def utf16_tail(text: str, limit: int) -> str:
    """The longest suffix of `text` within `limit` UTF-16 units, cut on a code-point boundary."""
    if limit <= 0:
        return ""
    tail = text[-limit:]
    excess, start = utf16_len(tail) - limit, 0
    while excess > 0:
        excess -= _units(tail[start])
        start += 1
    return tail[start:]


def utf16_head(text: str, limit: int) -> str:
    """The longest prefix of `text` within `limit` UTF-16 units, cut on a code-point boundary."""
    if limit <= 0:
        return ""
    head = text[:limit]
    excess, end = utf16_len(head) - limit, len(head)
    while excess > 0:
        end -= 1
        excess -= _units(head[end])
    return head[:end]


def clip(text: str, limit: int) -> str:
    """`text`, or its head and `…`, within `limit` UTF-16 units: what a `.max(limit)` accepts."""
    return text if utf16_len(text) <= limit else utf16_head(text, limit - 1) + "…"


def parse_jsonl(data: bytes) -> tuple[list[dict], int]:
    """(rows, bad_lines). `#` lines are comments; a trailing line without `\\n` is a torn write and skipped."""
    rows: list[dict] = []
    bad = 0
    complete = data[: data.rfind(b"\n") + 1]
    for raw in complete.split(b"\n"):
        line = raw.strip()
        if not line or line.startswith(b"#"):
            continue
        try:
            row = loads(line)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if isinstance(row, dict):
            rows.append(row)
        else:
            bad += 1
    return rows, bad


def read_jsonl_capped(path: Path, cap: int) -> tuple[list[dict], int]:
    """(rows, bad_lines) of one JSONL file; an absent file is ([], 0). SourceError(too_large | refused)."""
    data = read_capped(path, cap)
    return ([], 0) if data is None else parse_jsonl(data)


def stamp_of(path: Path) -> Stamp | None:
    """The identity a cache keys on, or None when absent. Never follows a link (lstat)."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


class StampedCache:
    """Parse each file once per stamp. `get` → (code, value); value is None unless code is `ok`.

    A file found gone drops its entry; a writer that deletes a file it knows was cached (a pruned
    ledger segment, which no reader asks for again) calls `forget`.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, int, Callable[[bytes], Any]], tuple[Stamp, Any]] = {}

    def forget(self, path: Path) -> None:
        name = str(path)
        with self._lock:
            for key in [key for key in self._entries if key[0] == name]:
                del self._entries[key]

    def get(self, path: Path, cap: int, parse: Callable[[bytes], Any]) -> tuple[str, Any]:
        key = (str(path), cap, parse)
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            with self._lock:
                self._entries.pop(key, None)
            return MISSING, None
        except OSError:
            return REFUSED, None
        if not stat.S_ISREG(st.st_mode):
            return REFUSED, None
        if st.st_size > cap:
            return TOO_LARGE, None
        stamp = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        with self._lock:
            hit = self._entries.get(key)
            if hit is not None and hit[0] == stamp:
                return OK, hit[1]
        try:
            data = read_capped(path, cap)
        except SourceError as exc:
            return exc.code, None
        if data is None:
            return MISSING, None
        value = parse(data)
        with self._lock:
            self._entries[key] = (stamp, value)
        return OK, value


class JsonlTail:
    """One append-only JSONL file, parsed incrementally. `read` → (code, rows, bad_lines).

    The returned list is a snapshot copy: callers may keep it. `parsed_lines` counts every line ever
    parsed, so a test can prove an append re-parses only what was appended.
    """

    def __init__(self, path: Path, cap: int) -> None:
        self.path, self.cap = path, cap
        self._lock = threading.Lock()
        self._inode: tuple[int, int] | None = None
        self._offset = 0
        self._rows: list[dict] = []
        self._bad = 0
        self.parsed_lines = 0

    def _reset(self) -> None:
        self._inode, self._offset, self._rows, self._bad = None, 0, [], 0

    def read(self) -> tuple[str, list[dict], int]:
        with self._lock:
            try:
                fd = open_regular(self.path, os.O_RDONLY)
            except FileNotFoundError:
                self._reset()
                return MISSING, [], 0
            except (SourceError, OSError):
                self._reset()
                return REFUSED, [], 0
            try:
                st = os.fstat(fd)
                if st.st_size > self.cap:
                    self._reset()
                    return TOO_LARGE, [], 0
                inode = (st.st_dev, st.st_ino)
                if inode != self._inode or st.st_size < self._offset:
                    self._reset()
                    self._inode = inode
                if st.st_size > self._offset:
                    os.lseek(fd, self._offset, os.SEEK_SET)
                    fresh = _read_fd(fd, st.st_size - self._offset)
                    complete = fresh[: fresh.rfind(b"\n") + 1]
                    if complete:
                        rows, bad = parse_jsonl(complete)
                        self.parsed_lines += complete.count(b"\n")
                        self._rows.extend(rows)
                        self._bad += bad
                        self._offset += len(complete)
            except OSError:
                self._reset()
                return REFUSED, [], 0
            finally:
                os.close(fd)
            return OK, list(self._rows), self._bad


_TAILS: dict[str, JsonlTail] = {}
_TAILS_LOCK = threading.Lock()


def shared_tail(path: Path, cap: int) -> JsonlTail:
    """The one incremental reader per append-only file, shared by every caller in this process."""
    with _TAILS_LOCK:
        reader = _TAILS.get(str(path))
        if reader is None or reader.cap != cap:
            reader = _TAILS[str(path)] = JsonlTail(path, cap)
        return reader


def append_line(path: Path, line: bytes, *, fsync: bool = False) -> bool:
    """Append one whole line to a 0600 regular file, never through a link. Returns True when it created it.

    O_APPEND makes each write land at the end; the loop finishes a short write, so a line is never
    interleaved with another writer's under the caller's lock. OSError and SourceError(refused) propagate.
    """
    existed = os.path.lexists(path)
    fd = open_regular(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) & 0o077:
            os.fchmod(fd, 0o600)
        view = memoryview(line)
        while view:
            written = os.write(fd, view)
            view = view[written:]
        if fsync:
            os.fsync(fd)
    finally:
        os.close(fd)
    created = not existed
    if created and fsync:
        try:
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            return created
        try:
            os.fsync(dir_fd)
        except OSError:
            pass
        finally:
            os.close(dir_fd)
    return created


def is_real_dir(path: Path | None) -> bool:
    """An existing directory that is not itself a link."""
    if path is None:
        return False
    try:
        return stat.S_ISDIR(os.lstat(path).st_mode)
    except OSError:
        return False


def _tighten(path: Path, want: int, base: Path, changed: list[str]) -> None:
    try:
        st = os.lstat(path)
        if stat.S_ISLNK(st.st_mode):
            return
        is_dir = stat.S_ISDIR(st.st_mode)
        if (want == 0o700) != is_dir or not (is_dir or stat.S_ISREG(st.st_mode)):
            return
        if stat.S_IMODE(st.st_mode) & 0o077:
            # lchmod where the OS has it, so a link swapped in after the lstat is never followed.
            os.chmod(path, want, follow_symlinks=os.chmod not in os.supports_follow_symlinks)
            changed.append(path.relative_to(base).as_posix())
    except (OSError, ValueError):
        return


def _entries(path: Path) -> list[Path]:
    try:
        if not is_real_dir(path):
            return []
        return sorted(path.iterdir())
    except OSError:
        return []


def tighten_params(base: Path) -> list[str]:
    """0700 on `base`, its subfolders and `checkpoints/*`; 0600 on the files of the operator's folders.

    Only a mode WIDER than wanted is changed; links are skipped (lstat), never followed; one failing
    path never stops the rest, and this never raises. Returns the paths it changed, relative to `base`.
    """
    from laya_bridge import paths  # paths imports this module

    private_dirs = (
        paths.DATA_DIRNAME,
        paths.EVALS_DIRNAME,
        paths.GOLDEN_DIRNAME,
        paths.LEDGER_DIRNAME,
        paths.LANDING_DIRNAME,
        paths.STABILITY_DIRNAME,
        paths.RETRAIN_DIRNAME,
    )
    changed: list[str] = []
    if not is_real_dir(base):
        return changed
    _tighten(base, 0o700, base, changed)
    for child in _entries(base):
        if not is_real_dir(child):
            continue
        _tighten(child, 0o700, base, changed)
        if child.name in private_dirs:
            for item in _entries(child):
                _tighten(item, 0o600, base, changed)
        elif child.name == paths.CHECKPOINTS_DIRNAME:
            for checkpoint in _entries(child):
                if not is_real_dir(checkpoint):
                    continue
                _tighten(checkpoint, 0o700, base, changed)
                if checkpoint.name.endswith(".data"):
                    for item in _entries(checkpoint):
                        _tighten(item, 0o600, base, changed)
    return changed
