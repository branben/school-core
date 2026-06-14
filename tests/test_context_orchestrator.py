"""Tests for context_orchestrator.py 4-layer enrichment — U7-2.

Run: python -m pytest tests/test_context_orchestrator.py -v
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from context_orchestrator import (
    enrich_prompt,
    _archival_context,
    _cocoindex_context,
    _engram_context,
    LAYER_3_CHAR_BUDGET,
)


@pytest.fixture
def tmp_vault(tmp_path):
    """Create a minimal vault directory for CocoIndex searches."""
    vault = tmp_path / "vault"
    vault.mkdir()
    return vault


@pytest.fixture
def tmp_consolidation_dir(tmp_path, monkeypatch):
    """Redirect consolidation_writer's dir to temp and set up test files."""
    from consolidation_writer import CONSOLIDATION_DIR as CDIR

    consolidation_dir = tmp_path / "consolidation"
    monkeypatch.setattr("consolidation_writer.CONSOLIDATION_DIR", consolidation_dir)
    return consolidation_dir


@pytest.fixture
def sample_consolidation_yaml(tmp_consolidation_dir):
    """Create a sample consolidation YAML for testing."""
    session_dir = tmp_consolidation_dir / "ses_test_001"
    session_dir.mkdir(parents=True, exist_ok=True)

    import yaml

    data = {
        "session_id": "ses_test_001",
        "domain": "python-testing",
        "timestamp": "2026-06-13T10:00:00+00:00",
        "patterns": [
            "Frequent domain: python-testing (3 occurrences)",
            "Success rate: 2/3 (67%)",
        ],
        "key_learnings": [
            "Used pytest parametrize",
            "Strategy: mock patching",
        ],
        "error_recurrence": {
            "AssertionError": 2,
            "CRITICAL:security_vulnerability": 1,
        },
    }
    with open(session_dir / "python-testing.yaml", "w") as f:
        yaml.dump(data, f, default_flow_style=False)
    return data


class TestArchivalContext:
    def test_layer3_available_included(
        self, tmp_consolidation_dir, sample_consolidation_yaml
    ):
        """When Layer 3 consolidation exists, it's included in context."""
        ctx = _archival_context("python-testing", "ses_test_001")
        assert ctx is not None
        assert "Archival patterns" in ctx
        assert "Frequent domain: python-testing" in ctx
        assert "Used pytest parametrize" in ctx
        assert "AssertionError" in ctx

    def test_layer3_missing_graceful(
        self, tmp_consolidation_dir
    ):
        """When no Layer 3 consolidation exists, returns None gracefully."""
        ctx = _archival_context("python-testing", "nonexistent_session")
        assert ctx is None

    def test_layer3_wrong_domain_falls_back(
        self, tmp_consolidation_dir, sample_consolidation_yaml
    ):
        """When domain doesn't match, falls back to any consolidation."""
        ctx = _archival_context("code-review", "ses_test_001")
        assert ctx is not None
        assert "Archival patterns" in ctx

    def test_context_limit_enforced(
        self, tmp_consolidation_dir
    ):
        """Layer 3 context is truncated to LAYER_3_CHAR_BUDGET."""
        import yaml

        session_dir = tmp_consolidation_dir / "ses_big"
        session_dir.mkdir(parents=True, exist_ok=True)

        # Create patterns that exceed budget
        data = {
            "session_id": "ses_big",
            "domain": "python-testing",
            "timestamp": "2026-06-13T10:00:00+00:00",
            "patterns": ["Pattern " + ("x" * 1000) for _ in range(20)],
            "key_learnings": ["Learning " + ("y" * 1000) for _ in range(20)],
            "error_recurrence": {f"Error {i}": i for i in range(50)},
        }
        with open(session_dir / "python-testing.yaml", "w") as f:
            yaml.dump(data, f, default_flow_style=False)

        ctx = _archival_context("python-testing", "ses_big")
        assert ctx is not None
        assert len(ctx) <= LAYER_3_CHAR_BUDGET + 50  # allow small overhead for truncation msg


class TestEnrichPrompt:
    def test_layer3_included_with_session_id(
        self, tmp_vault, tmp_consolidation_dir, sample_consolidation_yaml
    ):
        """enrich_prompt includes Layer 3 when session_id is provided."""
        with patch("context_orchestrator._cocoindex_context", return_value=None):
            with patch("context_orchestrator._engram_context", return_value=None):
                result = enrich_prompt(
                    domain="python-testing",
                    prompt="Write tests for user service",
                    vault_path=tmp_vault,
                    session_id="ses_test_001",
                )
        assert "Archival patterns" in result

    def test_layer3_missing_graceful_in_enrich(
        self, tmp_vault, tmp_consolidation_dir
    ):
        """enrich_prompt works when no Layer 3 consolidation exists."""
        with patch("context_orchestrator._cocoindex_context", return_value="**CocoIndex result**"):
            result = enrich_prompt(
                domain="python-testing",
                prompt="Write tests",
                vault_path=tmp_vault,
                session_id="no_consolidation",
            )
        # Should still have Layer 1 (CocoIndex) result
        assert "CocoIndex result" in result
        # No archival section
        assert "Archival" not in result

    def test_no_session_id_skips_layer3(self, tmp_vault):
        """Without session_id, Layer 3 is skipped entirely."""
        with patch("context_orchestrator._cocoindex_context", return_value=None):
            with patch("context_orchestrator._engram_context", return_value=None):
                result = enrich_prompt(
                    domain="python-testing",
                    prompt="Write tests",
                    vault_path=tmp_vault,
                )
        assert result == ""

    def test_cocodown_graceful(self, tmp_vault, tmp_consolidation_dir, sample_consolidation_yaml):
        """CocoIndex down: archival context still works."""
        with patch(
            "context_orchestrator._cocoindex_context",
            side_effect=FileNotFoundError("ccc not found"),
        ):
            result = enrich_prompt(
                domain="python-testing",
                prompt="Write tests",
                vault_path=tmp_vault,
                session_id="ses_test_001",
            )
        assert "Archival patterns" in result

    def test_engram_down_graceful(self, tmp_vault, tmp_consolidation_dir, sample_consolidation_yaml):
        """Engram down: archival context still works."""
        with patch("context_orchestrator._engram_context", side_effect=Exception("engram down")):
            result = enrich_prompt(
                domain="python-testing",
                prompt="Write tests",
                vault_path=tmp_vault,
                session_id="ses_test_001",
            )
        assert "Archival patterns" in result

    def test_all_sources_down_returns_empty(self, tmp_vault, tmp_consolidation_dir):
        """All non-blocking: if all sources fail, returns empty string."""
        with patch("context_orchestrator._cocoindex_context", side_effect=Exception("fail")):
            with patch("context_orchestrator._engram_context", side_effect=Exception("fail")):
                with patch("context_orchestrator._archival_context", side_effect=Exception("fail")):
                    result = enrich_prompt(
                        domain="python-testing",
                        prompt="Write tests",
                        vault_path=tmp_vault,
                        session_id="ses_test_001",
                    )
        assert result == ""

    def test_non_blocking_never_crashes(self, tmp_vault):
        """Non-blocking: enrich_prompt never raises, even with everything broken."""
        with patch(
            "context_orchestrator._cocoindex_context",
            side_effect=RuntimeError("broken"),
        ):
            with patch(
                "context_orchestrator._engram_context",
                side_effect=RuntimeError("broken"),
            ):
                with patch(
                    "context_orchestrator._archival_context",
                    side_effect=RuntimeError("broken"),
                ):
                    result = enrich_prompt(
                        domain="python-testing",
                        prompt="Write tests",
                        vault_path=tmp_vault,
                        session_id="ses_broken",
                    )
        assert result == ""


class TestCocoindexContext:
    def test_ccc_not_found(self, tmp_vault):
        """CocoIndex binary not found returns None."""
        with patch("subprocess.run", side_effect=FileNotFoundError):
            ctx = _cocoindex_context("test prompt", tmp_vault, 3)
        assert ctx is None

    def test_ccc_timeout(self, tmp_vault):
        """CocoIndex timeout returns None."""
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ccc", 30)):
            ctx = _cocoindex_context("test prompt", tmp_vault, 3)
        assert ctx is None


class TestEngramContext:
    def test_engram_returns_none_when_unavailable(self):
        """Engram context returns None when Engram is not available."""
        with patch("engram_adapter.engram_available", return_value=False):
            ctx = _engram_context("python-testing", "short prompt", 3)
        assert ctx is None

    def test_engram_no_key_terms(self):
        """Engram context returns None when prompt has no extractable terms."""
        with patch("engram_adapter.engram_available", return_value=True):
            with patch("engram_adapter.search_trajectories", return_value=[]):
                ctx = _engram_context("python-testing", "the a an is are", 3)
        assert ctx is None
