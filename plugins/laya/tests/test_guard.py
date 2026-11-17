"""Laya's caller policy: which pages may reach the routes that carry the operator's text, and who is the rack.

The policy is built per plugin from what the host's context says (`guard.policy_for`); `is_native` is laya's own
rule, so its truth table is pinned here too.
"""

from __future__ import annotations

import itertools

import pytest

from laya_bridge import guard

OPERATOR = "https://rack.example"
POLICY = guard.policy_for(7682, guard.operator_origins([OPERATOR], lambda _line: None))

HOSTS = [None, "127.0.0.1:7682", "localhost:7682", "[::1]:7682", "evil.example", "mymac.tail1234.ts.net", ""]
ORIGINS = [
    None,
    OPERATOR,
    "http://127.0.0.1:7682",
    "http://localhost:5179",
    "http://localhost:5180",
    "null",
    "https://evil.example",
]
EXTRAS = [
    {},
    {"x-forwarded-for": "10.0.0.2"},
    {"Via": "1.1 proxy"},
    {"tailscale-user-login": "x"},
    {"sec-fetch-site": "none"},
    {"sec-fetch-site": "cross-site"},
]
ADMITTED_ORIGINS = (None, OPERATOR, "http://127.0.0.1:7682", "http://localhost:5179")
LOOPBACK_HOSTS = (None, "127.0.0.1:7682", "localhost:7682", "[::1]:7682")


def expected(host, origin, extra) -> str | None:
    """The refusal code a caller hears, worked out from the rule's own sentence (guard.py's module note)."""
    if any(name.lower() != "sec-fetch-site" for name in extra):
        return "host_refused"  # a proxy header
    if host not in LOOPBACK_HOSTS:
        return "host_refused"
    if origin is not None:
        return None if origin in ADMITTED_ORIGINS else "origin_refused"
    return "no_cors_refused" if extra.get("sec-fetch-site") == "cross-site" else None


def test_every_caller_hears_what_the_rule_says():
    checked = 0
    for host, origin, extra in itertools.product(HOSTS, ORIGINS, EXTRAS):
        headers = dict(extra)
        if host is not None:
            headers["host"] = host
        if origin is not None:
            headers["origin"] = origin
        refusal = POLICY.refusal(headers)
        assert (refusal.code if refusal else None) == expected(host, origin, extra), headers
        checked += 1
    assert checked == len(HOSTS) * len(ORIGINS) * len(EXTRAS) and POLICY.refusal(None) is None


def test_no_hosted_origin_is_allowed_until_the_operator_names_one():
    bare = guard.policy_for()
    assert bare.allowed_origins == guard.DEV_ORIGINS
    assert not any(origin.endswith((".world", ".com", ".example")) for origin in bare.allowed_origins)
    assert bare.refusal({"host": "127.0.0.1:7682", "origin": OPERATOR}).code == "origin_refused"
    assert POLICY.refusal({"host": "127.0.0.1:7682", "origin": OPERATOR, "sec-fetch-site": "cross-site"}) is None


def test_the_bridges_own_page_may_read_and_another_loopback_port_may_not():
    assert guard.bridge_origins(7682) == {"http://127.0.0.1:7682", "http://localhost:7682"}
    assert POLICY.refusal({"host": "127.0.0.1:7682", "origin": "http://localhost:7682"}) is None
    assert POLICY.refusal({"host": "127.0.0.1:7682", "origin": "http://localhost:9999"}).code == "origin_refused"
    for bad in (None, 0, 70000, True, "7682"):
        assert guard.bridge_origins(bad) == frozenset()


def test_operator_origins_are_spelled_as_a_browser_spells_them_and_a_bad_one_is_dropped_with_a_line():
    said: list[str] = []
    assert guard.operator_origins(["HTTPS://Rack.Example:443", "not an origin"], said.append) == {OPERATOR}
    assert len(said) == 1 and "not an origin" not in said[0].split(":", 1)[0]
    assert guard.operator_origins(None, said.append) == frozenset()
    assert guard.operator_origins(OPERATOR, said.append) == frozenset() and len(said) == 2  # one string, refused whole


# -- is_native: who may tag a rack surface ---------------------------------------


@pytest.mark.parametrize(
    "headers",
    [
        None,  # in process (tldrack plugin_caller)
        {},
        {"host": "127.0.0.1:7682"},  # the coordinator's loopback urllib call
        {"Host": "localhost:7682", "content-type": "application/json", "user-agent": "Python-urllib/3.12"},
        {"host": "[::1]:7682"},
    ],
)
def test_native_callers(headers):
    assert guard.is_native(headers) is True


@pytest.mark.parametrize(
    "headers",
    [
        {"host": "127.0.0.1:7682", "origin": "https://rack.example"},  # a page the operator hosts
        {"host": "127.0.0.1:7682", "origin": "http://localhost:5173"},  # the dev SPA
        {"host": "127.0.0.1:7682", "sec-fetch-site": "none"},  # a browser, typed URL
        {"host": "127.0.0.1:7682", "Sec-Fetch-Site": "same-origin"},
        {"host": "127.0.0.1:7682", "x-forwarded-for": "10.0.0.2"},
        {"host": "127.0.0.1:7682", "forwarded": "for=1.2.3.4"},
        {"host": "127.0.0.1:7682", "tailscale-user-login": "x"},
        {"host": "mymac.tail1234.ts.net"},
        {"host": "evil.example"},
        {"host": ""},
    ],
)
def test_not_native(headers):
    assert guard.is_native(headers) is False
