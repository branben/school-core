"""Fail-closed publication guard in ``create_pr_for_issue``.

``create_pr_for_issue`` is the PR-creation boundary and already receives the full
evidence chain (``review_evidence``, ``verify_result``, ``entire_review``), but it
only *renders* the acceptance verdict into the PR body
(``src/bridge/pr_creator.py:353-358``) — it still opens the PR when
``accepted is False``.

The bridge gates this at ``issue_bridge.py:2427`` (``_review.get("accepted") is
False`` → continue), and ``review_evidence`` is that same dict
(``issue_bridge.py:3020``), so the default sync path was already covered. This
guard makes the boundary self-defending for any *direct* caller:
``create_prs_for_results`` (currently dead), the module CLI
(``pr_creator.py:720``), or a future worker.

Scope matches the bridge guard exactly — CRITICAL and HIGH both refuse
(``issue_bridge.py:2698``, ``conductor._persist_acceptance``). MEDIUM/LOW stay
advisory.

Test roles, stated honestly:

* ``test_accepted_false_refuses_to_publish`` and
  ``test_critical_entire_finding_refuses_to_publish`` are **RED → GREEN**: new
  behaviour. They fail before the guard and pass after.
* ``test_high_entire_finding_refuses_to_publish`` is **RED → GREEN** too: it
  asserted the old advisory contract and now asserts the veto.
* ``test_medium_entire_finding_still_publishes`` and
  ``test_accepted_true_still_publishes`` are **regression guards** — they pass
  before AND after, proving the widened gate did not swallow clean reviews.
"""

from unittest.mock import patch

from src.bridge.pr_creator import create_pr_for_issue


def _issue() -> dict:
    return {
        "issue_number": 1,
        "title": "Test",
        "domain": "debugging",
        "difficulty": "easy",
    }


def _task() -> dict:
    return {"response": "print('hello')", "agent": "foundry-coder-7b"}


class TestPublicationGuard:
    @patch("src.bridge.pr_creator._gh_api")
    @patch("src.bridge.pr_creator._gh")
    def test_accepted_false_refuses_to_publish(self, mock_gh, mock_api, capsys):
        """RED → GREEN. A rejected review must not produce a PR URL.

        Uses ``dry_run=True`` so the test cannot touch the network; the guard
        must fire BEFORE the dry-run short-circuit, which is what makes this
        assertion discriminating rather than trivially satisfied.
        """
        url = create_pr_for_issue(
            _issue(),
            _task(),
            "user/test",
            review_evidence={
                "accepted": False,
                "cto_verdict": "FAIL",
                "coo_verdict": "FAIL",
            },
            dry_run=True,
        )
        assert url is None
        mock_gh.assert_not_called()
        mock_api.assert_not_called()
        assert "refusing to open a PR" in capsys.readouterr().err

    @patch("src.bridge.pr_creator._gh_api")
    @patch("src.bridge.pr_creator._gh")
    def test_critical_entire_finding_refuses_to_publish(self, mock_gh, mock_api):
        """RED → GREEN. A CRITICAL entire-sensor finding is an automatic veto."""
        url = create_pr_for_issue(
            _issue(),
            _task(),
            "user/test",
            entire_review={
                "status": "fail",
                "findings": [
                    {
                        "severity": "CRITICAL",
                        "file": "src/app/main.py",
                        "line": 42,
                        "message": "hardcoded secret",
                    }
                ],
            },
            dry_run=True,
        )
        assert url is None
        mock_gh.assert_not_called()
        mock_api.assert_not_called()

    @patch("src.bridge.pr_creator._gh_api")
    @patch("src.bridge.pr_creator._gh")
    def test_high_entire_finding_refuses_to_publish(self, mock_gh, mock_api):
        """RED → GREEN. A HIGH entire-sensor finding is an automatic veto."""
        url = create_pr_for_issue(
            _issue(),
            _task(),
            "user/test",
            entire_review={
                "status": "fail",
                "findings": [
                    {"severity": "HIGH", "file": "a.py", "line": 1, "message": "meh"}
                ],
            },
            dry_run=True,
        )
        assert url is None
        mock_gh.assert_not_called()
        mock_api.assert_not_called()

    @patch("src.bridge.pr_creator._gh_api")
    @patch("src.bridge.pr_creator._gh")
    def test_medium_entire_finding_still_publishes(self, mock_gh, mock_api):
        """Regression guard: MEDIUM alone is advisory, not a veto."""
        url = create_pr_for_issue(
            _issue(),
            _task(),
            "user/test",
            entire_review={
                "status": "warn",
                "findings": [
                    {"severity": "MEDIUM", "file": "a.py", "line": 1, "message": "meh"}
                ],
            },
            dry_run=True,
        )
        assert url == "https://github.com/user/test/pull/0"

    @patch("src.bridge.pr_creator._gh_api")
    @patch("src.bridge.pr_creator._gh")
    def test_accepted_true_still_publishes(self, mock_gh, mock_api):
        """Regression guard: an accepted review must still reach publication."""
        url = create_pr_for_issue(
            _issue(),
            _task(),
            "user/test",
            review_evidence={
                "accepted": True,
                "cto_verdict": "PASS",
                "coo_verdict": "PASS",
            },
            dry_run=True,
        )
        assert url == "https://github.com/user/test/pull/0"

    @patch("src.bridge.pr_creator._gh_api")
    @patch("src.bridge.pr_creator._gh")
    def test_citation_form_is_reported(self, mock_gh, mock_api, capsys):
        """The entire veto rewrites findings with ``citation``, not file/line."""
        url = create_pr_for_issue(
            _issue(),
            _task(),
            "user/test",
            entire_review={
                "status": "fail",
                "findings": [
                    {"severity": "CRITICAL", "citation": "src/app/main.py:42"}
                ],
            },
            dry_run=True,
        )
        assert url is None
        assert "src/app/main.py:42" in capsys.readouterr().err
