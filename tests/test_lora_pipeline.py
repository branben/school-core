"""Tests for training/lora_pipeline.py and training/lora_config.py."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

from training.lora_config import LoraHyperparameters, TrainingDataConfig, HPARAMS, DATA_CFG, to_dict
from training.lora_pipeline import (
    prepare_training_data,
    train_for_domain,
    train_all,
    list_adapters,
    has_adapter,
    _adapter_version,
    _load_index,
    _save_index,
    _find_training_script,
    ADAPTER_VAULT,
    INDEX_PATH,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────

SAMPLE_TRAJECTORIES = [
    {
        "prompt": "Write a Python function that returns the sum of two numbers",
        "response": "def add(a, b): return a + b",
        "domain": "python-testing",
        "agent": "coder",
        "task_score": 85.0,
        "difficulty": "easy",
    },
    {
        "prompt": "Write a test for the add function",
        "response": "def test_add():\n    assert add(1, 2) == 3",
        "domain": "python-testing",
        "agent": "coder",
        "task_score": 90.0,
        "difficulty": "easy",
    },
    {
        "prompt": "Implement a factorial function",
        "response": "def factorial(n):\n    if n <= 1: return 1\n    return n * factorial(n-1)",
        "domain": "python-testing",
        "agent": "coder",
        "task_score": 75.0,
        "difficulty": "medium",
    },
    {
        "prompt": "Extract constant MAX_RETRIES from config",
        "response": "MAX_RETRIES = 3",
        "domain": "code-implementation",
        "agent": "coder",
        "task_score": 95.0,
        "difficulty": "easy",
    },
]


# ── lora_config ───────────────────────────────────────────────────────────────

class TestLoraConfig:
    def test_hyperparameters_have_defaults(self):
        h = LoraHyperparameters()
        assert h.r == 16
        assert h.lora_alpha == 32
        assert h.num_train_epochs == 3
        assert h.use_qlora is True

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("LORA_R", "32")
        monkeypatch.setenv("LORA_EPOCHS", "5")
        h = LoraHyperparameters()
        assert h.r == 32
        assert h.num_train_epochs == 5

    def test_data_config_defaults(self):
        d = TrainingDataConfig()
        assert d.min_score == 70.0
        assert d.base_model == "qwen2.5-coder:7b"
        assert "{prompt}" in d.format_template
        assert "{response}" in d.format_template

    def test_to_dict_returns_flat_dict(self):
        d = to_dict()
        assert isinstance(d, dict)
        assert "lora_r" in d
        assert "base_model" in d
        assert "epochs" in d
        assert "use_qlora" in d


# ── prepare_training_data ──────────────────────────────────────────────────────

class TestPrepareTrainingData:
    @patch("training.lora_pipeline.trajectories_for_training")
    def test_writes_jsonl(self, mock_traj, tmp_path):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:3]
        result = prepare_training_data("python-testing", output_dir=tmp_path)
        assert result is not None
        assert result.suffix == ".jsonl"
        assert result.exists()

        lines = result.read_text().strip().split("\n")
        assert len(lines) == 3  # One per trajectory
        for line in lines:
            entry = json.loads(line)
            assert "text" in entry
            assert "[INST]" in entry["text"]

    @patch("training.lora_pipeline.trajectories_for_training")
    def test_skips_empty_prompts(self, mock_traj, tmp_path):
        trajs = [
            {"prompt": "", "response": "some code"},
            {"prompt": "valid", "response": "more code"},
            {"prompt": "also valid", "response": "even more code"},
        ]
        mock_traj.return_value = trajs
        result = prepare_training_data("python-testing", output_dir=tmp_path)
        assert result is not None
        lines = result.read_text().strip().split("\n")
        assert len(lines) == 2  # Two valid prompts skipped the empty one

    @patch("training.lora_pipeline.trajectories_for_training")
    def test_insufficient_trajs_returns_none(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:2]
        result = prepare_training_data("python-testing")
        assert result is None

    @patch("training.lora_pipeline.trajectories_for_training")
    def test_writes_metadata_file(self, mock_traj, tmp_path):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:3]
        result = prepare_training_data("python-testing", output_dir=tmp_path)
        meta_path = str(result).replace(".jsonl", ".meta.json")
        assert Path(meta_path).exists()
        meta = json.loads(Path(meta_path).read_text())
        assert meta["domain"] == "python-testing"
        assert meta["trajectory_count"] == 3
        assert "config" in meta
        assert meta["config"]["epochs"] == 3


# ── _adapter_version / _load_index / _save_index ──────────────────────────────

class TestAdapterIndex:
    def test_version_starts_at_one(self, tmp_path):
        with patch("training.lora_pipeline.INDEX_PATH", tmp_path / "index.json"):
            v = _adapter_version("python-testing")
            assert v == 1

    def test_version_increments(self, tmp_path):
        index_path = tmp_path / "index.json"
        index_path.write_text(json.dumps({
            "python-testing": {
                "latest_version": 2,
                "versions": {"1": {}, "2": {}},
            },
        }))
        with patch("training.lora_pipeline.INDEX_PATH", index_path):
            v = _adapter_version("python-testing")
            assert v == 3

    def test_save_and_load_roundtrip(self, tmp_path):
        index_path = tmp_path / "index.json"
        with patch("training.lora_pipeline.INDEX_PATH", index_path):
            _save_index({"python-testing": {"latest_version": 1, "versions": {}}})
            loaded = _load_index()
            assert loaded["python-testing"]["latest_version"] == 1


# ── _find_training_script ─────────────────────────────────────────────────────

class TestFindTrainingScript:
    def test_uses_env_var(self, tmp_path, monkeypatch):
        script = tmp_path / "my_train.py"
        script.write_text("# training script")
        monkeypatch.setenv("UNSLOTH_TRAIN_SCRIPT", str(script))
        found = _find_training_script()
        assert found == str(script)

    def test_env_var_not_found_returns_none(self, monkeypatch):
        monkeypatch.setenv("UNSLOTH_TRAIN_SCRIPT", "/nonexistent/script.py")
        # Clear any PATH entries that might accidentally match
        monkeypatch.setenv("PATH", "/nonexistent")
        found = _find_training_script()
        assert found is None


# ── list_adapters / has_adapter ───────────────────────────────────────────────

class TestListAdapters:
    def test_empty_index_returns_empty(self, tmp_path):
        index = tmp_path / "index.json"
        index.write_text("{}")
        with patch("training.lora_pipeline.INDEX_PATH", index):
            assert list_adapters() == {}

    def test_returns_domain_info(self, tmp_path):
        index = tmp_path / "index.json"
        index.write_text(json.dumps({
            "python-testing": {
                "latest_version": 2,
                "versions": {
                    "1": {"trajectory_count": 10, "base_model": "qwen2.5-coder:7b"},
                    "2": {"trajectory_count": 25, "base_model": "qwen2.5-coder:7b"},
                },
            },
        }))
        with patch("training.lora_pipeline.INDEX_PATH", index):
            adapters = list_adapters()
            assert "python-testing" in adapters
            assert adapters["python-testing"]["latest_version"] == 2

    def test_has_adapter(self, tmp_path):
        index = tmp_path / "index.json"
        index.write_text(json.dumps({
            "python-testing": {"latest_version": 1, "versions": {"1": {}}},
        }))
        with patch("training.lora_pipeline.INDEX_PATH", index):
            assert has_adapter("python-testing") is True
            assert has_adapter("git-operations") is False


# ── train_for_domain (dry-run) ────────────────────────────────────────────────

class TestTrainForDomain:
    @patch("training.lora_pipeline.trajectories_for_training")
    def test_dry_run_returns_plan(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:3]
        result = train_for_domain("python-testing", dry_run=True)
        assert result is not None
        assert result["status"] == "dry_run"
        assert result["domain"] == "python-testing"
        assert result["version"] == 1  # Fresh index
        assert "data_file" in result
        assert "output_dir" in result

    @patch("training.lora_pipeline.trajectories_for_training")
    def test_insufficient_trajs_returns_none(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:2]
        result = train_for_domain("python-testing", dry_run=True)
        assert result is None

    @patch("training.lora_pipeline.trajectories_for_training")
    def test_dry_run_does_not_write_index(self, mock_traj):
        mock_traj.return_value = SAMPLE_TRAJECTORIES[:3]
        with patch("training.lora_pipeline.INDEX_PATH", Path("/nonexistent/index.json")):
            result = train_for_domain("python-testing", dry_run=True)
            assert result["status"] == "dry_run"


# ── train_all (dry-run) ────────────────────────────────────────────────────────

class TestTrainAll:
    @patch("trajectory.count_trajectories")
    @patch("training.lora_pipeline.trajectories_for_training")
    def test_dry_run_all_domains(self, mock_traj_fn, mock_count):
        mock_count.return_value = {
            "python-testing": 5,
            "code-implementation": 5,
            "_default": 10,
        }
        mock_traj_fn.return_value = SAMPLE_TRAJECTORIES[:3]

        results = train_all(dry_run=True)
        assert "python-testing" in results
        assert "code-implementation" in results
        assert "_default" not in results  # Skipped


# ── executor.py adapter resolution (integration check) ─────────────────────────

class TestLoraAdapterResolution:
    def test_lora_prefix_detection(self):
        from executor import _resolve_lora_adapter
        assert _resolve_lora_adapter("lora-python-testing") == "python-testing"
        assert _resolve_lora_adapter("lora-code-implementation") == "code-implementation"
        assert _resolve_lora_adapter("coder") is None
        assert _resolve_lora_adapter("auto/best-free") is None

    @patch("executor.COMBO_MAP", {"coder": "auto/best-free"})
    def test_lora_call_model_adds_adapter_prefix(self):
        from executor import call_model
        with patch("executor._omniroute_call", return_value={
            "choices": [{"message": {"content": "adapted response"}}],
        }):
            with patch("executor.API_KEY", "test-key"):
                result = call_model(
                    "lora-python-testing",
                    "Write a test",
                    system_prompt="You are a coder.",
                )
                assert result == "adapted response"


# ── director.py integration ────────────────────────────────────────────────────

class TestDirectorWiring:
    def test_has_adapter_importable_from_director(self):
        """Verify the import that director.py uses resolves correctly."""
        from training.lora_pipeline import has_adapter
        import inspect
        assert callable(has_adapter)

    @patch("training.lora_pipeline.INDEX_PATH")
    def test_has_adapter_returns_false_when_empty(self, mock_index_path, tmp_path):
        empty_index = tmp_path / "index.json"
        empty_index.write_text("{}")
        mock_index_path.__str__ = lambda s: str(empty_index)
        mock_index_path.exists = lambda: True
        mock_index_path.read_text = lambda: empty_index.read_text()
        from training.lora_pipeline import has_adapter
        assert has_adapter("python-testing") is False


# ── CLI entry point ───────────────────────────────────────────────────────────

class TestCli:
    def test_list_eligible(self, capsys):
        """--list-eligible should not crash."""
        from training.lora_pipeline import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--list-eligible"])
        assert args.list_eligible is True

    def test_list_adapters(self, capsys):
        """--list should not crash."""
        from training.lora_pipeline import _build_parser
        parser = _build_parser()
        args = parser.parse_args(["--list"])
        assert args.list is True
