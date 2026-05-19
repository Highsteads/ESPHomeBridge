# Reflashing the ESP32 BLE proxy for ESPHomeBridge testing

The BLE proxy at `192.168.100.242` (MAC `FCB4674EAEC8`) currently uses an
unrecoverable API encryption key from the decommissioned HA install. To
make it a fully testable target for ESPHomeBridge, reflash it with
`esp32-test-rig.yaml` (the file next to this one). That config exposes
every ESPHome entity type the plugin needs to handle: switch, dimmer,
RGB light, binary_sensor (motion + door), button, sensor (numeric +
uptime + WiFi), text_sensor, fan, and cover.

## Option A — ESPHome Web Flasher (easiest)

Requires the device physically reachable + a USB-C cable.

1. On the Mac, open <https://web.esphome.io/> in Chrome or Edge
   (Safari and Firefox don't support Web Serial.)

2. Plug the ESP32 into the Mac via USB.

3. On the page:
   - **Connect** → pick the USB serial device (looks like `cu.usbserial-XXX`
     or `cu.SLAB_USBtoUART`)
   - **Prepare for first use** → **Install ESPHome Web** (this puts the
     device into improv mode for WiFi setup)

4. Once back on the LAN, in another tab open the ESPHome dashboard:
   ```
   pip3 install --user esphome
   esphome dashboard ~/esphome-configs/
   ```
   Then browse to <http://localhost:6052>, **+ New Device**, paste in
   the `esp32-test-rig.yaml`, fill in WiFi creds, click **Install** →
   **Manual download**, and use the Web Flasher to push it.

## Option B — ESPHome CLI directly (no dashboard)

If you don't want the dashboard, you can compile + flash from the command
line:

```bash
pip3 install --user esphome
cd /Users/indigo/Documents/GitHub/ESPHomeBridge/test-fixtures
# Edit esp32-test-rig.yaml — set ssid: and password: for your WiFi
esphome compile esp32-test-rig.yaml
esphome upload esp32-test-rig.yaml --device /dev/cu.usbserial-XXX
```

The first flash needs USB. Subsequent updates can use OTA:

```bash
esphome upload esp32-test-rig.yaml --device 192.168.100.242
```

(OTA password is `indigotestrig` per the YAML.)

## Verifying the flash worked

After the device reboots and joins WiFi:

1. **In Indigo's event log** you should see:
   ```
   Discovered ESPHome device <MAC>: esphome-test-rig at 192.168.x.x:6053
     (esphome <ver>, board esp32dev)
   Connecting to <MAC> at ...
   Connected to <MAC>
   <MAC> (esphome-test-rig): N entities, esphome <ver>, model esp32dev
   ```
   No `Connection requires encryption` error this time — we disabled it.

2. **In Indigo's device list**, under the `ESPHome` folder, the plugin
   should auto-create one device per testable entity:
   - `esphome-test-rig - Test Switch` (relay)
   - `esphome-test-rig - Test Dimmer` (dimmer)
   - `esphome-test-rig - Test RGB` (dimmer)
   - `esphome-test-rig - Test Motion` (sensor — boolean)
   - `esphome-test-rig - Test Door` (sensor — boolean)
   - `esphome-test-rig - Test Counter` (sensor — numeric)
   - `esphome-test-rig - Uptime` (sensor)
   - `esphome-test-rig - WiFi RSSI` (sensor)
   - `esphome-test-rig - Test Status` (sensor — text)
   - + the node device itself (`esphomeNode`)

3. **In the device's own web UI** (because we enabled `web_server:`),
   browse to `http://<device-ip>/` — you'll see a list of all entities
   with toggle buttons you can use independently. Useful for sanity
   checks against what Indigo shows.

## After testing

Once the plugin works end-to-end against this rig:

1. Add API encryption back to the YAML:
   ```yaml
   api:
     encryption:
       key: "<base64 32-byte key — generate via `openssl rand -base64 32`>"
   ```

2. Enter the same key into the device's pluginProps in Indigo (or set
   it as the plugin's default encryption key in PluginConfig).

3. OTA-flash the updated config — device reboots, plugin reconnects
   with the key, everything continues working.

## Restoring it to BLE-proxy-only

Once we're done testing the plugin, if you want it back as a pure BLE
proxy with encryption:

```yaml
esphome:
  name: esp32-bluetooth-proxy

esp32:
  board: esp32dev
  framework:
    type: arduino

api:
  encryption:
    key: "<your key>"

ota:
  - platform: esphome
    password: "<your password>"

wifi:
  ssid: "..."
  password: "..."

bluetooth_proxy:
  active: true
```

Compile and OTA-upload via the dashboard or CLI.
