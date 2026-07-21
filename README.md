# ESPHome Bridge — Indigo Plugin

Bridges [ESPHome](https://esphome.io/) devices into [Indigo Domotics](https://www.indigodomo.com/)
2025.2+ as native device types via ESPHome's **Native API** (port 6053).

Auto-discovers ESPHome devices on your LAN via mDNS, connects directly to each
device over TCP (no MQTT broker needed), and surfaces each ESPHome entity
(sensor, switch, light, fan, cover, button, ...) as a queryable Indigo device
with native controls.

**No cloud. No MQTT broker required. Purely local.**

---

## Table of Contents

- [Status](#status)
- [Why Native API instead of MQTT](#why-native-api-instead-of-mqtt)
- [Quick Start](#quick-start)
- [Installation](#installation)
- [Plugin Configuration](#plugin-configuration)
- [Setting Up ESPHome Devices](#setting-up-esphome-devices)
- [Supported Entity Types](#supported-entity-types)
- [Plugin Menu Items](#plugin-menu-items)
- [Custom Events (Triggers)](#custom-events-triggers)
- [Architecture Overview](#architecture-overview)
- [Troubleshooting](#troubleshooting)
- [FAQ](#faq)
- [Beta Tester Checklist](#beta-tester-checklist)
- [Companion Plugins](#companion-plugins)
- [Contributing](#contributing)
- [License](#license)

---

## Status

**Public beta (v0.2.x).** Validated end-to-end on Athom plugs and a
custom ESP32 test rig running ESPHome 2026.4.x. Switch, dimmer, fan
(with variable speed), cover (with position) and sensor entity types
all confirmed working round-trip. RGB lights, climate, lock, and BLE
proxy support exist in code but are not yet validated against real
hardware. Report issues at the
[GitHub repo](https://github.com/Highsteads/ESPHomeBridge/issues).

---

## Why Native API instead of MQTT

ESPHome supports two integration paths:

| | Native API | MQTT |
|---|---|---|
| Required device config | `api:` (default in every YAML) | `mqtt:` block must be added |
| Discovery | mDNS, automatic | requires HA-style discovery topic config |
| Transport | TCP, per-device, port 6053 | Pub/sub on a shared broker |
| Audience | All ESPHome users by default | Users who configure MQTT explicitly |
| What Home Assistant uses | Yes | Rarely |

This plugin uses the **Native API**. That means it works against any
ESPHome device in its default configuration — you don't need to add
MQTT broker config to every YAML. It also means no broker is required
on your network at all (although MQTT-based plugins like
[Zigbee2MQTTBridge](https://github.com/Highsteads/Zigbee2MQTTBridge)
or [TasmotaBridge](https://github.com/Highsteads/TasmotaBridge) can
coexist happily — they share no infrastructure with this plugin).

---

## Quick Start

1. **Install the plugin** — download `ESPHomeBridge.indigoPlugin.zip`
   from the [Releases](https://github.com/Highsteads/ESPHomeBridge/releases)
   page, unzip, double-click. Indigo installs it automatically.
2. **(optional) Configure default encryption key** — `Plugins → ESPHome
   Bridge → Configure...`, paste the API encryption key from your YAML's
   `api: encryption: key:` line if you use one. Leave blank for
   unencrypted devices.
3. **Power up your ESPHome devices** — mDNS discovery finds them within
   seconds. The plugin auto-creates one Indigo `esphomeNode` device per
   ESPHome board plus one Indigo device per controllable entity (switch,
   light, fan, cover, sensor, etc.).
4. **Drag devices into rooms** — once moved out of the `ESPHome` folder
   the plugin never touches their location again.

That's it. Native Indigo controls work for each entity type (Turn On /
Off / Brightness slider for dimmers and fans, Open / Close for covers,
etc.). Sensor telemetry flows in real time over the persistent TCP
connection.

---

## Installation

1. Go to the
   [Releases page](https://github.com/Highsteads/ESPHomeBridge/releases)
   and download the latest `ESPHomeBridge.indigoPlugin.zip`
2. Unzip the file — you'll get `ESPHomeBridge.indigoPlugin`
3. Double-click — Indigo installs it
4. Open `Plugins` menu — `ESPHome Bridge` submenu appears
5. (optional) Configure via `Plugins → ESPHome Bridge → Configure...`

The plugin auto-installs two Python dependencies (`aioesphomeapi` and
`zeroconf`) on first launch — Indigo runs `pip install -r
requirements.txt` into the plugin's `Contents/Packages/` directory.
This takes ~30 seconds on the first run; instant thereafter.

---

## Plugin Configuration

`Plugins → ESPHome Bridge → Configure...`

| Setting | Purpose |
|---|---|
| **Auto-create Indigo devices on discovery** | When a new ESPHome device is discovered, automatically create the matching Indigo node device. Default on. |
| **Auto-create individual entity devices** | If on, also auto-create one Indigo device per controllable entity (switch, light, etc.). If off, only the parent `esphomeNode` is created. Default on. |
| **Default API Encryption Key** | Base64-encoded key from your YAML's `api: encryption: key:` line. Used for any device that doesn't have its own key set in its device-config dialog. Leave blank for unencrypted devices. |
| **Log Level** | Standard Indigo log levels. |

Per-device encryption keys can override the default in each
`esphomeNode` device's Configure dialog (useful when different
ESPHome devices have different keys).

---

## Setting Up ESPHome Devices

For each device's YAML config:

```yaml
esphome:
  name: my-device

esp32:                # or esp8266: / rp2040: / nrf52: as appropriate
  board: esp32dev

api:
  encryption:
    key: "..."        # OPTIONAL - paste a 32-byte base64 key here
  reboot_timeout: 0s  # don't reboot on long Indigo-disconnects

ota:                  # optional but recommended
  - platform: esphome
    password: "..."

wifi:
  ssid: "..."
  password: "..."
  ap:                 # optional captive-portal fallback
    ssid: "MyDevice-AP"
    password: "..."

captive_portal:       # so first-boot WiFi setup works
```

Discovery happens via mDNS automatically — no MQTT broker, no manual
device addition. The first time the device boots and joins your WiFi,
the plugin sees its `_esphomelib._tcp` advertisement and connects.

### A test rig YAML is included in the repo

See [`test-fixtures/esp32-test-rig.yaml`](test-fixtures/esp32-test-rig.yaml)
for a comprehensive ESP32 config that exposes one of every supported
entity type. Useful as a reference or a beta-test target. The
[`test-fixtures/REFLASH_INSTRUCTIONS.md`](test-fixtures/REFLASH_INSTRUCTIONS.md)
walks through flashing it via either USB (esphome web flasher) or OTA.

---

## Supported Entity Types

The plugin auto-detects each ESPHome entity from `list_entities_services`
and creates the matching Indigo device type.

### `esphomeNode` — the ESPHome board itself

One per discovered device, regardless of entity count. Diagnostic
device showing:

- `connected` — Boolean, true while the TCP connection is up
- `status` — `Online` / `Disconnected`
- `rssi` — Wi-Fi signal in dBm
- `lastSeen` — last received message timestamp

Stores in `pluginProps`: MAC address, hostname, IP, port, board model
(e.g. `esp32dev`), firmware version, optional per-device encryption key.

### `esphomeSwitch` — switch entity

**ESPHome class:** `switch`
**Indigo class:** `relay`

Native `Turn On / Turn Off / Toggle` work out of the box. Useful for
relay outputs, GPIO-driven outlets, virtual template switches.

### `esphomeSensor` — numeric or text sensor

**ESPHome classes:** `sensor`, `text_sensor`
**Indigo class:** `sensor`

- `value` — float (for numeric sensors)
- `valueText` — string (for text sensors, or when the numeric value is
  unparseable as a float)
- `unit` — pluginProp showing the unit of measurement (V, A, W, °C, etc.)

### `esphomeBinarySensor` — boolean sensor

**ESPHome class:** `binary_sensor`
**Indigo class:** `sensor` with `SupportsOnState`

`onOffState` reflects the device class — `motion`, `door`, `window`,
`occupancy`, `garage_door`, etc. Use as a trigger source like any
Indigo motion sensor.

### `esphomeLight` — dimmer / CT / RGB light

**ESPHome class:** `light`
**Indigo class:** `dimmer`

Supports:
- Native brightness slider (0-100%)
- `Turn On / Off / Toggle`
- Colour-temperature control if the ESPHome light supports CT
- RGB control via Indigo's `Set Color Levels` action — gates writes to
  `redLevel/greenLevel/blueLevel` on `SupportsRGB=true`, so plain
  dimmers don't error
- HSB and colour mode states

ESPHome's `supported_color_modes` is decoded automatically:
- `lt_st >= 19` → RGB capable
- `11` or `27` → colour-temperature capable

### `esphomeFan` — variable-speed fan

**ESPHome class:** `fan`
**Indigo class:** `dimmer` (brightness 0-100 = fan speed percentage)

- The plugin scales 0-100% to ESPHome's `supported_speed_count` (e.g.
  1-5 for a 5-speed fan)
- Native brightness slider sets the fan speed
- `Turn On / Off` work as expected
- `oscillating` and `direction` states surfaced for fans that support
  them

### `esphomeCover` — blind / shutter / garage door

**ESPHome class:** `cover`
**Indigo class:** `dimmer` (brightness 0-100 = position; 0=closed,
100=open)

- Native brightness slider sets the position
- `Turn On` = fully open, `Turn Off` = fully closed
- `currentOperation` state shows `idle` / `opening` / `closing`
- Requires the device's YAML to declare `has_position: true` and a
  `position_action` lambda for position-aware covers; simple
  open/close-only covers also work (Turn On / Off only)

---

## Plugin Menu Items

Available under `Plugins → ESPHome Bridge`:

| Menu item | Purpose |
|---|---|
| **Discover ESPHome Devices Now** | Restart the mDNS browser. Any retained advertisements replay. |
| **List Discovered Devices** | Print a one-line summary of every discovered ESPHome device to the event log. |
| **Dump All Entities to Log** | For every connected device, print its full entity list (key, type, name, object_id). Verbose; for debugging. |
| **Show Plugin Info** | Re-print the startup banner with current device counts and connection status. |

---

## Custom Events (Triggers)

Available via `Triggers → New Trigger`:

### ESPHome Device Came Online

Fires when an ESPHome device's TCP connection is established.

**Filter:** MAC Address (blank = any device)

### ESPHome Device Went Offline

Fires when an ESPHome device's TCP connection drops.

**Filter:** MAC Address (blank = any device)

### New ESPHome Device Discovered

Fires when a previously-unseen MAC publishes an mDNS advertisement
for the first time.

---

## Architecture Overview

```
+----------------------+    Native API (TCP, port 6053)
| ESPHome devices      | ←----- protobuf, encrypted ------→  +-----------------+
| (on LAN, any IP)     |                                     | aioesphomeapi   |
|                      | <-- mDNS _esphomelib._tcp.local --→ | (asyncio event  |
+----------------------+                                     |  loop in own    |
                                                             |  thread)        |
                                                             +-----+-----------+
                                                                   |
                                                                   | direct
                                                                   | Indigo IOM
                                                                   v
                                                             +-----------------+
                                                             |  Indigo Server  |
                                                             |  - devices      |
                                                             |  - states       |
                                                             |  - triggers     |
                                                             +-----------------+
```

The plugin spawns a dedicated thread that runs an asyncio event loop.
That thread owns the aioesphomeapi client objects (one per discovered
device) and the zeroconf mDNS browser. Indigo's plugin core stays
synchronous — actions from the Indigo client dispatch
`call_soon_threadsafe` into the asyncio thread.

aioesphomeapi has a deliberate sync-vs-async split:

- **Async (await them):** `connect`, `disconnect`, `device_info`,
  `list_entities_services`
- **Sync (call directly, no await):** `subscribe_states`,
  `switch_command`, `light_command`, `fan_command`, `cover_command`

Mixing those up gives the famously confusing
`TypeError: object NoneType can't be used in 'await' expression`. The
plugin's command dispatchers always call the `*_command` methods
synchronously from within the asyncio thread.

Per-device connection lifecycle:

1. mDNS discovers `_esphomelib._tcp.local.<mac>`
2. Plugin opens an `APIClient`, calls `await client.connect(login=True)`
3. Fetches device info + entity list
4. Auto-creates Indigo devices (one node + one per entity)
5. Subscribes to state callbacks
6. Sleeps in a poll loop; reconnects with exponential backoff on
   disconnect (fresh `APIClient` each attempt — reusing a stale client
   causes silent "Already connected" loops)

---

## Troubleshooting

### A new ESPHome device hasn't appeared in Indigo

1. **Check it's on the LAN.** Connect to its IP from a browser —
   ESPHome's optional `web_server:` component (if you have it in the
   YAML) shows a status page. mDNS discovery requires the device to
   be on the same subnet as the Indigo Mac (or have mDNS reflection
   set up on your router).
2. **Check the encryption key.** If your YAML has `api: encryption:
   key:`, the same key must be either in the plugin's Default
   Encryption Key (PluginConfig) or in the device's Configure dialog
   in Indigo. Otherwise you'll see `Connection requires encryption`
   warnings repeating forever.
3. **Restart discovery.** `Plugins → ESPHome Bridge → Discover
   ESPHome Devices Now` re-issues the mDNS browse. Useful if a
   device joined the LAN after the plugin started.
4. **Watch the log.** `Plugins → ESPHome Bridge → Show Plugin Info`
   prints connection status counts.

### `Connection requires encryption` (repeating)

The device has API encryption enabled but the plugin doesn't have a
key for it. Either:
- Add the key to the plugin's Default Encryption Key field, OR
- Edit the `esphomeNode` device in Indigo and paste the key into the
  Configure dialog (overrides the default just for that device), OR
- If you've lost the key, reflash the device with a new YAML
  containing a known key. See
  [`test-fixtures/REFLASH_INSTRUCTIONS.md`](test-fixtures/REFLASH_INSTRUCTIONS.md)
  for the reflash workflow.

### `Already connected to <device>` looping

Old bug fixed in v0.1.0. If you see this on a current version,
something has caught a stale `APIClient` in the connections cache.
Restart the plugin (`Plugins → Reload`).

### Fan / cover commands seem to no-op

Check the ESPHome YAML on the device:
- **Fan**: needs `platform: speed` (not `template`) for the
  `speed_level` API field to be honoured.
- **Cover**: needs `has_position: true` AND a `position_action`
  lambda for positional commands. Without those, the cover is
  binary-only.

### `TypeError: object NoneType can't be used in 'await' expression`

Reported as a plugin bug — the plugin code is awaiting an
aioesphomeapi method that's actually sync. See the
[architecture section](#architecture-overview) for the canonical
list. Report the device entity type + action that triggered it.

---

## FAQ

**Q: Does this plugin require MQTT?**
A: No. Native API is direct TCP per device. No broker needed. You can
   still run MQTT-based plugins (Zigbee2MQTT, Tasmota, Shelly) on the
   same Indigo install — they share no infrastructure with this one.

**Q: How do I rename an ESPHome device?**
A: Edit the device in Indigo (the rename is purely cosmetic in Indigo
   and doesn't affect ESPHome). To change the name reported by the
   ESPHome device itself, edit the `esphome.name:` line in its YAML
   and re-flash.

**Q: Do I need to re-add devices to Indigo when I OTA-flash an
ESPHome device?**
A: No. The plugin keys devices by MAC. Firmware version and entity
   list refresh automatically when the device reconnects.

**Q: My device shows up but `Connected: no` in the node device's
states. Why?**
A: The plugin found the device via mDNS but couldn't open the API
   connection. Most common cause is missing/wrong encryption key. See
   [Troubleshooting](#troubleshooting).

**Q: Can I control entities the plugin doesn't know about?**
A: Not via the native Indigo controls. The plugin currently supports
   switch, sensor, text_sensor, binary_sensor, light, fan, and cover
   entities. Climate, lock, button (as press-trigger), number, and
   select are not yet implemented. Open an issue if you have hardware
   for an unsupported entity type.

**Q: I moved a device from `ESPHome` to a room folder. Will the
plugin move it back?**
A: No. The plugin only assigns folder at initial device creation.
   After that, the folder is your choice and the plugin never touches it.

**Q: Why does the plugin not have a "scan IP range" feature like
TasmotaBridge had briefly?**
A: ESPHome's mDNS discovery is reliable and automatic. There's no
   case where scanning IPs would find devices that mDNS doesn't.

---

## Beta Tester Checklist

If you have ESPHome hardware in any of the unvalidated categories,
here's what would help most:

1. **RGB / RGBW / RGBCW lights** — the colour state callback and
   `Set Color Levels` action are coded but I don't have an RGB
   device to test against. Confirm: brightness slider works, Set
   Color Levels sets the colour, state changes round-trip back to
   Indigo.

2. **Climate (HVAC) entities** — not yet implemented. If you have
   an ESPHome thermostat, file an issue with your YAML so we can
   prioritise.

3. **Lock entities** — not yet implemented. Same deal — file an
   issue.

4. **BLE proxy devices** — discovery works, connection works, but
   BLE-proxy-specific state surfaces (BLE devices the proxy sees)
   aren't yet exposed. Reports welcome.

5. **Devices with encryption keys** — confirm both the default-key
   PluginConfig path and the per-device override path work.

File reports at <https://github.com/Highsteads/ESPHomeBridge/issues>
with the device model, firmware version, and the `Dump All Entities
to Log` output.

---

## Companion Plugins

This plugin is part of a growing collection for Indigo users who want
to bring DIY firmware and budget Wi-Fi smart-home gear into Indigo
natively, with no cloud:

- [**TasmotaBridge**](https://github.com/Highsteads/TasmotaBridge) —
  Tasmota MQTT devices (Sonoff, Athom, ESP-based plugs/switches)
- [**Zigbee2MQTTBridge**](https://github.com/Highsteads/Zigbee2MQTTBridge)
  — Zigbee devices via Zigbee2MQTT
- [**ShellyDirect**](https://github.com/Highsteads/ShellyDirect) —
  Shelly Gen2/3 over local HTTP
- [**Ecowitt**](https://github.com/Highsteads/Ecowitt) — Ecowitt
  weather stations
- **ESPHomeBridge** *(this plugin)* — ESPHome devices via native API

All five work side-by-side; they share no infrastructure (well, the
MQTT-based ones share a Mosquitto broker if you have one — Mosquitto
runs natively on the SMLight Hub, the Indigo Mac, or anywhere else).

---

## Contributing

Pull requests welcome at
<https://github.com/Highsteads/ESPHomeBridge>.

Code conventions:
- Python 3.13 (Indigo 2025.2 embedded)
- 4-space indent, no tabs
- snake_case for vars/functions, PascalCase for classes,
  UPPER_SNAKE for constants
- `try` / `except` around `dev.updateStateOnServer` calls — Indigo
  can reject writes if the state isn't declared
- All aioesphomeapi command methods (`switch_command`,
  `light_command`, etc.) are sync, NOT async — never await them
- Use `self.async_loop.call_soon_threadsafe(func)` to dispatch sync
  command calls from an Indigo callback thread into the asyncio thread

When adding a new entity type, also update:
- `Contents/Server Plugin/Devices.xml` — define the device
- `Contents/Server Plugin/plugin.py` — add to `_ensure_entity_devices`,
  `_apply_state_to_device`, and `actionControlDevice`
- `README.md` — add a section under
  [Supported Entity Types](#supported-entity-types)
- The [Beta Tester Checklist](#beta-tester-checklist) if the entity
  class is untested

---

## Logging

Every log line carries a millisecond timestamp `[HH:MM:SS.mmm]`, so you can
line events up precisely against the other CliveS plugins — Device Activity
Monitor uses the same format.

To turn the prefix off, or back on, at any time:

**Plugins → ESPHome Bridge → Toggle Timestamps in Log (on/off)**

The plugin stores the setting in `pluginPrefs` (`timestampEnabled`) and it
survives a restart. It defaults to ON.

## Authors & licence

Vibed into existence by **CliveS**, who knew what he wanted, argued until he got it, and tested it on a real house. Typed at inhuman speed by **Claude** (Anthropic), who mostly did as it was told.

© 2026 CliveS · [MIT licence](LICENSE) — copy it, fork it, bend it, break it, fix it, ship it. If it breaks, you get to keep both pieces.
