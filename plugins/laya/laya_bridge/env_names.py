"""Environment variables a scheduler sets for the work it starts: the `TLDRACK_` name first, then the name the
variable was born with.

The eval runner, the lease and the run deadline were first named `VOIDCRAFT_*`, and an older runner still sets
those. tldrack reads its own name first and falls back to the old one, so a runner can move when it likes. The
TypeScript half is `schemas/src/resource-lease.ts` (`env_value`); `schemas/tests/twins.test.ts` pins the prefixes.
Stdlib only: owned by plugins/laya/README.md — see "Pure modules".
"""

from __future__ import annotations

from collections.abc import Mapping

PREFIX = "TLDRACK_"
LEGACY_PREFIX = "VOIDCRAFT_"


def legacy_name(name: str) -> str | None:
    """`TLDRACK_X` → `VOIDCRAFT_X`; any other name has no older spelling."""
    return LEGACY_PREFIX + name[len(PREFIX) :] if name.startswith(PREFIX) else None


def read(env: Mapping[str, str], name: str) -> str | None:
    """The value of `name`, else of its older spelling. An empty value counts as unset, as a scheduler that clears a
    variable means."""
    value = env.get(name)
    if value:
        return value
    old = legacy_name(name)
    return (env.get(old) or None) if old else None


def spelled(name: str) -> str:
    """`name` as a message names it: both spellings when it has two."""
    old = legacy_name(name)
    return f"{name} (or {old})" if old else name
