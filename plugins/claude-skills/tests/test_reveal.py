"""The `POST /open` path guard — the plugin's security boundary.

Every test here is an escape attempt. `resolve_reveal_target` is the only thing
standing between an allowed origin and `open -R <anywhere>`.
"""

import sys
from pathlib import Path

import pytest

from claude_skills.reveal import handle_open, resolve_reveal_target


@pytest.fixture
def allowed(tmp_path: Path):
    root = tmp_path / "claude" / "skills"
    (root / "my-skill").mkdir(parents=True)
    (tmp_path / "outside").mkdir()
    return root, tmp_path


def test_path_inside_an_allowed_root_is_accepted(allowed):
    root, _ = allowed
    target, reason = resolve_reveal_target(str(root / "my-skill"), [root])
    assert reason is None
    assert target == (root / "my-skill").resolve()


def test_the_root_itself_is_accepted(allowed):
    root, _ = allowed
    assert resolve_reveal_target(str(root), [root])[1] is None


def test_path_outside_every_root_is_refused(allowed):
    root, tmp_path = allowed
    target, reason = resolve_reveal_target(str(tmp_path / "outside"), [root])
    assert target is None
    assert "outside" in reason


def test_dot_dot_traversal_is_refused(allowed):
    root, _ = allowed
    target, reason = resolve_reveal_target(f"{root}/../../outside", [root])
    assert target is None
    assert "outside" in reason


def test_symlink_pointing_out_of_the_root_is_refused(allowed):
    """The check runs AFTER resolve(), so a planted symlink is judged on where
    it really goes — not on the fact that the link itself sits inside a root."""
    root, tmp_path = allowed
    escape = root / "escape"
    escape.symlink_to(tmp_path / "outside", target_is_directory=True)
    assert escape.exists()  # the link is real and inside the root
    target, reason = resolve_reveal_target(str(escape), [root])
    assert target is None
    assert "outside" in reason


def test_nonexistent_path_is_refused(allowed):
    root, _ = allowed
    assert resolve_reveal_target(str(root / "ghost"), [root])[1] == "path does not exist"


@pytest.mark.parametrize("raw", [None, "", "   ", 42, {"path": "x"}, [], True])
def test_non_string_or_blank_paths_are_refused(raw, allowed):
    root, _ = allowed
    target, reason = resolve_reveal_target(raw, [root])
    assert target is None
    assert reason == "missing 'path'"


def test_empty_allowlist_refuses_everything(allowed):
    root, _ = allowed
    assert resolve_reveal_target(str(root / "my-skill"), [])[0] is None


def test_unreadable_root_is_skipped_not_fatal(allowed):
    """A root that cannot be resolved must not abort the whole check."""
    root, _ = allowed
    bogus = Path("\0bad")  # resolve() raises on a NUL byte
    target, reason = resolve_reveal_target(str(root / "my-skill"), [bogus, root])
    assert reason is None
    assert target == (root / "my-skill").resolve()


def test_handle_open_refuses_with_403(allowed):
    root, tmp_path = allowed
    status, payload = handle_open({"path": str(tmp_path / "outside")}, [root])
    assert status == 403
    assert "error" in payload


def test_handle_open_refuses_a_bodiless_request(allowed):
    root, _ = allowed
    assert handle_open(None, [root])[0] == 403


@pytest.mark.skipif(sys.platform == "darwin", reason="macOS actually opens Finder")
def test_non_macos_reports_not_implemented(allowed):
    root, _ = allowed
    assert handle_open({"path": str(root / "my-skill")}, [root])[0] == 501
