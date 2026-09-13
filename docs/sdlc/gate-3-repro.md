# GATE 3: REPRO — Systematic Debugging

> Implementation: `.agents/skills/systematic-debugging/` + Serena MCP
> Upstream spec: `docs/sdlc/loops.md` (inner loop, Validate phase)

**Iron law:** No fix without a confirmed reproduction and a failing test.

**Vehicle:**
- Skill: `systematic-debugging` (4-phase root cause)
- Symbol trace: Serena `find_symbol` / `find_referencing_symbols`
- Contract: `tests/test_bug.py` MUST fail before fix, MUST pass after
