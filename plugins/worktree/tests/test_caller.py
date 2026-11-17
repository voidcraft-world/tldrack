from types import SimpleNamespace

from worktree.caller import caller_session


def test_caller_uses_the_calling_pane_and_current_name(monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%12")
    calls = []
    monkeypatch.setattr(
        "worktree.caller.subprocess.run",
        lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0, stdout="renamed-parent\n"),
    )
    assert caller_session() == "renamed-parent"
    assert calls[0] == ["tmux", "display-message", "-p", "-t", "%12", "#{session_name}"]


def test_no_pane_means_no_parent(monkeypatch):
    monkeypatch.delenv("TMUX_PANE", raising=False)
    assert caller_session() is None
    monkeypatch.setenv("TMUX_PANE", "bad;pane")
    assert caller_session() is None
