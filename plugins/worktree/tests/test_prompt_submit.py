"""The first prompt is SUBMITTED, or the job says it was not (create.py § SUBMITTED).

The panes below are the frames a fresh Claude drew when the bug was measured:
the box with a draft, the box a long draft has scrolled, the burst filed as a
paste, and a turn running. Nothing here spawns: `run_bounded` is scripted.
"""

import pytest

from worktree import create as create_mod
from worktree.create import (
    PROMPT_NOT_TYPED,
    PROMPT_UNSUBMITTED,
    SUBMIT_LOOKS,
    SUBMIT_TRIES,
    input_box,
    prompt_drafted,
    prompt_submitted,
    send_prompt,
    submit_prompt,
)

RULE = "─" * 60
FOOTER = "  ⏵⏵ auto mode on (shift+tab to cycle)"
PROMPT = '<lead-message from="lead" run="R1" step="s1"/> You are a worker on LEAD run R1, step s1: fix the nav tooltip.'


def box(*rows: str, footer: str = FOOTER) -> str:
    """The usual frame: a caret line under a rule, the draft's rows, a rule."""
    first, *rest = rows or ("",)
    return "\n".join(["▐▛███▜▌ banner", "", RULE, f"❯ {first}", *(f"  {row}" for row in rest), RULE, footer])


def scrolled(*rows: str, hidden: int = 2) -> str:
    """A draft taller than the box: the caret line is off screen."""
    top = f"─── ↑ {hidden} more " + "─" * 40
    return "\n".join(["▐▛███▜▌ banner", top, *(f"  {row}" for row in rows), RULE, FOOTER])


EMPTY = box()
DRAFTED = box(PROMPT[:70], PROMPT[70:])
WORKING = box(footer=f"{FOOTER} · esc to interrupt")
DIALOG = "Do you want to proceed?\n❯ 1. Yes\n  2. No\n\nEsc to cancel"
SHELL = "someone@mac demo-app % "


class TestInputBox:
    def test_an_empty_box(self):
        assert input_box(EMPTY) == ("", False)

    def test_the_padding_of_an_empty_caret_line_is_not_a_draft(self):
        assert input_box(box(" ")) == ("", False)

    def test_a_wrapped_draft_is_one_text(self):
        assert input_box(box("fix the nav", "tooltip now")) == ("fix the nav tooltip now", False)

    def test_a_scrolled_box_shows_the_drafts_end(self):
        assert input_box(scrolled("the nav tooltip", "and ship it")) == ("the nav tooltip and ship it", True)

    def test_the_last_frame_in_the_pane_wins(self):
        assert input_box(f"{box('an older draft')}\n{box('the live one')}") == ("the live one", False)

    def test_a_scrolled_rule_with_no_frame_under_it_is_output(self):
        assert input_box("─── ↑ 2 more ──────────\nsome output\n") is None

    @pytest.mark.parametrize("pane", [DIALOG, SHELL, "", None, 42])
    def test_no_box(self, pane):
        assert input_box(pane) is None


class TestPromptDrafted:
    def test_the_prompt_in_the_box(self):
        assert prompt_drafted(PROMPT, DRAFTED)

    def test_a_wrap_inside_a_word_still_matches(self):
        assert prompt_drafted(PROMPT, box(PROMPT[:13], PROMPT[13:40], PROMPT[40:]))

    def test_a_scrolled_box_matches_on_the_prompts_end(self):
        # The caret line, and with it the prompt's start, is off screen.
        assert prompt_drafted(PROMPT, scrolled(PROMPT[-70:-30], PROMPT[-30:]))

    def test_a_burst_filed_as_a_paste(self):
        assert prompt_drafted(PROMPT, box("[Pasted text #1 +0 lines]"))

    def test_a_paste_followed_by_the_rest_typed(self):
        # The measured frame: the start became the paste, the tail arrived as keys.
        assert prompt_drafted(PROMPT, box(f"[Pasted text #1]{PROMPT[-40:]}"))

    def test_a_short_prompt_is_matched_whole(self):
        assert prompt_drafted("fix it", box("fix it"))

    def test_an_empty_box_is_not_a_draft(self):
        assert not prompt_drafted(PROMPT, EMPTY)

    def test_someone_elses_draft(self):
        assert not prompt_drafted(PROMPT, box("wait, do not start yet, I want to change the plan"))

    def test_a_stray_keystroke_is_not_ours(self):
        # `a` occurs in the prompt; one character is not the prompt.
        assert not prompt_drafted(PROMPT, box("a"))

    def test_a_paste_beside_foreign_text_is_not_ours(self):
        assert not prompt_drafted(PROMPT, box("my own note [Pasted text #1 +3 lines]"))

    @pytest.mark.parametrize("pane", [DIALOG, SHELL, "", None])
    def test_never_on_a_screen_with_no_box(self, pane):
        # An Enter on a dialog picks its highlighted option.
        assert not prompt_drafted(PROMPT, pane)


class TestPromptSubmitted:
    def test_a_running_turn(self):
        assert prompt_submitted(WORKING)

    def test_the_box_is_empty_again(self):
        assert prompt_submitted(EMPTY)

    def test_a_draft_still_in_the_box(self):
        assert not prompt_submitted(DRAFTED)
        assert not prompt_submitted(scrolled(PROMPT[-40:]))

    @pytest.mark.parametrize("pane", [DIALOG, SHELL, "", None])
    def test_a_screen_with_no_box_proves_nothing(self, pane):
        assert not prompt_submitted(pane)


class Pane:
    """A scripted tmux: each capture answers the next pane (the last one repeats);
    every other call is recorded. `None` is a session that died."""

    def __init__(self, monkeypatch, panes, *, refuse=()):
        self.panes, self.refuse = list(panes), refuse
        self.looks, self.keys = 0, []
        monkeypatch.setattr(create_mod, "run_bounded", self.run)
        monkeypatch.setattr(create_mod.time, "sleep", lambda _seconds: None)

    def run(self, command, _timeout, **_kwargs):
        assert command[:2] in (["tmux", "capture-pane"], ["tmux", "send-keys"]) and "=wt-x:" in command
        if command[1] == "capture-pane":
            pane = self.panes[min(self.looks, len(self.panes) - 1)]
            self.looks += 1
            return (1, "", "can't find pane") if pane is None else (0, pane, "")
        key = "Enter" if command[-1] == "Enter" and "-l" not in command else "text"
        self.keys.append(key)
        return (1, "", "refused") if key in self.refuse else (0, "", "")


ENTER = ["tmux", "send-keys", "-t", "=wt-x:", "Enter"]


class TestSubmitPrompt:
    def test_it_waits_until_the_pane_shows_the_line(self, monkeypatch):
        # The bug: the frame is drawn before the TUI reads input. An empty box
        # before any Enter is a prompt not painted yet, never "submitted".
        pane = Pane(monkeypatch, [EMPTY, EMPTY, DRAFTED, WORKING])
        assert submit_prompt("wt-x", PROMPT, ENTER) is True
        assert pane.keys == ["Enter"] and pane.looks == 4

    def test_a_swallowed_enter_is_pressed_again(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED, DRAFTED, WORKING])
        assert submit_prompt("wt-x", PROMPT, ENTER) is True
        assert pane.keys == ["Enter", "Enter"]

    def test_an_empty_box_after_the_enter_is_proof_too(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED, EMPTY])
        assert submit_prompt("wt-x", PROMPT, ENTER) is True
        assert pane.keys == ["Enter"]

    def test_a_repaint_between_the_enter_and_the_turn_is_waited_out(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED, "", WORKING])
        assert submit_prompt("wt-x", PROMPT, ENTER) is True
        assert pane.keys == ["Enter"]

    def test_the_enters_are_bounded(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED])
        assert submit_prompt("wt-x", PROMPT, ENTER) is False
        assert pane.keys == ["Enter"] * SUBMIT_TRIES

    @pytest.mark.parametrize("screen", [DIALOG, SHELL, EMPTY, box("someone else's half-written line, not ours")])
    def test_no_enter_on_a_screen_that_does_not_show_the_line(self, monkeypatch, screen):
        pane = Pane(monkeypatch, [screen])
        assert submit_prompt("wt-x", PROMPT, ENTER) is False
        assert pane.keys == [] and pane.looks == SUBMIT_LOOKS

    def test_a_dialog_after_the_enter_gets_no_second_one(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED, DIALOG])
        assert submit_prompt("wt-x", PROMPT, ENTER) is False
        assert pane.keys == ["Enter"]

    def test_a_session_that_died_is_not_submitted(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED, None])
        assert submit_prompt("wt-x", PROMPT, ENTER) is False
        assert pane.keys == ["Enter"]

    def test_a_refused_enter_is_not_submitted(self, monkeypatch):
        pane = Pane(monkeypatch, [DRAFTED, WORKING], refuse=("Enter",))
        assert submit_prompt("wt-x", PROMPT, ENTER) is False
        assert pane.keys == ["Enter"]  # not pressed again


class TestSendPrompt:
    def test_types_once_then_submits(self, monkeypatch):
        pane = Pane(monkeypatch, [SHELL, EMPTY, EMPTY, DRAFTED, WORKING])
        assert send_prompt("wt-x", PROMPT) is None
        assert pane.keys == ["text", "Enter"]

    def test_the_enter_never_rides_straight_behind_the_text(self, monkeypatch):
        # What the old two-call burst did: a look sits between the two now.
        pane = Pane(monkeypatch, [EMPTY, DRAFTED, WORKING])
        seen = []
        run = pane.run
        monkeypatch.setattr(create_mod, "run_bounded", lambda command, *a, **k: seen.append(command[1]) or run(command, *a, **k))
        assert send_prompt("wt-x", PROMPT) is None
        assert seen == ["capture-pane", "send-keys", "capture-pane", "send-keys", "capture-pane"]

    def test_a_prompt_left_in_the_box_says_so_and_is_typed_once(self, monkeypatch):
        pane = Pane(monkeypatch, [EMPTY, DRAFTED])
        assert send_prompt("wt-x", PROMPT) == PROMPT_UNSUBMITTED
        assert pane.keys.count("text") == 1

    def test_a_pane_that_never_looks_ready_gets_nothing(self, monkeypatch):
        pane = Pane(monkeypatch, [SHELL])
        assert send_prompt("wt-x", PROMPT, ready_timeout=0.05) == PROMPT_NOT_TYPED
        assert pane.keys == []

    def test_a_dead_session_gets_nothing(self, monkeypatch):
        pane = Pane(monkeypatch, [None])
        assert send_prompt("wt-x", PROMPT) == PROMPT_NOT_TYPED
        assert pane.keys == []

    def test_refused_text_is_not_typed_and_gets_no_enter(self, monkeypatch):
        pane = Pane(monkeypatch, [EMPTY, DRAFTED], refuse=("text",))
        assert send_prompt("wt-x", PROMPT) == PROMPT_NOT_TYPED
        assert pane.keys == ["text"]
