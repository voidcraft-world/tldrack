"""The CLI module imports without numpy or torch.

`label-open-work` re-runs `laya_lab.cli` in the bridge's venv (`open_work_data.reexec_in_bridge_venv`), which has
neither, so a top-level import that reaches them (`train` → `gate` → `floor`) kills the command before it parses an
argument. A fresh interpreter with both blocked proves it, so nothing another test imported can hide a regression.
"""

from __future__ import annotations

import subprocess
import sys

from laya_lab import siblings

SCRIPT = """
import importlib.abc, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name.split('.')[0] in ('numpy', 'torch'):
            raise ModuleNotFoundError(f"No module named '{name}' (blocked)")
        return None
sys.meta_path.insert(0, Block())
import laya_lab.cli
parser = laya_lab.cli.build_parser()
args = parser.parse_args(['label-open-work'])
assert args.command == 'label-open-work'
assert parser.parse_args(['train', 'agent_open_work']).epochs == 2
print('ok')
"""


def test_cli_imports_without_numpy_or_torch():
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT], capture_output=True, text=True, timeout=60, cwd=str(siblings.REPO / "lab")
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().splitlines()[-1] == "ok"
