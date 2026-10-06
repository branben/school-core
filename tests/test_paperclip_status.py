import json
from io import BytesIO
from urllib.parse import parse_qs, urlsplit
from urllib.error import URLError

import paperclip_status
from paperclip_status import PaperclipStatusMirror


class FakeResponse(BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


def _json_response(value, status=200):
    response = FakeResponse(json.dumps(value).encode())
    response.status = status
    return response


COMPANY_ID = "00000000-0000-4000-8000-000000000001"
PROJECT_ID = "00000000-0000-4000-8000-000000000002"


class FakePaperclip:
    def __init__(self):
        self.issues = []
        self.calls = []
        self.fail_after_create = False

    def open(self, request, timeout):
        method = request.get_method()
        parsed = urlsplit(request.full_url)
        path = parsed.path
        body = json.loads(request.data) if request.data else None
        self.calls.append((method, path, parse_qs(parsed.query), body, timeout, request.get_header("Authorization")))
        if method == "GET" and path.endswith("/issues"):
            marker = parse_qs(parsed.query).get("q", [""])[0]
            return _json_response([issue for issue in self.issues if marker in issue["title"]])
        if method == "POST" and path.endswith("/issues"):
            issue = {"id": f"paperclip-{len(self.issues) + 1}", **body}
            self.issues.append(issue)
            if self.fail_after_create:
                self.fail_after_create = False
                raise URLError("response dropped after create")
            return _json_response(issue, 201)
        if method == "GET" and "/api/issues/" in path:
            issue_id = path.rsplit("/", 1)[-1]
            issue = next(issue for issue in self.issues if issue["id"] == issue_id)
            return _json_response(issue)
        if method == "PATCH" and "/api/issues/" in path:
            issue_id = path.rsplit("/", 1)[-1]
            issue = next(issue for issue in self.issues if issue["id"] == issue_id)
            issue.update(body)
            return _json_response(issue)
        raise AssertionError(f"Unexpected Paperclip request: {method} {path}")


def _mirror(fake, *, enabled="1"):
    mirror = PaperclipStatusMirror(
        api_url="http://127.0.0.1:3100",
        company_id=COMPANY_ID,
        project_id=PROJECT_ID,
        api_key="paperclip-test-key",
        enabled=enabled,
    )
    mirror._open = fake.open
    return mirror


def _result(status="success", **extra):
    return {"issue_number": 42, "status": status, **extra}


def test_mirror_is_disabled_unless_explicitly_enabled(monkeypatch):
    monkeypatch.delenv("SCHOOL_CORE_PAPERCLIP_MIRROR", raising=False)
    monkeypatch.setenv("PAPERCLIP_API_URL", "http://127.0.0.1:3100")
    monkeypatch.setenv("PAPERCLIP_COMPANY_ID", COMPANY_ID)
    monkeypatch.setenv("PAPERCLIP_PROJECT_ID", PROJECT_ID)
    monkeypatch.setattr(paperclip_status.urllib.request, "build_opener", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("network must stay off")))

    assert paperclip_status.mirror_paperclip_status("owner/repo", _result()) == "disabled"


def test_create_mirrors_only_minimal_identity_and_final_review_state():
    fake = FakePaperclip()
    outcome = _mirror(fake).sync_result("owner/repo", _result(
        "success",
        title="Do not copy this private issue title",
        pr_url="https://github.com/owner/repo/pull/8",
        response="Do not copy this agent response",
    ))

    assert outcome == "created"
    assert len(fake.issues) == 1
    issue = fake.issues[0]
    assert issue["title"] == "School-core status: owner/repo#42"
    assert issue["status"] == "in_review"
    assert issue["projectId"] == PROJECT_ID
    assert issue.get("assigneeAgentId") is None
    assert "github.com/owner/repo/issues/42" in issue["description"]
    assert "github.com/owner/repo/pull/8" in issue["description"]
    assert "private issue title" not in json.dumps(issue)
    assert "agent response" not in json.dumps(issue)
    assert all(call[4] <= 3 for call in fake.calls)
    assert all(call[5] == "Bearer paperclip-test-key" for call in fake.calls)


def test_retry_finds_existing_task_and_updates_it_without_duplicate():
    fake = FakePaperclip()
    fake.fail_after_create = True
    mirror = _mirror(fake)

    assert mirror.sync_result("owner/repo", _result("retry")) == "reconciled"
    assert len(fake.issues) == 1
    assert fake.issues[0]["status"] == "blocked"

    assert mirror.sync_result("owner/repo", _result(
        "success", pr_url="https://github.com/owner/repo/pull/8",
    )) == "updated"
    assert len(fake.issues) == 1
    assert fake.issues[0]["status"] == "in_review"
    assert sum(call[0] == "POST" for call in fake.calls) == 1
    assert sum(call[0] == "PATCH" for call in fake.calls) == 2


def test_failed_outcome_is_blocked_and_never_sends_raw_error():
    fake = FakePaperclip()
    mirror = _mirror(fake)

    assert mirror.sync_result("owner/repo", _result(
        "error", error="private prompt text, token=secret-value",
    )) == "created"
    issue = fake.issues[0]
    assert issue["status"] == "blocked"
    assert "private prompt text" not in json.dumps(issue)
    assert "secret-value" not in json.dumps(issue)


def test_non_github_or_malformed_repository_and_issue_results_are_ignored():
    fake = FakePaperclip()
    mirror = _mirror(fake)

    assert mirror.sync_result("owner/../repo", _result()) == "invalid_source"
    assert mirror.sync_result("owner/repo", {"issue_number": 0, "status": "success"}) == "invalid_source"
    assert mirror.sync_result("owner/repo", _result(status=["success"])) == "invalid_source"
    assert fake.calls == []


def test_mirror_refuses_to_update_matching_issue_in_another_project():
    fake = FakePaperclip()
    mirror = _mirror(fake)
    result = _result()
    title = "School-core status: owner/repo#42"
    marker = "[school-core-source:owner/repo#42]"
    fake.issues.append({
        "id": "existing-mirror-id",
        "title": title,
        "description": marker,
        "status": "todo",
        "projectId": "00000000-0000-4000-8000-000000000099",
    })

    assert mirror.sync_result("owner/repo", result) == "failed"
    assert len(fake.issues) == 1
    assert not any(call[0] in {"POST", "PATCH"} for call in fake.calls)


def test_untrusted_pr_url_is_not_included_in_mirror():
    fake = FakePaperclip()
    mirror = _mirror(fake)

    assert mirror.sync_result("owner/repo", _result(
        "success", pr_url="https://attacker.invalid/steal",
    )) == "created"
    assert "attacker.invalid" not in fake.issues[0]["description"]


def test_api_failure_is_best_effort_and_does_not_escape():
    mirror = PaperclipStatusMirror(
        api_url="https://paperclip.example",
        company_id=COMPANY_ID,
        project_id=PROJECT_ID,
        api_key=None,
        enabled="true",
    )
    mirror._open = lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("secret should not be logged"))

    assert mirror.sync_result("owner/repo", _result()) == "failed"


def test_cleartext_remote_api_url_is_rejected():
    fake = FakePaperclip()
    mirror = PaperclipStatusMirror(
        api_url="http://paperclip.example",
        company_id=COMPANY_ID,
        project_id=PROJECT_ID,
        api_key=None,
        enabled="1",
    )
    mirror._open = fake.open

    assert mirror.sync_result("owner/repo", _result()) == "failed"
    assert fake.calls == []
