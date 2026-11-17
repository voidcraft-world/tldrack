"""A trusted viewer around an opaque-origin sandbox, never raw output on the bridge.

Only base64 payload bytes enter the wrapper. HTML gets a network-denying CSP
before its first byte and runs without same-origin, forms, popups or top navigation.
The viewer itself makes no requests. Download is an explicit user gesture.
"""

from __future__ import annotations

import base64
import html
import json
from pathlib import Path

ARTIFACT_CSP = (
    "default-src 'none'; script-src 'unsafe-inline' blob:; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; connect-src 'none'; frame-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)
VIEWER_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; frame-src blob: data: 'self'; connect-src 'none'; "
    "form-action 'none'; base-uri 'none'"
)


def build_artifact_page(row: dict, data: bytes) -> str:
    payload = base64.b64encode(data).decode("ascii")
    # Metadata is encoded too: no filename can close the wrapper's script element.
    metadata = base64.b64encode(json.dumps({"name": row["name"], "mime": row["mime"]}).encode()).decode()
    script = (Path(__file__).parent / "assets" / "artifact-viewer.js").read_text()
    return f'''<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{html.escape(VIEWER_CSP, quote=True)}">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Artifact preview</title>
<style>html,body{{height:100%;margin:0;background:#2d3035;color:#edf0f3;font:13px system-ui}}
body{{display:flex;flex-direction:column}}header{{display:flex;gap:12px;align-items:center;padding:10px 14px;border-bottom:1px solid #4b5057}}
#name{{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1}}a{{color:#a5ddc9}}small{{color:#b5bdc6}}
main{{flex:1;min-height:0;overflow:auto}}iframe{{width:100%;height:100%;border:0;background:#fff}}
pre{{padding:16px;white-space:pre-wrap;overflow-wrap:anywhere}}img{{max-width:100%;height:auto;display:block;margin:auto}}
p{{padding:16px}}</style></head><body data-payload="{payload}" data-metadata="{metadata}"
 data-policy="{html.escape(ARTIFACT_CSP, quote=True)}">
<header><span id="name"></span><a id="download">Download</a></header>
<small style="padding:6px 14px">Saved copy · self-contained preview · external resources blocked</small>
<main id="preview"></main><script>{script}</script></body></html>'''
