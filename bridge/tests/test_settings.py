"""settings.py: every optional part is off until the operator's settings.json turns it on."""

import json

import pytest

from tldrack.settings import Settings, SettingsError, load_settings, parse_settings


def test_no_file_is_every_default(tmp_path):
    settings = load_settings(tmp_path / "settings.json")
    assert settings == Settings()
    assert settings.allowed_origins == frozenset() and settings.supervisor_url is None
    assert not any(settings.enabled().values()), "a stranger's bridge has nothing optional on"


def test_a_full_file_parses(tmp_path):
    lease = tmp_path / "lease.py"
    lease.write_text("def mark_live(name): pass\n")
    dist = tmp_path / "dist"
    dist.mkdir()
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "allowed_origins": ["HTTPS://Rack.Example:443/"],
                "supervisor_url": "myapp://bridge/start",
                "supervisor_process": ["MyApp"],
                "ui_dist": str(dist),
                "session_namer": True,
                "lease_module": str(lease),
                "decision_docs": ["docs/**/*.md"],
                "standing_missions": [{"key": "docs", "title": "Docs"}],
            }
        )
    )
    settings = load_settings(path)
    assert settings.allowed_origins == frozenset({"https://rack.example"}), "the browser's spelling"
    assert settings.supervisor_url == "myapp://bridge/start" and settings.ui_dist == dist
    assert settings.supervisor_process == frozenset({"MyApp"})
    assert settings.session_namer and settings.lease_module == lease
    assert settings.decision_docs == ("docs/**/*.md",) and settings.standing_missions[0]["key"] == "docs"
    assert settings.enabled() == {"session namer": True, "live marks": True, "supervisor": True, "extra origins": True}


@pytest.mark.parametrize(
    "body,needle",
    [
        ({"alowed_origins": []}, "unknown key 'alowed_origins'"),
        ({"allowed_origins": "https://rack.example"}, "allowed_origins must be a list"),
        ({"allowed_origins": ["https://rack.example/app"]}, "allowed_origins"),
        ({"allowed_origins": ["*"]}, "allowed_origins"),
        ({"supervisor_url": "not a url"}, "supervisor_url must be a URL"),
        ({"session_namer": "yes"}, "session_namer must be true or false"),
        ({"supervisor_process": "MyApp"}, "supervisor_process must be a list"),
        ({"ui_dist": "relative"}, "ui_dist must be an absolute path"),
        ({"lease_module": "/nowhere/lease.py"}, "lease_module is not a file"),
        ({"decision_docs": [""]}, "decision_docs must be a list"),
        ({"standing_missions": {"key": "docs"}}, "standing_missions must be a list"),
        ([], "expected an object"),
    ],
)
def test_a_typo_is_a_message_not_a_feature_that_stays_off(tmp_path, body, needle):
    with pytest.raises(SettingsError, match=needle):
        parse_settings(body, tmp_path / "settings.json")


def test_unreadable_json_is_an_error(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{")
    with pytest.raises(SettingsError, match="cannot read"):
        load_settings(path)


def test_plugin_objects_are_shape_checked_and_handed_out_as_copies(tmp_path):
    settings = parse_settings(
        {"plugins": {"coordinator": {"enabled": True}, "laya": {"evals_dir": "/x", "nested": {"k": [1]}}}},
        tmp_path / "settings.json",
    )
    assert settings.plugin_settings("coordinator") == {"enabled": True}
    assert settings.plugin_settings("devices") == {}, "unset reads as every default"
    mine = settings.plugin_settings("laya")
    mine["nested"]["k"].append(2)
    mine["evals_dir"] = "/elsewhere"
    assert settings.plugin_settings("laya") == {"evals_dir": "/x", "nested": {"k": [1]}}, (
        "a plugin cannot change what it, or another, reads next"
    )
    assert not any(settings.enabled().values()), "settings for a plugin turn no bridge part on"


@pytest.mark.parametrize(
    "body,needle",
    [
        ({"plugins": []}, "plugins must be an object"),
        ({"plugins": {"Coordinator": {}}}, "'Coordinator' is not a plugin name"),
        ({"plugins": {"laya": True}}, "plugins.laya must be an object"),
        ({"plugins": {f"p{n}": {} for n in range(65)}}, "at most 64"),
    ],
)
def test_a_malformed_plugins_key_is_a_message(tmp_path, body, needle):
    with pytest.raises(SettingsError, match=needle):
        parse_settings(body, tmp_path / "settings.json")
