"""The built-in-command scrape: regex shape, floor union, cache invalidation.

The fixture below is written in the shape the Claude Code binary bundles its commands
in (the property names and their order), with the prose shortened. If a future CLI
changes that shape, this is the test that should fail first.
"""

import json

from claude_skills.builtins import (
    CURATED_BUILTINS,
    BuiltinsScanner,
    cache_key,
    extract_builtins,
    merge_with_floor,
    scan_binary,
)

# Note `requires:{workspace:!0}` sitting between name and menuDescription — the
# nested brace that broke a naive forward-matching regex during development.
BLOB = (
    b'}]}})}var AGS="unrelated string";var Lim=S(()=>{oy();Sy()});'
    b'function RGS(){return"# Fewer Permission Prompts\\n\\nLook through\\u2026"}'
    b'function Mim(){ou({name:"fewer-permission-prompts",requires:{workspace:!0},'
    b'menuDescription:"Pre-approve safe read-only commands based on your usage",'
    b'description:"Scan your transcripts for common read-only calls.",userInvocable:!0,'
    b"async getPromptForCommand(e){let t=RGS();if(e)t+=`\n"
    b'ou({name:"loop",menuDescription:"Repeat a prompt or command on an interval",'
    b'description:"Run a prompt on a recurring interval.",userInvocable:!0});'
    b'ou({name:"whiteboard",menuDescription:"Sketch on a whiteboard Artifact you can send",'
    b"userInvocable:!0});"
)


def test_extract_finds_names_across_intervening_properties():
    found = extract_builtins(BLOB)
    assert found["fewer-permission-prompts"] == "Pre-approve safe read-only commands based on your usage"
    assert found["loop"] == "Repeat a prompt or command on an interval"
    assert found["whiteboard"] == "Sketch on a whiteboard Artifact you can send"


def test_extract_ignores_unrelated_strings():
    assert "unrelated" not in extract_builtins(BLOB)


def test_extract_unescapes_js_escapes():
    blob = b'ou({name:"explain-usage",menuDescription:"Where did this session\\u2019s tokens go?"});'
    assert extract_builtins(blob)["explain-usage"] == "Where did this session’s tokens go?"


def test_extract_survives_a_malformed_escape():
    """A `\\'` is legal JS but invalid JSON — must fall back, not raise."""
    blob = b'ou({name:"doctor",menuDescription:"it\\\'s fine"});'
    assert "doctor" in extract_builtins(blob)


def test_single_character_names_are_not_matched():
    """NAME_RE requires >=2 chars deliberately: no built-in command is one letter,
    and the looser pattern invites false positives from minified identifiers."""
    assert extract_builtins(b'ou({name:"x",menuDescription:"desc"});') == {}


def test_extract_on_empty_and_garbage_input():
    assert extract_builtins(b"") == {}
    assert extract_builtins(b"\x00\xff\xfe binary noise") == {}


def test_menu_description_with_no_preceding_name_is_skipped():
    assert extract_builtins(b'menuDescription:"orphan with no name"') == {}


def test_nearest_preceding_name_wins():
    blob = b'ou({name:"far"});ou({name:"near",menuDescription:"desc"});'
    assert extract_builtins(blob) == {"near": "desc"}


# ── floor union ──────────────────────────────────────────────────────────────


def test_merge_prefers_the_scraped_description():
    records = {r["name"]: r for r in merge_with_floor({"loop": "scraped text"})}
    assert records["loop"]["description"] == "scraped text"
    assert records["loop"]["source"] == "scraped"


def test_merge_backfills_missing_commands_from_the_floor():
    records = {r["name"]: r for r in merge_with_floor({"loop": "x"})}
    assert set(CURATED_BUILTINS) <= set(records)
    assert records["dataviz"]["source"] == "curated"


def test_merge_keeps_scraped_commands_absent_from_the_floor():
    """The whole point of scraping: surfacing commands the floor never knew."""
    records = {r["name"]: r for r in merge_with_floor({"whiteboard": "Sketch"})}
    assert "whiteboard" not in CURATED_BUILTINS
    assert records["whiteboard"]["source"] == "scraped"


def test_merge_of_a_failed_scrape_still_returns_the_floor():
    records = merge_with_floor({})
    assert len(records) == len(CURATED_BUILTINS)
    assert all(r["source"] == "curated" for r in records)


def test_builtin_records_have_no_revealable_path():
    assert all(r["path"] is None for r in merge_with_floor({"loop": "x"}))
    assert all(r["tier"] == "builtin" for r in merge_with_floor({"loop": "x"}))


def test_merge_output_is_sorted():
    names = [r["name"] for r in merge_with_floor({"zzz": "a", "aaa": "b"})]
    assert names == sorted(names)


# ── chunked reading + cache ──────────────────────────────────────────────────


def test_scan_binary_finds_matches_split_across_chunks(tmp_path, monkeypatch):
    """A registration straddling a chunk boundary must still be found.

    Scaled-down but proportional to production: the search window
    (overlap + chunk) stays comfortably wider than one registration block, which
    is the invariant scan_binary depends on. Padding is chosen so registrations
    land across several boundaries.
    """
    monkeypatch.setattr("claude_skills.builtins.CHUNK_BYTES", 512)
    monkeypatch.setattr("claude_skills.builtins.OVERLAP_BYTES", 512)
    binary = tmp_path / "claude"
    for padding in (0, 100, 400, 500, 511):
        binary.write_bytes(b"\x00" * padding + BLOB + b"\x00" * 300)
        found = scan_binary(binary)
        assert "fewer-permission-prompts" in found, f"lost at padding {padding}"
        assert "whiteboard" in found, f"lost at padding {padding}"


def test_scan_binary_on_a_missing_file_is_empty(tmp_path):
    assert scan_binary(tmp_path / "nope") == {}


def test_cache_key_changes_with_mtime_and_size(tmp_path):
    binary = tmp_path / "claude"
    binary.write_bytes(b"a")
    first = cache_key(binary)
    binary.write_bytes(b"bb")  # different size + mtime
    assert first != cache_key(binary)


def test_cache_key_of_a_missing_file_is_none(tmp_path):
    assert cache_key(tmp_path / "nope") is None


def test_scanner_reads_a_warm_cache_without_rescanning(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_bytes(BLOB)
    cache_dir = tmp_path / "cache"
    scanner = BuiltinsScanner(cache_dir=cache_dir)
    monkeypatch.setattr("claude_skills.builtins.find_cli_binary", lambda: binary)

    cache_dir.mkdir()
    cached = [
        {
            "tier": "builtin",
            "name": "from-cache",
            "id": "from-cache",
            "description": "d",
            "path": None,
            "plugin": None,
            "source": "scraped",
        }
    ]
    (cache_dir / f"claude-builtins-{cache_key(binary)}.json").write_text(json.dumps(cached))

    result = scanner.get()
    assert result["status"] == "ready"
    assert [r["name"] for r in result["skills"]] == ["from-cache"]


def test_scanner_reports_scanning_then_ready(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_bytes(BLOB)
    scanner = BuiltinsScanner(cache_dir=tmp_path / "cache")
    monkeypatch.setattr("claude_skills.builtins.find_cli_binary", lambda: binary)

    first = scanner.get()
    assert first["status"] == "scanning"
    # Even mid-scan the panel is populated, never blank.
    assert len(first["skills"]) == len(CURATED_BUILTINS)

    for thread in list(__import__("threading").enumerate()):
        if thread.name == "claude-skills-builtins":
            thread.join(timeout=10)
    second = scanner.get()
    assert second["status"] == "ready"
    assert "whiteboard" in {r["name"] for r in second["skills"]}


def test_scanner_without_a_cli_on_path(monkeypatch, tmp_path):
    monkeypatch.setattr("claude_skills.builtins.find_cli_binary", lambda: None)
    result = BuiltinsScanner(cache_dir=tmp_path).get()
    assert result["status"] == "unavailable"
    assert len(result["skills"]) == len(CURATED_BUILTINS)  # floor still served


def test_scanner_tolerates_a_corrupt_cache_file(tmp_path, monkeypatch):
    binary = tmp_path / "claude"
    binary.write_bytes(BLOB)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    (cache_dir / f"claude-builtins-{cache_key(binary)}.json").write_text("{ truncated")
    monkeypatch.setattr("claude_skills.builtins.find_cli_binary", lambda: binary)
    assert BuiltinsScanner(cache_dir=cache_dir).get()["status"] == "scanning"
