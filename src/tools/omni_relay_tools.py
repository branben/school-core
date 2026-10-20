"""Read-only tool serving for the OmniRoute relay transport.

This closes the gap that made a bare completion useless for repo work: the
model answers `finish_reason: tool_calls` with no content, and `OmniRouteTransport`
correctly refuses that. Serving tools is what turns the transport into an agent.

Security posture, because every input here is hostile:

* **Allowlist.** Exactly `read_file`, `list_dir`, `search`. No write, no shell,
  no apply-patch. A model that asks for `write_file` gets a refusal, not a
  mutation. Read-only is the whole safety story in this slice.
* **Path confinement.** Every path is resolved with `realpath` BEFORE the
  containment check, so a symlink pointing outside the root is caught even
  though the requested path looked innocent. Relative `..` is not enough on
  its own -- it is the resolved path that decides.
* **Bounded reads.** A file larger than the cap is truncated with an explicit
  marker rather than loaded whole, so a 2GB file cannot exhaust memory.
* **Literal search.** The pattern is a plain substring, never a regex, so it
  cannot become a ReDoS vector or a path glob.
* **Tool output is data.** Read content is returned as tool-role content. It is
  never promoted into a system or developer message, so a file containing
  instructions cannot talk its way into the model's privileged position.
* **Refusals are visible.** A rejected tool call comes back as a tool-role
  error the model can read and correct, rather than an exception that kills the
  loop or a silent empty result that looks like success.

The registry deliberately holds no file handles and exposes no mutating
attributes. Absence of capability is stronger than a permission check on one.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "ToolError",
    "ToolRegistry",
    "build_tool_request",
    "TOOL_DEFINITIONS",
]

#: Hard cap on any single file read, in bytes. Larger files are truncated with
#: a visible marker so the model knows it is looking at a prefix.
MAX_READ_BYTES = 100_000

#: Cap on how many entries a single directory listing may return.
MAX_LIST_ENTRIES = 500

#: Cap on how many matches a single search may return.
MAX_SEARCH_MATCHES = 200


class ToolError(Exception):
    """A tool call could not be served. Always reported back to the model."""


#: The advertised tool surface. This list IS the capability: anything not here
#: cannot be called, regardless of what a model asks for.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file from the repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Path relative to the repo root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the entries in a repository directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string",
                             "description": "Directory relative to the root."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Find a literal string across repository files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string",
                                "description": "Literal substring to find."},
                },
                "required": ["pattern"],
            },
        },
    },
]

_ALLOWED = frozenset(d["function"]["name"] for d in TOOL_DEFINITIONS)

#: Directories never walked by `search`. Keeps the tool inside source code and
#: off dependency trees, virtualenvs, and VCS metadata.
_SEARCH_SKIP = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
    ".pytest_cache", "target", "dist", "build", ".index-gate", ".beads",
})


def build_tool_request(
    *, operation: str, prompt: str, model: str, max_tokens: int = 4000,
    tools: list[dict[str, Any]] | None = None,
    messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build the upstream request body, optionally advertising tools.

    `tools` and `messages` let an agent loop continue a conversation. When
    `tools` is empty or None the key is omitted entirely, so a plain
    completion is byte-identical to the no-tools path.
    """
    if operation != "complete":
        raise ToolError(f"operation not permitted: {operation!r}")
    if not isinstance(prompt, str) or not prompt.strip():
        raise ToolError("prompt must be a non-empty string")
    if not isinstance(model, str) or not model.strip():
        raise ToolError("model must be a non-empty string")

    body: dict[str, Any] = {
        "model": model,
        "max_tokens": int(max_tokens),
        "temperature": 0.0,
    }
    if messages:
        body["messages"] = list(messages)
    else:
        body["messages"] = [{"role": "user", "content": prompt}]
    if tools:
        body["tools"] = list(tools)
        # Require the model to pick a tool rather than guessing.
        body["tool_choice"] = "auto"
    return body


class ToolRegistry:
    """Serves a fixed set of read-only tools from one directory root.

    There is no registration API and no mutation API. The capability is the
    allowlist, fixed at construction.
    """

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self._root: Path | None = (
            Path(root).resolve() if root is not None else None
        )
        self.read_calls = 0

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(sorted(_ALLOWED))

    def definitions(self) -> list[dict[str, Any]]:
        """A deep-enough copy for the request body."""
        return json.loads(json.dumps(TOOL_DEFINITIONS))

    # -- path handling ------------------------------------------------------

    def _resolve(self, raw: Any) -> Path:
        """Resolve `raw` inside the root, or raise.

        The containment test runs on the RESOLVED path, which is what defeats
        a symlink that points outside the root while looking innocent.
        """
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("path must be a non-empty string")
        if self._root is None:
            raise ToolError("no repository root configured for tools")
        if os.path.isabs(raw):
            candidate = Path(raw)
        else:
            candidate = self._root / raw
        try:
            resolved = candidate.resolve()
        except (OSError, RuntimeError) as exc:
            raise ToolError(f"path could not be resolved: {exc}") from None
        # `resolved` is absolute and symlink-free; containment is exact.
        if resolved != self._root and self._root not in resolved.parents:
            raise ToolError(
                f"path escapes the repository root: {raw}")
        return resolved

    # -- tools --------------------------------------------------------------

    def _read_file(self, args: dict[str, Any]) -> str:
        target = self._resolve(args.get("path"))
        if not target.is_file():
            raise ToolError(f"not a readable file: {args.get('path')}")
        try:
            with open(target, "r", encoding="utf-8", errors="replace") as fh:
                data = fh.read(MAX_READ_BYTES + 1)
        except OSError as exc:
            raise ToolError(f"read failed: {type(exc).__name__}") from None
        self.read_calls += 1
        if len(data) > MAX_READ_BYTES:
            return (
                data[:MAX_READ_BYTES]
                + f"\n... [truncated at {MAX_READ_BYTES} bytes]"
            )
        return data

    def _list_dir(self, args: dict[str, Any]) -> str:
        target = self._resolve(args.get("path"))
        if not target.is_dir():
            raise ToolError(f"not a directory: {args.get('path')}")
        try:
            entries = sorted(p.name + ("/" if p.is_dir() else "")
                             for p in target.iterdir())
        except OSError as exc:
            raise ToolError(f"list failed: {type(exc).__name__}") from None
        return "\n".join(entries[:MAX_LIST_ENTRIES])

    def _search(self, args: dict[str, Any]) -> str:
        pattern = args.get("pattern")
        if not isinstance(pattern, str) or not pattern:
            raise ToolError("pattern must be a non-empty string")
        if self._root is None:
            raise ToolError("no repository root configured for tools")
        hits: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [d for d in dirnames if d not in _SEARCH_SKIP]
            for name in filenames:
                if len(hits) >= MAX_SEARCH_MATCHES:
                    break
                fp = Path(dirpath) / name
                try:
                    if fp.stat().st_size > MAX_READ_BYTES:
                        continue
                    text = fp.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if pattern in text:  # literal, never a regex
                    for i, line in enumerate(text.splitlines(), 1):
                        if pattern in line:
                            rel = fp.relative_to(self._root)
                            hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                            if len(hits) >= MAX_SEARCH_MATCHES:
                                break
        return "\n".join(hits) if hits else "(no matches)"

    # -- dispatch -----------------------------------------------------------

    def call(self, name: str, args: dict[str, Any]) -> str:
        """Run one allowlisted tool. Anything else raises `ToolError`."""
        if not isinstance(name, str) or name not in _ALLOWED:
            raise ToolError(f"tool not available: {name!r}")
        if not isinstance(args, dict):
            raise ToolError("tool arguments must be an object")
        handler = {
            "read_file": self._read_file,
            "list_dir": self._list_dir,
            "search": self._search,
        }[name]
        return handler(args)

    def execute(self, tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Serve a batch of tool calls, returning tool-role messages.

        A refused call yields an error CONTENT string, not an exception: the
        model must be able to see the refusal and correct itself.
        """
        out: list[dict[str, Any]] = []
        for raw in tool_calls or []:
            call_id = ""
            try:
                call_id = raw.get("id", "") or ""
                fn = raw.get("function")
                if not isinstance(fn, dict):
                    raise ToolError("tool call had no function")
                name = fn.get("name")
                raw_args = fn.get("arguments")
                if isinstance(raw_args, str):
                    try:
                        args = json.loads(raw_args)
                    except ValueError:
                        raise ToolError("tool arguments were not valid JSON")
                elif isinstance(raw_args, dict):
                    args = raw_args
                else:
                    raise ToolError("tool arguments were missing")
                content = self.call(name, args)
            except ToolError as exc:
                content = f"[tool error] {exc}"
            except Exception as exc:  # noqa: BLE001 — never kill the loop
                content = f"[tool error] {type(exc).__name__}"
            out.append({
                "role": "tool",
                "tool_call_id": call_id,
                "content": content,
            })
        return out

    def as_messages(self, tool_messages: list[dict[str, Any]]
                    ) -> list[dict[str, Any]]:
        """Normalize tool output into messages.

        Tool-role content is DATA. It is never re-roled as system or user, so
        instructions found inside a file cannot reach a privileged position.
        """
        out = []
        for m in tool_messages or []:
            out.append({
                "role": "tool",
                "tool_call_id": m.get("tool_call_id", ""),
                "content": m.get("content", ""),
            })
        return out

    def turn(self, tool_calls: list[dict[str, Any]]
             ) -> list[dict[str, Any]]:
        """Validate a raw tool-call list from the model, then serve it."""
        if not isinstance(tool_calls, list):
            raise ToolError("tool_calls must be a list")
        for raw in tool_calls:
            if not isinstance(raw, dict):
                raise ToolError("each tool call must be an object")
            fn = raw.get("function")
            if not isinstance(fn, dict) or "name" not in fn:
                raise ToolError("tool call was missing function.name")
        return self.execute(tool_calls)