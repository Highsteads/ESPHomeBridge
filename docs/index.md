---
title: Home
nav_order: 1
---

# ESPHome Bridge for Indigo

This plugin brings ESPHome devices into [Indigo](https://www.indigodomo.com), finding them on your home network by itself and talking to each one directly. There is no cloud account, no internet connection and no MQTT broker involved.

## What ESPHome is

ESPHome is free software that runs on small, cheap Wi-Fi boards — the ESP32 and ESP8266 chips inside a great many smart plugs, lights and sensors. Some devices, such as Athom's smart plugs, come with ESPHome already on them. Other people build their own, wiring a sensor or a relay to a bare board and writing a short text file, called the device's **configuration**, that says what each part does. ESPHome turns that file into the board's software.

Every ESPHome device can be reached over your home network through ESPHome's own connection, which ESPHome calls its **native API**. It is the connection Home Assistant uses, and it is the one this plugin uses, so a device works with Indigo in the setup it came with.

## What it does for you

- **Finds your ESPHome devices by itself.** Each one announces itself on the network, the way a printer does, and the plugin makes an Indigo device for it within seconds.
- **Makes one Indigo device for each ESPHome device,** of the right kind — a switch, a light, a fan, a blind or shutter, a thermostat, a lock, or a sensor — so Indigo's usual controls work on it.
- **Brings every reading in as part of that device.** A smart plug that measures power becomes one Indigo device that also shows its voltage, current, energy and the rest, not a separate device for each.
- **Shows changes as they happen,** because the plugin keeps a connection open to each device and the device sends each new reading straight away.
- **Sets numbers, picks options and presses buttons** on a device, from Indigo actions.
- **Loads new firmware onto a device** from the Plugins menu, so you do not have to go back to ESPHome for every change.
- **Copes with devices that use an encryption key,** and with things on the network that announce themselves as ESPHome devices but are not.

## Where to go next

| If you want to... | Read |
|---|---|
| Install the plugin and see your first device appear | [Getting started](getting-started.md) |
| Know what each device shows in Indigo | [Your devices](devices.md) |
| Understand what the plugin is doing behind the scenes | [How it works](how-it-works.md) |
| Control devices from triggers, schedules and action groups | [Actions and triggers](actions-and-triggers.md) |
| Know what every setting does | [Settings](settings.md) |
| Know what each item in the Plugins menu does | [The plugin menu](plugin-menu.md) |
| Sort out a problem | [When something goes wrong](troubleshooting.md) |
| See what changed in each version | [Version history](changelog.md) |

## Download

The latest version is always on the [Releases page](https://github.com/Highsteads/ESPHomeBridge/releases/latest).
