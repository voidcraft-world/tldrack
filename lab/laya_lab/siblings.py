"""The two sibling packages laya-lab reads conventions from, put on `sys.path` once.

- `plugins/laya` owns WHERE fine-tunes live and how `models.json` is read (`laya_bridge.paths`).
- `plugins/coordinator` owns the open-work question and its findings store.
- `tldrack` owns the agent rack's turn-end question (`tldrack/open_work.py`); only
  that stdlib module is imported, never the bridge's server.

laya-lab is not a bridge plugin, so the "plugins never import each other" rule does not bind it;
importing the owners is what keeps the trainer and the servers agreeing.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for sibling in (REPO / "plugins" / "laya", REPO / "plugins" / "coordinator", REPO / "bridge"):
    if str(sibling) not in sys.path:
        sys.path.insert(0, str(sibling))
