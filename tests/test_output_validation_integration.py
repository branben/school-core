"""Integration test: output validation hook fires in TaskRunner.run_task path.

Proves that when call_model returns invalid output, the hook short-circuits
BEFORE _run_two_judge_review and _persist_acceptance, returning status=error
with output_format_valid=False.
"""

import pytest
from unittest.mock import patch, MagicMock
from director import run_task, _validate_role_output
from scoring import ScoreStore


class TestOutputValidationHookIntegration:
    """Hook fires during run_task, not just as a unit."""

    def _make_store(self):
        """Return a real ScoreStore so routing doesn't crash."""
        store = ScoreStore()
        # Pre-populate so route_task finds qualified roles
        store.set_score("coder", "code-implementation", 50.0)
        store.set_score("searcher", "code-search", 50.0)
        store.set_score("reviewer", "code-review", 50.0)
        return store

    def test_coder_bad_output_short_circuits_before_review(self):
        """Coder returns 'cat README.md' → hook fires → no review called."""
        with patch('director.call_model') as mock_model, \
             patch('director._run_two_judge_review') as mock_review, \
             patch('director.write_bookbag') as mock_bookbag:

            mock_model.return_value = "cat README.md"

            result = run_task(
                prompt="Add a README",
                domain="code-implementation",
                difficulty="easy",
                force_agent="coder",
                store=self._make_store(),
            )

            # Hook should have rejected — review NEVER called
            mock_review.assert_not_called()
            mock_bookbag.assert_not_called()

            assert result["status"] == "error"
            assert result["output_format_valid"] is False
            assert "output_format_invalid" in result["error"]

    def test_coder_valid_output_proceeds_to_review(self):
        """Coder returns valid fenced code → review IS called."""
        with patch('director.call_model') as mock_model, \
             patch('director._run_two_judge_review') as mock_review, \
             patch('director.write_bookbag'):

            mock_model.return_value = "```python\n# hello.txt\nHello\n```"
            mock_review.return_value = {
                "accepted": True,
                "cto_verdict": "PASS",
                "coo_verdict": "PASS",
                "cto_score": 80,
                "coo_score": 80,
                "combined_score": 80.0,
                "findings": [],
            }

            result = run_task(
                prompt="Write hello.txt",
                domain="code-implementation",
                difficulty="easy",
                force_agent="coder",
                store=self._make_store(),
            )

            # Review SHOULD have been called
            mock_review.assert_called_once()
            assert result["status"] == "success"

    def test_searcher_bad_output_short_circuits(self):
        """Searcher returns prose → hook fires → no review."""
        with patch('director.call_model') as mock_model, \
             patch('director._run_two_judge_review') as mock_review:

            mock_model.return_value = "I think you should search for TODO comments"

            result = run_task(
                prompt="Find TODO comments",
                domain="code-search",
                difficulty="easy",
                force_agent="searcher",
                store=self._make_store(),
            )

            mock_review.assert_not_called()
            assert result["status"] == "error"
            assert result["output_format_valid"] is False

    def test_reviewer_bad_output_short_circuits(self):
        """Reviewer returns empty → hook fires → no review."""
        with patch('director.call_model') as mock_model, \
             patch('director._run_two_judge_review') as mock_review:

            mock_model.return_value = ""

            result = run_task(
                prompt="Review this code",
                domain="code-review",
                difficulty="easy",
                force_agent="reviewer",
                store=self._make_store(),
            )

            mock_review.assert_not_called()
            assert result["status"] == "error"
            assert result["output_format_valid"] is False
