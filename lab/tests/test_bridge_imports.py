"""The pure laya_bridge modules import in THIS venv, which has no bridge SDK.

The rule's owner is plugins/laya/README.md § Pure modules; the plugin's tests/test_purity.py
proves it from the other side (an AST walk). This side runs a fresh interpreter, so nothing another
test imported can make a broken module look fine.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys

from laya_lab import siblings

PACKAGE = siblings.REPO / "plugins" / "laya" / "laya_bridge"
#: The modules allowed to import `voidcraft_world_bridge`; every other module must be pure.
SDK = {"guard", "runtime", "plugin"}
LISTED = {
    "rows",
    "agreement",
    "files",
    "paths",
    "wire",
    "ledger",
    "landing",
    "review_surfaces",
    "review_log",
    "label_budget",
    "review_data",
    "review",
    "lab_files",
    "lab_jobs",
    "ops",
}


def pure_modules() -> list[str]:
    return sorted(path.stem for path in PACKAGE.glob("*.py") if path.stem not in SDK | {"__init__"})


def test_this_venv_has_no_bridge_sdk():
    assert importlib.util.find_spec("voidcraft_world_bridge") is None


def test_every_pure_module_imports_without_the_sdk():
    modules = pure_modules()
    assert set(modules) >= LISTED
    script = (
        "import json, sys\n"
        "from laya_lab import siblings\n"
        f"for name in {modules!r}:\n"
        "    __import__('laya_bridge.' + name)\n"
        "print(json.dumps({'sdk': 'voidcraft_world_bridge' in sys.modules, "
        "'torch': 'torch' in sys.modules}))\n"
    )
    done = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, cwd=str(siblings.REPO / "lab")
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout.strip().splitlines()[-1]) == {"sdk": False, "torch": False}
