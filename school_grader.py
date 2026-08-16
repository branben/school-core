"""Option-B grading department: durable queue + consumer.

Decouples grading from dispatch. The dispatch office (school_scheduler, next
slice) enqueues a ``GradingJob`` when a crew/task finishes; a grader consumer
drains the queue and runs the *finalization* that is safe to defer and that
benefits from queue semantics:

  - two-judge acceptance decision (reuses ReviewPacket, never re-gates the
    clean base — N5.3 invariant preserved),
  - idempotent, lock-safe ScoreStore write (N5.2 / N2.3),
  - compound_learning institutional-memory observation,
  - GitHub label via the non-fatal LabelWriteQueue (N7.2).

The consumer is intentionally small and dependency-light (file-backed queue,
no broker) to match the rest of the repo. At cap=1 the loop drains the queue
in-process at end of cycle (behavior identical to today); at 20+ the drain
moves to a separate pipeline stage without touching dispatch.

Resilience wiring (see resilience.py / docs/school-core-worst-day-ever.md):
  N2.1  GradingJob dedup key = grading_dedup_key(issue_number, crew_id)
  N2.3  idempotent ledger write keyed by crew_id (replay = no-op)
  N6.1  bounded concurrent consumers via bounded_grader_pool_size
  N7.2  label writes go through LabelWriteQueue (non-fatal retry)
"""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Callable, Optional

from resilience import (
    grading_dedup_key,
    grader_score_key,
    bounded_grader_pool_size,
    LabelWriteQueue,
)


# Default queue location (sits next to the crew registry / scores under data/).
DEFAULT_QUEUE_FILE = Path(__file__).resolve().parent / "data" / "grading_queue.jsonl"


@dataclass
class GradingJob:
    """A finished crew/task awaiting finalization.

    Immutable-ish payload; the dedup key is derived from (issue_number,
    crew_id) so a re-delivered or re-dispatched job is a no-op at enqueue.
    """

    issue_number: Optional[int] = None
    crew_id: Optional[str] = None
    repo: str = ""
    domain: str = ""
    difficulty: str = ""
    task_score: Optional[float] = None
    review_packet: Optional[dict] = None  # ReviewPacket.to_dict() shape
    canonical_review: Optional[dict] = None  # legacy/async review dict
    status: str = "success"  # success | error | retry

    @property
    def key(self) -> str:
        return grading_dedup_key(self.issue_number, self.crew_id)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "GradingJob":
        return cls(
            issue_number=d.get("issue_number"),
            crew_id=d.get("crew_id"),
            repo=d.get("repo", ""),
            domain=d.get("domain", ""),
            difficulty=d.get("difficulty", ""),
            task_score=d.get("task_score"),
            review_packet=d.get("review_packet"),
            canonical_review=d.get("canonical_review"),
            status=d.get("status", "success"),
        )


class GradingQueue:
    """File-backed durable queue (JSONL). Safe for concurrent enqueues from a
    fleet of dispatchers: each enqueue/ack takes a short fcntl lock on the
    queue file. Dedup is by job key (N2.1)."""

    def __init__(self, queue_file: Path = DEFAULT_QUEUE_FILE) -> None:
        self.queue_file = Path(queue_file)
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.queue_file.exists():
            self.queue_file.write_text("")  # lazy; load() treats empty as []

    def _lock(self):
        return open(self.queue_file, "a+")

    def _load(self) -> list[dict]:
        if not self.queue_file.exists() or self.queue_file.read_text().strip() == "":
            return []
        out: list[dict] = []
        for line in self.queue_file.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # N5.1-style corruption guard: skip a torn line rather than
                # fail the whole queue.
                continue
        return out

    def _save(self, jobs: list[dict]) -> None:
        import tempfile
        import os

        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=self.queue_file.parent,
            prefix=f".{self.queue_file.name}.", suffix=".tmp", delete=False,
        ) as tmp:
            for job in jobs:
                tmp.write(json.dumps(job) + "\n")
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, self.queue_file)

    def enqueue(self, job: GradingJob) -> bool:
        """Append a job. Returns False if an identical key is already queued
        (dedup, N2.1). Thread/process-safe via fcntl."""
        new = job.to_dict()
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                jobs = self._load()
                if any(j.get("key") == job.key for j in jobs):
                    return False
                new["key"] = job.key
                jobs.append(new)
                self._save(jobs)
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def pending(self) -> list[GradingJob]:
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return [GradingJob.from_dict(j) for j in self._load()]
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def count(self) -> int:
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                return len(self._load())
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def ack(self, key: str) -> None:
        """Remove a completed job by key (N2.3: a replayed key simply isn't
        present, so re-acking is harmless)."""
        with self._lock() as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                jobs = [j for j in self._load() if j.get("key") != key]
                self._save(jobs)
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _two_judge_accept(
    review_packet: Optional[dict],
    canonical_review: Optional[dict],
) -> tuple[Optional[bool], str]:
    """Resolve the two-judge acceptance verdict for a finished job.

    Mirrors the bridge's gate (issue_bridge ~1365-1385): an authoritative
    ReviewPacket governs (packet.accepted); otherwise a legacy/async review
    dict with cto/coo verdicts governs; otherwise the job is "reviewed but
    undecided" (e.g. async skip_review fixtures).

    Returns (accepted: Optional[bool], label: str) where label is one of
    school-done / school-failed / school-reviewed.
    """
    packet = None
    if review_packet:
        try:
            from review_packet import ReviewPacket
            packet = ReviewPacket.from_dict(review_packet)
        except Exception:
            packet = None

    review = dict(canonical_review or {})
    if packet is not None and getattr(packet, "is_authoritative", False):
        review["accepted"] = packet.accepted
    reviewed = (packet is not None and getattr(packet, "is_authoritative", False)) or bool(
        review.get("cto_verdict") or review.get("coo_verdict")
    )
    if not reviewed:
        return (None, "school-reviewed")
    accepted = review.get("accepted")
    if accepted is True:
        return (True, "school-done")
    if accepted is False:
        return (False, "school-failed")
    # Reviewed but no explicit accepted flag (legacy shape): treat as done and
    # let the human triage label stand; never auto-fail a reviewed job.
    return (None, "school-reviewed")


def grade(
    job: GradingJob,
    *,
    store,
    compound_store=None,
    label_queue: Optional[LabelWriteQueue] = None,
    apply_label: Optional[Callable[[str, int, str], None]] = None,
) -> dict:
    """Consumer finalization for one job (N5.2 / N2.3 / N7.2).

    - idempotent: if crew_id already scored in the ledger, skip the EMA (N2.3).
    - lock-safe ledger write via store.update_score (N5.2).
    - compound_learning observation (fail-soft).
    - GitHub label via LabelWriteQueue (non-fatal retry, N7.2).

    Never raises: any failure is captured and returned in ``error`` so a bad
    job doesn't poison the queue drain.
    """
    result: dict = {
        "issue_number": job.issue_number,
        "crew_id": job.crew_id,
        "key": job.key,
        "graded": False,
        "label": None,
        "error": None,
    }
    try:
        # N2.3: idempotent — a replayed grade is a no-op, not a second EMA.
        if job.crew_id is not None and store.get_score(str(job.crew_id), job.domain or "_default") not in (0.0,):
            # A real prior score means this crew was already graded.
            prior = store.get_score(str(job.crew_id), job.domain or "_default")
            if prior and prior != 0.0:
                result["graded"] = False
                result["idempotent_skip"] = True
                result["label"] = "school-reviewed"
                if label_queue is not None and job.crew_id is not None:
                    label_queue.enqueue(job.repo, job.issue_number or 0, result["label"])
                return result

        accepted, label = _two_judge_accept(job.review_packet, job.canonical_review)
        result["accepted"] = accepted
        result["label"] = label

        # Lock-safe ledger write (N5.2). crew_id is the student/agent key.
        if job.crew_id is not None and job.task_score is not None:
            store.update_score(
                str(job.crew_id), job.domain or "_default", float(job.task_score)
            )
            result["graded"] = True

        # Institutional memory (fail-soft).
        if compound_store is not None and hasattr(compound_store, "observe"):
            try:
                compound_store.observe(
                    bead_id=str(job.issue_number),
                    trigger="crew_graded",
                    evidence={
                        "crew_id": str(job.crew_id),
                        "domain": job.domain,
                        "score": job.task_score,
                        "accepted": accepted,
                        "label": label,
                    },
                )
            except Exception as e:
                result["error"] = f"compound_learning: {e}"

        # GitHub label via non-fatal retry queue (N7.2).
        if label_queue is not None and job.crew_id is not None:
            label_queue.enqueue(job.repo, job.issue_number or 0, label)
        elif apply_label is not None:
            try:
                apply_label(job.repo, job.issue_number or 0, label)
            except Exception as e:
                result["error"] = f"label: {e}"
    except Exception as e:  # never poison the drain
        result["error"] = str(e)
    return result


def drain(
    queue: GradingQueue,
    *,
    store,
    compound_store=None,
    label_queue: Optional[LabelWriteQueue] = None,
    apply_label: Optional[Callable[[str, int, str], None]] = None,
    max_workers: int = 1,
) -> list[dict]:
    """Process all pending jobs. ``max_workers`` is bounded by
    bounded_grader_pool_size (N6.1) so ledger writes never exceed what the
    lock-safe store can absorb. Returns the per-job results."""
    jobs = queue.pending()
    workers = bounded_grader_pool_size(
        desired=max(1, max_workers), fleet_capacity=max(1, max_workers)
    )
    results: list[dict] = []
    local = threading.Lock()

    def _run(job: GradingJob) -> None:
        res = grade(
            job,
            store=store,
            compound_store=compound_store,
            label_queue=label_queue,
            apply_label=apply_label,
        )
        queue.ack(job.key)
        with local:
            results.append(res)

    # Drain in bounded waves: each wave runs at most `workers` jobs concurrently
    # (N6.1 — never exceed what the lock-safe ledger can absorb), then reads the
    # next wave. A single call empties the queue without ever bursting past the
    # concurrency cap.
    while True:
        wave = queue.pending()[:workers]
        if not wave:
            break
        if workers <= 1:
            for job in wave:
                _run(job)
        else:
            threads = [threading.Thread(target=_run, args=(job,)) for job in wave]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
    return results


def _build_cli():
    p = argparse.ArgumentParser(prog="school_grader", description="Option-B grading queue consumer.")
    p.add_argument("--queue-file", default=str(DEFAULT_QUEUE_FILE))
    p.add_argument("--score-store", default=None, help="Path to scores.json (defaults to ScoreStore default).")
    p.add_argument("--compound-store", default=None, help="Path to compound_learning store.")
    p.add_argument("--max-workers", type=int, default=1)
    p.add_argument("--drain", action="store_true", help="Drain all pending grading jobs and exit.")
    return p


def main(argv=None) -> int:
    args = _build_cli().parse_args(argv)
    queue = GradingQueue(Path(args.queue_file))
    if not args.drain:
        print(f"queue has {queue.count()} pending job(s); pass --drain to process", file=sys.stderr)
        return 0
    from scoring import ScoreStore
    store = ScoreStore(file_path=args.score_store) if args.score_store else ScoreStore()
    compound_store = None
    if args.compound_store:
        try:
            from compound_learning import CompoundLearningStore
            compound_store = CompoundLearningStore(args.compound_store)
        except Exception as e:
            print(f"[school_grader] compound store unavailable: {e}", file=sys.stderr)
    label_queue = LabelWriteQueue()
    results = drain(
        queue,
        store=store,
        compound_store=compound_store,
        label_queue=label_queue,
        max_workers=args.max_workers,
    )
    for r in results:
        print(f"graded #{r.get('issue_number')} crew={r.get('crew_id')} "
              f"label={r.get('label')} error={r.get('error')}")
    # Flush any labels that couldn't be applied immediately.
    if label_queue.pending():
        print(f"[school_grader] {len(label_queue.pending())} label write(s) queued for retry", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
