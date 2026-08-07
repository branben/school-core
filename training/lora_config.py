"""training/lora_config.py — LoRA / QLoRA hyperparameters for SkillOpt fine-tuning.

All parameters are user-tunable via environment variable overrides
(e.g. ``LORA_RANK=32 python -m training.lora_pipeline``). This file
is the single source of truth for training configuration — both the
data-preparation pipeline and the external ``unsloth_train.py`` script
read from here (the latter via CLI args).

NOTE: All env-var-dependent defaults use ``field(default_factory=...)``
so that test monkeypatching (``monkeypatch.setenv`` before instantiation)
works correctly. Plain ``default=`` would evaluate the env var once at
class definition time.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class LoraHyperparameters:
    """Hyperparameters for LoRA / QLoRA fine-tuning.

    Defaults are tuned for a 7B base model with 4-bit QLoRA quantisation
    on a single consumer GPU (~12 GB VRAM). Override via env vars.
    """

    # ── LoRA rank / alpha ────────────────────────────────────────────────
    r: int = field(default_factory=lambda: int(os.environ.get("LORA_R", "16")))
    lora_alpha: int = field(default_factory=lambda: int(os.environ.get("LORA_ALPHA", "32")))
    target_modules: list[str] = field(
        default_factory=lambda: os.environ.get(
            "LORA_TARGET_MODULES",
            "q_proj,v_proj,k_proj,o_proj,gate_proj,up_proj,down_proj",
        ).split(",")
    )

    # ── Training ─────────────────────────────────────────────────────────
    per_device_train_batch_size: int = field(
        default_factory=lambda: int(os.environ.get("LORA_BATCH_SIZE", "4"))
    )
    gradient_accumulation_steps: int = field(
        default_factory=lambda: int(os.environ.get("LORA_GRAD_ACC", "4"))
    )
    num_train_epochs: int = field(
        default_factory=lambda: int(os.environ.get("LORA_EPOCHS", "3"))
    )
    learning_rate: float = field(
        default_factory=lambda: float(os.environ.get("LORA_LR", "2e-4"))
    )
    max_seq_length: int = field(
        default_factory=lambda: int(os.environ.get("LORA_MAX_SEQ", "2048"))
    )

    # ── QLoRA (4-bit) ────────────────────────────────────────────────────
    use_qlora: bool = field(
        default_factory=lambda: os.environ.get("LORA_USE_QLORA", "1") == "1"
    )
    bnb_4bit_compute_dtype: str = field(
        default_factory=lambda: os.environ.get("LORA_4BIT_DTYPE", "float16")
    )

    # ── Output / data ────────────────────────────────────────────────────
    output_base: str = field(
        default_factory=lambda: os.environ.get(
            "LORA_OUTPUT_BASE",
            os.path.join(os.path.dirname(os.path.dirname(__file__)), "adapter_vault"),
        )
    )
    save_steps: int = field(default_factory=lambda: int(os.environ.get("LORA_SAVE_STEPS", "50")))
    logging_steps: int = field(default_factory=lambda: int(os.environ.get("LORA_LOG_STEPS", "10")))


@dataclass
class TrainingDataConfig:
    """Configuration for training-data preparation from trajectories."""

    min_score: float = field(default_factory=lambda: float(os.environ.get("LORA_MIN_SCORE", "70")))
    base_model: str = field(default_factory=lambda: os.environ.get("LORA_BASE_MODEL", "qwen2.5-coder:7b"))
    prompt_field: str = "prompt"
    response_field: str = "response"
    # Format template for training pairs. Variables: {prompt}, {response}.
    # Default: Llama-style format. For Qwen ChatML, set:
    #   LORA_FORMAT="<|im_start|>user\n{prompt}<|im_end|>\n<|im_start|>assistant\n{response}<|im_end|>"
    format_template: str = field(
        default_factory=lambda: os.environ.get(
            "LORA_FORMAT",
            "<s>[INST] {prompt} [/INST] {response} </s>",
        )
    )


# Singleton instances — import these to get current config.
# NOTE: These are evaluated at module-load time with whatever env vars are
# set at that point. Tests that need different values should instantiate
# LoraHyperparameters() / TrainingDataConfig() directly after setenv.
HPARAMS = LoraHyperparameters()
DATA_CFG = TrainingDataConfig()


def to_dict() -> dict:
    """Return combined config as a flat dict for logging / JSONL headers."""
    return {
        "lora_r": HPARAMS.r,
        "lora_alpha": HPARAMS.lora_alpha,
        "target_modules": ",".join(HPARAMS.target_modules),
        "batch_size": HPARAMS.per_device_train_batch_size,
        "grad_accum": HPARAMS.gradient_accumulation_steps,
        "epochs": HPARAMS.num_train_epochs,
        "learning_rate": HPARAMS.learning_rate,
        "max_seq_length": HPARAMS.max_seq_length,
        "use_qlora": HPARAMS.use_qlora,
        "base_model": DATA_CFG.base_model,
        "min_score": DATA_CFG.min_score,
    }
