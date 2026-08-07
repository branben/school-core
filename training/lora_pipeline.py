"""training/lora_pipeline.py — SkillOpt LoRA training pipeline.

Prepares training data from high-scoring trajectories, shells out to
an external Unsloth training script, and registers trained adapters in
the adapter vault.

No Unsloth / torch / transformers dependency in school-core — all
training operations are subprocess only.

Usage:
    python -m training.lora_pipeline                         # Show eligible domains
    python -m training.lora_pipeline --domain python-testing  # Train one domain
    python -m training.lora_pipeline --domain python-testing --apply  # Train & register
    python -m training.lora_pipeline --all --apply            # Train all eligible domains
"""

import argparse
import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from training.lora_config import HPARAMS, DATA_CFG, to_dict
from trajectory import trajectories_for_training
from curricula.generator import SKIP_DOMAINS as CURRICULA_SKIP_DOMAINS

logger = logging.getLogger(__name__)

# Paths
SCHOOL_CORE = Path(__file__).resolve().parent.parent
ADAPTER_VAULT = SCHOOL_CORE / "adapter_vault"
INDEX_PATH = ADAPTER_VAULT / "index.json"
TRAINING_DIR = Path("/tmp") / "lora-training"


# ── Data preparation ──────────────────────────────────────────────────────────

def prepare_training_data(
    domain: str,
    min_score: float = 70.0,
    output_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Fetch qualifying trajectories and write them as a JSONL training file.

    Each line is a ``(prompt, response)`` pair formatted according to
    ``DATA_CFG.format_template``.

    Returns the path to the JSONL file, or ``None`` if insufficient data.
    """
    trajs = trajectories_for_training(domain, min_score=min_score)
    if len(trajs) < 3:
        logger.info("Domain %r: %d trajectories (need >= 3), skipping", domain, len(trajs))
        return None

    out_dir = output_dir or (TRAINING_DIR / domain.replace("/", "_"))
    out_dir.mkdir(parents=True, exist_ok=True)
    date_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    jsonl_path = out_dir / f"{domain}-{date_str}.jsonl"

    with open(jsonl_path, "w") as f:
        for t in trajs:
            prompt = t.get(DATA_CFG.prompt_field, "").strip()
            response = t.get(DATA_CFG.response_field, "").strip()
            if not prompt or not response:
                continue
            formatted = DATA_CFG.format_template.format(
                prompt=prompt,
                response=response,
            )
            f.write(json.dumps({"text": formatted}) + "\n")

    # Write a companion metadata file
    meta = {
        "domain": domain,
        "trajectory_count": len(trajs),
        "created": date_str,
        "config": to_dict(),
    }
    meta_path = str(jsonl_path).replace(".jsonl", ".meta.json")
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)

    logger.info(
        "Prepared %d trajectories → %s (%d examples)",
        len(trajs), jsonl_path, len(trajs),
    )
    return jsonl_path


# ── Subprocess training ───────────────────────────────────────────────────────

def _adapter_version(domain: str) -> int:
    """Compute the next version number for a domain's adapter."""
    index = _load_index()
    versions = index.get(domain, {}).get("versions", {})
    if not versions:
        return 1
    return max(int(v) for v in versions) + 1


def _load_index() -> dict:
    """Load the adapter vault index. Reads from disk each call — the index
    file is a sub-1 KB JSON so the I/O overhead is negligible."""
    if INDEX_PATH.exists():
        try:
            return json.loads(INDEX_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_index(index: dict) -> None:
    """Save the adapter vault index."""
    ADAPTER_VAULT.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")


def _find_training_script() -> Optional[str]:
    """Locate the external Unsloth training script.

    Search order:
    1. ``UNSLOTH_TRAIN_SCRIPT`` env var (explicit path)
    2. ``unsloth_train.py`` on PATH
    3. ``SCHOOL_CORE / scripts / unsloth_train.py``
    """
    explicit = os.environ.get("UNSLOTH_TRAIN_SCRIPT")
    if explicit:
        if Path(explicit).exists():
            return explicit
        logger.warning("UNSLOTH_TRAIN_SCRIPT=%s not found, falling back to PATH", explicit)

    # Search PATH
    for p in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(p) / "unsloth_train.py"
        if candidate.exists():
            return str(candidate)

    # School-core bundled script
    bundled = SCHOOL_CORE / "scripts" / "unsloth_train.py"
    if bundled.exists():
        return str(bundled)

    return None


def train_for_domain(
    domain: str,
    min_score: float = 70.0,
    base_model: Optional[str] = None,
    dry_run: bool = True,
) -> Optional[dict]:
    """Run the full LoRA training pipeline for one domain.

    Parameters
    ----------
    domain:
        Domain to train on (e.g. ``"python-testing"``).
    min_score:
        Minimum trajectory score to include.
    base_model:
        Base model name (default: ``DATA_CFG.base_model``).
    dry_run:
        If True, prepare data + show plan but don't train or register.

    Returns
    -------
    A dict with training results, or ``None`` if insufficient data.
    """
    # Step 1: Prepare training data
    jsonl_path = prepare_training_data(domain, min_score=min_score)
    if jsonl_path is None:
        return None

    bm = base_model or DATA_CFG.base_model
    version = _adapter_version(domain)
    adapter_dir = ADAPTER_VAULT / domain / f"v{version}"

    result = {
        "domain": domain,
        "version": version,
        "data_file": str(jsonl_path),
        "output_dir": str(adapter_dir),
        "base_model": bm,
        "status": "dry_run" if dry_run else "pending",
    }

    if dry_run:
        logger.info(
            "[DRY-RUN] Would train domain=%s base=%s data=%s output=%s",
            domain, bm, jsonl_path, adapter_dir,
        )
        return result

    # Step 2: Find the training script
    script = _find_training_script()
    if not script:
        logger.error(
            "No Unsloth training script found. "
            "Set UNSLOTH_TRAIN_SCRIPT env var or install unsloth_train.py on PATH."
        )
        result["status"] = "error"
        result["error"] = "Training script not found"
        return result

    # NOTE: The inference-side LoRA integration in ``executor.py`` is
    # currently prompt-level only (prepends ``[ACTIVATE ADAPTER: domain]``
    # to the system prompt). Actual weight-level loading of
    # ``adapter_model.safetensors`` requires OmniRoute / Ollama / Foundry
    # to support dynamic adapter loading — out of scope for school-core.
    #
    # Step 3: Build CLI args for the external script
    cmd = [
        sys.executable, script,
        "--data", str(jsonl_path),
        "--output", str(adapter_dir),
        "--base-model", bm,
        "--lora-r", str(HPARAMS.r),
        "--lora-alpha", str(HPARAMS.lora_alpha),
        "--target-modules", ",".join(HPARAMS.target_modules),
        "--batch-size", str(HPARAMS.per_device_train_batch_size),
        "--grad-accum", str(HPARAMS.gradient_accumulation_steps),
        "--epochs", str(HPARAMS.num_train_epochs),
        "--lr", str(HPARAMS.learning_rate),
        "--max-seq", str(HPARAMS.max_seq_length),
    ]
    if HPARAMS.use_qlora:
        cmd.append("--qlora")

    # Step 4: Execute training
    try:
        logger.info("Starting training: %s", " ".join(cmd))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=3600,  # 1-hour timeout for training
        )

        if proc.returncode != 0:
            error_msg = f"Training failed (exit={proc.returncode}): {proc.stderr[:500]}"
            logger.error(error_msg)
            result["status"] = "error"
            result["error"] = error_msg
            result["stdout"] = proc.stdout[-500:]
            result["stderr"] = proc.stderr[-500:]
            return result

        logger.info("Training completed for %s (v%d)", domain, version)
    except subprocess.TimeoutExpired:
        logger.error("Training timed out after 3600s for %s", domain)
        result["status"] = "error"
        result["error"] = "Training timed out"
        return result
    except FileNotFoundError:
        logger.error("Python executable not found: %s", sys.executable)
        result["status"] = "error"
        result["error"] = f"Python not found: {sys.executable}"
        return result

    # Step 5: Verify adapter output
    adapter_config = adapter_dir / "adapter_config.json"
    adapter_weights = adapter_dir / "adapter_model.safetensors"
    if not adapter_config.exists() or not adapter_weights.exists():
        logger.error(
            "Training completed but adapter files missing: %s, %s",
            adapter_config, adapter_weights,
        )
        result["status"] = "error"
        result["error"] = "Adapter files not found after training"
        return result

    # Step 6: Register in adapter vault index
    index = _load_index()
    index.setdefault(domain, {
        "latest_version": version,
        "versions": {},
    })
    index[domain]["latest_version"] = version
    index[domain]["versions"][str(version)] = {
        "created": datetime.now(timezone.utc).isoformat(),
        "trajectory_count": _count_trajs_in_jsonl(jsonl_path),
        "base_model": bm,
        "rank": HPARAMS.r,
        "notes": f"Training from {_count_trajs_in_jsonl(jsonl_path)} trajectories, "
                 f"{HPARAMS.num_train_epochs} epochs, rank={HPARAMS.r}",
    }
    _save_index(index)

    result["status"] = "success"
    logger.info("Registered adapter: %s v%d → %s", domain, version, adapter_dir)
    return result


def _count_trajs_in_jsonl(path: Path) -> int:
    """Count lines in a JSONL file."""
    try:
        return sum(1 for _ in open(path))
    except OSError:
        return 0


def train_all(
    min_score: float = 70.0,
    dry_run: bool = True,
) -> dict[str, dict]:
    """Train adapters for all eligible domains.

    Returns dict of ``{domain: result}``.
    """
    from trajectory import count_trajectories
    all_counts = count_trajectories()

    results = {}
    for domain in sorted(all_counts):
        # Skip non-curriculum domains (same set used by curricula/generator.py)
        if domain in CURRICULA_SKIP_DOMAINS:
            continue
        result = train_for_domain(domain, min_score=min_score, dry_run=dry_run)
        if result:
            results[domain] = result

    return results


# ── Adapter discovery (used by executor.py / director.py) ──────────────────────

def list_adapters() -> dict[str, dict]:
    """Return all registered adapters: ``{domain: {version, path, ...}}``."""
    index = _load_index()
    return {
        domain: {
            "latest_version": info.get("latest_version", 1),
            **info.get("versions", {}).get(str(info.get("latest_version", 1)), {}),
        }
        for domain, info in index.items()
    }


def has_adapter(domain: str) -> bool:
    """Check if a trained adapter exists for *domain*."""
    adapters = list_adapters()
    return domain in adapters


# ── CLI ───────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="SkillOpt LoRA training pipeline",
    )
    parser.add_argument("--domain", help="Train a specific domain")
    parser.add_argument("--all", action="store_true", help="Train all eligible domains")
    parser.add_argument("--min-score", type=float, default=70.0, help="Min trajectory score")
    parser.add_argument("--base-model", default=None, help="Override base model")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview only (default)")
    parser.add_argument("--apply", action="store_true", help="Run training and register adapter")
    parser.add_argument("--list", action="store_true", help="List registered adapters")
    parser.add_argument("--list-eligible", action="store_true", help="List domains with enough trajectories")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-5s | %(message)s",
        datefmt="%H:%M:%S",
    )

    dry_run = not args.apply

    if args.list:
        adapters = list_adapters()
        if not adapters:
            print("No registered adapters.")
            return
        print(f"{'Domain':25} v{'Path'}")
        print("-" * 80)
        for domain, info in sorted(adapters.items()):
            v = info.get("latest_version", "?")
            path = ADAPTER_VAULT / domain / f"v{v}"
            print(f"{domain:25} {v}  {path}")
        return

    if args.list_eligible:
        from trajectory import count_trajectories
        for domain, count in sorted(count_trajectories().items()):
            trajs = trajectories_for_training(domain, min_score=args.min_score)
            status = "✅" if len(trajs) >= 3 else "❌"
            print(f"{status} {domain:25} {len(trajs):3} eligible (from {count} total)")
        return

    if args.domain:
        results = {
            args.domain: train_for_domain(
                args.domain,
                min_score=args.min_score,
                base_model=args.base_model,
                dry_run=dry_run,
            ),
        }
    elif args.all:
        results = train_all(min_score=args.min_score, dry_run=dry_run)
    else:
        # Default: show eligible domains
        from trajectory import count_trajectories
        print("Eligible domains (use --domain X or --all --apply to train):")
        for domain, count in sorted(count_trajectories().items()):
            trajs = trajectories_for_training(domain, min_score=args.min_score)
            status = "✅" if len(trajs) >= 3 else "❌"
            print(f"  {status} {domain:25} {len(trajs):3} eligible (from {count} total)")
        print(f"\nConfig: base_model={args.base_model or DATA_CFG.base_model}, "
              f"min_score={args.min_score}, rank={HPARAMS.r}, epochs={HPARAMS.num_train_epochs}")
        return

    for domain, result in sorted(results.items()):
        if result is None:
            print(f"  {domain}: skipped (insufficient trajectories)")
        elif dry_run:
            print(f"  {domain}: [DRY-RUN] {result['data_file']} → {result['output_dir']}")
        elif result["status"] == "success":
            print(f"  {domain}: ✅ v{result['version']} → {result['output_dir']}")
        else:
            print(f"  {domain}: ❌ {result.get('error', 'unknown error')}")


if __name__ == "__main__":
    main()
