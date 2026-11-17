"""A read from the bridge's own page states its origin before any guard runs, and nothing else does."""

import pytest

from tldrack.own_page import own_page_origin


@pytest.mark.parametrize(
    "host, origin",
    [
        ("127.0.0.1:7682", "http://127.0.0.1:7682"),
        ("localhost:7682", "http://localhost:7682"),
        ("LOCALHOST:7682", "http://localhost:7682"),
        ("[::1]:7682", "http://[::1]:7682"),
        ("127.0.0.1:80", "http://127.0.0.1"),  # the spelling a browser sends: no default port
    ],
)
def test_a_same_origin_read_of_a_loopback_host_is_that_hosts_origin(host, origin):
    assert own_page_origin(None, "same-origin", host) == origin
    assert own_page_origin(None, " Same-Origin ", host) == origin


@pytest.mark.parametrize("site", ["cross-site", "same-site", "none", ""])
def test_every_other_fetch_site_infers_nothing(site):
    assert own_page_origin(None, site, "127.0.0.1:7682") is None


def test_a_native_caller_and_a_stated_origin_are_left_as_they_came():
    assert own_page_origin(None, None, "127.0.0.1:7682") is None  # curl, a hook, the CLI
    assert own_page_origin("https://rack.example", "same-origin", "127.0.0.1:7682") is None


@pytest.mark.parametrize("host", [None, "", "rebind.evil.example:7682", "mymac.tail1234.ts.net", "127.0.0.1:7682/x", "a b"])
def test_a_host_that_is_not_loopback_infers_nothing(host):
    assert own_page_origin(None, "same-origin", host) is None
