"""Shared fixtures for lens prompt tests."""

import pytest


@pytest.fixture(scope="session")
def lenses_mod():
    """Load the lenses module for test use."""
    import lenses
    return lenses
