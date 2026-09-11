"""FastAPI daemon wrapping Orca CLI — runs on host, not in container."""
import subprocess
import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="Orca Shim", version="1.0.0")

ORCA_BIN = os.environ.get("ORCA_BIN", "orca")


@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """Catch any unhandled exception and return 503."""
    from fastapi.responses import JSONResponse
    return JSONResponse(status_code=503, content={"detail": str(exc)})


def run_orca(args: list[str], timeout: int = 30) -> dict:
    """Run orca CLI and return parsed JSON."""
    cmd = [ORCA_BIN] + args + ["--json"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise HTTPException(503, f"Orca timed out ({timeout}s): {' '.join(args)}")
    except FileNotFoundError:
        raise HTTPException(503, "Orca CLI not found (is Orca installed?)")

    if proc.returncode != 0:
        raise HTTPException(503, f"Orca failed: {proc.stderr[:300]}")

    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise HTTPException(503, f"Invalid JSON from Orca: {proc.stdout[:200]}")


# ── Health ──────────────────────────────────────────────────────────────────


@app.get("/readyz")
def readyz():
    try:
        result = run_orca(["status"])
        runtime = result.get("runtime", {})
        if runtime.get("state") == "running":
            return {"ok": True}
        raise HTTPException(503, f"Not ready: {runtime.get('state')}")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(503, f"Orca not ready: {e}")


@app.get("/status")
def status():
    return run_orca(["status"])


# ── Worktree ────────────────────────────────────────────────────────────────


class WorktreeCreateBody(BaseModel):
    name: str
    repo_path: Optional[str] = None


class WorktreeRmBody(BaseModel):
    path: str


@app.post("/worktree/create")
def worktree_create(body: WorktreeCreateBody):
    args = ["worktree", "create", "--name", body.name]
    if body.repo_path:
        args += ["--repo", body.repo_path]
    return run_orca(args)


@app.post("/worktree/rm")
def worktree_rm(body: WorktreeRmBody):
    return run_orca(["worktree", "rm", "--worktree", f"path:{body.path}", "--force"])


@app.get("/worktree/list")
def worktree_list():
    return run_orca(["worktree", "list"])


@app.post("/worktree/prune")
def worktree_prune():
    return run_orca(["worktree", "prune"])


# ── Repo ────────────────────────────────────────────────────────────────────


class RepoAddBody(BaseModel):
    path: str


@app.get("/repo/list")
def repo_list():
    return run_orca(["repo", "list"])


@app.post("/repo/add")
def repo_add(body: RepoAddBody):
    return run_orca(["repo", "add", "--path", body.path])


# ── Terminal ────────────────────────────────────────────────────────────────


class TerminalSendBody(BaseModel):
    handle: str
    text: str
    enter: bool = True


class TerminalCloseBody(BaseModel):
    handle: str


@app.post("/terminal/send")
def terminal_send(body: TerminalSendBody):
    args = ["terminal", "send", "--terminal", body.handle, body.text]
    if not body.enter:
        args.append("--no-enter")
    return run_orca(args)


@app.post("/terminal/close")
def terminal_close(body: TerminalCloseBody):
    return run_orca(["terminal", "close", "--terminal", body.handle])
