---
title: Settings
nav_order: 6
---

# Settings

## The plugin's settings

Open these with **Plugins → ESPHome Bridge → Configure**. They apply to every device, and a change takes effect as soon as you click **Save**.

| Setting | What it does |
|---|---|
| **Auto-create Indigo devices on discovery** | Ticked, the plugin makes an Indigo device for each ESPHome device it finds, and brings it up to date each time it connects. Unticked, it still finds devices and connects to them, but makes nothing new. Ticked to start with. |
| **Ignore these devices** | Things on your network that announce themselves as ESPHome devices but are not, such as a SMLIGHT Zigbee coordinator. List their MAC addresses, network names or network addresses, separated by commas or spaces. The plugin never connects to anything listed and never warns about it. **List Discovered Devices** in the Plugins menu shows each one's MAC address. If an ignored device also has an Indigo device, the ignore list wins, the Indigo device stays disconnected, and the Event Log says so once. |
| **Default API Encryption Key** | The encryption key for any device that has none of its own in its settings. Paste it from the `key:` line under `api:` and `encryption:` in the device's ESPHome configuration. Leave it blank if your devices have no key. |
| **Ignore voltage changes smaller than (V)** | A reading in volts only changes in Indigo when it moves by at least this much from the last value recorded. It is 0.5 to start with. Set 0 to record every reading. It has to be a number, 0 or more. [How it works](how-it-works.md) explains why it is there. |
| **Log Level** | How much the plugin writes to the Event Log: **Debug (verbose)**, **Info**, **Warning only** or **Error only**. **Info** to start with. |

### Keeping the encryption key in one file

If you run several of my plugins, you can keep their passwords and keys in one shared file instead of typing them into each plugin. The file is called `IndigoSecrets.py` and lives in `/Library/Application Support/Perceptive Automation/`. A blank copy, `IndigoSecrets_example.py`, comes inside the plugin — copy it to that folder, rename it `IndigoSecrets.py`, and fill in the line `ESPHOME_DEFAULT_ENCRYPTION_KEY = ""` with your key between the quotes.

When the file has the key, it is used, whatever the **Default API Encryption Key** box says. This plugin needs nothing else from the file. The plugin reads the file when it starts, so restart the plugin after changing it.

## Each device's settings

Open these by double-clicking an ESPHome device in Indigo. The plugin fills most of them in itself.

| Setting | What it does |
|---|---|
| **MAC Address** | The device's MAC address, twelve letters and numbers with no colons. This is how the plugin knows which ESPHome device the Indigo device belongs to, so leave it as it is. |
| **Hostname**, **IP Address**, **API Port** | The device's name and network address, and the port it listens on, 6053 unless its configuration says otherwise. The plugin fills these in from the device's announcement, and always connects to the address the device announces, so there is no need to change them. |
| **API Encryption Key (Base64)** | This device's own encryption key, which it uses instead of the default. Leave it blank for a device with no key, or one that uses the default. If the device was waiting for a key, saving one here makes the plugin connect straight away. |
| **Board** and **ESPHome Version** | The board the device runs on and the version of ESPHome on it. For information only. |

Some kinds of device show a little more, also for information only:

| Kind of device | Extra settings |
|---|---|
| **ESPHome Sensor Node** | **Headline Unit** — the unit of the reading shown in the device list. |
| **ESPHome Fan Node** | **Speed Levels** — how many speeds the fan has. |
| **ESPHome Cover Node** | **Device Class** — what ESPHome calls the cover, such as a blind, shutter or garage door. |
| **ESPHome Climate Node** | **Min Temperature**, **Max Temperature** and **Supported Modes**, read from the device. |
