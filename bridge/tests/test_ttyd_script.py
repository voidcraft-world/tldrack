"""The `sh -c` body ttyd runs per connection.

The thing under test is a STRING that becomes a shell script, so these tests
actually execute it against a fake `tmux` on PATH rather than asserting on
substrings — a quoting bug that a `in script` assertion sails past is exactly
the kind that turns into a resurrected session or a shell injection.
"""

import os
import subprocess

import pytest

from tldrack.main import PANE_IDENTITY_ENV, shed_pane_identity, ttyd_session_script


# Writes its diagnostic to STDERR on failure, exactly as real tmux does — which
# is what makes the redirect in the attach branch testable at all.
# `has-session` answers FAKE_HAS_RC (default: the same as everything else), so a
# test can stage "the session is live, but the attach was refused".
FAKE_TMUX = """#!/bin/sh
echo "TMUX: $*"
rc="${FAKE_TMUX_RC:-0}"
if [ "$1" = "has-session" ]; then
  exit "${FAKE_HAS_RC:-$rc}"
fi
missing="can't find session: fake-diagnostic"
if [ "$rc" != "0" ]; then
  echo "${FAKE_TMUX_ERR:-$missing}" >&2
fi
exit "$rc"
"""


@pytest.fixture
def run_script(tmp_path):
    """Run the generated script with `tmux` stubbed out. Returns (exit, output)."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "tmux"
    fake.write_text(FAKE_TMUX)
    fake.chmod(0o755)

    def run(arg, *, session="tldrack", start_dir=None, tmux_rc="0", has_rc=None, tmux_err=None):
        script = ttyd_session_script(session, start_dir)
        env = dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}", FAKE_TMUX_RC=tmux_rc, TMPDIR=str(tmp_path / "tmp"))
        (tmp_path / "tmp").mkdir(exist_ok=True)
        if has_rc is not None:
            env["FAKE_HAS_RC"] = has_rc
        if tmux_err is not None:
            env["FAKE_TMUX_ERR"] = tmux_err
        argv = ["sh", "-c", script, "ttyd-session"] + ([arg] if arg is not None else [])
        proc = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            env=env,
            # The failed-attach branch blocks on `read`; EOF stands in for the
            # user not pressing Enter.
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
        return proc.returncode, proc.stdout + proc.stderr

    return run


# ── attach-only: the form every PERSISTED terminal uses ──────────────────


def test_attach_prefix_attaches_and_never_creates(run_script):
    _, out = run_script("attach:my-session")
    assert "TMUX: attach -t =my-session" in out
    assert "new -A" not in out


def test_attach_prefix_uses_exact_match(run_script):
    """`=` or tmux prefix-matches — `attach:web` would land in `web-gpu`.

    Same reason /kill-session and /rename-session pass `=<name>`.
    """
    _, out = run_script("attach:web")
    assert "-t =web" in out


def test_attach_ignores_start_dir(run_script):
    """-c only makes sense when creating; an attach keeps the session's cwd."""
    _, out = run_script("attach:my-session", start_dir="/repo")
    assert "-c" not in out


def test_dead_session_explains_itself_instead_of_creating_one(run_script):
    code, out = run_script("attach:gone", tmux_rc="1")
    assert "session gone is gone" in out
    assert "new -A" not in out, "a failed attach must never fall through to create"
    assert code != 0


def test_dead_session_says_it_ONCE(run_script):
    """tmux's own stderr is discarded; our sentence is the only telling.

    Unredirected, a dead pane opened with `can't find session: <name>` — a raw
    diagnostic naming a `-t` target the user never typed — immediately followed
    by our line saying the same thing in words. The regression this guards is
    the easy one: someone drops the `2>/dev/null` while editing the branch.
    """
    _, out = run_script("attach:gone", tmux_rc="1")
    assert "can't find session" not in out
    assert "fake-diagnostic" not in out
    assert "press Enter to retry" in out


def test_a_LIVE_session_that_refuses_the_attach_says_why(run_script):
    """ "is gone" about a live session is a lie, and tmux's reason is the fix.

    2026-11-04: a bridge that inherited a pane's TMUX made every attach-only pane
    print "session vc-6 is gone" while vc-6 ran, and the one line naming the
    cause was the stderr this branch dropped.
    """
    code, out = run_script(
        "attach:vc-6",
        tmux_rc="1",
        has_rc="0",
        tmux_err="sessions should be nested with care, unset $TMUX to force",
    )
    assert "session vc-6 is running, but tmux refused to attach" in out
    assert "sessions should be nested with care" in out
    assert "is gone" not in out
    assert "new -A" not in out, "a refused attach must never fall through to create"
    assert code != 0


def test_the_attach_leaves_no_temp_file(run_script, tmp_path):
    """stderr goes to an UNLINKED file, so a SIGHUP mid-attach (how ttyd ends most
    connections) cannot leave one behind."""
    run_script("attach:alive")
    run_script("attach:vc-6", tmux_rc="1", has_rc="0")
    run_script("attach:gone", tmux_rc="1")
    assert list((tmp_path / "tmp").iterdir()) == []


def test_a_SUCCESSFUL_attach_still_shows_tmux_output(run_script):
    """The redirect covers tmux's stderr only.

    It is in force for the life of the `tmux attach` call, but a successful
    attach hands the terminal over and everything the session prints from then
    on is the session's own stdout. A redirect that silenced a live agent's
    output would be a far worse bug than the noise it was added to remove.
    """
    code, out = run_script("attach:alive")
    assert "TMUX: attach -t =alive" in out
    assert code == 0


# ── attach-or-create: the live rack pane, and "+ NEW" ────────────────────


def test_bare_name_still_attaches_or_creates(run_script):
    """`+ NEW` has no backend route — navigating to ?arg=<name> IS the create."""
    _, out = run_script("my-session")
    assert "TMUX: new -A -s my-session" in out


def test_no_arg_falls_back_to_the_default_session(run_script):
    _, out = run_script(None, session="tldrack")
    assert "TMUX: new -A -s tldrack" in out


def test_start_dir_is_quoted_for_the_create_path(run_script):
    _, out = run_script("fresh", start_dir="/repo dir")
    assert "TMUX: new -A -s fresh -c /repo dir" in out


# ── the arg is argv, never script text ───────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        "attach:x; touch /tmp/pwned",
        "attach:$(touch /tmp/pwned)",
        "attach:`touch /tmp/pwned`",
        "x; touch /tmp/pwned",
    ],
)
def test_hostile_arg_reaches_tmux_verbatim(run_script, hostile, tmp_path):
    """ttyd -a passes ?arg= as argv and $1 stays a shell VARIABLE in both
    branches, so a session name can never become a command. This is the
    property bridge/CLAUDE.md's security posture rests on."""
    _, out = run_script(hostile, tmux_rc="1")
    assert "touch /tmp/pwned" in out, "expected the payload as literal argv text"
    assert not os.path.exists("/tmp/pwned")


# ── the bridge never runs as a resident of a pane ────────────────────────


def test_shed_pane_identity_drops_both_and_nothing_else():
    environ = {"TMUX": "/private/tmp/tmux-501/default,67609,651", "TMUX_PANE": "%651", "TLDRACK_PORT": "7681", "HOME": "/Users/x"}
    assert shed_pane_identity(environ) == ["TMUX", "TMUX_PANE"]
    assert environ == {"TLDRACK_PORT": "7681", "HOME": "/Users/x"}


def test_shed_pane_identity_is_a_no_op_outside_tmux():
    environ = {"HOME": "/Users/x"}
    assert shed_pane_identity(environ) == []
    assert environ == {"HOME": "/Users/x"}
    assert set(PANE_IDENTITY_ENV) == {"TMUX", "TMUX_PANE"}
