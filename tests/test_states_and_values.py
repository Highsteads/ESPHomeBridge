#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_states_and_values.py
# Description: Contract tests for the parts of ESPHome Bridge that turn an
#              ESPHome entity into an Indigo state: state-ID allocation,
#              value writing (including the NaN trap), preference coercion,
#              log levels and the v0.4.0 migration guard.
# Author:      CliveS & Claude Opus 4.8
# Date:        21-07-2026
# Version:     1.0

from __future__ import annotations

import json
import logging

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")


# ── Preference coercion ──────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    (True, True), (False, False),
    ("true", True), ("false", False),          # Indigo re-serialises checkboxes
    ("True", True), ("False", False),
    ("yes", True), ("no", False),
    ("1", True), ("0", False),
    (1, True), (0, False),
    ("", False),
])
def test_as_bool(plugin_mod, raw, expected):
    assert plugin_mod.as_bool(raw) is expected


def test_as_bool_false_string_is_not_true(plugin_mod):
    """bool("false") is True — the whole reason this helper exists."""
    assert bool("false") is True
    assert plugin_mod.as_bool("false") is False


def test_as_bool_falls_back_on_nonsense(plugin_mod):
    assert plugin_mod.as_bool("banana", True) is True
    assert plugin_mod.as_bool(None, True) is True


def test_saved_checkbox_string_does_not_flip_auto_create(plugin_mod):
    p = plugin_mod.Plugin("id", "ESPHome Bridge", "0.0.0",
                          {"autoCreateDevices": "false"})
    assert p.auto_create_nodes is False


# ── Log levels ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name,expected", [
    ("DEBUG", logging.DEBUG), ("info", logging.INFO),
    ("WARNING", logging.WARNING), ("ERROR", logging.ERROR),
    ("nonsense", logging.INFO), (logging.WARNING, logging.WARNING),
])
def test_lvl_maps_names_to_ints(plugin_mod, name, expected):
    """indigo.server.log(level=...) needs an int — a string logs as Info."""
    assert plugin_mod._lvl(name) == expected


# ── State-ID allocation ──────────────────────────────────────────────────────

def _entities(specs):
    """[(class name, entity name)] -> real aioesphomeapi EntityInfo objects."""
    out = []
    for i, (cls_name, name) in enumerate(specs):
        cls = getattr(aioesphomeapi, cls_name)
        out.append(cls(key=i + 1, name=name, object_id=name.lower().replace(" ", "_")))
    return out


def _map(plugin, specs, primary=None):
    return plugin._build_entity_key_map(_entities(specs), primary)


def test_entity_cannot_shadow_a_declared_state(plugin):
    """A firmware entity called "Status" must not land on the connection status."""
    em = _map(plugin, [("TextSensorInfo", "Status")])
    assert "status" not in em
    assert any(v["name"] == "Status" for v in em.values())


def test_entity_cannot_take_a_reserved_native_name(plugin):
    """batteryLevel is a native Indigo property — writes to it vanish."""
    em = _map(plugin, [("SensorInfo", "Battery Level")])
    assert "batteryLevel" not in em
    assert "battery" in em


def test_two_battery_sensors_still_get_distinct_ids(plugin):
    em = _map(plugin, [("SensorInfo", "Battery Level"), ("SensorInfo", "Battery")])
    ids = set(em)
    assert "batteryLevel" not in ids
    assert len(ids) == 2


def test_level_suffix_is_reserved_for_secondary_lights(plugin):
    """A secondary light owns "<id>Level" too, so a same-named sensor moves."""
    em = _map(plugin, [("LightInfo", "Status LED"), ("SensorInfo", "Status LED Level")])
    light_id = next(k for k, v in em.items() if v["kind"] == "light")
    sensor_id = next(k for k, v in em.items() if v["kind"] == "sensor")
    assert sensor_id != light_id + "Level"


def test_ordinary_names_are_untouched(plugin):
    em = _map(plugin, [("SensorInfo", "Power"), ("SensorInfo", "Total Energy")])
    assert set(em) == {"power", "totalEnergy"}


def test_node_info_states_can_still_be_owned_by_firmware(plugin):
    """Athom exports its own IP Address sensor — that one is welcome to win."""
    em = _map(plugin, [("TextSensorInfo", "IP Address")])
    assert "ipAddress" in em


def test_duplicate_entity_names_are_suffixed(plugin):
    em = _map(plugin, [("SensorInfo", "Power"), ("SensorInfo", "Power")])
    assert set(em) == {"power", "power2"}


def test_primary_entity_is_mapped_under_primary(plugin):
    ents = _entities([("SwitchInfo", "Relay"), ("SensorInfo", "Power")])
    em = plugin._build_entity_key_map(ents, ents[0])
    assert em["primary"]["kind"] == "switch"
    assert "relay" not in em


def test_allocated_ids_are_camel_case_ascii(plugin):
    em = _map(plugin, [("SensorInfo", "WiFi Signal dB"), ("SensorInfo", "Mains Voltage"),
                       ("SensorInfo", "Battery Level"), ("TextSensorInfo", "Status")])
    for sid in em:
        assert sid.isascii() and sid[0].isalpha()
        assert "_" not in sid and " " not in sid


# ── Value writing ────────────────────────────────────────────────────────────

def _sensor_state(value):
    """A real aioesphomeapi SensorState — the plugin dispatches on isinstance."""
    return aioesphomeapi.SensorState(key=1, state=value, missing_state=False)


def test_nan_is_not_written_to_a_number_state(plugin, fake_device):
    dev = fake_device()
    dev.states["power"] = 12.5
    plugin._apply_v040_state(dev, _sensor_state(float("nan")), "power",
                             {"unit": "W", "kind": "sensor"})
    assert dev.state_writes == []
    assert dev.states["power"] == 12.5      # last good reading survives


def test_infinity_is_not_written_either(plugin, fake_device):
    dev = fake_device()
    plugin._apply_v040_state(dev, _sensor_state(float("inf")), "power",
                             {"unit": "W", "kind": "sensor"})
    assert dev.state_writes == []


def test_a_real_reading_is_rounded_and_given_a_unit(plugin, fake_device):
    dev = fake_device()
    plugin._apply_v040_state(dev, _sensor_state(33.59825134277344), "totalEnergy",
                             {"unit": "kWh", "kind": "sensor"})
    assert dev.states["totalEnergy"] == 33.6
    assert dev.states["totalEnergy.ui"] == "33.60 kWh"


def test_seconds_readings_get_a_human_ui(plugin, fake_device):
    dev = fake_device()
    plugin._apply_v040_state(dev, _sensor_state(827412.0), "uptime",
                             {"unit": "s", "kind": "sensor"})
    assert dev.states["uptime"] == 827412
    assert dev.states["uptime.ui"] == "9d 13h 50m 12s"


def test_nan_never_reaches_the_native_sensor_value(plugin, fake_device):
    dev = fake_device(device_type_id="esphomeSensor")
    plugin._apply_sensor_headline(dev, _sensor_state(float("nan")), {"unit": "W"})
    assert "sensorValue" not in dev.states


def test_headline_reading_writes_sensor_value(plugin, fake_device):
    dev = fake_device(device_type_id="esphomeSensor")
    plugin._apply_sensor_headline(dev, _sensor_state(240.5), {"unit": "V"})
    assert dev.states["sensorValue"] == 240.5
    assert dev.states["sensorValue.ui"] == "240.50 V"


@pytest.mark.parametrize("secs,expected", [
    (0, "0s"), (59, "59s"), (60, "1m 0s"), (3661, "1h 1m 1s"),
    (90061, "1d 1h 1m 1s"),
])
def test_format_seconds(plugin, secs, expected):
    assert plugin._format_seconds(secs) == expected


def test_format_seconds_survives_rubbish(plugin):
    assert plugin._format_seconds("not a number") == "not a number"


# ── Migration guard ──────────────────────────────────────────────────────────

def test_legacy_entity_devices_are_recognised(plugin, fake_device):
    legacy = fake_device(address="AABBCCDDEEFF_12", device_type_id="esphomeSwitch")
    assert plugin._is_legacy_device(legacy) is True


def test_legacy_only_types_are_recognised(plugin, fake_device):
    legacy = fake_device(address="AABBCCDDEEFF", device_type_id="esphomeBinarySensor")
    assert plugin._is_legacy_device(legacy) is True


def test_current_devices_are_never_treated_as_legacy(plugin, fake_device):
    """A lost preferences flush replays the migration — it must be harmless."""
    for type_id in sorted(plugin._OUR_DEVICE_TYPES):
        dev = fake_device(address="AABBCCDDEEFF", device_type_id=type_id)
        assert plugin._is_legacy_device(dev) is False


# ── Dynamic state list ───────────────────────────────────────────────────────

def test_state_list_covers_every_mapped_entity(plugin, fake_device):
    ents = _entities([("SensorInfo", "Power"), ("TextSensorInfo", "Status"),
                      ("BinarySensorInfo", "Motion")])
    em = plugin._build_entity_key_map(ents, None)
    dev = fake_device(props={"entityKeyMap": json.dumps(em)})
    keys = [s["Key"] for s in plugin.getDeviceStateList(dev)]
    assert set(em) <= set(keys)
    assert len(keys) == len(set(keys)), "duplicate state IDs in the state list"


def test_state_list_includes_node_info_states(plugin, fake_device):
    dev = fake_device(props={"entityKeyMap": "{}"})
    keys = [s["Key"] for s in plugin.getDeviceStateList(dev)]
    for sid in ("ipAddress", "macAddress", "boardModel", "esphomeVersion"):
        assert sid in keys


def test_state_list_survives_a_broken_map(plugin, fake_device):
    dev = fake_device(props={"entityKeyMap": "{not json"})
    assert plugin.getDeviceStateList(dev) is not None


# ── MAC normalising ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("8c:ce:4e:57:4f:8d", "8CCE4E574F8D"),
    ("8CCE4E574F8D", "8CCE4E574F8D"),
    ("8c-ce-4e-57-4f-8d", "8CCE4E574F8D"),
    ("", ""),
    (None, ""),
])
def test_normalise_mac(plugin_mod, raw, expected):
    assert plugin_mod.normalise_mac(raw) == expected
