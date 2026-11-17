"""The laya plugin: the wire gate, the checkpoint registry, and the child-process lifecycle.

The lifecycle tests spawn a FAKE `laya-serve` — a tiny stdlib server written into a temp runtime
venv — so spawn → health → decide → idle stop runs for real without torch or weights.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import textwrap
import time
from pathlib import Path

import pytest

from laya_bridge import lab_files
from laya_bridge import install, paths
from laya_bridge.paths import read_extra_models, registry_view
from laya_bridge.plugin import LayaPlugin
from laya_bridge.runtime import LayaRuntime
from laya_bridge.wire import WireError, clean_request

KNOWN = frozenset({"english", "multilingual", "voidcraft-paste"})


def question(qtype="noul", **extra):
    return {"type": qtype, "instructions": "Is it?", **extra}


# -- wire ---------------------------------------------------------------------


def test_clean_request_keeps_only_documented_fields():
    body = {
        "state": "buy milk",
        "questions": {"kind": question("choice", criteria={"todo": "a task", "note": "prose"}, max_len=8192)},
        "max_len": 8192,
        "language": "fr",
    }
    cleaned = clean_request(body, KNOWN)
    assert cleaned == {
        "state": "buy milk",
        "questions": {"kind": {"type": "choice", "instructions": "Is it?", "criteria": {"todo": "a task", "note": "prose"}}},
    }


@pytest.mark.parametrize(
    "body, fragment",
    [
        ([], "JSON object"),
        ({"state": "", "questions": {"a": question()}}, "state"),
        ({"state": "x", "questions": {}}, "questions"),
        ({"state": "x", "questions": {"Bad-Name": question()}}, "snake_case"),
        ({"state": "x", "questions": {"a": question("vote")}}, "type"),
        ({"state": "x", "questions": {"a": question("choice")}}, "needs criteria"),
        ({"state": "x", "questions": {"a": question("score", criteria=["only one"])}}, "two levels"),
        ({"state": "x", "questions": {"a": question("noul", criteria={"y": "yes"})}}, "no criteria"),
        ({"state": "x" * 64_001, "questions": {"a": question()}}, "over"),
        ({"state": "x", "questions": {"a": question()}, "model": "convaiinnovations/other"}, "not served"),
        ({"state": "x", "questions": {"a": question()}, "model": "voidcraft-mail"}, "not served"),
        ({"state": "x", "questions": {"a": question("choice", criteria={"o" * 81: "too long a name"})}}, "at most 80"),
        # 41 code points, 82 UTF-16 units: the ledger's `choice` is `.max(80)` as JavaScript counts it
        ({"state": "x", "questions": {"a": question("choice", criteria={"😀" * 41: "an emoji name"})}}, "at most 80"),
    ],
)
def test_clean_request_refuses(body, fragment):
    with pytest.raises(WireError, match=fragment):
        clean_request(body, KNOWN)


def test_clean_request_passes_a_registered_model():
    assert (
        clean_request({"state": "x", "questions": {"a": question()}, "model": "voidcraft-paste"}, KNOWN)["model"]
        == "voidcraft-paste"
    )


# -- paths --------------------------------------------------------------------


def write_models(params: Path, models: dict) -> None:
    (params / "laya").mkdir(parents=True, exist_ok=True)
    (params / "laya" / "models.json").write_text(json.dumps({"version": 1, "models": models}))


def test_extra_models_resolve_inside_params_only(tmp_path):
    params = tmp_path / "params"
    good = params / "laya" / "checkpoints" / "paste-1"
    good.mkdir(parents=True)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (params / "laya" / "checkpoints" / "sneaky").symlink_to(outside)
    write_models(
        params,
        {
            "voidcraft-paste": {"path": "checkpoints/paste-1"},
            "voidcraft-sneaky": {"path": "checkpoints/sneaky"},
            "voidcraft-dotdot": {"path": "../elsewhere"},
            "english": {"path": "checkpoints/paste-1"},
            "Bad Name": {"path": "checkpoints/paste-1"},
            "voidcraft-missing": {"path": "checkpoints/nope"},
        },
    )
    env = {paths.DATA_ENV: str(params), paths.CONFIG_ENV: str(tmp_path / "config")}
    assert read_extra_models(env) == {"voidcraft-paste": str(good.resolve())}


@pytest.mark.parametrize("data", ["relative/path", "/definitely/not/here"])
def test_extra_models_empty_without_a_usable_store(data, tmp_path):
    assert read_extra_models({paths.DATA_ENV: data, paths.CONFIG_ENV: str(tmp_path)}) == {}


# -- the store: where `<data>/laya` is ------------------------------------------------


def test_the_store_follows_the_bridges_resolver_order(tmp_path):
    """paths.json `stores.laya`, then the data variable, then paths.json `data`, then the home default."""
    config, home = tmp_path / "config", tmp_path / "home"
    mapped, data, from_file = tmp_path / "mapped", tmp_path / "data", tmp_path / "from-file"
    for folder in (config, home, mapped, data, from_file):
        folder.mkdir()
    env = {paths.CONFIG_ENV: str(config), "HOME": str(home)}
    assert paths.laya_dir(env) == home.resolve() / ".local" / "share" / "tldrack" / "laya"
    (config / paths.PATHS_FILENAME).write_text(json.dumps({"data": str(from_file)}))
    assert paths.laya_dir(env) == from_file.resolve() / "laya"
    assert paths.laya_dir({**env, paths.DATA_ENV: str(data)}) == data.resolve() / "laya", "the variable outranks the file's role"
    (config / paths.PATHS_FILENAME).write_text(json.dumps({"data": str(from_file), "stores": {"laya": str(mapped)}}))
    assert paths.laya_dir({**env, paths.DATA_ENV: str(data)}) == mapped.resolve(), "a mapped store is the folder itself"
    assert not (data / "laya").exists() and not (home / ".local").exists(), "a read creates nothing"


@pytest.mark.parametrize(
    "body",
    [
        '{"stores": {"laya": "/definitely/not/here"}}',
        '{"stores": {"laya": "relative"}}',
        '{"data": "/definitely/not/here"}',
        "[]",
        "{not json",
    ],
)
def test_an_unusable_mapping_is_an_error_never_a_fresh_store_beside_the_real_one(body, tmp_path):
    (tmp_path / paths.PATHS_FILENAME).write_text(body)
    with pytest.raises(paths.PathsError):
        paths.laya_dir({paths.CONFIG_ENV: str(tmp_path), "HOME": str(tmp_path)})


def test_the_store_is_created_private_on_first_use(tmp_path):
    env = {paths.DATA_ENV: str(tmp_path / "data"), paths.CONFIG_ENV: str(tmp_path)}
    base = paths.ensure_laya_dir(env)
    assert base == (tmp_path / "data").resolve() / "laya" and stat.S_IMODE(base.stat().st_mode) == 0o700
    assert paths.ensure_laya_dir(env) == base


def test_extra_models_survive_a_corrupt_file(tmp_path):
    (tmp_path / "laya").mkdir()
    (tmp_path / "laya" / "models.json").write_text("{not json")
    assert read_extra_models({paths.DATA_ENV: str(tmp_path), paths.CONFIG_ENV: str(tmp_path / "config")}) == {}


# -- lifecycle against a fake laya-serve ---------------------------------------

FAKE_SERVE = textwrap.dedent(
    """\
    #!{python}
    import json, os
    from http.server import BaseHTTPRequestHandler, HTTPServer

    KEY = os.environ["LAYA_API_KEY"]
    assert os.environ["LAYA_HOST"] == "127.0.0.1"
    assert os.environ["LAYA_REVISION"] == "reviewed"

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass
        def _authed(self):
            if self.headers.get("Authorization") != "Bearer " + KEY:
                self.send_response(401); self.end_headers(); return False
            return True
        def _json(self, code, payload):
            data = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def do_GET(self):
            if self._authed():
                self._json(200, {{"status": "ok", "device": "fake", "revisions": {{"english": "55cf4c4ebb4e"}}}})
        def do_POST(self):
            if not self._authed():
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if "max_len" in body:
                self._json(422, {{"detail": "max_len leaked through the plugin"}}); return
            answers = {{name: {{"type": "noul", "noul": 0.8}} for name in body["questions"]}}
            self._json(200, {{"model": "laya-rl-agent", "answers": answers, "usage": {{"input_tokens": 12, "output_tokens": 0}}}})

    HTTPServer(("127.0.0.1", int(os.environ["LAYA_PORT"])), H).serve_forever()
    """
)


@pytest.fixture
def runtime_dir(tmp_path):
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    serve = bin_dir / "laya-serve"
    serve.write_text(FAKE_SERVE.format(python=sys.executable))
    serve.chmod(serve.stat().st_mode | stat.S_IXUSR)
    return tmp_path


class FakeClock:
    def __init__(self):
        self.now = 1_000.0

    def __call__(self):
        return self.now


def wait_for(runtime: LayaRuntime, state: str, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if runtime.state() == state:
            return
        time.sleep(0.05)
    raise AssertionError(f"runtime stayed {runtime.state()!r}, wanted {state!r}")


def test_not_installed_runtime_never_spawns(tmp_path):
    plugin = LayaPlugin(context=None, runtime=LayaRuntime(runtime_dir=tmp_path))
    status, payload = plugin.handle_post("/decide", {}, {"state": "x", "questions": {"a": question()}})
    assert status == 503 and payload["error"] == "not_installed"
    assert plugin.handle_get("/status", {})[1]["state"] == "not_installed"


def test_bad_body_is_refused_before_anything_starts(runtime_dir):
    runtime = LayaRuntime(runtime_dir=runtime_dir)
    status, payload = LayaPlugin(context=None, runtime=runtime).handle_post("/decide", {}, {"state": "x"})
    assert status == 400 and "questions" in payload["detail"]
    assert runtime.state() == "stopped"


def test_cold_decide_warms_then_answers_then_idles_out(runtime_dir, tree):
    checkpoint = tree.register()
    runtime = LayaRuntime(
        runtime_dir=runtime_dir,
        idle_stop_seconds=60,
        extra_models=lambda: {"voidcraft-agent-open-work": str(checkpoint)},
        registry=lambda: registry_view(tree.base),
    )
    plugin = LayaPlugin(context=None, runtime=runtime, params_dir=lambda: tree.base)
    body = {"state": "buy milk", "questions": {"todo": question()}, "max_len": 8192}
    try:
        status, payload = plugin.handle_post("/decide", {}, body)
        assert status == 503 and payload["error"] == "warming"

        wait_for(runtime, "ready")
        status, payload = plugin.handle_post("/decide", {}, body)
        assert status == 200, payload
        assert payload["answers"]["todo"]["noul"] == 0.8
        assert payload["decision_id"] and payload["model_id"] == "laya-local/english@55cf4c4" and payload["checkpoint"] is None
        status, pinned = plugin.handle_post("/decide", {}, {**body, "model": "voidcraft-agent-open-work"})
        assert status == 200 and pinned["checkpoint"] == checkpoint.name, "a basename, never a path"
        assert pinned["model_id"] == f"laya-local/voidcraft-agent-open-work@{checkpoint.name}"
        [first, second] = [json.loads(line) for line in (tree.base / "ledger" / "untagged.jsonl").read_text().splitlines()]
        assert (first["model"], first["checkpoint_path"]) == ("english", None)
        assert (second["model"], second["checkpoint_path"]) == ("voidcraft-agent-open-work", f"checkpoints/{checkpoint.name}")
        assert first["state"] is None, "untagged is hash-only"
        plugin.handle_post("/decide", {}, body)
        third = json.loads((tree.base / "ledger" / "untagged.jsonl").read_text().splitlines()[-1])
        assert (first["cold"], second["cold"], third["cold"]) == (True, True, False) and runtime.spawn() == 1, (
            "each model's first answer since laya-serve started is cold"
        )
        assert third["load"] >= 0
        status_body = plugin.handle_get("/status", {})[1]
        assert status_body["device"] == "fake"
        assert status_body["model_ids"] == {
            "english": "laya-local/english@55cf4c4",
            "voidcraft-agent-open-work": f"laya-local/voidcraft-agent-open-work@{checkpoint.name}",
        }

        # Not idle yet → kept; past the idle window → stopped, and the next decide warms again.
        assert runtime.reap_if_idle() is False
        runtime._last_used -= 61
        assert runtime.reap_if_idle() is True
        assert runtime.state() == "stopped"
    finally:
        runtime.stop()


def test_a_crashed_child_reports_failed_with_its_log_and_backs_off(tmp_path):
    bin_dir = tmp_path / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    serve = bin_dir / "laya-serve"
    serve.write_text(f"#!{sys.executable}\nimport sys\nprint('torch: out of memory')\nsys.exit(3)\n")
    serve.chmod(serve.stat().st_mode | stat.S_IXUSR)
    runtime = LayaRuntime(runtime_dir=tmp_path)

    assert runtime.ensure_started() == "starting"
    wait_for(runtime, "failed")
    status = runtime.status()
    assert "exited (3)" in status["error"] and "out of memory" in status["error"]
    # Inside the backoff window a decide does not respawn the crash loop.
    assert runtime.ensure_started() == "failed"


# -- the ledger seam and the host contract ---------------------------------------


def test_a_list_body_is_still_a_400(runtime_dir):
    status, payload = LayaPlugin(context=None, runtime=LayaRuntime(runtime_dir=runtime_dir)).handle_post("/decide", {}, [])
    assert status == 400 and payload["detail"] == "the body must be a JSON object"


def test_a_plugin_without_a_store_writes_nothing_and_answers_no_decision_id(runtime_dir):
    empty = Path(os.environ[paths.DATA_ENV])
    runtime = LayaRuntime(runtime_dir=runtime_dir, idle_stop_seconds=60)
    plugin = LayaPlugin(context=None, runtime=runtime, params_dir=lambda: None)
    try:
        plugin.handle_post("/decide", {}, {"state": "x", "questions": {"a": question()}})
        wait_for(runtime, "ready")
        status, payload = plugin.handle_post(
            "/decide", {}, {"state": "x", "questions": {"a": question()}, "ledger": {"surface": "agent_open_work"}}
        )
        assert status == 200 and payload["decision_id"] is None
        assert list(empty.iterdir()) == [], "a ledger that is off never creates laya/"
    finally:
        runtime.stop()


def test_a_first_run_makes_its_store_serves_the_base_model_and_records_the_decision(runtime_dir):
    """THE first-run promise: nothing configured, no fine-tune, and `/status` and `/decide` both answer."""
    data = Path(os.environ[paths.DATA_ENV])
    runtime_holder: list[LayaRuntime] = []
    plugin = LayaPlugin(context=None)
    plugin._runtime.close()
    runtime = LayaRuntime(runtime_dir=runtime_dir, idle_stop_seconds=60, first_fine_tune=plugin._first_fine_tune)
    plugin._runtime = runtime
    runtime_holder.append(runtime)
    try:
        assert (data / "laya").is_dir() and stat.S_IMODE((data / "laya").stat().st_mode) == 0o700
        plugin.handle_post("/decide", {}, {"state": "x", "questions": {"a": question()}})
        wait_for(runtime, "ready")
        status, body = plugin.handle_get("/status", {})
        assert status == 200 and body["state"] == "ready"
        assert body["serving"] == {
            "ready": True,
            "base": "convaiinnovations/laya",
            "base_only": True,
            "fine_tunes": [],
            "first_fine_tune": {
                "agent_open_work": {"labels": 0, "open": 0, "done": 0, "need": 40, "need_per_class": 10, "ready": False}
            },
        }
        status, payload = plugin.handle_post(
            "/decide", {}, {"state": "x", "questions": {"a": question()}, "ledger": {"surface": "agent_open_work"}}
        )
        assert status == 200 and payload["decision_id"] and payload["checkpoint"] is None
        assert (data / "laya" / "ledger" / "agent_open_work.jsonl").is_file()
    finally:
        runtime.stop()


def test_status_carries_a_swept_done_cut(runtime_dir, tree):
    tree.register(
        thresholds={
            "agent_open_work": lab_files.threshold_entry(
                0.7,
                "voidcraft-agent-open-work",
                "voidcraft-agent-open-work-20260102-0304",
                "2026-10-27T01:00:00Z",
                "operator",
                48,
                0.5,
                None,
                0.3,
            )
        }
    )
    runtime = LayaRuntime(runtime_dir=runtime_dir, idle_stop_seconds=60, registry=lambda: registry_view(tree.base))
    assert runtime.status()["thresholds"] == {
        "agent_open_work": {"at": 0.7, "model": "voidcraft-agent-open-work", "done_at": 0.3}
    }


def test_status_keeps_a_swept_cut_only_for_the_loaded_checkpoint(runtime_dir, tree):
    registered = tree.register(
        thresholds={
            "agent_open_work": lab_files.threshold_entry(
                0.62,
                "voidcraft-agent-open-work",
                "voidcraft-agent-open-work-20260102-0304",
                "2026-10-27T01:00:00Z",
                "operator",
                48,
                0.5,
                None,
            )
        }
    )
    other = tree.path("checkpoints/voidcraft-agent-open-work-20261101-0000")
    other.mkdir()
    loaded = {"voidcraft-agent-open-work": str(other.resolve())}
    runtime = LayaRuntime(
        runtime_dir=runtime_dir,
        idle_stop_seconds=60,
        extra_models=lambda: dict(loaded),
        registry=lambda: registry_view(tree.base),
    )
    assert runtime.status()["thresholds"] == {"agent_open_work": {"at": 0.62, "model": "voidcraft-agent-open-work"}}, (
        "not running: the registered checkpoint is the one the cut was swept on"
    )
    try:
        runtime.ensure_started()
        wait_for(runtime, "ready")
        assert runtime.loaded() == loaded and runtime.status()["thresholds"] == {}
        assert runtime.served_identity("voidcraft-agent-open-work") == (
            "laya-local/voidcraft-agent-open-work@voidcraft-agent-open-work-20261101-0000",
            str(other.resolve()),
        )
        assert runtime.served_identity("english") == ("laya-local/english@55cf4c4", None)
    finally:
        runtime.stop()
    assert runtime.loaded() is None and registered.is_dir()


def test_the_plugin_raises_its_body_cap_and_lists_every_route():
    plugin = LayaPlugin(context=None, runtime=LayaRuntime(runtime_dir=Path("/nonexistent")))
    assert plugin.max_body_bytes >= 256 * 1024
    assert "/plugin/laya/review/answer" in plugin.routes() and "/plugin/laya/react" in plugin.routes()


def test_install_is_locked_once_a_lockfile_exists(tmp_path):
    assert install.sync_argv("uv", tmp_path) == ["uv", "sync", "--project", str(tmp_path)]
    (tmp_path / "uv.lock").write_text("")
    assert install.sync_argv("uv", tmp_path)[-1] == "--locked"


def test_install_syncs_then_fetches_the_base_model_and_a_failed_fetch_is_not_a_failed_install(tmp_path, monkeypatch):
    ran: list[list[str]] = []
    codes = iter([0, 1])
    monkeypatch.setattr(install, "_call", lambda argv, out, timeout: (ran.append(list(argv)), next(codes))[1])
    said: list[str] = []
    assert install.run("uv", tmp_path, None, said.append) == (0, "fetch")
    assert ran[0] == ["uv", "sync", "--project", str(tmp_path)]
    assert ran[1][0] == str(tmp_path / ".venv" / "bin" / "python") and ran[1][3:5] == [install.BASE_REPO, "reviewed"]
    assert any("first start will fetch it" in line for line in said)
    ran.clear()
    monkeypatch.setattr(install, "_call", lambda argv, out, timeout: (ran.append(list(argv)), 2)[1])
    assert install.run("uv", tmp_path, None, said.append) == (2, "sync") and len(ran) == 1, "no fetch after a failed sync"


def test_the_status_lines_say_the_base_model_is_serving_and_how_far_a_first_fine_tune_is():
    road = {"labels": 12, "open": 9, "done": 3, "need": 40, "need_per_class": 10, "ready": False}
    lines = install.status_lines(
        {
            "state": "ready",
            "serving": {
                "ready": True,
                "base": install.BASE_REPO,
                "base_only": True,
                "fine_tunes": [],
                "first_fine_tune": {"agent_open_work": road},
            },
        }
    )
    assert lines == [
        "state: ready",
        "serving: the base model convaiinnovations/laya",
        "first fine-tune of agent_open_work: 12 of 40 labels (9 open, 3 done; 10 of each needed)",
    ]
    assert install.status_lines(None)[0].startswith("no bridge answers")
    served = install.status_lines(
        {
            "state": "ready",
            "serving": {
                "ready": True,
                "base": install.BASE_REPO,
                "base_only": False,
                "fine_tunes": ["voidcraft-agent-open-work"],
                "first_fine_tune": {},
            },
        }
    )
    assert served[1] == "serving: voidcraft-agent-open-work over the base model convaiinnovations/laya"


def test_mount_tightens_operator_file_modes(tree):
    path = tree.write_jsonl("data/agent-open-work-labels.jsonl", [])
    path.chmod(0o644)
    logged = []

    class Context:
        log = staticmethod(logged.append)

    LayaPlugin(Context(), runtime=LayaRuntime(runtime_dir=Path("/nonexistent")), params_dir=lambda: tree.base)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "[laya] tightened data/agent-open-work-labels.jsonl" in logged


# -- what the host's context says ------------------------------------------------------


class HostContext:
    """A host context as plugins/README.md § What the context carries describes one."""

    status_port = 7682
    allowed_origins = ("https://rack.example",)

    def __init__(self, settings: dict | None = None) -> None:
        self.logged: list[str] = []
        self._settings = settings or {}

    def log(self, line: str) -> None:
        self.logged.append(line)

    def plugin_settings(self, name: str) -> dict:
        assert name == "laya"
        return self._settings


def test_the_policy_routes_admit_the_operators_origin_the_bridges_page_and_nothing_else_hosted(tree):
    plugin = LayaPlugin(HostContext(), runtime=LayaRuntime(runtime_dir=Path("/nonexistent")), params_dir=lambda: tree.base)
    for origin, wanted in (
        ("https://rack.example", 200),
        ("http://127.0.0.1:7682", 200),
        ("http://localhost:5173", 200),
        ("https://elsewhere.example", 403),
        ("http://localhost:9999", 403),
    ):
        status, _body = plugin.handle_request("GET", "/ops", {}, None, {"host": "127.0.0.1:7682", "origin": origin})
        assert status == wanted, origin
    bare = LayaPlugin(context=None, runtime=LayaRuntime(runtime_dir=Path("/nonexistent")), params_dir=lambda: tree.base)
    assert bare.handle_request("GET", "/ops", {}, None, {"host": "127.0.0.1:7682", "origin": "https://rack.example"})[0] == 403


def test_the_eval_runner_is_off_until_the_operator_names_one(tree, tmp_path):
    runner = tmp_path / "runner"
    runner.mkdir()
    runtime = LayaRuntime(runtime_dir=Path("/nonexistent"))
    assert LayaPlugin(HostContext(), runtime=runtime, params_dir=lambda: tree.base)._runner is None
    assert LayaPlugin(HostContext({"evals_dir": str(runner)}), runtime=runtime, params_dir=lambda: tree.base)._runner == runner
    context = HostContext({"evals_dir": "relative", "unknown_key": 1})
    assert LayaPlugin(context, runtime=runtime, params_dir=lambda: tree.base)._runner is None
    assert any("evals_dir" in line for line in context.logged)


def test_a_host_whose_settings_reader_breaks_still_mounts_the_plugin(tree):
    class Broken(HostContext):
        def plugin_settings(self, name: str) -> dict:
            raise RuntimeError("settings unreadable")

    plugin = LayaPlugin(Broken(), runtime=LayaRuntime(runtime_dir=Path("/nonexistent")), params_dir=lambda: tree.base)
    assert plugin._runner is None and plugin.handle_get("/status", {})[0] == 200
