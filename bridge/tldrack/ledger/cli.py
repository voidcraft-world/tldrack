"""`tldrack ledger …` — the session ledger from a terminal. What it is: `ledger/__init__.py`."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import click

from tldrack.ledger.audience import CHILD_KINDS, CLASS_ORDER, measure
from tldrack.ledger.export import export_decisions, export_pairs, search
from tldrack.ledger.ingest import AlreadyRunning, ingest, ledger_status, load_roots
from tldrack.ledger.store import connect, ledger_dir
from tldrack.rack_metrics import resolve_metrics_path


def _require_dir():
    root = ledger_dir()
    if root is None:
        raise click.ClickException("the ledger is off: set TLDRACK_LEDGER_DIR (or the data directory, `tldrack/paths.py`)")
    return root


def _gb(n: int) -> str:
    return f"{n / 1e9:.2f} GB" if n >= 1e8 else f"{n / 1e6:.1f} MB"


@click.group(name="ledger")
def ledger_group() -> None:
    """The session ledger: every agent conversation on this Mac, kept, and the human decisions in it."""


@ledger_group.command(name="ingest")
@click.option(
    "--only",
    type=click.Choice(["claude", "codex", "bridge"]),
    default=None,
    help="One source only (the gone-file pass then skips).",
)
@click.option("--quiet", is_flag=True, help="Only the summary line.")
def ingest_cmd(only: str | None, quiet: bool) -> None:
    """Archive and index everything new. Safe to run any time; a second run waits for nobody."""
    root = _require_dir()
    log = None if quiet else (lambda msg: click.echo(msg, err=True))
    try:
        totals = ingest(root, only=only, log=log)
    except AlreadyRunning:
        click.echo("ledger · an ingest is already running")
        return
    click.echo(
        f"ledger · {totals['files_changed']}/{totals['files_seen']} files changed · "
        f"{_gb(totals['bytes_read'])} read · {_gb(totals['bytes_archived'])} archived · "
        f"{totals['rows_inserted']} side rows · {totals['errors']} errors · {totals['seconds']} s"
    )


@ledger_group.command(name="status")
@click.option("--json", "as_json", is_flag=True)
def status_cmd(as_json: bool) -> None:
    """What the ledger holds — counts only."""
    status = ledger_status(ledger_dir(), roots=load_roots())
    if as_json:
        click.echo(json.dumps(status, indent=2, default=str))
        return
    if not status.get("enabled"):
        click.echo(f"ledger · off — {status.get('reason')}")
        return
    if not status.get("ingested"):
        click.echo(f"ledger · {status['dir']} · never ingested — run `tldrack ledger ingest`")
        return
    files = status["files"]
    click.echo(
        f"ledger · {status['dir']} · db {_gb(status['db_bytes'])} · archive {_gb(files['archive_bytes'])} "
        f"of {_gb(files['source_bytes'])} source · {files['outlived_source']} files outlived their source"
    )
    click.echo(
        f"sessions · {sum(status['sessions'].values())} · "
        + " · ".join(f"{k} {v}" for k, v in sorted(status["sessions"].items()))
    )
    human = {k: v for k, v in status["events"].items() if k.startswith("human:")}
    machine = {k: v for k, v in status["events"].items() if k.startswith("machine:")}
    click.echo(f"human decisions · {sum(human.values())} · " + " · ".join(f"{k[6:]} {v}" for k, v in sorted(human.items())))
    if machine:
        click.echo(
            f"machine decisions · {sum(machine.values())} · " + " · ".join(f"{k[8:]} {v}" for k, v in sorted(machine.items()))
        )
    click.echo(f"turns {status['turns']} · messages {status['messages']} · tool calls {status['tool_calls']}")
    if status["bridge_rows"]:
        click.echo("side ledgers · " + " · ".join(f"{k} {v}" for k, v in sorted(status["bridge_rows"].items())))
    risk = status["at_risk"]
    if risk["count"]:
        click.echo(
            f"AT RISK · {risk['count']} transcripts near the reaper and not fully archived "
            f"(oldest {risk['oldest_age_days']} d) — run ingest now"
        )
    if status.get("running"):
        click.echo("an ingest is running now")


def _since(days: float | None) -> float:
    return time.time() - days * 86400 if days else 0.0


@ledger_group.command(name="export")
@click.argument("kind", type=click.Choice(["pairs", "decisions"]))
@click.option("--days", type=float, default=None, help="Only the last N days.")
@click.option("--machine", is_flag=True, help="decisions: include the machine's (auto-review, auto-mode blocks).")
def export_cmd(kind: str, days: float | None, machine: bool) -> None:
    """JSONL to stdout: `pairs` (turn end → what the human did next) or `decisions` (every human event)."""
    conn = connect(_require_dir(), readonly=True)
    try:
        rows = export_pairs(conn, _since(days)) if kind == "pairs" else export_decisions(conn, _since(days), machine)
        for row in rows:
            sys.stdout.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")
    finally:
        conn.close()


@ledger_group.command(name="audience")
@click.option("--days", type=float, default=None, help="Only the last N days.")
@click.option(
    "--metrics",
    "metrics_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="The rack-metrics database the signals come from (default: the bridge's own).",
)
@click.option("--json", "as_json", is_flag=True)
def audience_cmd(days: float | None, metrics_path: Path | None, as_json: bool) -> None:
    """Where each ⚠️ landed (held · new · repeat · retired) against the in-process children running then."""
    conn = connect(_require_dir(), readonly=True)
    try:
        result = measure(conn, since=_since(days), metrics_path=metrics_path or resolve_metrics_path())
    finally:
        conn.close()
    if as_json:
        click.echo(json.dumps(result, indent=2))
        return
    free, held_bg = result["free_with_children"], result["held_background_with_children"]
    every = result["all_with_children"]
    click.echo(
        f"⚠️ {result['warnings']} · unlinked {result['unlinked']} · classified by landing "
        f"{result['classified_by']['landing']} · by signal {result['classified_by']['signal']}"
    )
    click.echo(f"{'class':<16}" + "".join(f"{kind:>10}" for kind in CHILD_KINDS))
    for name in CLASS_ORDER:
        cells = result["classes"][name]
        if sum(cells.values()):
            click.echo(f"{name:<16}" + "".join(f"{cells[kind]:>10}" for kind in CHILD_KINDS))
    click.echo(f"FREE with children running · {free['n']} of {free['of']}  (the misses: ⚠️ shown while its own agents ran)")
    click.echo(f"HELD bg with children · {held_bg['n']} of {held_bg['of']}  (held, for the wrong reason)")
    click.echo(f"ALL with children · {every['n']} of {every['of']}")


@ledger_group.command(name="search")
@click.argument("query")
@click.option("--limit", type=int, default=20)
def search_cmd(query: str, limit: int) -> None:
    """Full-text search over every human and agent message."""
    conn = connect(_require_dir(), readonly=True)
    try:
        for row in search(conn, query, limit):
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(row["ts"])) if row["ts"] else "?"
            click.echo(f"{stamp} {row['source']}/{row['profile']} {row['role']:>6} {row['session_id']}  {row['snippet']}")
    finally:
        conn.close()


if __name__ == "__main__":  # `python -m tldrack.ledger.cli ingest` — the bridge's child
    ledger_group()
