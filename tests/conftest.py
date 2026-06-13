#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    conftest.py
# Description: Pytest scaffold for the device-zoo tests. Installs a minimal
#              `indigo` module stub BEFORE plugin.py is imported, loads the
#              plugin module from the bundle, and exposes it (and an entity
#              builder) as fixtures. Repo-root tests/ so fixtures stay OUT of the
#              shipped bundle. ESPHomeBridge had no test suite before this.
# Author:      CliveS & Claude Opus 4.8
# Date:        13-06-2026

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

THIS       = Path(__file__).resolve()
TESTS_DIR  = THIS.parent
REPO_ROOT  = TESTS_DIR.parent
SERVER_DIR = REPO_ROOT / "ESPHomeBridge.indigoPlugin" / "Contents" / "Server Plugin"

# ── indigo stub ──────────────────────────────────────────────────────────────
_indigo = types.ModuleType("indigo")
_indigo.PluginBase = object
_indigo.Dict       = dict
_indigo.List       = list
_indigo.server     = MagicMock()
_indigo.server.version    = "2025.2"
_indigo.server.apiVersion = "3.0"
_indigo.devices    = MagicMock()
_indigo.variables  = MagicMock()
_indigo.variable   = MagicMock()
_indigo.trigger    = MagicMock()
for _name in ("kDeviceAction", "kDimmerAction", "kSensorAction",
              "kUniversalAction", "kStateImageSel", "kProtocol"):
    setattr(_indigo, _name, MagicMock())
sys.modules["indigo"] = _indigo

sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(TESTS_DIR))
os.chdir(str(SERVER_DIR))

_spec = importlib.util.spec_from_file_location("plugin", str(SERVER_DIR / "plugin.py"))
_plugin = importlib.util.module_from_spec(_spec)
sys.modules["plugin"] = _plugin
try:
    _spec.loader.exec_module(_plugin)
except Exception:  # noqa: BLE001 - module-level defs/class are what we test
    pass


@pytest.fixture
def plugin_mod():
    return _plugin
