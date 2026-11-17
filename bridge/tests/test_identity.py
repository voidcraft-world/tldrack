from tldrack.identity import classify_launcher, format_uptime, parse_ps_line, walk_parent_chain
from tldrack.main import is_loopback_origin, shutdown_origin_allowed


def _chain(*comms, start_pid=100):
    """Build a parent chain from comm names, pids descending from start_pid."""
    return [{"pid": start_pid - i, "comm": comm} for i, comm in enumerate(comms)]


# --- parse_ps_line -----------------------------------------------------------


def test_parse_ps_line_basic():
    assert parse_ps_line("  812 /usr/bin/zsh\n") == (812, "/usr/bin/zsh")


def test_parse_ps_line_comm_path_with_spaces():
    # macOS comm is a full path — iTerm's contains no spaces but the .app dir can.
    line = "  512 /Applications/Some App.app/Contents/MacOS/Some App"
    assert parse_ps_line(line) == (512, "/Applications/Some App.app/Contents/MacOS/Some App")


def test_parse_ps_line_garbage():
    assert parse_ps_line("") is None
    assert parse_ps_line("justonefield") is None
    assert parse_ps_line("notanumber /bin/zsh") is None


# --- walk_parent_chain -------------------------------------------------------


def _fake_reader(table):
    return lambda pid: table.get(pid)


def test_walk_stops_at_launchd():
    table = {50: (20, "/usr/bin/uv"), 20: (1, "/bin/zsh")}
    chain = walk_parent_chain(50, read=_fake_reader(table))
    assert [row["comm"] for row in chain] == ["/usr/bin/uv", "/bin/zsh", "launchd"]
    assert chain[-1]["pid"] == 1


def test_walk_cycle_guard():
    table = {50: (60, "a"), 60: (50, "b")}  # 50 → 60 → 50 …
    chain = walk_parent_chain(50, read=_fake_reader(table))
    assert [row["pid"] for row in chain] == [50, 60]


def test_walk_max_depth():
    table = {pid: (pid + 1, f"p{pid}") for pid in range(2, 100)}
    assert len(walk_parent_chain(2, read=_fake_reader(table), max_depth=5)) == 5


def test_walk_reader_miss_stops():
    table = {50: (40, "/usr/bin/uv")}  # 40 already gone
    chain = walk_parent_chain(50, read=_fake_reader(table))
    assert [row["comm"] for row in chain] == ["/usr/bin/uv"]


# --- classify_launcher -------------------------------------------------------


SUPERVISOR = frozenset({"MyRackApp"})


def test_a_named_supervisor_is_classified_under_the_wire_kind():
    chain = _chain("/usr/local/bin/uv", "/Applications/MyRackApp.app/Contents/MacOS/MyRackApp", "launchd")
    result = classify_launcher(chain, tty=None, supervisor_comms=SUPERVISOR)
    assert result == {"kind": "menubar", "label": "a supervisor app (MyRackApp)"}


def test_a_supervisor_is_found_behind_the_python_and_uv_wrappers():
    chain = _chain(
        "/Users/u/code/project/bridge/.venv/bin/python",
        "/Users/u/.local/bin/uv",
        "/Users/u/code/project/build/MyRackApp.app/Contents/MacOS/MyRackApp",
        "launchd",
    )
    assert classify_launcher(chain, tty=None, supervisor_comms=SUPERVISOR)["kind"] == "menubar"


def test_no_process_is_a_supervisor_until_the_operator_names_one():
    chain = _chain("/usr/local/bin/uv", "/Applications/MyRackApp.app/Contents/MacOS/MyRackApp", "launchd")
    assert classify_launcher(chain, tty=None)["kind"] == "headless"


def test_classify_iterm_behind_shells():
    chain = _chain("uv", "/bin/zsh", "login", "/Applications/iTerm.app/Contents/MacOS/iTerm2", "launchd")
    result = classify_launcher(chain, tty="ttys003")
    assert result == {"kind": "iterm2", "label": "iTerm2 (tty ttys003)"}


def test_classify_iterm_real_chain_login_dash_and_itermserver():
    # A real iTerm tab's chain: login shell reports as "-zsh" (leading dash) and
    # the terminal parent is iTermServer-<version>, not iTerm2 itself.
    chain = _chain("uv", "-zsh", "login", "iTermServer-3.6.11", "iTerm2", "launchd")
    assert classify_launcher(chain, tty="ttys009")["kind"] == "iterm2"


def test_classify_apple_terminal():
    chain = _chain("zsh", "login", "/System/Applications/Utilities/Terminal.app/Contents/MacOS/Terminal", "launchd")
    assert classify_launcher(chain, tty="ttys001")["kind"] == "apple-terminal"


def test_classify_tmux():
    chain = _chain("zsh", "tmux", "launchd")
    assert classify_launcher(chain, tty="ttys002")["kind"] == "tmux"


def test_classify_agent_nearest_beats_iterm():
    # A claude/node agent running inside an iTerm tab is still an agent launch.
    chain = _chain("zsh", "node", "zsh", "iTerm2", "launchd")
    result = classify_launcher(chain, tty="ttys004")
    assert result == {"kind": "agent", "label": "headless agent (node)"}


def test_classify_claude_comm():
    chain = _chain("claude", "zsh", "iTerm2", "launchd")
    assert classify_launcher(chain, tty=None)["kind"] == "agent"


def test_classify_launchd_only():
    assert classify_launcher(_chain("launchd", start_pid=1), tty=None)["kind"] == "launchd"
    assert classify_launcher([], tty=None)["kind"] == "launchd"


def test_classify_unrecognized_is_headless_with_parent():
    chain = _chain("zsh", "supervisord", "launchd")
    result = classify_launcher(chain, tty=None)
    assert result == {"kind": "headless", "label": "headless, parent: supervisord"}


def test_classify_skips_python_wrappers():
    chain = _chain("Python", "python3.12", "uv", "iTerm2", "launchd")
    assert classify_launcher(chain, tty="ttys005")["kind"] == "iterm2"


# --- format_uptime -----------------------------------------------------------


def test_format_uptime_boundaries():
    assert format_uptime(45) == "45s"
    assert format_uptime(61) == "1m 1s"
    assert format_uptime(3700) == "1h 1m"
    assert format_uptime(90000) == "1d 1h"
    assert format_uptime(-5) == "0s"  # clock skew guard


# --- shutdown_origin_allowed -------------------------------------------------


def test_shutdown_origin_no_header_allowed():
    assert shutdown_origin_allowed(None) is True  # curl / CLI / native apps


def test_shutdown_origin_allowlist():
    for origin in (
        "http://localhost:5173",
        "https://localhost:5173",
        "http://127.0.0.1:5173",
        "https://127.0.0.1:5173",
    ):
        assert shutdown_origin_allowed(origin) is True, origin
    # A hosted page is served only when the operator's settings name its origin.
    named = frozenset({"https://rack.example"})
    assert shutdown_origin_allowed("https://rack.example") is False
    assert shutdown_origin_allowed("https://rack.example", named) is True
    assert shutdown_origin_allowed("https://www.rack.example", named) is False


def test_shutdown_origin_any_loopback_port_and_scheme():
    # The dev SPA roams: vite falls back to 5174+ when 5173 is taken, and
    # mkcert flips the scheme — any loopback origin is admitted.
    for origin in (
        "http://localhost:5174",
        "https://127.0.0.1:5999",
        "http://[::1]:5173",
        "http://localhost",
    ):
        assert shutdown_origin_allowed(origin) is True, origin


def test_shutdown_origin_rejected():
    for origin in (
        "https://evil.example",
        "null",  # sandboxed iframe / data: URL opaque origin
        "",
        "http://localhost.evil.com:5173",  # loopback-lookalike host
        "ws://localhost:5173",  # non-http(s) scheme
    ):
        assert shutdown_origin_allowed(origin) is False, origin


def test_is_loopback_origin_edge_cases():
    assert is_loopback_origin("http://LOCALHOST:5173") is True  # hostname is case-normalized
    assert is_loopback_origin("https://rack.example") is False
    assert is_loopback_origin("localhost:5173") is False  # no scheme
    assert is_loopback_origin("http://[::1") is False  # malformed IPv6 → ValueError path
