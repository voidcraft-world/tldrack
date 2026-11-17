"""PURE — a tolerant parser for the YAML subset that SKILL.md frontmatter uses.

Plugins are stdlib-only (plugins/README.md), so pyyaml is not available
and a hand-rolled parser is the only option. That is fine, because the surface
in play is tiny: a frontmatter block's *top-level scalar* keys, of which this
plugin reads exactly two (`name`, `description`).

The forms a SKILL.md header takes (the names are invented):

    ---                          ---                        ---
    name: release-notes          name: make-video           name: cloud-deploy
    description: One line.       description: >             description: Long…
    ---                            folded line one          version: 1
                                   folded line two          ---
                                 metadata: { "tags": "…" }
                                 ---

So four things must work: plain scalars, folded (`>`) and literal (`|`) blocks,
plain scalars that wrap onto indented continuation lines, and inline flow
mappings that must be captured without exploding. Nested block mappings are
recognised and skipped rather than mangled into a string.

Everything here is total: no input raises. A file with no frontmatter, an
unterminated block, or CRLF line endings yields `{}` and the caller falls back
to the directory name.
"""

from __future__ import annotations

import re

# A top-level key at indent 0: `name:`, `allowed-tools:`, `metadata.x:`.
KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_.-]*):[ \t]*(.*)$")
# Block scalar introducers, with optional chomping/indent indicators (`>-`, `|+2`).
BLOCK_RE = re.compile(r"^([>|])([0-9+-]*)$")
# A nested mapping line inside an indented block: `  tags: value`.
NESTED_KEY_RE = re.compile(r"^[ \t]+[A-Za-z_][A-Za-z0-9_.-]*:[ \t]")

FENCE = "---"


def split_frontmatter(text: str) -> list[str]:
    """The raw lines between the opening and closing `---` fences.

    Returns `[]` unless the very first non-blank line is a `---` fence AND a
    closing `---` / `...` fence follows. An unterminated block yields `[]`
    rather than swallowing the whole document.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    start = 0
    # Tolerate a UTF-8 BOM and leading blank lines before the opening fence.
    if lines and lines[0].startswith("﻿"):
        lines[0] = lines[0][1:]
    while start < len(lines) and not lines[start].strip():
        start += 1
    if start >= len(lines) or lines[start].strip() != FENCE:
        return []
    for i in range(start + 1, len(lines)):
        if lines[i].strip() in (FENCE, "..."):
            return lines[start + 1 : i]
    return []


def _strip_quotes(value: str) -> str:
    """Unwrap a fully-quoted scalar; leave everything else byte-for-byte."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def parse_frontmatter(text: str) -> dict[str, str]:
    """Top-level scalar keys of `text`'s frontmatter block. Never raises."""
    lines = split_frontmatter(text)
    result: dict[str, str] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith("#"):
            i += 1
            continue
        match = KEY_RE.match(line)
        if match is None:
            i += 1  # stray continuation we already consumed, or malformed — skip
            continue
        key, inline = match.group(1), match.group(2).strip()

        # Gather the indented body that belongs to this key, whatever its form.
        body: list[str] = []
        j = i + 1
        while j < len(lines):
            nxt = lines[j]
            if not nxt.strip():
                # A blank line continues a block scalar but ends a plain one;
                # keep it and let the join decide.
                body.append("")
                j += 1
                continue
            if _indent_of(nxt) == 0:
                break
            body.append(nxt)
            j += 1

        block = BLOCK_RE.match(inline)
        if block is not None:
            # Folded (`>`) joins with spaces, literal (`|`) preserves newlines.
            stripped = [b.strip() for b in body]
            if block.group(1) == ">":
                value = " ".join(s for s in stripped if s)
            else:
                value = "\n".join(stripped).strip("\n")
            result[key] = value
        elif inline:
            # Plain scalar, possibly wrapping onto indented continuation lines.
            parts = [_strip_quotes(inline)]
            parts.extend(b.strip() for b in body if b.strip())
            result[key] = " ".join(parts).strip()
        else:
            # Empty inline value: a nested mapping (skip — not a scalar) or a
            # bare indented text block (keep, folded).
            meaningful = [b for b in body if b.strip()]
            if meaningful and all(NESTED_KEY_RE.match(b) for b in meaningful):
                pass  # nested mapping — this parser only exposes scalars
            elif meaningful:
                result[key] = " ".join(b.strip() for b in meaningful).strip()
            else:
                result[key] = ""
        i = j
    return result
