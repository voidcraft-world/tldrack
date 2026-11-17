"""`busy_reasons()`: the jobs a bridge restart would kill, as the host's
freshness seam reads them (bridge/tldrack/freshness.py).

Every job here is a thread in the bridge process, so the answer must come from
in-process state — never from `PrStore`, whose `opening` is persisted and so
outlives the worker it describes."""

from test_plugin_harbor import MANAGED, FakeThread, plugin  # noqa: F401 — `plugin` is the fixture

from worktree import pr as pr_mod


def test_an_idle_plugin_is_not_busy(plugin):  # noqa: F811
    assert plugin.busy_reasons() == []


def test_a_create_in_flight_is_busy_and_a_failed_one_is_not(plugin):  # noqa: F811
    plugin._creates.claim("feat-x")
    plugin._creates.claim("feat-y")
    plugin._creates.fail("feat-y", "git worktree add failed")
    assert plugin.busy_reasons() == ["creating worktree feat-x"]


def test_fetch_and_brief_jobs_are_busy_while_running(plugin):  # noqa: F811
    plugin._fetch_jobs.claim()
    plugin._brief_jobs.claim(3)
    assert plugin.busy_reasons() == ["fetching remotes", "writing branch briefs (0/3)"]
    plugin._fetch_jobs.finish()
    plugin._brief_jobs.finish(None)
    assert plugin.busy_reasons() == []


def test_a_pr_is_busy_from_the_claim_until_its_worker_ends(plugin, monkeypatch):  # noqa: F811
    path = f"{MANAGED}/cache-all-pages"
    monkeypatch.setattr(plugin, "_managed", lambda: [{"branch": "feat/x", "path": path}])
    monkeypatch.setattr("worktree.plugin.shutil.which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(pr_mod, "gh_authed", lambda: True)
    monkeypatch.setattr(pr_mod, "has_commits", lambda *_a: True)
    monkeypatch.setattr(pr_mod, "open_pull_request", lambda *_a: ("open", "https://example.test/pr/1", None))

    status, _body = plugin.handle_post("/pr", {}, {"branch": "feat/x"})

    assert status == 202
    assert plugin.busy_reasons() == ["opening PR feat/x"]
    FakeThread.started[-1].run_now()
    assert plugin.busy_reasons() == []


def test_a_persisted_opening_row_from_a_dead_process_is_not_busy(plugin):  # noqa: F811
    plugin._store.set("feat/z", status="opening")
    assert plugin.busy_reasons() == []


def test_a_pr_worker_that_never_starts_is_not_busy(plugin, monkeypatch):  # noqa: F811
    monkeypatch.setattr(plugin, "_managed", lambda: [{"branch": "feat/x", "path": f"{MANAGED}/x"}])
    monkeypatch.setattr("worktree.plugin.shutil.which", lambda _name: "/usr/bin/gh")
    monkeypatch.setattr(pr_mod, "gh_authed", lambda: True)
    monkeypatch.setattr(pr_mod, "has_commits", lambda *_a: True)
    FakeThread.fail_start = True

    status, _body = plugin.handle_post("/pr", {}, {"branch": "feat/x"})

    assert status == 500
    assert plugin.busy_reasons() == []
