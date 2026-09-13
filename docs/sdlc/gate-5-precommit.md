# GATE 5: PRE-COMMIT — Security + Quality

> Implementation: `school-loop.yml` (pre-commit step) + `requesting-code-review` skill
> Upstream spec: `docs/sdlc/loops.md` (inner loop, Validate phase)

**Iron law:** Zero findings before commit.

**Vehicle:**
- CI: `.github/workflows/school-loop.yml` (lint + typecheck + security scan)
- Local: `pre-commit run --all-files`
- Skill: `requesting-code-review`
