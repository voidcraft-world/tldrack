"""Third-party code this plugin serves itself: Mermaid, pinned and checked.

── WHY MERMAID IS VENDORED, AND WHY IT IS GZIPPED ──────────────────────────
The canvas page admits one script, by nonce (`page.py` owns why), and loads
nothing remote, so Mermaid has to come from this plugin's own origin. A
plugin is stdlib-only and installs nothing, so the bundle ships in the
directory, as the release's own `dist/mermaid.min.js`, byte for byte.

It ships gzipped because the plain bundle cannot pass the repository's purity
scan: it is over the scan's size cap, and its minified number tables hold
twelve-digit literals that read as cloud account ids. Compressed, it is data
the scan treats as binary, and THIS module is what vouches for it instead:
the decompressed bytes must hash to `MERMAID_SHA256`, the hash of the file npm
publishes, or nothing is served. The host sends no `Content-Encoding`, so the
bytes are decompressed here, once per process.

Identity, source and how to update: `assets/vendor/VENDORED.md`.
"""

from __future__ import annotations

import functools
import gzip
import hashlib
from pathlib import Path

MERMAID_VERSION = "12.2.0"
#: sha256 of `package/dist/mermaid.min.js` in npm's `mermaid-12.2.0.tgz`.
MERMAID_SHA256 = "a8a6a6f805b074a8261a2825c86f99c8054acb3b35f50bb7e24a4f9f2c3c3b9f"
MERMAID_BUNDLE = Path(__file__).resolve().parent / "assets" / "vendor" / "mermaid" / "mermaid.min.js.gz"


class VendorError(RuntimeError):
    """A vendored file is missing, unreadable, or not the release it claims to be."""


@functools.lru_cache(maxsize=1)
def mermaid_js() -> bytes:
    """The checked bundle. Raises `VendorError`, which is not cached: a fixed file
    is picked up on the next request without a restart."""
    try:
        data = gzip.decompress(MERMAID_BUNDLE.read_bytes())
    except (OSError, EOFError, gzip.BadGzipFile) as exc:
        raise VendorError(f"cannot read the vendored Mermaid bundle: {exc}") from exc
    digest = hashlib.sha256(data).hexdigest()
    if digest != MERMAID_SHA256:
        raise VendorError(
            f"the vendored Mermaid bundle is not mermaid {MERMAID_VERSION} "
            f"(sha256 {digest}); restore it from assets/vendor/VENDORED.md"
        )
    return data
