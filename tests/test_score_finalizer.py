"""Focused coverage for the extracted director score finalizer."""

from unittest.mock import MagicMock, patch

from score_finalizer import finalize_score


def test_finalize_score_invokes_teacher_evidence_before_persisting_score():
    store = MagicMock()
    store.get_score.return_value = 10.0
    store.update_score.return_value = 20.0
    store.gate_for_score.return_value = "easy"
    activity = MagicMock()
    result = {
        "status": "success",
        "agent": "coder",
        "domain": "debugging",
        "response": "fixed",
    }
    attach = MagicMock()

    with patch("score_finalizer.get_log", return_value=activity):
        finalized = finalize_score(
            result,
            80.0,
            store=store,
            attach_teacher_evidence=attach,
        )

    attach.assert_called_once_with(result)
    store.update_score.assert_called_once()
    assert finalized["old_score"] == 10.0
    assert finalized["new_score"] == 20.0
    assert finalized["task_score"] == 80.0
    activity.finish_task.assert_called_once()


def test_finalize_score_keeps_blocked_results_unmodified():
    result = {"status": "blocked", "agent": "coder", "domain": "debugging"}
    attach = MagicMock()

    assert finalize_score(result, 80.0, attach_teacher_evidence=attach) is result
    attach.assert_not_called()
