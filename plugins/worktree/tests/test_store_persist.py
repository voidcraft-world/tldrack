"""The three worktree stores' failure policy — that degradation is REACHABLE.

Each store promises the same thing in its own words: losing the on-disk copy is
not a reason to fail the request that produced it, so the row stays in memory
and a line goes to stderr. Every mutation has already been applied under the
lock by the time `_persist` runs, so an OSError escaping it would 500 a request
whose change actually happened.

That promise used to hold only for the write. `mkdir` and `mkstemp` sat above
the try, and those are the two calls an unusable state directory fails at — so
the handler was unreachable in exactly the case it was written for. These pin
the widened try. The idiom is owned by
bridge/tldrack/atomic_store.py.

A FILE placed where a directory has to be is what makes the directory
unusable: `mkdir` then raises NotADirectoryError for every user on every
platform, which chmod cannot promise (root ignores it).
"""

import pytest

from worktree.harbor import BriefStore, PrIndexStore
from worktree.store import PrStore


@pytest.fixture
def blocked_path(tmp_path):
    """A path whose parent directory can never be created."""
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory needs to be")
    return blocker / "nested" / "state.json"


def test_pr_store_keeps_the_row_when_it_cannot_persist(blocked_path, capsys):
    store = PrStore(blocked_path)

    store.set("feat/x", status="open", url="https://x/1")

    assert "PR state kept in memory only" in capsys.readouterr().err
    assert store.get("feat/x")["status"] == "open"


def test_pr_index_store_keeps_rows_when_it_cannot_persist(blocked_path, capsys):
    store = PrIndexStore(blocked_path)

    store.replace({"feat/x": {"number": 7, "state": "open"}}, fetched_at=1000.0)

    assert "PR index kept in memory only" in capsys.readouterr().err
    _fetched_at, rows = store.snapshot()
    assert rows["feat/x"]["number"] == 7


def test_brief_store_keeps_entries_when_it_cannot_persist(blocked_path, capsys):
    store = BriefStore(blocked_path)

    store.set({"branch": "feat/x", "summary": "a brief"})

    assert "briefs kept in memory only" in capsys.readouterr().err
    assert store.get("feat/x") is not None


@pytest.mark.parametrize("build", [PrStore, PrIndexStore, BriefStore])
def test_every_store_logs_under_its_own_name(build, blocked_path, capsys):
    """The prefix is the PLUGIN's name, not the host's — these stores print to
    the bridge's stderr alongside `coordinator:` lines, and named
    `tldrack:` they were indistinguishable from the host's own."""
    store = build(blocked_path)
    if isinstance(store, PrStore):
        store.set("b", status="open")
    elif isinstance(store, PrIndexStore):
        store.set_error("gh missing")
    else:
        store.set_error("login failed")

    err = capsys.readouterr().err
    assert err.startswith("worktree: ")
    assert "tldrack:" not in err
