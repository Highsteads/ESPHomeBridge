#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    conftest.py
# Description: Test seam for ESPHome Bridge. Installs a fake `indigo` module into
#              sys.modules BEFORE plugin.py is imported, so the plugin can be
#              exercised with no Indigo server and no ESPHome hardware. Lives at
#              the repo root so nothing here ships inside the bundle.
# Author:      CliveS & Claude Opus 4.8
# Date:        21-07-2026
# Version:     2.0

from __future__ import annotations

import importlib.util
import logging
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


# ==========================================================================
# Fake Indigo object model
#
# Only the surface plugin.py actually touches. A fake that drifts from the
# real API is worse than no fake at all, so this stays deliberately small.
# ==========================================================================

class FakeDevice:
    def __init__(self, dev_id=1, name="ESPHome Node", address="AABBCCDDEEFF",
                 device_type_id="esphomeNode", props=None, states=None):
        self.id            = dev_id
        self.name          = name
        self.address       = address
        self.deviceTypeId  = device_type_id
        self.pluginProps   = dict(props or {})
        self.ownerProps    = self.pluginProps
        self.states        = dict(states or {})
        self.state_writes  = []       # ordered audit of every write
        self.subModel      = ""
        self.onState       = False
        self.brightness    = 0
        self.refresh_calls = 0

    def updateStateOnServer(self, key, value=None, uiValue=None):
        self.states[key] = value
        if uiValue is not None:
            self.states[f"{key}.ui"] = uiValue
        self.state_writes.append((key, value, uiValue))

    def updateStatesOnServer(self, updates):
        for u in updates:
            self.updateStateOnServer(u["key"], u.get("value"), u.get("uiValue"))

    def stateListOrDisplayStateIdChanged(self):
        """Real Indigo materialises newly declared states here.

        A new Number state appears as 0.0 and a new Integer as 0 — NOT None.
        Reproducing that keeps anyone from reading a fresh 0.0 as a reading.
        """
        self.refresh_calls += 1

    def replacePluginPropsOnServer(self, props):
        self.pluginProps = dict(props)
        self.ownerProps  = self.pluginProps

    def replaceOnServer(self):
        pass


class FakeDeviceCollection(dict):
    """Stands in for indigo.devices — subscriptable by id, iterable by plugin."""

    def iter(self, _filter=None):
        return list(self.values())


class FakeServer:
    def __init__(self):
        self.lines = []        # [(message, level)]
        self.version    = "2025.2"
        self.apiVersion = "3.0"

    def log(self, message, type=None, level=None, isError=False):
        self.lines.append((message, level))

    def getInstallFolderPath(self):
        return "/tmp/fake-indigo"


class _LogCapture(logging.Handler):
    """Captures log records so tests can assert on level and text."""

    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


class FakePluginBase:
    """Minimal stand-in for indigo.PluginBase."""

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        self.pluginId          = pluginId
        self.pluginDisplayName = pluginDisplayName
        self.pluginVersion     = pluginVersion
        self.pluginPrefs       = pluginPrefs
        self.logger            = logging.getLogger(f"test.{pluginId}")
        self.logger.handlers   = [_LogCapture()]
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate  = False

    @staticmethod
    def _state_dict(key, kind):
        return {"Key": key, "Type": kind}

    def getDeviceStateDictForStringType(self, key, trigger, control):
        return self._state_dict(key, "String")

    def getDeviceStateDictForNumberType(self, key, trigger, control):
        return self._state_dict(key, "Number")

    def getDeviceStateDictForBoolOnOffType(self, key, trigger, control):
        return self._state_dict(key, "Bool")

    def getDeviceStateList(self, dev):
        return []

    def getDeviceDisplayStateId(self, dev):
        return None


_indigo = types.ModuleType("indigo")
_indigo.PluginBase = FakePluginBase
_indigo.Dict       = dict
_indigo.List       = list
_indigo.server     = FakeServer()
_indigo.devices    = FakeDeviceCollection()
_indigo.device     = MagicMock()
_indigo.variables  = MagicMock()
_indigo.variable   = MagicMock()
_indigo.trigger    = MagicMock()
for _name in ("kDeviceAction", "kDimmerAction", "kSensorAction",
              "kThermostatAction", "kUniversalAction", "kStateImageSel",
              "kProtocol"):
    setattr(_indigo, _name, MagicMock())
sys.modules["indigo"] = _indigo

sys.path.insert(0, str(SERVER_DIR))
sys.path.insert(0, str(TESTS_DIR))
os.chdir(str(SERVER_DIR))

_spec = importlib.util.spec_from_file_location("plugin", str(SERVER_DIR / "plugin.py"))
_plugin = importlib.util.module_from_spec(_spec)
sys.modules["plugin"] = _plugin
# No try/except on purpose: if plugin.py can't be imported the whole suite must
# fail loudly. Swallowing it used to let a broken module report green.
_spec.loader.exec_module(_plugin)


@pytest.fixture
def plugin_mod():
    return _plugin


@pytest.fixture
def indigo_stub():
    return _indigo


@pytest.fixture
def fake_device():
    return FakeDevice


@pytest.fixture
def plugin(plugin_mod, indigo_stub):
    """A real Plugin instance on the fake Indigo, with a clean device table."""
    indigo_stub.devices.clear()
    prefs = {"autoCreateDevices": True, "logLevel": "DEBUG", "timestampEnabled": False}
    p = plugin_mod.Plugin("com.clives.indigoplugin.esphomebridge",
                          "ESPHome Bridge", plugin_mod.PLUGIN_VERSION, prefs)
    p.log_records = p.logger.handlers[0].records
    p.log_records.clear()
    return p
