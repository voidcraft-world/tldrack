"""The bridge's typist (pane_typist.py): it types only into a free agent's empty input, and a queued
line is pinned to the agent it was meant for."""

import pytest

from tldrack.ledger.claude_fold import origin_of
from tldrack.pane_typist import (
    COMPOSER,
    KEYS_REFUSED,
    SUBMIT_TRIES,
    UNSUBMITTED,
    PaneTypist,
    claude_draft,
    decide_typing,
    one_line,
    still_drafted,
    submitted,
    type_line,
)
from tldrack.rack_db import RackDb

BOX = "────────────────\n❯  \n────────────────\n"


@pytest.mark.parametrize(
    "cli, state, pane, held",
    [
        ("claude", "free", BOX, False),
        ("claude", "busy", BOX, True),
        ("claude", "background", BOX, True),
        ("claude", "attention", BOX, True),
        ("claude", None, BOX, True),
        ("claude", "free", BOX.replace("❯  ", "❯ a draft"), True),
        ("claude", "free", "user@mac ~ % ", True),
        ("claude", "free", "", True),
        ("codex", "free", f"› {COMPOSER}", False),
        ("codex", "free", "› half typed", True),
        ("codex", "free", f"Do you trust the files in this folder?\n{COMPOSER}", True),
        ("shell", "free", BOX, True),
    ],
)
def test_decide_typing(cli, state, pane, held):
    assert (decide_typing(cli, state, pane) is not None) is held


def test_draft_reader_and_one_line():
    assert claude_draft(BOX) == ""
    assert claude_draft(BOX.replace("❯  ", "❯ fix the\nnav bar")) == "fix the nav bar", "a multi-line draft"
    assert one_line("a\nb\n\nc") == "a b c" and len(one_line("x" * 5000)) == 2000


def test_flush_types_once_and_drops_a_line_meant_for_another_agent(tmp_path):
    typed = []
    panes = {"wt-a": BOX, "wt-b": BOX}
    typist = PaneTypist(RackDb(tmp_path / "rack.sqlite"), capture=panes.get, send=lambda s, cli, text: typed.append((s, text)))
    typist.queue("wt-a", "id-a", "tell", "<lead-message/> one")
    typist.queue("wt-a", "id-a", "tell", "<lead-message/> two")
    typist.queue("wt-b", "id-b", "tell", "<lead-message/> for b")
    rows = {
        "wt-a": {"live": True, "state": "free", "lineage": {"id": "id-a"}, "profile": "a"},
        "wt-b": {"live": True, "state": "free", "lineage": {"id": "someone-else"}, "profile": "a"},
    }
    typist.flush(rows, lambda row: "claude")
    assert typed == [("wt-a", "<lead-message/> one")], "one line per session per pass; b's name was reused"
    typist.flush(rows, lambda row: "claude")
    assert typed[-1] == ("wt-a", "<lead-message/> two")
    assert typist.pending() == [], "b's line was dropped, not kept for whoever wears the name"


def test_rack_lines_are_never_read_as_the_operator():
    assert origin_of({}, '<rack-wake run="R1104-aaaa" n="2"/> S1 ready. Run: tldrack lead status', "main") == "peer"
    assert origin_of({}, '<lead-message from="lead-one" run="R" step="S1"/> use 410', "main") == "peer"
    assert origin_of({}, "please fix the nav", "main") == "human"


# ── a worker's first brief on `unknown` (§ WHEN IT IS SAFE) ─────────────────────

IDLE_CODEX = f"  >_ OpenAI Codex\n\n› {COMPOSER}\n  GPT-5-Codex high · lead/r1 · No changes\n  ? for shortcuts\n"
WORKING_CODEX = f"• Working (12s • esc to interrupt)\n\n› {COMPOSER}\n  ? for shortcuts\n"
SPINNER_CLAUDE = "✻ Pondering… (esc to interrupt)\n" + BOX


def drafted_box(text: str) -> str:
    return f"────────────────\n❯ {text}\n────────────────\n"


@pytest.mark.parametrize(
    "cli, state, pane, first_brief, held",
    [
        ("codex", "unknown", IDLE_CODEX, True, False),
        ("codex", None, IDLE_CODEX, True, False),
        ("codex", "free", IDLE_CODEX, True, False),
        ("codex", "unknown", WORKING_CODEX, True, True),
        ("codex", "unknown", "› half typed\n  ? for shortcuts", True, True),
        ("codex", "unknown", f"Do you trust the files in this folder?\n› {COMPOSER}", True, True),
        ("codex", "unknown", None, True, True),
        ("codex", "unknown", "", True, True),
        ("codex", "busy", IDLE_CODEX, True, True),
        ("codex", "attention", IDLE_CODEX, True, True),
        ("codex", "background", IDLE_CODEX, True, True),
        ("codex", "unknown", IDLE_CODEX, False, True),
        ("codex", None, IDLE_CODEX, False, True),
        ("claude", "unknown", BOX, True, False),
        ("claude", "unknown", SPINNER_CLAUDE, True, True),
        ("claude", "unknown", drafted_box("a draft"), True, True),
        ("shell", "unknown", IDLE_CODEX, True, True),
    ],
)
def test_a_first_brief_may_go_on_unknown_only_into_an_idle_empty_input(cli, state, pane, first_brief, held):
    assert (decide_typing(cli, state, pane, first_brief=first_brief) is not None) is held


def test_a_running_turn_is_named_as_the_hold():
    assert decide_typing("codex", "unknown", WORKING_CODEX, first_brief=True) == "a turn is running in the pane"
    assert decide_typing("codex", "unknown", IDLE_CODEX) == "the agent is unknown", "a wake or tell still needs free"


def _codex_rows(session="wt-c", lineage="id-c", state="unknown"):
    return {session: {"live": True, "state": state, "lineage": {"id": lineage}, "profile": "codex"}}


def test_an_unpinned_brief_waits_for_its_pin_then_goes_on_unknown(tmp_path):
    typed = []
    typist = PaneTypist(
        RackDb(tmp_path / "rack.sqlite"), capture=lambda s: IDLE_CODEX, send=lambda s, cli, text: typed.append((s, cli, text))
    )
    typist.queue("wt-c", None, "brief", "<lead-message/> your brief")
    assert typist.flush(_codex_rows(), lambda row: "codex") == []
    assert typist.pending()[0]["hold"] == "the worker is not proven yet"
    assert typist.pin("wt-c", "id-c") == 1
    typist.flush(_codex_rows(), lambda row: "codex")
    assert typed == [("wt-c", "codex", "<lead-message/> your brief")]
    assert typist.pending() == []


def test_a_pinned_brief_never_reaches_a_reused_name(tmp_path):
    typed = []
    typist = PaneTypist(RackDb(tmp_path / "rack.sqlite"), capture=lambda s: IDLE_CODEX, send=lambda s, cli, text: typed.append(s))
    typist.queue("wt-c", None, "brief", "<lead-message/> brief")
    typist.pin("wt-c", "id-c")
    typist.flush(_codex_rows(lineage="someone-else"), lambda row: "codex")
    assert typed == [] and typist.pending() == []


def test_a_held_line_holds_the_ones_behind_it(tmp_path):
    typed = []
    typist = PaneTypist(
        RackDb(tmp_path / "rack.sqlite"), capture=lambda s: IDLE_CODEX, send=lambda s, cli, text: typed.append(text)
    )
    typist.queue("wt-c", None, "brief", "<lead-message/> brief")
    typist.queue("wt-c", "id-c", "tell", "<lead-message/> tell")
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    assert typed == [], "the tell may not overtake the brief it builds on"
    assert [p["hold"] for p in typist.pending()] == ["the worker is not proven yet", None]
    typist.pin("wt-c", "id-c")
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    assert typed == ["<lead-message/> brief", "<lead-message/> tell"]


def test_drop_pending_by_kind_and_unpinned(tmp_path):
    typist = PaneTypist(RackDb(tmp_path / "rack.sqlite"), capture=lambda s: None, send=lambda *a: None)
    typist.queue("wt-c", None, "brief", "old brief")
    typist.queue("wt-c", "id-c", "tell", "a tell")
    assert typist.drop_pending("wt-c", "superseded", kind="brief") == 1
    assert typist.drop_pending("wt-c", "gone", unpinned_only=True) == 0
    assert [p["kind"] for p in typist.pending()] == ["tell"]


# ── submit confirm (§ SUBMITTED, OR IT SAYS SO) ───────────────────────────────

LINE = '<lead-message from="lead-a" run="R1" step="S1"/> your brief is ready'
DRAFTED_CODEX = f"  >_ OpenAI Codex\n\n› {LINE}\n  ? for shortcuts\n"
APPROVAL_CODEX = "Allow command?\n› 1. Yes, proceed\n  2. No, and tell Codex what to do differently\n"


class FakeTmux:
    """`run` records argv; `capture` replays the panes the agent would draw after each Enter."""

    def __init__(self, *after_enter: str | None, refuse: bool = False) -> None:
        self.after_enter = list(after_enter)
        self.calls: list[list[str]] = []
        self.refuse = refuse
        self.enters = 0

    def run(self, argv: list[str]) -> bool:
        self.calls.append(argv)
        if argv[-1] == "Enter":
            self.enters += 1
        return not self.refuse

    def capture(self, session: str) -> str | None:
        index = min(self.enters, len(self.after_enter)) - 1
        return self.after_enter[index] if index >= 0 else None


def _type(cli: str, tmux: FakeTmux) -> str | None:
    return type_line("wt-c", cli, LINE, run=tmux.run, capture=tmux.capture, sleep=lambda s: None)


def test_submit_is_confirmed_by_the_input_coming_back_empty():
    tmux = FakeTmux(IDLE_CODEX)
    assert _type("codex", tmux) is None and tmux.enters == 1
    assert tmux.calls[1][:3] == ["tmux", "paste-buffer", "-p"], "Codex gets a bracketed paste"
    tmux = FakeTmux(BOX)
    assert _type("claude", tmux) is None and tmux.enters == 1
    assert tmux.calls[0][-2:] == ["--", LINE], "Claude gets the text as literal keys"


def test_a_line_still_in_the_input_gets_another_enter_bounded():
    tmux = FakeTmux(DRAFTED_CODEX, WORKING_CODEX)
    assert _type("codex", tmux) is None and tmux.enters == 2, "a running turn proves it left"
    tmux = FakeTmux(DRAFTED_CODEX)
    assert _type("codex", tmux) == UNSUBMITTED, "an honest answer: in the input, not sent"
    assert tmux.enters == SUBMIT_TRIES
    tmux = FakeTmux(drafted_box(LINE), BOX)
    assert _type("claude", tmux) is None and tmux.enters == 2


def test_no_enter_is_pressed_into_a_screen_that_does_not_show_our_line():
    tmux = FakeTmux(APPROVAL_CODEX)
    assert _type("codex", tmux) == UNSUBMITTED, "the keys went in: it must never be typed again"
    assert tmux.enters == 1, "an Enter here would approve the command"
    tmux = FakeTmux(None)
    assert _type("codex", tmux) == UNSUBMITTED and tmux.enters == 1, "an unreadable pane is not proof of anything"


def test_a_refused_paste_presses_no_enter_and_may_be_typed_again():
    tmux = FakeTmux(IDLE_CODEX, refuse=True)
    assert _type("codex", tmux) == KEYS_REFUSED and tmux.enters == 0


def test_still_drafted_reads_pasted_stubs_and_ignores_history():
    assert still_drafted("codex", LINE, "› [Pasted Content 1834 chars]\n  ? for shortcuts")
    assert still_drafted("claude", LINE, drafted_box("[Pasted text #1 +12 lines]"))
    history = f"› {LINE}\n\nsome reply\n\n› 1. Yes, proceed\n"
    assert not still_drafted("codex", LINE, history), "the composer is the LAST › line"
    assert not submitted("codex", DRAFTED_CODEX) and submitted("codex", IDLE_CODEX)


def test_an_unsubmitted_line_is_never_typed_twice(tmp_path):
    sends, resubmits = [], []
    panes = {"wt-c": IDLE_CODEX}

    def send(session, cli, text):
        sends.append(text)
        panes["wt-c"] = DRAFTED_CODEX  # it went in, but Enter never took
        return UNSUBMITTED

    typist = PaneTypist(
        RackDb(tmp_path / "rack.sqlite"),
        capture=panes.get,
        send=send,
        resubmit=lambda session, cli, text: resubmits.append(text) or False,
    )
    typist.queue("wt-c", "id-c", "brief", LINE)
    typist.flush(_codex_rows(), lambda row: "codex")
    assert typist.pending()[0]["hold"] == UNSUBMITTED
    typist.flush(_codex_rows(), lambda row: "codex")
    assert sends == [LINE] and resubmits == [LINE], "the second pass presses Enter again, it does not retype"
    panes["wt-c"] = IDLE_CODEX  # the operator pressed Enter
    typist.flush(_codex_rows(), lambda row: "codex")
    assert sends == [LINE] and typist.pending() == []


def test_an_unsubmitted_line_settles_when_a_turn_has_run(tmp_path):
    panes = {"wt-c": IDLE_CODEX}

    def send(session, cli, text):
        panes["wt-c"] = DRAFTED_CODEX
        return UNSUBMITTED

    typist = PaneTypist(RackDb(tmp_path / "rack.sqlite"), capture=panes.get, send=send)
    typist.queue("wt-c", "id-c", "tell", LINE)
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    typist.flush(_codex_rows(state="busy"), lambda row: "codex")
    assert typist.pending() == []


class ApprovalAfterEnter:
    """A Codex that takes the line and goes straight to an approval prompt: the first Enter lands, and
    the pane never shows the line or an empty composer until the prompt is answered."""

    def __init__(self) -> None:
        self.pane = IDLE_CODEX
        self.pastes = 0
        self.enters = 0

    def run(self, argv: list[str]) -> bool:
        if argv[1] == "paste-buffer":
            self.pastes += 1
            self.pane = DRAFTED_CODEX
        elif argv[-1] == "Enter":
            self.enters += 1
            if self.pane == DRAFTED_CODEX:
                self.pane = APPROVAL_CODEX
        return True

    def capture(self, session: str) -> str | None:
        return self.pane


def test_a_line_that_went_through_to_an_approval_prompt_is_never_retyped(tmp_path):
    codex = ApprovalAfterEnter()
    resubmits = []
    typist = PaneTypist(
        RackDb(tmp_path / "rack.sqlite"),
        capture=codex.capture,
        send=lambda s, cli, text: type_line(s, cli, text, run=codex.run, capture=codex.capture, sleep=lambda _: None),
        resubmit=lambda s, cli, text: resubmits.append(text) or False,
    )
    typist.queue("wt-c", "id-c", "tell", LINE)
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    assert typist.pending()[0]["hold"] == UNSUBMITTED
    typist.flush(_codex_rows(state="free"), lambda row: "codex")  # a stale `free`, the prompt still up
    assert codex.enters == 1 and resubmits == [], "no Enter on the prompt"
    codex.pane = IDLE_CODEX  # answered; the turn ran and ended
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    assert typist.pending() == [], "delivered"
    assert codex.pastes == 1 and codex.enters == 1, "never retyped"


def test_refused_keys_leave_the_line_to_be_typed_again(tmp_path):
    sends = []
    typist = PaneTypist(
        RackDb(tmp_path / "rack.sqlite"),
        capture=lambda s: IDLE_CODEX,
        send=lambda s, cli, text: sends.append(text) or (KEYS_REFUSED if len(sends) == 1 else None),
    )
    typist.queue("wt-c", "id-c", "tell", LINE)
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    assert typist.pending()[0]["hold"] == KEYS_REFUSED
    typist.flush(_codex_rows(state="free"), lambda row: "codex")
    assert sends == [LINE, LINE] and typist.pending() == []
