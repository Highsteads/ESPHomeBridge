# ESPHome Bridge for Indigo

**Bring your ESPHome plugs, lights and sensors into Indigo, found by themselves and talked to directly over your home network.**

**Version:** 0.9.0 | **Author:** CliveS & Claude | **Needs:** Indigo 2025.2 or later

**[Read the full guide](https://highsteads.github.io/ESPHomeBridge/)** — setting up, what everything means, and what to do when something goes wrong.

---

## What it does

[ESPHome](https://esphome.io) is free software that runs on the small Wi-Fi boards inside a great many smart plugs, lights and sensors — some come with it already on them, and some people build their own. This plugin lets [Indigo](https://www.indigodomo.com) use those devices through ESPHome's own connection, the same one Home Assistant uses, so there is no cloud account, no MQTT broker and nothing to add to a device that came with ESPHome on it.

- **Finds your devices by itself.** Each ESPHome device announces itself on the network, and the plugin makes an Indigo device for it within seconds.
- **Makes one Indigo device for each ESPHome device,** of the right kind — a switch, light, fan, blind, thermostat, lock or sensor — so Indigo's usual controls work on it.
- **Brings every reading in as part of that device,** so a smart plug that measures power is one Indigo device showing its power, voltage, current and energy.
- **Shows changes within a second or two,** because each device sends its readings to Indigo as they change.
- **Sets numbers, picks options and presses buttons** on a device, from Indigo actions.
- **Loads new firmware onto a device** from the Plugins menu.
- **Keeps SQL Logger's history small** by leaving out the readings that change on every message, and ignoring the tiny wobble in mains voltage.
- **Copes with encryption keys,** and with things on the network that announce themselves as ESPHome devices but are not.

## What it works with

Any device running ESPHome that is on the same home network as the Mac that runs Indigo.

| In Indigo | Your ESPHome device |
|---|---|
| **ESPHome Switch Node** | A smart plug or relay that switches something on and off |
| **ESPHome Light Node** | A light, dimmable or coloured |
| **ESPHome Fan Node** | A fan, with its speeds |
| **ESPHome Cover Node** | A blind, shutter, curtain or garage door |
| **ESPHome Climate Node** | A thermostat or heat pump controller |
| **ESPHome Lock Node** | A lock |
| **ESPHome Sensor Node** | A device with readings and nothing to control, such as a power-monitoring plug with no relay |
| **ESPHome Node (info-only)** | Anything else |

I have tested switches, lights, fans, blinds and sensors on Athom smart plugs and an ESP32 board of my own. Colour lights, thermostats and locks are in the plugin but untested on real hardware, so reports are welcome.

## Installing

1. Go to the [Releases page](https://github.com/Highsteads/ESPHomeBridge/releases/latest) and download `ESPHomeBridge.indigoPlugin.zip`
2. Unzip the downloaded file — you will get `ESPHomeBridge.indigoPlugin`
3. Double-click `ESPHomeBridge.indigoPlugin` — Indigo will install it automatically

## Setting it up

1. If your ESPHome devices use an encryption key, open **Plugins → ESPHome Bridge → Configure**, paste the key into **Default API Encryption Key**, and click **Save**. Otherwise there is nothing to set.
2. Wait a few seconds. Your devices appear in a new device folder called **ESPHome**, each named as it is in ESPHome.
3. Switch one from Indigo, and Indigo should show the change straight away.

The [full guide](https://highsteads.github.io/ESPHomeBridge/) goes through each step, explains every setting, and covers what to do if something does not work.

## What's new

**v0.9.0** — Voltage readings are only recorded when the mains actually moves. The new setting **Ignore voltage changes smaller than (V)** is 0.5 to start with, so the constant wobble of a few hundredths of a volt no longer adds a row to SQL Logger's history every few seconds, while a real rise or fall, or a slow drift that adds up to half a volt, is still recorded. Set it to 0 to record every reading.

**v0.8.5** — ESPHome devices no longer fill SQL Logger's history with a row every couple of seconds. The plugin tells SQL Logger to skip the last-heard time, the uptime and the two Wi-Fi signal readings, keeping anything you already told it to skip.

Every version is listed in the [version history](https://highsteads.github.io/ESPHomeBridge/changelog.html).

## Authors & licence

Vibed into existence by **CliveS**, who knew what he wanted, argued until he got it, and tested it on a real house. Typed at inhuman speed by **Claude** (Anthropic), who mostly did as it was told.

© 2026 CliveS · [MIT licence](LICENSE) — copy it, fork it, bend it, break it, fix it, ship it. If it breaks, you get to keep both pieces.
