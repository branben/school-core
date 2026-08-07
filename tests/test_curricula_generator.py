"""Tests for curricula/generator.py — AutoHarness curriculum generator."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import yaml

from curricula.generator import (
    _normalize_prompt,
    _parse_prompt,
    _generate_task_description,
    _truncate,
    calculate_gates,
    generate_tasks,
    generate_curriculum,
    generate_all,
    update_index,
    main,
    CURRICULA_DIR,
    SKIP_DOMAINS,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_TRAJECTORIES = [
    {
        "prompt": "Write a Python function called `is_palindrome` that takes a string and returns True if it's a palindrome (case-insensitive).",
        "response": "def is_palindrome(s): return s.lower() == s.lower()[::-1]",
        "domain": "python-testing",
        "agent": "coder",
        "task_score": 72.5,
        "difficulty": "medium",
    },
    {
        "prompt": "Write a pytest test for the is_palindrome function covering empty string, single char, and mixed case.",
        "response": "def test_is_palindrome():\n    assert is_palindrome('')\n    assert is_palindrome('a')\n    assert is_palindrome('Racecar')",
        "domain": "python-testing",
        "agent": "coder",
        "task_score": 85.0,
        "difficulty": "medium",
    },
    {
        "prompt": "Fix the failing test in test_score_store — the mock needs to return the correct value for empty stores.",
        "response": "def test_empty_store():\n    store = ScoreStore()\n    assert store.get_score('coder', 'testing') == 0.0",
        "domain": "python-testing",
        "agent": "coder",
        "task_score": 70.0,
        "difficulty": "easy",
    },
    {
        "prompt": "Extract MAX_SPECTATORS=10 constant from the game loop and move it to constants.py.",
        "response": "MAX_SPECTATORS = 10",
        "domain": "code-implementation",
        "agent": "coder",
        "task_score": 95.0,
        "difficulty": "easy",
    },
    {
        "prompt": "Room creation fails with console error — host stuck waiting for host, others cannot join.",
        "response": "Fixed room creation race condition by adding a lock around the room state transition.",
        "domain": "code-implementation",
        "agent": "coder",
        "task_score": 70.0,
        "difficulty": "medium",
    },
    {
        "prompt": "Write a Python function `add(a, b)` that returns the sum of two numbers.",
        "response": "def add(a, b): return a + b",
        "domain": "python-coding",
        "agent": "coder",
        "task_score": 100.0,
        "difficulty": "easy",
    },
    {
        "prompt": "route 0",
        "response": "print('hello')",
        "domain": "python-coding",
        "agent": "coder",
        "task_score": 70.0,
        "difficulty": "easy",
    },
]

MOCK_TRAJECTORY_DIR = Path("/tmp/test_trajectories")


# ── _normalize_prompt ─────────────────────────────────────────────────────────

class TestNormalizePrompt:
    def test_strips_markdown_header(self):
        assert _normalize_prompt("# Write a test") == "Write a test"

    def test_strips_prefix(self):
        assert _normalize_prompt("Task: Write a test") == "Write a test"
        assert _normalize_prompt("Issue: Room creation fails") == "Room creation fails"
        assert _normalize_prompt("Problem: Can't connect") == "Can't connect"

    def test_strips_parenthetical_notes(self):
        assert _normalize_prompt("Fix the bug (see #123 for details)") == "Fix the bug"

    def test_takes_first_line_only(self):
        result = _normalize_prompt("First line\nSecond line\nThird line")
        assert result == "First line"
        assert "\n" not in result

    def test_empty_prompt(self):
        assert _normalize_prompt("") == ""
        assert _normalize_prompt("   ") == ""


# ── _parse_prompt ─────────────────────────────────────────────────────────────

class TestParsePrompt:
    def test_direct_write_prompt(self):
        action, text = _parse_prompt("Write a Python function that returns the factorial of a number.")
        assert action == "WRITE"
        assert "factorial" in text

    def test_direct_implement_prompt(self):
        action, text = _parse_prompt("Implement a rate limiter for the API")
        assert action == "IMPLEMENT"
        assert "rate limiter" in text

    def test_template_extract(self):
        action, subject = _parse_prompt("Extract MAX_SPECTATORS=10 constant from the game loop")
        assert action == "Extract"
        assert "MAX_SPECTATORS" in subject

    def test_template_fix(self):
        action, subject = _parse_prompt("Fix the failing test in test_score_store")
        assert action == "Fix"
        assert "test_score_store" in subject or "failing test" in subject

    def test_template_add(self):
        action, subject = _parse_prompt("Add error handling for empty input")
        assert action == "Add"
        assert "empty input" in subject

    def test_issue_room_creation(self):
        """Room creation fails... — noun-phrase issue description."""
        action, subject = _parse_prompt("Room creation fails with console error — host stuck waiting")
        assert action == "Fix bug in"
        assert "Room creation" in subject

    def test_short_generic_prompt(self):
        """Very short/generic prompts should fall back."""
        action, subject = _parse_prompt("route 0")
        assert action == "Work on"

    def test_empty_prompt(self):
        action, subject = _parse_prompt("")
        assert action == "Work on"
        assert subject == "a coding task"

    def test_truncated_long_prompt(self):
        long = "Write a comprehensive Python function that handles all edge cases for the palindrome problem including unicode"
        action, text = _parse_prompt(long)
        assert action == "WRITE"
        assert len(text) <= 73  # 70 + "..."


# ── _generate_task_description ────────────────────────────────────────────────

class TestGenerateTaskDescription:
    def test_direct_action_returns_as_is(self):
        result = _generate_task_description("WRITE", "Write a Python function for factorial", "python-testing")
        assert result == "Write a Python function for factorial"

    def test_template_extract_in_code_impl(self):
        result = _generate_task_description("Extract", "MAX_SPECTATORS constant", "code-implementation")
        assert "MAX_SPECTATORS" in result
        assert "Extract" in result

    def test_fix_bug_in_special(self):
        result = _generate_task_description("Fix bug in", "Room creation has race condition", "code-implementation")
        assert "Fix a bug where" in result
        assert "Room creation" in result

    def test_fix_in_python_testing(self):
        result = _generate_task_description("Fix", "failing test in score_store", "python-testing")
        assert "Fix" in result
        assert "score_store" in result
        # Should pick "Fix the failing test for {subject}"
        assert "failing test" in result

    def test_write_in_python_coding(self):
        result = _generate_task_description("WRITE", "Write a Python function for factorial", "python-coding")
        assert result == "Write a Python function for factorial"

    def test_add_in_code_impl(self):
        result = _generate_task_description("Add", "error handling for empty input", "code-implementation")
        assert "Add" in result
        assert "error handling" in result


# ── _truncate ──────────────────────────────────────────────────────────────────

class TestTruncate:
    def test_short_text(self):
        assert _truncate("short") == "short"

    def test_long_text(self):
        long = "a" * 100
        result = _truncate(long, max_len=20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_exact_max_len(self):
        assert _truncate("hello world", max_len=11) == "hello world"

    def test_default_max_len_70(self):
        long = "a" * 100
        result = _truncate(long)
        assert len(result) == 70
        assert result.endswith("...")


# ── calculate_gates ────────────────────────────────────────────────────────────

class TestCalculateGates:
    def test_three_gates_for_ten_trajs(self):
        trajs = [{"task_score": float(s)} for s in [95, 90, 85, 80, 75, 70, 65, 60, 55, 50]]
        gates = calculate_gates(trajs)
        # 10 trajs → 3 gates (since n <= 15 → n_gates=3)
        assert len(gates) == 3

    def test_first_gate_is_zero(self):
        trajs = [{"task_score": float(s)} for s in [95, 80, 70]]
        gates = calculate_gates(trajs)
        assert gates[0]["score_required"] == 0

    def test_single_gate_for_few_trajs(self):
        trajs = [{"task_score": float(s)} for s in [85, 80, 75]]
        gates = calculate_gates(trajs)
        assert len(gates) == 1

    def test_empty_trajs_returns_empty(self):
        assert calculate_gates([]) == []

    def test_trajs_grouped_by_score(self):
        trajs = [{"task_score": float(s)} for s in [70, 75, 80, 85, 90, 95]]
        gates = calculate_gates(trajs)
        # 6 trajs → 2 gates (n <= 8 → n_gates=2)
        assert len(gates) == 2
        # Higher-scoring trajs in first gate
        first_scores = [t["task_score"] for t in gates[0]["trajectories"]]
        second_scores = [t["task_score"] for t in gates[1]["trajectories"]]
        assert all(s >= max(second_scores) for s in first_scores)
        assert all(s < min(first_scores) for s in second_scores)


# ── generate_tasks ────────────────────────────────────────────────────────────

class TestGenerateTasks:
    def test_deduplicates_tasks(self):
        gate = {
            "score_required": 0,
            "trajectories": [
                {"prompt": "Write a test for function X", "task_score": 85},
                {"prompt": "Write a test for function X", "task_score": 85},
                {"prompt": "Write a test for function Y", "task_score": 75},
            ],
        }
        tasks = generate_tasks(gate, "python-testing")
        assert len(tasks) <= 2  # Deduplicated
        assert len(tasks) >= 1

    def test_skips_generic_tasks(self):
        gate = {
            "score_required": 0,
            "trajectories": [
                {"prompt": "route 0", "task_score": 70},
                {"prompt": "Write a proper function", "task_score": 85},
            ],
        }
        tasks = generate_tasks(gate, "python-coding")
        # "route 0" should be skipped as generic
        assert len(tasks) >= 1
        assert all("route" not in t.lower() for t in tasks)

    def test_limits_to_max_tasks(self):
        gate = {
            "score_required": 0,
            "trajectories": [{"prompt": f"Write function {i}", "task_score": 85.0} for i in range(20)],
        }
        tasks = generate_tasks(gate, "python-testing", max_tasks=3)
        assert len(tasks) == 3


# ── generate_curriculum ────────────────────────────────────────────────────────

class TestGenerateCurriculum:
    @patch("curricula.generator.trajectories_for_training")
    def test_generates_valid_yaml(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:5]  # 5 qualifying trajs
        result = generate_curriculum("python-testing", dry_run=True)
        assert result is not None
        data = yaml.safe_load(result)
        assert "domain" in data
        assert data["domain"].startswith("auto-python-testing-")
        assert "gate_steps" in data
        assert len(data["gate_steps"]) >= 1
        for step in data["gate_steps"]:
            assert "score_required" in step
            assert "task_count" in step
            assert "tasks" in step
            assert "evaluation_rubric" in step

    @patch("curricula.generator.trajectories_for_training")
    def test_insufficient_trajs_returns_none(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:2]  # Only 2
        result = generate_curriculum("python-testing", dry_run=True)
        assert result is None

    @patch("curricula.generator.trajectories_for_training")
    def test_skipped_domain_returns_none(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:5]
        result = generate_curriculum("_default", dry_run=True)
        assert result is None

    @patch("curricula.generator.trajectories_for_training")
    def test_writes_file_in_apply_mode(self, mock_traj, tmp_path):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:5]
        result = generate_curriculum("python-testing", output_dir=tmp_path, dry_run=False)
        assert result is not None
        assert result.endswith(".yaml")
        written_file = tmp_path / result
        assert written_file.exists()
        data = yaml.safe_load(written_file.read_text())
        assert data["domain"].startswith("auto-python-testing-")

    @patch("curricula.generator.trajectories_for_training")
    def test_dry_run_does_not_write(self, mock_traj, tmp_path):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:5]
        result = generate_curriculum("python-testing", output_dir=tmp_path, dry_run=True)
        assert result is not None  # Returns YAML string
        assert not list(tmp_path.glob("*.yaml"))  # No files written


# ── generate_all ────────────────────────────────────────────────────────────────

class TestGenerateAll:
    @patch("trajectory.count_trajectories")
    @patch("curricula.generator.trajectories_for_training")
    def test_generates_multiple_domains(self, mock_traj_fn, mock_count):
        # Simulate 3 domains with trajectories
        mock_count.return_value = {
            "python-testing": 5,
            "code-implementation": 5,
            "python-coding": 5,
        }
        # Return same sample for any domain
        mock_traj_fn.return_value = SAMPLE_TRAJECTORIES[:5]

        results = generate_all(dry_run=True)
        # Should generate curricula for all domains with >= 3 eligible trajs
        assert "python-testing" in results
        assert "code-implementation" in results
        assert "python-coding" in results

    @patch("trajectory.count_trajectories")
    @patch("curricula.generator.trajectories_for_training")
    def test_skipped_domains_not_included(self, mock_traj_fn, mock_count):
        mock_count.return_value = {"_default": 10}
        mock_traj_fn.return_value = SAMPLE_TRAJECTORIES[:5]

        results = generate_all(dry_run=True)
        assert "_default" not in results
        assert len(results) == 0


# ── update_index ───────────────────────────────────────────────────────────────

class TestUpdateIndex:
    def test_appends_new_entries(self, tmp_path):
        # Create a minimal index.yaml
        index_path = tmp_path / "index.yaml"
        index_path.write_text("curricula:\n  python-testing:\n    file: python-testing.yaml\n    description: Manual\n    gates: [0, 25, 50, 75, 95]\n    prerequisites: []\n")

        with patch("curricula.generator.CURRICULA_DIR", tmp_path):
            update_index([
                {
                    "domain": "auto-python-testing-20260730",
                    "filename": "auto-python-testing-20260730.yaml",
                    "description": "Auto-generated from 5 trajs",
                    "gates": [0, 70],
                },
                {
                    "domain": "auto-code-implementation-20260730",
                    "filename": "auto-code-implementation-20260730.yaml",
                    "description": "Auto-generated from 10 trajs",
                    "gates": [0, 70, 85],
                },
            ])

        # Read back and verify
        with open(index_path) as f:
            index = yaml.safe_load(f)

        assert "python-testing" in index["curricula"]  # Original preserved
        assert "auto-python-testing-20260730" in index["curricula"]  # New entry
        assert index["curricula"]["auto-python-testing-20260730"]["file"] == "auto-python-testing-20260730.yaml"
        assert index["curricula"]["auto-python-testing-20260730"]["gates"] == [0, 70]

    def test_preserves_existing_entries(self, tmp_path):
        index_path = tmp_path / "index.yaml"
        index_path.write_text("curricula:\n  git-operations:\n    file: git-operations.yaml\n    description: Manual\n    gates: [0, 25, 50, 75]\n    prerequisites: []\n")

        with patch("curricula.generator.CURRICULA_DIR", tmp_path):
            update_index([
                {
                    "domain": "auto-python-testing-20260730",
                    "filename": "auto-python-testing-20260730.yaml",
                    "description": "Auto-generated",
                    "gates": [0, 70],
                },
            ])

        with open(index_path) as f:
            index = yaml.safe_load(f)

        assert "git-operations" in index["curricula"]  # Original still there
        assert "auto-python-testing-20260730" in index["curricula"]  # New added


# ── SKIP_DOMAINS ───────────────────────────────────────────────────────────────

class TestSkipDomains:
    def test_default_is_skipped(self):
        assert "_default" in SKIP_DOMAINS

    def test_unknown_is_skipped(self):
        assert "unknown" in SKIP_DOMAINS

    def test_real_domains_not_skipped(self):
        for d in ("python-testing", "code-implementation", "python-coding", "git-operations", "code-review"):
            assert d not in SKIP_DOMAINS
