---
title: The plugin menu
nav_order: 7
---

# The plugin menu

These are under **Plugins → ESPHome Bridge**.

| Menu item | What it does |
|---|---|
| **Discover ESPHome Devices Now** | Starts listening for device announcements afresh, so devices already on the network announce themselves again. Useful if a device joined the network and has not appeared. |
| **List Discovered Devices** | Writes a line to the Event Log for every device the plugin has heard from since it started, with its MAC address, name, network address, ESPHome version and board, and a tag saying what the plugin made of it: **CONNECTED** (talking to the plugin now), **ADOPTED** (has an Indigo device but is not connected), **DISCOVERED** (found, with no Indigo device), **PARKED** (given up on for now) or **IGNORED** (on the ignore list). A parked device gets a second line saying how long ago, after how many failures, and why. |
| **Dump All Entities to Log** | For every connected device, writes everything it offers to the Event Log — each reading, setting and control, with ESPHome's own name and number for it. It is a lot of lines, and most useful to include in a bug report. |
| **Upload Firmware (OTA)...** | Loads new firmware onto a device over the network. See below. |
| **Toggle Timestamps in Log (on/off)** | Every line the plugin writes to the log starts with the time to the thousandth of a second, which helps when lining events up. This turns that on or off. It stays as you leave it. |
| **Show Plugin Info** | Writes the plugin's version, details of your Mac and Indigo, and how many devices are discovered, connected, in Indigo and given up on, to the log. It is useful to include if you ask for help on the Indigo forum. |

## Upload Firmware (OTA)

"OTA" stands for over the air — loading new software onto a device over Wi-Fi rather than with a cable. This menu item does it from Indigo, so you do not have to go back to ESPHome for every change.

1. Build the new firmware in ESPHome and download the file it makes, usually `firmware.ota.bin` or `firmware.bin`.
2. Choose **Plugins → ESPHome Bridge → Upload Firmware (OTA)...**.
3. Pick the device in **Target Device**. The list has every ESPHome device in Indigo, and also any the plugin has found that are not in Indigo — one it cannot read because of an encryption key, say.
4. Type the full path to the file in **Firmware .bin path**. In Finder you can select the file, hold the Option key, and choose **Copy as Pathname** from the Edit menu, then paste it in — the quote marks Finder adds are fine.
5. Click the button to start.

The plugin checks the file is there and is at least 100 KB, because ESPHome firmware is usually between 600 KB and 1.5 MB and anything much smaller is the wrong file. It closes its own connection to the device if it has one, sends the file in the background, and writes how it went to the Event Log. The device restarts itself a few seconds after a good upload, and the plugin connects to it again by itself.

The device needs its web page switched on, with `web_server:` in its configuration, because that is where the file is sent. The plugin does not send a web page username or password.
