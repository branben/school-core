"""Tests for the poller approval cutover boundary."""

from unittest.mock import patch

from src.agentmail_poller import _execute_approval


def test_missing_coordinator_blocks_approval_without_side_effects():
    reply = {
        "command": "approve", "bead": "bead-1", "candidate_id": "candidate-1",
        "head_sha": "a" * 40, "repository": "example/repo", "message_id": "m-1",
        "from": "human@example.com",
    }
    with patch("src.agentmail_poller.subprocess.run") as run:
        result = _execute_approval(reply, "/tmp/repo")
    assert "coordinator unavailable" in result
    assert "no external write performed" in result
    run.assert_not_called()


def test_missing_repository_scope_blocks_before_coordinator():
    class Coordinator:
        def run(self, **kwargs):
            raise AssertionError("must not delegate")

    reply = {
        "command": "approve", "bead": "bead-1", "candidate_id": "candidate-1",
        "head_sha": "a" * 40, "repository": "example/repo", "message_id": "m-1",
        "from": "human@example.com",
    }
    result = _execute_approval(reply, "/tmp/repo", coordinator=Coordinator())
    assert "repository scope is not configured" in result
    assert "no external write performed" in result
