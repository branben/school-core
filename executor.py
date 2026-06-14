import json
import re
import subprocess
import sys
import uuid
import urllib.request
import urllib.error
from typing import Optional

OMNIROUTE_BASE = "http://localhost:20128/v1"
A2A_BASE = "http://localhost:20128/a2a"
API_KEY = "***REMOVED***"

COMBO_MAP = {
    # Cloud models (via OmniRoute)
    "gemini-3-flash-preview": "free-stack",
    "gemma-4-31b-it:free": "free-stack",
    "owl-alpha": "openrouter/owl-alpha",
    "gemini-2.0-flash": "free-stack",
    "kimi-k2.6:free": "free-stack",
    "always-on-max": "always-on-max",
    "always-on-free": "always-on-free",
    "north-coding": "north-coding",
    # Local models (via Foundry Local — GPU-accelerated, subprocess transport)
    # 0.5b and 1.5b run fine on M1 16GB; 7b+ causes OOM/paging → use cloud instead
    "foundry-coder-0.5b": "foundry/qwen2.5-coder-0.5b",
    "foundry-coder-1.5b": "foundry/qwen2.5-coder-1.5b",
    "foundry-coder-7b": "openrouter/owl-alpha",  # Cloud: 7b too large for local M1 16GB
    "foundry-smollm3-3b": "foundry/smollm3-3b",
    "foundry-phi4": "foundry/phi-4",
    # A2A agents (agent-to-agent protocol — fallback)
    "openhands": "a2a/antigravity",
    "a2a-agent": "a2a/antigravity",
}


class ExecutorError(Exception):
    pass


FOUNDRY = "foundry"
A2A = "a2a"

FOUNDRY_MAX_TOKENS = 2048
FOUNDRY_TEMPERATURE = 0.3

_FORMATTING_RE = re.compile(r'\x1b\[[0-9;]*m|[\u2500-\u257f]')


def _omniroute_call(combo: str, messages: list, timeout: int) -> dict:
    body = {
        "model": combo,
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": 4096,
        "stream": False,
    }
    req = urllib.request.Request(
        f"{OMNIROUTE_BASE}/chat/completions",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "OpenCode/1.0",
        },
        method="POST",
    )
    raw = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500] if e.fp else str(e)
        raise ExecutorError(f"OmniRoute HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise ExecutorError(f"OmniRoute connection failed: {e.reason}")
    except json.JSONDecodeError as e:
        preview = raw[:300] if raw else "empty response"
        raise ExecutorError(f"Invalid JSON: {e} | raw: {preview}")


def _foundry_call(model_name: str, messages: list, timeout: int) -> dict:
    """Call a Foundry Local model via `foundry complete` subprocess.

    Foundry's OpenAI-compatible REST API does not reliably bind to a port,
    so we use the CLI directly. The model_name here is the short alias
    (e.g., 'qwen2.5-coder-0.5b'), not the full variant ID.
    """
    # Build a single prompt from messages (foundry complete takes a single prompt)
    prompt_parts = []
    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if role == "system":
            prompt_parts.append(f"System: {content}")
        elif role == "assistant":
            prompt_parts.append(f"Assistant: {content}")
        else:
            prompt_parts.append(content)
    prompt = "\n\n".join(prompt_parts)

    cmd = [
        "foundry", "complete", model_name, prompt,
        "--max-tokens", str(FOUNDRY_MAX_TOKENS),
        "--temperature", str(FOUNDRY_TEMPERATURE),
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, timeout=timeout, check=False, text=True,
        )
    except subprocess.TimeoutExpired:
        raise ExecutorError(f"Foundry model '{model_name}' timed out after {timeout}s")
    except FileNotFoundError:
        raise ExecutorError("Foundry CLI not found in PATH")

    if result.returncode != 0:
        stderr = result.stderr.strip()[:500] if result.stderr else ""
        # Auto-load model if not loaded
        if "not loaded" in stderr.lower():
            sys.stderr.write(f"[executor] Loading Foundry model {model_name}...\n")
            try:
                subprocess.run(
                    ["foundry", "model", "load", model_name],
                    capture_output=True, timeout=120, check=True,
                )
            except (subprocess.CalledProcessError, FileNotFoundError) as load_err:
                raise ExecutorError(f"Foundry auto-load failed: {load_err}")
            sys.stderr.write("[executor] Loaded. Retrying...\n")
            return _foundry_call(model_name, messages, timeout)
        raise ExecutorError(f"Foundry error (rc={result.returncode}): {stderr}")

    # Strip terminal formatting (box-drawing, ANSI codes) from output
    raw_output = _FORMATTING_RE.sub("", result.stdout).strip()

    # Extract code blocks if present — return the full formatted output
    return {
        "choices": [{"message": {"content": raw_output}}],
        "model": model_name,
    }


def _a2a_call(
    task_text: str,
    *,
    system_prompt: Optional[str] = None,
    timeout: int = 120,
    task_id: Optional[str] = None,
) -> str:
    tid = task_id or f"task-{uuid.uuid4().hex[:12]}"

    parts = []
    if system_prompt:
        parts.append({"type": "text", "text": system_prompt})
    parts.append({"type": "text", "text": task_text})

    body = {
        "jsonrpc": "2.0",
        "id": "a2a-1",
        "method": "message/send",
        "params": {
            "id": tid,
            "sessionId": f"ses-{uuid.uuid4().hex[:12]}",
            "message": {
                "role": "user",
                "parts": parts,
            },
        },
    }
    req = urllib.request.Request(
        A2A_BASE,
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {API_KEY}",
            "User-Agent": "OpenCode/1.0",
        },
        method="POST",
    )
    raw = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode()
            data = json.loads(raw)
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:500] if e.fp else str(e)
        raise ExecutorError(f"A2A HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise ExecutorError(f"A2A connection failed: {e.reason}")
    except json.JSONDecodeError as e:
        preview = raw[:300] if raw else "empty response"
        raise ExecutorError(f"A2A invalid JSON: {e} | raw: {preview}")

    result = data.get("result", {})
    task = result.get("task", {})
    state = task.get("state", "unknown")

    # If still running, poll once (in practice message/send usually completes)
    if state in ("pending", "working"):
        return _a2a_poll(tid, timeout=timeout)

    artifacts = result.get("artifacts", [])
    if not artifacts:
        meta = result.get("metadata", {})
        raise ExecutorError(
            f"A2A task {tid} completed with no artifacts (state={state}, meta={meta})"
        )

    # Concatenate all text artifacts
    texts = []
    for a in artifacts:
        if a.get("type") == "text":
            texts.append(a.get("content", ""))
    if not texts:
        meta = result.get("metadata", {})
        raise ExecutorError(
            f"A2A task {tid} completed but no text artifacts (types: {[a.get('type') for a in artifacts]}, meta={meta})"
        )
    return "\n\n".join(texts)


def _a2a_poll(task_id: str, timeout: int = 120) -> str:
    import time

    deadline = time.monotonic() + timeout
    last_state = "pending"

    while time.monotonic() < deadline:
        body = {
            "jsonrpc": "2.0",
            "id": "a2a-poll",
            "method": "tasks/get",
            "params": {"id": task_id},
        }
        req = urllib.request.Request(
            A2A_BASE,
            data=json.dumps(body).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {API_KEY}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode())
        except Exception:
            time.sleep(5)
            continue

        result = data.get("result", {})
        task = result.get("task", {})
        state = task.get("state", "unknown")
        last_state = state

        if state == "completed":
            artifacts = result.get("artifacts", [])
            texts = [
                a.get("content", "")
                for a in artifacts
                if a.get("type") == "text"
            ]
            return "\n\n".join(texts) if texts else ""

        if state in ("failed", "canceled", "error"):
            error = task.get("error", {}).get("message", "unknown error")
            raise ExecutorError(f"A2A task {task_id} {state}: {error}")

        time.sleep(3)

    raise ExecutorError(
        f"A2A task {task_id} did not complete within {timeout}s "
        f"(last state: {last_state})"
    )


CLOUD_TIMEOUT = 30  # OmniRoute free-stack is unreliable — fail fast
FOUNDRY_TIMEOUT = 300  # Foundry models: 7b ~15s, phi-4 ~30s, cold loads ~60s
CLOUD_HEALTH_TIMEOUT = 5  # Quick ping for cloud availability check


def cloud_available() -> bool:
    """Check if cloud (OmniRoute) is reachable. Returns False on any error."""
    try:
        req = urllib.request.Request(
            f"{OMNIROUTE_BASE}/models",
            headers={
                "Authorization": f"Bearer {API_KEY}",
                "User-Agent": "OpenCode/1.0",
            },
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=CLOUD_HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def is_local_agent(agent_name: str) -> bool:
    """Returns True if the agent runs on local hardware (Foundry GPU)."""
    combo = COMBO_MAP.get(agent_name, "")
    return combo.startswith(f"{FOUNDRY}/")


def call_model(
    agent_name: str,
    prompt: str,
    system_prompt: Optional[str] = None,
    timeout: Optional[int] = None,
) -> str:
    combo = COMBO_MAP.get(agent_name)
    if not combo:
        raise ExecutorError(f"Unknown agent '{agent_name}' — no combo mapped")

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    # Route: Foundry (local GPU), A2A (agent-to-agent), or OmniRoute (cloud)
    if combo.startswith(f"{FOUNDRY}/"):
        model_name = combo.split("/", 1)[1]
        result = _foundry_call(model_name, messages, timeout or FOUNDRY_TIMEOUT)
    elif combo.startswith(f"{A2A}/"):
        agent_target = combo.split("/", 1)[1]
        return _a2a_call(
            task_text=prompt,
            system_prompt=system_prompt,
            timeout=timeout or 120,
        )
    else:
        result = _omniroute_call(combo, messages, timeout or CLOUD_TIMEOUT)

    choices = result.get("choices", [])
    if not choices:
        raise ExecutorError(f"No choices in response: {json.dumps(result)[:300]}")
    return choices[0]["message"]["content"]
