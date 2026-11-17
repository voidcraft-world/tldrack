"""`tldrack mission`: how an agent (or a shell) reads and writes its mission.

The store and the why: `missions.py`. This is a client of the running bridge, never of
the file, so one process owns the writes. The caller is whichever tmux session
`$TMUX_PANE` sits in, asked of tmux at call time, because a rename would make a stored
name wrong.
"""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

import click

BRIDGE_TIMEOUT_SECONDS = 5


def status_port() -> int:
    return int(os.environ.get("TLDRACK_STATUS_PORT", "7682"))


def own_session() -> str | None:
    """This pane's tmux session, or None outside tmux."""
    pane = os.environ.get("TMUX_PANE")
    if not pane:
        return None
    result = subprocess.run(
        ["tmux", "display-message", "-p", "-t", pane, "#{session_name}"], capture_output=True, text=True, timeout=3, check=False
    )
    name = result.stdout.strip()
    return name if result.returncode == 0 and name else None


def require_session() -> str:
    session = own_session()
    if session is None:
        raise click.ClickException("not inside a tmux session: run this from the agent's pane")
    return session


def call(
    path: str,
    body: Mapping[str, Any] | None = None,
    *,
    missing: str = "this bridge has no missions yet: restart it",
    timeout: float = BRIDGE_TIMEOUT_SECONDS,
) -> dict:
    """One bridge call. Every failure is a ClickException, so an agent reads a sentence. The call
    names its pane (`X-VC-Pane`), as the hook relay does, so the bridge knows which session asks.
    `tldrack lead` (lead/cli.py) shares it."""
    headers = {"Content-Type": "application/json"}
    if os.environ.get("TMUX_PANE"):
        headers["X-VC-Pane"] = os.environ["TMUX_PANE"]
    request = urllib.request.Request(
        f"http://127.0.0.1:{status_port()}{path}", data=json.dumps(body).encode() if body is not None else None, headers=headers
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as err:
        if err.code == 404:  # 404 only ever means this: the bridge predates the route
            raise click.ClickException(missing) from err
        try:
            message = json.loads(err.read()).get("error")
        except (ValueError, AttributeError):
            message = None
        raise click.ClickException(message or f"bridge answered {err.code}") from err
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        raise click.ClickException(f"bridge not reachable: {err}") from err


def format_mission(mission: Mapping[str, Any]) -> str:
    """A mission as plain text: what an agent reads back."""
    closed = " (closed)" if mission.get("closed_at") else ""
    lines = [f"{mission['title']}{closed} · {mission['id']}"]
    if mission.get("brief"):
        lines.append(mission["brief"])
    lines.append("")
    for member in mission.get("members") or []:
        touching = member.get("touching") or []
        lines.append(
            f"  {member['session']}: {member.get('role') or '(no role)'}"
            + (f"  [touching {', '.join(touching)}]" if touching else "")
            + ("  (cannot hear hooks)" if member.get("hears") is False else "")
        )
    notes = mission.get("notes") or []
    if notes:
        lines.append("")
        lines.extend(f"  {note.get('author')}: {note.get('text')}" for note in notes)
    return "\n".join(lines)


def own_mission_id() -> str:
    """The caller's open mission."""
    session = require_session()
    for mission in call("/missions").get("missions", []):
        if any(member.get("session") == session for member in mission.get("members", [])):
            return mission["id"]
    raise click.ClickException(f"{session} is not on a mission")


@click.group(name="mission")
def mission() -> None:
    """Sessions assigned to one task, and the notes they share."""


@mission.command(name="status")
@click.argument("mission_ref", required=False)
@click.option("--json", "as_json", is_flag=True, help="Print the raw document.")
def status_command(mission_ref: str | None, as_json: bool) -> None:
    """This session's mission in full, or MISSION_REF (an id, id prefix or title)."""
    ref = mission_ref or own_mission_id()
    body = call(f"/missions?{urllib.parse.urlencode({'mission': ref})}")
    click.echo(json.dumps(body, indent=2) if as_json else format_mission(body))


@mission.command(name="list")
def list_command() -> None:
    """Every open mission."""
    missions = call("/missions").get("missions", [])
    if not missions:
        click.echo("no open missions")
    for row in missions:
        click.echo(
            f"{row['id'][:8]}  {row['title']}  ·  {len(row.get('members') or [])} sessions  ·  {row.get('note_count', 0)} notes"
        )


@mission.command(name="start")
@click.argument("title")
@click.option("--brief", default="", help="One line on the overall task.")
def start_command(title: str, brief: str) -> None:
    """Start a mission with this session on it. Other sessions join for themselves."""
    own = own_session()
    created = call(
        "/missions", {"op": "create", "title": title, "brief": brief, "session": own, "sessions": [own] if own else []}
    )
    click.echo(f"{created['mission']['id'][:8]}  {created['mission']['title']}")


@mission.command(name="join")
@click.argument("mission_ref")
def join_command(mission_ref: str) -> None:
    """Put this session on a mission."""
    call("/missions", {"op": "assign", "session": require_session(), "mission": mission_ref})
    click.echo("joined")


@mission.command(name="leave")
def leave_command() -> None:
    """Take this session off its mission."""
    call("/missions", {"op": "assign", "session": require_session(), "mission": None})
    click.echo("left")


@mission.command(name="note")
@click.argument("text")
def note_command(text: str) -> None:
    """Tell every teammate something, now and in the mission's memory."""
    call("/missions", {"op": "note", "session": require_session(), "text": text})
    click.echo("noted")


@mission.command(name="role")
@click.argument("text")
def role_command(text: str) -> None:
    """Say what this session owns."""
    call("/missions", {"op": "role", "session": require_session(), "role": text})
    click.echo("set")


@mission.command(name="close")
@click.argument("mission_ref", required=False)
def close_command(mission_ref: str | None) -> None:
    """Close a mission. Its notes stay readable."""
    call("/missions", {"op": "close", "mission": mission_ref or own_mission_id()})
    click.echo("closed")
