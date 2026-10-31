---
title: Residual Observability Landing Manifest
type: ops
status: proposed
date: 2026-08-11
scope: school-core residual observability and control-plane changes after U1-U6
---

# Residual Observability Landing Manifest

> Canonical path: `docs/plans/2026-08-11-002-residual-observability-landing-manifest.md`.
> This change set starts from `main@7281763`, where U1-U6 are already committed.
> It must not alter or restage the U1-U6 commit.

## Purpose

Land the remaining control-plane and observability work that was intentionally
kept separate from U1-U6:

- shared AgentMail transport and inbound poller cleanup;
- readable notification cards and pipeline-blocked alerts;
- CI and school-loop alert wiring;
- Entire/Beads hook and Claude settings integration;
- environment and notification documentation.

## Exact include list

Stage these files as residual work:

```text
.env.example
.github/workflows/ci.yml
.github/workflows/school-loop.yml
.beads/hooks/pre-push
.beads/hooks/prepare-commit-msg
.beads/hooks/commit-msg
.beads/hooks/post-commit
.beads/hooks/post-rewrite
.beads/hooks/pre-push.pre-entire
.beads/hooks/prepare-commit-msg.pre-entire
.claude/settings.json
README.md
school_mail.py
scripts/school_inbound.py
src/agentmail_poller.py
agentmail_client.py
tests/test_school_mail.py
tests/test_agentmail_poller.py
docs/notification-style-guide.md
docs/plans/2026-08-11-002-residual-observability-landing-manifest.md
```

## Scope notes

- `README.md` contains only residual AgentMail/notification additions relative
  to `main@7281763`; the earlier Entire documentation is already committed in
  U1-U6.
- `.github/workflows/school-loop.yml` contains residual runner alert,
  concurrency, and hosted-board resilience changes. U2/U3 workflow changes are
  already in the base commit.
- `.github/workflows/ci.yml` contains residual CI notification wiring only.
- `.beads/hooks/*` and `.claude/settings.json` are operational Entire hook
  integration. They are intentionally separate from the U1-U6 code landing.
- `school_mail.py`, `agentmail_client.py`, `scripts/school_inbound.py`, and
  `src/agentmail_poller.py` form one shared transport/control-plane slice.
- `tests/test_school_mail.py` and `tests/test_agentmail_poller.py` cover the
  notification and inbound behavior. Do not add live network calls to tests.

## Explicit exclusions

Do not stage or modify:

```text
docs/plans/2026-08-11-001-u1-u6-landing-manifest.md
src/qodo_pre_merge.py
src/entire_review.py
crew_dispatch.py
tests/test_crew_dispatch.py
data/crew_runs.json
```

The U1-U6 implementation files are already at `main@7281763`; do not create a
second commit containing them.

## Validation contract

Before committing:

```bash
.venv/bin/python -m py_compile \
  school_mail.py agentmail_client.py scripts/school_inbound.py \
  src/agentmail_poller.py tests/test_school_mail.py \
  tests/test_agentmail_poller.py

.venv/bin/python - <<'PY'
from pathlib import Path
import yaml
for path in ['.github/workflows/ci.yml', '.github/workflows/school-loop.yml']:
    yaml.safe_load(Path(path).read_text())
    print(path, 'YAML-OK')
PY

.venv/bin/python -m pytest \
  tests/test_school_mail.py tests/test_agentmail_poller.py -q

git diff --cached --check
```

Do not push automatically. Report the exact committed path set and any
residual failure separately.
