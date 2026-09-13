# Plan: Cut OmniRoute + Scope Orca Shim (REVISED)

**Goal:** Containerize school-core without breaking FirstMate.

**Key principle:** Keep OmniRoute as a `--provider omniroute|nous` flag. Cut over when metrics confirm parity. No destructive deletes in Phase 1.

---

## Phase 1: Cut OmniRoute (school-core-3d9)

### New Module: `nous_client.py`
- Direct Nous API calls (`inference-api.nousresearch.com/v1`)
- Default model: `meituan/longcat-2.0:free`
- Env var: `NOUS_API_KEY` (optional — fallback to OmniRoute if unset)
- Env var: `NOUS_MODEL` (default: `meituan/longcat-2.0:free`)
- **No destructive changes** — OmniRoute code stays behind a flag

### Model Abstraction Layer
```python
# new: model_client.py
class ModelClient(Protocol):
    def complete(self, prompt: str, **kwargs) -> str: ...

class NousClient(ModelClient): ...
class OmniRouteClient(ModelClient): ...  # existing, unchanged
```

### executor.py Changes
- Add `ModelClient` protocol
- `call_model()` dispatches based on `MODEL_PROVIDER` env var (`nous|omniroute`)
- `COMBO_MAP` stays for OmniRoute path; Nous path uses single model
- If `NOUS_API_KEY` unset and `OMNIROUTE_API_KEY` set → auto-fallback to OmniRoute

### .env.example
```bash
# Either/or — set one
NOUS_API_KEY=           # Nous direct (free models, no proxy)
OMNIROUTE_API_KEY=      # OmniRoute proxy (multi-model routing)

NOUS_MODEL=meituan/longcat-2.0:free
MODEL_PROVIDER=nous     # nous|omniroute
```

### FirstMate Safety Check
```bash
# Before any code changes:
grep -r "import.*director\|import.*conductor\|import.*executor" <firstmate_path>
# If found: add abstraction layer so FirstMate doesn't break
```

### Tests
- Audit all OmniRoute-mocked tests
- Categorize: (a) protocol-level (keep), (b) business logic (rewrite), (c) OmniRoute-specific (delete or move to integration)
- CI matrix: test both `nous` and `omniroute` providers

### Done When
- `grep -r omniroute executor.py` returns only the OmniRoute client class (not hard dependency)
- All 1497 tests pass with `MODEL_PROVIDER=nous`
- All 1497 tests pass with `MODEL_PROVIDER=omniroute` (regression check)

---

## Phase 2: Orca HTTP Shim (school-core-9v8)

### Docker Networking
- Container uses `host.docker.internal` (Docker Desktop) or Docker Compose service discovery
- Shim daemon binds to `127.0.0.1:9100` (host-only, not exposed to container network)
- No `--network host` (Linux-only, breaks macOS)

### Shim API Spec (OpenAPI)
```yaml
/worktree/create:
  post:
    body: { name: string, repo_path?: string, idempotency_key: string }
    response: { path: string, name: string, created: boolean }

/worktree/rm:
  post:
    body: { path: string, idempotency_key: string }
    response: { ok: boolean }

/worktree/list:
  get:
    response: [{ path: string, name: string, repo: string }]

/terminal/send:
  post:
    body: { handle: string, text: string, enter?: boolean }
    response: { ok: boolean }

/readyz:
  get:
    response: { ok: boolean }
```

### Host Daemon: `scripts/orca_shim.py`
- FastAPI, port 9100, bind 127.0.0.1 only
- Auth: Bearer <REDACTED> from `ORCA_SHIM_TOKEN` env (optional but recommended)
- **No `shell=True`** — use allowlist of `orca` subcommands, `shlex.split()` for args
- Idempotency: track keys in-memory, dedupe repeats
- Health check: `/readyz` returns 200 when Orca CLI responds

### Client Module: `orca_http.py`
```python
class OrcaHTTPClient:
    def __init__(self, base_url: str = "http://host.docker.internal:9100", token: str = None):
        self.client = httpx.Client(base_url=base_url, timeout=30.0)
        self.token = token

    def create_worktree(self, name: str, repo_path: str = None, idempotency_key: str = None) -> dict: ...
    def remove_worktree(self, path: str, idempotency_key: str = None) -> dict: ...
    def list_worktrees(self) -> list: ...
    def send_terminal(self, handle: str, text: str) -> dict: ...
```

### orca_executor.py Migration
- Add `orca_mode` flag: `http` (default) or `cli` (fallback)
- `http` mode: uses `OrcaHTTPClient`
- `cli` mode: existing `subprocess.run(["orca", ...])`
- Startup retry with exponential backoff (5s, 10s, 20s) if shim unreachable

### Docker Compose (REQUIRED)
```yaml
services:
  orca-shim:
    build: { context: ., dockerfile: Dockerfile.shim }
    ports: ["9100:9100"]
    volumes:
      - /var/run/docker.sock:/var/run/docker.sock  # if needed
      - orca-workspaces:/workspaces
    healthcheck:
      test: ["CMD", "curl", "-f", "http://localhost:9100/readyz"]
      interval: 5s
      timeout: 3s
      retries: 5
    restart: unless-stopped

  school-core:
    build: .
    depends_on:
      orca-shim:
        condition: service_healthy
    environment:
      ORCA_HTTP: http://orca-shim:9100
      MODEL_PROVIDER: nous
    volumes:
      - ./data:/app/data

volumes:
  orca-workspaces:
```

### Done When
- `orca_http.py` has all 4 endpoints + idempotency
- `orca_shim.py` runs, responds to all 4 endpoints, rejects invalid input
- Docker Compose: `docker compose up` starts both, school-core uses HTTP
- `orca_mode=cli` fallback works for local dev

---

## Phase 3: Package Profiles + Setup

### Profiles (TEMPLATES, not live configs)
```
config/profiles/
├── _TEMPLATES/              # committed
│   ├── student-coder/
│   │   └── SOUL.md          # with {{PLACEHOLDER}} values
│   ├── student-reviewer/
│   └── ...
└── student-coder/           # gitignored — user's local copy
    └── SOUL.md
```

```gitignore
# .gitignore
config/profiles/*/
!config/profiles/_TEMPLATES/
```

### setup.sh Improvements
- `--dry-run` mode: show what would happen without doing it
- Detect existing profiles: prompt before overwrite
- `--profile-dir` flag for non-default paths
- Hermes optional: warn if missing, don't fail
- Copy templates → user profiles, substitute placeholders

### Documented Setup
```bash
# Local dev (with Orca + OmniRoute):
./setup.sh
python3 conductor.py --serve

# Docker (Nous + Orca shim):
docker compose up
```

### Done When
- `./setup.sh --dry-run` shows actions without executing
- Existing profiles are preserved (not overwritten)
- `docker compose up` works end-to-end
- New user can run without OmniRoute or Hermes installed

---

## Rollback Strategy

| Scenario | Rollback |
|----------|----------|
| Nous API outage | Set `MODEL_PROVIDER=omniroute` + `OMNIROUTE_API_KEY` |
| Shim daemon crash | `orca_mode=cli` flag (requires Orca on host) |
| Test regression | `MODEL_PROVIDER=omniroute` in CI matrix |
| Quality collapse | Keep both providers, measure before cutting over |

**No code is deleted in Phase 1.** OmniRoute stays as a flag-gated path until metrics confirm Nous parity.

---

## Execution Order

1. **Phase 1** (cut OmniRoute) — unblocks testing, keeps rollback
2. **Phase 2** (Orca shim) — unblocks containerization
3. **Phase 3** (profiles + setup) — polish, one-command install

**Estimated:** 2-3 days focused work.
