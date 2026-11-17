"""The link's one secret: minted once on this Mac, kept 0600, used only as an HMAC key.

Where the token goes and why it never travels any other way: owned by plugins/phone-link/README.md —
see the note there. This module is the only reader and writer of the file. Four rules hold here:

- **Minted once.** A fresh mint is linked into place with `os.link`, which fails when another writer got there
  first, so two processes starting together agree on one token instead of each overwriting the other.
  Only a file that is not a token at all (empty, truncated, hand-edited) is replaced.
- **Never widened.** The folder is 0700 and the file 0600; a looser mode found on load is tightened, and a
  symlink is refused rather than followed.
- **Never shown.** No repr, no error string and no return value outside `value()` carries the token.
  `value()` exists for the keeper's push to a phone, which sends it on adb's stdin.
- **A key, never a password.** On the wire the token is only ever an HMAC key: `sign` and `verify` take the
  messages proof.py builds, so nothing a caller sends or receives is the token itself.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import tempfile
import threading
from pathlib import Path

from phone_link.paths import TOKEN_FILE

#: `secrets.token_urlsafe(32)`: 32 random bytes, 43 URL-safe base64 characters.
TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
TOKEN_BYTES = 32
MAX_FILE_BYTES = 256


class TokenStore:
    def __init__(self, directory: Path) -> None:
        self._dir = Path(directory)
        self._lock = threading.Lock()
        self._token: str | None = None
        self.error: str | None = None
        self.minted = False

    def __repr__(self) -> str:
        return f"TokenStore({str(self._dir)!r})"

    @property
    def path(self) -> Path:
        return self._dir / TOKEN_FILE

    def value(self) -> str | None:
        """The token, loading or minting it on first use. None while the folder cannot be written."""
        with self._lock:
            if self._token is None:
                self._load_locked()
            return self._token

    def sign(self, message: bytes) -> str | None:
        """HMAC-SHA256 of `message` keyed by the token, as lowercase hex. None while there is no token."""
        token = self.value()
        if token is None:
            return None
        return hmac.new(token.encode("ascii"), message, hashlib.sha256).hexdigest()

    def verify(self, message: bytes, presented: str | None) -> bool:
        """Whether `presented` is `sign(message)`, compared in constant time."""
        expected = self.sign(message)
        if expected is None or presented is None:
            return False
        return hmac.compare_digest(presented.encode("ascii", "replace"), expected.encode("ascii"))

    # -- the file ----------------------------------------------------------------------------------
    def _load_locked(self) -> None:
        try:
            self._dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.chmod(self._dir, 0o700)
            token = _read(self.path)
            if token is None:
                self._mint(replace=self.path.exists())
                token = _read(self.path)
            if token is None:
                raise OSError("the token file did not read back")
        except OSError as exc:
            self.error = f"{type(exc).__name__}: {exc.strerror or exc}"[:160]
            return
        self._token = token
        self.error = None

    def _mint(self, *, replace: bool) -> None:
        token = secrets.token_urlsafe(TOKEN_BYTES)
        fd, tmp = tempfile.mkstemp(dir=self._dir, prefix=".token-")  # mkstemp creates it 0600
        try:
            os.write(fd, token.encode("ascii") + b"\n")
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            if replace:
                os.replace(tmp, self.path)
            else:
                try:
                    os.link(tmp, self.path)
                except FileExistsError:
                    pass  # another process minted first: its token is the token
        finally:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
        self.minted = True


def _read(path: Path) -> str | None:
    """The token in `path`, or None when there is no file or it holds no token. A symlink raises."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"{path.name} is not a regular file")
        if info.st_mode & 0o077:
            os.fchmod(fd, 0o600)
        text = os.read(fd, MAX_FILE_BYTES).decode("ascii", "replace").strip()
    finally:
        os.close(fd)
    return text if TOKEN_RE.fullmatch(text) else None
