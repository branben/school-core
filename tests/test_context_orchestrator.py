"""Tests for context_orchestrator.py 4-layer enrichment — U7-2.

Run: python -m pytest tests/test_context_orchestrator.py -v
"""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from context_orchestrator import (
    _archival_context,
    _cocoindex_context,
    _engram_context,
    _extract_symbol_names,
    enrich_prompt,
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


class TestExtractSymbolNames:
    """Unit tests for _extract_symbol_names — Serena LSP symbol extraction."""

    # ── Pattern-specific tests ───────────────────────────────────

    def test_upper_camel_case(self):
        """UpperCamelCase: UserService, CreateRoom, HttpHandler."""
        result = _extract_symbol_names(
            "Refactor the UserService and CreateRoom classes"
        )
        assert "UserService" in result
        assert "CreateRoom" in result

    def test_lower_camel_case(self):
        """lowerCamelCase: createRoom, handleAuth, getUserById."""
        result = _extract_symbol_names(
            "Fix createRoom and handleAuth methods"
        )
        assert "createRoom" in result
        assert "handleAuth" in result

    def test_snake_case(self):
        """snake_case: handle_auth, run_task, create_user."""
        result = _extract_symbol_names(
            "Update the handle_auth_callback and run_task functions"
        )
        assert "handle_auth_callback" in result
        assert "run_task" in result

    def test_all_caps(self):
        """ALL_CAPS: API, URL, HTTP, JSON."""
        result = _extract_symbol_names(
            "Add CORS headers and JSON parsing to the API endpoint"
        )
        assert "CORS" in result
        assert "JSON" in result

    def test_backtick_quoted(self):
        """Backtick-quoted identifiers: `myFunction`, `some_var`."""
        result = _extract_symbol_names(
            "Fix the `createRoom` function and update `handle_auth`"
        )
        assert "createRoom" in result
        assert "handle_auth" in result

    # ── Behaviour tests ──────────────────────────────────────────

    def test_mixed_patterns(self):
        """All patterns together in one prompt."""
        result = _extract_symbol_names(
            "Refactor `UserService` — fix create_user helper, "
            "rename ApiClient to HTTPClient, update handleAuth"
        )
        # Should capture a mix of patterns
        assert len(result) >= 2

    def test_deduplication_case_insensitive(self):
        """Same name in different cases deduplicates (case-insensitive)."""
        result = _extract_symbol_names("Use UserService and userService together")
        # Only one variant kept (first seen)
        names_lower = [n.lower() for n in result]
        assert names_lower.count("userservice") == 1

    def test_short_identifiers_filtered(self):
        """Identifiers shorter than 3 chars are excluded."""
        result = _extract_symbol_names("Use AB and CDE constants")
        # "AB" matches ALL_CAPS regex but is < 3 chars → filtered.
        # "CDE" matches ALL_CAPS and is ≥ 3 chars → kept.
        assert "AB" not in result
        assert "CDE" in result

    def test_max_five_limit(self):
        """Never returns more than 5 identifiers."""
        result = _extract_symbol_names(
            "Fix UserService CreateRoom handleAuth run_task "
            "ApiClient HTTPClient JSONParser OAuthProvider "
            "RequestHandler ResponseBuilder"
        )
        assert len(result) <= 5

    def test_empty_prompt(self):
        """Empty or symbol-free prompt returns empty list."""
        result = _extract_symbol_names("the a an is are was were")
        assert result == []

    def test_no_symbols_plain_text(self):
        """Plain text without identifiers returns empty list."""
        result = _extract_symbol_names("please fix the bug in the code")
        assert result == []

    def test_backtick_captures_non_identifier_names(self):
        """Backtick-quoted names are extracted even if no regex matches."""
        # "myFunc" DOES match lowerCamelCase regex, so use a name that
        # genuinely can't be caught by any regex pattern — a single
        # lowercase word with no camel/snake/ALL_CAPS structure.
        result = _extract_symbol_names("Call the `helper` function")
        assert "helper" in result


class TestEnrichPromptThreeLayerIntegration:
    """Integration test: enrich_prompt with all three enrichment layers active.

    Verifies that when CocoIndex (Layer 0), Serena (Layer 1), and Engram
    (Layer 2) all return data, the combined context blob contains sections
    from all three layers, with correct header, ordering, and graceful
    degradation when individual layers fail or return empty.
    """

    # ── Mock return fixtures ────────────────────────────────────

    COCO_CONTEXT = (
        "**Relevant files from vault:**\n"
        "- `scoring.py` (relevance: 0.66)\n"
        "  ```\n  class ScoreStore:\n      \"\"\"Persistent scoring for agents.\"\"\"\n  ```\n"
        "- `director.py` (relevance: 0.58)\n"
        "  ```\n  def run_task(prompt, domain, difficulty):\n  ```"
    )

    SERENA_CONTEXT = (
        "**Exact symbol locations (Serena LSP):**\n"
        "- `ScoreStore` (Class) → `scoring.py:116`\n"
        "- `run_task` (Function) → `director.py:467`"
    )

    ENGRAM_CONTEXT = (
        "**Past similar trajectories:**\n"
        "- [2026-07-28T10:00:00] **student-coder** (score=72.5)\n"
        "  > Fixed the run_task function to handle ScoreStore properly"
    )

    def test_all_three_layers_combined(self, tmp_path):
        """All three layers contribute to a single combined context blob."""
        with patch(
            "context_orchestrator._cocoindex_context",
            return_value=self.COCO_CONTEXT,
        ):
            with patch(
                "context_orchestrator._serena_context",
                return_value=self.SERENA_CONTEXT,
            ):
                with patch(
                    "context_orchestrator._engram_context",
                    return_value=self.ENGRAM_CONTEXT,
                ):
                    result = enrich_prompt(
                        domain="python-testing",
                        prompt="Fix ScoreStore in run_task",
                        vault_path=tmp_path,
                    )

        # Combined wrapper
        assert "### Context from Knowledge Vault" in result
        # All three layer headers present
        assert "Relevant files from vault:" in result
        assert "Exact symbol locations (Serena LSP):" in result
        assert "Past similar trajectories:" in result
        # Layer 0 (CocoIndex) appears before Layer 1 (Serena) and Layer 2 (Engram)
        coco_pos = result.index("Relevant files from vault:")
        serena_pos = result.index("Exact symbol locations (Serena LSP):")
        engram_pos = result.index("Past similar trajectories:")
        assert coco_pos < serena_pos < engram_pos, (
            f"Expected Layer 0 < Layer 1 < Layer 2 ordering, "
            f"got coco={coco_pos}, serena={serena_pos}, engram={engram_pos}"
        )

    def test_partial_layer_failure_others_survive(self, tmp_path):
        """When one layer returns None, the other two still appear."""
        with patch(
            "context_orchestrator._cocoindex_context",
            return_value=self.COCO_CONTEXT,
        ):
            with patch(
                "context_orchestrator._serena_context",
                return_value=None,  # Serena unavailable
            ):
                with patch(
                    "context_orchestrator._engram_context",
                    return_value=self.ENGRAM_CONTEXT,
                ):
                    result = enrich_prompt(
                        domain="python-testing",
                        prompt="Fix ScoreStore",
                        vault_path=tmp_path,
                    )

        assert "Relevant files from vault:" in result
        assert "Exact symbol locations (Serena LSP):" not in result
        assert "Past similar trajectories:" in result
        assert "### Context from Knowledge Vault" in result

    def test_layer_exception_non_blocking(self, tmp_path):
        """An exception in one layer doesn't block the others."""
        with patch(
            "context_orchestrator._cocoindex_context",
            return_value=self.COCO_CONTEXT,
        ):
            with patch(
                "context_orchestrator._serena_context",
                side_effect=RuntimeError("Serena crashed"),
            ):
                with patch(
                    "context_orchestrator._engram_context",
                    return_value=self.ENGRAM_CONTEXT,
                ):
                    result = enrich_prompt(
                        domain="python-testing",
                        prompt="Fix ScoreStore",
                        vault_path=tmp_path,
                    )

        assert "Relevant files from vault:" in result
        assert "Exact symbol locations (Serena LSP):" not in result
        assert "Past similar trajectories:" in result

    def test_all_layers_empty_returns_empty(self, tmp_path):
        """When all three layers return None, enrich_prompt returns ''."""
        with patch("context_orchestrator._cocoindex_context", return_value=None):
            with patch("context_orchestrator._serena_context", return_value=None):
                with patch("context_orchestrator._engram_context", return_value=None):
                    result = enrich_prompt(
                        domain="python-testing",
                        prompt="Fix ScoreStore",
                        vault_path=tmp_path,
                    )

        assert result == ""

    def test_repo_path_forwarded_to_serena(self, tmp_path):
        """repo_path is forwarded to _serena_context."""
        repo = tmp_path / "fake-repo"
        repo.mkdir()

        with patch("context_orchestrator._cocoindex_context", return_value=None):
            with patch("context_orchestrator._engram_context", return_value=None):
                with patch(
                    "context_orchestrator._serena_context"
                ) as mock_serena:
                    mock_serena.return_value = self.SERENA_CONTEXT
                    enrich_prompt(
                        domain="python-testing",
                        prompt="Fix ScoreStore",
                        vault_path=tmp_path,
                        repo_path=repo,
                    )

        mock_serena.assert_called_once_with(
            "Fix ScoreStore", repo, 3
        )

    def test_domain_filtering_skips_layers(self, tmp_path):
        """Domains not in a layer's trigger set skip that layer entirely.

        'python-coding' triggers Serena only (not CocoIndex, not Engram).
        """
        with patch(
            "context_orchestrator._cocoindex_context"
        ) as mock_coco:
            with patch(
                "context_orchestrator._serena_context",
                return_value=self.SERENA_CONTEXT,
            ):
                with patch(
                    "context_orchestrator._engram_context"
                ) as mock_engram:
                    result = enrich_prompt(
                        domain="python-coding",
                        prompt="Fix ScoreStore",
                        vault_path=tmp_path,
                    )

        # CocoIndex and Engram never called for this domain
        mock_coco.assert_not_called()
        mock_engram.assert_not_called()
        # Only Serena contributed
        assert "Exact symbol locations (Serena LSP):" in result
        assert "Relevant files from vault:" not in result
        assert "Past similar trajectories:" not in result


class TestEngramContext:
    def test_no_trajectories_returns_none(self):
        """File-based _engram_context returns None when no trajectory files exist."""
        with patch("trajectory.list_trajectories", return_value=[]):
            ctx = _engram_context("python-testing", "any prompt", 3)
        assert ctx is None

    def test_scored_trajectories_formatted(self):
        """File-based _engram_context returns formatted context when trajectories exist."""
        mock_trajs = [
            {
                "timestamp": "2026-07-28T10:00:00+00:00",
                "agent": "student-coder",
                "task_score": 72.5,
                "response": "Fixed the run_task function",
            },
        ]
        with patch("trajectory.list_trajectories", return_value=mock_trajs):
            ctx = _engram_context("python-testing", "Fix run_task", 3)
        assert ctx is not None
        assert "Past similar trajectories" in ctx
        assert "student-coder" in ctx
        assert "72.5" in ctx
        assert "Fixed the run_task function" in ctx


_REAL_TRAJ_DIR = Path(__file__).resolve().parent.parent.joinpath("data", "trajectories")
_HAS_TRAJECTORIES = _REAL_TRAJ_DIR.exists()


@pytest.fixture
def real_traj_dir(monkeypatch):
    """Restore TRAJECTORY_DIR to the real path (conftest redirects it to tmp)."""
    import trajectory as traj_mod
    monkeypatch.setattr(traj_mod, "TRAJECTORY_DIR", _REAL_TRAJ_DIR)


class TestEngramContextRealFiles:
    """Integration tests: _engram_context reading real trajectory files from disk.

    These tests hit the actual ``data/trajectories/`` directory and verify the
    full file-read pipeline end-to-end — no mocks. They validate that
    ``list_trajectories()`` → ``_engram_context()`` formatting works against
    real data with varying scores, agents, and timestamps.
    """

    @pytest.mark.skipif(not _HAS_TRAJECTORIES, reason="Requires data/trajectories/")
    def test_returns_context_for_domain_with_trajectories(self, real_traj_dir):
        """_engram_context returns non-None for a domain that has trajectory files."""
        ctx = _engram_context("python-testing", "Write tests", 2)
        assert ctx is not None
        assert "Past similar trajectories" in ctx
        assert "**" in ctx  # agent is bolded: **agent**

    @pytest.mark.skipif(not _HAS_TRAJECTORIES, reason="Requires data/trajectories/")
    def test_domains_return_distinct_context(self, real_traj_dir):
        """Different domains return different trajectory data."""
        python_ctx = _engram_context("python-testing", "Write tests", 2)
        git_ctx = _engram_context("git-operations", "Squash commits", 2)

        assert python_ctx is not None
        assert git_ctx is not None
        # Each domain has different agents/scores; context content should differ
        assert python_ctx != git_ctx

    @pytest.mark.skipif(not _HAS_TRAJECTORIES, reason="Requires data/trajectories/")
    def test_top_k_limits_results(self, real_traj_dir):
        """Asking for 1 result returns fewer lines than asking for 5."""
        ctx_1 = _engram_context("code-implementation", "Implement feature", 1)
        ctx_5 = _engram_context("code-implementation", "Implement feature", 5)

        assert ctx_1 is not None
        assert ctx_5 is not None
        lines_1 = ctx_1.strip().split("\n")
        lines_5 = ctx_5.strip().split("\n")
        assert len(lines_1) < len(lines_5)

    @pytest.mark.skipif(not _HAS_TRAJECTORIES, reason="Requires data/trajectories/")
    def test_nonexistent_domain_returns_none(self, real_traj_dir):
        """A domain that will never have trajectory files returns None."""
        ctx = _engram_context("__nonexistent_domain_xyz__", "Review code", 3)
        assert ctx is None
