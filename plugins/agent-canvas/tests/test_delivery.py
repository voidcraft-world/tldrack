"""The typing gate: every way an answer could land somewhere it must not."""

from __future__ import annotations

from agent_canvas.delivery import (
    QUESTION_MESSAGE_PREFIX,
    decide_delivery,
    format_reply,
    input_box_draft,
    pending_question,
)

RULE = "─" * 40
IDLE = f"some output\n{RULE}\n❯ \n{RULE}\n  92% left · 5h 53%\n"
DRAFT = f"some output\n{RULE}\n❯ half a thought\n{RULE}\n  92% left\n"
MULTILINE_DRAFT = f"{RULE}\n❯ line one\n  line two\n{RULE}\n"
SHELL = "someone@mac ~ % \n"


def test_empty_box_reads_empty_including_nbsp():
    assert input_box_draft(IDLE) == ""


def test_draft_is_seen_on_the_caret_line_and_below_it():
    assert input_box_draft(DRAFT) == "half a thought"
    assert input_box_draft(MULTILINE_DRAFT) == "line one line two"


def test_the_last_box_wins_over_earlier_scrollback():
    pane = f"{RULE}\n❯ an old echoed prompt\n{RULE}\nwork…\n{IDLE}"
    assert input_box_draft(pane) == ""


def test_idle_free_pane_sends():
    assert decide_delivery("free", None, IDLE) == ("send", None)
    assert decide_delivery("background", None, IDLE) == ("send", None)


def test_a_working_agent_queues():
    assert decide_delivery("busy", None, IDLE)[0] == "queue"


def test_foreign_attention_queues_because_enter_would_confirm_a_dialog():
    action, why = decide_delivery("attention", "Claude needs your permission to use Bash", IDLE)
    assert action == "queue" and "terminal" in why


def test_our_own_attention_is_safe_to_answer():
    assert decide_delivery("attention", f"{QUESTION_MESSAGE_PREFIX}Ship it?", IDLE) == ("send", None)


def test_a_draft_queues_rather_than_appending_to_it():
    assert decide_delivery("free", None, DRAFT) == ("queue", "a draft is in the input box")


def test_a_shell_never_gets_typed_into():
    assert decide_delivery("unknown", None, SHELL) == ("queue", "no Claude prompt in the pane")
    assert decide_delivery("free", None, None)[0] == "queue"


def test_reply_is_one_bounded_line_carrying_the_question():
    assert format_reply("Ship it?", "yes") == "canvas › Ship it? → yes"
    assert format_reply("", "  defer \n until monday ") == "canvas → defer until monday"
    assert format_reply("q", "   ") == ""
    assert "\n" not in format_reply("multi\nline", "a\nb")
    assert len(format_reply("q", "x" * 5000)) <= 2000
    # Typed with `send-keys -l`, so a control character would be a KEY: ESC [ Z is Shift+Tab.
    assert format_reply("Go?\x1b[Z\x1b[Z", "Yes\x03\x7f") == "canvas › Go?[Z[Z → Yes"


def test_pending_question_prefers_data_q():
    html = '<div class="choice" data-q="Merge now?"><p>long text</p><button data-reply="yes">Y</button></div>'
    assert pending_question(html) == "Merge now?"


def test_pending_question_falls_back_to_leading_text():
    html = '<h1>Plan</h1><div class="choice"><p>Which  path?</p><button data-reply="a">A</button></div>'
    assert pending_question(html) == "Which path?"


def test_no_choice_or_no_reply_button_asks_nothing():
    assert pending_question("<div class='card'><p>just a diagram</p></div>") is None
    assert pending_question('<div class="choice"><p>no buttons</p></div>') is None
    assert pending_question('<button data-reply="x">outside a choice</button>') is None
    assert pending_question(None) is None


def test_void_tags_inside_a_choice_do_not_unbalance_it():
    html = '<div class="choice"><p>Pick<br>one</p><input><button data-reply="a">A</button></div><p>after</p>'
    assert pending_question(html) == "Pick one"
