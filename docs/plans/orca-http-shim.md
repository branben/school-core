# Orca HTTP Shim — API Spec

**Goal:** Replace `subprocess.run(["orca", ...])` with HTTP calls so school-core can run in a container.

**Host:** FastAPI daemon on port 9100, binds `127.0.0.1` only.
**Client:** `orca_http.py` — httpx-based, env var `ORCA_HTTP`.

---

## Endpoints

### Health

```
GET /readyz
→ 200 {"ok": true}
→ 503 {"ok": false, "error": "Orca not running"}
```

### Worktree Operations

```
POST /worktree/create
Body:    { "name": "study-coder-abc123", "repo_path": "/Users/.../school-core" }
Response: { "path": "/Users/.../orca/workspaces/study-coder-abc123", "name": "study-coder-abc123" }
Error:    409 if name already exists
```

```
POST /worktree/rm
Body:    { "path": "/Users/.../study-coder-abc123" }
Response: { "ok": true }
Error:    404 if not found
```

```
GET /worktree/list
Response: [{ "path": "...", "name": "...", "repo": "..." }]
```

```
POST /worktree/prune
Body:    {}
Response: { "ok": true, "pruned": 3 }
```

### Repo Operations

```
GET /repo/list
Response: [{ "path": "...", "name": "...", "registered": true }]
```

```
POST /repo/add
Body:    { "path": "/Users/.../school-core" }
Response: { "ok": true, "name": "school-core" }
```

### Terminal Operations

```
POST /terminal/send
Body:    { "handle": "term-abc", "text": "git status", "enter": true }
Response: { "ok": true }
```

```
POST /terminal/close
Body:    { "handle": "term-abc" }
Response: { "ok": true }
```

### Runtime

```
GET /status
Response: { "ok": true, "runtime": { "state": "running" } }
```

---

## Error Contract

All errors return JSON:

```json
{ "error": "OrcaUnavailableError", "message": "..." }
```

Status codes:
- `400` — bad input (missing required field)
- `404` — resource not found (worktree, repo)
- `409` — conflict (duplicate name)
- `503` — Orca not running / not ready

---

## Client Module: `orca_http.py`

```python
"""HTTP client for Orca shim — replaces subprocess.run(['orca', ...])."""
import os, httpx, json
from typing import Optional

ORCA_HTTP = os.environ.get("ORCA_HTTP", "http://localhost:9100")

class OrcaHTTPError(Exception):
    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(message)

class OrcaHTTPClient:
    def __init__(self, base_url: str = ORCA_HTTP, token: str = None):
        headers = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.client = httpx.Client(base_url=base_url, timeout=30.0, headers=headers)

    def create_worktree(self, name: str, repo_path: str = None) -> dict: ...
    def remove_worktree(self, path: str) -> dict: ...
    def list_worktrees(self) -> list: ...
    def prune_worktrees(self) -> dict: ...
    def list_repos(self) -> list: ...
    def add_repo(self, path: str) -> dict: ...
    def send_terminal(self, handle: str, text: str, enter: bool = True) -> dict: ...
    def close_terminal(self, handle: str) -> dict: ...
    def status(self) -> dict: ...
    def health(self) -> bool: ...
```

---

## Migration in `orca_executor.py`

Add a mode flag:

```python
# At module level
ORCA_MODE = os.environ.get("ORCA_MODE", "http")  # http|cli

# In OrcaExecutionManager.__init__
if ORCA_MODE == "http":
    self._client = OrcaHTTPClient()
else:
    self._client = None  # use subprocess

# Replace _run_orca with:
def _run_orca(self, args: list[str], timeout: int = 15) -> dict:
    if ORCA_MODE == "http":
        return self._run_orca_http(args, timeout)
    return self._run_orca_cli(args, timeout)  # existing
```

---

## Host Daemon: `scripts/orca_shim.py`

```python
"""FastAPI daemon wrapping Orca CLI — runs on host, not in container."""
from fastapi import FastAPI, HTTPException
import subprocess, json, os

app = FastAPI(title="Orca Shim", version="1.0.0")
ORCA_BIN = os.environ.get("ORCA_BIN", "orca")

def run_orca(args: list[str]) -> dict:
    """Run orca CLI and return parsed JSON."""
    proc = subprocess.run([ORCA_BIN] + args + ["--json"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise HTTPException(503, f"Orca failed: {proc.stderr[:300]}")
    return json.loads(proc.stdout)

@app.get("/readyz")
def readyz():
    try:
        result = run_orca(["status"])
        return {"ok": result.get("runtime", {}).get("state") == "running"}
    except Exception:
        raise HTTPException(503, "Orca not ready")

@app.post("/worktree/create")
def worktree_create(body: dict):
    return run_orca(["worktree", "create", "--name", body["name"]])

# ... etc
```

---

## Docker Compose

```yaml
services:
  orca-shim:
    image: python:3.11-slim
    command: ["uvicorn", "scripts.orca_shim:app", "--host", "0.0.0.0", "--port", "9100"]
    ports: ["9100:9100"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock
    restart: unless-stopped
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9100/readyz"]

  school-core:
    build: .
    environment:
      ORCA_HTTP: http://orca-shim:9100
      ORCA_MODE: http
      MODEL_PROVIDER: nous
    depends_on:
      orca-shim:
        condition: service_healthy
```

---

## Done When

- [ ] `orca_http.py` module with all 8 endpoints
- [ ] `scripts/orca_shim.py` FastAPI daemon runs on host
- [ ] `orca_executor.py` uses HTTP when `ORCA_MODE=http`
- [ ] `docker compose up` starts both, school-core creates worktree via HTTP
- [ ] Fallback to CLI mode works for local dev (`ORCA_MODE=cli`)
