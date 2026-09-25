"""Tests for the bounded pytest execution configuration."""

import pytest


def test_timeout_plugin_is_configured(pytestconfig):
    assert str(pytestconfig.getini("timeout")) == "60"
    assert pytestconfig.getini("timeout_method") == "thread"
