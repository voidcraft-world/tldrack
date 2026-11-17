"""Bounded verifier preview; no arbitrary files are exposed through the card route.

Stdlib only: owned by plugins/laya/README.md — see "Pure modules".
"""

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from laya_bridge import files

FILENAME = "verifier-card.json"
#: Where a card's pull request goes, unless the operator names another: `plugins.laya.card_repo` in the bridge's
#: `settings.json` (README § Settings). A fork's operator points it at their own fork, not at this project.
DEFAULT_REPO = "voidcraft-world/tldrack"
#: `owner/name` as GitHub spells them; the same shape as schemas/src/laya-review.ts `GITHUB_REPO`.
REPO_SHAPE = re.compile(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+")
SETTINGS_FILE = "settings.json"


def card_repo(settings: Mapping) -> str:
    """The card's repository from this plugin's settings object; the default when unset or not `owner/name`."""
    value = settings.get("card_repo") if isinstance(settings, Mapping) else None
    return value if isinstance(value, str) and REPO_SHAPE.fullmatch(value) else DEFAULT_REPO


def configured_repo(env: Mapping[str, str] = os.environ) -> str:
    """The same answer for a process the host did not hand settings to (laya-lab): `plugins.laya.card_repo` read from
    the config role's `settings.json` (`$TLDRACK_CONFIG_DIR`, else `~/.config/tldrack`). Anything unreadable is the
    default: a card names a repository, it never refuses to exist over one."""
    raw = env.get("TLDRACK_CONFIG_DIR", "").strip()
    home = env.get("HOME", "").strip()
    config = Path(raw).expanduser() if raw else (Path(home) if os.path.isabs(home) else Path.home()) / ".config" / "tldrack"
    try:
        raw = files.read_capped(config / SETTINGS_FILE, 256 * 1024)
        body = files.loads(raw) if raw else None
    except (files.SourceError, OSError, ValueError, RecursionError):
        return DEFAULT_REPO
    plugins = body.get("plugins") if isinstance(body, dict) else None
    laya = plugins.get("laya") if isinstance(plugins, dict) else None
    return card_repo(laya if isinstance(laya, dict) else {})


#: A card holds exactly these: the three files laya-lab writes into `docs/`, and the golden set it was scored on.
ALLOWED = {"VERIFIER.md", "golden/agent-open-work.jsonl", "REPRODUCE.md", "MODEL_CARD.md"}
#: The longest file a card may carry, in UTF-16 units: the same bound as schemas/src/laya-review.ts
#: `layaVerifierCardSchema`. The public golden suite rides in the card, so it must fit (lab/tests/test_golden.py pins it).
FILE_MAX_CHARS = 100_000


def read(base, repo=DEFAULT_REPO):
    """The preview at `base`, or None. `repo` is the configured repository: a card written for another is stale."""
    if base is None:
        return None
    try:
        raw = files.read_capped(base / FILENAME, 256 * 1024)
        card = files.loads(raw) if raw else None
        if (
            not isinstance(card, dict)
            or card.get("v") != 1
            or card.get("repository") != repo
            or not isinstance(card.get("files"), dict)
            or set(card["files"]) != ALLOWED
            or any(not isinstance(v, str) or files.utf16_len(v) > FILE_MAX_CHARS for v in card["files"].values())
            or type(card.get("pairs")) is not int
            or card["pairs"] < 0
            or type(card.get("ready")) is not bool
            or not isinstance(card.get("at"), str)
        ):
            return None
        digest = hashlib.sha256(json.dumps(card["files"], sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return card if digest == card.get("digest") else None
    except (files.SourceError, ValueError):
        return None
