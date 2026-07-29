# CI Repo-Agnostic Fix — Wayfinder Map

## Problem
`.github/workflows/ci.yml` line 30 has a hardcoded list of 8 root-level
`.py` files for the syntax check step. When the framework is cloned as
a different repo (e.g. sound-royale-ny), this list either includes
non-existent files or omits real ones.

## Frontiers

F1: **What does compileall actually check?** Verify that `python -m compileall -q .`
fails on anything that matters (`.venv`, `__pycache__`, etc.) and what the
right include/exclude pattern is.

F2: **What module files exist at root?** Get the full list of `.py` files
at repo root that are part of the framework (not vendored deps).

F3: **How do other repos handle this?** Check how sound-royale-ny and
other school-core clones currently deal with ci.yml — do they override
it, fork it, or leave it hardcoded?

F4: **Dynamic discovery pattern** — what's the cleanest repo-agnostic
approach? Options:
  a) ✅ `python -m compileall -q .` — **DONE** (implemented in ci.yml line 30)
  b) ~Explicit list from `glob("*.py")` minus known excluded dirs~
  c) ~A script that generates the list dynamically~

F5: **Impact of the `__self__` sentinel on workflows** — the `school-loop.yml`
and `ci.yml` changes need to work together. Does ci.yml also need
`SCHOOL_REPO`-awareness or is it purely a syntax check?

## Hypothesis
The simplest fix (F4a) is `python -m compileall -q .` which recursively
compiles all `.py` files. We just need to verify it excludes `.venv` and
`__pycache__` automatically (compileall skips those by default on Python 3).
