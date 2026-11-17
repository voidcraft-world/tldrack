"""Store tests: atomic write, tolerant load, dismissal round-trip, no-op guard.

The dismissal set is the part of this feature that decides whether the audit
stays useful past its second run, so losing it to one malformed row — or to a
crash mid-write — is the failure mode worth testing hardest.
"""

from __future__ import annotations

import json

from coordinator.store import (
    CoordinatorStore,
    empty_scan,
    is_finding_key,
    parse_store_file,
    serialize_store_file,
)


def finding(key: str, kind: str = "open-offer") -> dict:
    return {"key": key, "kind": kind, "severity": 3, "headline": "h", "last_active": None}


def scan_with(*findings: dict) -> dict:
    scan = empty_scan()
    scan.update(scanned_at="2026-08-02T12:00:00Z", scanned=60, findings=list(findings))
    return scan


# ── writing ────────────────────────────────────────────────────────────────


def test_write_is_atomic_and_leaves_no_temp_files(tmp_path):
    path = tmp_path / "state" / "coordinator-findings.json"
    store = CoordinatorStore(path)
    store.set_scan(scan_with(finding("aaaaaaaa:open-offer")))

    assert path.is_file()
    body = json.loads(path.read_text())
    assert body["version"] == 1
    assert body["scan"]["findings"][0]["key"] == "aaaaaaaa:open-offer"
    # os.replace means the only artefact is the final file — a leftover .tmp
    # would mean a partial write escaped.
    assert [p.name for p in path.parent.iterdir()] == [path.name]


def test_set_scan_is_a_no_op_when_nothing_changed(tmp_path):
    path = tmp_path / "coordinator-findings.json"
    store = CoordinatorStore(path)
    writes = []
    original = store._persist_locked
    store._persist_locked = lambda: (writes.append(1), original())[1]

    scan = scan_with(finding("aaaaaaaa:open-offer"))
    store.set_scan(scan)
    store.set_scan(dict(scan))  # same content, fresh dict
    assert writes == [1]
    store.set_scan(scan_with(finding("bbbbbbbb:open-offer")))
    assert writes == [1, 1]


def test_stored_scan_is_copied_not_aliased(tmp_path):
    store = CoordinatorStore(tmp_path / "s.json")
    scan = scan_with(finding("aaaaaaaa:open-offer"))
    store.set_scan(scan)

    handed_out, _dismissed = store.snapshot()
    handed_out["findings"][0]["headline"] = "mutated by a client"
    scan["findings"][0]["headline"] = "mutated by the caller"

    fresh, _ = store.snapshot()
    assert fresh["findings"][0]["headline"] == "h"


# ── loading ────────────────────────────────────────────────────────────────


def test_load_drops_one_bad_row_and_keeps_the_rest(tmp_path):
    path = tmp_path / "s.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "scan": {
                    "scanned_at": "2026-08-02T12:00:00Z",
                    "scanned": 12,
                    "ranked": True,
                    "error": None,
                    "findings": [
                        finding("aaaaaaaa:open-offer"),
                        {"key": "no-kind-here:open-offer"},  # missing `kind`
                        {"kind": "open-offer"},  # missing `key`
                        "not even a dict",
                        finding("bbbbbbbb:blocked", "blocked"),
                    ],
                },
                "dismissed": ["cccccccc:stated-next-step", 42, "", "a bad key with spaces"],
            }
        )
    )
    store = CoordinatorStore(path)
    scan, dismissed = store.snapshot()

    assert [f["key"] for f in scan["findings"]] == ["aaaaaaaa:open-offer", "bbbbbbbb:blocked"]
    assert dismissed == {"cccccccc:stated-next-step"}
    assert scan["ranked"] is True
    assert scan["scanned"] == 12


def test_corrupt_envelope_is_ignored_and_not_clobbered(tmp_path):
    # A file the user might want to hand-repair must survive being unreadable.
    path = tmp_path / "s.json"
    path.write_text("{ this is not json")
    original = path.read_text()
    store = CoordinatorStore(path)
    scan, dismissed = store.snapshot()
    assert scan["findings"] == [] and dismissed == set()
    assert path.read_text() == original


def test_missing_file_starts_empty(tmp_path):
    scan, dismissed = CoordinatorStore(tmp_path / "never-written.json").snapshot()
    assert scan == empty_scan()
    assert dismissed == set()


def test_parse_store_file_rejects_a_non_object_envelope():
    for text in ("[]", '"nope"', "17"):
        try:
            parse_store_file(text)
        except ValueError:
            continue
        raise AssertionError(f"{text!r} should have been rejected")


# ── dismissals ─────────────────────────────────────────────────────────────


def test_dismissal_round_trips_through_the_file(tmp_path):
    path = tmp_path / "s.json"
    store = CoordinatorStore(path)
    store.set_scan(scan_with(finding("aaaaaaaa:open-offer")))
    assert store.dismiss("aaaaaaaa:open-offer") is True
    assert store.dismiss("aaaaaaaa:open-offer") is False  # already suppressed

    # A fresh store over the same file is what a bridge restart looks like.
    reopened = CoordinatorStore(path)
    _scan, dismissed = reopened.snapshot()
    assert dismissed == {"aaaaaaaa:open-offer"}


def test_dismissals_survive_a_later_scan(tmp_path):
    # The whole point: a re-scan must not resurrect what the user dismissed.
    path = tmp_path / "s.json"
    store = CoordinatorStore(path)
    store.set_scan(scan_with(finding("aaaaaaaa:open-offer")))
    store.dismiss("aaaaaaaa:open-offer")
    store.set_scan(scan_with(finding("aaaaaaaa:open-offer"), finding("bbbbbbbb:open-offer")))
    _scan, dismissed = store.snapshot()
    assert dismissed == {"aaaaaaaa:open-offer"}


def test_restore_clears_everything_and_reports_the_count(tmp_path):
    store = CoordinatorStore(tmp_path / "s.json")
    store.dismiss("aaaaaaaa:open-offer")
    store.dismiss("bbbbbbbb:stated-next-step")
    assert store.restore() == 2
    assert store.restore() == 0
    _scan, dismissed = store.snapshot()
    assert dismissed == set()


def test_finding_key_shape_guard():
    assert is_finding_key("abcdef01:rejected-plan")
    assert is_finding_key("abcdef01:some-future-kind")  # vocabulary is signals.py's job
    for bad in ("", "no-colon", "abcdef01:", ":kind", "abcdef01:Kind", 7, None, "abcdef01:kind with spaces"):
        assert not is_finding_key(bad)


def test_serialize_is_stable_and_sorted(tmp_path):
    text = serialize_store_file(empty_scan(), {"bbbbbbbb:open-offer", "aaaaaaaa:blocked"})
    assert json.loads(text)["dismissed"] == ["aaaaaaaa:blocked", "bbbbbbbb:open-offer"]
    assert text.endswith("\n")


# ── an unwritable state dir degrades, it does not raise ────────────────────


def test_an_unwritable_state_dir_keeps_the_change_in_memory(tmp_path, capsys):
    """mkdir and mkstemp are the likeliest OSError sites, so they have to be
    INSIDE the try. When they were outside, a read-only state dir threw straight
    out of `POST /dismiss` — the row stayed on screen while the store had
    already suppressed it, and the audit worker's own error handler raised the
    same error again while reporting the first."""
    directory = tmp_path / "state"
    directory.mkdir()
    store = CoordinatorStore(directory / "s.json")
    directory.chmod(0o500)  # read + execute, no write — same class as ENOSPC
    try:
        assert store.dismiss("aaaaaaaa:open-offer") is True
        store.set_scan(scan_with(finding("bbbbbbbb:open-offer")))
    finally:
        directory.chmod(0o700)

    assert "kept in memory only" in capsys.readouterr().err
    scan, dismissed = store.snapshot()
    # In-memory state is still correct and self-consistent; only durability lost.
    assert dismissed == {"aaaaaaaa:open-offer"}
    assert [f["key"] for f in scan["findings"]] == ["bbbbbbbb:open-offer"]
    assert list(directory.iterdir()) == []  # no orphaned .tmp left behind
