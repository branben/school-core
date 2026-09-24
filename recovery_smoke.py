#!/usr/bin/env python3
"""Disposable clean-device recovery smoke (school-core-zbs.2).

Proves a checkout can perform meaningful work against a real temporary Git
target repository — not status text:

1. target     resolve/inspect the target repo (or create a disposable one);
              requires a clean work tree with at least one commit.
2. candidate  create a real candidate: branch + committed change, captured as
              an immutable CandidateManifest (base/head SHAs, diff digest).
3. verification  execute the target's DECLARED local verification commands
              (project_verify.yaml / discovery) as real subprocesses, bound to
              the exact candidate head via run_candidate_gate.
4. identity   exact candidate/head identity re-validated against live Git.
5. journal    append-only StateJournal record (operation + events) — durable
              local state inspectable after process exit.
6. evidence   candidate.json / gate-evidence.json / report.json artifacts.

Every stage is fail-closed: absent declarations, failing commands, dirty
targets, or identity drift are explicit blockers — never silent success. No
GitHub contact and no Beads mutation happen here; the same path is compatible
with an authorized target repository via --target.

Evidence lands in data/recovery/smoke-<timestamp>/ (or --evidence-dir).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from recovery import Check, Report, _run, _tail

SMOKE_KIND = "recovery-smoke"
BEAD_ID = "school-core-zbs.2"
ISSUE_NUMBER = 2
DEFAULT_REPOSITORY = "local/recovery-smoke"

_SKIP_STAGES = ("candidate", "verification", "identity", "journal")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True, capture_output=True, text=True, timeout=30,
    ).stdout.strip()


def _local_runner(commands: list[dict], default_timeout: int):
    """Execute the target's declared verify commands directly (real
    subprocesses, bounded). Same result shape the candidate gate expects."""

    def runner(repo_path, project_verify=None, flake_path=None, timeout=None):
        timeout = timeout or default_timeout
        failures = []
        for command in commands:
            cwd = (Path(repo_path) / command.get("cwd", ".")).resolve()
            try:
                proc = subprocess.run(
                    ["bash", "-c", command["cmd"]],
                    cwd=str(cwd), capture_output=True, text=True, timeout=timeout,
                )
                rc = proc.returncode
                err = proc.stderr or proc.stdout
            except subprocess.TimeoutExpired:
                rc, err = 124, f"timed out after {timeout}s"
            except OSError as exc:
                rc, err = 126, str(exc)
            if rc != 0:
                failures.append({"cmd": command["cmd"], "exit": rc, "stderr": err[-4096:]})
        return {"passed": not failures, "ran": len(commands), "failures": failures, "skipped": False}

    return runner


def _finalize(report: Report, evidence: Path) -> Report:
    report.artifacts.setdefault("evidence_dir", str(evidence))
    report.artifacts.setdefault("report", str(evidence / "report.json"))
    report.write(evidence / "report.json")
    return report


def _skip(report: Report, reason: str) -> None:
    for name in _SKIP_STAGES:
        report.add(Check(name, "unknown", f"skipped: {reason}"))
    report.add(Check("evidence", "ok", "report written; earlier stages produced no further artifacts"))


def _prepare_target(report: Report, target: str | None, evidence: Path) -> Path | None:
    if target is None:
        try:
            root = Path(tempfile.mkdtemp(prefix="school-smoke-target-"))
            _git(root, "init", "-q", "-b", "main")
            _git(root, "config", "user.email", "recovery-smoke@localhost")
            _git(root, "config", "user.name", "Recovery Smoke")
            (root / "README.md").write_text("Disposable recovery smoke target.\n")
            marker = evidence / "verify-marker.txt"
            cmd = (
                "python3 -c \"from pathlib import Path; "
                f"Path({str(marker)!r}).write_text('ran'); print('verify ok')\""
            )
            (root / "project_verify.yaml").write_text(
                "verify:\n"
                "  - name: smoke-verify\n"
                f"    cmd: {json.dumps(cmd)}\n"
                "    cwd: .\n"
                "  - name: whitespace-check\n"
                "    cmd: \"git diff --check\"\n"
                "    cwd: .\n"
            )
            _git(root, "add", ".")
            _git(root, "commit", "-qm", "smoke target base")
        except (OSError, subprocess.SubprocessError) as exc:
            report.add(Check("target", "blocked", f"cannot create disposable target: {exc}"))
            return None
        report.add(Check("target", "ok", f"disposable target created at {root}"))
        return root

    root = Path(target).resolve()
    report.root = str(root)
    rc, out, err = _run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"], timeout=15)
    if rc != 0 or out.strip() != "true":
        report.add(Check("target", "missing", f"{root} is not a Git work tree",
                         "point --target at an existing Git repository"))
        return None
    rc, _, err = _run(["git", "-C", str(root), "rev-parse", "HEAD"], timeout=15)
    if rc != 0:
        report.add(Check("target", "blocked", f"target has no commits: {_tail(err)}",
                         "create at least one commit in the target repository"))
        return None
    rc, out, _ = _run(["git", "-C", str(root), "status", "--porcelain"], timeout=15)
    if rc == 0 and out.strip():
        count = len(out.strip().splitlines())
        report.add(Check("target", "blocked",
                         f"target work tree is dirty: {count} uncommitted path(s); smoke requires a clean tree",
                         "commit or stash the target's uncommitted changes"))
        return None
    report.add(Check("target", "ok", f"clean target repository at {root}"))
    return root


def run_smoke(
    *, target: str | None = None, evidence_dir: str | Path | None = None,
    repository: str = DEFAULT_REPOSITORY, timeout: int = 120,
) -> Report:
    stamp = _ts()
    evidence = Path(evidence_dir).resolve() if evidence_dir else (
        Path(__file__).resolve().parent / "data" / "recovery" / f"smoke-{stamp}"
    )
    report = Report("smoke", "")
    report.artifacts.update({
        "evidence_dir": str(evidence),
        "report": str(evidence / "report.json"),
        "candidate": str(evidence / "candidate.json"),
        "gate_evidence": str(evidence / "gate-evidence.json"),
        "journal": str(evidence / "state.sqlite3"),
    })

    # 1. target
    root = _prepare_target(report, target, evidence)
    if root is None:
        return _finalize(report, evidence)
    if not report.root:
        report.root = str(root)

    # 2. candidate: real branch + committed change + immutable identity
    from candidate_manifest import CandidateManifestError, CandidateStore, create_candidate

    store = CandidateStore(evidence / "candidates.json")
    branch = f"recovery/smoke-{stamp}"
    try:
        base_ref = _git(root, "branch", "--show-current") or "HEAD"
        _git(root, "checkout", "-q", "-b", branch)
        (root / "smoke-change.txt").write_text(
            f"recovery smoke change {stamp}\ngenerated by recovery.py smoke\n"
        )
        _git(root, "add", "smoke-change.txt")
        _git(root, "commit", "-qm", f"recovery smoke change {stamp}")
        manifest = create_candidate(
            store=store, repo_path=root, candidate_id=f"smoke-{stamp}",
            bead_id=BEAD_ID, issue_number=ISSUE_NUMBER, repository=repository,
            base_ref=base_ref, branch=branch, owner="recovery-smoke",
        )
    except (CandidateManifestError, OSError, subprocess.SubprocessError) as exc:
        report.add(Check("candidate", "blocked", f"candidate initialization failed: {exc}",
                         "ensure the target is a clean repository with one commit"))
        return _finalize(report, evidence)
    report.add(Check("candidate", "ok",
                     f"{manifest.candidate_id} on {branch}: head {manifest.head_sha[:12]} "
                     f"base {manifest.base_sha[:12]} diff {manifest.diff_digest[:12]}"))

    # 3. verification: the target's DECLARED commands, executed for real
    from candidate_gate import GateEvidenceError, run_candidate_gate

    gate = None
    verification_status = "unknown"
    try:
        import verify_gate
        commands = verify_gate._discover_commands(root, None)
    except Exception as exc:  # discovery must fail closed, not crash
        commands = []
        report.add(Check("verification", "blocked", f"verification discovery failed: {exc}"))
    else:
        if not commands:
            verification_status = "missing"
            report.add(Check("verification", "missing",
                             "no declared verification commands in target (add project_verify.yaml)",
                             "declare the target's verify commands in project_verify.yaml"))
        else:
            try:
                gate = run_candidate_gate(
                    repo_path=root, store=store, manifest=manifest,
                    runner=_local_runner(commands, timeout), commands=commands,
                    project_verify=None, flake_path=None, timeout=timeout,
                )
            except GateEvidenceError as exc:
                report.add(Check("verification", "blocked", f"candidate gate refused: {exc}"))
            else:
                failing = "; ".join(
                    f"{c['name']} exit {c['exit']}" for c in gate.checks if c["exit"] != 0
                )
                if gate.disposition == "current":
                    verification_status = "ok"
                    report.add(Check("verification", "ok",
                                     f"{len(gate.checks)} declared command(s) passed at head {manifest.head_sha[:12]}"))
                elif gate.disposition == "failed":
                    verification_status = "blocked"
                    report.add(Check("verification", "blocked",
                                     f"declared verification failed: {failing}",
                                     "fix the failing verify command(s) in the target"))
                else:
                    verification_status = "missing"
                    report.add(Check("verification", "missing", "verification was skipped by the gate runner"))

    # 4. identity: exact candidate/head evidence against live Git
    try:
        current_head = _git(root, "rev-parse", "HEAD")
        validation = store.validate(root, manifest)
        if validation.status == "current" and current_head == manifest.head_sha:
            report.add(Check("identity", "ok",
                             f"{manifest.candidate_id} @ head {manifest.head_sha[:12]} "
                             f"base {manifest.base_sha[:12]} diff {manifest.diff_digest[:12]}"))
        else:
            report.add(Check("identity", "blocked",
                             f"identity check failed: {validation.status} {validation.reason}".strip(),
                             "re-run the smoke flow; the target moved during verification"))
    except Exception as exc:
        report.add(Check("identity", "blocked", f"identity check failed: {exc}"))

    # 5. journal: durable append-only state
    from state_journal import StateJournal

    try:
        journal = StateJournal(evidence / "state.sqlite3")
        operation = journal.start_operation(
            operation_id=f"op-{stamp}", idempotency_key=f"{SMOKE_KIND}:{manifest.candidate_id}",
            candidate_id=manifest.candidate_id, head_sha=manifest.head_sha, kind=SMOKE_KIND,
        )
        journal.record_event(operation.operation_id, "gate", {
            "disposition": gate.disposition if gate else None,
            "checks": [dict(c) for c in gate.checks] if gate else [],
        })
        final_kind = "confirmed" if verification_status == "ok" else "failed"
        journal.record_event(operation.operation_id, final_kind, {
            "head_sha": manifest.head_sha,
            "verification": verification_status,
        })
        report.add(Check("journal", "ok",
                         f"operation {operation.operation_id} recorded ({final_kind}) in {evidence / 'state.sqlite3'}"))
    except Exception as exc:
        report.add(Check("journal", "blocked", f"state journal write failed: {exc}"))

    # 6. evidence artifacts
    try:
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "candidate.json").write_text(json.dumps(asdict(manifest), indent=2) + "\n")
        if gate is not None:
            (evidence / "gate-evidence.json").write_text(json.dumps(asdict(gate), indent=2) + "\n")
        report.add(Check("evidence", "ok",
                         f"candidate identity and verification evidence written under {evidence}"))
    except OSError as exc:
        report.add(Check("evidence", "blocked", f"evidence write failed: {exc}"))

    return _finalize(report, evidence)


__all__ = ["SMOKE_KIND", "run_smoke"]
