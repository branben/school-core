# Orca Dispatch Architecture

> How students actually execute code in a sandboxed terminal.

---

## The Problem

The current pipeline is text-only:

```
LLM API → text → bookbag → LLM-as-judge → score
```

The coder outputs Python as a string. The reviewer asks another LLM if it looks correct. Nobody **runs** the code.

The verify_gate approach (typecheck a cloned repo) doesn't fit because the pipeline produces one-off code snippets, not repo patches.

## The Solution: Orca Terminal Dispatch

Use Orca terminals as sandboxed execution environments. Instead of judging student output by reading it, **run it** in a real terminal and capture the evidence.

```
LLM → code text → Orca terminal → real execution → captured output
→ bookbag with execution evidence → CTO+COO review sees real output
→ score
```

---

## Architecture

### New Module: `orca_executor.py`

A new module that wraps Orca CLI calls for student code execution. It replaces the failed verify_gate approach with actual code execution.

```
orca_executor.py
  ├── OrcaExecutionManager  — worktree lifecycle, terminal pool
  ├── CodeExtractor         — extract runnable code from LLM responses
  ├── CodeRunner            — write code, execute, capture output
  └── ExecutionResult       — stdout, stderr, exit_code, duration
```

### Flow

```
conductor.py → director.run_task("Write a function...", domain="python-coding")
  │
  ├── call_model("coder", prompt) → "```python\ndef is_palindrome...```"
  │
  ├── orca_executor.execute(coder_response, language="python")
  │     │
  │     ├── 1. Extract code from response (strip ``` fences, detect language)
  │     │     → "def is_palindrome(s: str) -> bool: ..."
  │     │
  │     ├── 2. Write to temp file in Orca worktree
  │     │     → /tmp/school-exec/<bead>/solution.py
  │     │
  │     ├── 3. Create Orca terminal in shared worktree
  │     │     → orca terminal create --title "coder-task-<bead>"
  │     │
  │     ├── 4. Send execute command
  │     │     → orca terminal send --text "cd /tmp/school-exec/<bead>/ && python3 solution.py"
  │     │
  │     ├── 5. Wait for completion (timeout: 30s)
  │     │     → orca terminal wait --for exit --timeout-ms 30000
  │     │
  │     └── 6. Read output
  │           → orca terminal read → {stdout, stderr, exit_code}
  │
  ├── execution_evidence = {stdout, stderr, exit_code, timed_out}
  │
  ├── Bookbag written with output + execution_evidence
  │     └── CTO+COO review sees execution evidence as CRITICAL findings
  │           ├── exit_code != 0 → CRITICAL: code failed at runtime
  │           ├── timed_out → CRITICAL: code hung
  │           ├── stdout matches expected → PASS signal
  │           └── no execution possible → INFO: sandbox unavailable, prose review only
  │
  └── Score updated with execution-backed evidence
```

### Worktree Strategy

**Shared worktree, per-task terminals.** Not one worktree per task (too heavy — each is a git checkout).

```
school-core Orca worktree (one per session)
  │
  ├── terminal-1: "coder-task-a1b2"  (execute + read + close)
  ├── terminal-2: "coder-task-c3d4"
  └── terminal-3: "executor-task-e5f6"
```

The shared worktree is created once at school-core startup:

```bash
orca worktree create --name "school-core-exec" --no-parent --setup skip --json
```

Each task creates a lightweight terminal in that worktree, writes code to a temp dir, executes, reads output, then closes the terminal.

### Tempspace

All student code is written to a predictable temp path scoped by bead:

```
/tmp/school-exec/
  ├── <bead>/
  │   ├── solution.py        # extracted code
  │   ├── input.txt          # optional stdin
  │   └── output.txt         # captured stdout (written by runner)
  └── ...
```

The Orca terminal `cd`s to this path, executes, and output is captured via `orca terminal read`.

---

## Module Design

### `orca_executor.py`

```python
@dataclass
class ExecutionResult:
    stdout: str
    stderr: str
    exit_code: Optional[int]
    timed_out: bool
    duration_ms: int
    error: Optional[str]  # None if execution succeeded

class OrcaExecutionManager:
    """Manages the shared Orca worktree and terminal lifecycle."""

    WORKTREE_NAME = "school-core-exec"
    TEMP_BASE = Path("/tmp/school-exec")
    EXECUTABLE_DOMAINS = {"python-coding", "python-testing", "code-implementation", "terminal", "git-operations"}

    def __init__(self):
        self._worktree_ready = False
        self._ensure_worktree()

    def _ensure_worktree(self) -> bool:
        """Create the shared worktree if it doesn't exist. Returns True if ready."""
        ...

    def execute(self, code: str, language: str, bead: str, timeout_ms: int = 30000) -> ExecutionResult:
        """Write code to tempspace, execute in Orca terminal, return results."""
        ...

    def supports_domain(self, domain: str) -> bool:
        """Check if Orca execution is available for this domain."""
        return domain in self.EXECUTABLE_DOMAINS and self._worktree_ready

    def _create_terminal(self) -> str:
        """Create a terminal in the shared worktree. Returns handle."""
        ...

    def _write_code(self, code: str, language: str, bead: str) -> Path:
        """Write extracted code to tempspace. Returns the file path."""
        ...

    def _execute_in_terminal(self, terminal_handle: str, file_path: Path, timeout_ms: int) -> ExecutionResult:
        """Send command, wait for exit, read output."""
        ...

    def _close_terminal(self, terminal_handle: str):
        """Clean up the terminal session."""
        ...

    def _parse_response(self, response: str) -> tuple[str, str]:
        """Extract stderr from terminal output (stderr is captured inline)."""
        ...
```

### Code Extraction (`CodeExtractor`)

Extracting runnable code from an LLM response is non-trivial. The response might be:

1. Wrapped in ```python ... ``` fences
2. Just raw code with no fences
3. Code with explanatory text interspersed
4. A shell command (for executor/terminal domains)

```python
class CodeExtractor:
    @staticmethod
    def extract(response: str, language: str = "python") -> str:
        """Extract runnable code from LLM response."""
        # Strategy 1: Find ``` fences
        # Strategy 2: Find code block heuristics (indentation, def/class at line start)
        # Strategy 3: Return raw text as-is
        ...

    @staticmethod
    def strip_markdown_fences(text: str) -> Optional[str]:
        """Remove ```<lang> ... ``` fences. Returns None if no fences found."""
        ...
```

### Integration into director.py

In `_run_two_judge_review()`, replace the verify_gate call with Orca execution:

```python
def _run_two_judge_review(...):
    orca = OrcaExecutionManager()

    # ── Orca Execution ──
    execution_results = []
    if orca.supports_domain(task.get("domain", "")):
        try:
            code = CodeExtractor.extract(output, language="python")
            result = orca.execute(code, bead=bead, timeout_ms=30000)
            if result.exit_code != 0:
                execution_results.append(Finding(
                    section="execution",
                    issue_class="runtime_failure",
                    severity=Severity.CRITICAL,
                    citation=f"exit_code={result.exit_code}",
                    description=result.stderr[:300],
                    suggestion="Fix the runtime errors above",
                ))
            elif result.timed_out:
                execution_results.append(Finding(
                    section="execution",
                    issue_class="timeout",
                    severity=Severity.HIGH,
                    citation=f"timed out after 30s",
                    description="Code execution timed out",
                    suggestion="Ensure the code terminates in reasonable time",
                ))
            else:
                execution_results.append(Finding(
                    section="execution",
                    issue_class="execution_passed",
                    severity=Severity.LOW,
                    citation=f"exit_code=0",
                    description=f"Code executed successfully in {result.duration_ms}ms",
                ))
        except OrcaUnavailableError:
            execution_results.append(Finding(
                section="execution",
                issue_class="sandbox_unavailable",
                severity=Severity.LOW,
                citation="Orca not available",
                description="Code execution sandbox unavailable — prose review only",
            ))
```

---

## Files That Change

| File | Change |
|------|--------|
| `orca_executor.py` (NEW) | Orca CLI wrapper, terminal management, code execution |
| `director.py` | Replace verify_gate call with Orca execution in `_run_two_judge_review()` |
| `executor.py` (minor) | Optional: add `call_model_with_execution()` convenience |
| `flake.nix` (minor) | Update comment — no longer depends on Nix for student execution |

No changes needed to:
- `conductor.py` — it already calls `run_task()` which calls `_run_two_judge_review()`
- `bookbag.py` — execution evidence is just additional findings
- `scoring.py` — scoring logic unchanged

---

## Error Modes

| Situation | Behavior |
|-----------|----------|
| Orca not running | `OrcaExecutionManager` detects `orca status` fails, sets `_worktree_ready=False`. All domains fall through to prose-only review. |
| Orca CLI not installed | Same as above — `OrcaUnavailableError` caught in review. |
| Terminal creation fails | Orca server may be overloaded — retry once, then fall through. |
| Code execution times out | `terminal wait --for exit --timeout-ms 30000` returns timeout. Finding with `timed_out=True`. |
| Code has syntax error | Python exits with code 1, stderr contains traceback. Finding with `exit_code=1`. |
| Code is malicious (rm -rf /) | Runs inside Orca terminal — no host access. Worktree is disposable. The terminal session is closed after each task. |

---

## Safety Model

| Threat | Mitigation |
|--------|------------|
| Student code deletes files | Orca terminal has no special access — runs as the same user, but within a disposable worktree that can be recreated |
| Student code runs forever | Hard timeout of 30s via `--timeout-ms` |
| Student code accesses network | Orca terminal inherits host network — no network restriction yet. Future: run inside a container or use `nix develop .#sandboxShell` inside the Orca terminal |
| Student code forks bombs | Timeout kills the terminal. The `close()` cleans up |
| Multiple tasks concurrently | Each task gets its own terminal session. Terminals within the same worktree are isolated by the OS |
| Orca crashes during execution | `OrcaExecutionManager` detects on next call, falls back to prose-only |

---

## Implementation Phases

### Phase 1: Core (this task)
- `orca_executor.py` with `OrcaExecutionManager`
- Code extraction from LLM responses
- Integration into `_run_two_judge_review()`
- Replace verify_gate call

### Phase 2: Hardening
- Retry logic for terminal creation
- Better code extraction heuristics
- Multi-line output capture with cursor reads
- Structured output parsing (Python `assert` results, test output)

### Phase 3: Isolation (future)
- Container-based execution inside Orca terminal
- Nix sandboxShell support when available
- Network restriction via pf/iptables rules
- Per-task tempspace cleanup cron

---

## Key Design Decisions

1. **Shared worktree, not per-task** — creating a git worktree takes ~1s and creates a branch. One per session is fine. One per task is wasteful.

2. **Per-task terminal, not one terminal per session** — terminals are cheap and disposable. Closing after each task ensures no state leakage between tasks.

3. **Tempspace on host, not in worktree** — we write code to `/tmp/school-exec/<bead>/` which the Orca terminal can access. This avoids polluting the worktree with temp files.

4. **Exit code as primary signal** — 0 = success, non-zero = failure. Stderr is the error message. Stdout is the output. This is simple and universal.

5. **Code extraction is best-effort** — if we can't extract code (e.g., no fences, mixed prose/code), we fall back to plain text review. The execution evidence is supplementary, not required.
