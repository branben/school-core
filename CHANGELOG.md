# Changelog

## [Unreleased]

### Added
- CONTRIBUTING.md with contributing guidelines and development workflow.
- CODE_OF_CONDUCT.md (Contributor Covenant 2.1).
- OSS-readiness audit (`docs/solutions/oss-readiness-audit-2026-07-28.md`).
- Regression tests for `executor._resolve_api_key()` (env-only credential).

### Fixed
- **Security:** removed a hardcoded live OmniRoute API key from `executor.py` and
  `.scratch/diag_omniroute.py` — credentials are now env-only
  (`OMNIROUTE_API_KEY`) and fail loudly when unset.
- CI: `tests/test_leaf.py::test_run_task_delegates_to_director` mock expectation
  drifted when `skip_readiness` was added to `run_task()` — updated, suite green
  (942 passed).
- `scripts/terminal_drift_check.sh` — repo root now derived from the script
  location instead of a hardcoded user path.
- `.scratch/` untracked and gitignored (local diagnostic files only).

## [0.1.0] — 2026-07-27

### Added
- Pedagogy-first README restore (commit `7b8025f`).
- Three-tier role architecture (Principal / Teacher / Student).
- Doubt-Driven Development gate in Principal dispatch.
- Compound Engineering router (`scripts/ce_router.py`, `scripts/ce_runner.py`).
- Spec gate with DOD criteria checking (`scripts/spec_gate.py`).
- Student planning integration (`scripts/student_plan.py`).
- Principal doubt cycle (`principal_doubt.py`).
- School Mail notification (`school_mail.py`).
- Teacher review script (`scripts/run_teacher_review_once.py`).
- Cross-repo dispatch (PR #37, squash `3509611`).

### Changed
- README restored to accurate state — removed references to deleted modules.
- `conductor.py` — added DDD doubt cycle and CE router integration.
- `director.py` — added DOD gate and student plan extraction.
- `leaf.py` — added `complex_task` and `dod_gate` parameters to `run_task()`.

### Fixed
- Multiple multi-repo isolation bugs from PR #39 (PR #40, commit `6d1caed`).
