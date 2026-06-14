import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

ENGRAM_BIN = "engram"
ENGRAM_PROJECT = "agent-school"


def engram_available() -> bool:
    try:
        result = subprocess.run(
            [ENGRAM_BIN, "--version"],
            capture_output=True, timeout=5, check=False,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def save_trajectory(trajectory: dict, filepath: str) -> Optional[str]:
    """Save trajectory to Engram. Returns observation ID or None on failure."""
    if not engram_available():
        return None

    domain = trajectory.get("domain", "unknown")
    agent = trajectory.get("agent", "unknown")
    ts = trajectory.get("timestamp", "")
    score = trajectory.get("task_score")
    score_str = f" score={score}" if score is not None else " (unscored)"
    error_flag = " ERROR" if trajectory.get("error") else ""
    title = f"Trajectory: {domain}/{trajectory.get('difficulty', '?')} - {agent}{score_str}{error_flag}"

    # Enrich observation with adversarial review findings (backward compatible)
    if "adversarial_review" in trajectory:
        review = trajectory["adversarial_review"]
        trajectory["_review_findings_count"] = len(review.get("findings", []))
        trajectory["_review_verdict"] = review.get("verdict", "?")
        trajectory["_review_score"] = review.get("score", 0.0)

    # Include grounded score components for Layer 3 pattern extraction
    if "grounded_score" in trajectory:
        gs = trajectory["grounded_score"]
        if isinstance(gs, dict):
            trajectory["_grounded_components"] = gs.get("components", {})
            trajectory["_grounded_total"] = gs.get("total", 0.0)

    msg_body = json.dumps(trajectory, ensure_ascii=False)

    try:
        result = subprocess.run(
            [ENGRAM_BIN, "save", title, msg_body,
             "--type", "trajectory",
             "--project", ENGRAM_PROJECT],
            capture_output=True, timeout=10, check=False,
            text=True,
        )
        if result.returncode != 0:
            sys.stderr.write(f"[engram] save failed: {result.stderr.strip()}\n")
            return None

        m = re.search(r"#(\d+)", result.stdout)
        obs_id = m.group(1) if m else None
        return obs_id
    except subprocess.TimeoutExpired:
        sys.stderr.write("[engram] save timed out\n")
        return None


def delete_observation(obs_id: str) -> bool:
    """Delete an observation by ID (soft-delete)."""
    try:
        result = subprocess.run(
            [ENGRAM_BIN, "delete", obs_id],
            capture_output=True, timeout=10, check=False,
            text=True,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def search_trajectories(
    query: str = "",
    domain: Optional[str] = None,
    limit: int = 20,
) -> list:
    """Search Engram for trajectory memories. Returns list of (obs_id, title, body_json) tuples."""
    if not engram_available():
        return []

    search_query = query or "trajectory"
    try:
        result = subprocess.run(
            [ENGRAM_BIN, "search", search_query,
             "--type", "trajectory",
             "--project", ENGRAM_PROJECT,
             "--limit", str(limit)],
            capture_output=True, timeout=10, check=False,
            text=True,
        )
        if result.returncode != 0 or not result.stdout.strip():
            return []

        memories = _parse_search_output(result.stdout)

        if domain:
            memories = [
                m for m in memories
                if domain in m[1] or domain in m[2]
            ]

        return memories[:limit]
    except subprocess.TimeoutExpired:
        return []


def get_stats() -> Optional[dict]:
    """Get Engram memory system stats."""
    if not engram_available():
        return None
    try:
        result = subprocess.run(
            [ENGRAM_BIN, "stats"],
            capture_output=True, timeout=10, check=False,
            text=True,
        )
        if result.returncode != 0:
            return None
        lines = result.stdout.strip().splitlines()
        stats = {}
        for line in lines:
            m = re.match(r"\s*(\w[\w\s/]*?):\s+(.+)", line)
            if m:
                key = m.group(1).strip().lower().replace(" ", "_")
                stats[key] = m.group(2).strip()
        return stats
    except subprocess.TimeoutExpired:
        return None


def _parse_search_output(stdout: str) -> list:
    """Parse engram search output into list of (obs_id, title, body) tuples."""
    memories = []
    current_id = None
    current_title = None
    current_body_lines = []

    for line in stdout.splitlines():
        m_start = re.match(r"\[(\d+)\]\s+#(\d+)\s+\(([^)]+)\)\s+[—–-]\s+(.+)", line)
        if m_start:
            if current_id and current_title:
                body = "\n".join(current_body_lines).strip()
                memories.append((current_id, current_title, body))
            current_id = m_start.group(2)
            current_title = m_start.group(4).strip()
            current_body_lines = []
            continue

        m_body = re.match(r"\s{4}(.*)", line)
        if m_body and current_id:
            current_body_lines.append(m_body.group(1))
        elif current_id and line.strip() and not line.startswith("Found"):
            pass

    if current_id and current_title:
        body = "\n".join(current_body_lines).strip()
        memories.append((current_id, current_title, body))

    return memories
