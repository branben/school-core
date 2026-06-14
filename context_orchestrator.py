import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional

DEFAULT_VAULT = Path("~/Documents/Knowledge Core")

# Approximate char budget for Layer 3 archival context.
# Total context budget is ~10K chars; Layer 0 + Layer 1 can use ~2K,
# leaving ~8K. We cap Layer 3 at 4K to leave headroom.
LAYER_3_CHAR_BUDGET = 4000


def enrich_prompt(
    domain: str,
    prompt: str,
    vault_path: Optional[Path] = None,
    top_k: int = 3,
    session_id: Optional[str] = None,
) -> str:
    """Gather context from vault (CocoIndex) + past trajectories (Engram)
    + archival consolidation (Layer 3) and return a string to append
    to the agent's system prompt.
    Returns empty string on any failure (non-blocking by design)."""
    vault = vault_path or DEFAULT_VAULT
    parts = []

    # 1. Structural context from vault via CocoIndex (code-review, python-testing)
    if domain in ("code-review", "python-testing", "_default"):
        try:
            ctx = _cocoindex_context(prompt, vault, top_k)
            if ctx:
                parts.append(ctx)
        except Exception as e:
            sys.stderr.write(f"[context] cocoindex failed: {e}\n")

    # 2. Temporal context from Engram (all domains) — similar past trajectories
    if domain in ("_default", "code-review", "python-testing", "git-operations"):
        try:
            ctx = _engram_context(domain, prompt, top_k)
            if ctx:
                parts.append(ctx)
        except Exception as e:
            sys.stderr.write(f"[context] engram failed: {e}\n")

    # 3. Archival context from Layer 3 consolidation YAML files
    if session_id:
        try:
            ctx = _archival_context(domain, session_id)
            if ctx:
                parts.append(ctx)
        except Exception as e:
            sys.stderr.write(f"[context] archival failed: {e}\n")

    if not parts:
        return ""

    combined = "\\n\n".join(parts)
    return (
        "\n\n---\n### Context from Knowledge Vault\n"
        f"{combined}\n"
        "---"
    )


def _cocoindex_context(prompt: str, vault: Path, top_k: int) -> Optional[str]:
    """Search the vault with CocoIndex and return formatted snippets."""
    try:
        result = subprocess.run(
            ["ccc", "search", prompt, "--limit", str(top_k)],
            capture_output=True, timeout=30, check=False,
            text=True, cwd=str(vault),
        )
    except FileNotFoundError:
        sys.stderr.write("[context] ccc not found\n")
        return None
    except subprocess.TimeoutExpired:
        sys.stderr.write("[context] ccc search timed out\n")
        return None

    if result.returncode != 0 or not result.stdout.strip():
        return None

    snippets = _parse_cocoindex_output(result.stdout, max_snippets=top_k)
    if not snippets:
        return None

    lines = ["**Relevant files from vault:**"]
    for snippet in snippets:
        lines.append(f"- `{snippet['file']}` (relevance: {snippet['score']:.2f})")
        content_preview = snippet["content"].strip()
        if len(content_preview) > 300:
            content_preview = content_preview[:300] + "..."
        lines.append(f"  ```\n  {content_preview}\n  ```")

    return "\n".join(lines)


def _engram_context(domain: str, prompt: str, top_k: int) -> Optional[str]:
    """Search past trajectories in Engram for similar tasks."""
    from engram_adapter import search_trajectories as engram_search

    # Extract key terms from the prompt for search
    search_terms = _extract_key_terms(prompt)
    if not search_terms:
        return None

    results = engram_search(search_terms, limit=top_k)
    if not results:
        return None

    lines = ["**Past similar trajectories:**"]
    for obs_id, title, body_json in results:
        try:
            traj = json.loads(body_json)
        except json.JSONDecodeError:
            continue
        ts = traj.get("timestamp", "?")[:19]
        agent = traj.get("agent", "?")
        score = traj.get("task_score", "?")
        response_preview = (traj.get("response") or "")[:200]
        lines.append(f"- [{ts}] **{agent}** (score={score})")
        if response_preview:
            lines.append(f"  > {response_preview}")

    return "\n".join(lines) if len(lines) > 1 else None


def _parse_cocoindex_output(stdout: str, max_snippets: int) -> list:
    """Parse ccc search output into structured snippets."""
    snippets = []
    current = {}

    for line in stdout.splitlines():
        m_header = re.match(r"--- Result (\d+) \(score: ([\d.]+)\) ---", line)
        if m_header:
            if current and current.get("file"):
                snippets.append(current)
            current = {"score": float(m_header.group(2)), "file": "", "content": ""}
            continue

        m_file = re.match(r"File:\s+(.+?)(?::(\d+)-(\d+))?\s*\[(\w+)\]", line)
        if m_file and current is not None:
            current["file"] = m_file.group(1)
            current["lang"] = m_file.group(4)
            continue

        if current is not None and "file" in current:
            current.setdefault("content", "")
            current["content"] += line + "\n"

    if current and current.get("file"):
        snippets.append(current)

    return snippets[:max_snippets]


def _archival_context(domain: str, session_id: str) -> Optional[str]:
    """Load Layer 3 archival consolidation YAML files for context enrichment.

    Non-blocking: returns "" if no consolidation files or errors occur.
    Respects LAYER_3_CHAR_BUDGET to avoid blowing the 10K context limit.
    """
    try:
        from consolidation_writer import load_consolidation_for_domain, load_all_consolidation

        # Try domain-specific first, then fall back to all for this session
        data = load_consolidation_for_domain(session_id, domain)
        if not data:
            all_data = load_all_consolidation(session_id)
            data = next(
                (d for d in all_data if d.get("domain") == domain),
                all_data[0] if all_data else None,
            )
        if not data:
            return None

        lines = ["**Archival patterns from past sessions:**"]

        patterns = data.get("patterns", [])
        if patterns:
            lines.append("Patterns:")
            for p in patterns[:5]:
                lines.append(f"  - {p}")

        learnings = data.get("key_learnings", [])
        if learnings:
            lines.append("Key learnings:")
            for l in learnings[:5]:
                lines.append(f"  - {l}")

        errors = data.get("error_recurrence", {})
        if errors:
            lines.append("Recurring errors:")
            for err, count in list(errors.items())[:5]:
                lines.append(f"  - {err}: {count}x")

        result = "\n".join(lines)
        if len(result) > LAYER_3_CHAR_BUDGET:
            result = result[:LAYER_3_CHAR_BUDGET] + "\n  ... (truncated)"
        return result if len(lines) > 1 else None
    except Exception as e:
        sys.stderr.write(f"[context] archival load failed: {e}\n")
        return None


def _extract_key_terms(prompt: str) -> str:
    """Extract meaningful search terms from a prompt, max ~5 words."""
    cleaned = re.sub(r'[^\w\s]', ' ', prompt)
    words = cleaned.split()
    # Filter out very common words
    stopwords = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been",
        "has", "have", "had", "do", "does", "did", "will", "would",
        "could", "should", "may", "might", "can", "shall", "to", "of",
        "in", "for", "on", "with", "at", "by", "from", "as", "into",
        "through", "during", "before", "after", "above", "below",
        "between", "out", "off", "over", "under", "again", "further",
        "then", "once", "here", "there", "when", "where", "why",
        "how", "all", "each", "every", "both", "few", "more", "most",
        "other", "some", "such", "no", "nor", "not", "only", "own",
        "same", "so", "than", "too", "very", "just", "because",
        "and", "but", "or", "if", "while", "that", "this", "these",
        "those", "it", "its", "reply", "exactly", "please", "check",
        "review", "write", "fix", "add", "remove", "update", "change",
    }
    terms = [w.lower() for w in words if w.lower() not in stopwords and len(w) > 2]
    # Prefer unique terms, limit to 5
    seen = set()
    unique = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            unique.append(t)
    return " ".join(unique[:5])
