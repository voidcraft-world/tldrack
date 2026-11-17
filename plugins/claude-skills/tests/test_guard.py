"""POST /run's caller check: the operator's origins, the dev server and the terminal page, nothing else.

Every hosted origin here is invented (`*.example.test`).
"""

from __future__ import annotations

import re

import pytest

from claude_skills import guard

OPERATOR = "https://rack.example.test"
LOCAL = {"host": "127.0.0.1:7682"}


def check(port=None, allowed=(OPERATOR,)):
    return guard.run_refusal_for(port, allowed)


# ── the operator's origins ───────────────────────────────────────────────────


def test_no_hosted_origin_is_allowed_by_default():
    refusal = guard.run_refusal_for(None)({**LOCAL, "origin": OPERATOR, "sec-fetch-site": "cross-site"})
    assert refusal is not None and refusal.code == "origin_refused"
    assert all(re.match(r"https?://(localhost|127\.0\.0\.1):\d+$", origin) for origin in refusal.allowed_origins)


def test_an_operator_origin_may_start_a_run():
    assert check()({**LOCAL, "origin": OPERATOR, "sec-fetch-site": "cross-site"}) is None


def test_any_other_hosted_origin_is_refused_and_told_what_is_allowed():
    refusal = check()({**LOCAL, "origin": "https://evil.example.test"})
    assert refusal.code == "origin_refused"
    assert OPERATOR in refusal.allowed_origins


def test_an_allowed_origin_still_needs_a_loopback_host_and_no_proxy():
    assert check()({"host": "rack.example.test", "origin": OPERATOR}).code == "host_refused"
    assert check()({**LOCAL, "origin": OPERATOR, "x-forwarded-for": "10.0.0.2"}).code == "host_refused"
    assert check()({**LOCAL, "origin": OPERATOR, "tailscale-user-login": "x"}).code == "host_refused"


def test_operator_origins_are_read_in_the_browsers_spelling():
    logged: list[str] = []
    origins = guard.operator_origins(["HTTPS://Rack.Example.Test:443/", "http://notes.example.test:8080"], logged.append)
    assert origins == {OPERATOR, "http://notes.example.test:8080"}
    assert logged == []


@pytest.mark.parametrize(
    "bad", ["*", "null", "ftp://rack.example.test", "https://*.example.test", "https://rack.example.test/path", 3, None]
)
def test_a_bad_operator_origin_is_dropped_with_one_line_and_the_rest_kept(bad):
    logged: list[str] = []
    assert guard.operator_origins([bad, OPERATOR], logged.append) == {OPERATOR}
    assert len(logged) == 1


@pytest.mark.parametrize("raw", [OPERATOR, b"https://rack.example.test", 3, 4.5])
def test_origins_that_are_not_a_list_are_ignored_once(raw):
    logged: list[str] = []
    assert guard.operator_origins(raw, logged.append) == frozenset()
    assert len(logged) == 1


def test_absent_origins_read_as_none_and_say_nothing():
    logged: list[str] = []
    assert guard.operator_origins(None, logged.append) == frozenset()
    assert logged == []


# ── the dev server and the terminal page ─────────────────────────────────────


@pytest.mark.parametrize("origin", ["http://localhost:5173", "http://127.0.0.1:5179", "https://localhost:5175"])
def test_the_loopback_dev_server_may_ask(origin):
    assert check()({**LOCAL, "origin": origin, "sec-fetch-site": "same-site"}) is None


@pytest.mark.parametrize("origin", ["http://localhost:5180", "http://127.0.0.1:8000", "null"])
def test_any_other_loopback_page_may_not(origin):
    assert check()({**LOCAL, "origin": origin}).code == "origin_refused"


def test_the_bridges_own_terminal_page_may_start_a_run_and_no_other_port():
    refusal = check(7681)
    terminal = {**LOCAL, "origin": "http://127.0.0.1:7681", "sec-fetch-site": "same-site"}
    assert refusal(terminal) is None
    assert refusal({**terminal, "origin": "http://localhost:7681"}) is None
    assert refusal({**terminal, "origin": "http://127.0.0.1:7690"}).code == "origin_refused"
    # Without a ttyd port, the terminal page is refused like any other loopback page.
    assert check(None)(terminal).code == "origin_refused"


@pytest.mark.parametrize("port", [None, 0, 70000, "7681", True])
def test_no_usable_ttyd_port_admits_no_terminal_page(port):
    assert guard.terminal_origins(port) == frozenset()


# ── callers without an Origin ────────────────────────────────────────────────


def test_a_native_caller_and_a_typed_url_pass_but_a_no_cors_browser_request_does_not():
    assert check()(None) is None
    assert check()(LOCAL) is None
    assert check()({**LOCAL, "sec-fetch-site": "none"}) is None
    assert check()({**LOCAL, "sec-fetch-site": "cross-site"}).code == "no_cors_refused"


def test_the_module_names_no_hosted_site():
    text = open(guard.__file__, encoding="utf-8").read()
    assert text.count("://") >= 2  # the dev and terminal origins: the scan below is not vacuous
    hosts = re.findall(r"://([A-Za-z0-9.-]+)", text)
    assert set(hosts) <= {"localhost", "127.0.0.1"}, hosts
