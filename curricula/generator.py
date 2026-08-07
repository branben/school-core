"""curricula/generator.py — AutoHarness: auto-generate curriculum YAML from high-scoring trajectories.

Scans trajectories_for_training(domain, min_score=70), groups by domain,
extracts action/subject patterns from prompts using hybrid rule-based parsing,
writes curriculum YAML files matching the existing curricula/*.yaml format,
and optionally registers them in curricula/index.yaml.

Usage:
    python -m curricula.generator               # dry-run (preview only)
    python -m curricula.generator --dry-run       # explicit dry-run
    python -m curricula.generator --domain python-testing  # single domain
    python -m curricula.generator --apply         # write files + update index
"""

import argparse
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from trajectory import TRAJECTORY_DIR, trajectories_for_training

logger = logging.getLogger(__name__)

CURRICULA_DIR = Path(__file__).parent

# Domains that should be excluded from auto-generated curricula
SKIP_DOMAINS = {"_default", "unknown", "triage-category", "debugging"}

# ── Domain-specific task templates (hybrid generation) ──────────────────────────

DOMAIN_TEMPLATES: dict[str, list[str]] = {
    "python-testing": [
        "Write a pytest test for {subject}",
        "Add test coverage for {subject}",
        "Fix the failing test for {subject}",
        "Parameterize a test to cover edge cases in {subject}",
        "Mock {subject} in a test using monkeypatch or unittest.mock",
        "Write an integration test for {subject}",
    ],
    "code-implementation": [
        "Implement {subject}",
        "Refactor {subject} to improve maintainability",
        "Extract {subject} into a named constant or function",
        "Add error handling for {subject}",
        "Fix a bug in {subject}",
    ],
    "python-coding": [
        "Write a Python function for {subject}",
        "Implement {subject}",
        "Refactor {subject}",
    ],
    "code-review": [
        "Review {subject} for correctness and security issues",
        "Find code smells in {subject} and suggest fixes",
        "Review a PR that changes {subject}",
    ],
    "git-operations": [
        "Use git to manage {subject}",
        "Resolve a merge conflict in {subject}",
        "Clean up commit history for {subject}",
    ],
}

DEFAULT_TEMPLATES = [
    "Implement {subject}",
    "Refactor {subject}",
    "Fix issues in {subject}",
]

# Direct-use actions — prompts that already read like valid task descriptions
# and should NOT be wrapped in a template.
_DIRECT_ACTIONS: set[str] = {
    "write", "implement", "create", "refactor", "review",
    "design", "migrate", "configure", "parameterize",
}

# Verb patterns for prompts that need template wrapping.
# Each entry: (compiled_regex, action_name, capture_group)
_TEMPLATE_PATTERNS: list[tuple[re.Pattern, str, int]] = [
    (re.compile(r"^extract\s+(.+)", re.IGNORECASE), "Extract", 1),
    (re.compile(r"^add\s+(.+)", re.IGNORECASE), "Add", 1),
    (re.compile(r"^fix\s+(.+)", re.IGNORECASE), "Fix", 1),
    (re.compile(r"^set\s+up\s+(.+)", re.IGNORECASE), "Set up", 1),
    (re.compile(r"^remove\s+(.+)", re.IGNORECASE), "Remove", 1),
    (re.compile(r"^update\s+(.+)", re.IGNORECASE), "Update", 1),
    (re.compile(r"^clean\s+up\s+(.+)", re.IGNORECASE), "Clean up", 1),
    (re.compile(r"^resolve\s+(.+)", re.IGNORECASE), "Resolve", 1),
    (re.compile(r"^triage\s+(.+)", re.IGNORECASE), "Triage", 1),
]

# Issue-style patterns — prompts that start with a noun phrase describing a bug.
# These get wrapped as "Fix bug in {short_subject}".
_ISSUE_PATTERNS: list[re.Pattern] = [
    # "Room creation fails with..." — capitalized noun phrase ending with failure keyword
    re.compile(r"^([A-Z][a-z]+ [A-Za-z]+ (?:fails?|error|bug|issue|problem|crash|broken)[^.\n]+)", re.IGNORECASE | re.DOTALL),
    # "Creating room fails with..." — leading -ing verb phrase
    re.compile(r"^([A-Z][a-z]+ing [A-Za-z]+ [A-Za-z]+ (?:fails?|error|bug|issue|problem|crash|broken)[^.\n]+)", re.IGNORECASE | re.DOTALL),
]

# ── Prompt parsing helpers ─────────────────────────────────────────────────────


def _normalize_prompt(prompt: str) -> str:
    """Extract a clean, parseable first line from a raw trajectory prompt."""
    first_line = prompt.strip().split("\n")[0]
    first_line = re.sub(r"^#+\s*", "", first_line)
    first_line = re.sub(r"^(Task|Issue|Problem|Goal|Requirement|Title):\s*", "", first_line, flags=re.IGNORECASE)
    first_line = re.sub(r"\s*\([^)]*\)", "", first_line)
    first_line = first_line.strip("\"' \t")
    return first_line


def _truncate(text: str, max_len: int = 70) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def _clean_subject(subject: str) -> str:
    """Clean extracted subject text."""
    subject = re.sub(r"^(the|a|an)\s+", "", subject, flags=re.IGNORECASE)
    subject = subject.strip().rstrip(".,;:!?").strip("\"'")
    return _truncate(subject)


def _parse_prompt(prompt: str) -> tuple[str, str]:
    """Parse a prompt into (action, subject_or_text).

    Returns
    -------
    (action, subject_or_text) where:
    - If action is a *direct* action (e.g. "WRITE", "IMPLEMENT"), the
      second value IS the full task description and should NOT be template-wrapped.
    - If action is a template action (e.g. "Extract", "Fix", "Add"), the
      second value is the *subject* to wrap in a template.
    - Falls back to ("Work on", truncated prompt).
    """
    normalized = _normalize_prompt(prompt)
    if not normalized:
        return "Work on", "a coding task"

    # Skip very short or generic prompts
    if len(normalized) < 10 or normalized.lower() in ("route 0", "chore", "fix", "test"):
        return "Work on", "a coding task"

    lower = normalized.lower()
    first_word = lower.split()[0] if lower.split() else ""

    # DIRECT: prompt starts with a recognized action verb → use as-is
    if first_word in _DIRECT_ACTIONS:
        return first_word.upper(), _truncate(normalized)

    # TEMPLATE: known wrap-able patterns
    for pattern, action, group_idx in _TEMPLATE_PATTERNS:
        match = pattern.match(normalized)
        if match:
            subject = match.group(group_idx)
            return action, _clean_subject(subject)

    # ISSUE: noun-phrase description ("Room creation fails..." → "Fix bug in Room creation")
    for pattern in _ISSUE_PATTERNS:
        match = pattern.search(normalized)
        if match:
            return "Fix bug in", _clean_subject(match.group(1))

    # Fallback: first few words
    preview = _truncate(normalized.rstrip("."))
    if len(preview) < 60:
        return "Work on", preview
    return "Work on", _truncate(normalized)


def _generate_task_description(action: str, subject_or_text: str, domain: str) -> str:
    """Generate a clean task description.

    - DIRECT actions: return subject_or_text as-is (no template wrapping).
    - Template actions: wrap subject in a domain-appropriate template.
    - Special actions like "Fix bug in": use the specific template.
    """
    # DIRECT: prompt is already a valid task description
    # _parse_prompt returns uppercased action for direct-use prompts
    if action.isupper() and action.lower() in _DIRECT_ACTIONS:
        return subject_or_text

    # Fix bug in / Work on: use domain templates
    templates = DOMAIN_TEMPLATES.get(domain, DEFAULT_TEMPLATES)
    action_lower = action.lower()

    if action_lower == "fix bug in":
        return f"Fix a bug where {subject_or_text}"

    # Pick best matching template
    candidates = [t for t in templates if action_lower in t.lower()]
    if not candidates:
        candidates = templates

    task = candidates[0].replace("{subject}", subject_or_text).replace("{action}", action_lower)
    return task


# ── Gate calculation ───────────────────────────────────────────────────────────

def calculate_gates(trajectories: list[dict]) -> list[dict]:
    """Calculate gate thresholds from score distribution.

    Trajectories are sorted by score, then split into evenly-sized groups.
    Each group becomes a gate whose ``score_required`` is the lowest score
    in the group (rounded down to the nearest 5). The first gate is always 0.

    Returns a list of gate dicts::

        {
            "score_required": int,
            "description": str,
            "trajectories": list[dict],
        }
    """
    if not trajectories:
        return []

    sorted_traj = sorted(trajectories, key=lambda t: t.get("task_score", 0) or 0, reverse=True)
    n = len(sorted_traj)

    if n <= 3:
        n_gates = 1
    elif n <= 8:
        n_gates = 2
    elif n <= 15:
        n_gates = 3
    else:
        n_gates = 4

    group_size = max(1, n // n_gates)
    gates = []

    GATE_DESCRIPTIONS = [
        "Foundation — tasks derived from proven high-scoring trajectories",
        "Intermediate — apply patterns from successful solutions",
        "Advanced — complex problems requiring deeper understanding",
        "Expert — mastery-level challenges combining multiple techniques",
    ]

    for i in range(n_gates):
        start = i * group_size
        end = start + group_size if i < n_gates - 1 else n
        group = sorted_traj[start:end]
        if not group:
            continue

        min_in_group = min((t.get("task_score", 0) or 0) for t in group)
        threshold = max(0, int(min_in_group // 5) * 5)

        gates.append({
            "score_required": threshold,
            "description": GATE_DESCRIPTIONS[i],
            "trajectories": group,
        })

    # Ensure first gate starts at 0 so beginners can access it
    if gates and gates[0]["score_required"] > 0:
        gates[0]["score_required"] = 0

    return gates


# ── Task generation per gate ───────────────────────────────────────────────────

def generate_tasks(gate: dict, domain: str, max_tasks: int = 5) -> list[str]:
    """Generate deduplicated task descriptions from trajectories in a gate."""
    seen = set()
    tasks = []
    for traj in gate["trajectories"]:
        prompt = traj.get("prompt", "")
        action, subject = _parse_prompt(prompt)
        task = _generate_task_description(action, subject, domain)

        # Skip tasks that are too generic
        if task.lower().strip() in ("work on a coding task", "implement"):
            continue
        if task not in seen:
            seen.add(task)
            tasks.append(task)
        if len(tasks) >= max_tasks:
            break
    return tasks


# ── Full curriculum generation ─────────────────────────────────────────────────

def generate_curriculum(
    domain: str,
    min_score: float = 70.0,
    output_dir: Optional[Path] = None,
    dry_run: bool = True,
) -> Optional[str]:
    """Generate a curriculum YAML for *domain* using qualifying trajectories.

    Parameters
    ----------
    domain:
        Domain name (e.g. ``"python-testing"``).
    min_score:
        Minimum trajectory task_score to qualify (default ``70.0``).
    output_dir:
        Directory to write the YAML file (default: ``curricula/``).
    dry_run:
        If True (default), return YAML string without writing files.
        If False, write the YAML and return the filename.

    Returns
    -------
    YAML content string (dry_run) or filename (applied), or None if
    insufficient trajectories or domain is excluded.
    """
    if domain in SKIP_DOMAINS:
        logger.info("Domain %r is in SKIP_DOMAINS, skipping", domain)
        return None

    eligible = trajectories_for_training(domain, min_score=min_score)
    if len(eligible) < 3:
        logger.info("Domain %r: %d eligible trajectories (need >= 3), skipping", domain, len(eligible))
        return None

    gates = calculate_gates(eligible)
    if not gates:
        return None

    date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    gate_steps = []
    for gate in gates:
        tasks = generate_tasks(gate, domain)
        if not tasks:
            continue
        gate_steps.append({
            "score_required": gate["score_required"],
            "description": gate["description"],
            "task_count": len(tasks),
            "tasks": tasks,
            "evaluation_rubric": {
                "task_score": 70,
                "conditions": [
                    "Solution compiles or runs without errors",
                    "Follows domain best practices",
                    "Handles edge cases appropriately",
                ],
            },
        })

    if not gate_steps:
        return None

    curriculum = {
        "domain": f"auto-{domain}-{date_str}",
        "description": f"Auto-generated curriculum from {len(eligible)} high-scoring trajectories in {domain}",
        "version": 1,
        "gate_steps": gate_steps,
    }

    yaml_content = yaml.dump(curriculum, default_flow_style=False, sort_keys=False, allow_unicode=True)

    if dry_run:
        return yaml_content

    output_path = (output_dir or CURRICULA_DIR) / f"auto-{domain}-{date_str}.yaml"
    output_path.write_text(yaml_content)
    logger.info("Wrote curriculum: %s", output_path)
    return output_path.name


def generate_all(
    min_score: float = 70.0,
    output_dir: Optional[Path] = None,
    dry_run: bool = True,
) -> dict[str, str]:
    """Generate curricula for every domain with enough qualifying trajectories.

    Returns dict of ``{domain: result}`` where result is YAML string
    (dry_run) or filename (applied).
    """
    from trajectory import count_trajectories
    all_counts = count_trajectories()

    results = {}
    for domain in sorted(all_counts):
        result = generate_curriculum(domain, min_score=min_score, output_dir=output_dir, dry_run=dry_run)
        if result:
            results[domain] = result

    return results


# ── Index update ───────────────────────────────────────────────────────────────

def update_index(new_entries: list[dict]) -> None:
    """Append new curriculum entries to ``curricula/index.yaml``.

    Each entry in *new_entries* should have keys:
    ``domain``, ``filename``, ``description``, ``gates``.
    """
    index_path = CURRICULA_DIR / "index.yaml"
    if not index_path.exists():
        logger.error("Index file not found: %s", index_path)
        return

    with open(index_path) as f:
        index = yaml.safe_load(f) or {"curricula": {}}

    for entry in new_entries:
        domain_key = entry["domain"]
        index.setdefault("curricula", {})[domain_key] = {
            "file": entry["filename"],
            "description": entry["description"],
            "gates": entry.get("gates", [0, 25, 50, 75]),
            "prerequisites": [],
        }

    with open(index_path, "w") as f:
        yaml.dump(index, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    logger.info("Updated index.yaml with %d new entries", len(new_entries))


# ── CLI ────────────────────────────────────────────────────────────────────────

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="AutoHarness: generate curriculum YAML from high-scoring trajectories",
    )
    parser.add_argument("--domain", help="Specific domain (default: all with enough trajectories)")
    parser.add_argument("--min-score", type=float, default=70.0, help="Minimum trajectory score (default: 70)")
    parser.add_argument("--dry-run", action="store_true", default=True, help="Preview without writing (default)")
    parser.add_argument("--apply", action="store_true", help="Write files and update index")
    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")

    dry_run = not args.apply  # --apply overrides default dry-run
    output_dir = CURRICULA_DIR

    if args.domain:
        result = generate_curriculum(args.domain, min_score=args.min_score, output_dir=output_dir, dry_run=dry_run)
        results = {args.domain: result} if result else {}
    else:
        results = generate_all(min_score=args.min_score, output_dir=output_dir, dry_run=dry_run)

    if not results:
        logger.info("No curricula generated (insufficient high-scoring trajectories)")
        return

    if dry_run:
        sep = "=" * 60
        print(f"\n{sep}")
        print(f"AUTOHARNESS DRY-RUN — {len(results)} curricula would be generated")
        print(f"{sep}\n")
        for domain, yaml_content in sorted(results.items()):
            if yaml_content:
                print(f"--- auto-{domain} ---")
                print(yaml_content)
        print(f"\n{sep}")
        print("Run with --apply to write files and update index.yaml")
        print(f"{sep}")
    else:
        new_entries = []
        for domain, filename in sorted(results.items()):
            filepath = output_dir / filename
            with open(filepath) as f:
                data = yaml.safe_load(f)
            gates = [step["score_required"] for step in data.get("gate_steps", [])]
            new_entries.append({
                "domain": data["domain"],
                "filename": filename,
                "description": data["description"],
                "gates": gates,
            })

        update_index(new_entries)
        logger.info("Generated %d curricula and updated index.yaml", len(results))


if __name__ == "__main__":
    main()
