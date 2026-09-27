#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_actions_and_status.py
# Description: Regression tests for the 0.10.0 fixes found while writing the
#              guide: Connected/Status now follow the connection whether or not
#              auto-create is on, Status Request re-writes the node's latest
#              readings, Toggle works on a cover, and a colour-temperature
#              change no longer sends the light black.
# Author:      CliveS & Claude Opus 5.5
# Date:        27-09-2026
# Version:     1.0

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")

MAC = "AABBCC667788"   # stand-in — never a real MAC from the author's LAN
KEY = 5


class RunNowLoop:
    """Stands in for the plugin's asyncio loop: runs scheduled calls at once."""

    def is_running(self):
        return True

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def _live_client():
    client = MagicMock()
    client._connection = SimpleNamespace(is_connected=True)
    return client


def _node(plugin, fake_device, indigo_stub, type_id, kind, states=None, on=False):
    em = {"primary": {"key": KEY, "kind": kind, "name": kind, "unit": ""}}
    dev = fake_device(dev_id=11, address=MAC, device_type_id=type_id,
                      props={"entityKeyMap": json.dumps(em)}, states=states)
    dev.onState = on
    indigo_stub.devices[11] = dev
    plugin.async_loop = RunNowLoop()
    return dev


def _connect(plugin, client=None):
    client = client or _live_client()
    plugin.connections[MAC] = {"client": client, "info": object(),
                               "entities": {}, "states": {}}
    return client


def _action(indigo_stub, name, value=None):
    return SimpleNamespace(deviceAction=getattr(indigo_stub.kDeviceAction, name),
                           actionValue=value)


# ── Status Request ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("type_id,kind,state,key,expected", [
    ("esphomeSwitch", "switch",
     lambda: aioesphomeapi.SwitchState(key=KEY, state=True), "onOffState", True),
    ("esphomeLight", "light",
     lambda: aioesphomeapi.LightState(key=KEY, state=True, brightness=0.4),
     "brightnessLevel", 40),
    ("esphomeCover", "cover",
     lambda: aioesphomeapi.CoverState(key=KEY, position=0.25), "brightnessLevel", 25),
])
def test_status_request_rewrites_the_latest_reading(plugin, fake_device, indigo_stub,
                                                    type_id, kind, state, key, expected):
    dev = _node(plugin, fake_device, indigo_stub, type_id, kind)
    _connect(plugin)
    plugin._on_entity_state(MAC, state())
    dev.states.clear()                                   # as if the state drifted
    plugin.actionControlDevice(_action(indigo_stub, "RequestStatus"), dev)
    assert dev.states.get(key) == expected


def test_status_request_by_its_universal_name(plugin, fake_device, indigo_stub):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeSwitch", "switch")
    _connect(plugin)
    plugin._on_entity_state(MAC, aioesphomeapi.SwitchState(key=KEY, state=True))
    dev.states.clear()
    action = SimpleNamespace(deviceAction=indigo_stub.kUniversalAction.RequestStatus,
                             actionValue=None)
    plugin.actionControlDevice(action, dev)
    assert dev.states.get("onOffState") is True


def test_status_request_says_when_nothing_has_arrived(plugin, fake_device, indigo_stub):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeSwitch", "switch")
    _connect(plugin)
    plugin.actionControlDevice(_action(indigo_stub, "RequestStatus"), dev)
    assert any("no readings yet" in r.getMessage() for r in plugin.log_records)


def test_status_request_when_disconnected_warns_and_says_so(plugin, fake_device, indigo_stub):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeSwitch", "switch",
                states={"connected": True, "status": "Online"})
    plugin.actionControlDevice(_action(indigo_stub, "RequestStatus"), dev)
    assert any(r.levelno == logging.WARNING and "not connected" in r.getMessage()
               for r in plugin.log_records)
    assert dev.states["connected"] is False
    assert dev.states["status"] == "Disconnected"


# ── Toggle ───────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("is_open,position", [(True, 0.0), (False, 1.0)])
def test_cover_toggle(plugin, fake_device, indigo_stub, is_open, position):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeCover", "cover", on=is_open)
    client = _connect(plugin)
    plugin.actionControlDevice(_action(indigo_stub, "Toggle"), dev)
    client.cover_command.assert_called_once_with(key=KEY, position=position)


def test_switch_toggle(plugin, fake_device, indigo_stub):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeSwitch", "switch", on=True)
    client = _connect(plugin)
    plugin.actionControlDevice(_action(indigo_stub, "Toggle"), dev)
    client.switch_command.assert_called_once_with(key=KEY, state=False)


def test_light_toggle(plugin, fake_device, indigo_stub):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeLight", "light", on=False)
    client = _connect(plugin)
    plugin.actionControlDevice(_action(indigo_stub, "Toggle"), dev)
    assert client.light_command.call_args.kwargs["state"] is True


# ── Colour levels ────────────────────────────────────────────────────────────

def test_colour_temperature_change_does_not_send_black(plugin, fake_device, indigo_stub):
    dev = _node(plugin, fake_device, indigo_stub, "esphomeLight", "light", on=True)
    client = _connect(plugin)
    plugin.actionControlDevice(_action(indigo_stub, "SetColorLevels",
                                       {"whiteTemperature": 2700}), dev)
    sent = client.light_command.call_args.kwargs
    assert "rgb" not in sent
    assert sent["color_temperature"] == pytest.approx(1_000_000 / 2700)


def test_partial_rgb_keeps_the_other_channels(plugin_mod):
    out = plugin_mod.light_colour_kwargs({"redLevel": 50},
                                         {"greenLevel": 20, "blueLevel": 100})
    assert out == {"rgb": (0.5, 0.2, 1.0)}


def test_full_rgb_is_unchanged(plugin_mod):
    out = plugin_mod.light_colour_kwargs(
        {"redLevel": 100, "greenLevel": 0, "blueLevel": 50}, {})
    assert out == {"rgb": (1.0, 0.0, 0.5)}


def test_white_level_is_sent_as_white(plugin_mod):
    assert plugin_mod.light_colour_kwargs({"whiteLevel": 60}, {}) == {"white": 0.6}


@pytest.mark.parametrize("value", [{}, {"whiteTemperature": 0},
                                   {"whiteTemperature": "rubbish"}])
def test_nothing_usable_sends_nothing(plugin, plugin_mod, fake_device, indigo_stub, value):
    assert plugin_mod.light_colour_kwargs(value, {}) == {}
    dev = _node(plugin, fake_device, indigo_stub, "esphomeLight", "light", on=True)
    client = _connect(plugin)
    plugin.actionControlDevice(_action(indigo_stub, "SetColorLevels", value), dev)
    client.light_command.assert_not_called()


# ── Connected / Status ───────────────────────────────────────────────────────

class OnceThenGoneClient:
    """Connects once and then drops; every later attempt is refused."""

    connects = 0

    def __init__(self, *args, **kwargs):
        self._connection = None

    async def connect(self, login=True):
        OnceThenGoneClient.connects += 1
        if OnceThenGoneClient.connects > 1:
            raise aioesphomeapi.APIConnectionError("refused")
        self._connection = SimpleNamespace(is_connected=True)

    async def device_info(self):
        return SimpleNamespace(name="esp-test", esphome_version="2026.9.0", model="esp32")

    async def list_entities_services(self):
        return [], []

    def subscribe_states(self, _cb):
        self._connection = SimpleNamespace(is_connected=False)   # drops at once
        return lambda: None

    async def disconnect(self):
        return None


def test_manual_device_goes_online_with_auto_create_off(plugin, plugin_mod, fake_device,
                                                        indigo_stub, monkeypatch):
    async def _no_sleep(_secs):
        return None

    OnceThenGoneClient.connects = 0
    monkeypatch.setattr(aioesphomeapi, "APIClient", OnceThenGoneClient)
    monkeypatch.setattr(plugin_mod.asyncio, "sleep", _no_sleep)
    plugin.auto_create_nodes = False
    plugin.discovered[MAC] = {"hostname": "esp-test", "ip": "192.168.1.66", "port": 6053}
    dev = fake_device(dev_id=12, address=MAC, device_type_id="esphomeNode",
                      states={"connected": False, "status": "Disconnected"})
    indigo_stub.devices[12] = dev

    asyncio.run(plugin._connect_to_device(MAC))

    assert ("connected", True, None) in dev.state_writes
    assert ("status", "Online", None) in dev.state_writes
    assert dev.states["connected"] is False              # and back down on the drop
    assert dev.states["status"] == "Disconnected"


def test_start_clears_a_stale_online(plugin, fake_device, indigo_stub):
    dev = fake_device(dev_id=13, address=MAC, device_type_id="esphomeSwitch",
                      states={"connected": True, "status": "Online"})
    indigo_stub.devices[13] = dev
    plugin.deviceStartComm(dev)
    assert dev.states["connected"] is False
    assert dev.states["status"] == "Disconnected"


def test_start_while_connected_says_online(plugin, fake_device, indigo_stub):
    dev = fake_device(dev_id=14, address=MAC, device_type_id="esphomeSwitch",
                      states={"connected": False, "status": "Disconnected"})
    indigo_stub.devices[14] = dev
    _connect(plugin)
    plugin.deviceStartComm(dev)
    assert dev.states["connected"] is True
    assert dev.states["status"] == "Online"


def test_start_keeps_a_bad_key_status(plugin, plugin_mod, fake_device, indigo_stub):
    dev = fake_device(dev_id=15, address=MAC, device_type_id="esphomeNode",
                      states={"connected": False, "status": "Bad key"})
    indigo_stub.devices[15] = dev
    plugin.parked[MAC] = {"reason": plugin_mod.PARK_NEEDS_KEY, "since": 0, "failures": 0}
    plugin.async_loop = None                             # no retry thread in the test
    plugin.deviceStartComm(dev)
    assert dev.states["status"] == "Bad key"
    assert dev.state_writes == []


def test_start_does_not_rewrite_states_that_are_already_right(plugin, fake_device,
                                                              indigo_stub):
    dev = fake_device(dev_id=16, address=MAC, device_type_id="esphomeSwitch",
                      states={"connected": False, "status": "Disconnected"})
    indigo_stub.devices[16] = dev
    plugin.deviceStartComm(dev)
    assert dev.state_writes == []


# ── Light colour capabilities ────────────────────────────────────────────────

def test_capability_bits_match_the_library(plugin_mod):
    cap = aioesphomeapi.LightColorCapability
    assert plugin_mod.CAP_WHITE == cap.WHITE
    assert plugin_mod.CAP_COLOR_TEMPERATURE == cap.COLOR_TEMPERATURE
    assert plugin_mod.CAP_COLD_WARM_WHITE == cap.COLD_WARM_WHITE
    assert plugin_mod.CAP_RGB == cap.RGB


_FLAGS = ("SupportsColor", "SupportsRGB", "SupportsWhite", "SupportsWhiteTemperature")


@pytest.mark.parametrize("mode,expected", [
    ("ON_OFF",                (False, False, False, False)),
    ("BRIGHTNESS",            (False, False, False, False)),
    ("WHITE",                 (True,  False, True,  False)),
    ("COLOR_TEMPERATURE",     (True,  False, True,  True)),
    ("COLD_WARM_WHITE",       (False, False, False, False)),   # no mired range known
    ("RGB",                   (True,  True,  False, False)),
    ("RGB_WHITE",             (True,  True,  True,  False)),
    ("RGB_COLOR_TEMPERATURE", (True,  True,  True,  True)),
    ("RGB_COLD_WARM_WHITE",   (True,  True,  False, False)),   # no mired range known
])
def test_each_colour_mode_family(plugin_mod, mode, expected):
    props = plugin_mod.light_capability_props([aioesphomeapi.ColorMode[mode]])
    assert tuple(props[f] for f in _FLAGS) == expected


@pytest.mark.parametrize("mode", ["COLD_WARM_WHITE", "RGB_COLD_WARM_WHITE"])
def test_cold_warm_white_gets_temperature_once_its_range_is_known(plugin_mod, mode):
    props = plugin_mod.light_capability_props([aioesphomeapi.ColorMode[mode]], 153, 500)
    assert props["SupportsWhiteTemperature"] and props["SupportsWhite"] and props["SupportsColor"]
    assert props["WhiteTemperatureMin"] == "2000"
    assert props["WhiteTemperatureMax"] == "6536"


def test_a_light_offering_several_modes_gets_all_their_flags(plugin_mod):
    modes = [aioesphomeapi.ColorMode.RGB, aioesphomeapi.ColorMode.COLOR_TEMPERATURE]
    props = plugin_mod.light_capability_props(modes, 153, 370)
    assert all(props[f] for f in _FLAGS)


def test_nonsense_modes_and_range_give_no_flags(plugin_mod):
    props = plugin_mod.light_capability_props(["x", None], "rubbish", None)
    assert not any(props[f] for f in _FLAGS)
    assert "WhiteTemperatureMin" not in props


def test_light_props_come_from_the_capability_bits(plugin):
    light = aioesphomeapi.LightInfo(
        key=KEY, name="Lamp", object_id="lamp",
        supported_color_modes=[aioesphomeapi.ColorMode.RGB_COLOR_TEMPERATURE],
        min_mireds=153, max_mireds=500)
    props = plugin._props_for_primary("esphomeLight", light)
    assert props["SupportsWhiteTemperature"] is True
    assert props["SupportsRGB"] is True
