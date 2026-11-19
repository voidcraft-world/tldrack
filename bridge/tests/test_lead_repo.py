"""A LEAD run names its repository (rack_db.py V4, lead/service.py `_resolve_repo`): every worktree-plugin call
of the run carries it, workers branch from THAT repository's origin/main, and a run with none behaves as before."""

import sqlite3

from lead_fakes import H1, MAIN, build, ok, publish, repo_row, worker_appears

from tldrack import rack_db

A_MAIN = "d" * 40
B_MAIN = "e" * 40


def two_repos(fake) -> None:
    fake.worktree.repos = [repo_row("alpha-1111", "/srv/alpha", default=True), repo_row("beta-2222", "/srv/beta")]
    fake.git.mains = {"/srv/alpha": A_MAIN, "/srv/beta": B_MAIN}


def launch(service) -> str:
    return ok(service.handle("launch", {"profile": "auto"}, None))["session"]


def start(service, lead, **extra) -> tuple[int, dict]:
    return service.handle("start", {"title": "T", "plan_text": "# plan", **extra}, lead)


def run_one_step(service, fake, lead, run_id) -> None:
    """Spawn S1, let its worker appear, report ready, publish, tick, close: every plugin route a run uses."""
    ok(service.handle("step", {"id": "S1", "title": "s", "brief": "b"}, lead))
    ok(service.handle("spawn", {"step": "S1"}, lead))
    worker = worker_appears(service, fake, lead, "S1", run_id)
    ok(service.handle("progress", {"stage": "implement"}, worker))
    ok(service.handle("ready", {"head": H1, "title": "t", "body": "b"}, worker))
    publish(fake, service.runs.step(run_id, "S1")["branch"])
    service.tick()
    ok(service.handle("status", {}, lead))
    service.state_view()
    ok(service.handle("close", {"step": "S1"}, lead))


def main_reads(fake) -> set[str]:
    """Every directory origin/main was fetched or read in (the close's `ls-remote` runs in the worker's tree)."""
    return {a[2] for a in fake.git.calls if a[:2] == ["git", "-C"] and (a[3] == "fetch" or "refs/remotes/origin/main" in a)}


def run_calls(fake) -> list[tuple[str, str, str | None]]:
    """The run's plugin calls: `/repos` is the one read that names no repository by design."""
    return [c for c in fake.worktree.calls if c[1] != "/repos"]


def test_a_run_started_with_a_repo_sends_it_on_every_call_and_branches_from_its_main(tmp_path):
    service, fake, _ = build(tmp_path)
    two_repos(fake)
    lead = launch(service)
    body = ok(start(service, lead, repo="beta-2222"))
    assert body["repo"] == "beta-2222" and service.runs.run(body["run"])["repo"] == "beta-2222"
    run_one_step(service, fake, lead, body["run"])
    calls = run_calls(fake)
    routes = {path for _method, path, _repo in calls}
    assert {"/state", "/groups", "/group-create", "/group-join", "/create", "/group-report"} <= routes
    assert all(repo == "beta-2222" for _m, _p, repo in calls), [c for c in calls if c[2] != "beta-2222"]
    assert fake.worktree.creates[-1]["base"] == B_MAIN
    assert fake.worktree.worker(service.runs.step(body["run"], "S1")["branch"])["start_sha"] == B_MAIN
    assert main_reads(fake) == {"/srv/beta"}, "origin/main is read from the run's repository, never the start dir"
    assert "repo: beta-2222 (beta-2222) · /srv/beta" in ok(service.handle("status", {}, lead))["text"]


def test_a_path_inside_a_repository_names_it(tmp_path):
    service, fake, _ = build(tmp_path)
    two_repos(fake)
    lead = launch(service)
    assert ok(start(service, lead, repo="/srv/beta/bridge/tldrack"))["repo"] == "beta-2222"


def test_a_path_in_a_worktree_elsewhere_is_asked_of_git(tmp_path):
    service, fake, _ = build(tmp_path)
    two_repos(fake)
    real = fake.git

    def git(argv):
        if "--git-common-dir" in argv and argv[2] == "/elsewhere/beta-wt":
            real.calls.append(list(argv))
            return 0, "/srv/beta/.git\n"
        return real(argv)

    service.bridge.git = git
    lead = launch(service)
    assert ok(start(service, lead, repo="/elsewhere/beta-wt"))["repo"] == "beta-2222"


def test_the_default_is_the_repository_the_lead_works_in_else_the_first(tmp_path):
    service, fake, _ = build(tmp_path)
    two_repos(fake)
    lead = launch(service)
    row = next(r for r in fake.rows if r["session"] == lead)
    row["cwd"] = "/srv/beta/ui"
    assert ok(start(service, lead))["repo"] == "beta-2222"

    service, fake, _ = build(tmp_path / "second")
    two_repos(fake)
    lead = launch(service)
    next(r for r in fake.rows if r["session"] == lead)["cwd"] = "/var/scratch"
    assert ok(start(service, lead))["repo"] == "alpha-1111"


def test_an_unknown_repo_is_refused_at_start_with_the_settings_key(tmp_path):
    service, fake, _ = build(tmp_path)
    two_repos(fake)
    lead = launch(service)
    for wanted in ("gamma-3333", "/srv/gamma", "relative/path", ""):
        status, body = start(service, lead, repo=wanted)
        assert status == 400, (wanted, body)
    status, body = start(service, lead, repo="gamma-3333")
    assert "plugins.worktree.repos" in body["error"] and "alpha-1111" in body["error"]
    assert service.runs.runs() == [], "a refused start leaves no run"


def test_a_repo_is_refused_when_the_plugin_names_none(tmp_path):
    service, fake, _ = build(tmp_path)
    fake.worktree.repos = None  # a plugin from before /repos
    lead = launch(service)
    status, body = start(service, lead, repo="beta-2222")
    assert status == 503 and "restart the bridge" in body["error"]


def test_a_run_with_no_repo_behaves_as_before(tmp_path):
    """A plugin from before /repos: the run stores none, sends none, and branches from the start dir's main."""
    service, fake, _ = build(tmp_path)
    fake.worktree.repos = None
    lead = launch(service)
    body = ok(start(service, lead))
    assert body["repo"] is None
    run_one_step(service, fake, lead, body["run"])
    assert all(repo is None for _m, _p, repo in run_calls(fake))
    assert fake.worktree.creates[-1]["base"] == MAIN
    assert main_reads(fake) == {"/repo"}
    assert "the worktree plugin's first repository" in ok(service.handle("status", {}, lead))["text"]


def test_a_run_stored_before_v4_reads_as_the_first_repository(tmp_path):
    """An open run whose row has no repo (it started before V4) sends none and branches from the first repository."""
    service, fake, _ = build(tmp_path)
    two_repos(fake)
    lead = launch(service)
    run_id = ok(start(service, lead))["run"]
    with service.runs.db.write() as conn:
        conn.execute("UPDATE lead_runs SET repo = NULL WHERE id = ?", (run_id,))
    run_one_step(service, fake, lead, run_id)
    assert all(repo is None for _m, _p, repo in run_calls(fake))
    assert fake.worktree.creates[-1]["base"] == A_MAIN and main_reads(fake) == {"/srv/alpha"}
    assert "(the first repository: this run names none)" in ok(service.handle("status", {}, lead))["text"]


def test_a_v3_rack_db_gains_the_column_and_keeps_its_runs(tmp_path):
    path = tmp_path / "rack.sqlite"
    conn = sqlite3.connect(path, isolation_level=None)
    for script in rack_db.MIGRATIONS[:3]:
        conn.executescript(script)
    conn.execute("PRAGMA user_version = 3")
    conn.execute(
        "INSERT INTO lead_runs (id, title, lead_identity, parallel_max, spawn_cap, state, created_at)"
        " VALUES ('R0001-abcd', 't', 'x', 2, 4, 'open', 0)"
    )
    conn.close()
    with rack_db.RackDb(path).read() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == rack_db.SCHEMA_VERSION
        assert dict(conn.execute("SELECT id, repo FROM lead_runs").fetchone()) == {"id": "R0001-abcd", "repo": None}
