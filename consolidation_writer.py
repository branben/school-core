"""Consolidation Writer — U7-1.

Writes high-value observations from Layer 2 (Engram/episodic) to
Layer 3 (YAML archival) during sleep/wake consolidation.
"""

import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from engram_adapter import engram_available, search_trajectories

CONSOLIDATION_DIR = Path(__file__).parent / "data" / "sessions" / "consolidation"


def _ensure_dir(session_id: str) -> Path:
    session_dir = CONSOLIDATION_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def write_consolidation(
    session_id: str,
    domain: str,
    observations: list[dict],
) -> Optional[Path]:
    """Write high-value observations from Layer 2 to Layer 3 archival YAML.

    Reads recent Engram observations for the given session/domain,
    extracts high-value patterns, and writes a YAML summary.

    Args:
        session_id: The session identifier.
        domain: The domain (e.g. "python-testing", "code-review").
        observations: Raw episodic observations from Engram.

    Returns:
        Path to the written YAML file, or None on failure (non-blocking).
    """
    if not observations:
        observations = _fetch_from_engram(domain)
        if not observations:
            return None

    try:
        session_dir = _ensure_dir(session_id)

        # Extract patterns
        patterns = _extract_patterns(observations)
        key_learnings = _extract_key_learnings(observations)
        error_recurrence = _count_error_recurrence(observations)

        data = {
            "session_id": session_id,
            "domain": domain,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "patterns": patterns,
            "key_learnings": key_learnings,
            "error_recurrence": error_recurrence,
        }

        filepath = session_dir / f"{domain}.yaml"
        with open(filepath, "w", encoding="utf-8") as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        return filepath
    except Exception as e:
        sys.stderr.write(f"[consolidation] write failed for {session_id}/{domain}: {e}\n")
        return None


def load_consolidation_for_domain(
    session_id: str,
    domain: str,
) -> Optional[dict]:
    """Load a consolidation YAML for a given session + domain."""
    filepath = CONSOLIDATION_DIR / session_id / f"{domain}.yaml"
    if not filepath.exists():
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data
    except Exception as e:
        sys.stderr.write(f"[consolidation] load failed for {session_id}/{domain}: {e}\n")
        return None


def load_all_consolidation(session_id: str) -> list[dict]:
    """Load all consolidation YAMLs for a session. Returns list of dicts."""
    session_dir = CONSOLIDATION_DIR / session_id
    if not session_dir.exists():
        return []
    results = []
    for yaml_path in session_dir.glob("*.yaml"):
        try:
            with open(yaml_path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data is not None:
                results.append(data)
        except Exception as e:
            sys.stderr.write(f"[consolidation] load failed for {yaml_path}: {e}\n")
    return results


def _fetch_from_engram(domain: str) -> list[dict]:
    """Fetch recent episodic observations from Engram for a domain."""
    try:
        results = search_trajectories(domain=domain, limit=20)
        observations = []
        for obs_id, title, body_json in results:
            try:
                traj = json.loads(body_json)
                observations.append(traj)
            except json.JSONDecodeError:
                continue
        return observations
    except Exception:
        return []


def _extract_patterns(observations: list[dict]) -> list[str]:
    """Extract recurring patterns from a list of episodic observations."""
    if not observations:
        return []

    patterns = []

    # Domain activity pattern
    domain_counts = Counter(o.get("domain", "unknown") for o in observations)
    for domain, count in domain_counts.most_common():
        if count > 1:
            patterns.append(f"Frequent domain: {domain} ({count} occurrences)")

    # Success vs error ratio
    statuses = Counter(o.get("status", "unknown") for o in observations)
    total = len(observations)
    successes = statuses.get("success", 0)
    errors = statuses.get("error", 0)
    if total > 0:
        patterns.append(f"Success rate: {successes}/{total} ({100 * successes / total:.0f}%)")
    if errors > 0:
        patterns.append(f"Error rate: {errors}/{total} ({100 * errors / total:.0f}%)")

    # Score trend
    scores = [o.get("task_score") for o in observations if o.get("task_score") is not None]
    if len(scores) >= 2:
        avg = sum(scores) / len(scores)
        patterns.append(f"Average score: {avg:.1f} across {len(scores)} scored tasks")

    # Adversarial findings patterns
    adv_findings = []
    for o in observations:
        review = o.get("adversarial_review")
        if review and isinstance(review, dict):
            findings = review.get("findings", [])
            if findings:
                adv_findings.extend(findings)
    if adv_findings:
        issue_classes = Counter(f.get("issue_class", "unknown") for f in adv_findings)
        for issue_class, count in issue_classes.most_common(3):
            patterns.append(f"Adversarial: {issue_class} ({count} findings)")

    return patterns


def _extract_key_learnings(observations: list[dict]) -> list[str]:
    """Extract key decisions and successful strategies from observations."""
    learnings = []
    for o in observations:
        if o.get("decision"):
            learnings.append(o["decision"])
        if o.get("status") == "success" and o.get("strategy"):
            learnings.append(f"Strategy: {o['strategy']}")
        # Extract grounded score details
        grounded = o.get("grounded_score")
        if grounded and isinstance(grounded, dict):
            components = grounded.get("components", {})
            if components:
                top_component = max(components, key=lambda k: components[k])
                learnings.append(f"Strongest dimension: {top_component} ({components[top_component]:.1f})")
    return learnings


def _count_error_recurrence(observations: list[dict]) -> dict[str, int]:
    """Count recurring error types across observations."""
    errors: dict[str, int] = Counter()
    for o in observations:
        err = o.get("error")
        if err:
            errors[err] = errors.get(err, 0) + 1
        # Also check for adversarial review severities
        review = o.get("adversarial_review")
        if review and isinstance(review, dict):
            for f in review.get("findings", []):
                sev = f.get("severity", "LOW")
                if sev in ("CRITICAL", "HIGH"):
                    ic = f.get("issue_class", "unknown")
                    error_key = f"{sev}:{ic}"
                    errors[error_key] = errors.get(error_key, 0) + 1
    return dict(errors.most_common(10))
