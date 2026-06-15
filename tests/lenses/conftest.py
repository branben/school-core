"""Shared fixtures for lens prompt tests."""

import pytest

from adversarial_reviewer import LensType


@pytest.fixture(scope="session")
def lenses_mod():
    """Load the lenses module and return it for test use."""
    from lenses import get_lens_prompt
    return get_lens_prompt
