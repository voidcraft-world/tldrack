import uuid
from test_plugin_harbor import plugin  # noqa: F401


def test_routes_register_report_and_gate_publication(plugin, monkeypatch):
    monkeypatch.setattr(plugin._group_runner, "start", lambda: None)

    class Profiles:
        def prepare_lineage(self, parent):
            assert parent == "parent"
            return {"parent_id": str(uuid.uuid4()), "parent_name": parent}

    plugin._context.profiles = Profiles()
    code, result = plugin.handle_post(
        "/group-create",
        {},
        {"parent_session": "parent", "label": "Batch", "start_sha": "a" * 40, "workers": [{"branch": "batch/one"}]},
    )
    assert code == 200
    group_id = result["group"]["id"]
    assert plugin.handle_get("/groups", {})[1]["groups"][0]["percent"] == 0
    code, result = plugin.handle_post(
        "/group-report", {}, {"id": group_id, "branch": "batch/one", "stage": "blocked", "note": "missing data"}
    )
    assert code == 200 and result["group"]["blockers"] == 1
    assert plugin.handle_post("/group-control", {}, {"id": group_id, "action": "pause"})[1]["group"]["auto_pr"] is False
    assert "task_groups" in plugin.handle_get("/state", {})[1]


def test_open_group_grows_by_join_and_a_fixed_one_refuses_with_409(plugin, monkeypatch):
    monkeypatch.setattr(plugin._group_runner, "start", lambda: None)

    class Profiles:
        def prepare_lineage(self, parent):
            return {"parent_id": str(uuid.uuid4()), "parent_name": parent}

    plugin._context.profiles = Profiles()
    code, result = plugin.handle_post(
        "/group-create",
        {},
        {"parent_session": "lead", "label": "Run", "start_sha": "a" * 40, "membership": "open", "release": "each"},
    )
    assert code == 200
    group = result["group"]
    assert (group["total"], group["percent"], group["phase"], group["membership"], group["release"]) == (
        0,
        0,
        "working",
        "open",
        "each",
    )
    code, result = plugin.handle_post("/group-join", {}, {"id": group["id"], "branch": "lead/s1", "start_sha": "b" * 40})
    assert code == 200 and result["group"]["total"] == 1
    assert result["group"]["workers"][0]["start_sha"] == "b" * 40
    assert plugin.handle_post("/group-join", {}, {"id": group["id"], "branch": "bad name"})[0] == 400
    code, fixed = plugin.handle_post(
        "/group-create",
        {},
        {"parent_session": "lead", "label": "Batch", "start_sha": "a" * 40, "workers": [{"branch": "batch/one"}]},
    )
    code, result = plugin.handle_post("/group-join", {}, {"id": fixed["group"]["id"], "branch": "batch/two"})
    assert code == 409 and "fixed" in result["error"]
    assert plugin.handle_post("/group-join", {}, {"id": "missing", "branch": "lead/s2"})[0] == 400
    groups = plugin.handle_get("/groups", {})[1]
    assert groups["capabilities"]["task_group_modes"] is True
    assert [g["total"] for g in groups["groups"]] == [1, 1]
    capabilities = plugin.handle_get("/state", {})[1]["capabilities"]
    assert capabilities["task_group_modes"] is True and capabilities["create_respawn"] is True


def test_invalid_group_never_starts_worker(plugin, monkeypatch):
    monkeypatch.setattr(plugin._group_runner, "start", lambda: (_ for _ in ()).throw(AssertionError("started")))
    assert plugin.handle_post("/group-report", {}, {"id": "missing", "stage": "ready"})[0] == 400
    assert plugin.handle_post("/group-control", {}, {"id": "missing", "action": "resume"})[0] == 400


def test_plugin_reuses_harbor_index_for_manual_merged_group(plugin, monkeypatch):
    from worktree.task_groups import new_group

    group = plugin._groups.create(
        new_group(
            {"label": "Batch", "start_sha": "a" * 40, "workers": [{"branch": "batch/one"}]},
            {"parent_id": str(uuid.uuid4()), "parent_name": "parent"},
        )
    )
    branch = f"pr/{group['id'][:8]}/wt-batch-one/clean"
    plugin._pr_index.replace(
        {
            branch: {
                "state": "merged",
                "head_oid": "b" * 40,
                "base_ref": plugin._base,
                "url": "https://github.com/o/r/pull/25",
                "draft": False,
            }
        },
        1,
    )
    monkeypatch.setattr(plugin, "_maybe_refresh_prs", lambda *args: None)
    plugin._sync_group_prs()
    view = plugin.handle_get("/groups", {})[1]["groups"][0]
    assert (view["percent"], view["opened"], view["phase"]) == (100, 1, "complete")
