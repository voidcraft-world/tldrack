"""`assets/canvas-schema.json` is what the schemas package's generator emits, byte for byte.

The plugin is stdlib Python and cannot run Zod, so it reads a generated twin of
`schemas/src/agent-canvas.ts`. This test runs the generator's `--check` mode
(exit 1 when either of its outputs is stale) whenever this checkout can run it,
and skips with the reason when it cannot: the generator not yet present, no
node new enough to strip types, or `schemas/` not installed. The schemas
package's own test runs the same check in CI.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
GENERATOR = "schemas/scripts/export-agent-canvas.ts"
#: The first node that strips TypeScript types without a flag (schemas/package.json `engines`).
MIN_NODE = (22, 18)


def node_version(node: str) -> tuple[int, int] | None:
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v(\d+)\.(\d+)", out.strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def test_the_checked_in_schema_is_the_generated_one():
    if not (ROOT / GENERATOR).is_file():
        pytest.skip(f"{GENERATOR} is not in this checkout")
    node = shutil.which("node")
    version = node_version(node) if node else None
    if version is None or version < MIN_NODE:
        pytest.skip(f"needs node >= {MIN_NODE[0]}.{MIN_NODE[1]} on PATH (found {version})")
    if not (ROOT / "schemas" / "node_modules" / "zod").is_dir():
        pytest.skip("schemas/ is not installed: run npm ci in schemas/")
    result = subprocess.run([node, GENERATOR, "--check"], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, (result.stdout + result.stderr).strip()
