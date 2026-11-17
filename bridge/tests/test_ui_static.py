"""ui_static.py: which build is served, and that nothing outside it ever is."""

import pytest

from tldrack import ui_static


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html>rack")
    (root / "review.html").write_text("review")
    (root / "assets" / "app-abc123.js").write_text("export {}")
    (tmp_path / "secret.txt").write_text("not served")
    return root


def test_root_is_the_index_and_files_resolve_inside_the_build(dist):
    assert ui_static.resolve(dist, "/") == dist.resolve() / "index.html"
    assert ui_static.resolve(dist, "/review.html").name == "review.html"
    assert ui_static.resolve(dist, "/assets/app-abc123.js").name == "app-abc123.js"
    assert ui_static.resolve(dist, "/assets/") is None, "a directory with no index names nothing"
    assert ui_static.resolve(dist, "/missing.js") is None


@pytest.mark.parametrize(
    "path",
    [
        "/../secret.txt",
        "/assets/../../secret.txt",
        "/%2e%2e/secret.txt",
        "/assets/%2e%2e/%2e%2e/secret.txt",
        "//etc/passwd",
        "/\x00index.html",
        "/..",
        "/assets/..%2f..%2fsecret.txt",
    ],
)
def test_nothing_outside_the_build_is_named(dist, path):
    assert ui_static.resolve(dist, path) is None


def test_a_symlink_that_leaves_the_build_names_nothing(dist, tmp_path):
    (dist / "leak.txt").symlink_to(tmp_path / "secret.txt")
    assert ui_static.resolve(dist, "/leak.txt") is None


def test_hashed_assets_are_immutable_and_pages_are_revalidated(dist):
    assert "immutable" in ui_static.cache_control(dist, dist / "assets" / "app-abc123.js")
    assert ui_static.cache_control(dist, dist / "index.html") == "no-cache"
    assert ui_static.content_type(dist / "index.html") == "text/html; charset=utf-8"
    assert ui_static.content_type(dist / "assets" / "app-abc123.js") == "text/javascript; charset=utf-8"
    assert ui_static.content_type(dist / "font.woff2") == "font/woff2"


def test_the_first_build_that_holds_an_index_is_served(dist, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert ui_static.find_ui_dist(None, {ui_static.UI_DIST_ENV: str(dist)}) == dist
    assert ui_static.find_ui_dist(dist, {}) == dist
    assert ui_static.find_ui_dist(dist, {ui_static.UI_DIST_ENV: str(empty)}) == dist, (
        "a directory with no index.html is not a build"
    )
