"""Per-session memory: the fold, and what an orphan is. Every process line is invented."""

import subprocess

import pytest

from devices_plugin import sessions
from devices_plugin.sessions import (
    MAX_SESSIONS,
    fold_session_memory,
    parse_panes,
    parse_ps,
    read_session_memory,
)

KB = 1024

PS = """\
    1     0   100 /sbin/launchd
  100     1  6400 tmux new-session -d -s agent-1 /bin/zsh claude
  200   100  2000 -zsh
  201   200 300000 /Users/me/.local/bin/claude
  202   201 100000 node /x/playwright-mcp/cli.js
  300   100  2000 -zsh
  301   300 500000 /Users/me/.local/bin/claude
  400     1 10000 /Users/me/.codex/packages/app-server-daemon/releases/0.166/bin/codex app-server daemon
  500     1 23000 /Applications/Docker.app/Contents/Resources/cli-plugins/docker-agent serve
  600     1 90000 /Users/me/.local/bin/claude --resume=abc
  601   600 40000 node /x/some-mcp-server/index.js
  700     1 50000 node /y/mcp-server-github/dist/index.js
  800     1 11000 node /z/my-mcp-service --http-port 7684
"""
PANES = "agent-1 200\nwt-feat 300\n"
#: The operator's own long-lived MCP service, named in the `daemons` setting.
DAEMONS = ("my-mcp-service",)


def fold(ps=PS, panes=PANES, daemons=DAEMONS):
    return fold_session_memory(parse_ps(ps), parse_panes(panes), daemons)


def test_a_sessions_tree_is_its_pane_and_every_descendant_largest_first():
    assert fold()["sessions"] == [
        {"session": "wt-feat", "rss": 502000 * KB, "procs": 2},
        {"session": "agent-1", "rss": 402000 * KB, "procs": 3},
    ]


def test_orphans_are_stray_agents_and_mcp_servers_with_their_children():
    assert fold()["orphans"] == {"rss": 180000 * KB, "procs": 3}


def test_the_tmux_server_the_shared_daemons_and_other_apps_are_not_orphans():
    clean = fold(
        """\
  100     1  6400 tmux new-session -d -s agent-1 claude mcp
  400     1 10000 /x/codex app-server daemon
  500     1 23000 /Applications/Docker.app/docker-agent serve
  800     1 11000 node /z/my-mcp-service
""",
        "",
    )
    assert clean["orphans"] == {"rss": 0, "procs": 0}


def test_an_operators_mcp_service_is_an_orphan_until_named_in_daemons():
    line = "  800     1 11000 node /z/my-mcp-service --http-port 7684\n"
    assert fold(line, "", daemons=())["orphans"] == {"rss": 11000 * KB, "procs": 1}
    assert fold(line, "", daemons=DAEMONS)["orphans"] == {"rss": 0, "procs": 0}


def test_read_session_memory_passes_the_daemons_through():
    def run(argv):
        return "  800     1 11000 0.0 node /z/my-mcp-service\n" if argv[0] == "ps" else ""

    assert read_session_memory(run)["orphans"]["procs"] == 1
    assert read_session_memory(run, daemons=DAEMONS)["orphans"]["procs"] == 0


def test_a_word_that_merely_contains_mcp_is_not_an_mcp_server():
    assert fold("  900     1  100 /usr/bin/xmcpd --serve\n", "")["orphans"]["procs"] == 0


def test_parents_gone_from_the_listing_and_cycles_do_not_hang_or_count():
    result = fold("  10    11  100 a\n  11    10  100 b\n  12   999  100 c\n", "")
    assert result["sessions"] == [] and result["orphans"]["procs"] == 0


def test_a_session_with_two_panes_is_one_row():
    result = fold("  10     1  100 a\n  20     1  200 b\n", "s 10\ns 20\n")
    assert result["sessions"] == [{"session": "s", "rss": 300 * KB, "procs": 2}]


def test_the_cut_is_counted_never_silent():
    count = MAX_SESSIONS + 3
    ps = "".join(f"  {i + 10}     1  {i + 1} a\n" for i in range(count))
    panes = "".join(f"s{i} {i + 10}\n" for i in range(count))
    result = fold(ps, panes)
    assert len(result["sessions"]) == MAX_SESSIONS and result["more"] == 3
    assert result["sessions"][0]["session"] == f"s{count - 1}"


def test_unreadable_lines_are_skipped_and_a_name_may_hold_a_space():
    assert sorted(parse_ps("garbage\n  x y z\n  5 1 10 cmd with args\n")) == [5]
    assert parse_panes("my session 42\nbroken\n") == {42: "my session"}


def test_no_command_line_ever_reaches_the_result():
    result = fold("  10     1  100 claude --api-key=sekret\n", "s 10\n")
    assert "sekret" not in str(result)


def test_read_session_memory_asks_ps_then_tmux():
    calls = []

    def run(argv):
        calls.append(argv[0])
        return "  10     1  100 0.0 a\n" if argv[0] == "ps" else "s 10\n"

    assert read_session_memory(run)["sessions"][0]["session"] == "s"
    assert calls == ["ps", "tmux"]


def test_a_real_timeout_propagates_to_the_sampler():
    def run(argv):
        raise subprocess.TimeoutExpired(argv, 3)

    try:
        read_session_memory(run)
    except subprocess.TimeoutExpired:
        return
    raise AssertionError("the sampler owns what a timeout means")


def completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout, stderr)


def test_a_failed_command_is_a_failed_tick_not_an_empty_machine(monkeypatch):
    monkeypatch.setattr(sessions.subprocess, "run", lambda *_, **__: completed(1, stderr="ps: boom"))
    with pytest.raises(subprocess.CalledProcessError):
        sessions._run(["ps"])
    with pytest.raises(subprocess.CalledProcessError):
        sessions._run(["tmux", "list-panes"])


def test_no_tmux_server_is_an_answer_not_a_failure(monkeypatch):
    monkeypatch.setattr(
        sessions.subprocess, "run", lambda *_, **__: completed(1, stderr="no server running on /private/tmp/tmux-501/default")
    )
    assert sessions._run(["tmux", "list-panes"]) == ""


def test_cpu_uses_the_same_tree_including_multiple_panes_and_never_leaks_commands():
    processes = parse_ps("10 1 100 12.5 shell\n11 10 200 150.0 worker --secret=token\n20 1 300 0.0 server\n", cpu_column=True)
    result = fold_session_memory(processes, {10: "agent", 20: "service"})
    by_name = {row["session"]: row for row in result["sessions"]}
    assert by_name["agent"]["cpu_pct"] == 162.5
    assert by_name["service"]["cpu_pct"] == 0
    assert by_name["agent"]["rss"] == 300 * KB
    assert "token" not in str(result)


def test_invalid_cpu_does_not_become_a_false_zero():
    processes = parse_ps("10 1 100 nan shell\n11 10 200 10 worker\n", cpu_column=True)
    result = fold_session_memory(processes, {10: "agent"})
    assert "cpu_pct" not in result["sessions"][0]
    assert result["sessions"][0]["rss"] == 300 * KB
