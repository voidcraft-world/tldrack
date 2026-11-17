import base64
import json
import shutil
import subprocess

import pytest

from tldrack.index_patch import INJECTED_SCRIPT, patch_index_html


def test_injects_before_last_body_close():
    html = "<html><body><div></body>oops</div><body>x</body></html>"
    patched = patch_index_html(html)
    assert patched is not None
    assert patched.count(INJECTED_SCRIPT) == 1
    # Injected before the LAST </body>, everything after it untouched.
    assert patched.endswith(INJECTED_SCRIPT + "</body></html>")


def test_returns_none_without_body_close():
    assert patch_index_html("<html><body>no closing tag</html>") is None


def test_realistic_page_shape():
    html = "<!doctype html><html><head><title>ttyd</title></head><body><script>app()</script></body></html>"
    patched = patch_index_html(html)
    assert patched is not None
    assert "tldrack patch" in patched
    assert patched.index("app()") < patched.index("tldrack patch")


# ── Copy on select (OSC 52) ──────────────────────────────────────────────
# The script's decode is an ES5 port of ui/src/engine/terminal/terminal-clipboard.ts.
# These vectors mirror ui/tests/unit/terminal-clipboard.test.ts and RUN the
# port under node, so the two cannot drift apart unseen.


def _slice(start_marker: str, end_marker: str) -> str:
    start = INJECTED_SCRIPT.find(start_marker)
    end = INJECTED_SCRIPT.find(end_marker, start)
    assert start != -1 and end != -1, f"anchor missing: {start_marker!r} … {end_marker!r}"
    return INJECTED_SCRIPT[start:end]


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


UTF8_SAMPLE = "╭─ ✂ copied 👾\n│ line two"

OSC52_VECTORS = [
    (f"c;{_b64('git status')}", "git status"),
    (f"c;{_b64(UTF8_SAMPLE)}", UTF8_SAMPLE),
    (f";{_b64('a')}", "a"),
    (f"p;{_b64('b')}", "b"),
    (f"cs0;{_b64('c')}", "c"),
    ("c;?", None),  # a READ request — never answered
    (";?", None),
    ("c;", None),
    ("c", None),
    (f"x;{_b64('a')}", None),
    ("c;not*base64!", None),
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_osc52_decode_port_matches_the_front_end_vectors():
    decode_src = _slice("var OSC52_SELECTION_RE", "function exec_command_copy")
    program = (
        decode_src
        + "\nconst inputs = JSON.parse(process.argv[1]);"
        + "\nprocess.stdout.write(JSON.stringify(inputs.map(decode_osc52_write)));"
    )
    inputs = [data for data, _ in OSC52_VECTORS]
    result = subprocess.run(
        ["node", "-e", program, json.dumps(inputs)],
        capture_output=True,
        text=True,
        timeout=20,
        check=True,
    )
    assert json.loads(result.stdout) == [expected for _, expected in OSC52_VECTORS]


def test_osc52_handler_is_installed_and_never_writes_to_the_pty():
    poll = _slice("var poll = setInterval", "}, 250);")
    assert "install_osc52_copy(term);" in poll
    handler = _slice("function install_osc52_copy", "/* Clipboard images:")
    assert "registerOscHandler(52," in handler
    # A clipboard escape must only ever reach the clipboard — never typed back into the shell.
    for forbidden in ("term.input", "term.paste", "postMessage"):
        assert forbidden not in handler
