"""Option-B grading queue + consumer tests.

Covers: durable queue dedup (N2.1), idempotent ledger write (N2.3), lock-safe
ScoreStore update (N5.2), two-judge label resolution, non-fatal label retry
(N7.2), bounded drain (N6.1), and the CLI --drain path.
"""

import json
from pathlib import Path

from school_grader import (
    GradingJob,
    GradingQueue,
    grade,
    drain,
    _two_judge_accept,
)
from resilience import LabelWriteQueue


def _make_store(tmp_path):
    from scoring import ScoreStore

    return ScoreStore(file_path=str(tmp_path / "scores.json"))


def _review_packet(accepted):
    return {
        "schema_version": 1,
        "authority": "director",
        "accepted": accepted,
        "verdict": "ACCEPTED" if accepted else "REJECTED",
        "judges": {
            "cto": {"verdict": "pass", "score": 80},
            "coo": {"verdict": "pass", "score": 75},
        },
    }


# ── N2.1: queue dedup ───────────────────────────────────────────────────────

def test_queue_dedups_by_issue_crew_key(tmp_path):
    q = GradingQueue(tmp_path / "q.jsonl")
    j1 = GradingJob(issue_number=1, crew_id="c1")
    j2 = GradingJob(issue_number=1, crew_id="c1")  # same key
    j3 = GradingJob(issue_number=1, crew_id="c2")  # different crew
    assert q.enqueue(j1) is True
    assert q.enqueue(j2) is False  # dedup
    assert q.enqueue(j3) is True
    assert q.count() == 2


def test_queue_ack_removes_job(tmp_path):
    q = GradingQueue(tmp_path / "q.jsonl")
    q.enqueue(GradingJob(issue_number=5, crew_id="cx"))
    assert q.count() == 1
    q.ack(GradingJob(issue_number=5, crew_id="cx").key)
    assert q.count() == 0


# ── Two-judge label resolution ───────────────────────────────────────────────

def test_two_judge_accept_authoritative():
    acc, label = _two_judge_accept(_review_packet(True), None)
    assert acc is True and label == "school-done"
    rej, label2 = _two_judge_accept(_review_packet(False), None)
    assert rej is False and label2 == "school-failed"


def test_two_judge_accept_unreviewed():
    acc, label = _two_judge_accept(None, None)
    assert acc is None and label == "school-reviewed"


# ── N2.3 / N5.2: idempotent, lock-safe grade ─────────────────────────────────

def test_grade_writes_score_and_labels(tmp_path):
    store = _make_store(tmp_path)
    labels = LabelWriteQueue()
    job = GradingJob(
        issue_number=9, crew_id="crew-9", repo="branben/x", domain="python-coding",
        task_score=82.0, review_packet=_review_packet(True),
    )
    res = grade(job, store=store, label_queue=labels)
    assert res["graded"] is True
    assert res["label"] == "school-done"
    # ledger updated lock-safely via EMA (new = old*0.7 + task*0.3; old=0 -> 24.6)
    import pytest
    assert store.get_score("crew-9", "python-coding") == pytest.approx(82.0 * 0.3)
    # label enqueued (non-fatal retry path)
    assert labels.pending()[0] == ("branben/x", 9, "school-done")


def test_grade_is_idempotent_on_replay(tmp_path):
    store = _make_store(tmp_path)
    job = GradingJob(
        issue_number=9, crew_id="crew-9", repo="branben/x", domain="python-coding",
        task_score=82.0, review_packet=_review_packet(True),
    )
    grade(job, store=store)
    # Replay the same crew_id with a different score -> must NOT apply a second
    # EMA (N2.3 invariant: replayed grade is a no-op).
    replay = GradingJob(
        issue_number=9, crew_id="crew-9", repo="branben/x", domain="python-coding",
        task_score=10.0, review_packet=_review_packet(True),
    )
    res2 = grade(replay, store=store)
    assert res2.get("idempotent_skip") is True
    # score unchanged at the first EMA (24.6), not a blend of 82 and 10
    import pytest
    assert store.get_score("crew-9", "python-coding") == pytest.approx(82.0 * 0.3)


def test_grade_never_raises_on_bad_job(tmp_path):
    store = _make_store(tmp_path)
    # A job whose review_packet is malformed must not poison the drain.
    bad = GradingJob(issue_number=1, crew_id="c", review_packet={"authority": "director"})
    res = grade(bad, store=store)
    assert "error" in res  # captured, not raised


# ── N6.1: bounded drain ──────────────────────────────────────────────────────

def test_drain_processes_all_jobs(tmp_path):
    store = _make_store(tmp_path)
    q = GradingQueue(tmp_path / "q.jsonl")
    for i in range(3):
        q.enqueue(GradingJob(
            issue_number=i, crew_id=f"crew-{i}", domain="python-coding",
            task_score=70.0 + i, review_packet=_review_packet(True),
        ))
    results = drain(q, store=store, max_workers=2)
    assert len(results) == 3
    assert q.count() == 0  # all acked
    import pytest
    assert store.get_score("crew-0", "python-coding") == pytest.approx(70.0 * 0.3)


# ── CLI --drain path ─────────────────────────────────────────────────────────

def test_cli_drain_runs(tmp_path):
    store_path = tmp_path / "scores.json"
    q_path = tmp_path / "q.jsonl"
    q = GradingQueue(q_path)
    q.enqueue(GradingJob(
        issue_number=1, crew_id="crew-1", domain="python-coding",
        task_score=88.0, review_packet=_review_packet(True),
    ))
    from school_grader import main

    rc = main(["--queue-file", str(q_path), "--score-store", str(store_path), "--drain"])
    assert rc == 0
    from scoring import ScoreStore
    st = ScoreStore(file_path=str(store_path))
    import pytest
    assert st.get_score("crew-1", "python-coding") == pytest.approx(88.0 * 0.3)
