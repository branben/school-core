import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engram_adapter import engram_available, save_trajectory as engram_save

TRAJECTORY_DIR = Path(__file__).parent / "data" / "trajectories"


def ensure_dir():
    TRAJECTORY_DIR.mkdir(parents=True, exist_ok=True)


def capture_trajectory(
    domain: str,
    difficulty: str,
    agent: str,
    prompt: str,
    system_prompt: Optional[str],
    response: str,
    task_score: Optional[float] = None,
    old_score: Optional[float] = None,
    new_score: Optional[float] = None,
    evaluation: Optional[str] = None,
    error: Optional[str] = None,
) -> str:
    ensure_dir()
    timestamp = datetime.now(timezone.utc)
    ts_str = timestamp.strftime("%Y%m%d_%H%M%S_%f")

    trajectory = {
        "timestamp": timestamp.isoformat(),
        "domain": domain,
        "difficulty": difficulty,
        "agent": agent,
        "prompt": prompt,
        "system_prompt": system_prompt,
        "response": response,
        "task_score": task_score,
        "old_score": old_score,
        "new_score": new_score,
        "evaluation": evaluation,
        "error": error,
    }

    filename = f"{ts_str}--{domain}--{agent}.json"
    filepath = TRAJECTORY_DIR / filename

    with open(filepath, "w") as f:
        json.dump(trajectory, f, indent=2, ensure_ascii=False)

    # Save to Engram for searchable persistent memory
    if engram_available():
        obs_id = engram_save(trajectory, str(filepath))
        if obs_id:
            trajectory["engram_obs_id"] = obs_id
            with open(filepath, "w") as f:
                json.dump(trajectory, f, indent=2, ensure_ascii=False)

    return str(filepath)


def list_trajectories(domain: Optional[str] = None, limit: int = 20) -> list:
    ensure_dir()
    files = sorted(TRAJECTORY_DIR.glob("*.json"), reverse=True)
    result = []
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        if domain and data.get("domain") != domain:
            continue
        result.append(data)
        if len(result) >= limit:
            break
    return result


def count_trajectories() -> dict:
    ensure_dir()
    counts = {}
    for f in TRAJECTORY_DIR.glob("*.json"):
        try:
            with open(f) as fh:
                data = json.load(fh)
            d = data.get("domain", "unknown")
            counts[d] = counts.get(d, 0) + 1
        except (json.JSONDecodeError, OSError):
            pass
    return counts


def trajectories_for_training(domain: str, min_score: float = 50.0) -> list:
    ensure_dir()
    files = sorted(TRAJECTORY_DIR.glob(f"*--{domain}--*.json"))
    training = []
    for f in files:
        try:
            with open(f) as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        ts = data.get("task_score")
        if ts is not None and ts >= min_score and data.get("response"):
            training.append({
                "prompt": data["prompt"],
                "response": data["response"],
                "domain": domain,
                "agent": data.get("agent"),
                "timestamp": data.get("timestamp"),
            })
    return training
