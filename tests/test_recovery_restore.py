"""Tests for recovery.py restore: Beads sync and local durable-state checks.

The fake bd executable models only the CLI boundary; Git targets, service
reachability, reports, and all project paths are real temporary resources.
"""

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CLI = REPO_ROOT / "recovery.py"
REMOTE = "git+https://github.com/example/school-core.git"


@pytest.fixture(scope="module")
def local_service():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=10)


@pytest.fixture
def restore_root(tmp_path, local_service):
    root = tmp_path / "checkout"
    root.mkdir()
    beads = root / ".beads"
    beads.mkdir()
    (beads / "config.yaml").write_text(f'sync.remote: "{REMOTE}"\n')
    (beads / "metadata.json").write_text(json.dumps({
        "backend": "dolt",
        "dolt_mode": "embedded",
        "dolt_database": "school_core",
    }))

    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "remote", "add", "origin", REMOTE[4:]], check=True)

    (root / ".env.example").write_text(
        "OMNIROUTE_API_KEY=placeholder\nGITHUB_TOKEN=placeholder\n"
    )
    data = root / "data"
    (data / "trajectories").mkdir(parents=True)
    (data / "trajectories" / "one.json").write_text(json.dumps({
        "timestamp": "2026-09-01T00:00:00Z", "response": "retained",
    }))
    evidence = data / "recovery" / "smoke-one"
    evidence.mkdir(parents=True)
    (evidence / "report.json").write_text(json.dumps({"command": "smoke", "ok": True}))
    (evidence / "candidate.json").write_text(json.dumps({"candidate_id": "smoke-one"}))
    (evidence / "gate-evidence.json").write_text(json.dumps({"disposition": "current"}))
    import sqlite3
    with sqlite3.connect(evidence / "state.sqlite3") as db:
        db.execute("CREATE TABLE operations (id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO operations VALUES ('smoke-one')")

    return root


def _fake_bd(root: Path, tmp_path: Path, *, mode="success") -> tuple[Path, Path]:
    bin_dir = tmp_path / "fake-bin"
    bin_dir.mkdir()
    executable = bin_dir / "bd"
    executable.write_text("""#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['FAKE_BD_LOG'], 'a') as log:
    log.write(json.dumps(args) + '\\n')
mode = os.environ.get('FAKE_BD_MODE', 'success')
if args == ['bootstrap', '--yes', '--json']:
    db = pathlib.Path(os.environ['FAKE_BD_DB'])
    (db / '.dolt').mkdir(parents=True, exist_ok=True)
    (db / '.dolt' / 'repo_state.json').write_text(json.dumps({'head': 'refs/heads/main', 'remotes': {'origin': {'url': os.environ['FAKE_BD_REMOTE']}}}))
    print('{"action":"sync"}')
    raise SystemExit(0)
if args == ['dolt', 'remote', 'list']:
    print('origin ' + os.environ['FAKE_BD_REMOTE'])
    raise SystemExit(0)
if len(args) == 5 and args[:3] == ['dolt', 'remote', 'add']:
    raise SystemExit(0)
if args == ['list', '--json']:
    if mode == 'corrupt':
        print('not-json')
    elif mode == 'offline':
        print('[{"id":"school-core-test"}]')
    else:
        print('[{"id":"school-core-test"}]')
    raise SystemExit(0)
if args == ['dolt', 'pull', '--remote', 'origin']:
    if mode == 'offline':
        print('remote unavailable', file=sys.stderr)
        raise SystemExit(1)
    print('Pull complete.')
    raise SystemExit(0)
print('unexpected fake bd command', file=sys.stderr)
raise SystemExit(2)
""")
    executable.chmod(0o755)
    log = tmp_path / "bd-calls.jsonl"
    db = root / ".beads" / "embeddeddolt" / "school_core"
    return bin_dir, log


def _run_restore(root: Path, bin_dir: Path, log: Path, tmp_path: Path, service: str, mode="success"):
    env = dict(os.environ)
    env.update({
        "PATH": str(bin_dir) + os.pathsep + env.get("PATH", ""),
        "FAKE_BD_LOG": str(log),
        "FAKE_BD_DB": str(root / ".beads" / "embeddeddolt" / "school_core"),
        "FAKE_BD_REMOTE": REMOTE,
        "FAKE_BD_MODE": mode,
        "OMNIROUTE_API_KEY": "test-api-key",
        "GITHUB_TOKEN": "test-github-token",
        "OMNIROUTE_BASE": service,
    })
    return subprocess.run(
        [sys.executable, str(CLI), "restore", "--root", str(root), "--json"],
        cwd=root, env=env, capture_output=True, text=True, timeout=30,
    )


def _report(result):
    assert result.stdout, result.stderr
    return json.loads(result.stdout)


def _by_name(report):
    return {check["name"]: check for check in report["checks"]}


def _init_local_database(root: Path):
    database = root / ".beads" / "embeddeddolt" / "school_core"
    (database / ".dolt").mkdir(parents=True)
    (database / ".dolt" / "repo_state.json").write_text(json.dumps({
        "head": "refs/heads/main",
        "remotes": {"origin": {"url": REMOTE}},
    }))
    return database


def test_restore_pulls_configured_remote_and_is_idempotent(restore_root, tmp_path, local_service):
    _init_local_database(restore_root)
    bin_dir, log = _fake_bd(restore_root, tmp_path)

    first = _run_restore(restore_root, bin_dir, log, tmp_path, local_service)
    assert first.returncode == 0, first.stdout + first.stderr
    report = _report(first)
    assert _by_name(report)["beads"]["status"] == "ok"
    assert _by_name(report)["trajectories"]["status"] == "ok"
    assert _by_name(report)["evidence"]["status"] == "ok"

    second = _run_restore(restore_root, bin_dir, log, tmp_path, local_service)
    assert second.returncode == 0, second.stdout + second.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert calls.count(["dolt", "pull", "--remote", "origin"]) == 2
    assert (restore_root / "data" / "recovery" / "restore-report.json").is_file()


def test_restore_bootstraps_missing_database_from_configured_remote(restore_root, tmp_path, local_service):
    bin_dir, log = _fake_bd(restore_root, tmp_path)

    result = _run_restore(restore_root, bin_dir, log, tmp_path, local_service)

    assert result.returncode == 0, result.stdout + result.stderr
    calls = [json.loads(line) for line in log.read_text().splitlines()]
    assert ["bootstrap", "--yes", "--json"] in calls
    assert ["dolt", "pull", "--remote", "origin"] in calls
    assert (restore_root / ".beads" / "embeddeddolt" / "school_core" / ".dolt" / "repo_state.json").is_file()


def test_restore_fails_closed_when_remote_is_missing(restore_root, tmp_path, local_service):
    (restore_root / ".beads" / "config.yaml").write_text("# no configured remote\n")
    bin_dir, log = _fake_bd(restore_root, tmp_path)

    result = _run_restore(restore_root, bin_dir, log, tmp_path, local_service)

    assert result.returncode != 0
    assert _by_name(_report(result))["beads"]["status"] == "not_configured"
    assert not log.exists(), "missing remote must not invoke Beads or recreate local state"


def test_restore_does_not_overwrite_corrupt_local_beads_database(restore_root, tmp_path, local_service):
    database = _init_local_database(restore_root)
    (database / ".dolt" / "repo_state.json").write_text("{corrupt")
    bin_dir, log = _fake_bd(restore_root, tmp_path, mode="corrupt")

    result = _run_restore(restore_root, bin_dir, log, tmp_path, local_service, mode="corrupt")

    assert result.returncode != 0
    assert _by_name(_report(result))["beads"]["status"] == "blocked"
    assert not log.exists(), "unparseable Dolt state must fail before invoking bd"


def test_restore_reports_offline_remote_failure_without_leaking_output(restore_root, tmp_path, local_service):
    _init_local_database(restore_root)
    bin_dir, log = _fake_bd(restore_root, tmp_path, mode="offline")

    result = _run_restore(restore_root, bin_dir, log, tmp_path, local_service, mode="offline")

    assert result.returncode != 0
    beads = _by_name(_report(result))["beads"]
    assert beads["status"] == "blocked"
    assert "remote unavailable" not in json.dumps(beads)
