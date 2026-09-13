# GATE 9: AUDIT — Deep Quality Review

> Implementation: `director/scoring.py` + `thermo-nuclear-code-quality-review` skill
> Upstream spec: `docs/sdlc/loops.md` (inner loop, Review phase)

**Iron law:** Zero high-severity findings before merge.

**Vehicle:**
- Skill: `thermo-nuclear-code-quality-review`
- Integration: `director/scoring.py` `_acceptance_checks_from_spec`
- Serena: impact analysis (`find_referencing_symbols`)
- Anti-slop: `anti-slop` skill
