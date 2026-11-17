"""Routes, the page's CSP, the same-origin narrowing, and path containment."""

from __future__ import annotations

import re

import pytest
from conftest import IDLE_PANE, write_canvas, write_doc

from agent_canvas.desk import CanvasDesk
from agent_canvas.page import build_page, content_security_policy, framer_origins
from agent_canvas.paths import canvas_path, normalize_key, read_fragment
from agent_canvas.plugin import AgentCanvasPlugin, same_origin

KEY = "7-1760000000"


class FakeContext:
    def __init__(self, start_dir, allowed_origins=None):
        self.start_dir = str(start_dir)
        self.port = 7681
        self.status_port = 7682
        self.default_session = "main"
        if allowed_origins is not None:
            self.allowed_origins = allowed_origins
        self.logged: list[str] = []

    def log(self, message: str) -> None:
        self.logged.append(message)


@pytest.fixture()
def plugin(tmp_path, canvas_root, tmux, host):
    tmux.keys["vc-1"] = KEY
    tmux.panes["vc-1"] = IDLE_PANE
    host.rows["vc-1"] = {"state": "free"}
    desk = CanvasDesk(host, tmux=tmux)
    return AgentCanvasPlugin(FakeContext(tmp_path), desk=desk, start_watcher=False)


def test_routes_are_advertised(plugin):
    assert "/plugin/agent-canvas/reply" in plugin.routes()


def test_health_answers(plugin):
    status, body = plugin.handle_get("/health", {})
    assert status == 200 and body["ok"] is True


def test_artifacts_register_only_natively_and_follow_session_key(plugin, tmp_path, tmux, monkeypatch):
    monkeypatch.setattr("agent_canvas.plugin.workspace_for_session", lambda session: tmp_path)
    file = tmp_path / "proposal.html"
    file.write_text("<h1>Proposal</h1>")
    body = {"session": "vc-1", "path": str(file)}
    for headers in (
        {"origin": "http://localhost:7682", "host": "localhost:7682"},
        {"sec-fetch-site": "same-origin"},
        {"origin": "null"},
    ):
        assert plugin.handle_request("POST", "/artifacts/register", {}, body, headers)[0] == 403
    status, result = plugin.handle_request("POST", "/artifacts/register", {}, body, {})
    assert status == 200
    key = tmux.keys.pop("vc-1")
    tmux.keys["renamed"] = key
    view = plugin.handle_get("/frag", {"session": ["renamed"]})[1]
    assert view["artifacts"][0]["name"] == "proposal.html"
    file.unlink()
    status, page, mime = plugin.handle_get("/artifact", {"key": [key], "id": [result["artifact"]["id"]]})
    assert status == 200 and mime.startswith("text/html")
    assert b"<h1>Proposal</h1>" not in page
    assert plugin.handle_get("/artifact", {"key": ["../bad"], "id": ["bad"]})[0] == 400


def test_html_write_hook_registers_output_even_without_a_canvas(plugin, tmp_path, monkeypatch):
    monkeypatch.setattr("agent_canvas.plugin.workspace_for_session", lambda session: tmp_path)
    file = tmp_path / "generated.html"
    file.write_text("<h1>Output</h1>")
    payload = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(file)},
        "tool_response": {"type": "update"},
        "cwd": str(tmp_path),
    }
    status, output, _ = plugin.handle_request("POST", "/nudge", {"session": ["vc-1"]}, payload, {})
    assert status == 200 and b"Artifact saved" in output
    assert plugin.handle_get("/frag", {"session": ["vc-1"]})[1]["artifacts"][0]["name"] == file.name


def test_page_is_html_with_one_nonced_script(plugin):
    status, data, ctype = plugin.handle_get("/page", {"session": ["vc-1"]})
    assert status == 200 and ctype.startswith("text/html")
    page = data.decode()
    nonce = re.search(r"script-src 'nonce-([^']+)'", page).group(1)
    assert page.count("<script") == 1 and f'<script nonce="{nonce}">' in page
    # The CSP precedes the first byte any agent could control.
    assert page.index("Content-Security-Policy") < page.index('<main id="vc-canvas">')


def test_each_page_gets_a_fresh_nonce(plugin):
    first = plugin.handle_get("/page", {"session": ["vc-1"]})[1]
    second = plugin.handle_get("/page", {"session": ["vc-1"]})[1]
    assert first != second


def test_csp_admits_no_inline_or_remote_script():
    policy = content_security_policy("abc")
    assert "script-src 'nonce-abc'" in policy
    assert "unsafe-inline" not in policy.split("script-src")[1].split(";")[0]
    assert "img-src 'self' data:" in policy
    assert "default-src 'none'" in policy
    frames = policy.split("frame-src ")[1].split(";")[0]
    assert "'self'" not in frames
    assert all(url.endswith("/plugin/agent-canvas/artifact") for url in frames.split())


def test_artifact_frame_policy_tracks_the_guarded_host(plugin):
    _, raw, _ = plugin.handle_request("GET", "/page", {"session": ["vc-1"]}, None, {"host": "mac.example.ts.net:8443"})
    assert b"https://mac.example.ts.net:8443/plugin/agent-canvas/artifact" in raw
    with pytest.raises(ValueError):
        content_security_policy("nonce", "evil/; frame-src *")


def test_session_names_are_validated_everywhere(plugin):
    for route in ("/page", "/frag", "/draw"):
        assert plugin.handle_get(route, {"session": ["../etc"]})[0] == 400
    assert plugin.handle_post("/reply", {}, {"session": "a b", "answer": "x"})[0] == 400


def test_session_name_is_escaped_into_the_page():
    page = build_page('vc"1', "n")
    assert 'data-session="vc&quot;1"' in page


def test_with_no_configured_origins_only_loopback_may_frame_the_page(plugin):
    assert 'data-framers=""' in build_page("vc-1", "n")
    assert 'data-framers=""' in plugin.handle_get("/page", {"session": ["vc-1"]})[1].decode()


def test_the_operators_origins_may_frame_the_page(tmp_path, tmux, host):
    """Invented origins: the operator's hosted rack, and one on a custom port."""
    tmux.keys["vc-1"] = KEY
    context = FakeContext(tmp_path, allowed_origins=frozenset({"https://rack.example.test", "http://rack.example.test:8080"}))
    plugin = AgentCanvasPlugin(context, desk=CanvasDesk(host, tmux=tmux), start_watcher=False)
    page = plugin.handle_get("/page", {"session": ["vc-1"]})[1].decode()
    assert 'data-framers="http://rack.example.test:8080 https://rack.example.test"' in page


@pytest.mark.parametrize(
    "configured,expected",
    [
        (None, ()),
        ("https://rack.example.test", ()),  # a bare string is not a list of origins
        (["https://Rack.Example.TEST", "https://rack.example.test:443/"], ("https://rack.example.test",)),
        (["http://[::1]:5173", "http://rack.example.test:80"], ("http://[::1]:5173", "http://rack.example.test")),
        (
            [
                "https://rack.example.test/path",
                "https://u@example.test",
                "javascript:alert(1)",
                "https://rack.example.test?x=1",
                "rack.example.test",
                " https://rack.example.test",
                7,
                'https://rack.example.test" onload="x',
                "https://rack.example.test:99999",
            ],
            (),
        ),
    ],
)
def test_framer_origins_keep_only_bare_http_origins_as_a_browser_reports_them(configured, expected):
    assert framer_origins(configured) == expected


def test_frag_round_trip(plugin, canvas_root):
    write_canvas(canvas_root, KEY, "<p>hello</p>")
    status, body = plugin.handle_get("/frag", {"session": ["vc-1"]})
    assert status == 200 and body["html"] == "<p>hello</p>" and body["question"] is None


def test_draw_pastes_the_sessions_own_canvas_path_without_enter(plugin, canvas_root):
    status, body = plugin.handle_get("/draw", {"session": ["vc-1"]})
    assert status == 200 and body["key"] == KEY
    assert str(canvas_path(KEY, ".json")) in body["paste"]
    assert body["paste"].endswith(" ") and "\n" not in body["paste"]


def test_draw_for_a_gone_session_is_404(plugin):
    status, body = plugin.handle_get("/draw", {"session": ["vc-9"]})
    assert status == 404 and "vc-9" in body["error"]


def test_reply_delivers(plugin, canvas_root, tmux):
    write_canvas(canvas_root, KEY, '<div class="choice"><p>Go?</p><button data-reply="go">GO</button></div>')
    status, body = plugin.handle_request(
        "POST", "/reply", {}, {"session": "vc-1", "answer": "go"}, {"origin": "http://127.0.0.1:7682", "host": "127.0.0.1:7682"}
    )
    assert status == 200 and body["delivered"] is True
    assert tmux.typed == [("vc-1", "canvas › Go? → go")]


def test_reply_requires_an_answer(plugin):
    assert plugin.handle_post("/reply", {}, {"session": "vc-1", "answer": "  "})[0] == 400
    assert plugin.handle_post("/reply", {}, None)[0] == 400


def test_reply_from_another_loopback_page_is_refused(plugin, tmux):
    status, _ = plugin.handle_request(
        "POST", "/reply", {}, {"session": "vc-1", "answer": "go"}, {"origin": "http://localhost:5173", "host": "127.0.0.1:7682"}
    )
    assert status == 403 and tmux.typed == []


def test_same_origin_rules():
    assert same_origin({"origin": "http://127.0.0.1:7682", "host": "127.0.0.1:7682"})
    assert not same_origin({"origin": "http://127.0.0.1:5173", "host": "127.0.0.1:7682"})
    assert same_origin({})  # curl
    assert not same_origin({"sec-fetch-site": "cross-site"})  # a no-cors browser request


def test_a_damaged_mermaid_bundle_is_a_500_never_served(plugin, tmp_path, monkeypatch):
    from agent_canvas import vendor

    bad = tmp_path / "mermaid.min.js.gz"
    bad.write_bytes(__import__("gzip").compress(b"window.mermaid={}"))
    monkeypatch.setattr(vendor, "MERMAID_BUNDLE", bad)
    vendor.mermaid_js.cache_clear()
    try:
        status, body = plugin.handle_get("/mermaid.js", {})
    finally:
        vendor.mermaid_js.cache_clear()
    assert status == 500 and "not mermaid" in body["error"]
    assert any("not mermaid" in line for line in plugin._context.logged)


def test_session_start_names_the_sessions_own_canvas_and_makes_the_directory(plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CANVAS_DIR", str(tmp_path / "fresh" / "canvas"))
    status, data, ctype = plugin.handle_get("/session-start", {"session": ["vc-1"]})
    assert status == 200 and ctype.startswith("text/plain")
    assert str(canvas_path(KEY, ".json")) in data.decode() and str(canvas_path(KEY, ".html")) in data.decode()
    assert (tmp_path / "fresh" / "canvas").is_dir()


def test_session_start_is_empty_for_a_session_with_no_canvas_and_refuses_bad_names(plugin):
    assert plugin.handle_get("/session-start", {"session": ["vc-9"]})[:2] == (200, b"")
    assert plugin.handle_get("/session-start", {"session": ["../x"]})[0] == 400
    assert "/plugin/agent-canvas/session-start" in plugin.routes()


def post_check(plugin, file_path, headers=None, tool="Write"):
    body = {"hook_event_name": "PostToolUse", "tool_name": tool, "tool_input": {"file_path": file_path}}
    return plugin.handle_request("POST", "/check", {}, body, headers or {})


def test_the_check_hook_hears_the_issues_in_the_canvas_it_wrote(plugin, canvas_root):
    write_doc(canvas_root, KEY, {"type": "chart"})
    for tool in ("Write", "Edit"):
        status, data, ctype = post_check(plugin, str(canvas_root / f"{KEY}.json"), tool=tool)
        assert status == 200 and ctype.startswith("text/plain") and b"NOTHING was drawn" in data
    write_doc(canvas_root, KEY, {"type": "text", "text": "hi"})
    assert post_check(plugin, str(canvas_root / f"{KEY}.json"))[:2] == (200, b"")


def test_the_check_hook_is_quiet_for_any_file_outside_the_canvas_dir(plugin, canvas_root, tmp_path):
    write_doc(canvas_root, KEY, {"type": "chart"})
    elsewhere = tmp_path / "project"
    elsewhere.mkdir()
    for path in (
        str(elsewhere / f"{KEY}.json"),
        str(canvas_root / f"{KEY}.html"),
        str(canvas_root / f"${KEY}.json"),
        str(canvas_root / "notes.json"),
        7,
    ):
        assert post_check(plugin, path)[:2] == (200, b"")
    assert plugin.handle_request("POST", "/check", {}, None, {})[:2] == (200, b"")


@pytest.mark.parametrize(
    "headers", [{"origin": "http://127.0.0.1:7682", "host": "127.0.0.1:7682"}, {"sec-fetch-site": "same-origin"}]
)
def test_the_check_hook_refuses_browsers(plugin, canvas_root, headers):
    assert post_check(plugin, str(canvas_root / f"{KEY}.json"), headers)[0] == 403


def test_keys_cannot_escape_the_canvas_dir(canvas_root):
    assert normalize_key("$7-1760000000") == KEY
    for bad in ("../x", "7", "7-x", "/etc/passwd", None, "7-1760000000/../../x"):
        assert normalize_key(bad) is None
    with pytest.raises(ValueError):
        canvas_path("../../etc")


def test_a_symlinked_canvas_is_not_served(canvas_root, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("token")
    (canvas_root / f"{KEY}.html").symlink_to(secret)
    assert read_fragment(canvas_path(KEY)) is None


def test_an_oversized_canvas_is_truncated(canvas_root):
    write_canvas(canvas_root, KEY, "x" * (300 * 1024))
    text, _rev, truncated = read_fragment(canvas_path(KEY))
    assert truncated and len(text) == 256 * 1024


def test_busy_reasons_name_queued_answers(plugin, canvas_root, host):
    host.rows["vc-1"]["state"] = "busy"
    write_canvas(canvas_root, KEY, '<div class="choice"><button data-reply="y">Y</button></div>')
    plugin.handle_post("/reply", {}, {"session": "vc-1", "answer": "y"})
    assert plugin.busy_reasons() == ["answer queued for vc-1"]


def test_check_validates_the_key_and_reports(plugin, canvas_root):
    assert plugin.handle_get("/check", {"key": ["../x"]})[0] == 400
    write_doc(canvas_root, KEY, {"type": "text", "text": "hi"})
    status, body = plugin.handle_get("/check", {"key": [f"${KEY}"]})
    assert status == 200 and body["ok"] is True


def test_reply_with_answers_reaches_the_doc_path(plugin, canvas_root, tmux):
    write_doc(canvas_root, KEY, {"type": "choice", "q": "Go?", "options": ["go"]})
    status, _ = plugin.handle_request(
        "POST",
        "/reply",
        {},
        {"session": "vc-1", "answers": {"q1": "go"}},
        {"origin": "http://127.0.0.1:7682", "host": "127.0.0.1:7682"},
    )
    assert status == 200 and tmux.typed == [("vc-1", "canvas › Go? → go")]


def test_check_text_is_empty_when_clean_and_names_each_issue_when_not(plugin, canvas_root):
    write_doc(canvas_root, KEY, {"type": "text", "text": "hi"})
    status, body, ctype = plugin.handle_get("/check", {"key": [KEY], "format": ["text"]})
    assert (status, body, ctype.split(";")[0]) == (200, b"", "text/plain")
    write_doc(canvas_root, KEY, {"type": "text", "text": "hi"}, {"type": "chart"})
    text = plugin.handle_get("/check", {"key": [KEY], "format": ["text"]})[1].decode()
    assert "1 issue in" in text and "the rest was drawn" in text
    assert 'blocks[1]  unknown-block: unknown type "chart"' in text
    canvas_root.joinpath(f"{KEY}.json").write_text("{nope", encoding="utf-8")
    assert "NOTHING was drawn" in plugin.handle_get("/check", {"key": [KEY], "format": ["text"]})[1].decode()


def test_view_is_advertised_and_flips_from_any_loopback_page(plugin):
    assert "/plugin/agent-canvas/view" in plugin.routes()
    terminal_page = {"origin": "http://127.0.0.1:7681", "host": "127.0.0.1:7682"}
    status, body = plugin.handle_request("POST", "/view", {}, {"session": "vc-1", "view": "toggle"}, terminal_page)
    assert (status, body["view"]) == (200, "skills")
    assert plugin.handle_get("/frag", {"session": ["vc-1"]})[1]["view"] == "skills"


@pytest.mark.parametrize("body", [None, {"view": "skills"}, {"session": "../x", "view": "skills"}])
def test_view_refuses_a_body_without_a_valid_session(plugin, body):
    assert plugin.handle_post("/view", {}, body)[0] == 400


def test_the_skills_face_rides_the_one_nonced_script_ahead_of_the_kit(plugin):
    page = plugin.handle_get("/page", {"session": ["vc-1"]})[1].decode()
    assert page.count("<script") == 1
    script = page[page.index("<script") :]
    assert -1 < script.index("const vc_skills_view") < script.index("const BLOCKS")
    assert '<div id="vc-skills" hidden></div>' in page and '<nav id="vc-views"' in page
