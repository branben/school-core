"""Shared fixtures for school-core tests."""

import pytest


@pytest.fixture(autouse=True)
def verify_lens_module_loads():
    """Verify the lenses module loads correctly after the load-order fix.

    The constants are now defined before the LENS_PROMPTS dict, so a simple
    import should work. This fixture acts as a smoke test.
    """
    from lenses import get_lens_prompt
    from adversarial_reviewer import LensType

    assert get_lens_prompt(LensType.CORRECTNESS)
    assert get_lens_prompt(LensType.SECURITY)
    assert get_lens_prompt(LensType.COMPLETENESS)
