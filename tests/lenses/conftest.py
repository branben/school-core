"""Shared fixtures for lens prompt tests.

The lenses/__init__.py module has a load-order bug: LENS_PROMPTS dict is defined
before the _*_PROMPT constants it references, causing NameError on import.
We load the module with constants hoisted above the dict.
"""

import types
from pathlib import Path

import pytest

from adversarial_reviewer import LensType


def _load_lenses():
    src = (Path(__file__).parent.parent.parent / "lenses" / "__init__.py").read_text()
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
            # Count braces on this first line
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
    return mod


@pytest.fixture(scope="session")
def lenses_mod():
    return _load_lenses()
