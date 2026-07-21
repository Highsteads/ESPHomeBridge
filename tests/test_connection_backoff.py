#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_connection_backoff.py
# Description: Regression tests for the connect/retry loop. The live defect: a
#              SMLIGHT SMHUB Zigbee coordinator advertises itself as
#              _esphomelib._tcp and opens port 6053, but never answers the
#              ESPHome handshake. The plugin retried forever and warned every
#              ~35 seconds about hardware the user had never adopted.
# Author:      CliveS & Claude Opus 4.8
# Date:        21-07-2026
# Version:     1.0

from __future__ import annotations

import asyncio
import logging

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")

HELLO_TIMEOUT = "Timeout waiting for HelloResponse after 30.0s"
SMHUB_MAC     = "AABBCC001122"   # stand-in — never a real MAC from the author's LAN


class SilentClient:
    """Accepts the TCP connection, never completes the handshake."""

    instances = 0

    def __init__(self, *args, **kwargs):
        SilentClient.instances += 1

    async def connect(self, login=True):
        raise aioesphomeapi.APIConnectionError(HELLO_TIMEOUT)

    async def disconnect(self):
        return None


@pytest.fixture
def silent_node(plugin, plugin_mod, monkeypatch):
    """A discovered node that behaves like the SMHUB, with sleeps removed."""
    SilentClient.instances = 0
    monkeypatch.setattr(aioesphomeapi, "APIClient", SilentClient)

    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(plugin_mod.asyncio, "sleep", _no_sleep)
    plugin.discovered[SMHUB_MAC] = {
        "hostname": "rtos-smhub-001122", "ip": "192.168.1.172", "port": 6053,
        "version": "", "platform": "", "board": "", "first_seen": 0,
    }
    return plugin


def _run(plugin, mac=SMHUB_MAC):
    asyncio.run(plugin._connect_to_device(mac))


def _warnings(plugin):
    return [r for r in plugin.log_records if r.levelno >= logging.WARNING]


# ── The core defect ──────────────────────────────────────────────────────────

def test_unadopted_node_stops_retrying(silent_node, plugin_mod):
    """A discovered node with no Indigo device is dropped after a few goes."""
    _run(silent_node)
    assert SMHUB_MAC in silent_node.parked
    assert SilentClient.instances == plugin_mod.MAX_CONNECT_FAILURES_UNADOPTED


def test_gives_up_quietly(silent_node):
    """One warning for the first failure, one for giving up. Nothing else."""
    _run(silent_node)
    warnings = _warnings(silent_node)
    assert len(warnings) == 2, [r.getMessage() for r in warnings]
    assert "Retrying quietly" in warnings[0].getMessage()
    assert "gave up" in warnings[1].getMessage()


def test_give_up_message_is_actionable(silent_node):
    """The final line names the node, the reason and the way back."""
    _run(silent_node)
    msg = _warnings(silent_node)[-1].getMessage()
    assert SMHUB_MAC in msg
    assert "192.168.1.172" in msg
    assert HELLO_TIMEOUT in msg
    assert "may not be an ESPHome node" in msg
    assert "Adding it as an Indigo device" in msg


def test_adopted_node_gets_more_attempts(silent_node, plugin_mod, fake_device, indigo_stub):
    """A node the user HAS added is given a longer run before parking."""
    dev = fake_device(dev_id=7, address=SMHUB_MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[7] = dev
    _run(silent_node)
    assert SilentClient.instances == plugin_mod.MAX_CONNECT_FAILURES_ADOPTED
    assert SMHUB_MAC in silent_node.parked


def test_parked_node_ignores_further_mdns_announcements(silent_node):
    """mDNS re-advertises constantly — that must not restart the storm."""
    _run(silent_node)
    assert silent_node._should_connect(SMHUB_MAC) is False


def test_park_clears_the_connection_entry(silent_node):
    """A parked node leaves no half-live client behind for actions to use."""
    _run(silent_node)
    assert SMHUB_MAC not in silent_node.connections


# ── Recovery ─────────────────────────────────────────────────────────────────

def test_adding_a_device_unparks_immediately(silent_node, fake_device, indigo_stub, monkeypatch):
    _run(silent_node)
    spawned = []
    monkeypatch.setattr(silent_node, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    dev = fake_device(dev_id=9, address=SMHUB_MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[9] = dev
    assert silent_node._unpark(SMHUB_MAC, "device added") is True
    assert spawned and SMHUB_MAC in spawned[0]
    assert SMHUB_MAC not in silent_node.parked


def test_sweep_retries_once_the_backoff_has_elapsed(silent_node, plugin_mod, monkeypatch):
    _run(silent_node)
    spawned = []
    monkeypatch.setattr(silent_node, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    # Not yet due — nothing happens.
    silent_node._sweep_parked()
    assert not spawned
    # Wind the clock past the parked window.
    silent_node.parked[SMHUB_MAC]["since"] -= plugin_mod.PARKED_RETRY_AFTER + 1
    silent_node._sweep_parked()
    assert spawned
    assert SMHUB_MAC not in silent_node.parked


def test_sweep_retries_when_a_device_appears(silent_node, fake_device, indigo_stub, monkeypatch):
    _run(silent_node)
    spawned = []
    monkeypatch.setattr(silent_node, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    indigo_stub.devices[3] = fake_device(dev_id=3, address=SMHUB_MAC,
                                         device_type_id="esphomeSwitch")
    silent_node._sweep_parked()
    assert spawned


def test_unpark_does_nothing_for_an_unknown_node(silent_node):
    assert silent_node._unpark("000000000000", "spurious") is False


# ── A healthy node is unaffected ─────────────────────────────────────────────

def test_a_good_connection_resets_the_failure_count(plugin, plugin_mod, monkeypatch):
    """One bad attempt then a good one must not creep towards the park limit."""
    calls = {"n": 0}

    class FlakyClient:
        def __init__(self, *a, **kw):
            pass

        async def connect(self, login=True):
            calls["n"] += 1
            if calls["n"] == 1:
                raise aioesphomeapi.APIConnectionError(HELLO_TIMEOUT)
            if calls["n"] > 2:
                raise asyncio.CancelledError
            return None

        async def device_info(self):
            return type("Info", (), {"name": "good", "esphome_version": "2026.4.5",
                                     "model": "esp32dev"})()

        async def list_entities_services(self):
            return [], []

        def subscribe_states(self, cb):
            return lambda: None

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", FlakyClient)

    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(plugin_mod.asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(plugin, "_client_is_connected", staticmethod(lambda c: False))
    plugin.auto_create_nodes = False
    plugin.discovered["AABBCCDDEEFF"] = {
        "hostname": "good-node", "ip": "192.168.1.50", "port": 6053,
        "version": "", "platform": "", "board": "", "first_seen": 0,
    }
    # The third attempt cancels, which ends the loop cleanly.
    asyncio.run(plugin._connect_to_device("AABBCCDDEEFF"))
    # The good session cleared the earlier failure, so it never parked.
    assert "AABBCCDDEEFF" not in plugin.parked
