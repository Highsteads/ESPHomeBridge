#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: Indigo bridge for ESPHome devices via the Native API (port 6053).
#              Auto-discovers via mDNS, connects per device via aioesphomeapi,
#              maps each ESPHome entity to a native Indigo device.
# Author:      CliveS & Claude Opus 4.7
# Date:        20-05-2026
# Version:     0.3.0

try:
    import indigo
except ImportError:
    pass

import asyncio
import os
import sys
import threading
import time
from datetime import datetime

sys.path.insert(0, os.getcwd())
try:
    from plugin_utils import log_startup_banner
except ImportError:
    log_startup_banner = None

# aioesphomeapi + zeroconf are installed via requirements.txt into
# Contents/Packages/ on plugin startup. They're imported lazily in the
# async-thread setup so import errors get logged through self.logger
# rather than crashing the whole plugin on load.


# ============================================================
# Constants
# ============================================================

PLUGIN_ID      = "com.clives.indigoplugin.esphomebridge"
PLUGIN_VERSION = "0.3.0"

DEVICE_FOLDER_NAME = "ESPHome"

# How often the mDNS browser re-broadcasts (it's continuous between)
MDNS_SERVICE_TYPE = "_esphomelib._tcp.local."

# ESPHome's native API default port
DEFAULT_API_PORT = 6053

# Connection backoff
RECONNECT_BACKOFF_INITIAL = 5    # seconds
RECONNECT_BACKOFF_MAX     = 300


# ============================================================
# Helpers
# ============================================================

def log(message, level="INFO"):
    indigo.server.log(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", level=level)


def normalise_mac(raw):
    """Convert any MAC representation to 12-char uppercase hex with no separators."""
    if not raw:
        return ""
    return "".join(c for c in raw.upper() if c in "0123456789ABCDEF")[:12]


def is_valid_state_id(key):
    """Indigo state IDs: ASCII alphanumeric only, must start with a letter."""
    if not key or not key[0].isascii() or not key[0].isalpha():
        return False
    return all(c.isascii() and c.isalnum() for c in key)


def snake_to_camel(snake):
    """tasmota_field_name or Tasmota-name -> tasmotaFieldName for Indigo state IDs."""
    parts = (snake or "").replace("-", "_").split("_")
    if not parts:
        return ""
    return parts[0].lower() + "".join(p.capitalize() for p in parts[1:] if p)


# ============================================================
# Plugin
# ============================================================

class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        self.debug = pluginPrefs.get("logLevel", "INFO") == "DEBUG"

        # Discovery cache: mac -> {hostname, ip, port, name, first_seen}
        self.discovered = {}

        # Per-device connection state: mac -> {client, task, info, entities:{key:info}}
        self.connections = {}

        # Indigo device cache: mac -> indigo.Device for the esphomeNode
        # Entity devices keyed by f"{mac}_{entity_key}"
        self.nodes_by_mac = {}
        self.entity_devices = {}    # {f"{mac}_{key}": indigo.Device}

        # Event triggers
        self.event_triggers = {}

        # asyncio loop + thread (set in startup)
        self.async_loop = None
        self.async_thread = None
        self.async_started = threading.Event()

        # Config
        self.auto_create_nodes    = bool(pluginPrefs.get("autoCreateDevices", True))
        self.auto_create_entities = bool(pluginPrefs.get("autoCreateEntities", True))
        self.default_encryption_key = pluginPrefs.get("defaultEncryptionKey", "") or ""

        if log_startup_banner:
            log_startup_banner(pluginId, pluginDisplayName, pluginVersion, extras=[
                ("Discovery:",         "mDNS / _esphomelib._tcp"),
                ("API port:",          str(DEFAULT_API_PORT)),
                ("Auto-create nodes:", "yes" if self.auto_create_nodes else "no"),
                ("Auto-create entities:", "yes" if self.auto_create_entities else "no"),
            ])
        else:
            indigo.server.log(f"{pluginDisplayName} v{pluginVersion} starting")

    # --------------------------------------------------------
    # Lifecycle
    # --------------------------------------------------------

    def startup(self):
        # Verify dependencies imported cleanly
        try:
            import aioesphomeapi  # noqa: F401
            import zeroconf       # noqa: F401
        except ImportError as exc:
            self.logger.error(
                f"Required dependency not installed: {exc}. "
                "Plugin will not function. Check requirements.txt processing."
            )
            return

        # Start the asyncio loop in a dedicated thread
        self.async_loop = asyncio.new_event_loop()
        self.async_thread = threading.Thread(
            target=self._run_async_loop,
            name="ESPHomeAsync",
            daemon=True,
        )
        self.async_thread.start()

        # Wait briefly for the loop to be running before scheduling work
        if not self.async_started.wait(timeout=5):
            self.logger.error("asyncio event loop failed to start within 5s")
            return

        # Kick off the main coroutine
        asyncio.run_coroutine_threadsafe(self._async_main(), self.async_loop)
        self.logger.info("ESPHome Bridge async loop running")

    def shutdown(self):
        if self.async_loop and self.async_loop.is_running():
            # Cancel all tasks, then stop the loop
            try:
                asyncio.run_coroutine_threadsafe(
                    self._async_shutdown(), self.async_loop,
                ).result(timeout=5)
            except Exception as exc:
                self.logger.debug(f"async shutdown error: {exc}")
            self.async_loop.call_soon_threadsafe(self.async_loop.stop)
        if self.async_thread and self.async_thread.is_alive():
            self.async_thread.join(timeout=5)

    def _run_async_loop(self):
        asyncio.set_event_loop(self.async_loop)
        self.async_started.set()
        try:
            self.async_loop.run_forever()
        except Exception:
            self.logger.exception("asyncio loop crashed")
        finally:
            try:
                self.async_loop.close()
            except Exception:
                pass

    # --------------------------------------------------------
    # Async core
    # --------------------------------------------------------

    async def _async_main(self):
        """Main async entry point. Starts mDNS browsing and runs forever."""
        try:
            await self._start_mdns_browser()
            # Main loop just sleeps; work happens via mDNS callbacks and
            # per-device connection tasks spawned by them.
            while True:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.exception("async main loop failed")

    async def _async_shutdown(self):
        """Disconnect all device clients gracefully."""
        for mac, conn in list(self.connections.items()):
            client = conn.get("client")
            if client is not None:
                try:
                    await client.disconnect()
                except Exception:
                    pass
        if hasattr(self, "_zc_browser") and self._zc_browser is not None:
            try:
                await self._zc_browser.async_cancel()
            except Exception:
                pass

    # --------------------------------------------------------
    # mDNS discovery
    # --------------------------------------------------------

    async def _start_mdns_browser(self):
        """Set up zeroconf and an AsyncServiceBrowser for _esphomelib._tcp."""
        from zeroconf import IPVersion
        from zeroconf.asyncio import AsyncZeroconf, AsyncServiceBrowser

        self._zc = AsyncZeroconf(ip_version=IPVersion.V4Only)
        self._zc_browser = AsyncServiceBrowser(
            self._zc.zeroconf,
            MDNS_SERVICE_TYPE,
            handlers=[self._on_mdns_service_state_change],
        )
        self.logger.info(f"mDNS browser started (service type {MDNS_SERVICE_TYPE})")

    def _on_mdns_service_state_change(self, zeroconf, service_type, name, state_change):
        """zeroconf callback. Called from zeroconf's own thread.
        Schedule the actual handler on the asyncio loop."""
        from zeroconf import ServiceStateChange
        if state_change != ServiceStateChange.Added:
            return
        asyncio.run_coroutine_threadsafe(
            self._handle_mdns_added(zeroconf, service_type, name),
            self.async_loop,
        )

    async def _handle_mdns_added(self, zeroconf, service_type, name):
        from zeroconf.asyncio import AsyncServiceInfo
        info = AsyncServiceInfo(service_type, name)
        if not await info.async_request(zeroconf, timeout=3000):
            self.logger.debug(f"mDNS info request failed for {name}")
            return

        # Extract device details
        addresses = info.parsed_addresses() or []
        ip   = addresses[0] if addresses else ""
        port = info.port or DEFAULT_API_PORT
        # ESPHome's mDNS TXT record carries 'mac', 'version', 'platform'
        props = {
            (k.decode() if isinstance(k, bytes) else k):
            (v.decode() if isinstance(v, bytes) else v)
            for k, v in (info.properties or {}).items()
        }
        mac      = normalise_mac(props.get("mac", ""))
        hostname = name.split(".")[0]

        if not mac:
            # Fall back to hostname-derived ID if mac TXT absent
            self.logger.debug(f"mDNS service {name} has no mac TXT; skipping")
            return

        is_new = mac not in self.discovered
        self.discovered[mac] = {
            "hostname":  hostname,
            "ip":        ip,
            "port":      port,
            "version":   props.get("version", ""),
            "platform":  props.get("platform", ""),
            "board":     props.get("board", ""),
            "first_seen": time.time(),
        }

        if is_new:
            self.logger.info(
                f"Discovered ESPHome device {mac}: {hostname} at {ip}:{port} "
                f"(esphome {props.get('version','?')}, board {props.get('board','?')})"
            )
            self._fire_event("newDeviceDiscovered", mac)

        # Auto-connect (creates the Indigo node device too if auto-create is on)
        if mac not in self.connections:
            asyncio.create_task(self._connect_to_device(mac))

    # --------------------------------------------------------
    # Per-device connection
    # --------------------------------------------------------

    async def _connect_to_device(self, mac):
        """Open a persistent aioesphomeapi connection to one ESPHome device.
        Auto-reconnects with backoff on disconnect. A fresh APIClient is
        created on each reconnect attempt to avoid 'Already connected' errors
        from a stale client whose internal state survived the previous failure.
        """
        from aioesphomeapi import APIClient, APIConnectionError, InvalidAuthAPIError

        d = self.discovered.get(mac)
        if not d:
            return

        self.connections[mac] = {"client": None, "info": None, "entities": {}}
        backoff = RECONNECT_BACKOFF_INITIAL

        while True:
            # Resolve encryption key fresh on each iteration so a key clear
            # (e.g. after a "device is plaintext" error) takes effect on retry
            node_dev = self._find_node_device(mac)
            per_device_key = node_dev.pluginProps.get("encryptionKey", "") if node_dev else ""
            encryption_key = per_device_key or self.default_encryption_key or None
            client = APIClient(
                d["ip"], d["port"], password=None,
                noise_psk=encryption_key,
                client_info=f"Indigo ESPHomeBridge {PLUGIN_VERSION}",
            )
            self.connections[mac]["client"] = client

            unsubscribe = None
            try:
                self.logger.info(f"Connecting to {mac} at {d['ip']}:{d['port']}...")
                await client.connect(login=True)
                self.logger.info(f"Connected to {mac}")

                # Fetch device info + entity list
                device_info = await client.device_info()
                entities, services = await client.list_entities_services()
                self.connections[mac]["info"]     = device_info
                self.connections[mac]["entities"] = {e.key: e for e in entities}

                self.logger.info(
                    f"{mac} ({device_info.name}): {len(entities)} entities, "
                    f"esphome {device_info.esphome_version}, model {device_info.model}"
                )

                if self.auto_create_nodes:
                    self._ensure_node_device(mac, device_info)
                if self.auto_create_entities:
                    self._ensure_entity_devices(mac, entities)

                # subscribe_states is SYNCHRONOUS in aioesphomeapi (takes a
                # callback, returns an unsubscribe callable). No await.
                def _state_callback(state):
                    self._on_entity_state(mac, state)
                unsubscribe = client.subscribe_states(_state_callback)

                backoff = RECONNECT_BACKOFF_INITIAL

                # Hold the connection open. We use a long sleep rather than
                # an event-driven wait because aioesphomeapi doesn't expose a
                # disconnect-waiter; reconnect happens via the exception path
                # when the TCP connection drops and the next state callback
                # raises, or when our outer code disconnects on shutdown.
                while client._connection is not None and client._connection.is_connected:
                    await asyncio.sleep(10)
                self.logger.warning(f"{mac}: connection dropped")

            except InvalidAuthAPIError:
                self.logger.error(
                    f"{mac}: invalid API encryption key. Set the correct key in "
                    "the device's Configure dialog or in the plugin's default key. "
                    "Plugin will retry on next restart."
                )
                self._update_node_status(mac, connected=False, status="Bad key")
                return  # don't keep retrying with bad key
            except (APIConnectionError, OSError, ConnectionError) as exc:
                msg = str(exc).lower()
                # Device is plaintext but we sent encryption handshake.
                # Means a stale / wrong key is on this device's pluginProps.
                # Auto-clear it and retry — this self-heals freezer-plug-style
                # regressions where a key was accidentally written to a
                # plaintext device.
                if "using plaintext" in msg or "plaintext protocol" in msg:
                    self.logger.warning(
                        f"{mac}: device is plaintext but had an encryption key set. "
                        "Auto-clearing the key and retrying without encryption."
                    )
                    if node_dev:
                        try:
                            props = dict(node_dev.pluginProps)
                            props["encryptionKey"] = ""
                            node_dev.replacePluginPropsOnServer(props)
                        except Exception as clear_exc:
                            self.logger.debug(f"failed to clear key: {clear_exc}")
                    backoff = RECONNECT_BACKOFF_INITIAL
                    continue   # immediate retry — next loop reads fresh key (empty now)

                # 'Connection requires encryption' = device has API encryption
                # set but we have no key for it. Give up rather than spam logs.
                if "requires encryption" in msg or ("encryption" in msg and "wrong" in msg):
                    self.logger.error(
                        f"{mac}: {exc}. No usable encryption key. Set one in the "
                        "device's Configure dialog and restart the plugin. "
                        "Plugin will not retry until then."
                    )
                    self._update_node_status(mac, connected=False, status="Needs encryption key")
                    return
                self.logger.warning(f"{mac}: connection error: {exc}; reconnect in {backoff}s")
            except asyncio.CancelledError:
                self.logger.debug(f"{mac}: connection task cancelled")
                try:
                    if unsubscribe:
                        unsubscribe()
                    await client.disconnect()
                except Exception:
                    pass
                return
            except Exception:
                self.logger.exception(f"{mac}: unexpected connection error")
            finally:
                if unsubscribe:
                    try:
                        unsubscribe()
                    except Exception:
                        pass
                try:
                    await client.disconnect()
                except Exception:
                    pass

            # Update node device state to disconnected
            node = self._find_node_device(mac)
            if node:
                try:
                    node.updateStateOnServer("connected", False)
                    node.updateStateOnServer("status", "Disconnected")
                except Exception:
                    pass

            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)

    def _on_entity_state(self, mac, state):
        """Called from the asyncio thread when an entity state update arrives.
        Indigo's updateStateOnServer is thread-safe so we can write directly.
        """
        # state is one of: SwitchState, SensorState, BinarySensorState, LightState, etc.
        # All have .key matching the entity key from list_entities_services.
        key = getattr(state, "key", None)
        if key is None:
            return
        compound_addr = f"{mac}_{key}"
        dev = self.entity_devices.get(compound_addr) or self._find_entity_device(compound_addr)
        if dev is None:
            return

        try:
            self._apply_state_to_device(dev, state)
            dev.updateStateOnServer("lastSeen", datetime.now().isoformat(timespec="seconds"))
        except Exception:
            self.logger.exception(f"Failed to apply state to {dev.name}")

    # ESPHome ClimateMode enum (protobuf int) -> Indigo HvacMode
    # See aioesphomeapi.model.ClimateMode for the ESPHome int values.
    # Indigo HvacMode ints: 0=Off, 1=Heat, 2=Cool, 3=HeatCool (Auto).
    _CLIMATE_MODE_ESPHOME_TO_INDIGO = {
        0: 0,    # OFF        -> Off
        1: 3,    # HEAT_COOL  -> HeatCool
        2: 2,    # COOL       -> Cool
        3: 1,    # HEAT       -> Heat
        4: 0,    # FAN_ONLY   -> Off (Indigo has no native FanOnly mode)
        5: 0,    # DRY        -> Off (no native Indigo mode)
        6: 3,    # AUTO       -> HeatCool
    }

    _CLIMATE_MODE_INDIGO_TO_ESPHOME = {
        0: 0,    # Off       -> ESPHome OFF
        1: 3,    # Heat      -> ESPHome HEAT
        2: 2,    # Cool      -> ESPHome COOL
        3: 1,    # HeatCool  -> ESPHome HEAT_COOL
    }

    def _apply_state_to_device(self, dev, state):
        """Translate an ESPHome state object into Indigo state writes."""
        from aioesphomeapi import (
            SwitchState, SensorState, BinarySensorState, LightState,
            TextSensorState, FanState, CoverState, ClimateState,
            LockEntityState, NumberState, SelectState,
        )
        if isinstance(state, SwitchState):
            dev.updateStateOnServer("onOffState", bool(state.state))
        elif isinstance(state, BinarySensorState):
            dev.updateStateOnServer("onOffState", bool(state.state))
        elif isinstance(state, SensorState):
            if state.missing_state:
                return
            try:
                val = float(state.state)
                dev.updateStateOnServer("value", val)
            except (TypeError, ValueError):
                dev.updateStateOnServer("valueText", str(state.state))
        elif isinstance(state, TextSensorState):
            if state.missing_state:
                return
            dev.updateStateOnServer("valueText", str(state.state))
        elif isinstance(state, LightState):
            on = bool(state.state)
            # When off, force brightnessLevel to 0. Indigo's dimmer
            # semantics auto-correct onOffState based on brightness, so
            # writing brightness=75 while onState=False would leave
            # onState=True. Keep both in lockstep.
            if on and state.brightness is not None:
                dev.updateStatesOnServer([
                    {"key": "onOffState",      "value": True},
                    {"key": "brightnessLevel", "value": int(round(state.brightness * 100))},
                ])
            elif on:
                dev.updateStateOnServer("onOffState", True)
            else:
                dev.updateStatesOnServer([
                    {"key": "onOffState",      "value": False},
                    {"key": "brightnessLevel", "value": 0},
                ])
            if hasattr(state, "color_temperature") and state.color_temperature:
                dev.updateStateOnServer("colorTemp", int(state.color_temperature))
            # RGB - only write if the device actually supports color. ESPHome
            # populates state.red/green/blue even on monochromatic lights
            # (defaults of 1.0), so we can't gate on attribute presence.
            supports_rgb = bool(dev.pluginProps.get("SupportsRGB", False))
            if supports_rgb:
                if hasattr(state, "red") and state.red is not None:
                    dev.updateStateOnServer("redLevel",   int(round(state.red   * 100)))
                if hasattr(state, "green") and state.green is not None:
                    dev.updateStateOnServer("greenLevel", int(round(state.green * 100)))
                if hasattr(state, "blue") and state.blue is not None:
                    dev.updateStateOnServer("blueLevel",  int(round(state.blue  * 100)))

        elif isinstance(state, FanState):
            # ESPHome fan: state (bool), speed_level (int 0..N), oscillating,
            # direction. Map speed_level / supports_speed_levels to 0-100
            # for Indigo's brightness slider.
            on = bool(state.state)
            speed = getattr(state, "speed_level", None)
            try:
                max_speed = int((dev.pluginProps.get("speedLevels") or "0"))
            except (TypeError, ValueError):
                max_speed = 0
            if not on:
                dev.updateStatesOnServer([
                    {"key": "onOffState",      "value": False},
                    {"key": "brightnessLevel", "value": 0},
                ])
            elif speed is not None and max_speed > 0:
                pct = max(1, min(100, int(round(speed * 100 / max_speed))))
                dev.updateStatesOnServer([
                    {"key": "onOffState",      "value": True},
                    {"key": "brightnessLevel", "value": pct},
                ])
            else:
                dev.updateStateOnServer("onOffState", True)
            if hasattr(state, "oscillating") and state.oscillating is not None:
                dev.updateStateOnServer("oscillating", bool(state.oscillating))
            if hasattr(state, "direction") and state.direction is not None:
                # ESPHome FanDirection int: 0=forward, 1=reverse
                dev.updateStateOnServer("direction",
                    "reverse" if int(state.direction) == 1 else "forward")

        elif isinstance(state, LockEntityState):
            # LockState enum: 0=NONE, 1=LOCKED, 2=UNLOCKED, 3=JAMMED,
            # 4=LOCKING, 5=UNLOCKING, 6=OPENING, 7=OPEN
            try:
                lock_int = int(state.state) if state.state is not None else 0
            except (TypeError, ValueError):
                lock_int = 0
            label_map = {0:"unknown", 1:"locked", 2:"unlocked", 3:"jammed",
                         4:"locking", 5:"unlocking", 6:"opening", 7:"open"}
            label = label_map.get(lock_int, "unknown")
            # onOffState mirrors LOCKED (treat in-transit states as still effectively locked)
            on = lock_int in (1, 4, 5)   # LOCKED, LOCKING, UNLOCKING
            dev.updateStatesOnServer([
                {"key": "onOffState", "value": on},
                {"key": "lockState",  "value": label},
            ])

        elif isinstance(state, NumberState):
            if getattr(state, "missing_state", False):
                return
            try:
                dev.updateStateOnServer("value", float(state.state))
            except (TypeError, ValueError):
                pass

        elif isinstance(state, SelectState):
            if getattr(state, "missing_state", False):
                return
            dev.updateStateOnServer("selected", str(state.state))

        elif isinstance(state, ClimateState):
            # Convert ESPHome mode int to Indigo HvacMode int
            esp_mode = int(getattr(state, "mode", 0) or 0)
            indigo_mode = self._CLIMATE_MODE_ESPHOME_TO_INDIGO.get(esp_mode, 0)

            updates = [{"key": "hvacOperationMode", "value": indigo_mode}]

            # Current temperature (single sensor: temperatureInput1)
            cur = getattr(state, "current_temperature", None)
            if cur is not None:
                updates.append({"key": "temperatureInput1", "value": float(cur),
                                "uiValue": f"{float(cur):.1f}"})

            # Setpoints. Two-point devices use target_temperature_low/high.
            # Single-point devices put their target in target_temperature.
            if dev.pluginProps.get("twoPoint", False):
                lo = getattr(state, "target_temperature_low", None)
                hi = getattr(state, "target_temperature_high", None)
                if lo is not None:
                    updates.append({"key": "setpointHeat", "value": float(lo)})
                if hi is not None:
                    updates.append({"key": "setpointCool", "value": float(hi)})
            else:
                tgt = getattr(state, "target_temperature", None)
                if tgt is not None:
                    # Map single-point setpoint to whichever side the mode is on
                    if indigo_mode == 1:    # Heat
                        updates.append({"key": "setpointHeat", "value": float(tgt)})
                    elif indigo_mode == 2:  # Cool
                        updates.append({"key": "setpointCool", "value": float(tgt)})
                    else:
                        # Auto / Off - report on both
                        updates.append({"key": "setpointHeat", "value": float(tgt)})
                        updates.append({"key": "setpointCool", "value": float(tgt)})

            # Current HVAC action (heating / cooling / idle)
            action_raw = getattr(state, "action", None)
            if action_raw is not None:
                action_name = str(action_raw).split(".")[-1].lower()
                updates.append({"key": "action", "value": action_name})

            # Preset
            preset_raw = getattr(state, "preset", None)
            if preset_raw is not None and not isinstance(preset_raw, int):
                preset_name = str(preset_raw).split(".")[-1].lower()
                updates.append({"key": "preset", "value": preset_name})
            elif hasattr(state, "custom_preset") and state.custom_preset:
                updates.append({"key": "preset", "value": str(state.custom_preset)})

            try:
                dev.updateStatesOnServer(updates)
            except Exception as exc:
                self.logger.debug(f"Climate state write failed on {dev.name}: {exc}")

        elif isinstance(state, CoverState):
            # ESPHome cover: position (0.0-1.0), current_operation (0/1/2),
            # tilt (0.0-1.0). Map position to brightness 0-100; 0=closed,
            # 100=open. onOffState True if position > 0.
            pos = getattr(state, "position", None)
            op  = getattr(state, "current_operation", 0)
            if pos is not None:
                pct = int(round(pos * 100))
                dev.updateStatesOnServer([
                    {"key": "onOffState",      "value": pct > 0},
                    {"key": "brightnessLevel", "value": pct},
                ])
            op_map = {0: "idle", 1: "opening", 2: "closing"}
            dev.updateStateOnServer("currentOperation", op_map.get(int(op), str(op)))

    # --------------------------------------------------------
    # Indigo device lifecycle
    # --------------------------------------------------------

    def _ensure_device_folder(self, name):
        for folder in indigo.devices.folders:
            if folder.name == name:
                return folder.id
        new_folder = indigo.devices.folder.create(name)
        self.logger.info(f"Created device folder: '{name}'")
        return new_folder.id

    def _update_node_status(self, mac, connected, status):
        """Update the esphomeNode device's connection-status states."""
        node = self._find_node_device(mac)
        if not node:
            return
        try:
            node.updateStatesOnServer([
                {"key": "connected", "value": bool(connected)},
                {"key": "status",    "value": str(status)},
            ])
        except Exception as exc:
            self.logger.debug(f"node status update failed for {mac}: {exc}")

    def _find_node_device(self, mac):
        if mac in self.nodes_by_mac:
            return self.nodes_by_mac[mac]
        for d in indigo.devices.iter("self"):
            if d.deviceTypeId == "esphomeNode" and d.address == mac:
                self.nodes_by_mac[mac] = d
                return d
        return None

    def _find_entity_device(self, compound_addr):
        if compound_addr in self.entity_devices:
            return self.entity_devices[compound_addr]
        for d in indigo.devices.iter("self"):
            if d.address == compound_addr:
                self.entity_devices[compound_addr] = d
                return d
        return None

    def _ensure_node_device(self, mac, device_info):
        existing = self._find_node_device(mac)
        if existing:
            # Update read-only props if changed
            props = dict(existing.pluginProps)
            updates = {
                "ip":             self.discovered[mac]["ip"],
                "port":           str(self.discovered[mac]["port"]),
                "hostname":       self.discovered[mac]["hostname"],
                "boardModel":     getattr(device_info, "model", "") or "",
                "esphomeVersion": getattr(device_info, "esphome_version", "") or "",
            }
            changed = False
            for k, v in updates.items():
                if v and props.get(k, "") != v:
                    props[k] = v
                    changed = True
            if changed:
                existing.replacePluginPropsOnServer(props)
            try:
                existing.updateStateOnServer("connected", True)
                existing.updateStateOnServer("status",    "Online")
            except Exception:
                pass
            return existing

        # Create new node device
        try:
            folder_id = self._ensure_device_folder(DEVICE_FOLDER_NAME)
            props = {
                "address":        mac,
                "hostname":       self.discovered[mac]["hostname"],
                "ip":             self.discovered[mac]["ip"],
                "port":           str(self.discovered[mac]["port"]),
                "boardModel":     getattr(device_info, "model", "") or "",
                "esphomeVersion": getattr(device_info, "esphome_version", "") or "",
            }
            name = getattr(device_info, "name", "") or self.discovered[mac]["hostname"]
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                pluginId=self.pluginId,
                address=mac,
                name=name,
                deviceTypeId="esphomeNode",
                props=props,
                folder=folder_id,
            )
            ip = self.discovered[mac]["ip"]
            dev.subModel = f"{ip} - {props['boardModel']}" if props['boardModel'] else ip
            dev.replaceOnServer()
            dev.updateStateOnServer("connected", True)
            dev.updateStateOnServer("status",    "Online")
            self.nodes_by_mac[mac] = dev
            self.logger.info(f"Created Indigo node device: {dev.name} ({mac}) in folder '{DEVICE_FOLDER_NAME}'")
            return dev
        except Exception:
            self.logger.exception(f"Failed to create node device for {mac}")
            return None

    def _ensure_entity_devices(self, mac, entities):
        """Auto-create one Indigo device per ESPHome entity we know how to map."""
        from aioesphomeapi import (
            SwitchInfo, SensorInfo, BinarySensorInfo, LightInfo, TextSensorInfo,
            FanInfo, CoverInfo, ClimateInfo, LockInfo, NumberInfo, SelectInfo,
        )
        folder_id = self._ensure_device_folder(DEVICE_FOLDER_NAME)
        for e in entities:
            type_id = None
            extra_props = {}
            if isinstance(e, SwitchInfo):
                type_id = "esphomeSwitch"
            elif isinstance(e, BinarySensorInfo):
                type_id = "esphomeBinarySensor"
                extra_props["deviceClass"] = getattr(e, "device_class", "") or ""
            elif isinstance(e, SensorInfo):
                type_id = "esphomeSensor"
                extra_props["unit"] = getattr(e, "unit_of_measurement", "") or ""
            elif isinstance(e, TextSensorInfo):
                type_id = "esphomeSensor"
            elif isinstance(e, LightInfo):
                type_id = "esphomeLight"
                modes = set(getattr(e, "supported_color_modes", []) or [])
                # ColorMode int values: 1=on/off, 2=brightness, 11=color_temp,
                # 19=rgb_white, 27=rgb_cold_warm_white, 35=rgb. Presence of
                # any RGB-capable mode means SupportsColor / SupportsRGB.
                extra_props["SupportsColor"]            = any(m >= 19 for m in modes)
                extra_props["SupportsRGB"]              = any(m >= 19 for m in modes)
                extra_props["SupportsWhite"]            = 11 in modes or 27 in modes
                extra_props["SupportsWhiteTemperature"] = 11 in modes or 27 in modes
            elif isinstance(e, FanInfo):
                type_id = "esphomeFan"
                # aioesphomeapi's attribute is `supported_speed_count` (not
                # `supported_speed_levels` despite the protobuf field naming).
                extra_props["speedLevels"]          = str(getattr(e, "supported_speed_count", 0) or 0)
                extra_props["supportsOscillation"]  = bool(getattr(e, "supports_oscillation", False))
                extra_props["supportsDirection"]    = bool(getattr(e, "supports_direction", False))
            elif isinstance(e, CoverInfo):
                type_id = "esphomeCover"
                extra_props["supportsPosition"]     = bool(getattr(e, "supports_position", True))
                extra_props["supportsTilt"]         = bool(getattr(e, "supports_tilt", False))
                extra_props["deviceClass"]          = getattr(e, "device_class", "") or ""
            elif isinstance(e, LockInfo):
                type_id = "esphomeLock"
                extra_props["supportsOpen"] = bool(getattr(e, "supports_open", False))
                extra_props["requiresCode"] = bool(getattr(e, "requires_code", False))
            elif isinstance(e, NumberInfo):
                type_id = "esphomeNumber"
                extra_props["minValue"] = str(getattr(e, "min_value", 0) or 0)
                extra_props["maxValue"] = str(getattr(e, "max_value", 0) or 0)
                extra_props["step"]     = str(getattr(e, "step",      1) or 1)
                extra_props["unit"]     = getattr(e, "unit_of_measurement", "") or ""
            elif isinstance(e, SelectInfo):
                type_id = "esphomeSelect"
                opts = list(getattr(e, "options", []) or [])
                extra_props["options"] = ", ".join(str(o) for o in opts)
            elif isinstance(e, ClimateInfo):
                type_id = "esphomeClimate"
                modes = list(getattr(e, "supported_modes", []) or [])
                extra_props["visualMin"]            = str(getattr(e, "visual_min_temperature", 0) or 0)
                extra_props["visualMax"]            = str(getattr(e, "visual_max_temperature", 0) or 0)
                extra_props["supportedModes"]       = ", ".join(str(m).split(".")[-1] for m in modes)
                extra_props["twoPoint"]             = bool(getattr(e, "supports_two_point_target_temperature", False))
                extra_props["NumTemperatureInputs"] = "1" if getattr(e, "supports_current_temperature", False) else "0"
                # Heat/Cool setpoint flags driven by which modes the device exposes
                mode_names = [str(m).upper() for m in modes]
                has_heat = any("HEAT" in n for n in mode_names)
                has_cool = any("COOL" in n for n in mode_names)
                extra_props["SupportsHeatSetpoint"] = has_heat or extra_props["twoPoint"]
                extra_props["SupportsCoolSetpoint"] = has_cool or extra_props["twoPoint"]
                extra_props["SupportsHvacOperationMode"] = True
                extra_props["SupportsHvacFanMode"]  = bool(getattr(e, "supported_fan_modes", []) or [])
            else:
                continue  # unknown / unsupported entity type for v0.1

            compound = f"{mac}_{e.key}"
            if self._find_entity_device(compound):
                continue   # already exists

            name = f"{self.discovered[mac]['hostname']} - {e.name or e.object_id}"
            props = {
                "address":     compound,
                "nodeMac":     mac,
                "entityKey":   str(e.key),
                "entityName":  e.name or e.object_id or "",
                **extra_props,
            }
            try:
                dev = indigo.device.create(
                    protocol=indigo.kProtocol.Plugin,
                    pluginId=self.pluginId,
                    address=compound,
                    name=name,
                    deviceTypeId=type_id,
                    props=props,
                    folder=folder_id,
                )
                self.entity_devices[compound] = dev
                self.logger.info(f"  + Entity: {dev.name} ({type_id}, key={e.key})")
            except Exception:
                self.logger.exception(f"Failed to create entity device {compound}")

    # --------------------------------------------------------
    # Indigo native control callbacks
    # --------------------------------------------------------

    def actionControlDevice(self, action, dev):
        """Single Indigo dispatcher for both relay and dimmer actions.

        Indigo's canonical plugin SDK uses actionControlDevice for ALL
        device-control callbacks (relay turn-on/off/toggle AND dimmer
        setBrightness/brightenBy/dimBy). The separate actionControlDimmer
        method is NOT in the modern SDK and is silently ignored if defined
        on its own. Always use actionControlDevice as the single entry point.
        """
        mac = dev.pluginProps.get("nodeMac", "")
        try:
            key = int(dev.pluginProps.get("entityKey", "0"))
        except (TypeError, ValueError):
            self.logger.warning(f"{dev.name}: invalid entity key in pluginProps")
            return
        conn = self.connections.get(mac, {})
        client = conn.get("client")
        if not client:
            self.logger.warning(f"{dev.name}: no active connection to {mac}")
            return

        da = action.deviceAction

        if dev.deviceTypeId == "esphomeSwitch":
            if da == indigo.kDeviceAction.TurnOn:
                target = True
            elif da == indigo.kDeviceAction.TurnOff:
                target = False
            elif da == indigo.kDeviceAction.Toggle:
                target = not bool(dev.onState)
            else:
                self.logger.debug(f"Unhandled switch action {da} on {dev.name}")
                return

            def _do_switch():
                # switch_command is synchronous in aioesphomeapi - no await
                try:
                    client.switch_command(key=key, state=target)
                except Exception:
                    self.logger.exception(f"switch_command failed for {dev.name}")

            self.async_loop.call_soon_threadsafe(_do_switch)
            return

        if dev.deviceTypeId == "esphomeLight":
            # Empirically, ESPHome's light_command(state=False) without
            # an explicit brightness can be silently dropped on some
            # light types (monochromatic + default_transition_length).
            # Always send brightness alongside state for unambiguous intent.
            kwargs = {"key": key}
            if da == indigo.kDeviceAction.TurnOn:
                kwargs["state"] = True
                # Restore previous brightness if known, else full
                last = dev.brightness or 100
                kwargs["brightness"] = max(last, 1) / 100.0
            elif da == indigo.kDeviceAction.TurnOff:
                kwargs["state"] = False
                kwargs["brightness"] = 0.0
            elif da == indigo.kDeviceAction.Toggle:
                new_state = not bool(dev.onState)
                kwargs["state"] = new_state
                if new_state:
                    last = dev.brightness or 100
                    kwargs["brightness"] = max(last, 1) / 100.0
                else:
                    kwargs["brightness"] = 0.0
            elif da == indigo.kDeviceAction.SetBrightness:
                level = int(action.actionValue)
                kwargs["state"] = level > 0
                kwargs["brightness"] = level / 100.0
            elif da in (indigo.kDeviceAction.BrightenBy, indigo.kDeviceAction.DimBy):
                current = dev.brightness or 0
                delta = int(action.actionValue)
                if da == indigo.kDeviceAction.DimBy:
                    delta = -delta
                new_level = max(0, min(100, current + delta))
                kwargs["state"] = new_level > 0
                kwargs["brightness"] = new_level / 100.0
            elif da == indigo.kDeviceAction.SetColorLevels:
                # Indigo passes action.actionValue as a dict-like ColorValues
                # object with redLevel/greenLevel/blueLevel/whiteLevel/whiteLevel2
                # all in 0.0-100.0 range. Map to ESPHome's 0.0-1.0 rgb tuple
                # plus optional brightness preservation.
                colors = action.actionValue
                r = float(colors.get("redLevel",   0)) / 100.0
                g = float(colors.get("greenLevel", 0)) / 100.0
                b = float(colors.get("blueLevel",  0)) / 100.0
                kwargs["state"] = True
                kwargs["rgb"]   = (r, g, b)
                # Preserve current brightness
                cur_b = dev.brightness or 100
                kwargs["brightness"] = max(cur_b, 1) / 100.0
            else:
                self.logger.debug(f"Unhandled light action {da} on {dev.name}")
                return

            def _do_light():
                # light_command is synchronous in aioesphomeapi - no await
                try:
                    client.light_command(**kwargs)
                except Exception:
                    self.logger.exception(f"light_command failed for {dev.name}")

            self.async_loop.call_soon_threadsafe(_do_light)
            return

        if dev.deviceTypeId == "esphomeFan":
            try:
                max_speed = int((dev.pluginProps.get("speedLevels") or "0"))
            except (TypeError, ValueError):
                max_speed = 0
            fan_kwargs = {"key": key}
            if da == indigo.kDeviceAction.TurnOn:
                fan_kwargs["state"] = True
            elif da == indigo.kDeviceAction.TurnOff:
                fan_kwargs["state"] = False
            elif da == indigo.kDeviceAction.Toggle:
                fan_kwargs["state"] = not bool(dev.onState)
            elif da == indigo.kDeviceAction.SetBrightness:
                pct = int(action.actionValue)
                fan_kwargs["state"] = pct > 0
                if max_speed > 0 and pct > 0:
                    fan_kwargs["speed_level"] = max(1, min(max_speed, int(round(pct * max_speed / 100))))
            elif da in (indigo.kDeviceAction.BrightenBy, indigo.kDeviceAction.DimBy):
                current = dev.brightness or 0
                delta = int(action.actionValue)
                if da == indigo.kDeviceAction.DimBy:
                    delta = -delta
                pct = max(0, min(100, current + delta))
                fan_kwargs["state"] = pct > 0
                if max_speed > 0 and pct > 0:
                    fan_kwargs["speed_level"] = max(1, min(max_speed, int(round(pct * max_speed / 100))))
            else:
                self.logger.debug(f"Unhandled fan action {da} on {dev.name}")
                return

            def _do_fan():
                try:
                    client.fan_command(**fan_kwargs)
                except Exception:
                    self.logger.exception(f"fan_command failed for {dev.name}")

            self.async_loop.call_soon_threadsafe(_do_fan)
            return

        if dev.deviceTypeId == "esphomeLock":
            from aioesphomeapi import LockCommand
            if da == indigo.kDeviceAction.TurnOn:
                cmd = LockCommand.LOCK
            elif da == indigo.kDeviceAction.TurnOff:
                cmd = LockCommand.UNLOCK
            elif da == indigo.kDeviceAction.Toggle:
                cmd = LockCommand.UNLOCK if bool(dev.onState) else LockCommand.LOCK
            else:
                self.logger.debug(f"Unhandled lock action {da} on {dev.name}")
                return

            def _do_lock():
                try:
                    client.lock_command(key=key, command=cmd)
                except Exception:
                    self.logger.exception(f"lock_command failed for {dev.name}")

            self.async_loop.call_soon_threadsafe(_do_lock)
            return

        if dev.deviceTypeId == "esphomeClimate":
            # actionControlDevice doesn't carry thermostat-specific actions;
            # Indigo routes those to actionControlThermostat. Nothing to do
            # here - leave for that callback.
            self.logger.debug(f"Climate device on actionControlDevice path: {da}")
            return

        if dev.deviceTypeId == "esphomeCover":
            # Indigo's brightness 0-100 maps to position 0.0-1.0 (0=closed)
            cover_kwargs = {"key": key}
            if da == indigo.kDeviceAction.TurnOn:
                cover_kwargs["position"] = 1.0   # fully open
            elif da == indigo.kDeviceAction.TurnOff:
                cover_kwargs["position"] = 0.0   # fully closed
            elif da == indigo.kDeviceAction.SetBrightness:
                pct = int(action.actionValue)
                cover_kwargs["position"] = max(0.0, min(1.0, pct / 100.0))
            elif da in (indigo.kDeviceAction.BrightenBy, indigo.kDeviceAction.DimBy):
                current = dev.brightness or 0
                delta = int(action.actionValue)
                if da == indigo.kDeviceAction.DimBy:
                    delta = -delta
                pct = max(0, min(100, current + delta))
                cover_kwargs["position"] = pct / 100.0
            else:
                self.logger.debug(f"Unhandled cover action {da} on {dev.name}")
                return

            def _do_cover():
                try:
                    client.cover_command(**cover_kwargs)
                except Exception:
                    self.logger.exception(f"cover_command failed for {dev.name}")

            self.async_loop.call_soon_threadsafe(_do_cover)
            return

        self.logger.debug(f"actionControlDevice: no handler for type {dev.deviceTypeId} on {dev.name}")

    # --------------------------------------------------------
    # Custom actions (Actions.xml)
    # --------------------------------------------------------

    def _client_for(self, dev):
        """Common helper: resolve the aioesphomeapi client + entity key for
        a device-anchored custom action. Returns (client, key) or (None, 0)."""
        mac = dev.pluginProps.get("nodeMac", "") or dev.address
        try:
            key = int(dev.pluginProps.get("entityKey", "0") or 0)
        except (TypeError, ValueError):
            key = 0
        conn = self.connections.get(mac.split("-")[0], {})
        client = conn.get("client")
        if not client:
            self.logger.warning(f"{dev.name}: no active connection")
            return None, 0
        return client, key

    def actionSetNumberValue(self, action, dev):
        if dev.deviceTypeId != "esphomeNumber":
            return
        client, key = self._client_for(dev)
        if not client:
            return
        try:
            val = float(action.props.get("value", "0"))
        except (TypeError, ValueError):
            self.logger.warning(f"{dev.name}: bad number value")
            return

        def _do():
            try:
                client.number_command(key=key, state=val)
            except Exception:
                self.logger.exception(f"number_command failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do)

    def actionSetSelectOption(self, action, dev):
        if dev.deviceTypeId != "esphomeSelect":
            return
        client, key = self._client_for(dev)
        if not client:
            return
        opt = action.props.get("option", "").strip()
        if not opt:
            self.logger.warning(f"{dev.name}: select option not specified")
            return
        valid = [o.strip() for o in (dev.pluginProps.get("options") or "").split(",")]
        if valid and opt not in valid:
            self.logger.warning(f"{dev.name}: '{opt}' not in available options ({', '.join(valid)})")
            return

        def _do():
            try:
                client.select_command(key=key, state=opt)
            except Exception:
                self.logger.exception(f"select_command failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do)

    def actionLockOpen(self, action, dev):
        """OPEN command (latch-release) for locks that support it."""
        if dev.deviceTypeId != "esphomeLock":
            return
        from aioesphomeapi import LockCommand
        client, key = self._client_for(dev)
        if not client:
            return

        def _do():
            try:
                client.lock_command(key=key, command=LockCommand.OPEN)
            except Exception:
                self.logger.exception(f"lock_command OPEN failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do)

    def actionPressButton(self, action, dev):
        """Generic 'press an ESPHome Button entity' action, anchored on the
        esphomeNode device. Caller supplies the button's entity key (numeric)."""
        if dev.deviceTypeId != "esphomeNode":
            return
        mac = dev.address
        try:
            key = int(action.props.get("entityKey", "0") or 0)
        except (TypeError, ValueError):
            self.logger.warning(f"{dev.name}: bad entity key for pressButton")
            return
        if not key:
            self.logger.warning(f"{dev.name}: entity key required for pressButton")
            return
        conn = self.connections.get(mac, {})
        client = conn.get("client")
        if not client:
            self.logger.warning(f"{dev.name}: no active connection")
            return

        def _do():
            try:
                client.button_command(key=key)
            except Exception:
                self.logger.exception(f"button_command failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do)

    # --------------------------------------------------------
    # Thermostat actions
    # --------------------------------------------------------

    def actionControlThermostat(self, action, dev):
        """Indigo thermostat actions for esphomeClimate.

        Routes Indigo's kThermostatAction.* to ESPHome's climate_command(...).
        """
        if dev.deviceTypeId != "esphomeClimate":
            return

        mac = dev.pluginProps.get("nodeMac", "")
        try:
            key = int(dev.pluginProps.get("entityKey", "0"))
        except (TypeError, ValueError):
            return
        conn = self.connections.get(mac, {})
        client = conn.get("client")
        if not client:
            self.logger.warning(f"{dev.name}: no active connection to {mac}")
            return

        ta = action.thermostatAction
        kwargs = {"key": key}

        if ta == indigo.kThermostatAction.SetHvacMode:
            indigo_mode = int(action.actionMode)
            esp_mode = self._CLIMATE_MODE_INDIGO_TO_ESPHOME.get(indigo_mode)
            if esp_mode is None:
                self.logger.warning(f"{dev.name}: unsupported HVAC mode {indigo_mode}")
                return
            kwargs["mode"] = esp_mode

        elif ta == indigo.kThermostatAction.SetHeatSetpoint:
            sp = float(action.actionValue)
            if dev.pluginProps.get("twoPoint", False):
                kwargs["target_temperature_low"] = sp
            else:
                kwargs["target_temperature"] = sp

        elif ta == indigo.kThermostatAction.SetCoolSetpoint:
            sp = float(action.actionValue)
            if dev.pluginProps.get("twoPoint", False):
                kwargs["target_temperature_high"] = sp
            else:
                kwargs["target_temperature"] = sp

        elif ta in (indigo.kThermostatAction.IncreaseHeatSetpoint, indigo.kThermostatAction.DecreaseHeatSetpoint):
            delta = float(action.actionValue)
            if ta == indigo.kThermostatAction.DecreaseHeatSetpoint:
                delta = -delta
            current = dev.heatSetpoint or 20.0
            new_sp = current + delta
            if dev.pluginProps.get("twoPoint", False):
                kwargs["target_temperature_low"] = new_sp
            else:
                kwargs["target_temperature"] = new_sp

        elif ta in (indigo.kThermostatAction.IncreaseCoolSetpoint, indigo.kThermostatAction.DecreaseCoolSetpoint):
            delta = float(action.actionValue)
            if ta == indigo.kThermostatAction.DecreaseCoolSetpoint:
                delta = -delta
            current = dev.coolSetpoint or 24.0
            new_sp = current + delta
            if dev.pluginProps.get("twoPoint", False):
                kwargs["target_temperature_high"] = new_sp
            else:
                kwargs["target_temperature"] = new_sp

        elif ta == indigo.kThermostatAction.RequestStatusAll:
            # No explicit method; rely on subscribe_states which already
            # streams updates as they happen. Just log.
            self.logger.debug(f"{dev.name}: RequestStatusAll (passive)")
            return

        else:
            self.logger.debug(f"Unhandled thermostat action {ta} on {dev.name}")
            return

        def _do_climate():
            try:
                client.climate_command(**kwargs)
            except Exception:
                self.logger.exception(f"climate_command failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do_climate)

    # --------------------------------------------------------
    # Trigger lifecycle
    # --------------------------------------------------------

    def triggerStartProcessing(self, trigger):
        self.event_triggers[trigger.id] = trigger

    def triggerStopProcessing(self, trigger):
        self.event_triggers.pop(trigger.id, None)

    def _fire_event(self, event_type, mac):
        for trigger in self.event_triggers.values():
            if trigger.pluginTypeId != event_type:
                continue
            target = (trigger.pluginProps.get("targetAddress") or "").strip()
            if target and normalise_mac(target) != mac:
                continue
            indigo.trigger.execute(trigger)

    # --------------------------------------------------------
    # Menu callbacks
    # --------------------------------------------------------

    def menuDiscoverDevices(self, valuesDict=None, typeId=None):
        """Force a fresh mDNS query by restarting the browser (re-scans LAN)."""
        async def _refresh():
            if hasattr(self, "_zc_browser") and self._zc_browser is not None:
                try:
                    await self._zc_browser.async_cancel()
                except Exception:
                    pass
            await self._start_mdns_browser()
        asyncio.run_coroutine_threadsafe(_refresh(), self.async_loop)
        self.logger.info("mDNS browser restarted - any retained advertisements will replay")

    def menuListSeenDevices(self, valuesDict=None, typeId=None):
        if not self.discovered:
            indigo.server.log("No ESPHome devices discovered yet")
            return
        for mac, d in sorted(self.discovered.items()):
            connected = mac in self.connections and self.connections[mac].get("info")
            tag = "[CONNECTED]" if connected else "[DISCOVERED]"
            indigo.server.log(
                f"  {tag} {mac}  {d['hostname']:<25} {d['ip']:<16} "
                f"esphome {d.get('version','?')} board={d.get('board','?')}"
            )

    def menuDumpEntities(self, valuesDict=None, typeId=None):
        for mac, conn in sorted(self.connections.items()):
            info = conn.get("info")
            entities = conn.get("entities", {})
            indigo.server.log(f"=== {mac} ({info.name if info else '?'}) ===")
            for key, e in sorted(entities.items()):
                indigo.server.log(
                    f"  key={key:>8}  {type(e).__name__:<20} "
                    f"name={e.name!r:<30} object_id={getattr(e,'object_id','')!r}"
                )

    def showPluginInfo(self, valuesDict=None, typeId=None):
        if log_startup_banner:
            connected = sum(1 for c in self.connections.values() if c.get("info"))
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion, extras=[
                ("Discovered:",  str(len(self.discovered))),
                ("Connected:",   str(connected)),
                ("Indigo nodes:", str(len(self.nodes_by_mac))),
                ("Indigo entities:", str(len(self.entity_devices))),
            ])
        else:
            indigo.server.log(f"{self.pluginDisplayName} v{self.pluginVersion}")

    # --------------------------------------------------------
    # Device lifecycle
    # --------------------------------------------------------

    def deviceStartComm(self, dev):
        if dev.deviceTypeId == "esphomeNode":
            self.nodes_by_mac[dev.address] = dev
        else:
            self.entity_devices[dev.address] = dev

    def deviceStopComm(self, dev):
        if dev.deviceTypeId == "esphomeNode":
            self.nodes_by_mac.pop(dev.address, None)
        else:
            self.entity_devices.pop(dev.address, None)

    # --------------------------------------------------------
    # PluginPrefs
    # --------------------------------------------------------

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.default_encryption_key = valuesDict.get("defaultEncryptionKey", "") or ""
        self.auto_create_nodes      = bool(valuesDict.get("autoCreateDevices", True))
        self.auto_create_entities   = bool(valuesDict.get("autoCreateEntities", True))
