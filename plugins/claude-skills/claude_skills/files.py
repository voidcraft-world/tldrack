"""`GET /skill` — one skill's files, and the text of one of them, for the agent canvas's skills view.

The audit view (plugins/agent-canvas, `assets/skills.js`) lists every skill from
`GET /state`, then loads a body only when a row is opened: every SKILL.md body at once is a
payload nobody asked for, and the canvas frag is capped well below it.

The path rules are `POST /open`'s, for the same reason: `resolve_reveal_target` resolves
symlinks BEFORE the containment check, so a link planted in a skills dir is judged on
where it really points. The file is then resolved again under the skill's own real
directory, so `../` or a link inside the skill cannot step out of it. Text only, capped.
"""

from __future__ import annotations

from pathlib import Path

from claude_skills.reveal import resolve_reveal_target
from claude_skills.scan import MAX_STAT_DEPTH, SKILL_FILE

#: One file's text on the wire. A SKILL.md is ~4 KiB; this only stops a stray dump.
MAX_TEXT_BYTES = 256 * 1024
#: The listing stops here, and says so with `files_truncated`.
MAX_FILES = 200


def list_files(skill_dir: Path) -> tuple[list[dict], bool]:
    """Every file under the skill dir, relative and sorted, SKILL.md first.

    Bounded like `skill_stats`: no symlinked directories, no dot-dirs, `MAX_STAT_DEPTH` deep.
    """
    files: list[dict] = []
    truncated = False
    stack: list[tuple[Path, int]] = [(skill_dir, 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir() and not entry.is_symlink():
                    if depth + 1 < MAX_STAT_DEPTH and not entry.name.startswith("."):
                        stack.append((entry, depth + 1))
                    continue
                if not entry.is_file():
                    continue
                stat = entry.stat()
            except OSError:
                continue
            if len(files) >= MAX_FILES:
                truncated = True
                break
            files.append({"rel": entry.relative_to(skill_dir).as_posix(), "bytes": stat.st_size, "mtime": int(stat.st_mtime)})
    files.sort(key=lambda f: (f["rel"] != SKILL_FILE, f["rel"].count("/"), f["rel"]))
    return files, truncated


def resolve_file(skill_dir: Path, rel: object) -> tuple[Path | None, str | None]:
    """`rel` under the skill's REAL directory, or the reason it is refused."""
    if not isinstance(rel, str) or not rel.strip():
        return None, "missing 'file'"
    try:
        target = (skill_dir / rel).resolve()
        is_file = target.is_file()
    except (OSError, RuntimeError, ValueError):
        return None, "unresolvable file"
    if not target.is_relative_to(skill_dir):
        return None, "file is outside the skill"
    if not is_file:
        return None, "no such file in the skill"
    return target, None


def read_text(path: Path) -> tuple[str | None, bool, str | None]:
    """`(text, truncated, refusal)` — the head of a UTF-8 text file, never binary."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_TEXT_BYTES + 1)
    except OSError:
        return None, False, "unreadable file"
    if b"\x00" in raw[:8192]:
        return None, False, "binary file"
    truncated = len(raw) > MAX_TEXT_BYTES
    return raw[:MAX_TEXT_BYTES].decode("utf-8", errors="replace"), truncated, None


def handle_skill(query: dict[str, list[str]], allowlist: list[Path]) -> tuple[int, dict]:
    """The full `GET /skill?path=<skill dir>[&file=<rel>]` decision."""
    skill_dir, reason = resolve_reveal_target((query.get("path") or [""])[0], allowlist)
    if skill_dir is None:
        return 403, {"error": reason}
    if not skill_dir.is_dir():
        return 400, {"error": "path is not a skill directory"}
    rel = (query.get("file") or [SKILL_FILE])[0]
    target, reason = resolve_file(skill_dir, rel)
    if target is None:
        return (403 if reason == "file is outside the skill" else 404), {"error": reason}
    text, truncated, refusal = read_text(target)
    if refusal is not None:
        return 415, {"error": refusal}
    files, files_truncated = list_files(skill_dir)
    return 200, {
        "path": str(skill_dir),
        "file": target.relative_to(skill_dir).as_posix(),
        "text": text,
        "truncated": truncated,
        "files": files,
        "files_truncated": files_truncated,
    }
