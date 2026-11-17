import hashlib
import json
from types import SimpleNamespace
from laya_bridge import card_files
from laya_bridge.plugin import LayaPlugin

HEADERS = {"host": "localhost:7682", "origin": "http://localhost:5173", "content-type": "application/json"}


def preview(tree, repo=card_files.DEFAULT_REPO):
    outgoing = {
        "VERIFIER.md": "Public counts",
        "golden/agent-open-work.jsonl": "{}\n",
        "REPRODUCE.md": "command",
        "MODEL_CARD.md": "Base model",
    }
    digest = hashlib.sha256(json.dumps(outgoing, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    out = {
        "v": 1,
        "repository": repo,
        "at": "2026-11-09T10:00:00Z",
        "pairs": 20,
        "ready": True,
        "digest": digest,
        "files": outgoing,
    }
    tree.path(card_files.FILENAME).write_text(json.dumps(out))
    return out


def test_preview_and_armed_publish_contract(tree, fake_runtime, monkeypatch):
    import subprocess

    out = preview(tree)
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    status, body = plugin.handle_request("GET", "/card", {}, None, HEADERS)
    assert status == 200 and body["card"] == out
    status, body = plugin.handle_request("POST", "/card/publish", {}, {"digest": "stale"}, HEADERS)
    assert status == 409
    sent = []

    def fake(args, **kwargs):
        sent.append(args)
        return SimpleNamespace(
            returncode=0, stdout=json.dumps({"written": ["docs/VERIFIER.md", "docs/REPRODUCE.md", "docs/MODEL_CARD.md"]}) + "\n"
        )

    monkeypatch.setattr(subprocess, "run", fake)
    status, body = plugin.handle_request("POST", "/card/publish", {}, {"digest": out["digest"]}, HEADERS)
    assert status == 200 and body["url"] == "" and body["written"][0] == "docs/VERIFIER.md"  # written here, published nowhere
    assert body["repository"] == card_files.DEFAULT_REPO
    assert sent[0][-4:] == ["card", "--publish", "--digest", out["digest"]]


def test_cross_origin_publish_never_runs(tree, fake_runtime, monkeypatch):
    import subprocess

    out = preview(tree)
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("spawned")))
    status, _ = plugin.handle_request(
        "POST", "/card/publish", {}, {"digest": out["digest"]}, {**HEADERS, "origin": "https://evil.example"}
    )
    assert status == 403


def test_a_card_missing_a_file_or_holding_an_extra_one_is_no_preview(tree):
    out = preview(tree)
    for files in ({k: v for k, v in out["files"].items() if k != "MODEL_CARD.md"}, {**out["files"], "EXTRA.md": "x"}):
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        tree.path(card_files.FILENAME).write_text(json.dumps({**out, "files": files, "digest": digest}))
        assert card_files.read(tree.base) is None


def test_the_card_repository_is_the_operators_setting_and_a_card_for_another_is_stale(tree, fake_runtime):
    context = SimpleNamespace(plugin_settings=lambda name: {"card_repo": "someone/their-fork"} if name == "laya" else {})
    plugin = LayaPlugin(context=context, runtime=fake_runtime, params_dir=lambda: tree.base)
    preview(tree)  # written for the default repository
    status, body = plugin.handle_request("GET", "/card", {}, None, HEADERS)
    assert status == 200 and body["card"] is None
    out = preview(tree, "someone/their-fork")
    status, body = plugin.handle_request("GET", "/card", {}, None, HEADERS)
    assert body["card"] == out


def test_a_card_repository_is_owner_slash_name_or_the_default():
    assert card_files.card_repo({}) == card_files.DEFAULT_REPO == "voidcraft-world/tldrack"
    assert card_files.card_repo({"card_repo": "a-b/c.d_e"}) == "a-b/c.d_e"
    for bad in ("nope", "a/b/c", "https://github.com/a/b", 7, "a b/c"):
        assert card_files.card_repo({"card_repo": bad}) == card_files.DEFAULT_REPO, bad


def test_laya_lab_reads_the_same_key_from_settings_json(tmp_path):
    env = {"TLDRACK_CONFIG_DIR": str(tmp_path), "HOME": str(tmp_path)}
    assert card_files.configured_repo(env) == card_files.DEFAULT_REPO
    (tmp_path / "settings.json").write_text(json.dumps({"plugins": {"laya": {"card_repo": "someone/their-fork"}}}))
    assert card_files.configured_repo(env) == "someone/their-fork"
    (tmp_path / "settings.json").write_text("{not json")
    assert card_files.configured_repo(env) == card_files.DEFAULT_REPO
