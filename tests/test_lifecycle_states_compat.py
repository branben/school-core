"""py3.9 compat: lifecycle_states must import on Python 3.9 (CI matrix floor).

StrEnum is 3.11+; on 3.9 the import itself fails (CI run 36073807514,
py3.9 job: ImportError: cannot import name 'StrEnum' from 'enum').
The module must use a 3.9-compatible enum base.
"""
import sys


def test_lifecycle_states_imports_on_39():
    # This test is trivial on 3.11+ but is the guard on 3.9: if the module
    # imports at all, the compat fix holds.
    import lifecycle_states  # noqa: F401
    assert hasattr(lifecycle_states, "LifecycleState")
    # And the enum members must behave like strings (the StrEnum contract)
    assert lifecycle_states.LifecycleState.MERGED == "merged"
