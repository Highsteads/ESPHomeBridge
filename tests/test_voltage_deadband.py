#! /usr/bin/env python3
# -*- coding: utf-8 -*-
# Filename:    test_voltage_deadband.py
# Description: v0.9.0. Mains voltage wobbles by hundredths of a volt, so a power
#              plug's voltage state changed on almost every report (~40,000 SQL
#              Logger rows a day from two freezer plugs). A secondary sensor in
#              volts is now written only when it moves by at least the deadband,
#              measured from the value last WRITTEN so slow drift still lands.
# Author:      CliveS & Claude Opus 5.5
# Date:        23-09-2026
# Version:     1.0

import aioesphomeapi
import pytest

VOLT = {"unit": "V", "kind": "sensor"}


def _reading(value):
    return aioesphomeapi.SensorState(key=1, state=value, missing_state=False)


def _feed(plugin, dev, values, info=VOLT, state_id="voltage"):
    for v in values:
        plugin._apply_v040_state(dev, _reading(v), state_id, info)
    return [w[1] for w in dev.state_writes if w[0] == state_id]


def test_the_first_reading_is_always_written(plugin, fake_device):
    assert _feed(plugin, fake_device(), [250.43]) == [250.43]


def test_a_wobble_is_not_written(plugin, fake_device):
    assert _feed(plugin, fake_device(), [250.43, 250.41, 250.62, 250.05]) == [250.43]


def test_a_real_move_is_written(plugin, fake_device):
    assert _feed(plugin, fake_device(), [250.43, 249.9, 251.0]) == [250.43, 249.9, 251.0]


def test_slow_drift_lands_once_it_adds_up(plugin, fake_device):
    # Each step is 0.2 V; measured from the last WRITTEN value, the third step
    # has moved 0.6 V and must be recorded.
    assert _feed(plugin, fake_device(), [250.0, 250.2, 250.4, 250.6]) == [250.0, 250.6]


def test_zero_records_every_reading(plugin, fake_device):
    plugin.voltage_deadband = 0.0
    assert _feed(plugin, fake_device(), [250.43, 250.41, 250.42]) == [250.43, 250.41, 250.42]


def test_other_units_are_untouched(plugin, fake_device):
    got = _feed(plugin, fake_device(), [12.0, 12.1, 12.2],
                info={"unit": "W", "kind": "sensor"}, state_id="power")
    assert got == [12.0, 12.1, 12.2]


@pytest.mark.parametrize("raw,expected", [
    ("0.5", 0.5), ("0", 0.0), (" 1.25 ", 1.25), (2, 2.0),
    ("", 0.5), ("abc", 0.5), ("-1", 0.5), ("nan", 0.5), (None, 0.5),
])
def test_the_setting_is_parsed_safely(plugin_mod, raw, expected):
    assert plugin_mod.parse_deadband(raw) == expected


def test_the_dialog_refuses_a_bad_value(plugin):
    assert plugin.validatePrefsConfigUi({"voltageDeadband": "0.5"})[0] is True
    assert plugin.validatePrefsConfigUi({"voltageDeadband": "0"})[0] is True
    bad = plugin.validatePrefsConfigUi({"voltageDeadband": "-2"})
    assert bad[0] is False and "voltageDeadband" in bad[2]
    assert plugin.validatePrefsConfigUi({"voltageDeadband": "lots"})[0] is False


def test_saving_the_dialog_applies_the_setting(plugin):
    plugin.closedPrefsConfigUi({"voltageDeadband": "2", "autoCreateDevices": True,
                                "logLevel": "INFO"}, False)
    assert plugin.voltage_deadband == 2.0
