---
title: Your devices
nav_order: 3
---

# Your devices

Each ESPHome device becomes **one** Indigo device. The plugin looks at everything the ESPHome device offers and picks the kind of Indigo device from the thing you are most likely to want to control, in this order: a lock, a thermostat, a switch, a light, a fan, a blind or shutter. That part is driven by Indigo's usual controls. Everything else the device offers is shown on the same Indigo device, as extra states.

A light is passed over when it is only a status light — when ESPHome marks it as a settings or diagnostic item, or when the device measures power. That is how a smart plug with no relay, whose only light is the little one on its front, becomes a sensor rather than a dimmer.

A device with nothing to control but with readings becomes an **ESPHome Sensor Node**. One with neither becomes an **ESPHome Node (info-only)**.

## What every device shows

| Shown as | What it means |
|---|---|
| **Connected** | True while the plugin has a working connection to the device. It is set afresh when the plugin starts, whether or not **Auto-create Indigo devices on discovery** is ticked. |
| **Status** | **Online**, **Disconnected**, **Needs encryption key** or **Bad key**. The [When something goes wrong](troubleshooting.md) page explains the last two. |
| **Last Seen** | The date and time of the last reading the device sent, such as `2026-09-27T09:15:04`. |
| **ipAddress**, **macAddress**, **boardModel**, **esphomeVersion** | The device's network address, its MAC address (a number every network device is made with), the board it runs on, and the version of ESPHome on it. If the device already offers a reading with one of these names, that reading is used instead. |

**Send Status Request** on a switch, light, fan, blind or lock writes the latest readings the device has sent back into Indigo. ESPHome devices send every change as it happens, so Indigo is normally up to date already, and this writes the readings again. If the plugin has no connection to the device, the Event Log says so and **Connected** and **Status** are set to match.

## Everything else the device offers

Each reading or control on the ESPHome device becomes a state named after it, so a sensor called **Power** becomes a state called **power**, and **WiFi Signal dB** becomes **wifiSignalDb**.

| On the ESPHome device | In Indigo |
|---|---|
| A sensor with a number, such as power, voltage or temperature | A number, rounded to two decimal places, with its unit shown beside it. A reading in seconds, such as uptime, is kept as a number of seconds and shown as, say, `1h 11m 35s`. |
| A text sensor | The text, as the device sends it. |
| A binary sensor, such as motion or a door contact | On or off. |
| A number setting | The number. The **Set Number Entity Value** action changes it. |
| A select setting (a list of options) | The option chosen. The **Set Select Entity Option** action changes it. |
| A button | No state — the **Press ESPHome Button** action presses it. |
| A second switch, light, fan, blind or lock | On or off, and for a light, fan or blind a matching **Level** from 0 to 100. These are shown, not controlled, from Indigo. |

When a device has nothing to report for a reading — ESPHome's way of saying "no reading" — the plugin leaves the last good value in place rather than writing a blank.

Readings in volts, other than a sensor node's headline reading, only change in Indigo when they move by half a volt or more, unless you change that in the settings. The [Settings](settings.md) page explains why.

## ESPHome Switch Node

For a device whose main job is to switch something on and off, such as a smart plug with a relay. **Turn On**, **Turn Off** and **Toggle** work as they do for any Indigo relay.

## ESPHome Light Node

For a device whose main job is a light. **Turn On**, **Turn Off**, **Toggle**, **Set Brightness**, **Brighten By** and **Dim By** all work. **Turn On** brings a light that is off back at full brightness.

| Shown as | What it means |
|---|---|
| Brightness | The light's brightness from 0 to 100, and 0 when it is off. |
| **Color Temp** | The colour temperature the light reports, if it has one. |
| Red, green and blue levels | For a colour light, the colour it is set to. |

Indigo shows the colour controls the light can use: a colour picker for a colour light, a white level for one with a separate white channel, and a white-temperature slider for one that changes its shade of white. A light with separate cold and warm white LEDs gets the slider once it has told the plugin its warmest and coolest shades.

**Set Color Levels** sends only the levels you give it. Change the red level alone and green and blue stay as they are. Change the white temperature and the light moves to that shade of white, without touching the colour.

## ESPHome Fan Node

For a fan. Indigo shows it as a dimmer, with the brightness standing for the fan speed.

- **Turn On**, **Turn Off** and **Toggle** switch it on and off.
- **Set Brightness**, **Brighten By** and **Dim By** set the speed. The plugin turns the 0 to 100 into the fan's own speed steps, so on a fan with five speeds, 40 is speed two.
- **Oscillating** and **Direction** (**forward** or **reverse**) show those settings for a fan that has them.

## ESPHome Cover Node

For a blind, shutter, curtain or garage door — ESPHome calls all of these a **cover**. Indigo shows it as a dimmer, with the brightness standing for how far open it is: 0 is closed and 100 is fully open.

- **Turn On** opens it fully and **Turn Off** closes it. **Toggle** closes it if it is open at all, and opens it fully if it is closed.
- **Set Brightness**, **Brighten By** and **Dim By** move it to a position, if the cover can go to a position.
- **Operation** shows **idle**, **opening** or **closing**.

## ESPHome Climate Node

For a thermostat or heat pump controller. Indigo's usual thermostat controls work: set the mode, set the heat or cool setpoint, or nudge either up or down.

| Shown as | What it means |
|---|---|
| Mode | Off, Heat, Cool or Heat Cool. ESPHome's Auto shows as Heat Cool. Its Fan Only and Dry modes show as Off, because Indigo has no mode for them. |
| Temperature | The temperature the device reads, if it has a sensor. |
| Heat and cool setpoints | The target temperatures. A device that keeps a low and a high target shows the low as the heat setpoint and the high as the cool setpoint. One with a single target shows it on the side that matches its mode. |
| **HVAC Action** | What it is doing now, such as heating, cooling or idle. |
| **Preset** | The preset it is on, if it uses them. |

When the device is made, the plugin reads its lowest and highest temperatures and the modes it accepts, and shows them in the device's settings.

## ESPHome Lock Node

For a lock. Indigo shows it as a relay, where **on means locked**.

- **Turn On** locks, **Turn Off** unlocks, and **Toggle** does whichever it is not.
- **Lock State** shows the lock's own word for what it is doing: **locked**, **unlocked**, **jammed**, **locking**, **unlocking**, **opening**, **open** or **unknown**. While it is locking or unlocking, Indigo shows it as locked.
- The **Lock - Open (latch release)** action releases the latch, for a lock that can.

## ESPHome Sensor Node

For a device with readings and nothing to control, such as a power-monitoring plug without a relay, or a temperature sensor. The device list shows one **headline** reading, with its unit. The plugin picks it in this order: power, energy, temperature, humidity, light level, pressure, carbon dioxide, voltage, current, battery, signal strength. It passes over readings ESPHome marks as diagnostic unless there is nothing else. The unit appears in the device's settings as **Headline Unit**, and every other reading is a state of its own, as above.

## ESPHome Node (info-only)

For a device with nothing to control and no number readings — only text or on/off readings, say. The device list shows its **Status**, and its readings are states, as above.

## What I have tested

I have tested switches, lights, fans, blinds and sensors on Athom smart plugs and an ESP32 board of my own. Colour lights, thermostats and locks are in the plugin but I have no ESPHome hardware of those kinds to test them on, so if you have, a report on [GitHub](https://github.com/Highsteads/ESPHomeBridge/issues) would help a great deal.
