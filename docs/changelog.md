---
title: Version history
nav_order: 9
---

# Version history

The newest version is at the top.

## 0.10.1 — 5 October 2026

**A device that moves to a new network address is found there.** When your router gave an ESPHome device a new address, the plugin kept trying the old one until it was restarted. It now hears the device announce its new address, says so once in the Event Log, and connects there, straight away even if it had given up on it.

**A device that does not use encryption no longer sends the plugin round in circles.** With a **Default API Encryption Key** set, the plugin offered that key to a device that does not use one, cleared it, then offered it again, over and over with no pause. Once a device has said it does not use encryption, the plugin offers it no key at all. If the device later asks for one, or you type a key into its settings, the plugin uses keys for it again.

**Thermostats show their action and preset as words.** The HVAC Action showed a number, such as 3, rather than heating, and the preset was never shown at all. A thermostat also gets its heat and cool setpoints in Indigo, and its list of modes in the device's settings reads Off, Heat, Cool and so on rather than numbers. That part reaches each thermostat the next time the plugin connects to it.

**Disabling a device in Indigo leaves it alone.** The plugin went on writing readings to a disabled device. It now closes the connection to it, and enabling the device again reconnects it.

**A device that is switched off no longer fills the log once an hour.** The plugin warned twice each time it gave up, and once it had given up on a device you have in Indigo it started again a minute later, every minute. It now tries an hour later, says nothing more unless it gets through, and only starts again early for a device that is new in Indigo or has moved address. The line saying it is connecting is written once, not on every try.

## 0.10.0 — 27 September 2026

**Connected and Status now tell the truth whichever way auto-create is set.** With **Auto-create Indigo devices on discovery** unticked, a device you had made yourself never showed **Online**, even while it was working perfectly. It does now, and every device's **Connected** and **Status** are put right when the plugin starts, rather than carrying on from before.

**Toggle works on a blind.** It closes a blind that is open at all, and opens one that is closed. It did nothing before.

**Send Status Request works on switches, lights, fans, blinds and locks.** It writes the latest readings the device has sent back into Indigo, or says in the Event Log that there is no connection. It did nothing before.

**Changing a light's white temperature no longer turns it black.** The plugin sent the colour as black with it, and never sent the temperature at all. **Set Color Levels** now sends only the levels you give it, so changing the red alone leaves green and blue as they were.

**Lights get the right controls in Indigo.** A bulb that does both colour and white temperature had no white-temperature slider, and a light with separate cold and warm white LEDs was given a colour picker it cannot use. The plugin now reads what each light can really do, and a cold and warm white light gets a white-temperature slider once it has told the plugin its warmest and coolest shades. The change reaches each light the next time the plugin connects to it.

The help beside **Ignore these devices** now calls the menu item by its real name, **List Discovered Devices**.

## 0.9.0 — 23 September 2026

**Voltage readings are only recorded when the mains actually moves.** A power-monitoring plug reports the mains voltage every few seconds, and it wobbles by a few hundredths of a volt all the time, so every report was a new reading — about 40,000 rows a day in SQL Logger's history from two freezer plugs. The new setting **Ignore voltage changes smaller than (V)** is 0.5 to start with. A smaller change is not recorded, while a real rise or fall, or a slow drift that adds up to half a volt, still is. Set it to 0 to record every reading as before. Power, current and energy are not affected.

## 0.8.5 — 23 September 2026

**ESPHome devices no longer fill SQL Logger's history with a row every couple of seconds.** The plugin records when it last heard from each device, and that time changes on almost every message, so SQL Logger saved a whole history row each time — over 4 million rows for one smart plug in three months. The plugin now tells SQL Logger to skip that time, the uptime and the two Wi-Fi signal readings. Anything you already told SQL Logger to skip is kept, a device you set to skip entirely stays that way, and existing history is untouched.

## 0.8.4 — 11 September 2026

The note inside the plugin of where its code lives on GitHub uses the same spelling as other Indigo plugins. Nothing else changed.

## 0.8.3 — 7 September 2026

Some of the help beside the plugin's settings was too long for the settings window, so it was cut off mid-sentence. It now sits in paragraphs that wrap. No setting changed.

## 0.8.2 — 8 August 2026

The **About** item in the Plugins menu opens this project's page. It went nowhere before.

## 0.8.1 — 27 July 2026

**Correcting an encryption key reconnects the device straight away.** Before, a device turned away for a wrong or missing key stayed disconnected until the plugin was restarted, even after you put the right key in. Now it connects as soon as you save a key on the device or a new default key in the plugin's settings, and until then it is left alone rather than tried again and again.

## 0.8.0 — 22 July 2026

New **Ignore these devices** setting, for things on the network that announce themselves as ESPHome devices but are not, such as a SMLIGHT Zigbee coordinator. The plugin never connects to or warns about anything listed, **List Discovered Devices** shows them as **IGNORED**, and a change takes effect as soon as you click Save.

## 0.7.1 — 21 July 2026

Log lines no longer come out with the time printed twice, and a setting saved as the word "false" is read as off.

## 0.7.0 — 21 July 2026

Changes from a full review of the plugin.

- **A device that only looks like an ESPHome device no longer fills the log.** The plugin used to retry one for ever and warn every 35 seconds. Now it warns once, retries quietly with a longer gap each time, then stops and says why. It tries again an hour later, or straight away if you add it to Indigo.
- **List Discovered Devices** tags each device as connected, adopted, discovered or given up on.
- **Readings can no longer overwrite each other.** A reading called Status used to land on top of the plugin's own **Status**, and a battery reading used a name Indigo keeps for itself, so it vanished. Both now get a state of their own.
- **A missing reading leaves the last good value in place,** where it used to be written in as "not a number".
- **The one-off tidy-up from version 0.4.0** only ever removes devices of the old kind, so it can never remove a working one.
- **Settings behave.** A saved tick-box no longer reads as its opposite, and the **Log Level** you pick takes effect.
- **A sensor device answers a status request** instead of Indigo reporting an error.
- **ESPHome Device Came Online** and **Went Offline** triggers now run.
- The default encryption key can be kept in `IndigoSecrets.py`.

## 0.6.1 — 21 July 2026

Warnings and errors appear in the Event Log as warnings and errors. Before, they all appeared as ordinary lines.

## 0.6.0 — 17 June 2026

**Sensor devices.** A device with readings and nothing to control, such as a power-monitoring plug with no relay, becomes an **ESPHome Sensor Node** whose main reading shows in the device list. Before, the little status light on such a plug made it a dimmer. Plugs with a relay and real lights are unchanged, and devices of the wrong kind were replaced automatically.

## 0.5.4 — 10 June 2026

Tidying of the code, with no change you would see.

## 0.5.3 — 29 May 2026

A device found on the network that needs an encryption key but is not in Indigo is logged as an ordinary note, not an error.

## 0.5.2 — 25 May 2026

Editing a device only reconnects it when you change something that affects the connection, such as its encryption key.

## 0.5.1 — 23 May 2026

Every log line starts with the time to the thousandth of a second, with a menu item to turn that off.

## 0.5.0 — 20 May 2026

New **Upload Firmware (OTA)...** menu item, which loads new firmware onto a device over the network.

## 0.4.4 — 20 May 2026

Every device shows its network address, MAC address, board and ESPHome version.

## 0.4.3 — 20 May 2026

Readings in seconds, such as uptime, show as, say, `1h 11m 35s`.

## 0.4.1 and 0.4.2 — 20 May 2026

Number readings are rounded to two decimal places.

## 0.4.0 — 20 May 2026

**One Indigo device for each ESPHome device.** Before, each reading and control became an Indigo device of its own, often twenty or more for one smart plug. Now each ESPHome device is one Indigo device, with its readings as states. The old devices were removed on upgrade and the new ones made on the next discovery, keeping any encryption keys.

## 0.3.0 — 20 May 2026

Thermostats and locks, number and select settings, and the **Press ESPHome Button** action. Setting a fan's speed from Indigo reaches the fan. A device given an encryption key it does not use has the key cleared, and one needing a key the plugin lacks is no longer retried every few minutes.

## 0.2.0 — 20 May 2026

Fans and covers, and colour for lights. Turning a light off from Indigo now always works.

## 0.1.0 — 19 May 2026

First version: finds ESPHome devices on the network, connects to each, and brings in switches, sensors and lights.
