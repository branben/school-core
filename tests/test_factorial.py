"""Tests for src.math.factorial."""

import pytest

from src.math.factorial import factorial


def test_factorial_zero():
    assert factorial(0) == 1


def test_factorial_one():
    assert factorial(1) == 1


def test_factorial_five():
    assert factorial(5) == 120


def test_factorial_negative_raises():
    with pytest.raises(ValueError, match="negative"):
        factorial(-1)
