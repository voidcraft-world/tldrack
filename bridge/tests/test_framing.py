"""framing.py: a bridge page may be framed only by a page that may call the bridge."""

from tldrack import framing

LOOPBACK = "'self' http://localhost:* https://localhost:* http://127.0.0.1:* https://127.0.0.1:*"


def test_loopback_only_by_default():
    assert framing.frame_ancestors(()) == f"frame-ancestors {LOOPBACK}"


def test_the_operators_origins_are_added_sorted_and_once():
    allowed = {"https://rack.example", "https://mac.tail.example:8443", "http://dev.example:5173"}
    assert framing.frame_ancestors(allowed) == (
        f"frame-ancestors {LOOPBACK} http://dev.example:5173 https://mac.tail.example:8443 https://rack.example"
    )


def test_what_csp_cannot_spell_is_left_out_and_named():
    allowed = {"https://[fd7a:115c::1]", "https://rack.example", "https://rack.example/path", "https://a;b.example"}
    assert framing.framer_origins(allowed) == ("https://rack.example",)
    assert framing.unframeable(allowed) == ("https://[fd7a:115c::1]", "https://a;b.example", "https://rack.example/path")
    assert ";" not in framing.frame_ancestors(allowed), "an entry can never add a directive of its own"
