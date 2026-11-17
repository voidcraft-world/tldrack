"""The SKILL.md frontmatter parser, one test per form a SKILL.md header takes.

Every skill name and description here is invented; only the shapes matter.
"""

from claude_skills.frontmatter import parse_frontmatter, split_frontmatter


def test_plain_scalars():
    """The common case: one-line scalars."""
    meta = parse_frontmatter(
        "---\nname: release-notes\ndescription: Drafts the next release-notes entry.\n---\n\n# Release notes\n"
    )
    assert meta["name"] == "release-notes"
    assert meta["description"] == "Drafts the next release-notes entry."


def test_folded_block_scalar():
    """`description: >`, a folded block, with a flow mapping after it."""
    meta = parse_frontmatter(
        "---\n"
        "name: make-video\n"
        "description: >\n"
        "  READ THIS FIRST for any request to make a video.\n"
        "  It renders video from HTML.\n"
        'metadata: { "tags": "read-first, video" }\n'
        "---\n"
    )
    assert meta["name"] == "make-video"
    assert meta["description"] == ("READ THIS FIRST for any request to make a video. It renders video from HTML.")


def test_literal_block_preserves_newlines():
    meta = parse_frontmatter("---\nname: x\ndescription: |\n  line one\n  line two\n---\n")
    assert meta["description"] == "line one\nline two"


def test_block_scalar_with_chomping_indicator():
    meta = parse_frontmatter("---\nname: x\ndescription: >-\n  folded text\n---\n")
    assert meta["description"] == "folded text"


def test_plain_scalar_wrapping_onto_continuation_lines():
    """YAML lets a plain scalar wrap without any `>` introducer."""
    meta = parse_frontmatter("---\nname: x\ndescription: starts here\n  and continues here\nversion: 1\n---\n")
    assert meta["description"] == "starts here and continues here"
    assert meta["version"] == "1"


def test_extra_keys_between_name_and_description():
    meta = parse_frontmatter("---\nname: cloud-deploy\ndescription: Ships the app.\nversion: 1\n---\n")
    assert meta == {"name": "cloud-deploy", "description": "Ships the app.", "version": "1"}


def test_quoted_scalar_is_unwrapped():
    assert parse_frontmatter('---\nname: "quoted"\n---\n')["name"] == "quoted"


def test_nested_mapping_is_skipped_not_mangled():
    """A nested block mapping is not a scalar — it must not become a string."""
    meta = parse_frontmatter("---\nname: x\nmetadata:\n  tags: a, b\n  owner: me\ndescription: kept\n---\n")
    assert "metadata" not in meta
    assert meta["description"] == "kept"


def test_no_frontmatter_returns_empty():
    assert parse_frontmatter("# Just a heading\n\nSome prose.\n") == {}


def test_unterminated_frontmatter_returns_empty():
    """Must not swallow the whole document as one giant block."""
    assert parse_frontmatter("---\nname: x\ndescription: y\n\n# body, no closing fence\n") == {}


def test_crlf_line_endings():
    meta = parse_frontmatter("---\r\nname: x\r\ndescription: y\r\n---\r\n")
    assert meta == {"name": "x", "description": "y"}


def test_leading_bom_and_blank_lines():
    assert parse_frontmatter("﻿\n---\nname: x\n---\n")["name"] == "x"


def test_empty_input_is_total():
    assert parse_frontmatter("") == {}
    assert split_frontmatter("") == []


def test_comments_are_ignored():
    meta = parse_frontmatter("---\n# a comment\nname: x\n---\n")
    assert meta == {"name": "x"}
