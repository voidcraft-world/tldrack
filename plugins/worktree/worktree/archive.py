"""Archived branch tips: a FORCED delete pins what it deletes, so every delete
that nothing proved safe is reversible.

`refs/tldrack/archived/<branch>` holds the tip a `-D` removed. It is not a
`refs/heads/` ref, so the harbor sweep, `git branch` and every agent's view of
the repo never see it, yet it keeps the commits reachable and `git gc` leaves
them alone. Three verbs read and write it:

* `POST /delete-branch {force: true}` on a branch not proven landed or
  contained writes the pin BEFORE `git branch -D`. A pin that cannot be
  written refuses the delete: "never deleted without a pin" is the property.
* `POST /restore-branch {branch}` recreates `refs/heads/<branch>` at the pin,
  then drops every pin of that tip.
* `POST /forget-archived {branch, sha}` drops a pin, the one irreversible step,
  and only for the exact tip the caller was shown.

── The legacy namespace ───────────────────────────────────────────────────────
Pins written before the rename live under `refs/voidcraft/archived/`. They are
READ as pins of their own (`ARCHIVE_PREFIXES`), never moved: a rename that
rewrote refs in someone's repository would be a migration nobody asked for. A
branch pinned under both shows once, at its newer tip (a tie goes to the new
name); restore and forget act on every ref that holds the tip they name, so the
other prefix's divergent pin, if any, shows next and is never lost.

A landed delete pins nothing: the base already holds that work, and pinning
every CLEANUP would bury the few tips that matter.

── One pin per name ───────────────────────────────────────────────────────────
A second forced delete of the same name overwrites the pin only when the old
tip is an ancestor of the new one, so nothing is lost. A divergent old pin
refuses the delete (409, `archive_conflict`), and the person restores or
forgets it first. That is rare, and it never loses a commit silently.

The `adopt.ADOPT_REF_PREFIX` precedent: a namespace of its own under `refs/`
that no other tool writes, written with an old-value guard so a race cannot
clobber it.
"""

from __future__ import annotations

from pathlib import Path

from worktree import create as create_mod
from worktree.branches import FIELD_SEP, is_full_oid
from worktree.listing import PROBE_TIMEOUT_SECONDS

ARCHIVE_PREFIX = "refs/tldrack/archived/"
#: Read, never written: the pins made before the rename (module docstring).
LEGACY_ARCHIVE_PREFIX = "refs/voidcraft/archived/"
ARCHIVE_PREFIXES = (ARCHIVE_PREFIX, LEGACY_ARCHIVE_PREFIX)

#: The zero oid as `update-ref`'s old value: "this ref must not exist yet".
ABSENT_OLD_VALUE = "0" * 40

LIST_FORMAT = FIELD_SEP.join(("%(refname)", "%(objectname)", "%(committerdate:unix)", "%(contents:subject)"))


def archive_ref(branch: str) -> str:
    return f"{ARCHIVE_PREFIX}{branch}"


# ── argv builders (pure) ──────────────────────────────────────────────────


def build_list_command(repo_root: Path | str, branch: str | None = None) -> list[str]:
    """Every pin under both prefixes, or only those of `branch` (a pattern also
    matches refs below it, so the parse keeps the exact name)."""
    patterns = [prefix + branch if branch else prefix.rstrip("/") for prefix in ARCHIVE_PREFIXES]
    return ["git", "-C", str(repo_root), "for-each-ref", f"--format={LIST_FORMAT}", *patterns]


def build_pin_command(repo_root: Path | str, branch: str, oid: str, old: str | None) -> list[str]:
    """`old` None = must not exist yet; else the oid it must still hold."""
    return [
        "git",
        "-C",
        str(repo_root),
        "update-ref",
        "-m",
        "tldrack: archived by a forced delete",
        archive_ref(branch),
        oid,
        old or ABSENT_OLD_VALUE,
    ]


def build_unpin_command(repo_root: Path | str, ref: str, oid: str) -> list[str]:
    """`ref` is a full ref under either prefix: a pin is dropped where it lives."""
    return ["git", "-C", str(repo_root), "update-ref", "-d", ref, oid]


def build_restore_command(repo_root: Path | str, branch: str, oid: str) -> list[str]:
    """`update-ref` with the zero old value: creates `refs/heads/<branch>` only
    if no branch of that name exists. `git branch` would read `oid` as a
    start-point expression; this reads it as an oid."""
    return [
        "git",
        "-C",
        str(repo_root),
        "update-ref",
        "-m",
        "tldrack: restored from archive",
        f"refs/heads/{branch}",
        oid,
        ABSENT_OLD_VALUE,
    ]


# ── parsers (pure, total) ─────────────────────────────────────────────────


def parse_pins(text: object) -> list[dict]:
    """`for-each-ref` → every pin as `{ref, branch, oid, committed_at, subject}`,
    under either prefix, newest commit first (a tie: the new prefix first). A row
    that does not parse is dropped."""
    if not isinstance(text, str):
        return []
    pins = []
    for line in text.split("\n"):
        parts = line.split(FIELD_SEP)
        if len(parts) != 4:
            continue
        ref, oid, stamp, subject = parts
        prefix = next((p for p in ARCHIVE_PREFIXES if ref.startswith(p)), None)
        branch = ref[len(prefix) :] if prefix else ""
        if not branch or not is_full_oid(oid):
            continue
        pins.append(
            {
                "ref": ref,
                "branch": branch,
                "oid": oid,
                "committed_at": int(stamp) if stamp.isdigit() else None,
                "subject": subject,
            }
        )
    pins.sort(key=lambda pin: (pin["committed_at"] or 0, pin["ref"].startswith(ARCHIVE_PREFIX)), reverse=True)
    return pins


def parse_list(text: object) -> list[dict]:
    """`for-each-ref` → `[{branch, oid, sha, committed_at, subject}]`, ONE row
    per branch (its newest pin), newest commit first."""
    rows: dict[str, dict] = {}
    for pin in parse_pins(text):
        if pin["branch"] not in rows:
            rows[pin["branch"]] = {
                "branch": pin["branch"],
                "oid": pin["oid"],
                "sha": pin["oid"][:9],
                "committed_at": pin["committed_at"],
                "subject": pin["subject"],
            }
    return list(rows.values())


def newest_tip(pins: list[dict]) -> str | None:
    """The tip a branch's pins show as (`parse_pins` order), or None."""
    return pins[0]["oid"] if pins else None


def refs_holding(pins: list[dict], oid: str | None) -> list[str]:
    """Every pin ref that holds exactly `oid`: what a restore or a forget drops."""
    return [pin["ref"] for pin in pins if pin["oid"] == oid]


def written_pin(pins: list[dict], branch: str) -> str | None:
    """What the new-prefix ref holds now: the old value a write must name."""
    return next((pin["oid"] for pin in pins if pin["ref"] == archive_ref(branch)), None)


# ── readers (one fork each; module functions, so the plugin tests patch them) ─


def list_archived(repo_root: Path | str) -> list[dict]:
    """Every pinned tip. A failure lists nothing rather than failing the
    harbor document."""
    code, out, _err = create_mod.run_bounded(build_list_command(repo_root), PROBE_TIMEOUT_SECONDS)
    return parse_list(out) if code == 0 else []


def pins_for(repo_root: Path | str, branch: str) -> list[dict]:
    """Every pin of exactly `branch`, under either prefix (`parse_pins` order).
    [] when there is none or git could not say."""
    code, out, _err = create_mod.run_bounded(build_list_command(repo_root, branch), PROBE_TIMEOUT_SECONDS)
    return [pin for pin in parse_pins(out) if pin["branch"] == branch] if code == 0 else []


# ── decisions (pure) ──────────────────────────────────────────────────────


def decide_pin(*, existing: str | None, tip: str, existing_in_tip: bool | None) -> str:
    """`write` (no pin yet, or the old tip is inside the new one), `same`
    (already pinned there), or `conflict` (a divergent old pin, or ancestry
    git could not answer: unknown refuses)."""
    if existing is None:
        return "write"
    if existing == tip:
        return "same"
    return "write" if existing_in_tip is True else "conflict"


def decide_restore(*, name_error: str | None, pinned: str | None, branch_exists: bool, is_base: bool) -> tuple[int, dict] | None:
    if name_error is not None:
        return 400, {"error": name_error}
    if pinned is None:
        return 404, {"error": "no archived tip by that name"}
    if is_base:
        return 409, {"error": "that is the base branch's name"}
    if branch_exists:
        return 409, {"error": "a branch with that name exists — rename or delete it first"}
    return None


def decide_forget(*, name_error: str | None, pinned: str | None, expected: str | None) -> tuple[int, dict] | None:
    if name_error is not None:
        return 400, {"error": name_error}
    if pinned is None:
        return 404, {"error": "no archived tip by that name"}
    if not is_full_oid(expected) or expected != pinned:
        # The armed button showed one tip; forgetting another would be a
        # press the person never made.
        return 409, {"error": "the archived tip changed since it was shown — refresh first"}
    return None
