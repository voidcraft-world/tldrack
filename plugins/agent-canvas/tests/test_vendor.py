"""The vendored Mermaid: the bytes are the release, the record agrees, and the page loads it from here."""

from __future__ import annotations

import gzip
import hashlib
import re
from pathlib import Path

from agent_canvas import vendor
from agent_canvas.page import MERMAID_URL, build_page
from agent_canvas.plugin import AgentCanvasPlugin

VENDOR_DIR = Path(vendor.__file__).resolve().parent / "assets" / "vendor"


class Context:
    status_port = 7682

    def log(self, message: str) -> None:
        pass


def test_the_bundle_is_the_pinned_release():
    data = gzip.decompress(vendor.MERMAID_BUNDLE.read_bytes())
    assert hashlib.sha256(data).hexdigest() == vendor.MERMAID_SHA256
    # The IIFE build: it defines the global the kit calls, and imports nothing.
    assert data.rstrip().endswith(b'globalThis["mermaid"] = globalThis.__esbuild_esm_mermaid_nm["mermaid"].default;')
    assert not re.search(rb"\bimport\s*\(\s*[\"'`]", data[:4096])


def test_the_vendoring_record_and_the_license_agree_with_the_code():
    record = (VENDOR_DIR / "VENDORED.md").read_text(encoding="utf-8")
    assert f"## Mermaid {vendor.MERMAID_VERSION}" in record
    assert f"mermaid-{vendor.MERMAID_VERSION}.tgz" in record
    assert f"`{vendor.MERMAID_SHA256}`" in record
    assert "MIT License" in (VENDOR_DIR / "mermaid" / "LICENSE").read_text(encoding="utf-8")


def test_the_page_points_the_kit_at_the_vendored_route():
    page = build_page("vc-1", "n")
    assert f"/plugin/agent-canvas/mermaid.js?v={vendor.MERMAID_VERSION}" == MERMAID_URL
    assert f'data-mermaid="{MERMAID_URL}"' in page
    # Read once at startup, never after agent markup lands (kit.js says why).
    assert "script.src = MERMAID_URL;" in page and "script.src = document.body" not in page
    assert page.index("const MERMAID_URL") < page.index("function load_mermaid")
    assert "node_modules" not in page and "cdn" not in page.lower()


def test_the_route_serves_the_checked_bundle_as_javascript():
    plugin = AgentCanvasPlugin(Context(), desk=object(), start_watcher=False)
    assert "/plugin/agent-canvas/mermaid.js" in plugin.routes()
    status, data, ctype = plugin.handle_get("/mermaid.js", {"v": [vendor.MERMAID_VERSION]})
    assert status == 200 and ctype == "application/javascript; charset=utf-8"
    assert hashlib.sha256(data).hexdigest() == vendor.MERMAID_SHA256
