"""Who may talk to the listener (gate.py), and which requests it forwards (allowlist.py), exactly."""

import pytest

from phone_link import allowlist, gate
from phone_link.config import TailnetDoor

NONCE = "n" * 43
MAC = "a" * 64


def headers(**extra):
    base = {"host": ["127.0.0.1:7683"], "authorization": [f"PhoneLink n={NONCE}, mac={MAC}"]}
    base.update({name.replace("_", "-"): value if isinstance(value, list) else [value] for name, value in extra.items()})
    return base


# -- the gate -----------------------------------------------------------------------------------------
def test_the_app_is_admitted():
    assert gate.refusal(headers()) is None
    assert gate.refusal(headers(host="localhost:7683")) is None
    assert gate.refusal(headers(host="[::1]:7683")) is None
    no_host = headers()
    del no_host["host"]
    assert gate.refusal(no_host) is None
    no_credential = headers()
    del no_credential["authorization"]
    assert gate.refusal(no_credential) is None  # /nonce: the gate runs before any proof


@pytest.mark.parametrize(
    "host",
    [
        "evil.example",
        "evil.example:7683",
        "127.0.0.1.nip.io",
        "evil@127.0.0.1",
        "localhost.",
        "127.0.0.2:7683",
        "mymac.tail1234.ts.net",
        "",
        "0.0.0.0:7683",
    ],
)
def test_a_non_loopback_host_is_refused_before_the_token(host):
    assert gate.refusal(headers(host=host)) == gate.HOST_REFUSED
    assert gate.refusal(headers(host=host, authorization="nope")) == gate.HOST_REFUSED


def test_two_host_headers_are_refused():
    assert gate.refusal(headers(host=["127.0.0.1:7683", "evil.example"])) == gate.HOST_REFUSED


@pytest.mark.parametrize("origin", ["https://voidcraft.world", "http://localhost:5173", "null", ""])
def test_any_origin_is_a_browser_and_refused(origin):
    assert gate.refusal(headers(origin=origin)) == gate.BROWSER_REFUSED


@pytest.mark.parametrize("name", ["sec-fetch-site", "sec-fetch-mode", "sec-fetch-dest"])
def test_fetch_metadata_is_a_browser_too(name):
    assert gate.refusal({**headers(), name: ["none"]}) == gate.BROWSER_REFUSED


@pytest.mark.parametrize("name", ["x-forwarded-for", "forwarded", "via", "x-real-ip", "tailscale-user-login"])
def test_proxy_headers_are_refused(name):
    assert gate.refusal({**headers(), name: ["1.2.3.4"]}) == gate.PROXY_REFUSED


def test_the_credential_is_read_from_the_one_authorization_header():
    found = gate.credential(headers())
    assert (found.nonce, found.mac) == (NONCE, MAC)


@pytest.mark.parametrize(
    "auth",
    [
        None,
        [],
        [""],
        ["Bearer " + "t" * 43],
        [f"Basic {NONCE}"],
        [f"PhoneLink n={NONCE}"],
        [f"PhoneLink n={NONCE}, mac={MAC}"] * 2,
        [f"PhoneLink n={NONCE}x, mac={MAC}"],
        [NONCE],
    ],
)
def test_a_missing_or_malformed_credential_is_none(auth):
    sent = headers()
    if auth is None:
        del sent["authorization"]
    else:
        sent["authorization"] = auth
    assert gate.credential(sent) is None


# -- the tailnet door -----------------------------------------------------------------------------------
DOOR = TailnetDoor("mymac.tail1234.ts.net", 7683, "operator@example.com")


def served(**extra):
    """A request as `tailscale serve` hands it on: the client's own Host, and serve's headers for a tailnet user."""
    base = headers(
        host=DOOR.authority,
        x_forwarded_for="100.64.0.7",
        x_forwarded_host=DOOR.authority,
        x_forwarded_proto="https",
        tailscale_user_login="operator@example.com",
        tailscale_user_name="The Operator",
        tailscale_user_profile_pic="https://example.com/p.png",
        tailscale_headers_info="https://tailscale.com/s/serve-headers",
    )
    for name, value in extra.items():
        name = name.replace("_", "-")
        if value is None:
            base.pop(name, None)
        else:
            base[name] = value if isinstance(value, list) else [value]
    return base


def test_the_door_does_not_exist_until_it_is_configured():
    assert gate.refusal(served()) == gate.HOST_REFUSED
    assert gate.refusal(served(), DOOR) is None


def test_the_door_changes_nothing_for_loopback():
    assert gate.refusal(headers(), DOOR) is None
    # A tailnet caller claiming to be local still carries serve's headers, and rule 2 still refuses them.
    assert gate.refusal(served(host="127.0.0.1:7683"), DOOR) == gate.PROXY_REFUSED
    assert gate.refusal(headers(host="evil.example"), DOOR) == gate.HOST_REFUSED
    assert gate.refusal(headers(x_forwarded_for="1.2.3.4"), DOOR) == gate.PROXY_REFUSED


@pytest.mark.parametrize(
    "host",
    [
        "mymac.tail1234.ts.net",  # the port is part of the name the door answers to
        "mymac.tail1234.ts.net:8443",
        "someone-else.tail1234.ts.net:7683",
        "mymac.tail1234.ts.net.evil.example:7683",
        "x" + DOOR.authority,
    ],
)
def test_only_the_one_configured_name_and_port_is_the_door(host):
    assert gate.refusal(served(host=host), DOOR) == gate.HOST_REFUSED


def test_the_doors_name_is_matched_whatever_its_case():
    assert gate.refusal(served(host=DOOR.authority.upper()), DOOR) is None
    assert gate.refusal(served(tailscale_user_login="Operator@Example.com"), DOOR) is None
    assert gate.refusal(served(x_forwarded_host=DOOR.host), DOOR) is None  # with or without the port


@pytest.mark.parametrize(
    "change",
    [
        {"tailscale_funnel_request": "?1"},  # from the public internet
        {"tailscale_user_login": "stranger@example.com"},
        {"tailscale_user_login": None},  # a tagged device: serve vouches for no user
        {"tailscale_user_login": ["operator@example.com", "operator@example.com"]},
        {"x_forwarded_for": None},  # not through serve at all
        {"x_forwarded_proto": "http"},
        {"x_forwarded_proto": None},
        {"x_forwarded_host": "evil.example"},
        {"x_forwarded_host": None},
        {"x_forwarded_port": "7683"},  # a header of serve's family that serve does not send
        {"tailscale_something_new": "1"},
        {"forwarded": "for=1.2.3.4"},
        {"via": "1.1 proxy"},
        {"x_real_ip": "1.2.3.4"},
    ],
)
def test_the_door_takes_exactly_what_serve_sends_for_the_configured_login(change):
    assert gate.refusal(served(**change), DOOR) == gate.TAILNET_REFUSED


def test_a_browser_is_refused_at_the_door_too():
    assert gate.refusal(served(origin="https://mymac.tail1234.ts.net:7683"), DOOR) == gate.BROWSER_REFUSED
    assert gate.refusal(served(sec_fetch_site="same-origin"), DOOR) == gate.BROWSER_REFUSED


# -- the allowlist ------------------------------------------------------------------------------------
@pytest.mark.parametrize("path, method", sorted(allowlist.FORWARDED.items()))
def test_every_allowed_route_forwards_with_its_one_method(path, method):
    route = allowlist.route_for(method, path)
    assert route is not None and route.kind == "forward" and route.target == path
    other = "POST" if method == "GET" else "GET"
    assert allowlist.route_for(other, path) is None


def test_the_allowlist_is_the_contract():
    assert allowlist.FORWARDED == {
        "/plugin/laya/review/recent": "GET",
        "/plugin/laya/review/answer": "POST",
        "/plugin/laya/review/undo": "POST",
        "/ledger/search": "GET",
        "/ledger/conversation": "GET",
        "/ledger/threads": "GET",
        "/ledger/ingest": "POST",
        "/topic-labels": "GET",
    }


def test_a_full_page_of_the_join_fits_one_target():
    # The app asks for a whole RECENT page at once: 100 ids, the longest kind (a conversation id), comma-joined raw.
    ids = ",".join(f"{i:08x}-0000-4000-8000-{i:012x}" for i in range(100))
    target = f"/ledger/threads?c={ids}"
    assert len(target) <= allowlist.MAX_TARGET_CHARS
    route = allowlist.route_for("GET", target)
    assert route is not None and route.kind == "forward" and route.path == "/ledger/threads"


def test_health_is_local_and_get_only():
    assert allowlist.route_for("GET", "/health").kind == "health"
    assert allowlist.route_for("POST", "/health") is None


@pytest.mark.parametrize(
    "target",
    [
        "/plugin/laya/review/recent/",  # trailing slash
        "/plugin/laya/review/recent/extra",  # trailing segment
        "/plugin/laya/review/recentx",  # longer name
        "/plugin/laya/review",  # a prefix
        "/plugin/laya/review/queue",  # a sibling laya route that is NOT allowed
        "/plugin/laya/ops",
        "//plugin/laya/review/recent",  # double slash, or a network-path reference
        "/plugin//laya/review/recent",
        "/plugin/laya/review/./recent",
        "/plugin/laya/x/../review/recent",
        "/plugin/laya/review/%72ecent",  # an encoded letter
        "/plugin/laya/review/%2e%2e/review/recent",
        "/plugin/laya/review/%252e%252e/recent",  # double-encoded
        "/plugin%2flaya/review/recent",  # an encoded slash
        "/plugin/laya/review/recent%00",
        "/plugin/laya/review/recent;x=1",
        "/plugin/laya/review/recent#frag",
        "/plugin\\laya\\review\\recent",
        "/Plugin/laya/review/recent",  # case
        "/LEDGER/SEARCH",
        "http://127.0.0.1:7682/plugin/laya/review/recent",  # absolute form
        "plugin/laya/review/recent",
        "*",
        "",
        "/spawn",
        "/kill-session",
        "/shutdown",
        "/snapshot",
        "/ws",
        "/plugin/worktree/create",
        "/hook",
        "/" + "a" * 5000,
    ],
)
def test_anything_else_is_not_allowed(target):
    assert allowlist.route_for("GET", target) is None
    assert allowlist.route_for("POST", target) is None


def test_the_query_is_kept_but_re_encoded():
    route = allowlist.route_for("GET", "/ledger/search?q=hello+world&offset=10&q2=%2Fx")
    assert route.path == "/ledger/search"
    assert route.target == "/ledger/search?q=hello+world&offset=10&q2=%2Fx"
    assert allowlist.route_for("GET", "/ledger/search?q=a%20b").target == "/ledger/search?q=a+b"
    assert allowlist.route_for("GET", "/ledger/search?").target == "/ledger/search"
    too_many = "&".join(f"k{i}=v" for i in range(allowlist.MAX_QUERY_FIELDS + 1))
    assert allowlist.route_for("GET", f"/ledger/search?{too_many}") is None
