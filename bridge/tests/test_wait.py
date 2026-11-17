from tldrack.snapshot import TMUX_FIELD_SEP
from tldrack.wait import (
    DEFAULT_TIMEOUT_MS,
    MAX_TIMEOUT_MS,
    parse_session_ids,
    parse_timeout_ms,
    parse_wait_states,
    wait_outcome,
)


def _sess(name, session_id):
    return TMUX_FIELD_SEP.join((name, session_id))


def test_parse_session_ids_maps_names_to_ids():
    output = "\n".join([_sess("NOTES", "$0"), _sess("voidcraft", "$1")])
    assert parse_session_ids(output) == {"NOTES": "$0", "voidcraft": "$1"}


def test_parse_session_ids_skips_malformed_ids():
    # Anything not shaped like tmux's `$N` is dropped rather than pinned on —
    # a bad pin silently disables the replacement guard.
    output = "\n".join([_sess("good", "$4"), _sess("bad", "4"), _sess("worse", ""), "no separator"])
    assert parse_session_ids(output) == {"good": "$4"}


def test_parse_session_ids_no_server():
    assert parse_session_ids("") == {}


def test_parse_wait_states_single_and_multiple():
    assert parse_wait_states("free") == {"free"}
    assert parse_wait_states("free,attention") == {"free", "attention"}
    assert parse_wait_states(" free , attention ") == {"free", "attention"}


def test_parse_wait_states_rejects_unknown_words():
    # A caller waiting for a state that can never arrive would block until the
    # timeout and then look like a hung agent — 400 instead.
    assert parse_wait_states("idle") is None
    assert parse_wait_states("free,bogus") is None
    assert parse_wait_states("") is None
    assert parse_wait_states(None) is None
    assert parse_wait_states(",") is None


def test_parse_wait_states_allows_unknown_state():
    # "the bridge lost track of this session" is a legitimate thing to wait for.
    assert parse_wait_states("unknown") == {"unknown"}


def test_parse_timeout_clamps_and_defaults():
    assert parse_timeout_ms(None) == DEFAULT_TIMEOUT_MS
    assert parse_timeout_ms("nonsense") == DEFAULT_TIMEOUT_MS
    assert parse_timeout_ms("5000") == 5000
    assert parse_timeout_ms(str(MAX_TIMEOUT_MS * 10)) == MAX_TIMEOUT_MS


def test_parse_timeout_never_unbounded():
    # 0 / negative must not mean "wait forever" — one wedged agent would pin a
    # client and a server thread indefinitely.
    assert parse_timeout_ms("0") == DEFAULT_TIMEOUT_MS
    assert parse_timeout_ms("-1") == DEFAULT_TIMEOUT_MS


def test_wait_outcome_matches_wanted_state():
    assert wait_outcome("free", {"free"}, "$3", "$3") == "matched"
    assert wait_outcome("busy", {"free"}, "$3", "$3") == "waiting"
    assert wait_outcome("attention", {"free", "attention"}, "$3", "$3") == "matched"


def test_wait_outcome_replacement_beats_a_matching_state():
    # THE guard: kill a wedged session, start a fresh one under the same name,
    # and it must not report the ORIGINAL work as finished.
    assert wait_outcome("free", {"free"}, "$3", "$9") == "replaced"
    assert wait_outcome("busy", {"free"}, "$3", "$9") == "replaced"


def test_wait_outcome_session_disappearing_counts_as_replaced():
    assert wait_outcome("free", {"free"}, "$3", None) == "replaced"


def test_wait_outcome_store_only_session_has_nothing_to_pin():
    # No tmux presence when the wait began → only the state matters, and a
    # session APPEARING later must not read as a replacement.
    assert wait_outcome("free", {"free"}, None, None) == "matched"
    assert wait_outcome("free", {"free"}, None, "$7") == "matched"
    assert wait_outcome("busy", {"free"}, None, "$7") == "waiting"
