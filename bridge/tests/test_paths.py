"""paths.py: three roles, the named stores, and `paths.json`. Every directory here is a tmp one."""

import json

import pytest

from tldrack import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    """An empty home and no role variables: the resolver's own defaults."""
    monkeypatch.setenv("HOME", str(tmp_path))
    for name in (*paths.ROLE_ENV.values(), paths.PATHS_FILE_ENV):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def write_mapping(home, body):
    config = home / ".config" / "tldrack"
    config.mkdir(parents=True, exist_ok=True)
    (config / paths.PATHS_FILE).write_text(json.dumps(body))


def test_the_defaults_are_the_three_directories_under_home(home):
    assert paths.config_dir(create=False) == home / ".config" / "tldrack"
    assert paths.state_dir(create=False) == home / ".local" / "state" / "tldrack"
    assert paths.data_dir(create=False) == home / ".local" / "share" / "tldrack"
    assert not (home / ".local").exists(), "asking where is not creating"


def test_the_data_directory_always_exists_and_is_private(home):
    ledger = paths.store_dir("ledger")
    assert ledger == home / ".local" / "share" / "tldrack" / "agent-ledger" and ledger.is_dir()
    assert (ledger.stat().st_mode & 0o777) == paths.PRIVATE_MODE
    assert paths.store_dir("rack").parent == paths.store_dir("decisions").parent == paths.data_dir()


def test_reading_a_config_or_state_file_creates_nothing(home):
    assert paths.config_file("profiles.json") == home / ".config" / "tldrack" / "profiles.json"
    assert paths.state_file("x.json").parent == home / ".local" / "state" / "tldrack"
    assert not (home / ".config").exists() and not (home / ".local").exists()


def test_paths_json_maps_a_role_and_every_store_follows_it(home):
    older = home / "older-data"
    older.mkdir()
    write_mapping(home, {"data": str(older)})
    assert paths.data_dir() == older
    assert paths.store_dir("ledger") == older / "agent-ledger"
    assert paths.store_dir("rack") == older / "agent-rack"
    assert paths.state_dir(create=False) == home / ".local" / "state" / "tldrack", "unmapped roles keep the default"


def test_paths_json_maps_one_store_by_itself(home):
    big = home / "big-disk"
    big.mkdir()
    write_mapping(home, {"stores": {"ledger": str(big)}})
    assert paths.store_dir("ledger") == big
    assert paths.store_dir("decisions") == paths.data_dir() / "decisions"


def test_the_environment_wins_over_paths_json(home, monkeypatch):
    mapped, env = home / "mapped", home / "env"
    mapped.mkdir()
    write_mapping(home, {"state": str(mapped)})
    monkeypatch.setenv(paths.ROLE_ENV["state"], str(env))
    assert paths.state_dir() == env and env.is_dir()


def test_a_moved_config_role_never_moves_paths_json(home):
    elsewhere = home / "elsewhere"
    elsewhere.mkdir()
    write_mapping(home, {"config": str(elsewhere)})
    assert paths.config_dir() == elsewhere
    assert paths.paths_file() == home / ".config" / "tldrack" / paths.PATHS_FILE


@pytest.mark.parametrize(
    "body,needle",
    [
        ({"data": "relative/dir"}, "not an existing absolute directory"),
        ({"data": "/nowhere/at/all"}, "not an existing absolute directory"),
        ({"data": 7}, "must name an existing absolute directory"),
        ({"cache": "/tmp"}, "unknown key 'cache'"),
        ({"stores": {"photos": "/tmp"}}, "unknown store 'photos'"),
        ({"stores": ["ledger"]}, "stores must be an object"),
        (["data"], "expected an object"),
    ],
)
def test_a_bad_mapping_is_an_error_at_startup_not_an_empty_store(home, body, needle):
    write_mapping(home, body)
    with pytest.raises(paths.PathsError, match=needle):
        paths.export_env()


def test_unreadable_json_is_an_error(home):
    config = home / ".config" / "tldrack"
    config.mkdir(parents=True)
    (config / paths.PATHS_FILE).write_text("{not json")
    with pytest.raises(paths.PathsError, match="cannot read"):
        paths.data_dir()


def test_export_env_writes_what_it_resolved(home, monkeypatch):
    older = home / "older"
    older.mkdir()
    write_mapping(home, {"data": str(older)})
    exported = paths.export_env()
    assert exported[paths.ROLE_ENV["data"]] == str(older)
    assert {name: paths.os.environ[name] for name in exported} == exported
    for name in exported:  # monkeypatch never set these, so undo by hand
        monkeypatch.delenv(name, raising=False)


def test_a_store_mapping_survives_the_export_when_the_config_role_moved(home, monkeypatch):
    """After the export the config role is in the environment. The mapping file must still be the
    one that was read, or every store it maps silently falls back under the data role."""
    elsewhere, big = home / "elsewhere", home / "big"
    elsewhere.mkdir()
    big.mkdir()
    write_mapping(home, {"config": str(elsewhere), "stores": {"ledger": str(big)}})
    exported = paths.export_env()
    try:
        assert paths.config_dir() == elsewhere and paths.store_dir("ledger") == big
    finally:
        for name in exported:
            monkeypatch.delenv(name, raising=False)


def test_describe_says_who_decided(home, monkeypatch):
    big = home / "big"
    big.mkdir()
    write_mapping(home, {"stores": {"ledger": str(big)}})
    monkeypatch.setenv(paths.ROLE_ENV["state"], str(home / "s"))
    described = paths.describe()
    assert described["state"]["source"] == "env" and described["data"]["source"] == "default"
    assert described["stores"]["ledger"] == {"dir": str(big), "source": "paths.json"}
    assert described["stores"]["rack"]["source"] == "data"
    write_mapping(home, {"nope": 1})
    assert "unknown key" in paths.describe()["error"]


def test_the_kept_stores_resolve_without_any_parameter_directory(home):
    """The stores that used to stay off until an environment variable named their parent."""
    from tldrack.decisions import decisions_dir
    from tldrack.ledger.store import ledger_dir
    from tldrack.rack_db import resolve_rack_db_path
    from tldrack.topic_labels import resolve_topic_labels_path

    data = home / ".local" / "share" / "tldrack"
    assert ledger_dir() == data / "agent-ledger"
    assert decisions_dir() == data / "decisions"
    assert resolve_rack_db_path() == data / "agent-rack" / "rack.sqlite"
    assert resolve_topic_labels_path() == data / "agent-rack" / "topic-labels.jsonl"
