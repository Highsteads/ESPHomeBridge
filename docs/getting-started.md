---
title: Getting started
nav_order: 2
---

# Getting started

This takes a few minutes, and most of it happens by itself.

## What you need

- Indigo 2025.2 or later.
- One or more ESPHome devices already joined to your Wi-Fi.
- The Mac that runs Indigo and your ESPHome devices on the **same home network**. The plugin finds devices by listening for the announcements they make, and those announcements do not cross from one network to another. If your router keeps smart-home gadgets on a separate network of their own, it has to be set to pass these announcements across, which most routers call **mDNS** or **multicast DNS**.
- If a device uses an encryption key, the key itself. It is the long string of letters and numbers, usually ending in `=`, on the `key:` line under `api:` and `encryption:` in the device's ESPHome configuration.

## What your ESPHome devices need

A device needs the `api:` section in its configuration, which ESPHome puts there when you create a device, so most devices need no change at all. The lines below show the parts this plugin uses:

```yaml
api:
  encryption:
    key: "..."        # optional — leave the encryption lines out for no key
  reboot_timeout: 0s  # optional — see below

web_server:           # optional — only needed for Upload Firmware
```

- **encryption** — if the device has a key, Indigo needs the same key. The [Settings](settings.md) page shows where to put it.
- **reboot_timeout** — ESPHome restarts a device that has had nothing connected to it for 15 minutes. Setting it to `0s` stops that, so the device does not keep restarting while Indigo is switched off.
- **web_server** — the plugin's **Upload Firmware** menu item sends new firmware through the device's own web page, so that page has to be switched on.

## 1. Install the plugin

1. Go to the [Releases page](https://github.com/Highsteads/ESPHomeBridge/releases/latest) and download `ESPHomeBridge.indigoPlugin.zip`
2. Unzip the downloaded file — you will get `ESPHomeBridge.indigoPlugin`
3. Double-click `ESPHomeBridge.indigoPlugin` — Indigo will install it automatically

Indigo asks whether to enable the plugin. Say yes. The first time it starts, Indigo fetches two extra pieces of software the plugin needs, so the first start takes longer than later ones.

## 2. Check the settings

Open **Plugins → ESPHome Bridge → Configure**.

If your devices have no encryption key, leave everything as it is and click **Save**.

If they share one key, paste it into **Default API Encryption Key** and click **Save**. A device with a key of its own gets it in its own settings once it has appeared, as the next step explains. Every setting is explained on the [Settings](settings.md) page.

## 3. Let the devices appear

Within a few seconds the plugin finds the ESPHome devices on your network and makes an Indigo device for each, in a new device folder called **ESPHome**. Each one takes the name the device has in ESPHome. You can rename it and move it to another folder, and the plugin leaves both alone after that.

A device that wants a key the plugin does not have cannot be read, so it is not made. The Event Log has a line saying the plugin found an encrypted ESPHome device that is not set up in Indigo. To add it yourself:

1. Choose **Plugins → ESPHome Bridge → List Discovered Devices** and note the device's MAC address from the Event Log — the twelve letters and numbers after the tag in square brackets.
2. In Indigo, choose **New Device**, set **Type** to **ESPHome Bridge**, and pick the model that fits, or **ESPHome Node (info-only)** if you are not sure.
3. Type the MAC address into **MAC Address** exactly as the log shows it, with no colons, paste the key into **API Encryption Key (Base64)**, and click **Save**.

The plugin connects straight away. If the model you picked does not match the device, the plugin replaces your device with one of the right kind, named after the device and put in the **ESPHome** folder, keeping the key.

## 4. Check it works

Look at the new devices in Indigo's device list. A switch shows on or off, a light shows its brightness, a sensor shows its main reading. Switch one on and off from Indigo, and switch it at the device itself if it has a button — Indigo should show the change within a second or two.

To see everything the plugin found, choose **Plugins → ESPHome Bridge → List Discovered Devices**. The Event Log gets one line for each device, saying whether it is connected.

If a device does not appear, the [When something goes wrong](troubleshooting.md) page goes through the usual causes.
