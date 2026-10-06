#!/usr/bin/env python3
"""Narrow candidate-binding + PR gate + publication retry contract under isolation."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
DATA = REPO / "data"
RUNS = DATA / "last_run.json"

sys.path.insert(0, str(REPO))

BASE = []
if DATA.exists():
    try:
        raw = DATA.read_text()
        if raw.strip():
            obj = json.loads(raw)
            if isinstance(obj, dict):
                BASE = [
                    obj.get("base_repo"),
                    obj.get("base_ref"),
                    obj.get("base_sha"),
                ]
    except Exception:
        pass

if not BASE or not all(BASE):
    print("missing base identity in data/", file=sys.stderr)
    raise SystemExit(2)

base_repo, base_ref, base_sha = BASE
branch = f"school-candidate-{base_repo}-{base_sha[:12]}"
candidate_id = f"{base_repo}-{base_sha[:12]}"
issue_number = int(sys.argv[1]) if len(sys.argv) > 1 else 0
repo_slug = base_repo or "owner/repo"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def init_repo(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test User")
    (root / "README.md").write_text("base\n")
    git(root, "add", "README.md")
    git(root, "commit", "-m", "base")
    git(root, "checkout", "-b", branch)


def make_manifest(store, repo_path):
    from candidate_manifest import create_candidate
    return create_candidate(
        store=store,
        repo_path=repo_path,
        candidate_id=candidate_id,
        bead_id=f"bead-{candidate_id}",
        issue_number=issue_number or 1,
        repository=repo_slug,
        base_ref=base_ref,
        branch=branch,
        owner="student-bound",
    )


def main():
    worktree = REPO / "workspace" / branch
    init_repo(worktree)

    store = DATA / "candidates.json"
    manifest_store = Path(__file__).parent / "candidate_store"  # avoid clash
    manifest_store.mkdir(exist_ok=True)
    manifest = make_manifest(CandidateStore(store), worktree)

    verification = SimpleNamespace(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        passed=True,
        skipped=False,
        failures=(),
    )
    approval = SimpleNamespace(
        candidate_id=manifest.candidate_id,
        head_sha=manifest.head_sha,
        state="approved",
        approval_id=f"teacher-{candidate_id}",
    )

    from candidate_binding import bind_trusted_evidence, CandidateBindingStore, lookup_binding
    binding = bind_trusted_evidence(manifest=manifest, verification=verification, approval=approval)
    binding_store = CandidateBindingStore(DATA / "candidate_bindings.json")
    binding_store.put(binding)

    from candidate_pr_gate import (
        gate_candidate_publication,
        trusted_verification_from_binding,
        teacher_approval_from_binding,
    )
    decision = gate_candidate_publication(
        store=CandidateStore(store),
        manifest=manifest,
        repo_path=manifest.worktree,
        verification=trusted_verification_from_binding(binding),
        approval=teacher_approval_from_binding(binding),
        require_approval=True,
    )
    if not decision.allowed:
        print(f"gate refused: {decision.reason}", file=sys.stderr)
        raise SystemExit(3)

    from pr_provider import PrStateStore, publish_candidate_pr_idempotent
    journal = PrStateStore(DATA / "pr_publications.json")

    class FakePublisher:
        def __init__(self):
            self.calls = []
            self.find_calls = []

        def publish(self, **request):
            self.calls.append(request)
            return {
                "candidate_id": request["candidate_id"],
                "head_sha": request["head_sha"],
                "pr_url": f"https://github.com/{request['repository']}/pull/99",
            }

        def find_pr(self, *, repository, candidate_id, head_sha, branch):
            self.find_calls.append((repository, candidate_id, head_sha, branch))
            return None

    publisher = FakePublisher()
    result = publish_candidate_pr_idempotent(
        repo_path=worktree,
        store=CandidateStore(store),
        manifest=manifest,
        publisher=publisher,
        journal=journal,
        title="Bound candidate PR",
        body="Reviewable PR from bound candidate.",
    )
    print(json.dumps({
        "candidate_id": result.candidate_id,
        "head_sha": result.head_sha,
        "pr_url": result.pr_url,
        "publisher_calls": len(publisher.calls),
        "publisher_find_calls": len(publisher.find_calls),
        "issue_number": issue_number,
        "repo_slug": repo_slug,
        "base_ref": base_ref,
        "base_sha": base_sha,
        "branch": branch,
    }))
    # idempotent replay
    replay = publish_candidate_pr_idempotent(
        repo_path=worktree,
        store=CandidateStore(store),
        manifest=manifest,
        publisher=publisher,
        journal=journal,
        title="Bound candidate PR",
        body="Reviewable PR from bound candidate.",
    )
    print(json.dumps({
        "replay_pr_url": replay.pr_url,
        "replay_calls": len(publisher.calls),
    }))


if __name__ == "__main__":
    main()
