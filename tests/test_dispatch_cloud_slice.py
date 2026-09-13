"""Integration-ish tests for the U7 dispatch helper, run entirely against a
local HTTP stub (no external network). Verifies the real urllib calls, the
envelope branching, and the label/comment/cloud-start side effects."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"

# Classify through the helper import path (scripts/ on sys.path).
sys.path.insert(0, str(SCRIPTS))
from dispatch_cloud_slice import envelope_verdict, fetch_issue  # noqa: E402


class _Stub(BaseHTTPRequestHandler):
    labels_added = []
    comments = []
    cloud_started = []

    def log_message(self, *args):
        pass

    def _send(self, payload, ctype="application/json", code=200):
        data = payload.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if "/repos/x/y/issues/1" in self.path:
            self._send(json.dumps({"number": 1, "title": "Refactor banner component",
                                   "body": "", "labels": [{"name": "Ready-For-Agent"}]}))
        elif "/repos/x/y/issues/2" in self.path:
            self._send(json.dumps({"number": 2, "title": "Add auth rate limiting",
                                   "body": "", "labels": [{"name": "ready-for-agent"}]}))
        elif "/repos/x/y/issues/99" in self.path:
            self._send(json.dumps({}), code=404)
        else:
            self._send("{}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        if "/app-conversations" in self.path and "/start-tasks" in self.path:
            self._send(json.dumps({"items": [{"id": "start-1", "app_conversation_id": "abc123"}]}))
        elif "/app-conversations" in self.path:
            self._send(json.dumps({"id": "start-1", "app_conversation_id": "abc123"}))
        elif self.path.endswith("/labels"):
            _Stub.labels_added.extend(body.get("labels", []))
            self._send("[]")
        elif self.path.endswith("/comments"):
            _Stub.comments.append(body.get("body", ""))
            self._send("{}")
        else:
            self._send("{}")


@pytest.fixture()  # noqa: F821
def stub_server():
    srv = HTTPServer(("127.0.0.1", 0), _Stub)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    _Stub.labels_added.clear()
    _Stub.comments.clear()
    _Stub.cloud_started.clear()


def _run(base: str, issue: int):
    env = {
        **os.environ,
        "GITHUB_TOKEN": "tok",
        "OPENHANDS_CLOUD_API_KEY": "ohkey",
        "OH_GH_BASE": base,
        "OH_CLOUD_BASE": f"{base}/api/v1",
    }
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "dispatch_cloud_slice.py"),
         "--repo", "x/y", "--issue", str(issue)],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=30)


def test_auto_apply_starts_cloud_conversation(stub_server):
    result = _run(stub_server, 1)
    assert result.returncode == 0
    assert "auto-apply" in result.stdout
    assert any("Auto-apply" in c for c in _Stub.comments)
    assert "https://app.all-hands.dev/conversations/abc123" in " ".join(_Stub.comments)


def test_human_approve_pauses_and_labels(stub_server):
    result = _run(stub_server, 2)
    assert result.returncode == 1
    assert "human-approve" in result.stdout
    assert "human-approve" in _Stub.labels_added
    assert any("Paused for human approval" in c for c in _Stub.comments)


def test_missing_issue_is_input_error(stub_server):
    result = _run(stub_server, 99)
    assert result.returncode == 2
    assert "not found" in result.stderr or "HTTP" in result.stderr


def test_envelope_verdict_reads_labels_and_title():
    auto = envelope_verdict({"title": "Refactor banner", "body": "",
                             "labels": [{"name": "Ready-For-Agent"}]})
    assert auto[0] == "auto-apply"
    pause = envelope_verdict({"title": "Add auth rate limiting", "body": "",
                              "labels": [{"name": "ready-for-agent"}]})
    assert pause[0] == "human-approve"


def test_fetch_issue_hits_configured_base(stub_server, monkeypatch):
    monkeypatch.setenv("OH_GH_BASE", stub_server)
    issue = fetch_issue("x/y", 1, "tok")
    assert issue["title"] == "Refactor banner component"