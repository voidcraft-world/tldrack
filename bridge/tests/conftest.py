"""Every test gets its own empty config, state and data directories.

The kept stores (the ledger, decisions, the rack database) are on by default (`tldrack/paths.py`),
so a test that builds the production wiring would otherwise write into the home of whoever runs
the suite. A test that wants a particular layout sets its own environment on top.
"""

import pytest

from tldrack import paths


@pytest.fixture(autouse=True)
def isolated_tldrack_dirs(tmp_path_factory, monkeypatch):
    root = tmp_path_factory.mktemp("tldrack-home")
    for role in paths.ROLES:
        monkeypatch.setenv(paths.ROLE_ENV[role], str(root / role))
    # A test that runs the CLI exports this (paths.py `export_env`); it must not outlive the test.
    monkeypatch.setenv(paths.PATHS_FILE_ENV, str(root / "config" / paths.PATHS_FILE))
    return root
