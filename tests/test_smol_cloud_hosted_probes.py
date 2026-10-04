"""Opt-in hosted qualification probes for the SmolMachines Cloud substrate.

DANGER: these probes create REAL, BILLABLE cloud machines. They are skipped
unless ``SCHOOL_CORE_HOSTED_PROBES=1`` is set explicitly; nothing here runs in
CI or in a default local test run.

Run recipe (operator-side):

    cd school-core
    # SMOL_CLOUD_TOKEN=smk_... lives in .env (gitignored) — never in chat
    SCHOOL_CORE_HOSTED_PROBES=1 \
    SCHOOL_CORE_SMOL_CLOUD_IMAGE=alpine:3.21@sha256:<64-hex-digest> \
    python3 -m pytest tests/test_smol_cloud_hosted_probes.py -q -s

Evidence map (docs/student-vm-boundary.md, "Qualification evidence required
before production enablement"):

    P0  pre-flight   — spend-cap/plan ceilings read and gated      (run gate)
    P1  item 1       — create -> start -> exec -> bounded export ->
                       clean import -> destroy, identities recorded
    P2  item 4       — forbidden egress is unreachable in the guest
    P3  item 3       — host canaries are invisible to the guest
    P4  item 2       — guest failure blocks; zero host fallback
    P5  item 5       — oversized export is refused guest-side
    P6  item 6       — provider TTL sweep is the cleanup fallback
                       (deliberately leaves one machine for ~60s)
    P7  item 7       — durable evidence bounded and sanitized;
                       no provider (GitHub) write anywhere

Hard bounds: 1 vCPU / 1 GiB / 2 GiB disk / 120 s per task, machines run
sequentially and are deleted after each probe (except P6's TTL sweep).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tarfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from smol_cloud_runner import (
    SmolCloudRunner,
    _UrllibSmolCloudTransport,
    _sanitize_error_body,
    DEFAULT_MAX_SPEND_MICROS as HARD_STOP_MICROS,
)
from student_vm_runner import StudentTaskRequest, StudentVMBlocked

REPO_ROOT = Path(__file__).resolve().parent.parent
# The stop is enforced by the adapter (smol_cloud_runner.DEFAULT_MAX_SPEND_MICROS);
# this alias exists only so the probe's headroom assertion names the same number.

pytestmark = pytest.mark.skipif(
    os.environ.get("SCHOOL_CORE_HOSTED_PROBES") != "1",
    reason="hosted probes create real billable machines; set SCHOOL_CORE_HOSTED_PROBES=1 to run",
)

_DETAIL_KEYS = (
    "id", "state", "ready", "exitCode", "totalMicros", "totalUptimeSeconds",
    "effectiveMaxMachines", "monthlyBudgetMicros", "budgetRemainingMicros",
)
_PLAN_KEYS = ("maxConcurrentMachines", "maxCpus", "maxMemoryMb", "maxDiskGb")
_ENTRY_KEYS = {"method", "path", "status", "request_bytes", "response_bytes",
               "duration_ms", "detail", "error_detail"}


class RecordingTransport:
    """Wraps the real transport and journals redacted metadata only.

    Never records headers (Authorization!) or bodies (task data). Response
    detail is an allowlist of identity/exit/billing fields.
    """

    def __init__(self, inner, journal):
        self._inner = inner
        self.journal = journal

    def request(self, method, path, *, headers, body, timeout_seconds, response_limit_bytes):
        started = time.monotonic()
        response = self._inner.request(
            method, path, headers=headers, body=body,
            timeout_seconds=timeout_seconds, response_limit_bytes=response_limit_bytes,
        )
        detail = {}
        try:
            parsed = json.loads(response.body) if response.body else None
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            for key in _DETAIL_KEYS:
                if key in parsed:
                    detail[key] = parsed[key]
            if isinstance(parsed.get("plan"), dict):
                detail["plan"] = {
                    k: parsed["plan"][k] for k in _PLAN_KEYS if k in parsed["plan"]
                }
        entry = {
            "method": method,
            "path": path,
            "status": response.status,
            "request_bytes": len(body) if body else 0,
            "response_bytes": len(response.body),
            "duration_ms": int((time.monotonic() - started) * 1000),
        }
        if detail:
            entry["detail"] = detail
        if response.status >= 400:
            entry["error_detail"] = _sanitize_error_body(response.body)
        self.journal.append(entry)
        return response


class EvidenceWriter:
    """Rewrites one bounded JSON evidence file after every recorded probe."""

    def __init__(self, directory: Path, *, image: str):
        self.directory = directory
        self.image = image
        self.entries = []
        directory.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.path = directory / f"hosted-probes-{stamp}.json"
        self._flush()

    def record(self, kind: str, payload: dict) -> None:
        self.entries.append({
            "kind": kind,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "payload": payload,
        })
        self._flush()

    def _flush(self) -> None:
        blob = json.dumps(
            {"image": self.image, "entries": self.entries},
            indent=2, sort_keys=True,
        )
        if len(blob) > 256 * 1024:
            raise AssertionError("evidence file exceeded its bounded size")
        self.path.write_text(blob)


@pytest.fixture(autouse=True)
def provider_write_spy(monkeypatch):
    """Evidence item 7: a real provider (GitHub) write must be impossible here."""
    import candidate_pr

    calls = []

    def _blocked(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("qualification probe attempted a real provider write")

    monkeypatch.setattr(candidate_pr, "publish_candidate_pr", _blocked)
    return calls


@pytest.fixture(scope="module")
def hosted():
    if os.environ.get("SCHOOL_CORE_HOSTED_PROBES") != "1":
        pytest.skip("hosted probes are opt-in")
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env", override=False)
    except Exception:
        pass
    token = os.environ.get("SMOL_CLOUD_TOKEN", "")
    if not token.startswith("smk_"):
        pytest.fail(
            "SMOL_CLOUD_TOKEN is missing or malformed. Deliver it without chat: "
            "cd school-core && read -rsp 'New SmolMachines key: ' K && "
            "printf '\\nSMOL_CLOUD_TOKEN=%s\\n' \"$K\" >> .env && unset K"
        )
    image = os.environ.get("SCHOOL_CORE_SMOL_CLOUD_IMAGE", "")
    if not image or "@" not in image:
        pytest.fail(
            "SCHOOL_CORE_SMOL_CLOUD_IMAGE must be a digest-pinned reference "
            "(name@sha256:<64 lowercase hex>); the adapter refuses tags"
        )
    source_type = os.environ.get("SCHOOL_CORE_SMOL_CLOUD_SOURCE_TYPE", "smolmachine")
    if source_type not in ("image", "smolmachine"):
        pytest.fail(
            "SCHOOL_CORE_SMOL_CLOUD_SOURCE_TYPE must be 'image' or 'smolmachine'"
        )
    journal = []
    transport = RecordingTransport(_UrllibSmolCloudTransport(), journal)
    evidence = EvidenceWriter(REPO_ROOT / "data" / "hosted-qualification", image=image)
    return SimpleNamespace(
        token=token, image=image, source_type=source_type, transport=transport,
        journal=journal, evidence=evidence,
    )


def _task_repo(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()

    def _git(*args):
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    _git("init", "-q", "-b", "main")
    _git("config", "user.email", "hosted-probe@example.invalid")
    _git("config", "user.name", "Hosted Probe")
    (repo / "README.md").write_text("trusted base\n")
    _git("add", ".")
    _git("commit", "-qm", "base")
    return repo, _git("rev-parse", "HEAD")


def _request(repo, base_sha, **overrides):
    values = {
        "task_id": "hosted-probe",
        "repository": "example/project",
        "repo_path": repo,
        "base_sha": base_sha,
        "task": {"prompt": "hosted qualification probe"},
        "command": ("true",),
        "timeout_seconds": 120,
        "cpus": 1,
        "memory_mib": 1024,
        "storage_gib": 2,
        "max_output_bytes": 64 * 1024,
        "max_bundle_bytes": 16 * 1024 * 1024,
    }
    values.update(overrides)
    return StudentTaskRequest(**values)


def _runner(hosted, tmp_path, **overrides):
    kwargs = dict(
        image_reference=hosted.image,
        source_type=hosted.source_type,
        api_key=hosted.token,
        transport=hosted.transport,
        quarantine_path=tmp_path / "quarantine.jsonl",
        readiness_timeout_seconds=180,
        max_timeout_seconds=300,
    )
    kwargs.update(overrides)
    return SmolCloudRunner(**kwargs)


def _marker_lines(text, prefix):
    return [line.split(" ", 1)[1] for line in text.splitlines() if line.startswith(prefix)]


# ---------------------------------------------------------------------------
# P0 — pre-flight: spend cap and plan ceilings (run gate, evidence run gate)
# ---------------------------------------------------------------------------


def test_p0_account_budget_and_plan_preflight(hosted):
    response = hosted.transport.request(
        "GET", "/v1/account",
        headers={"Authorization": f"Bearer {hosted.token}", "Accept": "application/json"},
        body=None, timeout_seconds=60, response_limit_bytes=1 << 20,
    )
    assert response.status == 200, f"account pre-flight failed: HTTP {response.status}"
    account = json.loads(response.body)
    snapshot = {k: account[k] for k in _DETAIL_KEYS if k in account}
    # Record the real spend surface this account exposes. The SmolMachines
    # account is prepaid with a soft budget policy: it returns
    # budgetRemainingMicros/monthlyBudgetMicros as null but DOES expose
    # prepaidCreditMicros and lowBalanceThresholdMicros. Gating only on the
    # null budget fields made P0 unpassable on a funded account.
    for key in ("prepaidCreditMicros", "lowBalanceThresholdMicros", "status"):
        if key in account:
            snapshot[key] = account[key]
    if isinstance(account.get("periodCost"), dict):
        snapshot["periodCost"] = account["periodCost"]
    if isinstance(account.get("plan"), dict):
        snapshot["plan"] = {k: account["plan"][k] for k in _PLAN_KEYS if k in account["plan"]}
    hosted.evidence.record("p0_account_preflight", snapshot)

    # Available spend headroom: prefer an explicit monthly budget remaining,
    # otherwise fall back to prepaid credit (the funded balance this account
    # actually draws down). Fail closed only when neither is observable.
    remaining = account.get("budgetRemainingMicros")
    spend_source = "monthlyBudgetRemaining"
    if remaining is None:
        remaining = account.get("prepaidCreditMicros")
        spend_source = "prepaidCredit"
    if remaining is None:
        if os.environ.get("SCHOOL_CORE_HOSTED_ACCEPT_NO_BUDGET") != "1":
            pytest.fail(
                "the account exposes neither budgetRemainingMicros nor "
                "prepaidCreditMicros: set a spend cap in the SmolMachines dashboard "
                "(an explicit spend cap is an operator gate) or re-run with "
                "SCHOOL_CORE_HOSTED_ACCEPT_NO_BUDGET=1 to accept the self-imposed stop"
            )
    else:
        assert remaining >= HARD_STOP_MICROS, (
            f"{spend_source} headroom {remaining} micros is below the "
            f"{HARD_STOP_MICROS} micro stop"
        )


# ---------------------------------------------------------------------------
# P1 — evidence item 1: full lifecycle with recorded identities
# ---------------------------------------------------------------------------


def test_p1_lifecycle_export_import_destroy(hosted, tmp_path):
    repo, base_sha = _task_repo(tmp_path)
    runner = _runner(hosted, tmp_path)

    result = runner.execute(_request(
        repo, base_sha,
        command=("sh", "-c", "printf 'written in guest\\n' > vm-result.txt"),
    ))

    assert result.exit_code == 0
    with tarfile.open(fileobj=io.BytesIO(result.candidate_archive_bytes), mode="r:*") as archive:
        names = archive.getnames()
        # The guest exports with `tar -C /workspace .`, so members are
        # './'-prefixed; normalize before asserting on the candidate tree.
        normalized = {name[2:] if name.startswith("./") else name for name in names}
        assert "vm-result.txt" in normalized
        member = archive.extractfile("./vm-result.txt")
        assert member is not None
        extracted = member.read()
    assert extracted == b"written in guest\n"

    deletes = [e for e in hosted.journal if e["method"] == "DELETE"]
    assert deletes, "no machine delete was attempted"
    assert deletes[-1]["status"] in (200, 204)

    hosted.evidence.record("p1_lifecycle", {
        "image": hosted.image,
        "guest_id": result.guest_id,
        "base_sha": base_sha,
        "bundle_sha256": result.bundle_sha256,
        "archive_sha256": hashlib.sha256(result.candidate_archive_bytes).hexdigest(),
        "candidate_files": names,
    })


# ---------------------------------------------------------------------------
# P2 — evidence item 4: forbidden egress is blocked inside the guest
# ---------------------------------------------------------------------------

_EGRESS_SCRIPT = """
probe_try() {
  name="$1"; shift
  if command -v "$name" >/dev/null 2>&1; then
    if "$@" >/dev/null 2>&1; then
      echo "EGRESS_RESULT $name=reachable"
    else
      echo "EGRESS_RESULT $name=blocked"
    fi
  else
    echo "EGRESS_RESULT $name=absent"
  fi
}
probe_try curl curl -fsS --max-time 6 https://example.com/
probe_try wget wget -q -T 6 -O /dev/null https://example.com/
probe_try ping ping -c 1 -W 5 8.8.8.8
probe_try getent getent hosts example.com
probe_try python3 python3 -c 'import socket; socket.create_connection(("1.1.1.1", 443), 6)'
"""


def test_p2_forbidden_egress_is_blocked(hosted, tmp_path):
    repo, base_sha = _task_repo(tmp_path)
    runner = _runner(hosted, tmp_path)

    result = runner.execute(_request(repo, base_sha, command=("sh", "-c", _EGRESS_SCRIPT)))

    assert result.exit_code == 0, f"egress probe crashed: {result.stderr[-500:]}"
    results = dict(
        line.split("=", 1) for line in _marker_lines(result.stderr, "EGRESS_RESULT ")
    )
    attempts = [name for name, state in results.items() if state in ("blocked", "reachable")]
    reachable = [name for name, state in results.items() if state == "reachable"]

    hosted.evidence.record("p2_egress", {"results": results})
    assert attempts, "image exposes no network tool; the probe is vacuous"
    assert not reachable, f"guest reached forbidden egress: {reachable}"


# ---------------------------------------------------------------------------
# P3 — evidence item 3: host canaries are invisible to the guest
# ---------------------------------------------------------------------------


def test_p3_host_canaries_are_invisible_to_the_guest(hosted, tmp_path):
    secret = uuid.uuid4().hex + "-host-canary-secret"
    canary = tmp_path / f"host-canary-{uuid.uuid4().hex[:8]}"
    canary.write_text(secret)
    host_home = str(Path.home())

    # Each check tests the POSITIVE exposure condition; 'present' means the
    # guest can see a host artifact (bad), 'absent' means isolation holds.
    script = f"""
check() {{
  name="$1"; shift
  if "$@" >/dev/null 2>&1; then
    echo "CANARY_RESULT $name=present"
  else
    echo "CANARY_RESULT $name=absent"
  fi
}}
check host-canary-file test -e {canary}
check host-home-mount test -e {host_home}
check ssh-auth-sock test -n "${{SSH_AUTH_SOCK:-}}"
check orca-docker-socket test -S /var/run/docker.sock
check canary-secret-in-env sh -c 'env | grep -Fq {secret}'
"""
    repo, base_sha = _task_repo(tmp_path)
    runner = _runner(hosted, tmp_path)

    result = runner.execute(_request(repo, base_sha, command=("sh", "-c", script)))

    assert result.exit_code == 0, f"canary probe crashed: {result.stderr[-500:]}"
    results = dict(
        line.split("=", 1) for line in _marker_lines(result.stderr, "CANARY_RESULT ")
    )
    hosted.evidence.record("p3_host_canaries", {"results": results})
    assert set(results) == {
        "host-canary-file", "host-home-mount", "ssh-auth-sock",
        "orca-docker-socket", "canary-secret-in-env",
    }
    exposed = [name for name, state in results.items() if state != "absent"]
    assert not exposed, f"guest can see host artifacts: {exposed}"


# ---------------------------------------------------------------------------
# P4 — evidence item 2: guest failure blocks, zero host fallback
# ---------------------------------------------------------------------------


def test_p4_guest_failure_blocks_with_zero_host_fallback(hosted, tmp_path, monkeypatch):
    repo, base_sha = _task_repo(tmp_path)
    runner = _runner(hosted, tmp_path)

    real_popen = subprocess.Popen
    local_commands = []

    def recording_popen(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args")
        local_commands.append(list(argv))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", recording_popen)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(repo, base_sha, command=("sh", "-c", "exit 3")))

    assert "exit status 3" in str(excinfo.value)
    for argv in local_commands:
        assert argv[0] == "git", f"non-git host execution observed: {argv}"
    deletes = [e for e in hosted.journal if e["method"] == "DELETE"]
    assert deletes, "failed guest did not trigger machine delete"
    hosted.evidence.record("p4_guest_failure", {
        "blocked": str(excinfo.value),
        "host_commands": [" ".join(argv[:3]) for argv in local_commands],
    })


# ---------------------------------------------------------------------------
# P5 — evidence item 5: oversized export is refused guest-side
# ---------------------------------------------------------------------------


def test_p5_oversized_export_is_refused(hosted, tmp_path):
    repo, base_sha = _task_repo(tmp_path)
    runner = _runner(hosted, tmp_path)

    with pytest.raises(StudentVMBlocked) as excinfo:
        runner.execute(_request(
            repo, base_sha,
            command=("sh", "-c", "head -c 200000 /dev/zero > big.bin && echo wrote"),
            max_output_bytes=1024,
        ))

    assert "exit status" in str(excinfo.value)
    execs = [e for e in hosted.journal if e["method"] == "POST" and "/exec" in e["path"]]
    assert execs, "no exec was attempted"
    exit_code = execs[-1].get("detail", {}).get("exitCode")
    assert exit_code is not None and exit_code != 0
    deletes = [e for e in hosted.journal if e["method"] == "DELETE"]
    assert deletes
    hosted.evidence.record("p5_oversized_export", {"blocked": str(excinfo.value)})


# ---------------------------------------------------------------------------
# P6 — evidence item 6: provider TTL sweep is the cleanup fallback
# ---------------------------------------------------------------------------


def test_p6_provider_ttl_sweep_is_the_cleanup_fallback(hosted, tmp_path):
    name = f"sc-ttl-probe-{uuid.uuid4().hex[:10]}"
    payload = {
        "name": name,
        "source": {"type": hosted.source_type, "reference": hosted.image},
        "resources": {"cpus": 1, "memoryMb": 1024, "diskGb": 2},
        "network": {"mode": "blocked"},
        "ttlSeconds": 60,
    }
    headers = {
        "Authorization": f"Bearer {hosted.token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    created = hosted.transport.request(
        "POST", "/v1/machines", headers=headers,
        body=json.dumps(payload).encode(),
        timeout_seconds=120, response_limit_bytes=1 << 20,
    )
    assert created.status == 201, f"ttl probe create failed: HTTP {created.status}"
    machine_id = json.loads(created.body)["id"]

    # Deliberately NO delete: the sweep is the cleanup net that quarantine
    # records rely on. Poll until the provider removes it.
    swept_after = None
    created_at = time.monotonic()
    deadline = created_at + 240
    while time.monotonic() < deadline:
        time.sleep(15)
        state = hosted.transport.request(
            "GET", f"/v1/machines/{machine_id}", headers=headers,
            body=None, timeout_seconds=60, response_limit_bytes=1 << 20,
        )
        if state.status == 404:
            swept_after = int(time.monotonic() - created_at)
            break

    if swept_after is None:
        # Never leave a billable machine behind: clean up, then fail the probe.
        hosted.transport.request(
            "DELETE", f"/v1/machines/{machine_id}?includeUsage=true",
            headers=headers, body=None, timeout_seconds=60, response_limit_bytes=1 << 20,
        )
        pytest.fail("provider TTL sweep did not remove the machine within 240s")

    hosted.evidence.record("p6_ttl_sweep", {
        "machine_id": machine_id,
        "ttl_seconds": 60,
        "swept_after_seconds": swept_after,
    })


# ---------------------------------------------------------------------------
# P7 — evidence item 7: durable evidence bounded, sanitized, no provider write
# ---------------------------------------------------------------------------


def test_p7_durable_evidence_is_bounded_and_sanitized(hosted, provider_write_spy):
    assert provider_write_spy == [], "a provider write was attempted"
    evidence = hosted.evidence
    text = evidence.path.read_text()
    blob = json.loads(text)

    assert len(text) <= 256 * 1024, "evidence file is unbounded"
    assert hosted.token not in text
    assert "smk_" not in text
    assert blob["image"] == hosted.image
    assert blob["entries"], "no probe evidence was recorded"
    for entry in blob["entries"]:
        assert entry["kind"].startswith("p") and entry["recorded_at"]

    # Journal hygiene: only redacted metadata keys were ever recorded.
    for record in hosted.journal:
        assert set(record) <= _ENTRY_KEYS, f"unexpected journal keys: {set(record) - _ENTRY_KEYS}"
        if "detail" in record:
            assert set(record["detail"]) <= set(_DETAIL_KEYS) | {"plan"}

    # Evidence item 7: qualification tests never used a real provider write;
    # the autouse spy proves zero publish_candidate_pr calls this session.
    hosted.evidence.record("p7_evidence_hygiene", {
        "evidence_file": evidence.path.name,
        "evidence_bytes": len(text),
        "journal_entries": len(hosted.journal),
    })
