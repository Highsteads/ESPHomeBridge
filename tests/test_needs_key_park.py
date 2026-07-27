#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_needs_key_park.py
# Description: Regression tests for the v0.8.1 encryption-key recovery fix.
#
#              The defect: a node that failed with InvalidAuthAPIError (or
#              "connection requires encryption") called _release_connection and
#              returned. _release_connection only nulls conn["client"] — it
#              leaves the MAC in self.connections and never parks it. That zombie
#              entry blocked BOTH recovery routes: _should_connect refuses while
#              `mac in self.connections`, and deviceStartComm's retry only fires
#              for a mac in self.parked. So correcting the key in the Configure
#              dialog did nothing and the node stayed dead until a plugin restart
#              — defeating the whole point of the deviceStartComm retry hook.
#
#              The fix parks with reason PARK_NEEDS_KEY. _sweep_parked must then
#              leave those alone (its device-exists branch fires every sweep, so
#              parking without that guard would turn one bad key into a
#              reconnect/fail/park storm once a minute).
# Author:      CliveS & Claude Opus 5
# Date:        27-07-2026
# Version:     1.0

from __future__ import annotations

import asyncio

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")

BAD_KEY_MAC = "AABBCC334455"   # stand-in — never a real MAC from the author's LAN


class BadKeyClient:
    """Refuses the handshake the way a wrong noise_psk does."""

    def __init__(self, *args, **kwargs):
        pass

    async def connect(self, login=True):
        raise aioesphomeapi.InvalidAuthAPIError("Invalid encryption key")

    async def disconnect(self):
        return None


class NeedsKeyClient:
    """An encrypted node we hold no key for at all."""

    def __init__(self, *args, **kwargs):
        pass

    async def connect(self, login=True):
        raise aioesphomeapi.APIConnectionError(
            "Connection requires encryption but no key was supplied")

    async def disconnect(self):
        return None


@pytest.fixture
def node(plugin, plugin_mod, monkeypatch):
    async def _no_sleep(_secs):
        return None

    monkeypatch.setattr(plugin_mod.asyncio, "sleep", _no_sleep)
    plugin.discovered[BAD_KEY_MAC] = {
        "hostname": "esp-kitchen-334455", "ip": "192.168.1.55", "port": 6053,
        "version": "", "platform": "", "board": "", "first_seen": 0,
    }
    return plugin


def _run(plugin, mac=BAD_KEY_MAC):
    asyncio.run(plugin._connect_to_device(mac))


# ── the defect ───────────────────────────────────────────────────────────────

def test_bad_key_parks_rather_than_leaving_a_zombie(node, plugin_mod, monkeypatch):
    monkeypatch.setattr(aioesphomeapi, "APIClient", BadKeyClient)
    _run(node)
    assert BAD_KEY_MAC not in node.connections, \
        "a stale connections entry blocks every retry path"
    assert BAD_KEY_MAC in node.parked
    assert node.parked[BAD_KEY_MAC]["reason"] == plugin_mod.PARK_NEEDS_KEY


def test_missing_key_parks_the_same_way(node, plugin_mod, monkeypatch, fake_device, indigo_stub):
    monkeypatch.setattr(aioesphomeapi, "APIClient", NeedsKeyClient)
    dev = fake_device(dev_id=9, address=BAD_KEY_MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[9] = dev
    _run(node)
    assert BAD_KEY_MAC not in node.connections
    assert node.parked[BAD_KEY_MAC]["reason"] == plugin_mod.PARK_NEEDS_KEY


def test_editing_the_key_can_now_reach_the_node(node, monkeypatch, fake_device, indigo_stub):
    """deviceStartComm's retry hook fires on `mac in self.parked` — which the
    zombie entry used to prevent. This is the precondition it depends on."""
    monkeypatch.setattr(aioesphomeapi, "APIClient", BadKeyClient)
    _run(node)
    dev = fake_device(dev_id=11, address=BAD_KEY_MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[11] = dev
    assert BAD_KEY_MAC in node.parked, "deviceStartComm would not retry otherwise"
    retried = []
    monkeypatch.setattr(node, "request_retry", lambda mac, why: retried.append(mac))
    node.deviceStartComm(dev)
    assert retried == [BAD_KEY_MAC]


# ── and the storm the naive fix would cause ─────────────────────────────────

def test_sweep_does_not_auto_retry_a_needs_key_node(node, plugin_mod, fake_device, indigo_stub):
    """_sweep_parked's device-exists branch fires EVERY sweep. Left ungated it
    would reconnect, fail and re-park a bad-key node once a minute, for ever."""
    dev = fake_device(dev_id=12, address=BAD_KEY_MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[12] = dev
    node.parked[BAD_KEY_MAC] = {"reason": plugin_mod.PARK_NEEDS_KEY,
                                "since": 0, "failures": 1}
    unparked = []
    node._unpark = lambda mac, why: unparked.append(mac)
    node._sweep_parked()
    assert unparked == []
    assert BAD_KEY_MAC in node.parked, "must stay parked until the user acts"


def test_sweep_still_retries_an_ordinary_park(node, fake_device, indigo_stub):
    """The normal recovery path must be untouched by the guard."""
    dev = fake_device(dev_id=13, address=BAD_KEY_MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[13] = dev
    node.parked[BAD_KEY_MAC] = {"reason": "unreachable", "since": 0, "failures": 3}
    unparked = []
    node._unpark = lambda mac, why: unparked.append(mac)
    node._sweep_parked()
    assert unparked == [BAD_KEY_MAC]


# ── waking them deliberately ────────────────────────────────────────────────

def test_retry_nodes_awaiting_key_wakes_only_those(node, plugin_mod):
    node.parked["AABBCC000001"] = {"reason": plugin_mod.PARK_NEEDS_KEY, "since": 0, "failures": 1}
    node.parked["AABBCC000002"] = {"reason": "unreachable", "since": 0, "failures": 5}
    asked = []
    node.request_retry = lambda mac, why: asked.append(mac)
    node.retry_nodes_awaiting_key("test")
    assert asked == ["AABBCC000001"]


def test_new_default_key_triggers_the_retry(node, plugin_mod, monkeypatch):
    node.default_encryption_key = ""
    node.parked[BAD_KEY_MAC] = {"reason": plugin_mod.PARK_NEEDS_KEY, "since": 0, "failures": 1}
    called = []
    monkeypatch.setattr(node, "retry_nodes_awaiting_key", lambda why: called.append(why))
    node.closedPrefsConfigUi(
        {"defaultEncryptionKey": "a-new-key", "autoCreateDevices": True,
         "logLevel": "INFO", "ignoredDevices": ""}, False)
    assert len(called) == 1


def test_unchanged_default_key_does_not_trigger_it(node, plugin_mod, monkeypatch):
    node.default_encryption_key = "same-key"
    called = []
    monkeypatch.setattr(node, "retry_nodes_awaiting_key", lambda why: called.append(why))
    node.closedPrefsConfigUi(
        {"defaultEncryptionKey": "same-key", "autoCreateDevices": True,
         "logLevel": "INFO", "ignoredDevices": ""}, False)
    assert called == []
