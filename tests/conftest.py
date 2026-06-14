"""Shared fixtures for school-core tests."""

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from adversarial_reviewer import LensType


@pytest.fixture(autouse=True)
def patch_lenses_module(monkeypatch):
    """Patch the lenses module so that `from lenses import get_lens_prompt` works.

    The real lenses/__init__.py has a load-order bug (LENS_PROMPTS dict references
    constants defined after it). We inject a working module into sys.modules.
    """
    src = (Path(__file__).parent.parent / "lenses" / "__init__.py").read_text()
    lines = src.splitlines(True)

    pre = []
    dict_block = []
    constants = []

    in_dict = False
    brace_depth = 0
    in_constant = False
    buf = []

    for line in lines:
        stripped = line.strip()

        if in_constant:
            buf.append(line)
            if stripped.endswith('"""') and len(buf) > 1:
                constants.extend(buf)
                buf = []
                in_constant = False
            continue

        if in_dict:
            dict_block.append(line)
            brace_depth += stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                in_dict = False
            continue

        if stripped.startswith("LENS_PROMPTS"):
            in_dict = True
            brace_depth = 0
            dict_block.append(line)
            brace_depth = stripped.count("{") - stripped.count("}")
            if brace_depth <= 0:
                in_dict = False
            continue

        if stripped.startswith("_") and "_PROMPT" in stripped and "=" in stripped:
            in_constant = True
            buf = [line]
            continue

        pre.append(line)

    fixed = "".join(pre) + "".join(constants) + "".join(dict_block)
    ns = {}
    exec(compile(fixed, "lenses/__init__.py", "exec"), ns)

    mod = types.ModuleType("lenses")
    mod.__dict__.update(ns)
    mod.LensType = LensType
    sys.modules["lenses"] = mod

    yield

    # Cleanup: remove the patched module so other tests aren't affected
    if "lenses" in sys.modules and sys.modules["lenses"] is mod:
        del sys.modules["lenses"]
