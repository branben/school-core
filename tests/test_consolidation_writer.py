"""Tests for consolidation_writer.py — U7-1.

Run: python -m pytest tests/test_consolidation_writer.py -v
"""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from consolidation_writer import (
    write_consolidation,
    load_consolidation_for_domain,
    load_all_consolidation,
    _extract_patterns,
    _extract_key_learnings,
    _count_error_recurrence,
)


@pytest.fixture
def tmp_consolidation_dir(tmp_path, monkeypatch):
    """Redirect CONSOLIDATION_DIR to a temp directory."""
    consolidation_dir = tmp_path / "consolidation"
    monkeypatch.setattr("consolidation_writer.CONSOLIDATION_DIR", consolidation_dir)
    return consolidation_dir


@pytest.fixture
def sample_observations():
    return [
        {
            "domain": "python-testing",
            "status": "success",
            "task_score": 75.0,
            "decision": "Used pytest parametrize",
            "strategy": "parametrize over subTest",
        },
        {
            "domain": "python-testing",
            "status": "success",
            "task_score": 80.0,
        },
        {
            "domain": "python-testing",
            "status": "error",
            "error": "AssertionError: expected 5 got 3",
        },
        {
            "domain": "code-review",
            "status": "success",
            "task_score": 65.0,
            "adversarial_review": {
                "verdict": "FAIL",
                "score": 70.0,
                "findings": [
                    {
                        "section": "test_edge_cases",
                        "issue_class": "missing_edge_case",
                        "severity": "HIGH",
                        "citation": "line 42",
                        "description": "Missing null input test",
                    },
                    {
                        "section": "mock_setup",
                        "issue_class": "logic_error",
                        "severity": "MEDIUM",
                        "citation": "line 15",
                        "description": "Mock not reset between tests",
                    },
                ],
            },
        },
    ]


class TestWriteConsolidation:
    def test_happy_path_creates_yaml(self, tmp_consolidation_dir, sample_observations):
        """Happy path: write_consolidation creates a valid YAML file."""
        result = write_consolidation(
            session_id="ses_test_001",
            domain="python-testing",
            observations=sample_observations[:3],
        )
        assert result is not None
        assert result.exists()
        assert result.name == "python-testing.yaml"

        with open(result) as f:
            data = yaml.safe_load(f)
        assert data["session_id"] == "ses_test_001"
        assert data["domain"] == "python-testing"
        assert "timestamp" in data
        assert isinstance(data["patterns"], list)
        assert isinstance(data["key_learnings"], list)
        assert isinstance(data["error_recurrence"], dict)

    def test_empty_observations_no_engram_returns_none(self, tmp_consolidation_dir):
        """Empty observations + no trajectory files = nothing to consolidate, returns None."""
        with patch("trajectory.list_trajectories", return_value=[]):
            result = write_consolidation(
                session_id="ses_empty_001",
                domain="python-testing",
                observations=[],
            )
        assert result is None

    def test_empty_observations_with_engram_fetches(self, tmp_consolidation_dir):
        """Empty observations but trajectory files exist: fetches from filesystem."""
        mock_results = [
            {"domain": "python-testing", "status": "success", "task_score": 75.0},
        ]
        with patch("trajectory.list_trajectories", return_value=mock_results):
            result = write_consolidation(
                session_id="ses_fetch_001",
                domain="python-testing",
                observations=[],
            )
        assert result is not None
        with open(result) as f:
            data = yaml.safe_load(f)
        assert data["domain"] == "python-testing"

    def test_no_engram_no_observations_returns_none(self, tmp_consolidation_dir):
        """When no trajectory files exist and no observations, returns None."""
        with patch("trajectory.list_trajectories", return_value=[]):
            result = write_consolidation(
                session_id="ses_none_001",
                domain="python-testing",
                observations=[],
            )
        assert result is None

    def test_existing_file_overwritten(self, tmp_consolidation_dir, sample_observations):
        """Writing to an existing session/domain overwrites the file."""
        write_consolidation("ses_overwrite", "python-testing", sample_observations[:1])
        write_consolidation("ses_overwrite", "python-testing", sample_observations)

        result = load_consolidation_for_domain("ses_overwrite", "python-testing")
        assert result is not None
        # Should have patterns from the second write
        assert len(result["patterns"]) > 0

    def test_multiple_domains_separate_files(self, tmp_consolidation_dir, sample_observations):
        """Different domains produce separate YAML files."""
        write_consolidation("ses_multi", "python-testing", sample_observations[:3])
        write_consolidation("ses_multi", "code-review", sample_observations[3:])

        pt = load_consolidation_for_domain("ses_multi", "python-testing")
        cr = load_consolidation_for_domain("ses_multi", "code-review")
        assert pt is not None
        assert cr is not None
        assert pt["domain"] == "python-testing"
        assert cr["domain"] == "code-review"

    def test_failure_returns_none(self, tmp_consolidation_dir):
        """Non-blocking: exceptions return None, never crash."""
        with patch("consolidation_writer._ensure_dir", side_effect=IOError("disk full")):
            result = write_consolidation("ses_fail", "python-testing", [{"domain": "x"}])
        assert result is None


class TestLoadConsolidation:
    def test_load_existing(self, tmp_consolidation_dir, sample_observations):
        """Load an existing consolidation YAML."""
        write_consolidation("ses_load", "python-testing", sample_observations[:2])
        data = load_consolidation_for_domain("ses_load", "python-testing")
        assert data is not None
        assert data["session_id"] == "ses_load"

    def test_load_missing_returns_none(self, tmp_consolidation_dir):
        """Loading a missing consolidation returns None."""
        data = load_consolidation_for_domain("nonexistent", "python-testing")
        assert data is None

    def test_load_all_for_session(self, tmp_consolidation_dir, sample_observations):
        """Load all consolidation YAMLs for a session."""
        write_consolidation("ses_all", "python-testing", sample_observations[:3])
        write_consolidation("ses_all", "code-review", sample_observations[3:])
        all_data = load_all_consolidation("ses_all")
        assert len(all_data) == 2
        domains = {d["domain"] for d in all_data}
        assert "python-testing" in domains
        assert "code-review" in domains

    def test_load_all_missing_session(self, tmp_consolidation_dir):
        """Loading all for a nonexistent session returns empty list."""
        assert load_all_consolidation("nonexistent") == []


class TestExtractPatterns:
    def test_recurring_domain_pattern(self):
        obs = [
            {"domain": "python-testing", "status": "success"},
            {"domain": "python-testing", "status": "success"},
            {"domain": "python-testing", "status": "error"},
        ]
        patterns = _extract_patterns(obs)
        assert any("Frequent domain: python-testing" in p for p in patterns)

    def test_success_rate_pattern(self):
        obs = [
            {"domain": "x", "status": "success"},
            {"domain": "x", "status": "success"},
            {"domain": "x", "status": "error"},
        ]
        patterns = _extract_patterns(obs)
        assert any("Success rate: 2/3" in p for p in patterns)

    def test_score_average_pattern(self):
        obs = [
            {"domain": "x", "status": "success", "task_score": 70.0},
            {"domain": "x", "status": "success", "task_score": 90.0},
        ]
        patterns = _extract_patterns(obs)
        assert any("Average score: 80.0" in p for p in patterns)

    def test_adversarial_findings_pattern(self):
        obs = [
            {
                "domain": "x",
                "status": "success",
                "adversarial_review": {
                    "findings": [
                        {"issue_class": "missing_edge_case", "severity": "HIGH"},
                        {"issue_class": "missing_edge_case", "severity": "MEDIUM"},
                    ],
                },
            },
        ]
        patterns = _extract_patterns(obs)
        assert any("Adversarial: missing_edge_case" in p for p in patterns)

    def test_empty_observations(self):
        assert _extract_patterns([]) == []


class TestExtractKeyLearnings:
    def test_decisions_extracted(self):
        obs = [
            {"domain": "x", "status": "success", "decision": "Used pytest parametrize"},
        ]
        learnings = _extract_key_learnings(obs)
        assert "Used pytest parametrize" in learnings

    def test_strategies_extracted(self):
        obs = [
            {"domain": "x", "status": "success", "strategy": "mock patching"},
        ]
        learnings = _extract_key_learnings(obs)
        assert "Strategy: mock patching" in learnings

    def test_grounded_score_extracted(self):
        obs = [
            {
                "domain": "x",
                "status": "success",
                "grounded_score": {
                    "components": {"correctness": 85.0, "completeness": 70.0},
                },
            },
        ]
        learnings = _extract_key_learnings(obs)
        assert any("Strongest dimension: correctness" in l for l in learnings)

    def test_empty_observations(self):
        assert _extract_key_learnings([]) == []


class TestCountErrorRecurrence:
    def test_error_counts(self):
        obs = [
            {"domain": "x", "status": "error", "error": "AssertionError"},
            {"domain": "x", "status": "error", "error": "AssertionError"},
            {"domain": "x", "status": "error", "error": "TypeError"},
        ]
        errors = _count_error_recurrence(obs)
        assert errors["AssertionError"] == 2
        assert errors["TypeError"] == 1

    def test_adversarial_high_severity_counted(self):
        obs = [
            {
                "domain": "x",
                "status": "success",
                "adversarial_review": {
                    "findings": [
                        {"issue_class": "security_vulnerability", "severity": "CRITICAL"},
                        {"issue_class": "logic_error", "severity": "HIGH"},
                    ],
                },
            },
        ]
        errors = _count_error_recurrence(obs)
        assert "CRITICAL:security_vulnerability" in errors
        assert "HIGH:logic_error" in errors

    def test_empty_observations(self):
        assert _count_error_recurrence([]) == {}
