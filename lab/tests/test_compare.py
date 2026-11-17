"""`laya-lab compare`: voter specs, the endpoint voter over real loopback HTTP, and a report that
carries ids and numbers but never a text. Every text is synthetic."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from laya_lab import compare, infer
from laya_bridge import lab_files, paths

from conftest import SUITE, FakeRouter, synthetic


@pytest.fixture
def endpoint():
    """A `/v1/systemone` server on 127.0.0.1 that says 0.9 for a state containing "open", else 0.1."""
    seen: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body)
            if body.get("state") == "explode":
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            p = 0.9 if "open" in json.dumps(body["state"]).lower() else 0.1
            data = json.dumps({"answers": {name: {"type": "noul", "noul": p} for name in body["questions"]}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def test_loopback_only():
    for url in ("http://127.0.0.1:8010", "http://localhost:8010/v1/systemone", "http://[::1]:8010"):
        assert infer.is_loopback_url(url), url
    for url in ("http://10.0.0.2:8010", "https://example.com", "file:///tmp/x", "127.0.0.1:8010", "http://127.0.0.1.example.com"):
        assert not infer.is_loopback_url(url), url
    with pytest.raises(ValueError, match="loopback"):
        infer.EndpointRouter("http://192.168.1.20:8010")


def test_specs_refuse_what_they_cannot_run():
    assert compare.parse_spec("laya:served") == ("laya", "served")
    assert compare.parse_spec("mica=http://127.0.0.1:8010") == ("mica", "http://127.0.0.1:8010")
    for spec in (
        "laya:",
        "mica",
        "Mica=http://127.0.0.1:1",
        "mean=http://127.0.0.1:1",
        "layax=http://127.0.0.1:1",
        "mica=http://203.0.113.9:8010",
    ):
        with pytest.raises(compare.Refused):
            compare.parse_spec(spec)


def test_the_endpoint_answers_like_a_router(endpoint):
    url, seen = endpoint
    router = infer.EndpointRouter(url)
    question = {"type": "noul", "instructions": "Is work open?"}
    assert infer.predict_noul(router, "mica", ["still open", "all done"], question) == [0.9, 0.1]
    assert seen[0]["questions"] == {"q": question} and "model" not in seen[0]  # the voter's NAME is not forwarded
    with pytest.raises(ValueError, match="HTTP 500"):
        router.predict("explode", {"q": question})
    with pytest.raises(ValueError, match="unreachable"):
        infer.EndpointRouter("http://127.0.0.1:9", timeout=2).predict("x", {"q": question})


def test_compare_scores_laya_beside_an_endpoint_and_writes_no_text(lab, public_golden, endpoint, capsys):
    url, _seen = endpoint
    open_texts, done_texts = synthetic("still-open", 3), synthetic("all-done", 3)
    keys = lab.unlabelled(open_texts + done_texts)
    lab.labels({key: i < 3 for i, key in enumerate(keys)})
    laya = FakeRouter(lambda state: 0.8 if "open" in state.lower() else 0.3)

    code = compare.run(SUITE, lab.base, voters=["laya:served", "laya:english", f"mica={url}"], router=laya)

    assert code == 0
    reports = list((lab.base / paths.EVALS_DIRNAME).glob("compare-*.json"))
    assert len(reports) == 1
    raw = reports[0].read_text()
    report = json.loads(raw)
    assert [v["name"] for v in report["voters"]] == ["laya:english", "mica"]  # served == english until a fine-tune exists
    golden, held = report["sets"]["golden"], report["sets"]["heldout"]
    assert golden["rows"] == 2 and held["rows"] == 6 and held["labels"] == "merged"
    assert set(golden["voters"]) == {"laya:english", "mica", "mean"}
    assert held["voters"]["mica"]["auc"] == 1.0 and held["voters"]["mica"]["at_live"]["accuracy"] == 1.0
    assert held["voters"]["laya:english"]["at_live"]["accuracy"] == 1.0  # 0.8 ≥ 0.7 > 0.3
    assert held["voters"]["mean"]["latency_ms"] is None
    assert held["agreement"] == [{"a": "laya:english", "b": "mica", "same_verdict": 1.0, "pearson": 1.0}]
    assert set(held["p"]) == set(keys)
    for text in (*open_texts, *done_texts, "Phase C is not started.", "All three phases shipped."):
        assert text not in raw
    # the rack's readers never take a compare report for a golden or held-out one
    assert lab_files.newest_report(lab.base, SUITE, "english", "golden") is None
    assert lab_files.newest_report(lab.base, SUITE, "english", "heldout") is None
    out = capsys.readouterr().out
    assert "compare agent-open-work" in out and "mica" in out and "report →" in out


def test_compare_exits_1_on_a_voter_that_cannot_answer(lab, public_golden, capsys):
    code = compare.run(SUITE, lab.base, voters=["mica=http://127.0.0.1:9"], heldout=False)
    assert code == 1 and "unreachable" in capsys.readouterr().out
    assert not list(lab.base.glob(f"{paths.EVALS_DIRNAME}/compare-*.json"))


def test_compare_refuses_an_off_machine_voter_before_any_call(lab, public_golden, capsys):
    assert compare.run(SUITE, lab.base, voters=["other=https://example.com/api"]) == compare.REFUSED
    assert "loopback" in capsys.readouterr().out


def test_an_eval_run_reads_its_compare_verdict_and_ops_shows_it(lab, public_golden, monkeypatch):
    """The compare verdict probe, and the rack's COMPARE line: names and numbers, never a URL or a reason."""
    from laya_bridge import ops

    open_texts, done_texts = synthetic("still-open", 3), synthetic("all-done", 3)
    keys = lab.unlabelled(open_texts + done_texts)
    lab.labels({key: i < 3 for i, key in enumerate(keys)})
    laya = FakeRouter(lambda state: 0.8 if "open" in state.lower() else 0.3)
    monkeypatch.setenv(lab_files.EVAL_RUN_ENV, "0123456789ab")
    assert compare.run(SUITE, lab.base, router=laya, device="cpu") == 0

    verdict, why = lab_files.run_verdict(lab.base, "compare", SUITE)
    assert why == "" and verdict["verdict"] == "PASS" and verdict["accelerator"] == "cpu"
    # `laya:served` is english until a fine-tune exists; a built-in's digest is the laya release serving it
    assert verdict["model_digest"] == infer.checkpoint_digest("english", None)
    head = verdict["headline"]
    assert head["cases"] == 6 and head["auc"] == 1.0 and head["accuracy"] == 1.0  # the held-out set, not golden
    assert set(head) <= {"cases", "auc", "brier", "ece", "accuracy", "kappa", "latency_ms_p50"}

    [shown] = ops.ops_state(lab.base, None, [], lab_ok=True)["compare"]
    assert shown["surface"] == "agent_open_work" and shown["heldout_skipped"] is False
    assert [s["set"] for s in shown["sets"]] == ["golden", "heldout"]
    held = shown["sets"][1]
    assert held["rows"] == 6 and {v["name"] for v in held["voters"]} == {"laya:english"}
    assert held["voters"][0]["served"] is True and held["voters"][0]["auc"] == 1.0
    assert "url" not in json.dumps(shown) and "127.0.0.1" not in json.dumps(shown)
