"""Standing missions: seeded once, never closed by accident, hidden from the rack while empty,
and the owner of an area's paths, routes and words."""

import json

import pytest

from tldrack import error_fix
from tldrack.missions import STANDING_SEEDS, MissionError, MissionStore, domain_matches, standing_seeds

# Invented seeds: the shape an operator writes under `standing_missions` in settings.json.
SEEDS = standing_seeds(
    [
        {
            "key": "docs",
            "title": "Docs",
            "keeper": "Scribe",
            "brief": "Owns the documentation.",
            "domains": ["docs/*", "site/src/guide*", "docs"],
        },
        {"key": "engine", "title": "Engine", "keeper": "Keeper", "brief": "Owns the engine.", "domains": ["engine/*", "engine"]},
        {
            "key": "ci",
            "title": "Build and CI",
            "keeper": "Builder",
            "brief": "Owns the build.",
            "domains": ["ci/*", ".github/*", "build"],
        },
        {
            "key": "rack",
            "title": "Agent Rack",
            "keeper": "Warden",
            "brief": "Owns the rack.",
            "domains": ["bridge/*", "plugins/*", "ui/*", "rack"],
        },
    ]
)


def _store(tmp_path, clock=lambda: 1.0):
    return MissionStore(tmp_path / "missions.json", clock=clock)


def test_seeds_mint_once_and_survive_a_reload(tmp_path):
    store = _store(tmp_path)
    assert store.seed_standing(SEEDS) == [seed.key for seed in SEEDS]
    assert store.seed_standing(SEEDS) == []
    reloaded = _store(tmp_path)
    assert reloaded.seed_standing(SEEDS) == [], "the file remembers what it minted"
    titles = [m["title"] for m in reloaded.standing_state()["missions"]]
    assert titles == sorted(seed.title for seed in SEEDS)
    assert json.loads((tmp_path / "missions.json").read_text())["seeded"] == sorted(s.key for s in SEEDS)


def test_a_closed_seed_is_not_minted_again(tmp_path):
    store = _store(tmp_path)
    store.seed_standing(SEEDS)
    engine = next(m for m in store.standing_state()["missions"] if m["title"] == "Engine")
    with pytest.raises(MissionError):
        store.close(engine["id"])
    store.set_standing(engine["id"], False)
    store.close(engine["id"])
    assert _store(tmp_path).seed_standing(SEEDS) == []
    assert "Engine" not in [m["title"] for m in _store(tmp_path).standing_state()["missions"]]


def test_the_rack_does_not_list_an_empty_standing_mission(tmp_path):
    store = _store(tmp_path)
    store.seed_standing(SEEDS)
    assert store.state()["missions"] == []
    assert len(store.standing_state()["missions"]) == len(SEEDS)


def test_view_carries_keeper_and_domains(tmp_path):
    store = _store(tmp_path)
    store.seed_standing(SEEDS)
    docs = next(m for m in store.standing_state()["missions"] if m["title"] == "Docs")
    assert docs["standing"] and docs["keeper"] == "Scribe" and "docs" in docs["domains"]


@pytest.mark.parametrize(
    "texts,title",
    [
        (["#/docs/guide"], "Docs"),
        (["site/src/guide-intro.md"], "Docs"),
        (["engine/src/sim/economy.ts"], "Engine"),
        (["bridge/tldrack/main.py"], "Agent Rack"),
        (["ci/scripts/build.py"], "Build and CI"),
    ],
)
def test_owning_mission(tmp_path, texts, title):
    store = _store(tmp_path)
    store.seed_standing(SEEDS)
    assert store.owning_mission(texts)["title"] == title


def test_no_owner_for_an_unknown_area(tmp_path):
    store = _store(tmp_path)
    store.seed_standing(SEEDS)
    assert store.owning_mission(["#/w/‹id›", "x is not a function"]) is None
    assert store.owning_mission(["#/docsland"]) is None, "a term matches a whole word only"


def test_domain_matches():
    assert domain_matches("engine/*", ["engine/src/x.ts"])
    assert not domain_matches("engine/*", ["site/engine.ts"])
    assert domain_matches("rack", ["the RACK row"])


def test_standing_op_validates(tmp_path):
    store = _store(tmp_path)
    mission = store.create("Temp")
    with pytest.raises(MissionError):
        store.set_standing(mission["id"], "yes")
    view = store.set_standing(mission["id"], True, "Keeper", ["a/*", "word"])
    assert view["standing"] and view["keeper"] == "Keeper" and view["domains"] == ["a/*", "word"]


def test_mission_note_in_the_brief(tmp_path):
    store = _store(tmp_path)
    store.seed_standing(SEEDS)
    owner = store.owning_mission(["#/docs/guide"])
    note = error_fix.mission_note(owner)
    assert "Scribe" in note and "not instructions" in note and "mission join" in note
    assert error_fix.mission_note(None) is None


def test_nothing_is_minted_by_default(tmp_path):
    assert STANDING_SEEDS == ()
    assert _store(tmp_path).seed_standing() == []


@pytest.mark.parametrize(
    "bad",
    [
        {"key": "Docs", "title": "Docs"},
        {"key": "docs"},
        {"key": "docs", "title": "Docs", "domains": "docs/*"},
        {"key": "docs", "title": "Docs", "keeper": 3},
    ],
)
def test_a_bad_seed_is_refused(bad):
    with pytest.raises(MissionError):
        standing_seeds([bad])


def test_a_seed_key_listed_twice_is_refused():
    with pytest.raises(MissionError):
        standing_seeds([{"key": "docs", "title": "Docs"}, {"key": "docs", "title": "More docs"}])
