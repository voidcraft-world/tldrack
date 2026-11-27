import pytest

from tldrack.session_names import SessionNameStore, parse_names, prompt_name


@pytest.mark.parametrize(
    "prompt,want",
    [
        ("Please could you fix agent rack resizing?\nHere is the code", "fix agent rack resizing"),
        ("\n# Add batch match attachments\nDetails", "Add batch match attachments"),
        ("\x1b[31mFix the rack", "Fix the rack"),
        ("", None),
        (None, None),
        ([], None),
    ],
)
def test_prompt_names_are_short_local_text(prompt, want):
    assert prompt_name(prompt) == want


def test_name_is_bounded():
    assert len(prompt_name("x" * 10000)) <= 56


def test_first_prompt_only_persists_and_manual_names_win(tmp_path):
    path = tmp_path / "names.json"
    names = SessionNameStore(path)
    names.note_prompt("vc-1", "Add batch match attachments")
    names.note_prompt("vc-1", "Actually fix a different thing")
    names.note_prompt("my-chosen-name", "Do not replace this name")
    assert SessionNameStore(path).get_all() == {"vc-1": "Add batch match attachments"}
    names.manual("vc-1", "vc-2")
    names = SessionNameStore(path)
    names.note_prompt("vc-2", "Even a manually chosen vc-N name wins")
    assert names.get_all() == {}
    names.forget("vc-2")
    names.note_prompt("vc-2", "New session")
    assert names.get_all() == {"vc-2": "New session"}


def test_invalid_disk_entries_are_dropped():
    assert parse_names('{"names":{"vc-1":"good","bad.name":"bad","vc-2":12,"vc-3":"bad\\nname"}}') == {"vc-1": "good"}


def test_a_first_prompt_writes_the_placeholder_and_owes_a_topic(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    epoch = names.note_prompt("vc-1", "Fix the docs voice lines so readers hear")
    assert epoch is not None
    assert names.get_all() == {"vc-1": "Fix the docs voice lines so readers hear"}
    assert names.settle_topic("vc-1", epoch, "docs-voice")
    assert SessionNameStore(tmp_path / "names.json").get_all() == {"vc-1": "docs-voice"}
    # Final now: later prompts owe nothing and change nothing.
    assert names.note_prompt("vc-1", "Now the lobby music") is None
    assert names.get_all() == {"vc-1": "docs-voice"}


def test_lead_worker_label_uses_the_step_and_survives_reload(tmp_path):
    path = tmp_path / "names.json"
    names = SessionNameStore(path)
    worker = "wt-lead-r1127-5916-s011"
    assert names.note_prompt(worker, '<lead-message from="lead"/> run the brief') is None
    epoch = names.note_prompt(worker, "Terminal layout\nKeep expanded worker controls readable")
    assert names.get_all() == {worker: "Terminal layout"}
    assert names.settle_topic(worker, epoch, "worker-controls")
    assert SessionNameStore(path).get_all() == {worker: "worker-controls"}
    assert names.note_prompt("wt-my-branch", "Another task") is None


def test_the_same_prompt_twice_asks_once(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    assert names.note_prompt("vc-1", "Rack names") is not None
    assert names.note_prompt("vc-1", "Rack names\n") is None  # /spawn's prompt, then its hook


def test_a_rename_or_respawn_drops_a_topic_still_in_flight(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    epoch = names.note_prompt("vc-1", "Rack names")
    names.manual("vc-1", "mine")
    assert not names.settle_topic("vc-1", epoch, "rack-names")
    assert names.get_all() == {}

    epoch = names.note_prompt("vc-2", "Old occupant")
    names.forget("vc-2")
    fresh = names.note_prompt("vc-2", "New occupant")
    assert fresh != epoch
    assert not names.settle_topic("vc-2", epoch, "old-topic")
    assert names.settle_topic("vc-2", fresh, "new-topic")
    assert names.get_all() == {"vc-2": "new-topic"}


def test_a_vague_session_retries_then_keeps_its_placeholder(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    asked = [names.note_prompt("vc-1", prompt) for prompt in ("hi", "continue", "yes", "fix rack names")]
    assert [a is not None for a in asked] == [True, True, True, False]
    assert names.get_all() == {"vc-1": "hi"}
    assert not names.settle_topic("vc-1", asked[0], "late")


def test_pending_survives_a_restart_and_old_files_load_final(tmp_path):
    path = tmp_path / "names.json"
    names = SessionNameStore(path)
    names.note_prompt("vc-1", "hi")
    reloaded = SessionNameStore(path)
    epoch = reloaded.note_prompt("vc-1", "Worker replays")
    assert epoch is not None and reloaded.settle_topic("vc-1", epoch, "worker-replays")

    old = tmp_path / "old.json"
    old.write_text('{"names":{"vc-3":"some long sentence label"}}')
    legacy = SessionNameStore(old)
    assert legacy.note_prompt("vc-3", "something else") is None
    assert legacy.get_all() == {"vc-3": "some long sentence label"}


def test_settle_refuses_a_bad_label(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    epoch = names.note_prompt("vc-1", "Rack names")
    assert not names.settle_topic("vc-1", epoch, "bad\nlabel")
    assert not names.settle_topic("vc-1", epoch, "")
    assert names.settle_topic("vc-1", epoch, "rack-names")


def test_adopt_restores_a_final_label(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    epoch = names.note_prompt("vc-1", "hi")
    names.adopt("vc-1", "rack-names")
    assert not names.settle_topic("vc-1", epoch, "other")
    assert names.note_prompt("vc-1", "more") is None
    assert names.get_all() == {"vc-1": "rack-names"}


LEAD = "lead-claude-c-0101-1200"


@pytest.mark.parametrize("session", ["vc-4", LEAD])
@pytest.mark.parametrize(
    "line",
    ['<rack-wake launch="lead"/> You are a LEAD', '  <lead-message from="x"/> do S2', '<rack-wake run="R1" n="3"/> 2 events'],
)
def test_a_line_the_rack_typed_names_nothing_and_spends_no_attempt(tmp_path, session, line):
    names = SessionNameStore(tmp_path / "names.json")
    for _ in range(5):
        assert names.note_prompt(session, line) is None
    assert names.get_all() == {}
    asked = [names.note_prompt(session, prompt) for prompt in ("hi", "continue", "yes", "Laya training data")]
    assert [a is not None for a in asked] == [True, True, True, False], "all three attempts are still there"


def test_a_lead_is_labelled_at_the_operators_first_prompt(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    names.note_prompt(LEAD, '<rack-wake launch="lead"/> You are a LEAD')
    epoch = names.note_prompt(LEAD, "Collect Laya training data")
    assert names.get_all() == {LEAD: "Collect Laya training data"}
    assert names.settle_topic(LEAD, epoch, "laya-data")
    assert SessionNameStore(tmp_path / "names.json").get_all() == {LEAD: "laya-data"}


def test_a_title_is_final_supersedes_a_topic_and_never_a_rename(tmp_path):
    path = tmp_path / "names.json"
    names = SessionNameStore(path)
    epoch = names.note_prompt(LEAD, "hi")
    assert names.title(LEAD, "laya-training-data")
    assert not names.settle_topic(LEAD, epoch, "late-topic")
    assert names.note_prompt(LEAD, "more work") is None
    assert names.title(LEAD, "laya-data"), "a later title still wins over an earlier one"
    assert SessionNameStore(path).get_all() == {LEAD: "laya-data"}

    names.manual("vc-5", "vc-5")
    assert not names.title("vc-5", "rack-names")
    assert not names.title("my-custom-name", "rack-names")
    assert not names.title("vc-6", "bad\nlabel") and not names.title("vc-6", "")
    assert names.get_all() == {LEAD: "laya-data"}


def test_a_reopened_lead_takes_its_label_back(tmp_path):
    names = SessionNameStore(tmp_path / "names.json")
    names.adopt(f"{LEAD}-2", "laya-data")
    assert names.get_all() == {f"{LEAD}-2": "laya-data"}
