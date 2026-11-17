"""`app_package`: env over file over the default, and nothing that is not a package name reaches the phone's shell."""

from __future__ import annotations

import json

import pytest

from phone_link.adb import PUSH_COMMAND, READ_COMMAND, Adb, Runner, push_command, read_command
from phone_link.config import APP_ENV, CONFIG_ENV, LinkConfig, load_config, valid_package
from phone_link.paths import DEFAULT_APP_PACKAGE

OTHER = "org.example.review"


def config_file(tmp_path, content) -> dict[str, str]:
    path = tmp_path / "phone-link.json"
    path.write_text(content if isinstance(content, str) else json.dumps(content))
    return {CONFIG_ENV: str(path)}


def test_nothing_configured_is_the_default_and_says_nothing(tmp_path):
    config = load_config({CONFIG_ENV: str(tmp_path / "absent.json")})
    assert config == LinkConfig(DEFAULT_APP_PACKAGE, "default", None)
    assert DEFAULT_APP_PACKAGE == "world.voidcraft.tldrack"


def test_the_file_sets_it(tmp_path):
    assert load_config(config_file(tmp_path, {"app_package": OTHER})) == LinkConfig(OTHER, "file", None)


def test_a_file_without_the_key_is_the_default(tmp_path):
    assert load_config(config_file(tmp_path, {"other": 1})) == LinkConfig()


def test_env_wins_over_the_file(tmp_path):
    env = {**config_file(tmp_path, {"app_package": "org.example.file"}), APP_ENV: OTHER}
    assert load_config(env) == LinkConfig(OTHER, "env", None)


@pytest.mark.parametrize(
    "content, says",
    [
        ("{not json", "not JSON"),
        ("[1, 2]", "not a JSON object"),
        ({"app_package": 7}, "'int'"),
        ({"app_package": "single"}, "'single'"),
    ],
)
def test_a_broken_file_is_named_and_the_default_stands(tmp_path, content, says):
    config = load_config(config_file(tmp_path, content))
    assert (config.app_package, config.source) == (DEFAULT_APP_PACKAGE, "default")
    assert says in config.error and DEFAULT_APP_PACKAGE in config.error


@pytest.mark.parametrize(
    "value",
    [
        "x.y; reboot",
        "x.y' && cat /data/x '",
        "x.y$(id)",
        "x.y`id`",
        "x.y z",
        "x.y\n",
        "x..y",
        ".x.y",
        "x.y.",
        "1x.y",
        "x.1y",
        "x-y.z",
        "x.y/../z",
        "x",
        "",
        "a." + "b" * 300,
    ],
)
def test_nothing_shaped_like_a_shell_word_is_accepted(tmp_path, value):
    assert not valid_package(value)
    config = load_config(
        {CONFIG_ENV: str(tmp_path / "absent.json"), APP_ENV: value} if value else config_file(tmp_path, {"app_package": value})
    )
    assert config.app_package == DEFAULT_APP_PACKAGE
    with pytest.raises(ValueError):
        read_command(value)
    with pytest.raises(ValueError):
        push_command(value)


@pytest.mark.parametrize("value", ["world.voidcraft.tldrack", "org.example.review", "a.b", "A_1.b_2.C3"])
def test_package_names_are_accepted(value):
    assert valid_package(value)


def test_the_commands_name_the_configured_app():
    assert read_command(OTHER) == f"run-as {OTHER} cat files/phone-link-token"
    assert push_command(OTHER).startswith(f"run-as {OTHER} sh -c 'umask 077")
    assert (read_command(DEFAULT_APP_PACKAGE), push_command(DEFAULT_APP_PACKAGE)) == (READ_COMMAND, PUSH_COMMAND)


def test_an_adb_handle_refuses_a_bad_package_before_any_call():
    with pytest.raises(ValueError):
        Adb("/fake/adb", Runner(), "x.y; reboot")
