import json
import subprocess
import uuid
from pathlib import Path

import pytest

from worktree.task_groups import (
    TaskGroups,
    batch_due,
    delivered,
    follows_open_pr,
    new_group,
    publishable,
    releasable,
)
from worktree import task_group_runner as runner

PARENT = {"parent_id": str(uuid.uuid4()), "parent_name": "parent"}
SHA = "a" * 40


@pytest.mark.parametrize("phase", ["blocked", "complete"])
def test_outcomes_stay_in_rack_state_without_terminal_input(store, monkeypatch, phase):
    group = make(store, 1)
    if phase == "blocked":
        store.report(group["id"], {"branch": "batch/lane-0", "stage": "blocked", "note": "Waiting for scene review"})
    else:
        ready(store, group)
        store.control(group["id"], "seal")
        store.worker_result(group["id"], "batch/lane-0", pr_status="open", pr_url="https://github.com/o/r/pull/12")
    # Also exercise a legacy record that previously queued terminal notification.
    store.change(group["id"], lambda g: g.update(notified=None))
    monkeypatch.setattr(runner.pr, "_run", lambda *a, **k: pytest.fail("status must not touch terminals or start a process"))
    job = runner.GroupRunner(store, list, Path("/unused"), "main", print)
    job.tick()
    job.tick()
    view = store.views()[0]
    assert view["phase"] == phase
    assert view["parent_id"] == PARENT["parent_id"]
    if phase == "blocked":
        assert view["workers"][0]["note"] == "Waiting for scene review"
    else:
        assert view["opened"] == 1 and view["workers"][0]["pr_url"].endswith("/12")


def make(store, count=2, start=SHA):
    return store.create(
        new_group(
            {"label": "Riverfront", "start_sha": start, "workers": [{"branch": f"batch/lane-{i}"} for i in range(count)]}, PARENT
        )
    )


def ready(store, group, index=0, **extra):
    return store.report(
        group["id"],
        {
            "branch": group["workers"][index]["branch"],
            "stage": "ready",
            "head": SHA,
            "validated": True,
            "title": "Fix riverfront",
            "body": "Behavior and validation.",
            **extra,
        },
    )


@pytest.fixture
def store(tmp_path):
    return TaskGroups(tmp_path / "groups.sqlite")


def test_batch_waits_for_seal_every_ready_and_no_blocker(store):
    group = make(store)
    group = ready(store, group)
    assert group["percent"] == 50 and group["ready"] == 1
    assert not releasable(group)
    group = ready(store, group, 1)
    assert group["percent"] == 100 and not releasable(group)
    group = store.control(group["id"], "seal")
    assert releasable(group)
    group = store.report(group["id"], {"branch": group["workers"][1]["branch"], "stage": "blocked", "note": "missing survey"})
    assert group["blockers"] == 1 and not releasable(group)


def indexed_pr(state="merged", **overrides):
    return {
        "state": state,
        "url": "https://github.com/o/r/pull/24",
        "draft": False,
        "head_oid": SHA,
        "base_ref": "main",
        **overrides,
    }


def test_manual_merge_reconciles_reserved_branch_and_preserves_validation_history(store):
    group = make(store, 1)
    store.report(group["id"], {"branch": "batch/lane-0", "stage": "blocked", "note": "Old review wait"})
    branch = f"pr/{group['id'][:8]}/wt-batch-lane-0/clean"
    store.reconcile_prs({branch: indexed_pr()}, "main")
    result = store.views()[0]
    assert (result["percent"], result["opened"], result["blockers"], result["phase"]) == (100, 1, 0, "complete")
    assert result["ready"] == 0  # delivery must not manufacture visual validation
    assert result["workers"][0]["stage"] == "blocked"
    assert result["workers"][0]["pr_state"] == "merged"
    stamp = result["updated_at"]
    store.reconcile_prs({branch: indexed_pr()}, "main")
    store.reconcile_prs({}, "main")  # truncated/offline index cannot erase known delivery
    assert store.views()[0]["updated_at"] == stamp


@pytest.mark.parametrize(
    "row", [indexed_pr("closed"), indexed_pr(draft=True), indexed_pr(base_ref="unrelated"), indexed_pr(head_oid="invalid")]
)
def test_reconciliation_rejects_non_delivery_evidence(store, row):
    make(store, 1)
    store.reconcile_prs({"batch/lane-0": row}, "main")
    assert store.views()[0]["opened"] == 0


def test_reconciliation_refuses_ambiguous_namespace_and_mismatched_ready_head(store):
    group = make(store, 1)
    prefix = f"pr/{group['id'][:8]}/wt-batch-lane-0/"
    store.reconcile_prs({prefix + "one": indexed_pr(), prefix + "two": indexed_pr()}, "main")
    assert store.views()[0]["opened"] == 0
    ready(store, group)
    store.reconcile_prs({"batch/lane-0": indexed_pr(head_oid="b" * 40)}, "main")
    assert store.views()[0]["opened"] == 0


def test_manual_open_pr_counts_without_claiming_completion_and_allows_final_report(store):
    group = make(store, 1)
    store.report(group["id"], {"branch": "batch/lane-0", "stage": "validate"})
    store.reconcile_prs({"batch/lane-0": indexed_pr("open")}, "main")
    result = store.views()[0]
    assert (result["percent"], result["opened"], result["phase"]) == (80, 1, "working")
    result = ready(store, group)
    assert result["phase"] == "complete"
    assert result["workers"][0]["pr_url"].endswith("/24")


def test_explicit_validation_and_publication_branch_required(store):
    group = make(store)
    for extra in ({"validated": False}, {"head": "bad"}, {"title": ""}, {"body": ""}, {"publication_branch": "someone/else"}):
        with pytest.raises(ValueError):
            ready(store, group, **extra)
    assert store.get(group["id"])["workers"][0]["stage"] == "queued"
    clean = f"pr/{group['id'][:8]}/{group['workers'][0]['session']}/clean"
    assert ready(store, group, publication_branch=clean)["ready"] == 1


def test_duplicate_sessions_and_invalid_stage_rejected(store):
    with pytest.raises(ValueError):
        new_group({"label": "x", "start_sha": SHA, "workers": [{"branch": "a/b"}, {"branch": "a-b"}]}, PARENT)
    group = make(store)
    with pytest.raises(ValueError):
        store.report(group["id"], {"branch": "batch/lane-0", "stage": []})


def test_restart_preserves_members_blockers_and_recovers_only_inflight(store):
    group = make(store)
    ready(store, group)
    store.worker_result(group["id"], "batch/lane-0", pr_status="opening")
    store.report(group["id"], {"branch": "batch/lane-1", "stage": "failed", "note": "launch refused"})
    restored = TaskGroups(store.path)
    restored.recover()
    result = restored.list()[0]
    assert result["workers"][0]["pr_status"] == "pending"
    assert result["blockers"] == 1 and len(result["workers"]) == 2
    assert not releasable(result)


def test_pause_and_frozen_publication(store):
    group = make(store, 1)
    ready(store, group)
    store.control(group["id"], "seal")
    assert not releasable(store.control(group["id"], "pause"))
    assert releasable(store.control(group["id"], "resume"))
    store.worker_result(group["id"], "batch/lane-0", pr_status="opening")
    with pytest.raises(ValueError):
        ready(store, group)


def test_no_side_effect_for_partial_or_failed_batch(store, monkeypatch):
    group = make(store)
    ready(store, group)
    store.control(group["id"], "seal")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: pytest.fail("no network before all ready"))
    job = runner.GroupRunner(store, list, Path("/unused"), "main", print)
    job.tick()


def test_all_preflight_before_any_publish_and_retry_is_idempotent(store, monkeypatch):
    group = make(store)
    ready(store, group)
    ready(store, group, 1)
    store.control(group["id"], "seal")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: "")
    events = []

    def preflight(g, w, m, b):
        events.append("check:" + w["branch"])
        if w["branch"].endswith("1"):
            raise ValueError("dirty")

    monkeypatch.setattr(runner, "preflight", preflight)
    monkeypatch.setattr(runner, "publish", lambda *a: pytest.fail("blocked batch must not publish"))
    job = runner.GroupRunner(store, list, Path("/unused"), "main", print)
    job.tick()
    assert store.list()[0]["blockers"] == 1
    monkeypatch.setattr(runner, "preflight", lambda *a: None)

    def publish(g, w, m, b):
        events.append("publish:" + w["branch"])
        return "https://github.com/o/r/pull/" + str(len(events))

    monkeypatch.setattr(runner, "publish", publish)
    store.control(group["id"], "retry")
    job.tick()
    job.tick()
    assert store.list()[0]["opened"] == 2
    assert len([e for e in events if e.startswith("publish:")]) == 2


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.test")
    (path / "code").write_text("base\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")
    base = git(path, "rev-parse", "HEAD")
    git(path, "update-ref", "refs/remotes/origin/main", base)
    git(path, "switch", "-c", "batch/lane-0")
    (path / "code").write_text("fixed\n")
    git(path, "commit", "-am", "fix")
    return path, base, git(path, "rev-parse", "HEAD")


def test_real_git_pin_dirty_and_snapshot_guards(store, repo):
    path, base, head = repo
    group = make(store, 1, base)
    group = ready(store, group, head=head)
    worker = group["workers"][0]
    managed = [{"branch": "batch/lane-0", "path": str(path)}]
    assert runner.preflight(group, worker, managed, "main") == (path, "batch/lane-0")
    (path / "code").write_text("changed after validation\n")
    with pytest.raises(ValueError, match="uncommitted"):
        runner.preflight(group, worker, managed, "main")
    git(path, "commit", "-am", "later")
    with pytest.raises(ValueError, match="moved"):
        runner.preflight(group, worker, managed, "main")
    worker["head"] = git(path, "rev-parse", "HEAD")
    group["start_sha"] = head
    with pytest.raises(ValueError, match="snapshot"):
        runner.preflight(group, worker, managed, "main")
    clean = f"pr/{group['id'][:8]}/wt-batch-lane-0/clean"
    git(path, "branch", "-m", clean)
    worker["publication_branch"] = clean
    with pytest.raises(ValueError, match="still contains"):
        runner.preflight(group, worker, [{"branch": clean, "path": str(path)}], "main")


def test_restarted_publication_queries_exact_pr_without_another_push(store, repo, monkeypatch):
    path, base, head = repo
    group = ready(store, make(store, 1, base), head=head)
    worker = group["workers"][0]
    calls = []
    original = runner.checked

    def checked(command, cwd, timeout=10):
        if command[:2] == ["git", "push"]:
            calls.append(command)
            return ""
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps(
                [
                    {
                        "url": "https://github.com/o/r/pull/12",
                        "headRefOid": head,
                        "headRefName": worker["branch"],
                        "baseRefName": "main",
                    }
                ]
            )
        if command[0] == "gh":
            pytest.fail("existing PR must not be recreated")
        return original(command, cwd, timeout)

    monkeypatch.setattr(runner, "checked", checked)
    assert runner.publish(group, worker, [{"branch": worker["branch"], "path": str(path)}], "main").endswith("/12")
    assert calls == []


def test_parent_can_enroll_known_publication_branches_but_cannot_register_a_worker_twice(store):
    group = store.create(
        new_group(
            {
                "label": "Existing batch",
                "start_sha": SHA,
                "workers": [{"branch": "batch/lane-0", "publication_branch": "fix/clean-lane"}],
            },
            PARENT,
        )
    )
    assert ready(store, group)["workers"][0]["publication_branch"] == "fix/clean-lane"
    with pytest.raises(ValueError, match="already belongs"):
        make(store, 1)


def test_polling_view_omits_large_pr_body_but_publication_keeps_it(store):
    group = make(store, 1)
    ready(store, group)
    assert "body" not in store.views()[0]["workers"][0]
    assert store.get(group["id"])["workers"][0]["body"]


def test_new_publication_pushes_the_tested_object_and_uses_body_file(store, repo, monkeypatch):
    path, base, head = repo
    group = ready(store, make(store, 1, base), head=head)
    worker = group["workers"][0]
    calls = []
    original = runner.checked

    def checked(command, cwd, timeout=10):
        if command[:2] == ["git", "push"]:
            calls.append(command)
            return ""
        if command[:3] == ["gh", "pr", "list"]:
            return "[]"
        if command[:3] == ["gh", "pr", "create"]:
            assert Path(command[-1]).read_text() == worker["body"]
            return "https://github.com/o/r/pull/13"
        return original(command, cwd, timeout)

    monkeypatch.setattr(runner, "checked", checked)
    assert runner.publish(group, worker, [{"branch": worker["branch"], "path": str(path)}], "main").endswith("/13")
    assert calls[0][-1] == f"{head}:refs/heads/batch/lane-0"


# ── delivered: a branch that landed inside another branch's PR ──────────────
# The rule of 2026-11-14, over invented groups: lanes of one build merged through ONE combined PR read
# `blocked` with no PR of its own, while the harbor said each had landed.

LANDED = {"how": "ancestor", "pr": 41}


def each_group(store, count=2):
    return store.create(
        new_group(
            {
                "label": "Build",
                "start_sha": SHA,
                "membership": "open",
                "release": "each",
                "workers": [{"branch": f"batch/lane-{i}"} for i in range(count)],
            },
            PARENT,
        )
    )


def test_a_landed_worker_with_no_pr_is_delivered_not_a_blocker_and_not_publishable(store):
    group = each_group(store)
    store.report(group["id"], {"branch": "batch/lane-0", "stage": "blocked", "note": "waiting on the combined PR"})
    ready(store, group, 1)
    assert store.list()[0]["blockers"] == 1
    store.reconcile_landing({"batch/lane-0": LANDED, "batch/lane-1": {"how": "commits", "pr": None}})
    view = store.list()[0]
    lane = view["workers"][0]
    assert lane["landing"] == {**LANDED, "branch": "batch/lane-0"} and lane["error"] is None
    assert delivered(lane) and not lane["pr_url"]
    assert (view["blockers"], view["percent"], view["phase"]) == (0, 100, "complete")
    assert not publishable(view, lane) and not publishable(view, view["workers"][1])
    with pytest.raises(ValueError, match="frozen"):
        ready(store, group)
    assert not store.awaiting_delivery()


def test_resuming_a_paused_group_never_publishes_a_landed_worker(store, monkeypatch):
    group = each_group(store, 1)
    store.control(group["id"], "pause")
    ready(store, group)
    store.reconcile_landing({"batch/lane-0": LANDED})
    store.control(group["id"], "resume")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: pytest.fail("a landed worker is never published"))
    monkeypatch.setattr(runner, "publish", lambda *a, **k: pytest.fail("a landed worker is never published"))
    runner.GroupRunner(store, list, Path("/unused"), "main", print).tick()
    assert store.list()[0]["phase"] == "complete"


def test_a_batch_releases_without_the_landed_worker_and_never_publishes_it(store, monkeypatch):
    group = make(store)
    ready(store, group, 1)
    store.reconcile_landing({"batch/lane-0": LANDED})
    store.control(group["id"], "seal")
    current = store.get(group["id"])
    assert releasable(current), "a delivered worker waits on nothing"
    assert [batch_due(w) for w in current["workers"]] == [False, True]
    monkeypatch.setattr(runner, "checked", lambda *a, **k: "")
    monkeypatch.setattr(runner, "preflight", lambda g, w, m, b: None)
    published = []
    monkeypatch.setattr(runner, "publish", lambda g, w, m, b: published.append(w["branch"]) or "https://github.com/o/r/pull/9")
    runner.GroupRunner(store, list, Path("/unused"), "main", print).tick()
    assert published == ["batch/lane-1"]
    assert store.list()[0]["phase"] == "complete"


def test_the_publication_branch_landing_delivers_the_worker_too(store):
    group = make(store, 1)
    branch = f"pr/{group['id'][:8]}/wt-batch-lane-0/clean"
    ready(store, group, publication_branch=branch)
    store.reconcile_landing({branch: {"how": "commits", "pr": None}})
    assert store.list()[0]["workers"][0]["landing"]["branch"] == branch


def test_a_landed_worker_stops_following_its_own_open_pr(store):
    group = each_group(store, 1)
    ready(store, group)
    store.worker_result(group["id"], "batch/lane-0", pr_url="https://github.com/o/r/pull/5", pr_status="open", pr_state="open")
    current = store.get(group["id"])
    assert follows_open_pr(current, current["workers"][0])
    store.reconcile_landing({"batch/lane-0": LANDED})
    current = store.get(group["id"])
    assert not follows_open_pr(current, current["workers"][0])


def test_landing_is_final_and_waits_for_a_publication_in_flight(store):
    group = make(store, 2)
    store.worker_result(group["id"], "batch/lane-1", pr_status="opening")
    store.reconcile_landing({"batch/lane-0": LANDED, "batch/lane-1": LANDED})
    workers = store.list()[0]["workers"]
    assert delivered(workers[0]) and not delivered(workers[1])
    # A tree CLEANUP removed (its branch gone from every later sweep) never undoes it.
    store.reconcile_landing({})
    store.reconcile_landing({"other": LANDED})
    assert delivered(store.list()[0]["workers"][0])
    assert store.awaiting_delivery()


def test_squash_landing_on_remote_group_base_without_a_local_base_branch(repo, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from worktree.plugin import WorktreePlugin

    path, start, _ = repo
    target = "integration/staging"
    branch = "batch/lane-0"
    git(path, "switch", "--detach", start)
    git(path, "merge", "--squash", branch)
    git(path, "commit", "-m", "Combined work")
    git(path, "update-ref", f"refs/remotes/origin/{target}", git(path, "rev-parse", "HEAD"))
    monkeypatch.setenv("TLDRACK_WORKTREE_PATH", str(tmp_path / "prs.json"))
    plugin = WorktreePlugin(SimpleNamespace(start_dir=path, log=lambda _: None))
    monkeypatch.setattr(plugin, "_start_background", lambda fn, _name: fn())
    monkeypatch.setattr(plugin, "_maybe_refresh_prs", lambda _: None)
    group = plugin._groups.create(
        new_group({"label": "Run", "start_sha": start, "base": target, "workers": [{"branch": branch}]}, PARENT)
    )
    for _ in range(3):
        plugin._sync_group_prs()
    assert plugin._groups.get(group["id"])["workers"][0]["landing"] == {"branch": branch, "how": "squash", "pr": None}
    assert git(path, "rev-parse", "main") == start
