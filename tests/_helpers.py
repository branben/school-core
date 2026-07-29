"""Shared test helpers for school-core tests.

Import from here rather than conftest.py to avoid import-shadowing issues
when test subdirectories (e.g. ``tests/lenses/``) have their own conftest.py.
"""


def make_passing_review() -> dict:
    """Return a fresh PASS verdict dict for mocking ``director._run_two_judge_review``.

    Returns a new dict on every call so tests can safely mutate the result
    without corrupting other tests.
    """
    return {
        "cto_verdict": "PASS",
        "coo_verdict": "PASS",
        "cto_score": 80,
        "coo_score": 80,
        "combined_score": 80,
        "findings": [],
        "accepted": True,
    }
