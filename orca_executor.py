"""
orca_executor.py — Execute student code and manage worktrees in Orca.

Three main capabilities:

1. **Code execution**: Uses Orca CLI to create disposable terminal sessions
   for running student code extracted from LLM responses. Exit codes are
   captured inline via `echo "XEXITCODE:$?"` marker.

2. **Hermes agent execution**: Runs Hermes AI agent inside an Orca terminal
   with one-shot mode (`hermes chat -q --yolo --quiet --max-turns 1`).
   Output is captured via file redirect + XEXITCODE polling.

3. **Worktree lifecycle**: Creates proper Orca child worktrees (visible in
   Orca's UI sidebar) for per-round student isolation. Each worktree gets
   a structured StudentBrief + output files for audit trail.

Safety:
  - Code runs in an Orca terminal (same user, disposable session)
  - Hard timeout on all execution (default 30s for code, 120s for Hermes)
  - Temp files scoped by bead, cleaned up after execution
  - Terminal closed after each task (no state leakage)

Usage:
    from orca_executor import OrcaExecutionManager, CodeExtractor, StudentBrief

    manager = OrcaExecutionManager()
    code = CodeExtractor.extract("```python\nprint('hi')\n```")
    result = manager.execute(code=code, bead="task-abc")

    path = manager.create_worktree("study-coder-r1")
    manager.write_student_brief(path, StudentBrief(...))

    response = manager.run_hermes(worktree_path=path, bead="task-abc", task="Write a palindrome function")
"""

from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ── Exceptions ────────────────────────────────────────────────────────────────


class OrcaUnavailableError(Exception):
    """Raised when Orca runtime is not available or cannot be started.

    This is a hard failure — the pipeline cannot execute student code
    without an Orca terminal. Callers should abort the task and report
    the error rather than silently falling back to prose-only review.
    """
    pass


# ── Data Structures ───────────────────────────────────────────────────────────


@dataclass
class StudentBrief:
    """Structured student brief per faculty-orchestrator contract.

    Written into a student's worktree before dispatch so the student has
    a clear task description, guardrails, expected output format, and
    completion signal. Mirrors the contract from student-contract.md.
    """

    bead: str
    role: str
    domain: str
    task: str
    difficulty: str = "easy"
    guardrails: list[str] = None
    expected_output_format: str = "prose_answer"
    verification: str = "none"
    completion_signal: str = ""

    def to_dict(self) -> dict:
        return {
            "bead": self.bead,
            "role": self.role,
            "domain": self.domain,
            "difficulty": self.difficulty,
            "task": self.task,
            "guardrails": self.guardrails or [
                "Work within this worktree only",
                "No destructive operations",
                "No git push or external API calls",
            ],
            "expected_output_format": self.expected_output_format,
            "verification": self.verification,
            "completion_signal": self.completion_signal or f".hermes/outputs/{self.bead}.json",
        }


@dataclass
class ExecutionResult:
    """Result of executing student code in an Orca terminal."""

    stdout: str
    stderr: str
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int
    error: Optional[str] = None

    @property
    def passed(self) -> bool:
        """Code executed successfully with no errors."""
        return self.exit_code == 0 and not self.timed_out and self.error is None


# ── Code Extraction ───────────────────────────────────────────────────────────


class CodeExtractor:
    """Extract runnable code from LLM responses.

    LLMs often wrap code in ``` fences with optional language tags.
    This class strips those fences and returns clean, runnable code.
    """

    @staticmethod
    def extract(response: str, language: Optional[str] = None) -> str:
        """Extract runnable code from an LLM response."""
        code = CodeExtractor._strip_fences(response, language)
        if code:
            return code

        code = CodeExtractor._strip_fences(response, None)
        if code:
            return code

        return response.strip()

    @staticmethod
    def language_for_domain(domain: str) -> Optional[str]:
        """Map a task domain to the expected code language."""
        domain_lang = {
            "python-coding": "python",
            "python-testing": "python",
            "code-implementation": "python",
            "terminal": "bash",
            "git-operations": "bash",
        }
        return domain_lang.get(domain)

    @staticmethod
    def _strip_fences(text: str, language: Optional[str] = None) -> Optional[str]:
        """Strip markdown ``` fences from text."""
        if language:
            pattern = rf"```{language}\n(.*?)\n```"
        else:
            pattern = r"```(?:\w+)?\n(.*?)\n```"

        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()
        return None


# ── Orca Execution Manager ────────────────────────────────────────────────────


class OrcaExecutionManager:
    """Manages disposable Orca terminal sessions for code execution.

    Creates terminals directly in the current Orca project context
    (no worktree creation), executes code extracted from LLM responses,
    and returns structured results with stdout, stderr, and exit codes.
    """

    TEMP_BASE = Path("/tmp/school-exec")

    EXECUTABLE_DOMAINS = frozenset({
        "python-coding",
        "python-testing",
        "code-implementation",
        "git-operations",
        "terminal",
    })

    DEFAULT_TIMEOUT_MS = 30000
    HERMES_TIMEOUT_MS = 120000  # 2 min for one-shot Hermes calls
    POLL_INTERVAL = 0.5  # seconds between output checks

    # Shell prompt pattern: user@hostname path %
    SHELL_PROMPT_RE = re.compile(r".+@.+\s+\S*\s*[%$#]\s*$")
    EXIT_CODE_RE = re.compile(r"^XEXITCODE:(\d+)$")

    def __init__(self):
        """Initialize Orca connection.

        Verifies Orca runtime is running and ready. Does NOT create
        a worktree — terminals are created in the current project context.

        Raises OrcaUnavailableError if Orca is not running.
        """
        self._ensure_orca_ready()

    # ── School-core project path for worktree creation ───────────────────────
    REPO_PATH = Path(__file__).parent.expanduser().resolve()

    # ── Orca CLI helpers ──────────────────────────────────────────────────────

    def _run_orca(self, args: list[str], timeout: int = 15) -> dict:
        """Run an Orca CLI command with --json and parse the output.

        Returns the payload with the outer 'result' key unwrapped.

        Raises OrcaUnavailableError on any failure.
        """
        cmd = ["orca"] + args + ["--json"]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise OrcaUnavailableError(
                f"Orca CLI timed out ({timeout}s): {' '.join(args)}"
            )
        except FileNotFoundError:
            raise OrcaUnavailableError("Orca CLI not found (is Orca installed?)")

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or str(proc.returncode))[:300]
            raise OrcaUnavailableError(
                f"Orca command failed (exit={proc.returncode}): {detail}"
            )

        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError as e:
            raise OrcaUnavailableError(
                f"Orca JSON parse error: {e}\nRaw output: {proc.stdout[:200]}"
            )

        if isinstance(data, dict) and data.get("error"):
            raise OrcaUnavailableError(f"Orca error: {data['error']}")

        if isinstance(data, dict) and "result" in data:
            return data["result"]

        return data

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def _ensure_orca_ready(self) -> None:
        """Verify Orca runtime is running and ready."""
        result = self._run_orca(["status"])
        runtime = result.get("runtime", {})
        runtime_state = runtime.get("state", "unknown")
        if runtime_state != "ready":
            raise OrcaUnavailableError(
                f"Orca runtime not ready (state={runtime_state}). "
                f"Run 'orca open' first."
            )

    # ── Worktree lifecycle ────────────────────────────────────────────────────

    def _register_repo(self, repo_path: Path) -> Optional[str]:
        """Ensure a repo path is known to Orca; return its repo id.

        Orca requires a repo to be registered (``orca repo add --path``)
        before ``worktree create --repo <path>`` will accept it. A fresh
        cross-repo clone is not registered, so this is mandatory for
        cross-repo dispatch. Idempotent: if already listed, reuse the id.

        Args:
            repo_path: Absolute path to the local clone.

        Returns:
            The Orca repo id, or None if registration failed.
        """
        try:
            listed = self._run_orca(["repo", "list"], timeout=15)
        except OrcaUnavailableError:
            return None
        repos = listed.get("repos", listed.get("repositories", []))
        for r in repos if isinstance(repos, list) else []:
            if isinstance(r, dict) and Path(str(r.get("path", ""))).resolve() == Path(repo_path).resolve():
                return r.get("id")
        # Not registered — add it.
        try:
            added = self._run_orca(["repo", "add", "--path", str(repo_path)], timeout=30)
        except OrcaUnavailableError:
            return None
        if isinstance(added, dict):
            return added.get("id") or added.get("repo", {}).get("id")
        return None

    def create_worktree(self, name: str, repo_path: Optional[Path] = None) -> str:
        """Create a child worktree for a student round.

        Uses the native one-call ``orca worktree create --name <name> --repo
        <path>`` contract (verified against the live Orca runtime: the worktree
        is created as a fresh checkout under ``~/orca/workspaces/…`` and the
        returned ``id`` is ``"<uuid>::<path>"``). These worktrees appear in
        Orca's UI sidebar.

        Each student round gets its own worktree for:
        - Isolation: task files, briefs, and outputs scoped per round
        - Visibility: appears in Orca's UI sidebar
        - Audit trail: each round's state preserved on disk

        IMPORTANT: the created worktree lives at Orca's checkout path, NOT
        inside ``REPO_PATH``. Always use the returned ``path`` for any file
        operations inside the worktree.

        Cross-repo dispatch: pass ``repo_path`` (the cloned target repo, e.g.
        from ``repo_reader.clone_repo(owner/repo, force_fresh=True)``) to scope
        the worktree to the target repo. When omitted, falls back to
        ``REPO_PATH`` (single-repo / school-core mode).

        Args:
            name: Worktree name (e.g., "study-coder-r1").
            repo_path: Optional target repo path for ``--repo``. Defaults to
                ``REPO_PATH``.

        Returns:
            Absolute path to the created worktree.

        Raises:
            OrcaUnavailableError: If worktree cannot be created.
        """
        target = repo_path or self.REPO_PATH
        # Cross-repo targets (fresh clones) must be registered with Orca before
        # worktree create will accept them. school-core (REPO_PATH) is already
        # registered, so skip the registration round-trip in that case.
        if repo_path is not None and Path(repo_path).resolve() != Path(self.REPO_PATH).resolve():
            registered = self._register_repo(Path(repo_path))
            if registered is None:
                # Registration failure means Orca is down or repo add failed.
                # Surface a clear error rather than failing opaquely at
                # worktree create with a repo_not_found.
                raise OrcaUnavailableError(
                    f"Failed to register target repo with Orca: {repo_path}"
                )
        result = self._run_orca([
            "worktree", "create",
            "--name", name,
            "--repo", str(target),
        ], timeout=30)

        # Response format: {"worktree": {"id": "<uuid>::<path>", ...}}
        wt_info = result.get("worktree", result)
        wt_id = wt_info.get("id", "")
        if "::" in wt_id:
            path = wt_id.split("::", 1)[1]
        else:
            path = result.get("path", wt_info.get("path", ""))

        if not path:
            raise OrcaUnavailableError(
                f"Failed to create worktree '{name}': {json.dumps(result)[:300]}"
            )
        return path

    def close_worktree(self, path: str) -> bool:
        """Remove a worktree by path via the native Orca CLI.

        Uses ``orca worktree rm --worktree path:<path> --force`` — the single,
        verified disposal contract (the git/rm-rmtree fallback was removed:
        Orca owns worktree lifecycle, and the CLI dispose is reliable).

        Args:
            path: Absolute path to the worktree.

        Returns:
            ``True`` if the worktree was removed (or was already gone),
            ``False`` if the CLI call failed and the path still exists.

        Idempotent:
            Returns ``True`` immediately if the path does not exist.
        """
        p = Path(path)
        if not p.exists():
            return True  # Already gone — idempotent

        try:
            self._run_orca([
                "worktree", "rm",
                "--worktree", f"path:{path}",
                "--force",
            ], timeout=15)
        except Exception:
            # A single failure may mean Orca was momentarily busy; retry once.
            try:
                self._run_orca([
                    "worktree", "rm",
                    "--worktree", f"path:{path}",
                    "--force",
                ], timeout=15)
            except Exception:
                return False

        return not p.exists()

    def cleanup_worktrees_by_prefix(self, prefix: str = "study-") -> int:
        """Remove all worktrees whose path (or name) starts with a given prefix.

        Checks both the worktree name AND the last path component because
        Orca's list API may return empty names for some worktree types.

        Args:
            prefix: Name/path-component prefix to match (default: "study-").

        Returns:
            Number of worktrees removed.
        """
        try:
            result = self._run_orca(["worktree", "list"], timeout=15)
            wts = result.get("worktrees", [])
        except Exception:
            return 0

        removed = 0
        for wt in wts:
            name = wt.get("name", "") or ""
            path = wt.get("path", "") or ""
            dirname = Path(path).name if path else ""
            # Check both name field and path basename (Orca may not populate name)
            if name.startswith(prefix) or dirname.startswith(prefix):
                if self.close_worktree(path):
                    removed += 1
        return removed

    # ── Student briefs (written to worktrees) ───────────────────────────────

    def write_student_brief(self, worktree_path: str, brief: StudentBrief) -> Path:
        """Write a structured student brief to the worktree.

        Per the student-contract skill, every student must receive:
        - task
        - guardrails
        - expected output format
        - verification command
        - completion signal

        The brief is written to `.hermes/briefs/{bead}.json` in the worktree.

        Args:
            worktree_path: Path to the student's worktree.
            brief: Structured StudentBrief dataclass.

        Returns:
            Path to the written brief file.
        """
        brief_dir = Path(worktree_path) / ".hermes" / "briefs"
        brief_dir.mkdir(parents=True, exist_ok=True)
        brief_path = brief_dir / f"{brief.bead}.json"
        brief_path.write_text(json.dumps(brief.to_dict(), indent=2))
        return brief_path

    def write_student_output(self, worktree_path: str, bead: str, data: dict) -> Path:
        """Write student output and results to the worktree.

        Writes to `.hermes/outputs/{bead}.json` so the worktree contains
        a complete audit trail of what was done.

        Args:
            worktree_path: Path to the student's worktree.
            bead: Unique identifier for this task.
            data: Dict of output data (response, scores, review, etc.).

        Returns:
            Path to the written output file.
        """
        out_dir = Path(worktree_path) / ".hermes" / "outputs"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{bead}.json"
        out_path.write_text(json.dumps(data, indent=2))
        return out_path

    # ── Terminal lifecycle ────────────────────────────────────────────────────

    def create_terminal(self, title: str = "exec") -> str:
        """Create a terminal in the current project context and return its handle.

        No --worktree flag is passed, so Orca associates the terminal with
        the current project's worktree (visible in Orca's UI sidebar).

        Args:
            title: Terminal title shown in Orca's UI.

        Returns:
            Terminal handle string for subsequent commands.

        Raises:
            OrcaUnavailableError: If terminal cannot be created.
        """
        result = self._run_orca([
            "terminal", "create",
            "--title", title,
        ], timeout=15)

        terminal = result.get("terminal", result)
        handle = terminal.get("handle", "")
        if not handle:
            raise OrcaUnavailableError(
                f"Failed to get terminal handle: {json.dumps(result)[:200]}"
            )
        return handle

    def close_terminal(self, handle: str) -> None:
        """Close a terminal session. Best-effort."""
        try:
            self._run_orca(["terminal", "close", "--terminal", handle], timeout=10)
        except Exception:
            pass

    # ── Code execution ────────────────────────────────────────────────────────

    def _write_code(self, code: str, bead: str) -> Path:
        """Write extracted code to tempspace at /tmp/school-exec/<bead>/solution.py."""
        task_dir = self.TEMP_BASE / bead
        task_dir.mkdir(parents=True, exist_ok=True)
        file_path = task_dir / "solution.py"
        file_path.write_text(code, encoding="utf-8")
        return file_path

    # ── Terminal output parsing ───────────────────────────────────────────────

    @staticmethod
    def _is_shell_prompt(line: str) -> bool:
        """Check if a terminal line is a shell prompt (e.g., 'user@host dir %')."""
        return bool(OrcaExecutionManager.SHELL_PROMPT_RE.match(line.strip()))

    @staticmethod
    def _parse_exit_code(lines: list[str]) -> tuple[Optional[int], list[str]]:
        """Extract exit code marker from the last output line.

        Returns (exit_code, remaining_lines) with the marker line removed.
        If no marker found, returns (None, lines).
        """
        if not lines:
            return None, lines

        last = lines[-1].strip()
        match = OrcaExecutionManager.EXIT_CODE_RE.match(last)
        if match:
            return int(match.group(1)), lines[:-1]
        return None, lines

    @staticmethod
    def _clean_tail_lines(lines: list[str]) -> list[str]:
        """Remove shell prompts and empty lines from raw tail output."""
        cleaned = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if OrcaExecutionManager._is_shell_prompt(stripped):
                continue
            cleaned.append(stripped)
        return cleaned

    def execute(
        self,
        code: str,
        bead: str,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> ExecutionResult:
        """Execute code in an Orca terminal and capture results.

        Pipeline:
            1. Write code to tempspace at /tmp/school-exec/<bead>/solution.py
            2. Create Orca terminal (in current project context)
            3. Send `cd <dir> && python3 file ; echo "XEXITCODE:$?"`
            4. Poll for XEXITCODE marker every 0.5s (up to timeout_ms)
            5. Read final output, parse exit code from marker
            6. Split stdout/stderr, close terminal, return result

        The exit code is captured inline via the XEXITCODE marker because
        Orca's --for exit clears the terminal buffer. Polling lets us
        detect completion early for fast code and timeouts for infinite loops.

        Args:
            code: Clean, runnable code string (use CodeExtractor first).
            bead: Unique task identifier for tempspace scoping.
            timeout_ms: Maximum execution time in milliseconds.

        Returns:
            ExecutionResult with stdout, stderr, exit_code, and timing.
        """
        start = time.monotonic()

        file_path = self._write_code(code, bead)
        handle = self.create_terminal(title=f"exec-{bead[:8]}")

        timed_out = False

        try:
            # ---- Baseline read ----
            baseline = self._read_terminal_tail(handle)
            baseline_cursor = baseline.get("latestCursor", "0")

            # ---- Send execute command with exit-code echo ----
            exec_cmd = (
                f"cd {file_path.parent} && python3 {file_path.name} ; "
                f'echo "XEXITCODE:$?"'
            )
            self._run_orca([
                "terminal", "send",
                "--terminal", handle,
                "--text", exec_cmd,
                "--enter",
            ], timeout=10)

            # ---- Poll for completion (XEXITCODE marker) ----
            deadline = start + (timeout_ms / 1000)
            while time.monotonic() < deadline:
                time.sleep(self.POLL_INTERVAL)

                # Read and check for XEXITCODE marker in cleaned output
                poll_result = self._read_terminal_tail(handle, cursor=baseline_cursor)
                poll_tail = poll_result.get("tail", [])
                cleaned = self._clean_tail_lines(poll_tail)
                if cleaned and self.EXIT_CODE_RE.match(cleaned[-1].strip()):
                    break
            else:
                # Polling loop exhausted — code didn't finish in time
                timed_out = True

            # ---- Read final output ----
            result = self._read_terminal_tail(handle, cursor=baseline_cursor)
            tail_lines = result.get("tail", [])

            # ---- Clean and parse ----
            cleaned = self._clean_tail_lines(tail_lines)
            exit_code, output_lines = self._parse_exit_code(cleaned)

            # Fallback: no marker found
            if exit_code is None:
                exit_code = 0 if not timed_out else None

            # ---- Split into stdout/stderr ----
            raw_text = "\n".join(output_lines)
            stdout, stderr = self._split_output(raw_text, exit_code)

            duration_ms = int((time.monotonic() - start) * 1000)

            return ExecutionResult(
                stdout=stdout.strip(),
                stderr=stderr.strip(),
                exit_code=exit_code,
                timed_out=timed_out,
                duration_ms=duration_ms,
            )

        finally:
            self.close_terminal(handle)

    def _read_terminal_tail(self, handle: str, cursor: Optional[str] = None) -> dict:
        """Read the tail of an Orca terminal's output.

        Orca's read API returns lines in the 'tail' field (not 'lines').
        Items are plain strings. Response structure (after _run_orca
        unwraps 'result'): {"terminal": {"tail": [...], "nextCursor": "N", ...}}

        Returns empty tail on any error (never raises).
        """
        args = ["terminal", "read", "--terminal", handle, "--limit", "500"]
        if cursor is not None:
            args.extend(["--cursor", cursor])

        try:
            result = self._run_orca(args, timeout=10)
            terminal_info = result.get("terminal", result)
            return terminal_info
        except Exception:
            return {"tail": [], "nextCursor": "0", "latestCursor": "0"}

    @staticmethod
    def _split_output(raw: str, exit_code: Optional[int]) -> tuple[str, str]:
        """Split terminal output into stdout and stderr heuristically.

        Lines containing Python error keywords are classified as stderr.
        Everything else is stdout. When exit_code is 0, all output is stdout.
        """
        error_keywords = [
            "Traceback", "Error:", "Exception:", "SyntaxError",
            "NameError", "TypeError", "ValueError", "KeyError",
            "IndexError", "AttributeError", "ImportError",
            "ModuleNotFoundError", "ZeroDivisionError",
            "FileNotFoundError", "RuntimeError", "AssertionError",
            "IndentationError", "StopIteration", "OverflowError",
        ]

        if exit_code == 0:
            return raw, ""

        stdout_lines = []
        stderr_lines = []
        for line in raw.splitlines():
            if any(kw in line for kw in error_keywords):
                stderr_lines.append(line)
            else:
                stdout_lines.append(line)
        return "\n".join(stdout_lines), "\n".join(stderr_lines)

    # ── Cleanup ───────────────────────────────────────────────────────────────

    # ── Hermes agent execution ───────────────────────────────────────────────

    def run_hermes(
        self,
        worktree_path: str,
        bead: str,
        task: str,
        timeout_ms: int = HERMES_TIMEOUT_MS,
        handle: Optional[str] = None,
    ) -> str:
        """Run Hermes agent inside the worktree's Orca terminal and capture output.

        Pipeline (skill-compliant — no nested-quote ``terminal send`` mangling):
            1. Write task to ``.hermes/briefs/{bead}-hermes-task.txt``
            2. Write a launcher ``bash`` script (``run-hermes.sh``) that runs
               ``hermes chat -q "$(cat task.txt)" …`` into ``response.txt`` and
               touches a ``DONE`` sentinel.
            3. Launch that script via ``orca terminal create --command`` (or send
               to an existing ``handle``). The script is referenced by PATH — no
               inline quotes that Orca would mangle.
            4. Poll the ``DONE`` sentinel file (up to timeout_ms) — stable, unlike
               scraping the terminal buffer for an XEXITCODE marker.
            5. Read ``response.txt`` from disk.
            6. Close terminal (unless ``handle`` was provided — caller owns lifecycle).

        Args:
            worktree_path: Path to the leaf worktree.
            bead: Unique task identifier.
            task: The full task prompt (system prompt + task).
            timeout_ms: Max wait time in milliseconds (default 120s).
            handle: Optional existing terminal handle. If provided, the terminal
                    is reused instead of creating a new one, and the caller is
                    responsible for closing it.

        Returns:
            The captured Hermes response text.

        Raises:
            OrcaUnavailableError: If Orca is down or Hermes fails.
        """
        wp = Path(worktree_path)
        outputs_dir = wp / ".hermes" / "outputs"
        briefs_dir = wp / ".hermes" / "briefs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        briefs_dir.mkdir(parents=True, exist_ok=True)

        # Write task to a file (read by the launcher via $(cat …))
        task_file = briefs_dir / f"{bead}-hermes-task.txt"
        task_file.write_text(task, encoding="utf-8")

        response_file = outputs_dir / "response.txt"
        done_file = outputs_dir / f"{bead}-hermes-done"
        launcher = briefs_dir / f"{bead}-run-hermes.sh"
        # Remove stale artifacts if present
        for f in (response_file, done_file, launcher):
            if f.exists():
                f.unlink()

        # Launcher script: run hermes, capture output, touch DONE sentinel.
        # The $(cat task_file) keeps the multi-line task out of the terminal
        # command line, avoiding the quote-mangling trap entirely.
        launcher.write_text(
            "#!/usr/bin/env bash\n"
            f'cd {shlex.quote(str(wp))}\n'
            f'hermes chat -q "$(cat {shlex.quote(str(task_file))})" '
            f"--yolo --quiet --max-turns 1 "
            f"> {shlex.quote(str(response_file))} 2>&1\n"
            f'touch {shlex.quote(str(done_file))}\n',
            encoding="utf-8",
        )
        launcher.chmod(0o755)

        start = time.monotonic()
        own_terminal = handle is None
        # NOTE: do NOT pre-create a terminal here. When own_terminal, we create
        # the Hermes terminal directly via `terminal create --command` below and
        # capture its handle — creating one first (then a second via
        # --command) would orphan the first and leave the Hermes terminal open.

        try:
            launch_cmd = f"bash {shlex.quote(str(launcher))}"
            if own_terminal:
                # `terminal create --command` spawns the Hermes terminal AND runs
                # the launcher on startup. Capture its handle so finally closes
                # the *actual* Hermes terminal (not a throwaway one).
                result = self._run_orca([
                    "terminal", "create",
                    "--worktree", f"path:{wp}",
                    "--title", f"hermes-{bead[:8]}",
                    "--command", launch_cmd,
                ], timeout=15)
                terminal = result.get("terminal", result)
                handle = terminal.get("handle", handle)
            else:
                self._run_orca([
                    "terminal", "send",
                    "--terminal", handle,
                    "--text", launch_cmd,
                    "--enter",
                ], timeout=10)

            # ---- Poll the DONE sentinel (stable) instead of terminal scraping ----
            deadline = start + (timeout_ms / 1000)
            while time.monotonic() < deadline:
                if done_file.exists():
                    break
                time.sleep(self.POLL_INTERVAL)
            else:
                # Timed out before DONE touched
                if response_file.exists():
                    response = response_file.read_text(encoding="utf-8").strip()
                    if response:
                        # Partial response is better than nothing
                        return response
                raise OrcaUnavailableError(
                    f"Hermes timed out after {timeout_ms}ms for bead={bead}"
                )

            response = response_file.read_text(encoding="utf-8").strip() \
                if response_file.exists() else ""

            if not response:
                raise OrcaUnavailableError(
                    f"Hermes produced no output for bead={bead}"
                )

            return response

        finally:
            if own_terminal and handle:
                self.close_terminal(handle)
            # Clean up transient artifacts (response file kept for audit trail)
            for f in (task_file, launcher, done_file):
                if f.exists():
                    f.unlink()

    def cleanup_tempspace(self, bead: str) -> None:
        """Remove tempspace files for a completed task."""
        task_dir = self.TEMP_BASE / bead
        if task_dir.exists():
            shutil.rmtree(task_dir, ignore_errors=True)

    def supports_domain(self, domain: str) -> bool:
        """Check whether this domain produces executable code."""
        return domain in self.EXECUTABLE_DOMAINS



