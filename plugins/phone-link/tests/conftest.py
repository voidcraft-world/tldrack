import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def no_operator_config(tmp_path, monkeypatch):
    """A test never reads the operator's `~/.config/tldrack/phone-link.json` or their env: config.py."""
    monkeypatch.delenv("TLDRACK_PHONE_LINK_APP", raising=False)
    monkeypatch.setenv("TLDRACK_PHONE_LINK_CONFIG", str(tmp_path / "no-phone-link-config.json"))
