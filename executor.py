import json
import os
import sys
import uuid
import urllib.request
import urllib.error
from typing import Optional

OMNIROUTE_BASE = "http://localhost:20128/v1"
A2A_BASE = "http://localhost:20128/a2a"
API_KEY = os.environ.get("OMNIROUTE_API_KEY", "sk-1f24b3ef61d2e1f9-a3db47-823f823a")

COMBO_MAP = {
    # Specialized roles — each role has a specific tool domain and model assignment.
    # Roles replace the old fungible "student" agents. Domain determines role;
    # score determines whether the role is qualified for the task difficulty.
    "searcher": "auto/best-free",
    "executor": "auto/best-free",
    "reviewer": "mistral/mistral-small-latest",
    "browser": "auto/best-free",
    "coder": "auto/best-free",
    # A2A fallback (agent-to-agent protocol)
    "openhands": "a2a/antigravity",
    "a2a-agent": "a2a/antigravity",
}

# Domain → Role mapping. When a task comes in with a given domain,
# the director routes it to the specialized role that handles that domain.
DOMAIN_ROLE_MAP = {
    "code-search": "searcher",
    "debugging": "searcher",
    "python-testing": "coder",
    "python-coding": "coder",
    "code-implementation": "coder",
    "code-review": "reviewer",
    "adversarial-review": "reviewer",
    "git-operations": "executor",
    "terminal": "executor",
    "web-automation": "browser",
    "_default": "coder",
}


def get_role_for_domain(domain: str) -> str:
    """Map a task domain to the specialized role that handles it."""
    return DOMAIN_ROLE_MAP.get(domain, DOMAIN_ROLE_MAP["_default"])


class ExecutorError(Exception):
    pass


A2A = "a2a"


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


CLOUD_TIMEOUT = 45  # Auto/best-free dream stack (6 providers, auto-failover) — generous timeout
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

    # Route: A2A (agent-to-agent) or OmniRoute (cloud).
    # Role-based tool dispatch: each role gets a tailored system prompt injected
    # by the director before call_model is invoked. This function handles only
    # the transport layer.
    if combo.startswith(f"{A2A}/"):
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
