"""Machinery names: which ones the bridge refuses for an agent, and that the page's defaults are the same list."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tldrack import service_names

UI_FLAGS = Path(__file__).resolve().parents[2] / "ui" / "src" / "config" / "service-session-flags.ts"


def test_a_name_under_a_reserved_prefix_is_named_with_its_prefix():
    assert service_names.reserved_prefix("laya-limits") == "laya-"
    assert service_names.reserved_prefix("svc-vite") == "svc-"
    assert "'laya-'" in service_names.reserved_error("laya-limits")


@pytest.mark.parametrize("name", ["vc-3", "limits-laya", "Laya-limits", "service-notes", "layabout", "svc", None, 7])
def test_any_other_name_is_free(name):
    assert service_names.reserved_prefix(name) is None and service_names.reserved_error(name) is None


def test_the_page_s_default_prefixes_are_the_bridge_s():
    """Two languages, one list: a prefix added on one side fails here until the other has it."""
    if not UI_FLAGS.is_file():
        pytest.skip("no ui/ beside this bridge")
    text = UI_FLAGS.read_text(encoding="utf-8")

    def listed(key: str) -> tuple[str, ...]:
        found = re.search(rf"{key}:\s*\[([^\]]*)\]", text)
        assert found, f"{key} not found in {UI_FLAGS.name}"
        return tuple(re.findall(r"'([^']+)'", found.group(1)))

    assert listed("service_prefixes") == service_names.SERVICE_PREFIXES
    assert listed("job_prefixes") == service_names.JOB_PREFIXES
