"""The pure modules import only the stdlib and each other: laya-lab's SDK-free venv imports them.

The rule's owner is plugins/laya/README.md § Pure modules; lab/tests/test_bridge_imports.py
proves the same from the other side, by importing each one in laya-lab's own venv.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[1] / "laya_bridge"
PURE = (
    "rows",
    "agreement",
    "files",
    "paths",
    "wire",
    "ledger",
    "landing",
    "envelope",
    "review_surfaces",
    "review_log",
    "label_budget",
    "review_data",
    "review",
    "lab_files",
    "lab_jobs",
    "ops",
    "challenger",
    "card_files",
    "install",
    "env_names",
)
SDK = ("guard", "runtime", "plugin")
POINTER = 'Stdlib only: owned by plugins/laya/README.md — see "Pure modules".'


def imports(module: str) -> list[tuple[str, int]]:
    tree = ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend((alias.name, node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{module}.py:{node.lineno} uses a relative import"
            if node.module == "laya_bridge":
                found.extend((f"laya_bridge.{alias.name}", node.lineno) for alias in node.names)
            else:
                found.append((node.module or "", node.lineno))
    return found


def test_every_module_is_classified():
    modules = {path.stem for path in PACKAGE.glob("*.py")} - {"__init__"}
    assert modules == set(PURE) | set(SDK), "a new module must be declared pure or SDK here"


@pytest.mark.parametrize("module", PURE)
def test_a_pure_module_imports_only_the_stdlib_and_pure_siblings(module):
    for name, line in imports(module):
        top = name.split(".")[0]
        if top == "laya_bridge":
            sibling = name.split(".")[1]
            assert sibling in PURE, f"{module}.py:{line} imports {name}, which is not pure"
        else:
            assert top in sys.stdlib_module_names, f"{module}.py:{line} imports {name}, which is not the stdlib"


@pytest.mark.parametrize("module", PURE)
def test_a_pure_module_says_so_in_its_docstring(module):
    docstring = ast.get_docstring(ast.parse((PACKAGE / f"{module}.py").read_text(encoding="utf-8"))) or ""
    assert POINTER in docstring, f"{module}.py: its docstring needs the one-line pointer"
