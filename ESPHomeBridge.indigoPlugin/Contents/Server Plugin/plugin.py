#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    plugin.py
# Description: Indigo bridge for ESPHome devices via the Native API (port 6053).
#              Auto-discovers via mDNS, connects per device via aioesphomeapi,
#              maps each ESPHome entity to a native Indigo device.
# Author:      CliveS & Claude Opus 4.8
# Date:        22-07-2026
# Version:     0.8.0
#
# v0.8.0 (22-07-2026): ignore list. New Configure field 'Ignore these devices'
# (MACs, hostnames or IPs, comma/space separated) for hardware that advertises
# _esphomelib._tcp but is not an ESPHome node — the SMLIGHT SMHUB/SLZB boxes
# being the live case. Ignored devices are never probed, parked or warned
# about, show as [IGNORED] in List Seen Devices, and a prefs change takes
# effect immediately (parked entries dropped, live retry loops ended).
#
# v0.7.1 (21-07-2026): shared plugin_utils.py refreshed to v1.3 — the
# estate-wide propagation of the four Appliance Monitor deep-review fixes.
# * install_timestamp_filter() is idempotent — a second call used to stack a
#   second filter, so every log line came out with two timestamps.
# * `import indigo` is soft, so the module imports outside the Indigo host and
#   can be exercised by offline tests.
# * A malformed log call keeps its arguments in the log instead of dropping
#   them, so a %-placeholder mismatch is visible.
# * New shared as_bool() — a pref re-serialised as the string "false" is
#   truthy, which is exactly the wrong answer.
#
# v0.7.0 (21-07-2026): DEEP REVIEW batch. Dynamic state IDs can no longer
# collide with the states declared in Devices.xml, with the plugin's own node-info
# states, or with Indigo's reserved native names (a "Battery Level" sensor now maps
# to `battery`, not the reserved `batteryLevel`). NaN / infinity readings are
# dropped instead of being written into a Number state. The v0.4.0 migration only
# deletes genuinely legacy devices, so a lost preferences flush can't wipe a
# working set. Checkbox preferences are read through a real boolean coercion
# ("false" used to read as True). esphomeSensor devices answer Indigo's status
# request. The Device Came Online / Went Offline events actually fire. mDNS
# discovery retries if zeroconf fails to start, discovery callbacks log their own
# exceptions, and connection tasks are held so the garbage collector can't cancel
# them. The default encryption key is read from IndigoSecrets.py first. Log level
# set in Configure now takes effect. Dead v0.3.x code removed.
#
# v0.6.0 (17-06-2026): SENSOR NODES + intent-aware classification. The node-type
# classifier no longer lets a status-LED Light hijack the device type: a Light is
# demoted from "primary" when it's flagged config/diagnostic OR the node carries
# power/energy sensors (i.e. it's a meter, and the light is the plug's status LED).
# A node with no genuine control but with sensors is now created as a new
# esphomeSensor (type="sensor") device whose headline reading — chosen by
# device_class priority (power > energy > temperature > …) — drives the native
# sensorValue; all other entities remain custom states. Fixes relay-less Athom
# power-monitor plugs that were being created as dimmers. Switchable plugs (Switch
# present) and real lamps are unaffected. Migration of mis-typed nodes is automatic
# via the existing recreate-on-type-change path. (Manual type override is a planned
# follow-up.) plugin.py header version was lagging at 0.5.3; now aligned.
#
# v0.5.1 (23-05-2026): Millisecond timestamp [HH:MM:SS.mmm] prefix on every
# log line via plugin_utils.install_timestamp_filter() — matches Device
# Activity Monitor convention. Module-level log() helper bumped to ms.
# New "Toggle Timestamps in Log" menu item.

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
try:
    from plugin_utils import install_timestamp_filter
except ImportError:
    install_timestamp_filter = None

# Secrets policy: credentials come from IndigoSecrets.py first, PluginConfig
# second. Per-key try/except so a missing key can't blank the others.
sys.path.insert(0, "/Library/Application Support/Perceptive Automation")
try:
    from IndigoSecrets import ESPHOME_DEFAULT_ENCRYPTION_KEY
except ImportError:
    ESPHOME_DEFAULT_ENCRYPTION_KEY = ""

# aioesphomeapi + zeroconf are installed via requirements.txt into
# Contents/Packages/ on plugin startup. They're imported lazily in the
# async-thread setup so import errors get logged through self.logger
# rather than crashing the whole plugin on load.


# ============================================================
# Constants
# ============================================================

PLUGIN_ID      = "com.clives.indigoplugin.esphomebridge"
PLUGIN_VERSION = "0.8.0"

DEVICE_FOLDER_NAME = "ESPHome"

# How often the mDNS browser re-broadcasts (it's continuous between)
MDNS_SERVICE_TYPE = "_esphomelib._tcp.local."

# ESPHome's native API default port
DEFAULT_API_PORT = 6053

# Connection backoff
RECONNECT_BACKOFF_INITIAL = 5    # seconds
RECONNECT_BACKOFF_MAX     = 300

# Adaptive retry tiers. Not everything advertising _esphomelib._tcp is an
# ESPHome node — a SMLIGHT SMHUB Zigbee coordinator borrows the same service
# type, opens port 6053 and then never answers the handshake. Left alone the
# plugin retried forever and warned every 35 seconds about hardware the user
# had never adopted. So: warn once, retry quietly with back-off, then park.
MAX_CONNECT_FAILURES_ADOPTED   = 10   # a node the user HAS added to Indigo
MAX_CONNECT_FAILURES_UNADOPTED = 3    # merely discovered — likely not ours at all
# How long a parked node is left alone before one more attempt. Adding it as an
# Indigo device un-parks it immediately (see deviceStartComm).
PARKED_RETRY_AFTER = 3600         # seconds


# ============================================================
# Helpers
# ============================================================

import logging


_LOG_LEVELS = {
    "DEBUG":   logging.DEBUG,
    "INFO":    logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR":   logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def _lvl(level):
    """Map a level NAME to a Python logging int.

    indigo.server.log(level=...) wants an int. A STRING is silently ignored
    and the line logs as plain Info (21-07-2026 estate-wide sweep).
    """
    if isinstance(level, int):
        return level
    return _LOG_LEVELS.get(str(level).upper(), logging.INFO)


def log(message, level="INFO"):
    indigo.server.log(f"[{datetime.now().strftime('%H:%M:%S.%f')[:-3]}] {message}", level=_lvl(level))


def as_bool(value, default=False):
    """Coerce a pluginPrefs / pluginProps value to a real bool.

    Indigo re-serialises a saved dialog's checkbox as the STRING "false", and
    bool("false") is True — so every checkbox read has to go through this.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("true", "yes", "on", "1"):
        return True
    if text in ("false", "no", "off", "0", ""):
        return False
    return default


def normalise_mac(raw):
    """Convert any MAC representation to 12-char uppercase hex with no separators."""
    if not raw:
        return ""
    return "".join(c for c in raw.upper() if c in "0123456789ABCDEF")[:12]


def parse_ignore_list(raw):
    """Parse the Configure dialog's 'Ignore these devices' field.

    Tokens are comma or space separated. A token that is a MAC in any common
    notation (AABBCC001122, aa:bb:cc:00:11:22, aa-bb-cc-00-11-22) is stored
    normalised; anything else is kept as a lower-cased hostname or IP token.
    A trailing '.local' is stripped so 'slzb-06.local' and 'slzb-06' both
    match. Blank or junk input yields an empty set — never raises.
    """
    tokens = set()
    for tok in (raw or "").replace(",", " ").split():
        bare = tok.replace(":", "").replace("-", "")
        if len(bare) == 12 and all(c in "0123456789abcdefABCDEF" for c in bare):
            tokens.add(bare.upper())
        else:
            host = tok.lower().rstrip(".")
            if host.endswith(".local"):
                host = host[: -len(".local")]
            if host:
                tokens.add(host)
    return tokens


def is_valid_state_id(key):
    """Indigo state IDs: ASCII alphanumeric only, must start with a letter."""
    if not key or not key[0].isascii() or not key[0].isalpha():
        return False
    return all(c.isascii() and c.isalnum() for c in key)


# State IDs a dynamic entity state must never take. Two groups:
#   * declared — every state declared in Devices.xml across our device types.
#     A firmware entity called "Status" would otherwise land on top of the
#     connection status the plugin writes.
#   * native — Indigo's own reserved property names. Writes to these are
#     silently routed to the native property and never appear as custom states.
# The four node-info states (ipAddress, macAddress, boardModel, esphomeVersion)
# are deliberately NOT reserved — a firmware entity of the same name is welcome
# to own them, and _write_node_info_states stands aside when one does.
DECLARED_STATE_IDS = {
    "connected", "status", "lastSeen", "colorTemp", "oscillating", "direction",
    "currentOperation", "action", "preset", "lockState",
}

RESERVED_NATIVE_STATE_IDS = {
    "batteryLevel", "onOffState", "sensorValue", "brightnessLevel",
    "hvacOperationMode", "temperatureInput1", "setpointHeat", "setpointCool",
    "redLevel", "greenLevel", "blueLevel", "whiteLevel",
}

# Preferred substitutes for reserved names, so the obvious sensor still lands
# somewhere sensible rather than being suffixed into "batteryLevel2".
RESERVED_STATE_ID_REMAP = {
    "batteryLevel": "battery",
    "sensorValue":  "reading",
    "onOffState":   "onOff",
}


# ESPHome sensor device_classes that mark a node as a METER rather than a
# controllable device. When a node carries any of these AND its only "control"
# is a status LED, the node is classified as an esphomeSensor — not a light.
# (Fixes relay-less power-monitor plugs being mistaken for dimmers.)
METERING_DEVICE_CLASSES = {
    "power", "energy", "apparent_power", "reactive_power",
    "current", "voltage", "power_factor", "frequency",
}

# device_class priority for choosing a sensor node's HEADLINE value — the one
# routed to the native Indigo sensorValue. First match wins.
HEADLINE_DEVICE_CLASS_PRIORITY = [
    "power", "energy", "temperature", "humidity", "illuminance",
    "pressure", "carbon_dioxide", "voltage", "current",
    "battery", "signal_strength",
]


def _entity_category_int(entity):
    """ESPHome EntityCategory as an int: 0=NONE, 1=CONFIG, 2=DIAGNOSTIC."""
    try:
        return int(getattr(entity, "entity_category", 0) or 0)
    except (TypeError, ValueError):
        return 0


def node_has_metering(entities):
    """True if the node exposes a power/energy-style sensor — i.e. it's a meter."""
    from aioesphomeapi import SensorInfo
    for e in entities:
        if isinstance(e, SensorInfo) and \
           (getattr(e, "device_class", "") or "").lower() in METERING_DEVICE_CLASSES:
            return True
    return False


def light_is_status_only(light, entities):
    """A Light should not be a node's PRIMARY device type when it's plainly a
    status indicator rather than a controllable lamp:
      - it's flagged CONFIG/DIAGNOSTIC, or
      - the node is a power meter (power/energy sensors present) — then the
        light is the plug's status LED and the node's purpose is metering.
    Genuinely metered smart lamps (rare) are handled by a manual override."""
    if _entity_category_int(light) != 0:
        return True
    return node_has_metering(entities)


def pick_headline_sensor(entities):
    """Choose the sensor whose value becomes a sensor-node's native sensorValue.
    Prefers a meaningful device_class (power, temperature, …), skips diagnostic
    sensors, then falls back to the first sensor with a unit, then the first
    sensor. Returns the SensorInfo, or None if the node has no numeric sensor."""
    from aioesphomeapi import SensorInfo
    all_sensors = [e for e in entities if isinstance(e, SensorInfo)]
    if not all_sensors:
        return None
    sensors = [e for e in all_sensors if _entity_category_int(e) == 0] or all_sensors
    by_class = {}
    for e in sensors:
        dc = (getattr(e, "device_class", "") or "").lower()
        by_class.setdefault(dc, e)
    for dc in HEADLINE_DEVICE_CLASS_PRIORITY:
        if dc in by_class:
            return by_class[dc]
    for e in sensors:
        if getattr(e, "unit_of_measurement", "") or "":
            return e
    return sensors[0]


# ============================================================
# Plugin
# ============================================================

class Plugin(indigo.PluginBase):

    def __init__(self, pluginId, pluginDisplayName, pluginVersion, pluginPrefs):
        super().__init__(pluginId, pluginDisplayName, pluginVersion, pluginPrefs)

        self.timestamp_enabled = as_bool(pluginPrefs.get("timestampEnabled", True), True)
        if install_timestamp_filter:
            self._ts_filter = install_timestamp_filter(self, enabled=self.timestamp_enabled)
        else:
            self._ts_filter = None

        self._apply_log_level(pluginPrefs.get("logLevel", "INFO"))

        # Discovery cache: mac -> {hostname, ip, port, name, first_seen}
        self.discovered = {}

        # Per-device connection state: mac -> {client, task, info, entities:{key:info}}
        self.connections = {}

        # Indigo device cache: mac -> indigo.Device for the node device
        self.nodes_by_mac = {}

        # Event triggers
        self.event_triggers = {}

        # asyncio loop + thread (set in startup)
        self.async_loop = None
        self.async_thread = None
        self.async_started = threading.Event()

        # Strong references to the per-device connection tasks. asyncio only
        # holds a weak reference, so a task nobody keeps can be collected
        # mid-flight and the connection silently disappears.
        self._connect_tasks = set()

        # Nodes we've stopped retrying: mac -> {"reason", "since", "failures"}
        self.parked = {}

        # Config
        self.auto_create_nodes = as_bool(pluginPrefs.get("autoCreateDevices", True), True)
        self.default_encryption_key = self._resolve_default_key(pluginPrefs)

        # Never probe these: MACs / hostnames / IPs from the Configure dialog.
        # For hardware that advertises _esphomelib._tcp but isn't an ESPHome
        # node (the SMLIGHT case in the adaptive-tier comment above).
        self.ignored_tokens = parse_ignore_list(pluginPrefs.get("ignoredDevices", ""))
        self._ignore_conflict_warned = set()   # one warning per conflicting MAC

        # Startup banner moved to showPluginInfo on demand (revised 25-May-2026 per Jay).

    def _resolve_default_key(self, prefs):
        """Default API encryption key: IndigoSecrets.py first, PluginConfig second."""
        key = (ESPHOME_DEFAULT_ENCRYPTION_KEY or "").strip()
        if key:
            return key
        return (prefs.get("defaultEncryptionKey", "") or "").strip()

    def _apply_log_level(self, level_name):
        """Apply the Configure dialog's Log Level to the Indigo log handler.

        Without this the setting was stored and ignored — debug lines never
        reached the event log whatever the user picked.
        """
        level = _lvl(level_name or "INFO")
        handler = getattr(self, "indigo_log_handler", None)
        if handler is not None:
            try:
                handler.setLevel(level)
            except Exception:
                pass
        logger = getattr(self, "logger", None)
        if logger is not None:
            try:
                logger.setLevel(min(level, logging.INFO))
            except Exception:
                pass

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
        if as_bool(self.pluginPrefs.get("migrated_v040", False)):
            return
        saved_keys = {}
        ids_to_delete = []
        for dev in indigo.devices.iter(self.pluginId):
            # Preserve encryption keys keyed on MAC (the node device's address)
            if dev.deviceTypeId == "esphomeNode":
                key = (dev.pluginProps.get("encryptionKey", "") or "").strip()
                if key:
                    saved_keys[dev.address] = key
            if self._is_legacy_device(dev):
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
        if deleted:
            self.logger.warning(
                f"=== v0.4.0 migration: deleted {deleted} legacy device(s); "
                f"preserved {len(saved_keys)} encryption key(s). "
                "New one-per-node devices will be created on next mDNS discovery. ==="
            )

    # Device types only the pre-v0.4.0 one-device-per-entity model ever created.
    _LEGACY_ONLY_TYPES = {
        "esphomeBinarySensor", "esphomeNumber", "esphomeSelect", "esphomeText",
    }

    def _is_legacy_device(self, dev):
        """True only for a device built by the pre-v0.4.0 one-per-entity model.

        The migration flag lives in pluginPrefs, and preferences are only
        flushed to disk on a graceful shutdown — so a crash could lose it and
        run the migration a second time. Deleting only genuinely legacy devices
        makes that replay harmless instead of wiping a working device set.
        """
        if dev.deviceTypeId in self._LEGACY_ONLY_TYPES:
            return True
        # Legacy entity devices used a compound "<mac>_<entitykey>" address;
        # the v0.4.0 model addresses a node by its bare MAC.
        return "_" in (dev.address or "")

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
        "esphomeNode", "esphomeSensor", "esphomeSwitch", "esphomeLight",
        "esphomeFan", "esphomeCover", "esphomeClimate", "esphomeLock",
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

    def _allocate_state_id(self, base, used_ids, needs_level=False):
        """Pick a free state ID for an entity.

        Reserved native names get a sensible substitute first (a "Battery Level"
        sensor becomes `battery`, never the reserved `batteryLevel`), then any
        remaining clash is settled with a numeric suffix.
        """
        candidate = base
        if candidate in RESERVED_NATIVE_STATE_IDS:
            remapped = RESERVED_STATE_ID_REMAP.get(candidate)
            if remapped and remapped not in used_ids and \
               not (needs_level and remapped + "Level" in used_ids):
                return remapped
        n = 2
        while candidate in used_ids or (needs_level and candidate + "Level" in used_ids):
            candidate = f"{base}{n}"
            n += 1
        return candidate

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
        """Pick the Indigo deviceTypeId for a node from its entities.

        Returns (type_id, primary_entity_or_None).

        A genuine control wins by priority (Lock > Climate > Switch > Light >
        Fan > Cover) — EXCEPT a status-LED Light is demoted (see
        light_is_status_only) so a relay-less power-monitor plug isn't taken
        for a dimmer. With no genuine control, a node with numeric sensors
        becomes an esphomeSensor (its headline value drives the native
        sensorValue); otherwise a plain info esphomeNode.
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
                    if cls is LightInfo and light_is_status_only(e, entities):
                        continue   # status LED — not the node's purpose
                    return type_id, e
        headline = pick_headline_sensor(entities)
        if headline is not None:
            return "esphomeSensor", headline
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
        # Seed the taken set with everything a dynamic state must not shadow:
        # the states declared in Devices.xml and Indigo's reserved native
        # property names.
        used_ids = set(DECLARED_STATE_IDS) | set(RESERVED_NATIVE_STATE_IDS)
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
            # A secondary light / fan / cover also claims "<id>Level", so that
            # name has to be taken at the same time or a later "Foo Level"
            # sensor would quietly overwrite it.
            needs_level = kind in ("light", "fan", "cover")
            state_id = self._allocate_state_id(base, used_ids, needs_level)
            used_ids.add(state_id)
            if needs_level:
                used_ids.add(state_id + "Level")
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
        """Main async entry point. Starts mDNS browsing and runs forever.

        The browser start is retried with back-off: zeroconf can fail on a
        network that isn't up yet, and a single failure used to leave the
        plugin alive but permanently blind.
        """
        backoff = RECONNECT_BACKOFF_INITIAL
        try:
            while True:
                try:
                    await self._start_mdns_browser()
                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.warning(
                        f"mDNS browser failed to start ({exc}); retrying in {backoff}s"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, RECONNECT_BACKOFF_MAX)
            # Work happens via mDNS callbacks and per-device connection tasks
            # spawned by them; the loop itself only sweeps parked nodes.
            while True:
                await asyncio.sleep(60)
                try:
                    self._sweep_parked()
                except Exception:
                    self.logger.exception("parked-node sweep failed")
        except asyncio.CancelledError:
            return
        except Exception:
            self.logger.exception("async main loop failed")

    def _spawn_task(self, coro, label):
        """Create a task, keep a strong reference, and log any exception.

        asyncio only holds a weak reference to a running task, so one nobody
        keeps can be collected mid-flight. An unretrieved exception is also
        invisible — this logs it.
        """
        task = asyncio.create_task(coro)
        self._connect_tasks.add(task)

        def _done(t):
            self._connect_tasks.discard(t)
            if t.cancelled():
                return
            exc = t.exception()
            if exc is not None:
                self.logger.error(f"{label} task failed: {exc}")

        task.add_done_callback(_done)
        return task

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
        """Wrapper so one bad advertisement can't take discovery down silently."""
        try:
            await self._handle_mdns_added_inner(zeroconf, service_type, name)
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(f"mDNS handler failed for {name}")

    async def _handle_mdns_added_inner(self, zeroconf, service_type, name):
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

        if self._is_ignored(mac, hostname=hostname, ip=ip):
            # On the Configure dialog's ignore list: keep the discovery details
            # so List Seen Devices can show it, but never probe, park or warn.
            self.parked.pop(mac, None)
            if is_new:
                self.logger.debug(
                    f"{mac} ({hostname} at {ip}): on the ignore list; not connecting"
                )
            node = self._find_node_device(mac)
            if node is not None and mac not in self._ignore_conflict_warned:
                self._ignore_conflict_warned.add(mac)
                self.logger.warning(
                    f"{mac} is on the ignore list but Indigo device '{node.name}' "
                    "exists for it — the ignore list wins and the device will stay "
                    "disconnected. Remove one or the other to clear this warning."
                )
            return

        if is_new:
            self.logger.info(
                f"Discovered ESPHome device {mac}: {hostname} at {ip}:{port} "
                f"(esphome {props.get('version','?')}, board {props.get('board','?')})"
            )
            self._fire_event("newDeviceDiscovered", mac)

        # Auto-connect (creates the Indigo node device too if auto-create is on)
        if self._should_connect(mac):
            self._spawn_task(self._connect_to_device(mac), f"connect {mac}")

    def _is_ignored(self, mac, hostname="", ip=""):
        """Is this node on the Configure dialog's ignore list?

        Matches by MAC, hostname (with or without .local) or IP. When the
        caller has no hostname/ip to hand, the discovery cache fills them in.
        """
        if not self.ignored_tokens:
            return False
        d = self.discovered.get(mac, {})
        hostname = (hostname or d.get("hostname", "")).lower().rstrip(".")
        if hostname.endswith(".local"):
            hostname = hostname[: -len(".local")]
        ip = ip or d.get("ip", "")
        return (mac in self.ignored_tokens
                or (hostname != "" and hostname in self.ignored_tokens)
                or (ip != "" and ip in self.ignored_tokens))

    def _should_connect(self, mac):
        """Should an mDNS announcement start a connection attempt?

        No if the node is on the ignore list, no if a connection is already
        running, and no if the node is parked: mDNS re-advertises every few
        minutes, so connecting on every announcement would undo the back-off
        completely and bring the warning storm back.
        """
        if self._is_ignored(mac):
            return False
        if mac in self.connections:
            return False
        if mac in self.parked:
            self.logger.debug(f"{mac}: re-advertised but parked; leaving it alone")
            return False
        return True

    # --------------------------------------------------------
    # Per-device connection
    # --------------------------------------------------------

    def _release_connection(self, mac):
        """Drop the client reference for a connection we've stopped running.

        Leaving a disconnected client in place made actions look sendable when
        nothing could reach the device.
        """
        conn = self.connections.get(mac)
        if conn is not None:
            conn["client"] = None

    def _park_connection(self, mac, reason, failures):
        """Stop retrying a node, and record why so it can be picked up later."""
        self._release_connection(mac)
        self.connections.pop(mac, None)
        self.parked[mac] = {
            "reason":   reason,
            "since":    time.time(),
            "failures": failures,
        }

    def _unpark(self, mac, why):
        """Retry a parked node now. Safe to call from the asyncio thread only."""
        if mac not in self.parked:
            return False
        if mac in self.connections:
            return False
        if mac not in self.discovered:
            return False
        self.parked.pop(mac, None)
        self.logger.info(f"{mac}: retrying connection ({why})")
        self._spawn_task(self._connect_to_device(mac), f"connect {mac}")
        return True

    def request_retry(self, mac, why):
        """Ask the asyncio thread to retry a parked node. Callable from any thread."""
        loop = self.async_loop
        if loop is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(self._unpark, mac, why)

    def _sweep_parked(self):
        """Periodic recovery: a parked node gets one more go once it has been
        quiet for PARKED_RETRY_AFTER, or straight away once the user adds a
        matching Indigo device."""
        now = time.time()
        for mac, entry in list(self.parked.items()):
            if self._is_ignored(mac):
                # Newly ignored via the Configure dialog — drop it for good.
                self.parked.pop(mac, None)
                continue
            if self._find_node_device(mac) is not None:
                self._unpark(mac, "an Indigo device now exists for it")
            elif now - entry.get("since", 0) >= PARKED_RETRY_AFTER:
                self._unpark(mac, "back-off elapsed")

    @staticmethod
    def _client_is_connected(client):
        """Is this aioesphomeapi client still connected?

        `_connection` is a private attribute, so a library upgrade could remove
        it. When it can't be read we assume the connection is up rather than
        spinning through a reconnect every ten seconds — a genuine drop still
        surfaces as an exception on the next call.
        """
        try:
            conn = client._connection
        except AttributeError:
            return True
        if conn is None:
            return False
        return bool(getattr(conn, "is_connected", True))

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
        if self._is_ignored(mac):
            return

        self.connections[mac] = {"client": None, "info": None, "entities": {}}
        self.parked.pop(mac, None)
        backoff    = RECONNECT_BACKOFF_INITIAL
        was_online = False
        failures   = 0
        last_error = ""

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
                self._fire_event("deviceOnline", mac)

                # subscribe_states is SYNCHRONOUS in aioesphomeapi (takes a
                # callback, returns an unsubscribe callable). No await.
                def _state_callback(state):
                    self._on_entity_state(mac, state)
                unsubscribe = client.subscribe_states(_state_callback)

                backoff    = RECONNECT_BACKOFF_INITIAL
                failures   = 0
                was_online = True

                # Hold the connection open. We use a long sleep rather than
                # an event-driven wait because aioesphomeapi doesn't expose a
                # disconnect-waiter; reconnect happens via the exception path
                # when the TCP connection drops and the next state callback
                # raises, or when our outer code disconnects on shutdown.
                while self._client_is_connected(client):
                    await asyncio.sleep(10)
                self.logger.warning(f"{mac}: connection dropped")

            except InvalidAuthAPIError:
                self.logger.error(
                    f"{mac}: invalid API encryption key. Set the correct key in "
                    "the device's Configure dialog or in the plugin's default key. "
                    "Plugin will retry on next restart."
                )
                self._update_node_status(mac, connected=False, status="Bad key")
                self._release_connection(mac)
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
                    if node_dev:
                        # Configured Indigo device without a usable key — actionable.
                        self.logger.error(
                            f"{mac}: {exc}. No usable encryption key. Set one in the "
                            "device's Configure dialog and restart the plugin. "
                            "Plugin will not retry until then."
                        )
                        self._update_node_status(mac, connected=False, status="Needs encryption key")
                    else:
                        # Discovered on the network but not set up in Indigo — an
                        # expected state, not an error (see 'not configured is INFO
                        # not ERROR'). Log at INFO so the event log stays clean.
                        self.logger.info(
                            f"{mac} at {d['ip']}: encrypted ESPHome device not set up "
                            "in Indigo — add it and set its API encryption key to use it. "
                            "Ignoring until then."
                        )
                    self._release_connection(mac)
                    return
                last_error = str(exc)
            except asyncio.CancelledError:
                # The finally block below does the unsubscribe + disconnect;
                # repeating them here just raises CancelledError again.
                self.logger.debug(f"{mac}: connection task cancelled")
                self._release_connection(mac)
                return
            except Exception as exc:
                last_error = str(exc) or exc.__class__.__name__
                if failures == 0:
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

            # --- the connection is down; decide whether to try again ---
            if self._is_ignored(mac):
                # Added to the ignore list while this task was running.
                self.logger.info(f"{mac}: on the ignore list — stopping connection attempts")
                self._release_connection(mac)
                self.connections.pop(mac, None)
                return
            node = self._find_node_device(mac)
            if was_online:
                # A drop after a good session isn't evidence the node is bogus,
                # so the failure count starts again from scratch.
                was_online = False
                failures   = 0
                backoff    = RECONNECT_BACKOFF_INITIAL
                self._fire_event("deviceOffline", mac)
            failures += 1
            if node:
                try:
                    node.updateStateOnServer("connected", False)
                    node.updateStateOnServer("status", "Disconnected")
                except Exception:
                    pass

            # Adaptive logging: say it plainly once, then go quiet.
            if failures == 1:
                self.logger.warning(
                    f"{mac} at {d['ip']}: connection failed: {last_error}. "
                    f"Retrying quietly with back-off."
                )
            else:
                self.logger.debug(
                    f"{mac}: connection failed ({failures}): {last_error}; "
                    f"reconnect in {backoff}s"
                )

            limit = MAX_CONNECT_FAILURES_ADOPTED if node else MAX_CONNECT_FAILURES_UNADOPTED
            if failures >= limit:
                where = "this Indigo device" if node else \
                        "no matching Indigo device — it may not be an ESPHome node at all"
                self.logger.warning(
                    f"{mac} at {d['ip']}: gave up after {failures} failed connections "
                    f"({last_error}); {where}. No further attempts for "
                    f"{PARKED_RETRY_AFTER // 60} minutes. Adding it as an Indigo device "
                    "retries straight away."
                )
                self._park_connection(mac, last_error, failures)
                return

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
            TextSensorState, FanState, CoverState, LockEntityState, NumberState, SelectState,
        )
        if state_id == "primary":
            # A sensor node routes its headline reading into the native
            # sensorValue (with unit in the .ui). Every other node type
            # delegates to the legacy handler, whose writes target the right
            # native states (onOffState, brightnessLevel, hvacOperationMode…).
            if dev.deviceTypeId == "esphomeSensor":
                self._apply_sensor_headline(dev, state, info)
                return
            self._apply_state_to_device(dev, state)
            return

        # Secondary entity — write to the dynamic state.
        if isinstance(state, SensorState):
            if getattr(state, "missing_state", False):
                return
            try:
                raw = float(state.state)
                unit = info.get("unit", "")
                if not math.isfinite(raw):
                    # ESPHome reports an unavailable reading as NaN. Writing that
                    # into a Number state poisons the state, the SQL Logger row
                    # and anything that compares against it — drop it instead and
                    # leave the last good value in place.
                    self.logger.debug(
                        f"{dev.name}: ignoring non-finite reading for '{state_id}'"
                    )
                    return
                if unit == "s":
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
                num = float(state.state)
            except (TypeError, ValueError):
                return
            if not math.isfinite(num):
                return
            dev.updateStateOnServer(state_id, num)
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

    def _apply_sensor_headline(self, dev, state, info):
        """Write a sensor node's primary (headline) reading into the native
        Indigo sensorValue, rounded, with the unit shown in the .ui suffix."""
        from aioesphomeapi import SensorState, TextSensorState
        if isinstance(state, SensorState):
            if getattr(state, "missing_state", False):
                return
            try:
                raw = float(state.state)
            except (TypeError, ValueError):
                return
            unit = (info or {}).get("unit", "")
            if not math.isfinite(raw):
                # See _apply_v040_state — a NaN never reaches sensorValue.
                self.logger.debug(f"{dev.name}: ignoring non-finite headline reading")
                return
            if unit == "s":
                val = int(round(raw))
                ui  = self._format_seconds(val)
            else:
                val = round(raw, 2)
                ui  = f"{val:.2f} {unit}".rstrip() if unit else f"{val:.2f}"
            try:
                dev.updateStateOnServer("sensorValue", val, uiValue=ui)
            except Exception as exc:
                self.logger.debug(f"sensorValue write failed on {dev.name}: {exc}")
        elif isinstance(state, TextSensorState):
            # A text headline can't be a numeric sensorValue — surface it in the .ui.
            if getattr(state, "missing_state", False):
                return
            try:
                dev.updateStateOnServer("sensorValue", 0, uiValue=str(state.state))
            except Exception:
                pass

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
            except (TypeError, ValueError):
                dev.updateStateOnServer("valueText", str(state.state))
                return
            if math.isfinite(val):
                dev.updateStateOnServer("value", val)
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
                num = float(state.state)
            except (TypeError, ValueError):
                return
            if math.isfinite(num):
                dev.updateStateOnServer("value", num)

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
        cached = self.nodes_by_mac.get(mac)
        if cached is not None:
            # Re-fetch: an Indigo device object is a snapshot, and the cached
            # one goes stale the moment the user saves the Configure dialog —
            # which is exactly where the encryption key gets changed.
            try:
                fresh = indigo.devices[cached.id]
            except Exception:
                self.nodes_by_mac.pop(mac, None)
                return None
            self.nodes_by_mac[mac] = fresh
            return fresh
        for d in indigo.devices.iter("self"):
            if d.address == mac and d.deviceTypeId in self._OUR_DEVICE_TYPES:
                self.nodes_by_mac[mac] = d
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
        if type_id == "esphomeSensor":
            # Sensor-class node: the native sensorValue must exist, so set
            # SupportsSensorValue in props at creation (hidden XML defaults are
            # NOT applied by indigo.device.create — reserved-state gotcha).
            return {
                "SupportsSensorValue": True,
                "SupportsOnState":     False,
                "unit": getattr(primary_entity, "unit_of_measurement", "") or "",
            }
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

    def actionControlSensor(self, action, dev):
        """Sensor-class devices (esphomeSensor) get their own action callback.

        Declaring type="sensor" obliges the plugin to implement this — without
        it Indigo logs 'plugin does not define method actionControlSensor' and
        drops the action. Readings arrive over the subscription, so a status
        request only has to confirm the node is connected.
        """
        sa = getattr(action, "sensorAction", None)
        if sa == indigo.kSensorAction.RequestStatus:
            conn = self.connections.get(dev.address, {})
            if conn.get("client") is not None:
                self.logger.info(
                    f"{dev.name}: connected — readings stream in as the device sends them."
                )
            else:
                self.logger.warning(f"{dev.name}: not connected to {dev.address}")
            return
        self.logger.debug(f"Unhandled sensor action {sa} on {dev.name}")

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
        """List every node seen via mDNS, with what the plugin made of each.

        Tags: CONNECTED (talking to us), ADOPTED (has an Indigo device),
        DISCOVERED (seen, no Indigo device), PARKED (stopped retrying — most
        often something that isn't an ESPHome node but shares the mDNS
        service type, such as a SMLIGHT Zigbee coordinator), IGNORED (on the
        Configure dialog's ignore list — never probed).
        """
        if not self.discovered:
            indigo.server.log("No ESPHome devices discovered yet")
            return
        for mac, d in sorted(self.discovered.items()):
            if self._is_ignored(mac):
                tag = "[IGNORED]"
            elif mac in self.parked:
                tag = "[PARKED]"
            elif mac in self.connections and self.connections[mac].get("info"):
                tag = "[CONNECTED]"
            elif self._find_node_device(mac) is not None:
                tag = "[ADOPTED]"
            else:
                tag = "[DISCOVERED]"
            indigo.server.log(
                f"  {tag:<13} {mac}  {d['hostname']:<25} {d['ip']:<16} "
                f"esphome {d.get('version','?')} board={d.get('board','?')}"
            )
        for mac, entry in sorted(self.parked.items()):
            mins = int((time.time() - entry.get("since", 0)) // 60)
            indigo.server.log(
                f"  PARKED {mac} for {mins} min after {entry.get('failures', 0)} "
                f"failed connections: {entry.get('reason', 'unknown')}"
            )
        if self.parked:
            indigo.server.log(
                "  A parked node is retried automatically once the back-off "
                "elapses, or straight away if you add it as an Indigo device."
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

    # --- OTA firmware upload (menu) ---

    def getOurDevicesForMenu(self, filter, valuesDict, typeId, targetId):
        """List callback for the OTA-upload menu's Target Device dropdown.
        Includes BOTH adopted Indigo devices AND mDNS-discovered nodes
        that haven't been adopted yet (e.g. encryption-locked nodes that
        still need the new firmware). The picker value is the IP so the
        upload only needs that, not a device lookup."""
        items = []
        seen_ips = set()
        # Indigo devices first
        for dev in indigo.devices.iter(self.pluginId):
            if dev.deviceTypeId not in self._OUR_DEVICE_TYPES:
                continue
            ip = dev.pluginProps.get("ip", "")
            if not ip:
                continue
            label = f"{dev.name} ({ip})"
            items.append((ip, label))
            seen_ips.add(ip)
        # Then any mDNS-discovered nodes not yet adopted (e.g. those
        # locked out by an unknown encryption key)
        for mac, d in self.discovered.items():
            ip = d.get("ip", "")
            if not ip or ip in seen_ips:
                continue
            host = d.get("hostname", mac)
            items.append((ip, f"{host} ({ip}) — discovered, not adopted"))
            seen_ips.add(ip)
        items.sort(key=lambda x: x[1].lower())
        return items

    def menuOtaUpload(self, valuesDict, typeId):
        """Submit handler for the OTA-upload menu. Validates the firmware
        path + IP, then kicks off the actual upload in a background
        thread (the upload takes ~5-30s depending on size and won't fit
        inside Indigo's ~30s UI-callback budget).
        """
        errors = indigo.Dict()
        ip   = (valuesDict.get("targetDeviceId") or "").strip()
        path = (valuesDict.get("firmwarePath") or "").strip()
        # Strip quotes that Finder's Copy-as-Pathname adds
        path = path.strip('"').strip("'")
        if not ip:
            errors["targetDeviceId"] = "Pick a target device."
        if not path:
            errors["firmwarePath"] = "Provide a path to the firmware .bin file."
        elif not os.path.isfile(path):
            errors["firmwarePath"] = f"File not found: {path}"
        elif os.path.getsize(path) < 100_000:
            errors["firmwarePath"] = (
                f"File looks too small ({os.path.getsize(path)} bytes). "
                "ESPHome firmware .bin files are typically 600 KB-1.5 MB."
            )
        if errors:
            return (False, valuesDict, errors)
        # Find a friendly name + dev_id for logging (best-effort)
        dev_name = ip
        dev_id   = None
        for dev in indigo.devices.iter(self.pluginId):
            if dev.pluginProps.get("ip") == ip:
                dev_name = dev.name
                dev_id   = dev.id
                break
        # Fire and forget — worker reports progress + outcome to the log
        threading.Thread(
            target=self._ota_upload_worker,
            args=(dev_id, dev_name, ip, path),
            name=f"OTA-{ip}",
            daemon=True,
        ).start()
        self.logger.info(
            f"OTA upload started: {dev_name} <- {os.path.basename(path)} "
            f"({os.path.getsize(path):,} bytes). Watch this log for progress."
        )
        return (True, valuesDict)

    def _ota_upload_worker(self, dev_id, dev_name, ip, path):
        """Background worker: POST the .bin to the device's /update endpoint.

        The ESPHome web_server v3 OTA endpoint is multipart/form-data:
          - file:        the firmware bytes (field name 'update' or 'file')
          - Response:    200 + 'OK' on success; on success the device
                         reboots itself ~1-2s after the response.

        We disconnect the API client before uploading so the ESP doesn't
        kill our connection mid-flight — but we let it reconnect via the
        existing reconnect loop after the device comes back up.
        """
        import requests
        url = f"http://{ip}/update"
        size = os.path.getsize(path)
        self.logger.info(f"OTA {dev_name}: POST {url} ({size:,} bytes)...")
        # Best-effort: stop the API client so it doesn't fight the upload.
        # The reconnect loop will pick it back up automatically once the
        # device finishes flashing and reboots.
        mac = ""
        if dev_id is not None:
            for m, d in self.nodes_by_mac.items():
                if d.id == dev_id:
                    mac = m
                    break
        if mac and mac in self.connections:
            client = self.connections[mac].get("client")
            if client:
                try:
                    self.async_loop.call_soon_threadsafe(
                        lambda: asyncio.create_task(client.disconnect())
                    )
                    time.sleep(1)
                except Exception as exc:
                    self.logger.debug(f"OTA {dev_name}: client disconnect raised: {exc}")
        # Upload — the ESPHome /update endpoint accepts the file under a
        # form field. Different ESPHome web_server versions have used
        # 'update' and 'file'; we just try the modern one.
        t0 = time.time()
        try:
            with open(path, "rb") as f:
                resp = requests.post(
                    url,
                    files={"file": (os.path.basename(path), f, "application/octet-stream")},
                    timeout=180,
                )
        except requests.exceptions.RequestException as exc:
            self.logger.error(f"OTA {dev_name}: upload failed: {exc}")
            return
        dt = time.time() - t0
        if 200 <= resp.status_code < 300:
            self.logger.info(
                f"OTA {dev_name}: upload complete in {dt:.1f}s "
                f"(HTTP {resp.status_code}). Device is rebooting; "
                "plugin will reconnect on next mDNS announcement."
            )
        else:
            self.logger.error(
                f"OTA {dev_name}: device returned HTTP {resp.status_code}: "
                f"{resp.text[:200]}"
            )

    def showPluginInfo(self, valuesDict=None, typeId=None):
        connected = sum(1 for c in self.connections.values() if c.get("info"))
        extras = [
            ("Discovered:",        str(len(self.discovered))),
            ("Connected:",         str(connected)),
            ("Indigo nodes:",      str(len(self.nodes_by_mac))),
            ("Parked nodes:",      str(len(self.parked))),
            ("Timestamps in Log:", "ON" if self.timestamp_enabled else "OFF"),
        ]
        if log_startup_banner:
            log_startup_banner(self.pluginId, self.pluginDisplayName, self.pluginVersion, extras=extras)
        else:
            indigo.server.log(f"{self.pluginDisplayName} v{self.pluginVersion}")
            for label, value in extras:
                indigo.server.log(f"  {label} {value}")

    def menuToggleTimestamps(self):
        self.timestamp_enabled = not self.timestamp_enabled
        self.pluginPrefs["timestampEnabled"] = self.timestamp_enabled
        if self._ts_filter:
            self._ts_filter.enabled = self.timestamp_enabled
        state = "ON" if self.timestamp_enabled else "OFF"
        indigo.server.log(f"[{self.pluginDisplayName}] Timestamps in Log -> {state}")

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
        # (one per ESPHome node).
        if dev.deviceTypeId in self._OUR_DEVICE_TYPES:
            self.nodes_by_mac[dev.address] = dev
            # Adding (or re-enabling) a device for a parked node is the clearest
            # possible signal that the user wants it connected — try again now
            # rather than making them restart the plugin.
            if dev.address in self.parked:
                self.request_retry(dev.address, "device added or re-enabled in Indigo")

    def deviceStopComm(self, dev):
        if dev.deviceTypeId in self._OUR_DEVICE_TYPES:
            self.nodes_by_mac.pop(dev.address, None)

    @staticmethod
    def didDeviceCommPropertyChange(oldDevice, newDevice):
        """Restart comm only when the ESPHome connection params change.

        `address` is the MAC (Indigo's device address — node identity);
        `hostname`/`ip`/`port` define where to connect; `encryptionKey` is
        required by the Native API. Other props (boardModel, esphomeVersion,
        deviceClass, speedLevels, visualMin/Max, supportedModes) are
        informational and don't justify a restart.
        """
        keys = ("address", "hostname", "ip", "port", "encryptionKey")
        return any(oldDevice.pluginProps.get(k) != newDevice.pluginProps.get(k) for k in keys)

    # --------------------------------------------------------
    # PluginPrefs
    # --------------------------------------------------------

    def closedPrefsConfigUi(self, valuesDict, userCancelled):
        if userCancelled:
            return
        self.default_encryption_key = self._resolve_default_key(valuesDict)
        self.auto_create_nodes      = as_bool(valuesDict.get("autoCreateDevices", True), True)
        self._apply_log_level(valuesDict.get("logLevel", "INFO"))

        old_tokens = self.ignored_tokens
        self.ignored_tokens = parse_ignore_list(valuesDict.get("ignoredDevices", ""))
        if self.ignored_tokens != old_tokens:
            self._ignore_conflict_warned.clear()
            for mac, d in sorted(self.discovered.items()):
                if self._is_ignored(mac):
                    self.logger.info(
                        f"{mac} ({d.get('hostname', '?')} at {d.get('ip', '?')}): "
                        "on the ignore list; no further connection attempts"
                    )
            loop = self.async_loop
            if loop is not None and loop.is_running():
                loop.call_soon_threadsafe(self._apply_ignore_list_now)

    def _apply_ignore_list_now(self):
        """Runs on the asyncio loop after a prefs change: stop anything that is
        now ignored straight away, rather than waiting for the next parked
        sweep or back-off tick."""
        for mac in list(self.parked):
            if self._is_ignored(mac):
                self.parked.pop(mac, None)
        for mac, conn in list(self.connections.items()):
            if not self._is_ignored(mac):
                continue
            client = conn.get("client")
            if client is not None:
                # Drop the transport; the connect task's post-disconnect
                # ignore check then ends its retry loop.
                self._spawn_task(client.disconnect(), f"disconnect {mac}")
