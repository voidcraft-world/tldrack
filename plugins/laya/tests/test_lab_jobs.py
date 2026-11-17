"""Lab jobs: every verb a fixed laya-lab command in its own tmux session, the FIFO behind a running job, the pane
marks, the refusals, the guard that keeps TRAIN, LABEL and --force out of every lab argv, and the optional eval
runner. Every registry file here is written by the test: invented, never a copy of a real runner's."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from laya_bridge import envelope, lab_jobs, paths, review_log, rows
from laya_bridge.envelope import Refusal
from laya_bridge.lab_jobs import job_rows, parse_lab_panes
from laya_bridge.plugin import LabDrain, LayaPlugin
from laya_bridge.review_surfaces import REVIEW_SURFACES

SURFACE = "agent_open_work"


# -- what a verb runs ----------------------------------------------------------------


def test_every_review_surface_has_a_full_row_of_lab_verbs():
    assert set(lab_jobs.LAB_ARGS) == set(lab_jobs.LAB_EVALS) == set(REVIEW_SURFACES)
    assert all(set(row) == set(lab_jobs.LAB_VERBS) for row in (*lab_jobs.LAB_ARGS.values(), *lab_jobs.LAB_EVALS.values()))
    assert {"train", "label", "label-open-work"}.isdisjoint(lab_jobs.LAB_VERBS)


@pytest.mark.parametrize("verb", lab_jobs.LAB_VERBS)
def test_no_lab_verb_can_put_an_unshadowed_checkpoint_live(verb):
    """`train` registers what passes its gate as SERVED; only the gated retrain may train (lab_jobs.py owns why)."""
    args = lab_jobs.lab_args(verb, SURFACE)
    assert args == lab_jobs.LAB_ARGS[SURFACE][verb] and not lab_jobs.is_forbidden(args)
    assert lab_jobs.lab_args(verb, "paste") is None and lab_jobs.lab_args("train", SURFACE) is None


@pytest.mark.parametrize(
    "args",
    [
        ("train", "agent_open_work"),
        ("train", "agent_open_work", "--force"),
        ("label-open-work",),
        ("golden", "agent-open-work", "--force"),
        (),
    ],
)
def test_the_guard_refuses_train_label_and_force(args):
    assert lab_jobs.is_forbidden(args)


@pytest.fixture
def lab_repo(tmp_path):
    """A checkout with laya-lab installed."""
    repo = tmp_path / "repo"
    binary = repo / lab_jobs.LAB_BIN
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n")
    return repo


def registry_entry(verb: str) -> dict:
    """An invented registry file for one lab verb, in the shape a runner keeps."""
    return {
        "version": 1,
        "id": lab_jobs.eval_id(verb, SURFACE),
        "data_class": "private_operator",
        "args": None,
        "engine": {"kind": "command", "cwd": "lab", "argv": ["lab/.venv/bin/laya-lab", *lab_jobs.LAB_ARGS[SURFACE][verb]]},
    }


@pytest.fixture
def runner(tmp_path):
    """An eval runner's directory holding a registry file per lab verb."""
    folder = tmp_path / "runner"
    (folder / lab_jobs.REGISTRY_DIRNAME).mkdir(parents=True)
    for verb in lab_jobs.LAB_VERBS:
        rewrite(folder, verb, **registry_entry(verb))
    return folder


def rewrite(runner: Path, verb: str, **changes) -> None:
    path = runner / lab_jobs.REGISTRY_DIRNAME / f"laya-{verb}.json"
    entry = json.loads(path.read_text()) if path.exists() else {}
    entry.update(changes)
    path.write_text(json.dumps(entry))


def test_with_a_runner_the_argv_is_its_registry_files_and_an_edited_one_never_runs(runner):
    golden = registry_entry("golden")
    assert lab_jobs.lab_args("golden", SURFACE, runner) == lab_jobs.LAB_ARGS[SURFACE]["golden"]
    for changes in (
        {"engine": {**golden["engine"], "argv": ["lab/.venv/bin/laya-lab", "train", "agent_open_work"]}},
        {"engine": {**golden["engine"], "argv": ["lab/.venv/bin/laya-lab", "golden", "agent-open-work", "--force"]}},
        {"engine": {**golden["engine"], "argv": ["node", "x.ts"]}},
        {"args": {"flags": ["--model"]}},
        {"data_class": "public"},
    ):
        rewrite(runner, "golden", **changes)
        assert lab_jobs.lab_args("golden", SURFACE, runner) is None, changes
        rewrite(runner, "golden", **golden)
    assert lab_jobs.lab_args("golden", SURFACE, runner) == lab_jobs.LAB_ARGS[SURFACE]["golden"]
    (runner / "registry" / "laya-golden.json").write_text("{not json")
    assert lab_jobs.lab_args("golden", SURFACE, runner) is None
    (runner / "registry" / "laya-golden.json").unlink()
    assert lab_jobs.lab_args("golden", SURFACE, runner) is None


def test_the_runner_is_named_by_a_setting_and_a_bad_one_is_ignored_with_a_line(runner, tmp_path):
    said: list[str] = []
    assert lab_jobs.runner_dir(None, said.append) is None and said == []
    assert lab_jobs.runner_dir(str(runner), said.append) == runner and said == []
    for bad in ("relative/evals", str(tmp_path / "nope"), 7, ""):
        assert lab_jobs.runner_dir(bad, said.append) is None
    assert len(said) == 4 and all("evals_dir" in line for line in said)


# -- the pure parts ----------------------------------------------------------------


def test_the_job_argv_sets_the_exit_keeper_and_the_stamps_before_the_command_runs(lab_repo):
    env = {paths.DATA_ENV: "/data", paths.CONFIG_ENV: "/config", "HOME": "/Users/x"}
    assert lab_jobs.job_argv("sweep-reset", SURFACE, lab_repo, env) == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "laya-sweep-reset",
        "-c",
        str(lab_repo / "lab"),
        "-e",
        "TLDRACK_CONFIG_DIR=/config",
        "-e",
        "TLDRACK_DATA_DIR=/data",
        "sleep",
        "60",
        ";",
        "set-option",
        "-w",
        "-t",
        "=laya-sweep-reset:",
        "remain-on-exit",
        "failed",
        ";",
        "set-option",
        "-t",
        "=laya-sweep-reset:",
        "@laya_verb",
        "sweep-reset",
        ";",
        "set-option",
        "-t",
        "=laya-sweep-reset:",
        "@laya_surface",
        "agent_open_work",
        ";",
        "respawn-pane",
        "-k",
        "-t",
        "=laya-sweep-reset:",
        str(lab_repo / "lab/.venv/bin/laya-lab"),
        "sweep",
        "agent-open-work",
        "--reset",
    ]
    assert "-e" not in lab_jobs.job_argv("golden", SURFACE, lab_repo, {}), "an unset variable is not passed empty"


def test_start_argv_is_the_runners_start_in_the_lab_session_with_its_stamps():
    assert lab_jobs.start_argv("sweep-reset", SURFACE) == [
        sys.executable,
        "-m",
        "evals.cli",
        "start",
        "laya-sweep-reset",
        "--session",
        "laya-sweep-reset",
        "--stamp",
        "@laya_verb=sweep-reset",
        "--stamp",
        "@laya_surface=agent_open_work",
        "--json",
    ]


def test_the_tooltip_is_the_lab_command_or_with_a_runner_the_recorded_run(runner):
    assert lab_jobs.cli_text("score", SURFACE) == "cd lab && uv run laya-lab review-open-work --model served"
    assert lab_jobs.cli_text("score", SURFACE, runner) == "evals start laya-score"
    assert all(row["cli"] == f"evals start laya-{row['verb']}" for row in job_rows(None, None, runner))
    assert all(row["cli"].startswith("cd lab && uv run laya-lab ") for row in job_rows(None))


def pane(session, dead, status, verb="", surface="", eval_id=""):
    return "\x1f".join((session, dead, status, verb, surface, eval_id))


def test_a_lab_job_is_found_by_its_stamped_session_or_its_eval_id():
    output = "\n".join(
        [
            pane("laya-sweep", "0", "", "sweep", SURFACE, "laya-sweep"),
            pane("laya-score", "1", "3", "score", SURFACE),  # an older bridge's session: stamps only
            pane("eval-laya-golden", "0", "", eval_id="laya-golden"),  # a runner started it from elsewhere
            pane("laya-promote", "1", "x", "promote", SURFACE),
            pane("eval-laya-promote", "0", "", eval_id="laya-promote"),  # live outranks the dead one
            pane("nightly-digest", "0", "", "run"),
            pane("laya-sweep-reset", "0", "", "sweep"),  # stamped verb does not match the name
            pane("eval-elo-queue", "0", "", eval_id="elo-queue"),
            "laya-old\x1f0\x1f\x1fscore\x1f" + SURFACE,  # the old five-field format
            "garbage",
        ]
    )
    assert parse_lab_panes(output) == {
        "sweep": {"state": "working", "exit_code": None, "session": "laya-sweep", "surface": SURFACE},
        "score": {"state": "failed", "exit_code": 3, "session": "laya-score", "surface": SURFACE},
        "golden": {"state": "working", "exit_code": None, "session": "eval-laya-golden", "surface": SURFACE},
        "promote": {"state": "working", "exit_code": None, "session": "eval-laya-promote", "surface": SURFACE},
    }
    assert lab_jobs.running(parse_lab_panes(output)) == "laya-sweep"


def test_job_rows_are_one_per_verb_and_surface_working_then_queued_then_failed():
    idle = job_rows(None)
    assert [(row["verb"], row["state"], row["position"]) for row in idle] == [(v, "idle", None) for v in lab_jobs.LAB_VERBS]
    assert all(row["session"] == f"laya-{row['verb']}" and row["exit_code"] is None for row in idle)
    panes = {
        "score": {"state": "working", "exit_code": None, "session": "eval-laya-score", "surface": SURFACE},
        "golden": {"state": "failed", "exit_code": 1, "session": "laya-golden", "surface": SURFACE},
        "sweep": {"state": "failed", "exit_code": 3, "session": "laya-sweep", "surface": SURFACE},
    }
    queue = [{"verb": "golden", "surface": SURFACE, "at": "x"}, {"verb": "score", "surface": SURFACE, "at": "y"}]
    got = {row["verb"]: row for row in job_rows(panes, queue)}
    assert (got["score"]["state"], got["score"]["session"], got["score"]["position"]) == ("working", "eval-laya-score", None)
    assert (got["golden"]["state"], got["golden"]["position"], got["golden"]["exit_code"]) == ("queued", 1, None)
    assert (got["sweep"]["state"], got["sweep"]["exit_code"]) == ("failed", 3)
    assert got["compare"]["state"] == "idle"


# -- the queue file ----------------------------------------------------------------


def test_the_queue_file_is_owner_only_ordered_and_forgiving(tree):
    assert lab_jobs.read_queue(tree.base) == [] and lab_jobs.read_queue(None) == []
    entries = [
        {"verb": "golden", "surface": SURFACE, "at": "2026-11-09T03:00:00Z"},
        {"verb": "compare", "surface": SURFACE, "at": "2026-11-09T03:00:01Z"},
    ]
    lab_jobs.write_queue(tree.base, entries)
    path = lab_jobs.queue_path(tree.base)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert lab_jobs.read_queue(tree.base) == entries and lab_jobs.position(entries, "compare", SURFACE) == 2
    path.write_text(
        json.dumps({"version": 1, "queue": [*entries, entries[0], {"verb": "train", "surface": SURFACE, "at": "x"}, "junk"]})
    )
    assert lab_jobs.read_queue(tree.base) == entries, "duplicates, unknown verbs and junk are dropped"
    path.write_text(json.dumps({"version": 9, "queue": entries}))
    assert lab_jobs.read_queue(tree.base) == []
    path.unlink()
    elsewhere = tree.params / "elsewhere.json"
    elsewhere.write_text(json.dumps({"version": 1, "queue": entries}))
    path.symlink_to(elsewhere)
    assert lab_jobs.read_queue(tree.base) == [], "a link is never read"
    lab_jobs.write_queue(tree.base, entries[:1])
    assert not path.is_symlink() and json.loads(elsewhere.read_text())["queue"] == entries, "nor written through"


# -- the press and the queue -------------------------------------------------------


@pytest.fixture
def tmux(monkeypatch):
    """tmux and a runner's start, faked: `panes` is what list-panes prints, `replies` what each start answers (a
    `(code, stdout, stderr)` for tmux's own, a dict for a runner's JSON line, None for a runner that printed none)."""
    state = {"panes": "", "starts": [], "replies": [], "cwds": [], "killed": []}

    def run_start(argv, cwd, env=None):
        if argv[:2] == ["tmux", "kill-session"]:
            state["killed"].append(argv[3])
            return 1, "", "can't find session"
        state["starts"].append(argv[4])
        state["cwds"].append(cwd)
        reply = state["replies"].pop(0) if state["replies"] else None
        if argv[0] == "tmux":
            return reply if reply is not None else (0, "", "")
        if reply is None:
            return 1, "", "evals: registry unreadable"
        return 0 if reply.get("ok") else 1, json.dumps({"message": "", **reply}) + "\n", ""

    monkeypatch.setattr(lab_jobs, "tmux_available", lambda: True)
    monkeypatch.setattr(lab_jobs, "list_panes", lambda: state["panes"])
    monkeypatch.setattr(lab_jobs, "run_start", run_start)
    return state


def refusal(call) -> tuple[int, str]:
    with pytest.raises(Refusal) as caught:
        call()
    return caught.value.status, caught.value.code


def press(tree, repo, verb):
    reply, _done = lab_jobs.press(verb, SURFACE, tree.base, repo=repo)
    return reply


def test_a_press_with_nothing_running_starts_at_once(tree, lab_repo, tmux):
    reply = press(tree, lab_repo, "score")
    assert reply == {
        "v": 1,
        "ok": True,
        "message": "score started: watch laya-score in the rack",
        "session": "laya-score",
        "queued": None,
    }
    assert tmux["starts"] == ["laya-score"] and tmux["cwds"] == [lab_repo]
    assert tmux["killed"] == ["=laya-score"], "a dead pane of the same verb is replaced, never stacked on"
    assert lab_jobs.read_queue(tree.base) == []


def test_with_a_runner_a_press_starts_through_it_and_its_refusals_are_the_pressers(tree, lab_repo, tmux, runner):
    tmux["replies"] = [{"ok": True, "code": "started"}]
    reply, _done = lab_jobs.press("score", SURFACE, tree.base, repo=lab_repo, runner=runner)
    assert reply["session"] == "laya-score" and tmux["starts"] == ["laya-score"] and tmux["killed"] == []
    assert tmux["cwds"] == [runner.parent], "the runner starts from its own checkout"
    for reply, wanted in (
        ({"ok": False, "code": "spawn_failed", "message": f"no server at {Path.home()}/x"}, (500, "spawn_failed")),
        (None, (503, "evals_unavailable")),
        ({"ok": False, "code": "refused", "message": "laya-golden is private_operator"}, (503, "eval_refused")),
    ):
        tmux["replies"] = [reply]
        with pytest.raises(Refusal) as caught:
            lab_jobs.press("golden", SURFACE, tree.base, repo=lab_repo, runner=runner)
        assert (caught.value.status, caught.value.code) == wanted
        assert str(Path.home()) not in caught.value.message
    assert lab_jobs.read_queue(tree.base) == []
    (runner / "registry" / "laya-compare.json").unlink()
    assert refusal(lambda: lab_jobs.press("compare", SURFACE, tree.base, repo=lab_repo, runner=runner)) == (503, "eval_missing")


def test_presses_behind_a_running_job_queue_in_order_and_run_one_at_a_time(tree, lab_repo, tmux):
    tmux["panes"] = pane("laya-score", "0", "", "score", SURFACE, "laya-score")
    golden = press(tree, lab_repo, "golden")
    assert golden["queued"] == 1 and golden["session"] is None and "behind laya-score" in golden["message"]
    assert press(tree, lab_repo, "compare")["queued"] == 2
    assert press(tree, lab_repo, "golden")["message"] == "golden is already queued #1"
    assert refusal(lambda: lab_jobs.press("score", SURFACE, tree.base, repo=lab_repo)) == (409, "busy")
    assert tmux["starts"] == []
    assert [
        (r["verb"], r["state"], r["position"])
        for r in job_rows(parse_lab_panes(tmux["panes"]), lab_jobs.read_queue(tree.base))
        if r["state"] != "idle"
    ] == [("score", "working", None), ("golden", "queued", 1), ("compare", "queued", 2)]

    assert lab_jobs.advance(tree.base, repo=lab_repo).started is None, "nothing starts while SCORE runs"
    tmux["panes"] = ""  # SCORE ended
    done = lab_jobs.advance(tree.base, repo=lab_repo)
    assert done.started == ("golden", SURFACE, "laya-golden") and done.waiting == 1
    tmux["panes"] = pane("laya-golden", "0", "", "golden", SURFACE, "laya-golden")
    assert lab_jobs.advance(tree.base, repo=lab_repo).started is None
    tmux["panes"] = pane("laya-golden", "1", "1", "golden", SURFACE, "laya-golden")  # a failure never holds the queue
    done = lab_jobs.advance(tree.base, repo=lab_repo)
    assert done.started == ("compare", SURFACE, "laya-compare") and done.waiting == 0
    assert tmux["starts"] == ["laya-golden", "laya-compare"]


def test_the_queue_waits_for_a_lab_eval_started_elsewhere_and_for_a_tmux_it_cannot_read(tree, lab_repo, tmux):
    tmux["panes"] = pane("eval-laya-compare", "0", "", eval_id="laya-compare")
    assert press(tree, lab_repo, "score")["queued"] == 1
    tmux["panes"] = None
    assert lab_jobs.advance(tree.base, repo=lab_repo).waiting == 1
    assert press(tree, lab_repo, "golden")["queued"] == 2, "a tmux that would not answer never reads as idle"
    assert tmux["starts"] == []


def test_a_queued_job_is_gated_again_when_its_turn_comes(tree, lab_repo, tmux):
    [key] = tree.unlabelled(["an operator flip"])
    tree.register(train_texts=["an operator flip"])  # trained on, so never a clean label PROMOTE's guard would keep
    tree.append_jsonl(
        "data/agent-open-work-operator.jsonl",
        review_log.answer_row(
            answer_id=review_log.new_answer_id(),
            at="2026-10-27T00:00:00Z",
            surface=SURFACE,
            key=key,
            stratum="sample",
            answer="flip",
            against={"by": "claude-sonnet-5-5", "open": True},
            laya=None,
            decision_id=None,
            held_out=True,
        ),
    )
    tmux["panes"] = pane("laya-score", "0", "", "score", SURFACE)
    assert press(tree, lab_repo, "promote")["queued"] == 1
    assert press(tree, lab_repo, "golden")["queued"] == 2
    (tree.base / "data" / "agent-open-work-operator.jsonl").unlink()  # the flip is gone before PROMOTE's turn
    tmux["panes"] = ""
    done = lab_jobs.advance(tree.base, repo=lab_repo)
    assert [(v, r.code) for v, _s, r in done.dropped] == [("promote", "nothing_to_promote")]
    assert done.started == ("golden", SURFACE, "laya-golden") and tmux["starts"] == ["laya-golden"]
    assert rows.state_key("an operator flip") == key


def test_a_run_the_runner_reports_busy_keeps_its_place(tree, lab_repo, tmux, runner):
    tmux["panes"] = pane("laya-score", "0", "", "score", SURFACE)
    lab_jobs.press("golden", SURFACE, tree.base, repo=lab_repo, runner=runner)
    lab_jobs.press("compare", SURFACE, tree.base, repo=lab_repo, runner=runner)
    tmux["panes"] = ""
    tmux["replies"] = [{"ok": False, "code": "working", "message": "GOLDEN is already running"}]
    done = lab_jobs.advance(tree.base, repo=lab_repo, runner=runner)
    assert done.started is None and done.dropped == [] and done.waiting == 2
    assert [e["verb"] for e in lab_jobs.read_queue(tree.base)] == ["golden", "compare"]


def test_a_failed_start_is_the_pressers_refusal_and_leaves_nothing_queued(tree, lab_repo, tmux):
    tmux["replies"] = [(1, "", f"no server at {Path.home()}/x")]
    with pytest.raises(Refusal) as caught:
        lab_jobs.press("golden", SURFACE, tree.base, repo=lab_repo)
    assert (caught.value.status, caught.value.code) == (500, "spawn_failed")
    assert str(Path.home()) not in caught.value.message and "~/x" in caught.value.message
    assert lab_jobs.read_queue(tree.base) == []
    tmux["replies"] = [(None, "", "")]
    assert refusal(lambda: lab_jobs.press("golden", SURFACE, tree.base, repo=lab_repo)) == (500, "spawn_failed")


def test_the_gates_refuse_before_anything_queues(tree, lab_repo, tmux, monkeypatch, tmp_path):
    assert refusal(lambda: lab_jobs.press("score", SURFACE, None, repo=lab_repo)) == (503, "params_unavailable")
    with pytest.raises(Refusal) as caught:
        lab_jobs.press("score", SURFACE, None, repo=lab_repo)
    assert caught.value.hint == envelope.PARAMS_HINT, "one params_unavailable, the deck's and the lab's"
    assert refusal(lambda: lab_jobs.press("score", SURFACE, tmp_path / "nope", repo=lab_repo)) == (503, "params_unavailable")
    assert refusal(lambda: lab_jobs.press("score", SURFACE, tree.base, repo=tmp_path)) == (503, "lab_missing")
    assert refusal(lambda: lab_jobs.press("sweep", SURFACE, tree.base, repo=lab_repo)) == (409, "too_few_labels")
    assert refusal(lambda: lab_jobs.press("promote", SURFACE, tree.base, repo=lab_repo)) == (409, "nothing_to_promote")
    monkeypatch.setattr(lab_jobs, "tmux_available", lambda: False)
    assert refusal(lambda: lab_jobs.press("score", SURFACE, tree.base, repo=lab_repo)) == (503, "tmux_missing")
    assert tmux["starts"] == [] and lab_jobs.read_queue(tree.base) == []


def test_a_queue_that_cannot_be_written_is_a_refusal_not_a_crash(tree, lab_repo, tmux):
    os.chmod(tree.base, 0o500)
    try:
        assert refusal(lambda: lab_jobs.press("score", SURFACE, tree.base, repo=lab_repo)) == (503, "queue_unwritable")
    finally:
        os.chmod(tree.base, 0o700)
    assert tmux["starts"] == []


@pytest.mark.parametrize(
    "body",
    [
        [],
        {},
        {"verb": "score"},
        {"verb": "train", "surface": SURFACE},
        {"verb": "label", "surface": SURFACE},
        {"verb": "score", "surface": "paste"},
        {"verb": "score", "surface": SURFACE, "x": 1},
    ],
)
def test_bad_lab_bodies(body):
    assert refusal(lambda: lab_jobs.clean_lab_request(body)) == (400, "bad_body")


# -- the route and the drain -------------------------------------------------------

HEADERS = {"host": "127.0.0.1:7682", "origin": "http://localhost:5173", "content-type": "application/json"}


def test_the_lab_route_starts_or_queues_and_kicks_the_drain(tree, fake_runtime, tmux, monkeypatch, lab_repo, no_drain_thread):
    plugin = LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    real_press = lab_jobs.press
    monkeypatch.setattr(
        lab_jobs,
        "press",
        lambda verb, surface, base, loaded=None, runner=None: real_press(
            verb, surface, base, loaded=loaded, repo=lab_repo, runner=runner
        ),
    )
    status, body = plugin.handle_request("POST", "/lab", {}, {"verb": "score", "surface": SURFACE}, HEADERS)
    assert status == 202 and body["session"] == "laya-score" and body["queued"] is None
    assert no_drain_thread == []
    tmux["panes"] = pane("laya-score", "0", "", "score", SURFACE, "laya-score")
    status, body = plugin.handle_request("POST", "/lab", {}, {"verb": "golden", "surface": SURFACE}, HEADERS)
    assert status == 202 and body["queued"] == 1 and body["session"] is None
    assert len(no_drain_thread) == 1, "a queued press wakes the drain"
    status, body = plugin.handle_request("POST", "/lab", {}, {"verb": "sweep", "surface": SURFACE}, HEADERS)
    assert status == 409 and body["code"] == "too_few_labels" and body["error"] == body["message"]
    assert body["session"] is None and body["queued"] is None
    status, ops = plugin.handle_request("GET", "/ops", {}, None, HEADERS)
    assert status == 200 and [(j["verb"], j["state"]) for j in ops["jobs"] if j["state"] != "idle"] == [
        ("score", "working"),
        ("golden", "queued"),
    ]


def test_a_queue_that_outlived_a_bridge_restart_wakes_the_drain_at_mount(tree, fake_runtime, no_drain_thread):
    LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    assert no_drain_thread == []
    lab_jobs.write_queue(tree.base, [{"verb": "golden", "surface": SURFACE, "at": "2026-11-09T03:00:00Z"}])
    LayaPlugin(context=None, runtime=fake_runtime, params_dir=lambda: tree.base)
    assert len(no_drain_thread) == 1


def test_the_drain_passes_until_nothing_waits_and_survives_a_failed_pass():
    passes = [
        RuntimeError("boom"),
        lab_jobs.Advance(held=False, waiting=2),
        lab_jobs.Advance(started=("golden", SURFACE, "laya-golden"), waiting=1),
        lab_jobs.Advance(dropped=[("compare", SURFACE, Refusal(503, "lab_missing", "laya-lab is not installed"))], waiting=0),
    ]

    def advance():
        step = passes.pop(0)
        if isinstance(step, Exception):
            raise step
        return step

    log: list[str] = []
    loops: list = []
    drain = LabDrain(advance, log.append, interval=0, start=loops.append)
    drain.kick()
    drain.kick()
    assert len(loops) == 1, "one thread at a time"
    loops[0]()
    assert passes == [] and log == [
        "[laya] lab queue pass failed: RuntimeError",
        "[laya] lab queue: golden started in laya-golden",
        "[laya] lab queue: compare dropped — lab_missing: laya-lab is not installed",
    ]
    drain.kick()
    assert len(loops) == 2, "a finished loop can be kicked again"
    drain.close()
    drain.kick()
    assert len(loops) == 2, "a closed drain never starts"


def test_a_press_that_lands_as_the_last_pass_exits_still_gets_a_pass():
    """The lost wakeup: the pass that starts A reads an empty queue and lets go of the queue lock; B is pressed, queued
    behind A, and kicks while the loop is still alive. The loop owes B another pass instead of exiting on it."""
    drain_box: list[LabDrain] = []
    passes: list = []

    def press_b_after_the_read():
        drain_box[0].kick()  # the press's kick: the loop is alive, so no second thread
        return lab_jobs.Advance(started=("score", SURFACE, "laya-score"), waiting=0)

    script = [
        press_b_after_the_read,
        lambda: lab_jobs.Advance(waiting=1),
        lambda: lab_jobs.Advance(started=("golden", SURFACE, "laya-golden"), waiting=0),
    ]

    def advance():
        step = script.pop(0)
        passes.append(step)
        return step()

    log: list[str] = []
    loops: list = []
    drain = LabDrain(advance, log.append, interval=0, start=loops.append)
    drain_box.append(drain)
    drain.kick()
    drain.kick()  # a kick before the first pass is that pass, not one more
    loops[0]()
    assert len(loops) == 1, "the sticky kick reuses the live loop"
    assert (
        script == []
        and len(passes) == 3
        and log == ["[laya] lab queue: score started in laya-score", "[laya] lab queue: golden started in laya-golden"]
    )
    drain.kick()
    assert len(loops) == 2, "the loop retired after its owed pass, so a later kick starts a fresh one"


def test_a_thread_that_never_started_does_not_wedge_the_drain():
    def refuse(_work):
        raise RuntimeError("can't start new thread")

    loops: list = []
    drain = LabDrain(lambda: lab_jobs.Advance(waiting=0), lambda _m: None, interval=0, start=refuse)
    with pytest.raises(RuntimeError):
        drain.kick()
    drain._start = loops.append
    drain.kick()
    assert len(loops) == 1, "a failed start leaves the drain kickable"
