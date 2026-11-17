"""`tldrack laya …`: the bridge's CLI runs this file with the words after `laya` (laya_bridge/install.py owns the verbs)."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from laya_bridge.install import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
