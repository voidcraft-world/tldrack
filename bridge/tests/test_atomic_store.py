"""`atomic_write`'s failure policy — that the degradation is REACHABLE.

The happy path is covered incidentally by every store's own suite. What was
never covered, and what these pin, is the promise in the docstring: a state
directory the process cannot write degrades to "kept in memory only" instead of
raising. Before the try block was widened to include `mkdir` and `mkstemp`,
that promise held only for the write itself — an unusable directory failed
above the handler and escaped into a request that had already mutated state.

The unwritable directory is simulated by putting a FILE where a directory has
to be, rather than by chmod: `mkdir` then fails with NotADirectoryError on every
platform and for every user, including root.
"""

from tldrack.atomic_store import atomic_write


def test_writes_and_replaces(tmp_path):
    target = tmp_path / "state" / "icons.json"
    atomic_write(target, '{"version": 1}', "icons")
    assert target.read_text() == '{"version": 1}'


def test_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "icons.json"
    atomic_write(target, "body", "icons")
    assert [p.name for p in tmp_path.iterdir()] == ["icons.json"]


def test_degrades_when_the_state_dir_cannot_be_created(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory needs to be")
    target = blocker / "nested" / "state.json"

    atomic_write(target, "body", "icons")  # must not raise

    assert "kept in memory only" in capsys.readouterr().err


def test_degrades_when_the_temp_file_cannot_be_created(tmp_path, capsys, monkeypatch):
    def refuse(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr("tldrack.atomic_store.tempfile.mkstemp", refuse)
    target = tmp_path / "state.json"

    atomic_write(target, "body", "icons")  # must not raise

    err = capsys.readouterr().err
    assert "kept in memory only" in err
    assert not target.exists()


def test_reports_the_noun_it_was_given(tmp_path, capsys):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    atomic_write(blocker / "nested" / "s.json", "body", "notify config")
    assert "notify config kept in memory only" in capsys.readouterr().err
