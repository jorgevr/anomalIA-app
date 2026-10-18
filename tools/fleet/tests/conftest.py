"""Loads tools/fleet/fleet.py as a module named 'fleet', independent of sys.path,
since it is a standalone script rather than an installed package."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

FLEET_PY = Path(__file__).resolve().parent.parent / "fleet.py"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _load_fleet_module():
    spec = importlib.util.spec_from_file_location("fleet", FLEET_PY)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fleet"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fleet():
    """A freshly loaded copy of the fleet module, so monkeypatches in one test
    (e.g. the mutation test) never leak into another."""
    module = _load_fleet_module()
    yield module
    sys.modules.pop("fleet", None)
