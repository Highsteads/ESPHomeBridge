#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_address_and_recovery.py
# Description: Regression tests for the 0.10.1 fixes from the 05-Oct-2026 audit.
#
#              ES-2  A node that moves address (DHCP) was never re-learnt: the
#                    mDNS callback dropped ServiceStateChange.Updated, and the
#                    connect loop read the discovery entry once and kept dialling
#                    the old address for ever.
#              ES-1  Plaintext recovery cleared the device key, then the next pass
#                    picked up the migration key or the plugin default key and
#                    looped straight back, with no back-off and no count.
#              ES-4  Climate capabilities, supportedModes, the HVAC action and the
#                    preset were read from str() of an IntEnum, which is the digit.
#              ES-5  A disabled device was still found and written to.
#              Sweep A dead adopted node was unparked on every 60 s sweep because
#                    "an Indigo device now exists" - it had existed all along.
# Author:      CliveS & Claude Opus 5.5
# Date:        05-10-2026
# Version:     1.0

from __future__ import annotations

import asyncio
import logging

import pytest

aioesphomeapi = pytest.importorskip("aioesphomeapi")
zeroconf_asyncio = pytest.importorskip("zeroconf.asyncio")

MAC     = "AABBCC0000A1"          # stand-in - never a real MAC from the author's LAN
MAC_FMT = "AA:BB:CC:00:00:A1"
OLD_IP  = "192.168.1.60"
NEW_IP  = "192.168.1.61"
HELLO   = "Timeout waiting for HelloResponse after 30.0s"
PLAIN   = ("esp: The device is using plaintext protocol; Try enabling encryption "
           "on the device or turning off encryption on the client (Indigo)")


def _entry(ip=OLD_IP, port=6053, first_seen=0):
    return {"hostname": "esp-test-a1", "ip": ip, "port": port, "version": "2026.9.0",
            "platform": "ESP32", "board": "esp32dev", "first_seen": first_seen}


@pytest.fixture
def no_sleep(plugin_mod, monkeypatch):
    async def _no_sleep(_secs):
        return None
    monkeypatch.setattr(plugin_mod.asyncio, "sleep", _no_sleep)


def _infos(plugin, level=logging.INFO):
    return [r.getMessage() for r in plugin.log_records if r.levelno == level]


def _warnings(plugin):
    return [r.getMessage() for r in plugin.log_records if r.levelno >= logging.WARNING]


# ── ES-2: mDNS Updated is handled ────────────────────────────────────────────

def test_mdns_updated_is_not_dropped(plugin, plugin_mod, monkeypatch):
    from zeroconf import ServiceStateChange
    scheduled = []

    def _fake_rcts(coro, loop):
        scheduled.append(coro)
        coro.close()

    monkeypatch.setattr(plugin_mod.asyncio, "run_coroutine_threadsafe", _fake_rcts)
    plugin._on_mdns_service_state_change(None, "_esphomelib._tcp.local.",
                                         "esp-test-a1._esphomelib._tcp.local.",
                                         ServiceStateChange.Updated)
    assert len(scheduled) == 1


def test_mdns_removed_is_still_ignored(plugin, plugin_mod, monkeypatch):
    from zeroconf import ServiceStateChange
    scheduled = []
    monkeypatch.setattr(plugin_mod.asyncio, "run_coroutine_threadsafe",
                        lambda coro, loop: (scheduled.append(coro), coro.close()))
    plugin._on_mdns_service_state_change(None, "_esphomelib._tcp.local.",
                                         "esp-test-a1._esphomelib._tcp.local.",
                                         ServiceStateChange.Removed)
    assert scheduled == []


def _fake_service_info(ip, port=6053):
    class FakeInfo:
        def __init__(self, service_type, name):
            self.port = port
            self.properties = {b"mac": MAC_FMT.encode(), b"version": b"2026.9.0",
                               b"platform": b"ESP32", b"board": b"esp32dev"}

        async def async_request(self, zc, timeout=3000):
            await asyncio.sleep(0)     # a real yield, so two handlers interleave
            return True

        def parsed_addresses(self):
            return [ip]
    return FakeInfo


def test_mdns_update_refreshes_the_address(plugin, monkeypatch):
    plugin.discovered[MAC] = _entry(first_seen=123)
    plugin.connections[MAC] = {"client": None, "info": None, "entities": {}, "states": {}}
    monkeypatch.setattr(zeroconf_asyncio, "AsyncServiceInfo", _fake_service_info(NEW_IP))
    asyncio.run(plugin._handle_mdns_added_inner(None, "_esphomelib._tcp.local.",
                                                "esp-test-a1._esphomelib._tcp.local."))
    assert plugin.discovered[MAC]["ip"] == NEW_IP
    assert plugin.discovered[MAC]["first_seen"] == 123      # not reset by a refresh
    moved = [m for m in _infos(plugin) if NEW_IP in m and OLD_IP in m]
    assert len(moved) == 1, _infos(plugin)


def test_mdns_update_with_the_same_address_says_nothing(plugin, monkeypatch):
    plugin.discovered[MAC] = _entry()
    plugin.connections[MAC] = {"client": None, "info": None, "entities": {}, "states": {}}
    monkeypatch.setattr(zeroconf_asyncio, "AsyncServiceInfo", _fake_service_info(OLD_IP))
    asyncio.run(plugin._handle_mdns_added_inner(None, "_esphomelib._tcp.local.",
                                                "esp-test-a1._esphomelib._tcp.local."))
    assert _infos(plugin) == []


def test_a_moved_parked_node_is_retried_at_once(plugin, monkeypatch):
    plugin.discovered[MAC] = _entry()
    plugin.parked[MAC] = {"reason": HELLO, "since": 0, "failures": 3, "device_id": None}
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    monkeypatch.setattr(zeroconf_asyncio, "AsyncServiceInfo", _fake_service_info(NEW_IP))
    asyncio.run(plugin._handle_mdns_added_inner(None, "_esphomelib._tcp.local.",
                                                "esp-test-a1._esphomelib._tcp.local."))
    assert MAC not in plugin.parked
    assert len(spawned) == 1


def test_added_then_updated_starts_one_connection(plugin, monkeypatch):
    """Added and Updated can arrive together; they must not race to two tasks."""
    starts = []

    async def _fake_connect(mac, quiet=False):
        # The real coroutine claims the slot when it first runs - too late to
        # stop a second spawn queued in the same tick.
        plugin.connections[mac] = {"client": None, "info": None, "entities": {}, "states": {}}
        starts.append(mac)

    monkeypatch.setattr(plugin, "_connect_to_device", _fake_connect)
    monkeypatch.setattr(zeroconf_asyncio, "AsyncServiceInfo", _fake_service_info(OLD_IP))
    name = "esp-test-a1._esphomelib._tcp.local."

    async def _both():
        await asyncio.gather(
            plugin._handle_mdns_added_inner(None, "_esphomelib._tcp.local.", name),
            plugin._handle_mdns_added_inner(None, "_esphomelib._tcp.local.", name),
        )
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(_both())
    assert starts == [MAC]


# ── ES-2: the connect loop re-reads the address on every attempt ─────────────

def test_retry_dials_the_new_address(plugin, monkeypatch, no_sleep):
    dialled = []

    class MovingClient:
        def __init__(self, host, port, **kw):
            dialled.append((host, port))
            if len(dialled) == 1:
                plugin.discovered[MAC] = _entry(ip=NEW_IP, port=6054)

        async def connect(self, login=True):
            raise aioesphomeapi.APIConnectionError(HELLO)

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", MovingClient)
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert dialled[0] == (OLD_IP, 6053)
    assert all(d == (NEW_IP, 6054) for d in dialled[1:]), dialled
    # The give-up line names where it was really trying.
    assert NEW_IP in _warnings(plugin)[-1]


def test_connecting_line_is_said_once_not_every_attempt(plugin, plugin_mod, monkeypatch, no_sleep):
    class Silent:
        def __init__(self, *a, **kw):
            pass

        async def connect(self, login=True):
            raise aioesphomeapi.APIConnectionError(HELLO)

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", Silent)
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert len([m for m in _infos(plugin) if m.startswith("Connecting to")]) == 1


# ── ES-1: plaintext recovery never loops ─────────────────────────────────────

def _plaintext_client(seen, connect_ok_without_key=False):
    class PlainClient:
        def __init__(self, *a, noise_psk=None, **kw):
            seen.append(noise_psk)
            self.psk = noise_psk

        async def connect(self, login=True):
            if len(seen) > 30:
                raise asyncio.CancelledError    # bound the old infinite loop
            if self.psk is not None or not connect_ok_without_key:
                raise aioesphomeapi.APIConnectionError(PLAIN if self.psk else HELLO)
            return None

        async def disconnect(self):
            return None
    return PlainClient


@pytest.mark.parametrize("source", ["default", "migration"])
def test_plaintext_suppresses_every_key_source(plugin, monkeypatch, no_sleep, source):
    seen = []
    monkeypatch.setattr(aioesphomeapi, "APIClient", _plaintext_client(seen))
    if source == "default":
        plugin.default_encryption_key = "ZGVmYXVsdGtleWRlZmF1bHRrZXlkZWZhdWx0a2V5MDA="
    else:
        monkeypatch.setattr(plugin, "_migration_saved_keys",
                            lambda: {MAC: "bWlncmF0aW9ua2V5bWlncmF0aW9ua2V5bWlncmF0aW8="})
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert seen[0] is not None
    assert all(psk is None for psk in seen[1:]), seen
    assert len(seen) < 30
    assert MAC in plugin.parked                 # counted, backed off, parked
    assert MAC in plugin.plaintext_nodes


def test_a_key_that_came_from_the_default_is_not_a_warning(plugin, monkeypatch, no_sleep):
    seen = []
    monkeypatch.setattr(aioesphomeapi, "APIClient", _plaintext_client(seen))
    plugin.default_encryption_key = "ZGVmYXVsdGtleWRlZmF1bHRrZXlkZWZhdWx0a2V5MDA="
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert not any("plaintext" in m for m in _warnings(plugin))


def test_device_key_on_a_plaintext_node_is_cleared(plugin, monkeypatch, no_sleep,
                                                   fake_device, indigo_stub):
    seen = []
    monkeypatch.setattr(aioesphomeapi, "APIClient", _plaintext_client(seen))
    dev = fake_device(dev_id=11, address=MAC, device_type_id="esphomeSensor",
                      props={"encryptionKey": "ZGV2aWNla2V5ZGV2aWNla2V5ZGV2aWNla2V5ZGV2aWM="})
    indigo_stub.devices[11] = dev
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert dev.pluginProps["encryptionKey"] == ""
    assert all(psk is None for psk in seen[1:]), seen
    assert any("plaintext" in m for m in _warnings(plugin))


def test_repeated_plaintext_error_is_counted(plugin, plugin_mod, monkeypatch, no_sleep):
    """Told 'plaintext' while already connecting without a key: a failure, not a loop."""
    seen = []

    class AlwaysPlain:
        def __init__(self, *a, noise_psk=None, **kw):
            seen.append(noise_psk)

        async def connect(self, login=True):
            if len(seen) > 30:
                raise asyncio.CancelledError
            raise aioesphomeapi.APIConnectionError(PLAIN)

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", AlwaysPlain)
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert len(seen) == plugin_mod.MAX_CONNECT_FAILURES_UNADOPTED
    assert MAC in plugin.parked


def test_requires_encryption_clears_the_plaintext_decision(plugin, plugin_mod, monkeypatch,
                                                           no_sleep):
    """The node was re-flashed with encryption: the next key must be used."""
    class NeedsKey:
        def __init__(self, *a, **kw):
            pass

        async def connect(self, login=True):
            raise aioesphomeapi.APIConnectionError("esp: Connection requires encryption")

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", NeedsKey)
    plugin.plaintext_nodes.add(MAC)
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert MAC not in plugin.plaintext_nodes
    assert plugin.parked[MAC]["reason"] == plugin_mod.PARK_NEEDS_KEY


def test_a_key_typed_on_the_device_lifts_the_plaintext_decision(plugin, fake_device,
                                                                indigo_stub):
    plugin.plaintext_nodes.add(MAC)
    dev = fake_device(dev_id=12, address=MAC, device_type_id="esphomeSensor",
                      props={"encryptionKey": "bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5bmV3a2V5MDA="})
    indigo_stub.devices[12] = dev
    plugin.deviceStartComm(dev)
    assert MAC not in plugin.plaintext_nodes


# ── ES-4: climate enums read by member, not by str() ─────────────────────────

def test_climate_capabilities_from_enum_members(plugin):
    from aioesphomeapi import ClimateInfo, ClimateMode
    info = ClimateInfo(key=1, supported_modes=[ClimateMode.OFF, ClimateMode.HEAT],
                       supports_current_temperature=True)
    props = plugin._props_for_primary("esphomeClimate", info)
    assert props["SupportsHeatSetpoint"] is True
    assert props["SupportsCoolSetpoint"] is False
    assert props["supportedModes"] == "OFF, HEAT"


def test_climate_capabilities_from_raw_ints(plugin):
    """An older library may hand back bare ints; read them as the enum."""
    from aioesphomeapi import ClimateInfo
    info = ClimateInfo(key=1)
    object.__setattr__(info, "supported_modes", [0, 2])   # frozen dataclass
    props = plugin._props_for_primary("esphomeClimate", info)
    assert props["SupportsCoolSetpoint"] is True
    assert props["SupportsHeatSetpoint"] is False
    assert props["supportedModes"] == "OFF, COOL"


def test_climate_action_and_preset_by_name(plugin, fake_device):
    from aioesphomeapi import ClimateAction, ClimateMode, ClimatePreset, ClimateState
    dev = fake_device(dev_id=13, address=MAC, device_type_id="esphomeClimate")
    state = ClimateState(key=1, mode=ClimateMode.HEAT, action=ClimateAction.HEATING,
                         preset=ClimatePreset.AWAY, target_temperature=20.0)
    plugin._apply_state_to_device(dev, state)
    assert dev.states["hvacOperationMode"] == 1
    assert dev.states["action"] == "heating"
    assert dev.states["preset"] == "away"


def test_climate_custom_preset_wins(plugin, fake_device):
    from aioesphomeapi import ClimateState
    dev = fake_device(dev_id=14, address=MAC, device_type_id="esphomeClimate")
    plugin._apply_state_to_device(dev, ClimateState(key=1, custom_preset="Boost"))
    assert dev.states["preset"] == "Boost"


# ── ES-5: disabled devices are left alone ────────────────────────────────────

def _disabled_node(fake_device, indigo_stub, dev_id=21):
    dev = fake_device(dev_id=dev_id, address=MAC, device_type_id="esphomeSensor",
                      props={"entityKeyMap": '{"temp": {"key": 5, "kind": "sensor"}}'})
    dev.enabled = False
    indigo_stub.devices[dev_id] = dev
    return dev


def test_find_skips_a_disabled_device(plugin, fake_device, indigo_stub):
    _disabled_node(fake_device, indigo_stub)
    assert plugin._find_node_device(MAC) is None


def test_find_skips_a_cached_device_once_disabled(plugin, fake_device, indigo_stub):
    dev = fake_device(dev_id=22, address=MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[22] = dev
    plugin.deviceStartComm(dev)
    assert plugin._find_node_device(MAC) is dev
    dev.enabled = False
    assert plugin._find_node_device(MAC) is None


def test_disabled_device_gets_no_state_writes(plugin, fake_device, indigo_stub):
    from aioesphomeapi import SensorState
    dev = _disabled_node(fake_device, indigo_stub)
    plugin._on_entity_state(MAC, SensorState(key=5, state=21.5))
    assert dev.state_writes == []


def test_auto_create_does_not_duplicate_a_disabled_device(plugin, fake_device, indigo_stub):
    indigo_stub.device.reset_mock()
    _disabled_node(fake_device, indigo_stub)
    plugin.discovered[MAC] = _entry()
    info = type("Info", (), {"name": "esp", "esphome_version": "2026.9.0",
                             "model": "esp32dev"})()
    plugin._ensure_node_device(MAC, info, [])
    assert indigo_stub.device.create.call_count == 0
    assert indigo_stub.device.delete.call_count == 0


def test_disabled_node_is_not_connected(plugin, fake_device, indigo_stub):
    _disabled_node(fake_device, indigo_stub)
    plugin.discovered[MAC] = _entry()
    assert plugin._should_connect(MAC) is False


def test_disabling_drops_a_running_connection(plugin, fake_device, indigo_stub, monkeypatch):
    dev = fake_device(dev_id=23, address=MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[23] = dev
    plugin.discovered[MAC] = _entry()
    disconnected = []

    class Live:
        async def disconnect(self):
            disconnected.append(True)

    plugin.connections[MAC] = {"client": Live(), "info": object(), "entities": {}, "states": {}}
    dev.enabled = False

    async def _go():
        plugin._stop_if_disabled(MAC)
        for _ in range(3):
            await asyncio.sleep(0)

    asyncio.run(_go())
    assert disconnected == [True]


def test_reenabling_connects_again(plugin, fake_device, indigo_stub, monkeypatch):
    dev = fake_device(dev_id=24, address=MAC, device_type_id="esphomeSensor")
    indigo_stub.devices[24] = dev
    plugin.discovered[MAC] = _entry()
    calls = []
    monkeypatch.setattr(plugin, "request_connect", lambda mac, why: calls.append(mac))
    plugin.deviceStartComm(dev)
    assert calls == [MAC]


def test_connect_if_idle_spawns_once(plugin, fake_device, indigo_stub, monkeypatch):
    plugin.discovered[MAC] = _entry()
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    plugin._connect_if_idle(MAC, "device enabled")
    plugin._connect_if_idle(MAC, "device enabled")
    assert len(spawned) == 1


# ── Sweep: only a NEW device unparks ─────────────────────────────────────────

def test_sweep_leaves_a_dead_adopted_node_parked(plugin, fake_device, indigo_stub,
                                                 monkeypatch, no_sleep):
    class Silent:
        def __init__(self, *a, **kw):
            pass

        async def connect(self, login=True):
            raise aioesphomeapi.APIConnectionError(HELLO)

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", Silent)
    indigo_stub.devices[31] = fake_device(dev_id=31, address=MAC,
                                          device_type_id="esphomeSensor")
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC))
    assert MAC in plugin.parked
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    plugin._sweep_parked()
    plugin._sweep_parked()
    assert spawned == []
    assert MAC in plugin.parked


def test_sweep_unparks_when_a_different_device_appears(plugin, fake_device, indigo_stub,
                                                       monkeypatch):
    plugin.discovered[MAC] = _entry()
    plugin.parked[MAC] = {"reason": HELLO, "since": 0, "failures": 10, "device_id": 31}
    indigo_stub.devices[32] = fake_device(dev_id=32, address=MAC,
                                          device_type_id="esphomeSensor")
    spawned = []
    monkeypatch.setattr(plugin, "_spawn_task",
                        lambda coro, label: (coro.close(), spawned.append(label))[1])
    import time as _t
    plugin.parked[MAC]["since"] = _t.time()       # back-off not yet due
    plugin._sweep_parked()
    assert len(spawned) == 1


def test_a_retry_after_the_backoff_is_quiet(plugin, plugin_mod, fake_device, indigo_stub,
                                            monkeypatch, no_sleep):
    """The first round warned twice; the hourly retry of a still-dead node says nothing."""
    class Silent:
        def __init__(self, *a, **kw):
            pass

        async def connect(self, login=True):
            raise aioesphomeapi.APIConnectionError(HELLO)

        async def disconnect(self):
            return None

    monkeypatch.setattr(aioesphomeapi, "APIClient", Silent)
    plugin.discovered[MAC] = _entry()
    asyncio.run(plugin._connect_to_device(MAC, quiet=True))
    assert _warnings(plugin) == []
    assert MAC in plugin.parked
