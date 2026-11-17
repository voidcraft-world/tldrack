"""Task-group modes: open membership + join, release "each", the fast-forward-only
follow-up, and the per-worker CI rollup. The batch contract is test_task_groups.py's."""

import json
import subprocess
import uuid
from pathlib import Path

import pytest

from worktree import task_group_runner as runner
from worktree.task_groups import (
    OPEN_MAX_WORKERS,
    TaskGroupConflict,
    TaskGroups,
    follow_up_due,
    membership_of,
    new_group,
    publishable,
    release_of,
    summary,
)

PARENT = {"parent_id": str(uuid.uuid4()), "parent_name": "lead"}
SHA = "a" * 40
H1, H2 = "b" * 40, "c" * 40
URL = "https://github.com/o/r/pull/41"


@pytest.fixture
def store(tmp_path):
    return TaskGroups(tmp_path / "groups.sqlite")


def run_group(store, workers=(), **modes):
    return store.create(
        new_group(
            {"label": "Lead run", "start_sha": SHA, "membership": "open", "release": "each", "workers": list(workers), **modes},
            PARENT,
        )
    )


def ready(store, group_id, branch, head=SHA):
    return store.report(
        group_id,
        {
            "branch": branch,
            "stage": "ready",
            "head": head,
            "validated": True,
            "title": "Step",
            "body": "What and how it was tested.",
        },
    )


def worker(group, branch):
    return next(w for w in group["workers"] if w["branch"] == branch)


def job(store, clock=None):
    return runner.GroupRunner(store, list, Path("/unused"), "main", print, clock=clock or (lambda: 0.0))


# ── modes and membership ────────────────────────────────────────────────────


def test_old_callers_and_stored_groups_read_as_fixed_batch(store):
    group = store.create(new_group({"label": "Batch", "start_sha": SHA, "workers": [{"branch": "batch/a"}]}, PARENT))
    assert (group["membership"], group["release"]) == ("fixed", "batch")
    legacy = {k: v for k, v in group.items() if k not in ("membership", "release")}
    assert (membership_of(legacy), release_of(legacy)) == ("fixed", "batch")
    ready(store, group["id"], "batch/a")
    assert not publishable(store.get(group["id"]), store.get(group["id"])["workers"][0])


@pytest.mark.parametrize(
    "body, message",
    [
        ({"membership": "fixed", "workers": []}, "1–32"),
        ({"membership": "open", "workers": [{"branch": f"lead/s{i}"} for i in range(65)]}, "0–64"),
        ({"membership": "shared"}, "membership"),
        ({"release": "drip"}, "release"),
    ],
)
def test_mode_and_size_validation(body, message):
    with pytest.raises(ValueError, match=message):
        new_group({"label": "x", "start_sha": SHA, **body}, PARENT)


def test_an_empty_open_group_is_working_at_zero_percent(store):
    group = run_group(store)
    assert (group["total"], group["percent"], group["phase"], group["ready"]) == (0, 0, "working", 0)


def test_join_adds_one_validated_worker_and_a_repeat_is_a_no_op(store):
    group = run_group(store)
    joined = store.join(group["id"], {"branch": "lead/s1", "start_sha": H1})
    assert joined["total"] == 1 and worker(joined, "lead/s1")["start_sha"] == H1
    again = store.join(group["id"], {"branch": "lead/s1"})
    assert again["total"] == 1
    with pytest.raises(TaskGroupConflict, match="different publication"):
        store.join(group["id"], {"branch": "lead/s1", "publication_branch": "lead/other"})
    with pytest.raises(ValueError, match="distinct session"):
        store.join(group["id"], {"branch": "lead-s1"})
    with pytest.raises(ValueError, match="valid and distinct"):
        store.join(group["id"], {"branch": "lead/s2", "publication_branch": "lead/s1"})
    store.join(group["id"], {"branch": "lead/s5", "publication_branch": "lead/pub5"})
    with pytest.raises(ValueError, match="overlap"):
        store.join(group["id"], {"branch": "lead/s6", "publication_branch": "lead/s5"})
    with pytest.raises(ValueError, match="start_sha"):
        store.join(group["id"], {"branch": "lead/s3", "start_sha": "main"})
    assert [w["branch"] for w in store.get(group["id"])["workers"]] == ["lead/s1", "lead/s5"]


def test_join_refuses_fixed_sealed_full_and_another_groups_branch(store):
    fixed = store.create(new_group({"label": "Batch", "start_sha": SHA, "workers": [{"branch": "batch/a"}]}, PARENT))
    with pytest.raises(TaskGroupConflict, match="fixed"):
        store.join(fixed["id"], {"branch": "lead/s1"})
    group = run_group(store)
    with pytest.raises(ValueError, match="already belongs"):
        store.join(group["id"], {"branch": "batch/a"})
    store.control(group["id"], "seal")
    with pytest.raises(TaskGroupConflict, match="sealed"):
        store.join(group["id"], {"branch": "lead/s1"})
    full = run_group(store, [{"branch": f"full/s{i}"} for i in range(OPEN_MAX_WORKERS)])
    with pytest.raises(TaskGroupConflict, match="at most"):
        store.join(full["id"], {"branch": "full/one-more"})


# ── release "each" ──────────────────────────────────────────────────────────


def test_each_publishes_one_ready_worker_while_a_sibling_works(store, monkeypatch):
    group = run_group(store, [{"branch": "lead/s1"}, {"branch": "lead/s2"}])
    store.report(group["id"], {"branch": "lead/s2", "stage": "implement"})
    ready(store, group["id"], "lead/s1")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: "")
    published = []
    monkeypatch.setattr(runner, "publish", lambda g, w, m, b: published.append(w["branch"]) or URL)
    job(store).tick()
    view = store.get(group["id"])
    assert published == ["lead/s1"] and not view["sealed"]
    s1 = worker(view, "lead/s1")
    assert (s1["pr_url"], s1["pr_status"], s1["pr_state"], s1["pr_head"]) == (URL, "open", "open", SHA)
    assert worker(view, "lead/s2")["pr_url"] is None
    job(store).tick()
    assert published == ["lead/s1"]  # a published worker is not published again


def test_each_keeps_a_failure_on_its_own_worker_and_pause_holds_all(store, monkeypatch):
    group = run_group(store, [{"branch": "lead/s1"}, {"branch": "lead/s2"}])
    ready(store, group["id"], "lead/s1")
    ready(store, group["id"], "lead/s2")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: "")

    def publish(g, w, m, b):
        if w["branch"] == "lead/s1":
            raise ValueError("publication worktree has uncommitted changes")
        return URL

    monkeypatch.setattr(runner, "publish", publish)
    store.control(group["id"], "pause")
    job(store).tick()
    assert store.list()[0]["opened"] == 0
    store.control(group["id"], "resume")
    job(store).tick()
    view = store.get(group["id"])
    assert worker(view, "lead/s1")["error"] == "publication worktree has uncommitted changes"
    assert worker(view, "lead/s2")["pr_url"] == URL


def test_a_failed_fetch_marks_every_due_worker(store, monkeypatch):
    group = run_group(store, [{"branch": "lead/s1"}, {"branch": "lead/s2"}])
    ready(store, group["id"], "lead/s1")
    ready(store, group["id"], "lead/s2")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: (_ for _ in ()).throw(ValueError("offline")))
    monkeypatch.setattr(runner, "publish", lambda *a: pytest.fail("nothing publishes without the base"))
    job(store).tick()
    assert [w["error"] for w in store.get(group["id"])["workers"]] == ["offline", "offline"]


def test_a_report_landing_mid_tick_is_not_published_against_the_old_head(store, monkeypatch):
    group = run_group(store, [{"branch": "lead/s1"}])
    ready(store, group["id"], "lead/s1")

    def fetch(*_a, **_k):
        ready(store, group["id"], "lead/s1", head=H1)  # the worker re-reports during the fetch
        return ""

    monkeypatch.setattr(runner, "checked", fetch)
    monkeypatch.setattr(runner, "publish", lambda *a: pytest.fail("stale claim published"))
    job(store).tick()
    assert worker(store.get(group["id"]), "lead/s1")["pr_url"] is None


# ── follow-up heads ─────────────────────────────────────────────────────────


def opened(store, branch="lead/s1", head=SHA):
    group = run_group(store, [{"branch": branch}])
    ready(store, group["id"], branch, head)
    store.worker_result(group["id"], branch, pr_url=URL, pr_status="open", pr_state="open", pr_head=head)
    return group


def test_a_re_reported_head_on_an_open_pr_is_a_follow_up(store, monkeypatch):
    group = opened(store)
    current = store.get(group["id"])
    assert not follow_up_due(current, current["workers"][0])
    ready(store, group["id"], "lead/s1", H2)
    current = store.get(group["id"])
    assert follow_up_due(current, current["workers"][0]) and not publishable(current, current["workers"][0])
    monkeypatch.setattr(runner, "checked", lambda *a, **k: "")
    calls = []
    monkeypatch.setattr(runner, "publish", lambda *a: pytest.fail("a follow-up never opens a second PR"))
    monkeypatch.setattr(
        runner,
        "follow_up",
        lambda g, w, m, b: calls.append(w["head"]) or {"pr_head": w["head"], "pr_state": "open", "error": None},
    )
    job(store).tick()
    job(store).tick()
    after = store.get(group["id"])["workers"][0]
    assert calls == [H2] and (after["pr_head"], after["pr_status"], after["pr_url"]) == (H2, "open", URL)


def test_a_failed_follow_up_keeps_its_pr_and_retry_clears_it(store, monkeypatch):
    group = opened(store)
    ready(store, group["id"], "lead/s1", H2)
    monkeypatch.setattr(runner, "checked", lambda *a, **k: "")
    monkeypatch.setattr(runner, "follow_up", lambda *a: (_ for _ in ()).throw(ValueError(runner.FOLLOW_UP_REFUSAL)))
    job(store).tick()
    failed = store.get(group["id"])["workers"][0]
    assert (failed["error"], failed["pr_status"], failed["pr_url"]) == (runner.FOLLOW_UP_REFUSAL, "open", URL)
    retried = store.control(group["id"], "retry")["workers"][0]
    assert retried["error"] is None and retried["pr_status"] == "open"


def test_an_interrupted_follow_up_recovers_as_open(store):
    group = opened(store)
    store.worker_result(group["id"], "lead/s1", pr_status="opening")
    store.recover()
    assert store.get(group["id"])["workers"][0]["pr_status"] == "open"


def test_a_batch_group_never_follows_up(store):
    group = store.create(new_group({"label": "Batch", "start_sha": SHA, "workers": [{"branch": "batch/a"}]}, PARENT))
    ready(store, group["id"], "batch/a", H2)
    store.worker_result(group["id"], "batch/a", pr_url=URL, pr_status="open", pr_state="open", pr_head=H1)
    current = store.get(group["id"])
    assert not follow_up_due(current, current["workers"][0])


def git(path, *args):
    return subprocess.check_output(["git", "-C", str(path), *args], text=True, stderr=subprocess.DEVNULL).strip()


@pytest.fixture
def published(tmp_path):
    """A repo whose `lead/s1` PR head H1 is on a real (bare) origin, and a tested H2 on top."""
    origin = tmp_path / "origin.git"
    subprocess.check_call(["git", "init", "-q", "--bare", "-b", "main", str(origin)])
    path = tmp_path / "repo"
    path.mkdir()
    git(path, "init", "-b", "main")
    git(path, "config", "user.name", "Fixture")
    git(path, "config", "user.email", "fixture@example.test")
    (path / "code").write_text("base\n")
    git(path, "add", ".")
    git(path, "commit", "-m", "base")
    git(path, "remote", "add", "origin", str(origin))
    git(path, "push", "-q", "origin", "main")
    git(path, "fetch", "-q", "origin")
    base = git(path, "rev-parse", "HEAD")
    git(path, "switch", "-c", "lead/s1")
    (path / "code").write_text("step\n")
    git(path, "commit", "-am", "step")
    h1 = git(path, "rev-parse", "HEAD")
    git(path, "push", "-q", "origin", f"{h1}:refs/heads/lead/s1")
    (path / "code").write_text("step, reviewed\n")
    git(path, "commit", "-am", "review fix")
    return path, origin, base, h1, git(path, "rev-parse", "HEAD")


def gh_fake(monkeypatch, view=None, listed=None):
    original = runner.checked
    pushes = []

    def checked(command, cwd, timeout=10):
        if command[:2] == ["git", "push"]:
            pushes.append(command)
        if command[:3] == ["gh", "pr", "view"]:
            return json.dumps(view)
        if command[:3] == ["gh", "pr", "list"]:
            return json.dumps(listed or [])
        if command[0] == "gh":
            pytest.fail(f"unexpected gh call {command}")
        return original(command, cwd, timeout)

    monkeypatch.setattr(runner, "checked", checked)
    return pushes


def follow_up_fixture(store, published):
    path, origin, base, h1, h2 = published
    group = run_group(store)
    group = store.join(group["id"], {"branch": "lead/s1", "start_sha": base})
    group = ready(store, group["id"], "lead/s1", h2)
    store.worker_result(group["id"], "lead/s1", pr_url=URL, pr_status="open", pr_state="open", pr_head=h1)
    group = store.get(group["id"])
    return group, group["workers"][0], [{"branch": "lead/s1", "path": str(path)}]


def open_view(head):
    return {"state": "OPEN", "headRefOid": head, "headRefName": "lead/s1", "baseRefName": "main"}


def test_follow_up_fast_forwards_the_open_pr_without_force(store, published, monkeypatch):
    path, origin, _base, h1, h2 = published
    group, member, managed = follow_up_fixture(store, published)
    pushes = gh_fake(monkeypatch, view=open_view(h1))
    assert runner.follow_up(group, member, managed, "main") == {"pr_head": h2, "pr_state": "open", "error": None}
    assert pushes == [["git", "push", "--no-follow-tags", "origin", f"{h2}:refs/heads/lead/s1"]]
    assert git(origin, "rev-parse", "refs/heads/lead/s1") == h2


def test_follow_up_refuses_a_head_that_does_not_descend_from_the_pr(store, published, monkeypatch):
    path, origin, base, h1, _h2 = published
    git(path, "reset", "-q", "--hard", base)
    (path / "code").write_text("rewritten\n")
    git(path, "commit", "-am", "rewrite")
    diverged = git(path, "rev-parse", "HEAD")
    group, member, managed = follow_up_fixture(store, published)
    member = {**member, "head": diverged}
    pushes = gh_fake(monkeypatch, view=open_view(h1))
    with pytest.raises(ValueError, match=runner.FOLLOW_UP_REFUSAL):
        runner.follow_up(group, member, managed, "main")
    assert pushes == [] and git(origin, "rev-parse", "refs/heads/lead/s1") == h1


@pytest.mark.parametrize("state", ["MERGED", "CLOSED"])
def test_follow_up_on_a_finished_pr_records_it_and_never_pushes(store, published, monkeypatch, state):
    _path, origin, _base, h1, _h2 = published
    group, member, managed = follow_up_fixture(store, published)
    pushes = gh_fake(monkeypatch, view={**open_view(h1), "state": state})
    result = runner.follow_up(group, member, managed, "main")
    assert result["pr_state"] == state.lower() and "new step" in result["error"]
    assert ("closed_seen_at" in result) == (state == "CLOSED")
    assert pushes == [] and git(origin, "rev-parse", "refs/heads/lead/s1") == h1


def test_follow_up_still_runs_the_preflight(store, published, monkeypatch):
    path, _origin, _base, h1, _h2 = published
    group, member, managed = follow_up_fixture(store, published)
    (path / "code").write_text("edited after the report\n")
    gh_fake(monkeypatch, view=open_view(h1))
    with pytest.raises(ValueError, match="uncommitted"):
        runner.follow_up(group, member, managed, "main")


def test_each_adopts_an_unindexed_open_pr_it_can_fast_forward_but_batch_does_not(store, published, monkeypatch):
    path, origin, base, h1, h2 = published
    listed = [{"url": URL, "headRefOid": h1, "headRefName": "lead/s1", "baseRefName": "main"}]
    group = store.join(run_group(store)["id"], {"branch": "lead/s1", "start_sha": base})
    group = ready(store, group["id"], "lead/s1", h2)
    managed = [{"branch": "lead/s1", "path": str(path)}]
    pushes = gh_fake(monkeypatch, listed=listed)
    assert runner.publish(group, group["workers"][0], managed, "main") == URL
    assert git(origin, "rev-parse", "refs/heads/lead/s1") == h2 and len(pushes) == 1
    batch = {**group, "release": "batch"}
    git(origin, "update-ref", "refs/heads/lead/s1", h1)
    with pytest.raises(ValueError, match="does not match the tested commit"):
        runner.publish(batch, group["workers"][0], managed, "main")


# ── CI rollup ───────────────────────────────────────────────────────────────


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def ci_view(state="OPEN", head=SHA, conclusion="SUCCESS"):
    return json.dumps(
        {"state": state, "headRefOid": head, "statusCheckRollup": [{"status": "COMPLETED", "conclusion": conclusion}]}
    )


def test_ci_is_read_at_most_once_a_minute_and_a_failure_keeps_the_last_reading(store, monkeypatch):
    group = opened(store)
    answers = [ci_view(), ValueError("gh: rate limited"), ci_view(conclusion="FAILURE", head=H2)]
    calls = []

    def checked(command, cwd, timeout=10):
        calls.append(command)
        assert command[:4] == ["gh", "pr", "view", URL] and command[-1] == runner.CI_VIEW_FIELDS
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(runner, "checked", checked)
    clock = Clock()
    supervisor = job(store, clock)
    supervisor.tick()
    first = store.get(group["id"])["workers"][0]
    assert (first["ci"], first["ci_head"]) == ("passing", SHA)
    clock.now += 30
    supervisor.tick()
    assert len(calls) == 1
    clock.now += 30
    supervisor.tick()
    assert len(calls) == 2 and store.get(group["id"])["workers"][0]["ci"] == "passing"
    clock.now += 60
    supervisor.tick()
    last = store.get(group["id"])["workers"][0]
    assert (last["ci"], last["ci_head"]) == ("failing", H2)


def test_ci_reads_stop_once_the_pr_merges_or_closes(store, monkeypatch):
    group = opened(store)
    monkeypatch.setattr(runner, "checked", lambda *a, **k: ci_view(state="CLOSED"))
    clock = Clock()
    supervisor = job(store, clock)
    supervisor.tick()
    closed = store.get(group["id"])["workers"][0]
    assert closed["pr_state"] == "closed" and closed["closed_seen_at"]
    monkeypatch.setattr(runner, "checked", lambda *a, **k: pytest.fail("a closed PR is not polled"))
    clock.now += 600
    supervisor.tick()
    store.worker_result(group["id"], "lead/s1", pr_state="merged")
    supervisor.tick()


def test_a_batch_group_is_never_polled_for_ci(store, monkeypatch):
    group = store.create(new_group({"label": "Batch", "start_sha": SHA, "workers": [{"branch": "batch/a"}]}, PARENT))
    ready(store, group["id"], "batch/a")
    store.control(group["id"], "seal")
    store.worker_result(group["id"], "batch/a", pr_url=URL, pr_status="open")
    monkeypatch.setattr(runner, "checked", lambda *a, **k: pytest.fail("batch groups read no CI"))
    job(store).tick()
    assert "ci" not in store.get(group["id"])["workers"][0]


def test_a_new_follow_up_head_makes_ci_due_at_once(store, monkeypatch):
    group = opened(store)
    monkeypatch.setattr(runner, "checked", lambda *a, **k: ci_view() if a[0][0] == "gh" else "")
    clock = Clock()
    supervisor = job(store, clock)
    supervisor.tick()
    ready(store, group["id"], "lead/s1", H2)
    monkeypatch.setattr(runner, "follow_up", lambda g, w, m, b: {"pr_head": w["head"], "pr_state": "open", "error": None})
    reads = []
    monkeypatch.setattr(runner, "checked", lambda *a, **k: reads.append(a[0]) or (ci_view(head=H2) if a[0][0] == "gh" else ""))
    clock.now += 5
    supervisor.tick()
    assert [c[:3] for c in reads if c[0] == "gh"] == [["gh", "pr", "view"]]
    assert store.get(group["id"])["workers"][0]["ci_head"] == H2


def test_an_index_older_than_the_closed_reading_does_not_revive_the_pr(store):
    group = opened(store)
    store.worker_result(group["id"], "lead/s1", pr_state="closed", closed_seen_at=2000.0)
    row = {"state": "open", "url": URL, "draft": False, "head_oid": SHA, "base_ref": "main"}
    store.reconcile_prs({"lead/s1": row}, "main", 1999.0)
    assert store.get(group["id"])["workers"][0]["pr_state"] == "closed"
    store.reconcile_prs({"lead/s1": row}, "main", 2001.0)
    assert store.get(group["id"])["workers"][0]["pr_state"] == "open"


def test_summary_counts_an_open_groups_joined_workers(store):
    group = run_group(store)
    store.join(group["id"], {"branch": "lead/s1"})
    store.join(group["id"], {"branch": "lead/s2"})
    view = ready(store, group["id"], "lead/s1")
    assert (view["total"], view["percent"], view["phase"]) == (2, 50, "working")
    assert summary({**view, "workers": []})["phase"] == "working"
