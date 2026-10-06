import json
import subprocess
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import HTTPServer

import pytest

import activity_server
import beads_workspace
from activity_server import ActivityHandler


NOW = datetime(2026, 10, 2, 12, 0, tzinfo=timezone.utc)


def issue(issue_id, title, *, status="open", kind="task", parent=None, assignee=None):
    return {
        "id": issue_id,
        "title": title,
        "description": "",
        "status": status,
        "issue_type": kind,
        "priority": 2,
        "parent": parent,
        "assignee": assignee,
        "owner": None,
        "labels": [],
        "created_at": "2026-10-01T12:00:00Z",
        "updated_at": "2026-10-01T12:00:00Z",
        "started_at": None,
    }


def test_workspace_groups_tasks_under_epics_and_keeps_only_unassigned_roots_in_inbox():
    issues = [
        issue("school-core-proj", "Main project", kind="epic"),
        issue("school-core-proj.1", "Nested task", parent="school-core-proj"),
        issue("school-core-proj.2", "Active task", status="in_progress", parent="school-core-proj", assignee="Hermes"),
        issue("school-core-loose", "Inbox task"),
        issue("school-core-owned", "Assigned root", assignee="Freebuff"),
    ]

    workspace = beads_workspace.build_workspace(issues, [], now=NOW)

    assert workspace["projects"][0]["id"] == "school-core-proj"
    assert workspace["projects"][0]["open_count"] == 2
    by_id = {item["id"]: item for item in workspace["issues"]}
    assert by_id["school-core-proj.1"]["project_id"] == "school-core-proj"
    assert by_id["school-core-proj.2"]["project_id"] == "school-core-proj"
    assert [item["id"] for item in workspace["inbox"]] == ["school-core-loose"]
    assert {item["id"] for item in workspace["todos"]} == {
        "school-core-proj.1", "school-core-loose", "school-core-owned"
    }
    assert {item["id"] for item in workspace["in_progress"]} == {"school-core-proj.2"}


def test_owner_email_is_not_mistaken_for_a_task_assignment():
    item = beads_workspace.build_workspace(
        [issue("school-core-abc", "Owner only") | {"owner": "person@example.com"}],
        [],
        now=NOW,
    )["issues"][0]

    assert item["assignee"] is None


def test_live_activity_requires_exact_bead_id_nonterminal_stage_and_fresh_timestamp():
    activity = [
        {
            "type": "student_stage",
            "bead": "school-core-abc",
            "stage": "hermes_thinking",
            "timestamp": (NOW - timedelta(minutes=1)).isoformat(),
        },
        {
            "type": "student_stage",
            "bead": "school-core-abcd-extra",
            "stage": "hermes_thinking",
            "timestamp": (NOW - timedelta(minutes=1)).isoformat(),
        },
        {
            "type": "student_stage",
            "bead": "school-core-abc",
            "stage": "done",
            "timestamp": (NOW - timedelta(seconds=5)).isoformat(),
        },
        {
            "type": "student_stage",
            "bead": "school-core-old",
            "stage": "hermes_thinking",
            "timestamp": (NOW - timedelta(minutes=20)).isoformat(),
        },
    ]

    workspace = beads_workspace.build_workspace(
        [issue("school-core-abc", "Exact match"), issue("school-core-abcd", "Prefix only"), issue("school-core-old", "Stale")],
        activity,
        now=NOW,
    )
    by_id = {item["id"]: item["activity"] for item in workspace["issues"]}

    assert by_id["school-core-abc"]["state"] == "unknown"
    assert by_id["school-core-abc"]["agent"] is None
    assert by_id["school-core-abcd"]["state"] == "unknown"
    assert by_id["school-core-old"]["state"] == "unknown"


def test_finished_event_after_recent_hermes_start_clears_active_signal():
    activity = [
        {
            "type": "student_stage",
            "bead": "school-core-abc",
            "stage": "hermes_thinking",
            "timestamp": (NOW - timedelta(seconds=30)).isoformat(),
        },
        {
            "type": "student_stage",
            "bead": "school-core-abc",
            "stage": "done",
            "timestamp": (NOW - timedelta(seconds=5)).isoformat(),
        },
    ]

    item = beads_workspace.build_workspace(
        [issue("school-core-abc", "Finished")], activity, now=NOW
    )["issues"][0]

    assert item["activity"]["state"] == "unknown"


def test_done_runtime_event_never_counts_as_active_even_if_recent():
    activity = [{
        "type": "student_stage",
        "bead": "school-core-abc",
        "stage": "done",
        "timestamp": (NOW - timedelta(seconds=5)).isoformat(),
    }]

    item = beads_workspace.build_workspace(
        [issue("school-core-abc", "Finished")], activity, now=NOW
    )["issues"][0]

    assert item["activity"]["state"] == "unknown"


def test_fresh_runtime_event_without_exact_bead_id_is_not_attributed():
    item = beads_workspace.build_workspace(
        [issue("school-core-abc", "No join key")],
        [{
            "type": "student_stage",
            "bead": "coder-python-coding-abc12345",
            "stage": "hermes_thinking",
            "timestamp": (NOW - timedelta(seconds=30)).isoformat(),
        }],
        now=NOW,
    )["issues"][0]

    assert item["activity"]["state"] == "unknown"


def test_preview_does_not_mutate_and_confirm_applies_only_allowlisted_bd_args(tmp_path):
    records = [issue("school-core-abc", "Safe task")]
    calls = []

    def runner(args):
        calls.append(list(args))
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps(records), "")
        return subprocess.CompletedProcess(args, 0, "updated", "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    preview = service.preview({
        "action": "assign",
        "issue_id": "school-core-abc",
        "assignee": "Hermes",
    })

    assert calls == [[
        "--sandbox", "list", "--all", "--include-gates", "--limit", "0",
        "--no-pager", "--readonly", "--json",
    ]]
    assert "No agent will be launched" in preview["summary"]

    result = service.confirm(preview["token"])

    assert result["ok"] is True
    assert calls[-1] == ["--sandbox", "update", "school-core-abc", "--assignee", "Hermes"]
    assert all("push" not in call and "dispatch" not in call for call in calls)


def test_create_preview_requires_an_existing_open_epic_and_rechecks_it(tmp_path):
    epic = issue("school-core-proj", "Project", kind="epic")
    calls = []

    def runner(args):
        calls.append(list(args))
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps([epic]), "")
        return subprocess.CompletedProcess(args, 0, "school-core-new", "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    preview = service.preview({
        "action": "create", "title": "Child task", "issue_type": "task", "parent": "school-core-proj"
    })
    assert calls == [["--sandbox", "list", "--all", "--include-gates", "--limit", "0", "--no-pager", "--readonly", "--json"]]
    epic["title"] = "Renamed project"
    with pytest.raises(beads_workspace.WorkspaceConflict, match="project changed"):
        service.confirm(preview["token"])
    assert not any(len(call) > 1 and call[1] == "create" for call in calls)


def test_create_preview_rejects_non_epic_project_before_mutation(tmp_path):
    calls = []

    def runner(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, json.dumps([issue("school-core-parent", "Not a project")]), "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    with pytest.raises(beads_workspace.WorkspaceValidationError, match="open epic"):
        service.preview({"action": "create", "title": "Child", "parent": "school-core-parent"})
    assert len(calls) == 1
    assert not any(len(call) > 1 and call[1] == "create" for call in calls)


def test_claiming_hermes_from_ui_only_changes_beads_assignment(tmp_path):
    record = issue("school-core-abc", "Safe task")
    calls = []

    def runner(args):
        calls.append(list(args))
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps([record]), "")
        return subprocess.CompletedProcess(args, 0, "updated", "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    preview = service.preview({"action": "assign", "issue_id": "school-core-abc", "assignee": "Hermes"})
    assert "No agent will be launched" in preview["summary"]
    service.confirm(preview["token"])

    assert calls[-1] == ["--sandbox", "update", "school-core-abc", "--assignee", "Hermes"]
    assert len(calls) == 3  # read, re-check, one assignment write
    assert not any("dispatch" in value or "crew" in value for call in calls for value in call)


def test_confirmation_rejects_stale_preview_without_running_mutation(tmp_path):
    current = issue("school-core-abc", "Safe task")
    calls = []

    def runner(args):
        calls.append(list(args))
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, json.dumps([current]), "")
        return subprocess.CompletedProcess(args, 0, "updated", "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    preview = service.preview({"action": "status", "issue_id": "school-core-abc", "status": "in_progress"})
    current["status"] = "closed"

    with pytest.raises(beads_workspace.WorkspaceConflict, match="changed"):
        service.confirm(preview["token"])

    assert not any(len(call) > 1 and call[1] in {"update", "close"} for call in calls)


def test_rejects_shellish_ids_and_unapproved_assignees_before_bd_call(tmp_path):
    calls = []

    def runner(args):
        calls.append(list(args))
        return subprocess.CompletedProcess(args, 0, "[]", "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    with pytest.raises(beads_workspace.WorkspaceValidationError):
        service.preview({"action": "assign", "issue_id": "x; rm -rf /", "assignee": "Hermes"})
    assert calls == []
    with pytest.raises(beads_workspace.WorkspaceValidationError):
        service.preview({"action": "assign", "issue_id": "school-core-abc", "assignee": "someone else"})

    assert not any(len(call) > 1 and call[1] in {"update", "close"} for call in calls)


def test_workspace_endpoints_are_local_and_reject_cross_origin_mutations(monkeypatch):
    class FakeService:
        def __init__(self):
            self.confirmed = []

        def workspace(self):
            return {"issues": [], "projects": [], "inbox": [], "todos": [], "in_progress": [], "blocked": [], "closed": []}

        def preview(self, payload):
            return {"token": "one-use-token", "summary": "Assign safely", "args": ["--sandbox", "update", "school-core-abc", "--assignee", "Hermes"], "expires_in": 120}

        def confirm(self, token):
            self.confirmed.append(token)
            return {"ok": True, "message": "Beads updated locally."}

    service = FakeService()
    monkeypatch.setattr(activity_server, "WORKSPACE_SERVICE", service)
    server = HTTPServer(("127.0.0.1", 0), ActivityHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base + "/api/workspace") as response:
            assert json.loads(response.read())["issues"] == []
            assert response.headers.get("Access-Control-Allow-Origin") is None
        with urllib.request.urlopen(f"http://localhost:{server.server_port}/api/workspace") as response:
            assert response.status == 200

        preview_request = urllib.request.Request(
            base + "/api/workspace/preview",
            data=json.dumps({"action": "assign"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(preview_request) as response:
            preview = json.loads(response.read())
            assert preview["command"] == "bd --sandbox update school-core-abc --assignee Hermes"
            assert "args" not in preview

        blocked_request = urllib.request.Request(
            base + "/api/workspace/confirm",
            data=json.dumps({"token": "one-use-token"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "http://attacker.invalid", "Sec-Fetch-Site": "cross-site"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(blocked_request)
        assert error.value.code == 403
        assert service.confirmed == []

        content_type_request = urllib.request.Request(
            base + "/api/workspace/confirm",
            data=b"{\"token\":\"one-use-token\"}",
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(content_type_request)
        assert error.value.code == 415
        assert service.confirmed == []

        confirmed_request = urllib.request.Request(
            base + "/api/workspace/confirm",
            data=json.dumps({"token": "one-use-token"}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
            method="POST",
        )
        with urllib.request.urlopen(confirmed_request) as response:
            assert json.loads(response.read())["ok"] is True
        assert service.confirmed == ["one-use-token"]

        with urllib.request.urlopen(base + "/workspace") as response:
            assert response.status == 200
            assert b"Beads Workspace" in response.read()
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_create_preview_is_non_mutating_until_confirmation(tmp_path):
    calls = []

    def runner(args):
        calls.append(list(args))
        if len(args) > 1 and args[1] == "list":
            return subprocess.CompletedProcess(args, 0, "[]", "")
        return subprocess.CompletedProcess(args, 0, "school-core-new", "")

    service = beads_workspace.WorkspaceService(root=tmp_path, runner=runner)
    preview = service.preview({
        "action": "create",
        "title": "A new task",
        "description": "Short details",
        "issue_type": "task",
    })

    assert calls == []
    assert "Create task: A new task" in preview["summary"]
    service.confirm(preview["token"])
    assert calls[-1][:4] == ["--sandbox", "create", "A new task", "--description"]
    assert calls[-1][4:7] == ["Short details", "--type", "task"]
    assert not any("push" in call or "dispatch" in call for call in calls)
