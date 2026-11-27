"""`app_package`: env over file over the default, and nothing that is not a package name reaches the phone's shell.
`tailnet`: off unless every field is of its shape, and a refused door is named and stays off."""

from __future__ import annotations

import json

import pytest

from phone_link.adb import PUSH_COMMAND, READ_COMMAND, Adb, Runner, push_command, read_command
from phone_link.config import APP_ENV, CONFIG_ENV, LinkConfig, TailnetDoor, load_config, valid_package
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


# -- the tailnet door -----------------------------------------------------------------------------------
DOOR = {"host": "mymac.tail1234.ts.net", "port": 7683, "login": "operator@example.com"}


def test_the_tailnet_door_is_off_by_default(tmp_path):
    assert load_config({}).tailnet is None and load_config({}).tailnet_error is None
    quiet = load_config(config_file(tmp_path, {"app_package": OTHER}))
    assert quiet.tailnet is None and quiet.tailnet_error is None
    assert quiet.tailnet_json() == {"on": False, "host": None, "port": None, "error": None}


def test_a_whole_door_is_read_and_the_app_setting_is_independent_of_it(tmp_path):
    env = config_file(tmp_path, {"tailnet": DOOR})
    config = load_config(env)
    assert config.tailnet == TailnetDoor(DOOR["host"], 7683, "operator@example.com")
    assert config.tailnet.authority == f"{DOOR['host']}:7683"
    assert config.tailnet.url == f"https://{DOOR['host']}:7683/"
    assert (config.app_package, config.source, config.error) == (DEFAULT_APP_PACKAGE, "default", None)
    # The app may come from the environment while the door comes from the file.
    both = load_config({**env, APP_ENV: OTHER})
    assert (both.app_package, both.source) == (OTHER, "env") and both.tailnet == config.tailnet
    assert config.tailnet_json() == {"on": True, "host": DOOR["host"], "port": 7683, "error": None}


@pytest.mark.parametrize(
    ("door", "says"),
    [
        ("mymac.tail1234.ts.net", "expected an object"),
        ({"host": DOOR["host"], "port": 7683}, "expected an object"),
        ({**DOOR, "funnel": True}, "expected an object"),
        ({**DOOR, "host": "*.tail1234.ts.net"}, "tailnet.host"),
        ({**DOOR, "host": ".ts.net"}, "tailnet.host"),
        ({**DOOR, "host": "tail1234.ts.net.evil.example"}, "tailnet.host"),
        ({**DOOR, "host": DOOR["host"].upper()}, "tailnet.host"),
        ({**DOOR, "host": "mymac.tail1234.ts.net:7683"}, "tailnet.host"),
        ({**DOOR, "host": "127.0.0.1"}, "tailnet.host"),
        ({**DOOR, "host": 7}, "tailnet.host"),
        ({**DOOR, "port": "7683"}, "tailnet.port"),
        ({**DOOR, "port": True}, "tailnet.port"),
        ({**DOOR, "port": 0}, "tailnet.port"),
        ({**DOOR, "port": 70000}, "tailnet.port"),
        ({**DOOR, "port": 443}, "Funnel"),
        ({**DOOR, "port": 8443}, "Funnel"),
        ({**DOOR, "port": 10000}, "Funnel"),
        ({**DOOR, "login": ""}, "tailnet.login"),
        ({**DOOR, "login": "two words"}, "tailnet.login"),
        ({**DOOR, "login": None}, "tailnet.login"),
    ],
)
def test_a_door_that_is_not_whole_stays_off_and_says_why(tmp_path, door, says):
    config = load_config(config_file(tmp_path, {"tailnet": door}))
    assert config.tailnet is None
    assert says in config.tailnet_error and "stays off" in config.tailnet_error
    assert config.tailnet_json()["on"] is False
