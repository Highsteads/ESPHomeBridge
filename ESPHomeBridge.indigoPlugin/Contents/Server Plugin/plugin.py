#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: Indigo bridge for ESPHome devices via the Native API (port 6053).
#              Auto-discovers via mDNS, connects per device via aioesphomeapi,
#              maps each ESPHome entity to a native Indigo device.
# Author:      CliveS & Claude Opus 4.7
# Date:        20-05-2026
# Version:     0.4.4

try:
    import indigo
except ImportError:
    pass

import asyncio
import json
import math
import os
import re
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
PLUGIN_VERSION = "0.4.4"

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

        # One-shot v0.4.0 migration: pre-v0.4.0 plugin created one Indigo
        # device per ESPHome entity (often 20+ devices per node). v0.4.0
        # switches to one Indigo device per ESPHome node (Tasmota-style)
        # with all entities as custom states on that single device. Wipe
        # all legacy devices; mDNS discovery will recreate them correctly.
        self._migrate_to_v040()

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

    def _migrate_to_v040(self):
        """One-shot migration from v0.3.x to v0.4.0.

        Pre-v0.4.0 the plugin created one Indigo device per ESPHome entity
        (typically 20+ devices per node — Power, Voltage, Energy, IP Address,
        MAC Address, ...). v0.4.0 collapses that to one Indigo device per
        ESPHome node (Tasmota-style), with all the per-entity values as
        custom states on that one device.

        Migration:
          1. Snapshot encryptionKey from every existing esphomeNode device
             into pluginPrefs.migration_v040_saved_keys so they can be
             restored when the new node devices are auto-recreated on
             mDNS discovery.
          2. Delete ALL devices that belong to this plugin. The next mDNS
             round will recreate them with the v0.4.0 model.
          3. Mark migration complete in pluginPrefs.migrated_v040 so we
             never run again.
        """
        if self.pluginPrefs.get("migrated_v040", False):
            return
        saved_keys = {}
        ids_to_delete = []
        for dev in indigo.devices.iter(self.pluginId):
            # Preserve encryption keys keyed on MAC (the node device's address)
            if dev.deviceTypeId == "esphomeNode":
                key = (dev.pluginProps.get("encryptionKey", "") or "").strip()
                if key:
                    saved_keys[dev.address] = key
            ids_to_delete.append((dev.id, dev.name))
        try:
            self.pluginPrefs["migration_v040_saved_keys"] = json.dumps(saved_keys)
        except Exception as exc:
            self.logger.debug(f"failed to save migration keys: {exc}")
        deleted = 0
        for dev_id, name in ids_to_delete:
            try:
                indigo.device.delete(dev_id)
                deleted += 1
            except Exception as exc:
                self.logger.warning(f"failed to delete legacy device {name} (id={dev_id}): {exc}")
        self.pluginPrefs["migrated_v040"] = True
        self.logger.warning(
            f"=== v0.4.0 migration: deleted {deleted} legacy device(s); "
            f"preserved {len(saved_keys)} encryption key(s). "
            "New one-per-node devices will be created on next mDNS discovery. ==="
        )

    # ========== v0.4.0 helpers: classify + map entities to states ==========

    _STATE_ID_OVERRIDES = {
        # entity_name -> state_id when the default camelCase isn't ideal
        "IP Address":            "ipAddress",
        "Mac Address":           "macAddress",
        "MAC Address":           "macAddress",
        "WiFi Signal dB":        "wifiSignalDb",
        "WiFi Signal Percent":   "wifiSignalPercent",
        "Connected SSID":        "connectedSsid",
        "Total Energy":          "totalEnergy",
        "Total Energy Since Boot": "totalEnergySinceBoot",
        "Last Restart":          "lastRestart",
        "Status LED":            "statusLed",
        "Power Factor":          "powerFactor",
        "Apparent Power":        "apparentPower",
        "Reactive Power":        "reactivePower",
    }

    # Priority list: first entity-info class found on a node becomes the
    # node's primary control type. Lock wins over Climate, Climate over
    # Switch, etc. — the rationale is "what the user most-likely cares
    # about controlling". If a node has none of these, it becomes a
    # plain esphomeNode (info-only, sensor states still attached).
    _PRIMARY_TYPE_PRIORITY = [
        ("LockInfo",    "esphomeLock"),
        ("ClimateInfo", "esphomeClimate"),
        ("SwitchInfo",  "esphomeSwitch"),
        ("LightInfo",   "esphomeLight"),
        ("FanInfo",     "esphomeFan"),
        ("CoverInfo",   "esphomeCover"),
    ]

    _OUR_DEVICE_TYPES = {
        "esphomeNode", "esphomeSwitch", "esphomeLight", "esphomeFan",
        "esphomeCover", "esphomeClimate", "esphomeLock",
    }

    def _to_state_id(self, name):
        """Convert ESPHome entity name to a valid Indigo state ID.

        Indigo rules (discovered the hard way in Zigbee2MQTTBridge v1.7):
          - camelCase ASCII only — NO underscores even though XML allows them
          - must start with a letter, all chars alnum
          - pluginProps keys (and state IDs) must not begin with `_`
        """
        if name in self._STATE_ID_OVERRIDES:
            return self._STATE_ID_OVERRIDES[name]
        parts = [p for p in re.split(r"[^A-Za-z0-9]+", name or "") if p]
        if not parts:
            return ""
        out = parts[0][0].lower() + parts[0][1:] if parts[0] else ""
        for p in parts[1:]:
            if not p:
                continue
            out += p[0].upper() + p[1:].lower() if len(p) > 1 else p.upper()
        out = "".join(c for c in out if c.isascii() and c.isalnum())
        if not out or not out[0].isalpha():
            out = "x" + out
        return out

    def _format_seconds(self, secs):
        """Format an integer seconds value as 'Xd Xh Xm Xs', matching the
        style Athom's text-uptime sensor uses ('3h 5m 2s'). Days only show
        if >= 1, hours only if >= 1 or days present, etc. Always shows
        seconds so values < 60s aren't blank."""
        try:
            secs = int(secs)
        except (TypeError, ValueError):
            return str(secs)
        d, rem = divmod(secs, 86400)
        h, rem = divmod(rem, 3600)
        m, s   = divmod(rem, 60)
        parts = []
        if d:
            parts.append(f"{d}d")
        if h or d:
            parts.append(f"{h}h")
        if m or h or d:
            parts.append(f"{m}m")
        parts.append(f"{s}s")
        return " ".join(parts)

    def _classify_node_type(self, entities):
        """Pick the Indigo deviceTypeId for a node based on its entities.

        Returns (type_id, primary_entity_or_None).
        """
        from aioesphomeapi import (
            SwitchInfo, LightInfo, FanInfo, CoverInfo,
            ClimateInfo, LockInfo,
        )
        type_class_map = {
            "LockInfo":    LockInfo,
            "ClimateInfo": ClimateInfo,
            "SwitchInfo":  SwitchInfo,
            "LightInfo":   LightInfo,
            "FanInfo":     FanInfo,
            "CoverInfo":   CoverInfo,
        }
        for cls_name, type_id in self._PRIMARY_TYPE_PRIORITY:
            cls = type_class_map[cls_name]
            for e in entities:
                if isinstance(e, cls):
                    return type_id, e
        return "esphomeNode", None

    def _build_entity_key_map(self, entities, primary_entity):
        """Build the entityKeyMap (stored in pluginProps as JSON).

        Maps state_id -> {key, kind, name, unit, options} where:
          - key: ESPHome entity key (int)
          - kind: sensor|text|binary|number|select|button|light|switch|fan|cover
          - name: display name from ESPHome
          - unit: unit_of_measurement if applicable
          - options: select-option list (csv) if kind==select

        The PRIMARY entity is also included so action callbacks can find
        its key under a known state_id (`primary`).
        """
        from aioesphomeapi import (
            SensorInfo, TextSensorInfo, BinarySensorInfo,
            NumberInfo, SelectInfo, ButtonInfo, LightInfo, SwitchInfo,
            FanInfo, CoverInfo, LockInfo, ClimateInfo,
        )
        kind_for = [
            (LockInfo,         "lock"),
            (ClimateInfo,      "climate"),
            (SwitchInfo,       "switch"),
            (LightInfo,        "light"),
            (FanInfo,          "fan"),
            (CoverInfo,        "cover"),
            (SensorInfo,       "sensor"),
            (TextSensorInfo,   "text"),
            (BinarySensorInfo, "binary"),
            (NumberInfo,       "number"),
            (SelectInfo,       "select"),
            (ButtonInfo,       "button"),
        ]
        primary_key = getattr(primary_entity, "key", None) if primary_entity else None
        out = {}
        used_ids = set()
        # The primary entity gets state_id="primary" — actions look it up here.
        if primary_entity is not None:
            kind = "node"
            for cls, k in kind_for:
                if isinstance(primary_entity, cls):
                    kind = k
                    break
            out["primary"] = {
                "key":  int(primary_entity.key),
                "kind": kind,
                "name": primary_entity.name or primary_entity.object_id or "",
                "unit": getattr(primary_entity, "unit_of_measurement", "") or "",
            }
            used_ids.add("primary")
        for e in entities:
            if primary_key is not None and getattr(e, "key", None) == primary_key:
                continue
            kind = None
            for cls, k in kind_for:
                if isinstance(e, cls):
                    kind = k
                    break
            if kind is None:
                continue
            base = self._to_state_id(e.name or e.object_id or "")
            if not base:
                continue
            state_id = base
            n = 2
            while state_id in used_ids:
                state_id = f"{base}{n}"
                n += 1
            used_ids.add(state_id)
            info = {
                "key":  int(e.key),
                "kind": kind,
                "name": e.name or e.object_id or "",
                "unit": getattr(e, "unit_of_measurement", "") or "",
            }
            if kind == "select":
                info["options"] = list(getattr(e, "options", []) or [])
            if kind == "number":
                info["min"]  = float(getattr(e, "min_value", 0) or 0)
                info["max"]  = float(getattr(e, "max_value", 0) or 0)
                info["step"] = float(getattr(e, "step",      1) or 1)
            out[state_id] = info
        return out

    # Node-info states that the plugin populates itself from mDNS /
    # device_info (not from any ESPHome entity). Always added unless
    # the firmware also exposes a same-named entity (avoids duplicates).
    _NODE_INFO_STATES = ("ipAddress", "macAddress", "boardModel", "esphomeVersion")

    def getDeviceStateList(self, dev):
        """Add dynamic states declared in pluginProps.entityKeyMap to the
        base state list from Devices.xml. Per Indigo's gotcha rules
        (Zigbee2MQTTBridge v1.7 lesson): always make a COPY of the base
        list before appending — the parser returns a live reference and
        mutating it permanently corrupts subsequent reads.
        """
        state_list = list(indigo.PluginBase.getDeviceStateList(self, dev) or [])
        if dev.deviceTypeId not in self._OUR_DEVICE_TYPES:
            return state_list
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
        except Exception:
            em = {}
        # Add node-info states (ipAddress, macAddress, etc.) unless the
        # firmware exposes its own same-named entity that would conflict.
        for sid in self._NODE_INFO_STATES:
            if sid in em:
                continue
            state_list.append(self.getDeviceStateDictForStringType(sid, sid, sid))
        for sid, info in em.items():
            if sid == "primary":
                continue  # primary's data goes to native states (onOffState etc)
            kind = info.get("kind", "sensor")
            label = info.get("name", sid)
            if kind in ("sensor", "number"):
                state_list.append(self.getDeviceStateDictForNumberType(sid, label, label))
            elif kind in ("text", "select"):
                state_list.append(self.getDeviceStateDictForStringType(sid, label, label))
            elif kind == "binary":
                state_list.append(self.getDeviceStateDictForBoolOnOffType(sid, label, label))
            elif kind in ("switch", "light", "fan", "cover", "lock"):
                # Secondary control entities: expose as on/off plus a brightness
                # state for light/fan/cover. For now just a bool — full
                # secondary-control devices are a future enhancement.
                state_list.append(self.getDeviceStateDictForBoolOnOffType(sid, label, label))
                if kind in ("light", "fan", "cover"):
                    state_list.append(self.getDeviceStateDictForNumberType(
                        sid + "Level", label + " Level", label + " Level"))
        return state_list

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
            # (e.g. after a "device is plaintext" error) takes effect on retry.
            # v0.4.0: also consult the migration-saved keys snapshot — the
            # node device may not exist yet at first-connect, but a key
            # preserved across the v0.4.0 migration must still apply.
            node_dev = self._find_node_device(mac)
            per_device_key = node_dev.pluginProps.get("encryptionKey", "") if node_dev else ""
            if not per_device_key:
                per_device_key = self._migration_saved_keys().get(mac, "")
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

                # v0.4.0: one Indigo device per node. _ensure_node_device
                # picks the device type from the node's entity list and
                # stores the entityKeyMap so state writes can be routed
                # by entity_key. No per-entity device creation any more.
                if self.auto_create_nodes:
                    self._ensure_node_device(mac, device_info, entities)

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
        """v0.4.0: state updates land on the SINGLE node device for this
        MAC. Look up which state_id this entity_key maps to via the
        device's entityKeyMap, then route the write either to native
        states (if this is the primary entity) or to the corresponding
        custom state."""
        key = getattr(state, "key", None)
        if key is None:
            return
        dev = self._find_node_device(mac)
        if dev is None:
            return
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
        except Exception:
            return
        # Find which state_id this entity belongs to (key -> state_id)
        state_id = None
        info = None
        for sid, einfo in em.items():
            if int(einfo.get("key", -1)) == int(key):
                state_id = sid
                info = einfo
                break
        if state_id is None:
            # Entity not in the map — was added after device creation.
            # A future enhancement could re-run _ensure_node_device to
            # rebuild the map. For now, drop silently.
            return
        try:
            self._apply_v040_state(dev, state, state_id, info)
            dev.updateStateOnServer("lastSeen", datetime.now().isoformat(timespec="seconds"))
        except Exception:
            self.logger.exception(f"Failed to apply state to {dev.name} ({state_id})")

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

    def _apply_v040_state(self, dev, state, state_id, info):
        """v0.4.0 state writer.

        If state_id == "primary" we're getting an update for the entity
        that backs the node's Indigo device type (Lock/Climate/Switch/
        Light/Fan/Cover). Route into the native Indigo states for that
        device type (onOffState, brightnessLevel, hvacOperationMode, etc.)
        by delegating to the existing v0.3.x _apply_state_to_device logic
        — that handler is unchanged because the writes it makes (e.g.
        onOffState, brightnessLevel) are correct for the new model too.

        Otherwise this is a secondary entity that lives as a custom
        state on the device — write the value into that state.
        """
        from aioesphomeapi import (
            SwitchState, SensorState, BinarySensorState, LightState,
            TextSensorState, FanState, CoverState, ClimateState,
            LockEntityState, NumberState, SelectState,
        )
        if state_id == "primary":
            # Delegate to legacy handler — its writes target the native
            # Indigo states (onOffState, brightnessLevel, hvacOperationMode)
            # which is exactly what we want for the primary entity.
            self._apply_state_to_device(dev, state)
            return

        # Secondary entity — write to the dynamic state.
        kind = info.get("kind", "sensor")
        if isinstance(state, SensorState):
            if getattr(state, "missing_state", False):
                return
            try:
                raw = float(state.state)
                unit = info.get("unit", "")
                if math.isnan(raw):
                    val = raw
                    ui  = "nan"
                elif unit == "s":
                    # Sensor reports seconds (uptime, runtime, etc). Keep
                    # the raw stored value as integer seconds — useful for
                    # script logic ("uptime > 86400") — but show a
                    # human-readable Xd Xh Xm Xs in the .ui.
                    val = int(round(raw))
                    ui  = self._format_seconds(val)
                else:
                    # Round the RAW stored value to 2dp. Indigo's Custom
                    # States panel displays the raw state, not the .ui
                    # suffix — so for clean readings (33.60 kWh, not
                    # 33.59825134277344) the raw itself has to be rounded.
                    val = round(raw, 2)
                    ui  = f"{val:.2f} {unit}".rstrip() if unit else f"{val:.2f}"
                dev.updateStateOnServer(state_id, val, uiValue=ui)
            except (TypeError, ValueError):
                dev.updateStateOnServer(state_id, str(state.state))
        elif isinstance(state, TextSensorState):
            if getattr(state, "missing_state", False):
                return
            dev.updateStateOnServer(state_id, str(state.state))
        elif isinstance(state, BinarySensorState):
            dev.updateStateOnServer(state_id, bool(state.state))
        elif isinstance(state, NumberState):
            if getattr(state, "missing_state", False):
                return
            try:
                dev.updateStateOnServer(state_id, float(state.state))
            except (TypeError, ValueError):
                pass
        elif isinstance(state, SelectState):
            if getattr(state, "missing_state", False):
                return
            dev.updateStateOnServer(state_id, str(state.state))
        elif isinstance(state, SwitchState):
            dev.updateStateOnServer(state_id, bool(state.state))
        elif isinstance(state, LightState):
            # Secondary light (e.g. Athom Status LED) — bool + Level
            dev.updateStatesOnServer([
                {"key": state_id,            "value": bool(state.state)},
                {"key": state_id + "Level",  "value": int(round((getattr(state, "brightness", 0) or 0) * 100))},
            ])
        elif isinstance(state, FanState):
            dev.updateStateOnServer(state_id, bool(state.state))
        elif isinstance(state, CoverState):
            pos = getattr(state, "position", None)
            updates = [{"key": state_id, "value": pos is not None and pos > 0}]
            if pos is not None:
                updates.append({"key": state_id + "Level", "value": int(round(pos * 100))})
            dev.updateStatesOnServer(updates)
        elif isinstance(state, LockEntityState):
            try:
                lock_int = int(state.state) if state.state is not None else 0
            except (TypeError, ValueError):
                lock_int = 0
            dev.updateStateOnServer(state_id, lock_int in (1, 4, 5))  # LOCKED-ish

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
        """v0.4.0: there is now exactly one Indigo device per MAC, of one
        of several types (esphomeNode/Switch/Light/Fan/Cover/Climate/Lock)
        depending on the node's primary entity. Address is always the MAC."""
        if mac in self.nodes_by_mac:
            return self.nodes_by_mac[mac]
        for d in indigo.devices.iter("self"):
            if d.address == mac and d.deviceTypeId in self._OUR_DEVICE_TYPES:
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

    def _ensure_node_device(self, mac, device_info, entities):
        """v0.4.0 one-device-per-node creator.

        Picks the Indigo device type from the entity list (Lock > Climate >
        Switch > Light > Fan > Cover > else esphomeNode), builds the
        entityKeyMap describing every entity's state ID + kind, and
        creates or updates the single Indigo device for this node.

        On create, looks up any saved encryption key from the v0.4.0
        migration snapshot and applies it so encrypted-rig style nodes
        don't need their key re-entered after the upgrade.
        """
        ip   = self.discovered[mac]["ip"]
        port = self.discovered[mac]["port"]
        host = self.discovered[mac]["hostname"]
        board = getattr(device_info, "model", "") or ""
        esp_v = getattr(device_info, "esphome_version", "") or ""

        type_id, primary_entity = self._classify_node_type(entities)
        entity_key_map = self._build_entity_key_map(entities, primary_entity)

        # Per-type extra pluginProps (capabilities derived from primary entity)
        extra_props = self._props_for_primary(type_id, primary_entity)

        existing = self._find_node_device(mac)
        if existing:
            # In-place update — but if Indigo's existing deviceTypeId doesn't
            # match the type we'd pick now, we can't just rename it in place
            # (Indigo forbids deviceTypeId changes). Delete + recreate.
            if existing.deviceTypeId != type_id:
                self.logger.info(
                    f"{mac}: device type changed "
                    f"{existing.deviceTypeId} -> {type_id}; recreating"
                )
                preserved_key = existing.pluginProps.get("encryptionKey", "")
                try:
                    indigo.device.delete(existing.id)
                except Exception as exc:
                    self.logger.warning(f"failed to delete old node device: {exc}")
                # Fall through to create
                existing = None
                if preserved_key:
                    saved = self._migration_saved_keys()
                    saved[mac] = preserved_key
                    self.pluginPrefs["migration_v040_saved_keys"] = json.dumps(saved)
            else:
                props = dict(existing.pluginProps)
                props.update({
                    "ip":             ip,
                    "port":           str(port),
                    "hostname":       host,
                    "boardModel":     board,
                    "esphomeVersion": esp_v,
                    "entityKeyMap":   json.dumps(entity_key_map),
                })
                props.update(extra_props)
                try:
                    existing.replacePluginPropsOnServer(props)
                    existing = indigo.devices[existing.id]   # re-fetch — stale
                    existing.stateListOrDisplayStateIdChanged()
                    existing.updateStateOnServer("connected", True)
                    existing.updateStateOnServer("status",    "Online")
                    self._write_node_info_states(existing, mac, ip, board, esp_v, entity_key_map)
                except Exception as exc:
                    self.logger.debug(f"props/state update failed for {mac}: {exc}")
                self.nodes_by_mac[mac] = existing
                return existing

        # Create new device
        try:
            folder_id = self._ensure_device_folder(DEVICE_FOLDER_NAME)
            props = {
                "address":        mac,
                "hostname":       host,
                "ip":             ip,
                "port":           str(port),
                "boardModel":     board,
                "esphomeVersion": esp_v,
                "entityKeyMap":   json.dumps(entity_key_map),
            }
            # Restore any preserved encryption key for this MAC
            saved = self._migration_saved_keys()
            if mac in saved:
                props["encryptionKey"] = saved[mac]
            props.update(extra_props)
            name = host or getattr(device_info, "name", "") or mac
            dev = indigo.device.create(
                protocol=indigo.kProtocol.Plugin,
                pluginId=self.pluginId,
                address=mac,
                name=name,
                deviceTypeId=type_id,
                props=props,
                folder=folder_id,
            )
            dev.subModel = f"{ip} - {board}" if board else ip
            dev.replaceOnServer()
            dev = indigo.devices[dev.id]                     # re-fetch
            dev.stateListOrDisplayStateIdChanged()
            dev.updateStateOnServer("connected", True)
            dev.updateStateOnServer("status",    "Online")
            self._write_node_info_states(dev, mac, ip, board, esp_v, entity_key_map)
            self.nodes_by_mac[mac] = dev
            self.logger.info(
                f"Created Indigo device: {dev.name} ({mac}) type={type_id} "
                f"with {len(entity_key_map)} entities mapped to states "
                f"in folder '{DEVICE_FOLDER_NAME}'"
            )
            return dev
        except Exception:
            self.logger.exception(f"Failed to create node device for {mac}")
            return None

    def _props_for_primary(self, type_id, primary_entity):
        """Capability flags / metadata derived from the primary entity for
        each Indigo device type. These mirror the per-entity extra_props
        used in the v0.3.x model (light SupportsRGB, fan speedLevels,
        climate visualMin/Max, etc.) so the dimmer / thermostat / etc.
        controls in Indigo's UI work natively."""
        if primary_entity is None:
            return {}
        from aioesphomeapi import (
            LightInfo, FanInfo, CoverInfo, ClimateInfo, LockInfo,
        )
        if isinstance(primary_entity, LightInfo) and type_id == "esphomeLight":
            modes = set(getattr(primary_entity, "supported_color_modes", []) or [])
            return {
                "SupportsColor":            any(m >= 19 for m in modes),
                "SupportsRGB":              any(m >= 19 for m in modes),
                "SupportsWhite":            11 in modes or 27 in modes,
                "SupportsWhiteTemperature": 11 in modes or 27 in modes,
            }
        if isinstance(primary_entity, FanInfo) and type_id == "esphomeFan":
            return {
                "speedLevels":         str(getattr(primary_entity, "supported_speed_count", 0) or 0),
                "supportsOscillation": bool(getattr(primary_entity, "supports_oscillation", False)),
                "supportsDirection":   bool(getattr(primary_entity, "supports_direction", False)),
            }
        if isinstance(primary_entity, CoverInfo) and type_id == "esphomeCover":
            return {
                "supportsPosition": bool(getattr(primary_entity, "supports_position", True)),
                "supportsTilt":     bool(getattr(primary_entity, "supports_tilt", False)),
                "deviceClass":      getattr(primary_entity, "device_class", "") or "",
            }
        if isinstance(primary_entity, LockInfo) and type_id == "esphomeLock":
            return {
                "supportsOpen": bool(getattr(primary_entity, "supports_open", False)),
                "requiresCode": bool(getattr(primary_entity, "requires_code", False)),
            }
        if isinstance(primary_entity, ClimateInfo) and type_id == "esphomeClimate":
            modes      = list(getattr(primary_entity, "supported_modes", []) or [])
            mode_names = [str(m).upper() for m in modes]
            has_heat   = any("HEAT" in n for n in mode_names)
            has_cool   = any("COOL" in n for n in mode_names)
            two_point  = bool(getattr(primary_entity, "supports_two_point_target_temperature", False))
            return {
                "visualMin":               str(getattr(primary_entity, "visual_min_temperature", 0) or 0),
                "visualMax":               str(getattr(primary_entity, "visual_max_temperature", 0) or 0),
                "supportedModes":          ", ".join(str(m).split(".")[-1] for m in modes),
                "twoPoint":                two_point,
                "NumTemperatureInputs":    "1" if getattr(primary_entity, "supports_current_temperature", False) else "0",
                "SupportsHeatSetpoint":    has_heat or two_point,
                "SupportsCoolSetpoint":    has_cool or two_point,
                "SupportsHvacOperationMode": True,
                "SupportsHvacFanMode":     bool(getattr(primary_entity, "supported_fan_modes", []) or []),
            }
        return {}

    def _write_node_info_states(self, dev, mac, ip, board, esp_v, entity_key_map):
        """Populate the plugin-supplied node info states (ipAddress,
        macAddress, boardModel, esphomeVersion). These mirror what the
        Athom firmware exposes via dedicated entities but apply to any
        node regardless of what its YAML chose to expose.

        If the firmware does export an entity with the same state_id
        (e.g. Athom's IP Address text sensor maps to "ipAddress"), the
        entity write will overwrite ours on the next state update —
        which is what we want (the entity might be more up-to-date if
        the device gets a new DHCP lease).
        """
        # Format the bare MAC ("8CCE4E574F8D") as the colon-separated
        # form humans expect ("8C:CE:4E:57:4F:8D")
        formatted_mac = ":".join(mac[i:i+2] for i in range(0, len(mac), 2)) if mac else ""
        updates = []
        for sid, val in (
            ("ipAddress",      ip or ""),
            ("macAddress",     formatted_mac),
            ("boardModel",     board or ""),
            ("esphomeVersion", esp_v or ""),
        ):
            if sid in entity_key_map:
                continue   # firmware-driven entity wins
            updates.append({"key": sid, "value": str(val)})
        if updates:
            try:
                dev.updateStatesOnServer(updates)
            except Exception as exc:
                self.logger.debug(f"node info state write failed for {mac}: {exc}")

    def _migration_saved_keys(self):
        """Read the v0.4.0 migration's saved encryption-key snapshot."""
        try:
            return json.loads(self.pluginPrefs.get("migration_v040_saved_keys", "") or "{}")
        except Exception:
            return {}

    def _ensure_entity_devices(self, mac, entities):
        """v0.4.0: entities no longer become Indigo devices. They are
        mapped to custom states on the single node device by
        _ensure_node_device(). This method is kept as a no-op for any
        legacy code path that might still call it."""
        return

    def _ensure_entity_devices_LEGACY(self, mac, entities):
        """Legacy v0.3.x one-device-per-entity creator. Retained for
        reference only — no longer reached. Will be deleted in v0.5."""
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
                # Tell Indigo this is NOT a relay. Hidden XML defaults are
                # NOT applied at indigo.device.create() — must be in props.
                extra_props["SupportsOnState"]     = False
                extra_props["SupportsSensorValue"] = False
                extra_props["isTextSensor"]        = False
            elif isinstance(e, TextSensorInfo):
                type_id = "esphomeSensor"
                extra_props["SupportsOnState"]     = False
                extra_props["SupportsSensorValue"] = False
                extra_props["isTextSensor"]        = True
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

        v0.4.0: dev is now the SINGLE node device, and the primary
        entity's ESPHome key lives in pluginProps["entityKeyMap"]
        under state_id "primary". Look it up there.
        """
        mac = dev.address                 # node device's address IS the MAC
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
            key = int(em.get("primary", {}).get("key", 0))
        except Exception:
            self.logger.warning(f"{dev.name}: entityKeyMap missing/invalid in pluginProps")
            return
        if not key:
            self.logger.warning(f"{dev.name}: no primary entity key — node has no controllable entity")
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

    def _client_and_key(self, dev, state_id):
        """Resolve (client, entity_key) for a custom action targeting one
        entity on a node device. state_id picks which entity in the
        device's entityKeyMap to use.

        Returns (client, key) or (None, 0). Logs the reason on failure.
        """
        mac = dev.address
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
        except Exception:
            self.logger.warning(f"{dev.name}: entityKeyMap missing/invalid")
            return None, 0
        info = em.get(state_id)
        if not info:
            self.logger.warning(f"{dev.name}: no entity mapped to state_id '{state_id}'")
            return None, 0
        try:
            key = int(info.get("key", 0))
        except (TypeError, ValueError):
            key = 0
        if not key:
            return None, 0
        conn = self.connections.get(mac, {})
        client = conn.get("client")
        if not client:
            self.logger.warning(f"{dev.name}: no active connection to {mac}")
            return None, 0
        return client, key

    # --- Action ConfigUI list callbacks (entity dropdowns) ---

    def _list_entities_of_kind(self, dev_id, kind):
        """Return [(state_id, display_name), ...] for the given device's
        entities of the given kind. Used by action ConfigUI dropdowns."""
        try:
            dev = indigo.devices[int(dev_id)] if dev_id else None
        except Exception:
            return []
        if dev is None:
            return []
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
        except Exception:
            return []
        items = []
        for sid, info in em.items():
            if info.get("kind") != kind:
                continue
            label = info.get("name") or sid
            items.append((sid, label))
        items.sort(key=lambda x: x[1].lower())
        return items

    def getNumberEntities(self, filter, valuesDict, typeId, targetId):
        return self._list_entities_of_kind(targetId, "number")

    def getSelectEntities(self, filter, valuesDict, typeId, targetId):
        return self._list_entities_of_kind(targetId, "select")

    def getButtonEntities(self, filter, valuesDict, typeId, targetId):
        return self._list_entities_of_kind(targetId, "button")

    # --- Custom action callbacks ---

    def actionSetNumberValue(self, action, dev):
        """Set a Number entity on this node device. The action's
        entityStateId picks which Number entity to target."""
        state_id = (action.props.get("entityStateId") or "").strip()
        if not state_id:
            self.logger.warning(f"{dev.name}: entityStateId not chosen in action config")
            return
        client, key = self._client_and_key(dev, state_id)
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
        """Set a Select entity on this node device. entityStateId picks
        which Select; option must be one of the entity's declared options."""
        state_id = (action.props.get("entityStateId") or "").strip()
        if not state_id:
            self.logger.warning(f"{dev.name}: entityStateId not chosen in action config")
            return
        client, key = self._client_and_key(dev, state_id)
        if not client:
            return
        opt = (action.props.get("option") or "").strip()
        if not opt:
            self.logger.warning(f"{dev.name}: select option not specified")
            return
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
            valid = em.get(state_id, {}).get("options", [])
        except Exception:
            valid = []
        if valid and opt not in valid:
            self.logger.warning(
                f"{dev.name}: '{opt}' not in available options ({', '.join(valid)})"
            )
            return

        def _do():
            try:
                client.select_command(key=key, state=opt)
            except Exception:
                self.logger.exception(f"select_command failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do)

    def actionLockOpen(self, action, dev):
        """OPEN command (latch-release) for locks that support it.
        Targets the device's primary lock entity."""
        if dev.deviceTypeId != "esphomeLock":
            return
        from aioesphomeapi import LockCommand
        client, key = self._client_and_key(dev, "primary")
        if not client:
            return

        def _do():
            try:
                client.lock_command(key=key, command=LockCommand.OPEN)
            except Exception:
                self.logger.exception(f"lock_command OPEN failed for {dev.name}")

        self.async_loop.call_soon_threadsafe(_do)

    def actionPressButton(self, action, dev):
        """Press a Button entity on this node device. entityStateId picks
        which button."""
        state_id = (action.props.get("entityStateId") or "").strip()
        if not state_id:
            self.logger.warning(f"{dev.name}: entityStateId not chosen in action config")
            return
        client, key = self._client_and_key(dev, state_id)
        if not client:
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

        # v0.4.0: primary entity key lives in entityKeyMap, node device's
        # address is the MAC directly
        mac = dev.address
        try:
            em = json.loads(dev.pluginProps.get("entityKeyMap", "") or "{}")
            key = int(em.get("primary", {}).get("key", 0))
        except Exception:
            self.logger.warning(f"{dev.name}: entityKeyMap missing/invalid")
            return
        if not key:
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

    def getDeviceDisplayStateId(self, dev):
        """v0.4.0: device list display column.

        For info-only nodes (esphomeNode) we want the 'status' string
        (Online/Disconnected/Bad key). For nodes with a primary control
        entity (relay/dimmer/thermostat/etc.) Indigo's default native
        state (onOffState, brightnessLevel, hvacOperationMode) is the
        right pick — defer to PluginBase.
        """
        if dev.deviceTypeId == "esphomeNode":
            return "status"
        return indigo.PluginBase.getDeviceDisplayStateId(self, dev)

    def deviceStartComm(self, dev):
        # v0.4.0: every device created by this plugin is now a node device
        # (one per ESPHome node). entity_devices dict kept only for
        # transitional compat with any code path that still references it.
        if dev.deviceTypeId in self._OUR_DEVICE_TYPES:
            self.nodes_by_mac[dev.address] = dev

    def deviceStopComm(self, dev):
        if dev.deviceTypeId in self._OUR_DEVICE_TYPES:
            self.nodes_by_mac.pop(dev.address, None)

    # --------------------------------------------------------
    # PluginPrefs
    # --------------------------------------------------------

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.default_encryption_key = valuesDict.get("defaultEncryptionKey", "") or ""
        self.auto_create_nodes      = bool(valuesDict.get("autoCreateDevices", True))
        self.auto_create_entities   = bool(valuesDict.get("autoCreateEntities", True))
