# GATE 4: TDD — Red → Green → Refactor

> Implementation: `verify_gate.py` (test step) + CI
> Upstream spec: `docs/sdlc/loops.md` (inner loop, Validate phase)

**Iron law:** Failing test first, then make it pass.

**Vehicle:**
- CI: `.github/workflows/school-loop.yml` (test step)
- Local: `pytest tests/ -q` (Python) / `pnpm test` (TypeScript)
- Skill: `test-driven-development`
