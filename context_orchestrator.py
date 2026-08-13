import hashlib
import os
import re
import subprocess
import sys
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Optional

# Default vault path. Resolves to <repo>/data/vault so the framework works
# out-of-the-box without a personal path. Override via the vault_path argument
# or by setting AGENT_SCHOOL_VAULT env var.


_CONTEXT_CACHE_MAX = 64
_CONTEXT_CACHE: OrderedDict[tuple, str] = OrderedDict()
_CONTEXT_CACHE_LOCK = threading.RLock()


def clear_context_cache() -> None:
    """Clear same-session context packets (primarily for tests and operators)."""
    with _CONTEXT_CACHE_LOCK:
        _CONTEXT_CACHE.clear()


def _repo_identity(repo_path: Optional[Path]) -> str:
    """Return a code-sensitive, non-persisted identity for cache keys."""
    if not repo_path:
        return "none"
    path = Path(repo_path).expanduser().resolve()
    try:
        git_marker = path / ".git"
        head = git_marker.read_text(errors="replace") if git_marker.is_file() else ""
        head_file = git_marker / "HEAD"
        if head_file.exists():
            head += head_file.read_text(errors="replace")
        index = git_marker / "index"
        stat = index.stat() if index.exists() else None
        return f"{path}:{head[:200]}:{stat.st_mtime_ns if stat else 0}:{stat.st_size if stat else 0}"
    except OSError:
        return str(path)


def _context_cache_key(domain, prompt, vault, top_k, session_id, repo_path) -> tuple:
    normalized = " ".join((prompt or "").split()).lower()
    prompt_id = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return (
        str(domain), prompt_id, str(Path(vault).expanduser().resolve()), int(top_k),
        str(session_id), _repo_identity(repo_path),
    )


def _cached_context(key: tuple) -> Optional[str]:
    with _CONTEXT_CACHE_LOCK:
        if key not in _CONTEXT_CACHE:
            return None
        value = _CONTEXT_CACHE.pop(key)
        _CONTEXT_CACHE[key] = value
        return value


def _store_context(key: tuple, value: str) -> None:
    with _CONTEXT_CACHE_LOCK:
        _CONTEXT_CACHE.pop(key, None)
        _CONTEXT_CACHE[key] = value
        while len(_CONTEXT_CACHE) > _CONTEXT_CACHE_MAX:
            _CONTEXT_CACHE.popitem(last=False)


def _probe_context_source(metrics, source: str, probe):
    """Run one optional context probe and report only hit/latency metadata."""
    if metrics is None:
        return probe()
    import time
    started = time.perf_counter()
    result = None
    try:
        result = probe()
        return result
    finally:
        metrics.record_context(
            source,
            hit=bool(result),
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )


_REPO_ROOT = Path(__file__).resolve().parent
# Default vault = repo root, where `ccc init` creates the .cocoindex_code/
# search index. ccc scopes results by cwd, so vault MUST be the directory
# containing .cocoindex_code/.  Override via AGENT_SCHOOL_VAULT env var.
DEFAULT_VAULT = Path(os.environ.get("AGENT_SCHOOL_VAULT", str(_REPO_ROOT)))

# Approximate char budget for Layer 3 archival context.
# Total context budget is ~10K chars; Layer 0 + Layer 1 can use ~2K,
# leaving ~8K. We cap Layer 3 at 4K to leave headroom.
LAYER_3_CHAR_BUDGET = 4000

# Char budget for Serena LSP symbol results. Layer 0 + Layer 1 share
# ~2K chars total, so we cap symbol locations at 600 chars.
SERENA_CHAR_BUDGET = 600


def enrich_prompt(
    domain: str,
    prompt: str,
    vault_path: Optional[Path] = None,
    top_k: int = 3,
    session_id: Optional[str] = None,
    repo_path: Optional[Path] = None,
    metrics=None,
) -> str:
    """Gather context from vault (CocoIndex) + past trajectories (Engram)
    + Serena (LSP symbol search) + archival consolidation (Layer 3) and
    return a string to append to the agent's system prompt.

    Args:
        repo_path: Optional path to the target repo for Serena symbol
                   lookups. Pass the repo clone path for code-heavy
                   domains to get exact symbol locations.

    Returns empty string on any failure (non-blocking by design)."""
    vault = vault_path or DEFAULT_VAULT
    cache_key = None
    if session_id:
        cache_key = _context_cache_key(
            domain, prompt, vault, top_k, session_id, repo_path,
        )
        cached = _cached_context(cache_key)
        if cached is not None:
            if metrics is not None:
                metrics.record_context("cache", hit=True)
            return cached

    # Probe order is fixed for deterministic rendering; execution is bounded
    # and concurrent because these sources do not depend on one another.
    probes = []
    if domain in ("code-review", "python-testing", "_default"):
        probes.append(("cocoindex", lambda: _cocoindex_context(prompt, vault, top_k)))
    if domain in ("code-implementation", "python-coding", "python-testing",
                  "code-review", "debugging", "_default"):
        probes.append(("serena", lambda: _serena_context(prompt, repo_path, top_k)))
    if domain in ("_default", "code-review", "python-testing", "git-operations"):
        probes.append(("engram", lambda: _engram_context(domain, prompt, top_k)))
    if session_id:
        probes.append(("archival", lambda: _archival_context(domain, session_id)))

    def _run_probe(item):
        source, probe = item
        try:
            return source, _probe_context_source(metrics, source, probe)
        except Exception as e:
            sys.stderr.write(f"[context] {source} failed: {e}\n")
            return source, None

    parts = []
    if probes:
        with ThreadPoolExecutor(max_workers=min(4, len(probes)), thread_name_prefix="school-context") as pool:
            results = list(pool.map(_run_probe, probes))
        for _source, ctx in results:
            if ctx:
                parts.append(ctx)

    if not parts:
        result = ""
        if cache_key is not None:
            _store_context(cache_key, result)
        return result

    combined = "\\n\n".join(parts)
    result = (
        "\n\n---\n### Context from Knowledge Vault\n"
        f"{combined}\n"
        "---"
    )
    if cache_key is not None:
        _store_context(cache_key, result)
    return result


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
    """Search past trajectory files for similar tasks (file-based RAG).

    Reads trajectory JSON files from ``data/trajectories/`` sorted by
    recency then filtered by score. Prefer scored trajectories first.
    """
    from trajectory import list_trajectories as _list_trajectories

    # Fetch recent trajectories for this domain
    trajs = _list_trajectories(domain=domain, limit=top_k * 3)
    if not trajs:
        return None

    # Sort: scored (desc) first, then unscored
    scored = [t for t in trajs if t.get('task_score') is not None and t['task_score'] > 0]
    unscored = [t for t in trajs if t.get('task_score') is None or t['task_score'] == 0]
    scored.sort(key=lambda t: t['task_score'], reverse=True)
    ordered = (scored + unscored)[:top_k]

    lines = ["**Past similar trajectories:**"]
    for traj in ordered:
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
        from consolidation_writer import (
            load_consolidation_for_domain,
            load_all_consolidation,
            load_latest_consolidation_for_domain,
        )

        # Prefer the current session's requested domain. If it is absent,
        # prefer the newest prior session for that same domain. Only when no
        # same-domain archive exists do we retain the older compatibility
        # fallback to an unrelated consolidation in the current session.
        data = load_consolidation_for_domain(session_id, domain)
        current_fallback = None
        if not data:
            all_data = load_all_consolidation(session_id)
            current_fallback = all_data[0] if all_data else None
            data = next(
                (d for d in all_data if d.get("domain") == domain),
                None,
            )
        if not data:
            data = load_latest_consolidation_for_domain(
                domain, exclude_session_id=session_id
            )
        if not data:
            data = current_fallback
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


def _serena_context(
    prompt: str,
    repo_path: Optional[Path] = None,
    top_k: int = 3,
) -> Optional[str]:
    """Layer 1 structural context via Serena's LSP symbol search.

    Extracts symbol-like identifiers from the prompt and resolves each
    to its exact file/line location. Complements CocoIndex's semantic
    search with precise symbol-level resolution.

    Non-blocking: returns ``None`` if Serena is unavailable or no
    symbols are found.
    """
    from serena_adapter import serena_available, find_symbol

    if not serena_available():
        return None

    # Extract potential symbol names from the prompt (CamelCase,
    # snake_case identifiers).
    candidates = _extract_symbol_names(prompt)
    if not candidates:
        return None

    resolved = []
    for name in candidates[:top_k]:
        try:
            result = find_symbol(name, project_path=repo_path)
        except Exception:
            continue
        if result is None:
            continue
        # Normalise: result may be a single dict or a list.
        # Filter out error wrappers (raw key = tool error message).
        items = result if isinstance(result, list) else [result]
        for item in items:
            if isinstance(item, dict) and "raw" not in item:
                resolved.append(item)

    if not resolved:
        return None

    lines = ["**Exact symbol locations (Serena LSP):**"]
    total_chars = len(lines[0])
    for r in resolved[:top_k]:
        # Normalise across Serena's native field names and generic formats.
        name = r.get("name_path") or r.get("name", "?")
        kind = r.get("kind", "")
        file_ = r.get("relative_path") or r.get("file", "?")
        body = r.get("body_location", {})
        line = body.get("start_line") if isinstance(body, dict) else r.get("line", r.get("start_line", "?"))
        kind_str = f" ({kind})" if kind else ""
        entry = f"- `{name}`{kind_str} → `{file_}:{line}`"
        if total_chars + len(entry) > SERENA_CHAR_BUDGET:
            lines.append("  ... (truncated)")
            break
        lines.append(entry)
        total_chars += len(entry)

    return "\n".join(lines)


def _extract_symbol_names(prompt: str) -> list[str]:
    """Extract CamelCase and snake_case identifiers from a prompt.

    Returns up to 5 unique identifier-like tokens, excluding common
    stopwords and very short tokens.
    """
    # Match CamelCase (both UpperCamelCase and lowerCamelCase),
    # snake_case, ALL_CAPS, and backtick-quoted identifiers.
    candidates = re.findall(
        r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)"  # UpperCamelCase
        r"|\b([a-z]+(?:[A-Z][a-z]+)+)"         # lowerCamelCase
        r"|\b([a-z]+(?:_[a-z]+){1,})"           # snake_case
        r"|\b([A-Z]{2,})"                        # ALL_CAPS
        ,
        prompt,
    )
    # Also capture backtick-quoted identifiers: `foo_bar`
    backticked = re.findall(r"`([a-zA-Z_][a-zA-Z0-9_]*)`", prompt)
    candidates.extend(backticked)

    # re.findall with multiple groups returns tuples; flatten to single values.
    flat: list[str] = []
    for t in candidates:
        if isinstance(t, tuple):
            flat.append("".join(t))
        else:
            flat.append(t)

    seen = set()
    unique = []
    for c in flat:
        c_lower = c.lower()
        if c_lower in seen or len(c) < 3:
            continue
        seen.add(c_lower)
        unique.append(c)

    return unique[:5]


