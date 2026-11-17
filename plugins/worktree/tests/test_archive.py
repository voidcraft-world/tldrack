"""Archived branch tips (`worktree/archive.py`): the listing parser and the
three decisions — never a fork."""

from worktree import archive as R
from worktree.branches import FIELD_SEP


def oid(seed: str) -> str:
    return (seed + "0" * 40)[:40]


def listed(branch: str, commit: str, stamp: str, subject: str) -> str:
    return FIELD_SEP.join((f"{R.ARCHIVE_PREFIX}{branch}", commit, stamp, subject))


def test_the_listing_is_newest_first_and_total():
    text = "\n".join(
        [
            listed("old", oid("a1"), "100", "first"),
            listed("feat/new", oid("b2"), "200", "second"),
            listed("bad", "notanoid", "1", "x"),
            FIELD_SEP.join(("refs/heads/x", oid("c3"), "1", "not ours")),
            "garbage",
        ]
    )
    rows = R.parse_list(text)
    assert [r["branch"] for r in rows] == ["feat/new", "old"]
    assert rows[0] == {"branch": "feat/new", "oid": oid("b2"), "sha": oid("b2")[:9], "committed_at": 200, "subject": "second"}
    assert R.parse_list(None) == []


def legacy(branch: str, commit: str, stamp: str, subject: str) -> str:
    return FIELD_SEP.join((f"{R.LEGACY_ARCHIVE_PREFIX}{branch}", commit, stamp, subject))


def test_new_pins_are_written_under_the_tldrack_name_and_both_are_read():
    assert R.archive_ref("feat/x") == "refs/tldrack/archived/feat/x"
    command = R.build_list_command("/r")
    assert command[-2:] == ["refs/tldrack/archived", "refs/voidcraft/archived"]
    assert R.build_list_command("/r", "feat/x")[-2:] == ["refs/tldrack/archived/feat/x", "refs/voidcraft/archived/feat/x"]


def test_a_legacy_pin_is_listed_and_a_branch_pinned_twice_shows_once_at_its_newer_tip():
    text = "\n".join(
        [
            legacy("old", oid("a1"), "100", "legacy only"),
            legacy("both", oid("b1"), "300", "legacy newer"),
            listed("both", oid("b2"), "200", "new older"),
            listed("tie", oid("c2"), "50", "new"),
            legacy("tie", oid("c1"), "50", "legacy"),
        ]
    )
    rows = R.parse_list(text)
    assert [(r["branch"], r["oid"]) for r in rows] == [("both", oid("b1")), ("old", oid("a1")), ("tie", oid("c2"))]
    pins = [p for p in R.parse_pins(text) if p["branch"] == "both"]
    assert R.newest_tip(pins) == oid("b1") and R.refs_holding(pins, oid("b1")) == ["refs/voidcraft/archived/both"]
    assert R.written_pin(pins, "both") == oid("b2") and R.written_pin([], "both") is None


def test_the_pin_must_not_exist_unless_its_old_value_is_named():
    fresh = R.build_pin_command("/r", "b", oid("7e"), None)
    assert fresh[-3:] == [f"{R.ARCHIVE_PREFIX}b", oid("7e"), "0" * 40]
    again = R.build_pin_command("/r", "b", oid("7e"), oid("0d"))
    assert again[-1] == oid("0d")


def test_restore_creates_only_an_absent_branch_from_an_oid():
    command = R.build_restore_command("/r", "feat/x", oid("7e"))
    assert command[3] == "update-ref" and command[-3:] == ["refs/heads/feat/x", oid("7e"), "0" * 40]


class TestDecidePin:
    def test_no_pin_writes_and_the_same_tip_is_a_no_op(self):
        assert R.decide_pin(existing=None, tip=oid("7e"), existing_in_tip=None) == "write"
        assert R.decide_pin(existing=oid("7e"), tip=oid("7e"), existing_in_tip=None) == "same"

    def test_an_old_pin_inside_the_new_tip_is_overwritten(self):
        assert R.decide_pin(existing=oid("0d"), tip=oid("7e"), existing_in_tip=True) == "write"

    def test_divergent_or_unknown_refuses(self):
        assert R.decide_pin(existing=oid("0d"), tip=oid("7e"), existing_in_tip=False) == "conflict"
        assert R.decide_pin(existing=oid("0d"), tip=oid("7e"), existing_in_tip=None) == "conflict"


class TestDecideRestore:
    def test_the_refusal_table_in_order(self):
        assert R.decide_restore(name_error="bad", pinned=oid("7e"), branch_exists=False, is_base=False)[0] == 400
        assert R.decide_restore(name_error=None, pinned=None, branch_exists=False, is_base=False)[0] == 404
        assert R.decide_restore(name_error=None, pinned=oid("7e"), branch_exists=False, is_base=True)[0] == 409
        assert R.decide_restore(name_error=None, pinned=oid("7e"), branch_exists=True, is_base=False)[0] == 409
        assert R.decide_restore(name_error=None, pinned=oid("7e"), branch_exists=False, is_base=False) is None


class TestDecideForget:
    def test_only_the_exact_tip_shown_is_forgotten(self):
        assert R.decide_forget(name_error=None, pinned=oid("7e"), expected=oid("7e")) is None
        assert R.decide_forget(name_error=None, pinned=oid("7e"), expected=oid("7e")[:9])[0] == 409
        assert R.decide_forget(name_error=None, pinned=oid("7e"), expected=None)[0] == 409
        assert R.decide_forget(name_error=None, pinned=None, expected=oid("7e"))[0] == 404
