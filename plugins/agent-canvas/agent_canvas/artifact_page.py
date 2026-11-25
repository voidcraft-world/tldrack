"""A trusted viewer around an opaque-origin sandbox, never raw output on the bridge.

Only base64 payload bytes enter the wrapper. HTML gets a network-denying CSP
before its first byte and runs without same-origin, forms, popups or top navigation.
The viewer itself makes no requests. Download is an explicit user gesture.

Output HTML cannot download: its sandbox has no `allow-downloads`, so a page's own export button
saves nothing. A page that made a picture hands it up instead:

    parent.postMessage({type: "artifact-save", id, name, bytes: ArrayBuffer}, "*", [bytes])

and the viewer answers `{type: "artifact-save-result", id, ok, name | error}`. The viewer saves it
only when the message came from its own sandbox, the bytes are a PNG, JPEG or WebP by their first
bytes and at most 16 MiB, and a click stands behind it. The file name is rebuilt from safe
characters and the extension from the bytes, and the saved blob is untyped like the Download
link's. So a page still cannot save a script, a document or a file nobody clicked for. Adding
`allow-downloads` would allow all three.

A click is the browser's own report (`navigator.userActivation`; a click inside the sandbox counts),
and it lasts about five seconds. Chrome can take longer than that to encode one large PNG, and a
"save every figure" button sends many files. So the viewer holds a click it has seen for one minute
and 32 saves, and a page's button sends `{type: "artifact-save-click"}` in its click handler, before
it starts encoding, for the viewer to see the click while it is live.
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
