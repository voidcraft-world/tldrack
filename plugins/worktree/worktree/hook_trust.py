"""Whether a repository's `.tldrack/bootstrap.sh` may run: the operator's pin.

── Why a pin ───────────────────────────────────────────────────────────────
The hook is code the REPOSITORY supplies, and it would run on every worktree
create and sync, from the rack's button, from `wt`, and from Claude Code's
`WorktreeCreate` hook. git never runs a hook that a clone brings with it, and
neither does this: a hook runs only when the sha256 of its bytes equals the one
the operator pinned for that repository, after reading it (`wt trust <sha256>`,
or the rack's `POST /trust-hook`, which sends the digest it showed). A hook that
changes stops running until it is pinned again; an unpinned one is a note on the
row, never a refused create.

The bytes that were hashed are the bytes that run: `create.run_bootstrap` copies
them to a private temp file first, so a write between the check and the run
changes nothing.

The pins are one file in the state directory, keyed by the main checkout's
resolved path, shared by every door.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from worktree.row_store import load_store_file, persist_atomically
from worktree.store import resolve_store_path

TRUST_FILE_NAME = "worktree-trusted-hooks.json"
TRUST_FILE_VERSION = 1
#: A bootstrap hook is a short script; past this it is not read, so not run.
HOOK_MAX_BYTES = 256 * 1024
#: Pins kept; past this the file is a mistake, and the oldest go.
PINS_MAX = 256

HEX = frozenset("0123456789abcdef")


def trust_path() -> Path:
    """Beside the unkeyed PR store, so a test that relocates that relocates this."""
    return resolve_store_path().parent / TRUST_FILE_NAME


def digest_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_digest(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and set(value) <= HEX


def read_hook(script: Path) -> bytes | None:
    """The hook's bytes, or None when it is gone, unreadable or too big to be one."""
    try:
        if not script.is_file() or script.stat().st_size > HOOK_MAX_BYTES:
            return None
        return script.read_bytes()
    except OSError:
        return None


#: Bidirectional controls: they reorder what a terminal draws, so a review could
#: read a line other than the one that runs.
_BIDI = frozenset(chr(c) for c in (*range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C))


def printable(data: bytes) -> tuple[str, int]:
    """The hook as text a person can review, and how many characters had to be shown
    escaped. Every control character but newline and tab (ESC, CR, backspace, C1)
    and every bidi control becomes a visible `\\xNN` / `\\uNNNN`: a hook must not
    be able to draw something other than what it is on the reviewer's terminal."""
    out: list[str] = []
    escaped = 0
    for char in data.decode("utf-8", "backslashreplace"):
        code = ord(char)
        if char in "\n\t" or (code >= 0x20 and code != 0x7F and not 0x80 <= code <= 0x9F and char not in _BIDI):
            out.append(char)
            continue
        escaped += 1
        out.append(f"\\x{code:02x}" if code < 0x100 else f"\\u{code:04x}")
    return "".join(out), escaped


def _key(main_checkout: Path) -> str:
    return str(Path(main_checkout).expanduser().resolve())


def _parse(text: str) -> dict[str, str]:
    data = json.loads(text)
    if not isinstance(data, dict) or data.get("version") != TRUST_FILE_VERSION:
        raise ValueError("not a hook trust file")
    pins = data.get("pins")
    return {k: v for k, v in pins.items() if isinstance(k, str) and is_digest(v)} if isinstance(pins, dict) else {}


def pins() -> dict[str, str]:
    return load_store_file(trust_path(), _parse, {})


def pinned(main_checkout: Path) -> str | None:
    return pins().get(_key(main_checkout))


def status(main_checkout: Path, script: Path | None) -> dict:
    """`{sha256, trusted}` for the `/repos` row; both None-ish when there is no hook."""
    data = read_hook(script) if script is not None else None
    if data is None:
        return {"sha256": None, "trusted": False}
    digest = digest_of(data)
    return {"sha256": digest, "trusted": pinned(main_checkout) == digest}


def pin(main_checkout: Path, script: Path | None, sha256: object) -> str | None:
    """Trust the hook whose bytes hash to `sha256` → None, or why not. The digest
    must be the one the operator was shown: a hook that changed since is refused."""
    if not is_digest(sha256):
        return "sha256 is 64 lowercase hex characters"
    data = read_hook(script) if script is not None else None
    if data is None:
        return "this repository has no bootstrap hook"
    current = digest_of(data)
    if current != sha256:
        return f"the hook changed since it was shown: its sha256 is now {current}"
    table = pins()
    table.pop(_key(main_checkout), None)
    table[_key(main_checkout)] = current
    kept = dict(list(table.items())[-PINS_MAX:])
    persist_atomically(
        trust_path(),
        json.dumps({"version": TRUST_FILE_VERSION, "pins": kept}, indent=1),
        kept="the pin, until the bridge restarts",
    )
    return None


def verified_hook(main_checkout: Path, script: Path) -> tuple[bytes | None, str | None]:
    """`(bytes to run, None)`, or `(None, the note that says why it did not run)`."""
    data = read_hook(script)
    if data is None:
        return None, "bootstrap hook missing or unreadable"
    digest = digest_of(data)
    trusted = pinned(main_checkout)
    if trusted == digest:
        return data, None
    why = "changed since it was trusted" if trusted else "not trusted yet"
    return None, (f"bootstrap hook not run: .tldrack/bootstrap.sh is {why}. Read it, then: wt trust {digest}")
