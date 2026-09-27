---
title: When something goes wrong
nav_order: 8
---

# When something goes wrong

Each section starts with what you see, then what it means and what to do.

## A device has not appeared in Indigo

- Choose **Plugins → ESPHome Bridge → List Discovered Devices** and look in the Event Log. If the device is not listed at all, the plugin has not heard it announce itself.
  - Check the device is powered and on your Wi-Fi — if its configuration has `web_server:`, its web page opens when you type its network address into a browser.
  - Check it is on the same network as the Mac that runs Indigo. Announcements do not cross from one network to another unless your router passes them on, which most routers call **mDNS** or **multicast DNS**.
  - Choose **Plugins → ESPHome Bridge → Discover ESPHome Devices Now**, which makes devices announce themselves again.
- If it is listed as **IGNORED**, take it out of **Ignore these devices** in **Plugins → ESPHome Bridge → Configure**.
- If it is listed as **DISCOVERED** or **PARKED**, look in the Event Log for a line about an **encrypted ESPHome device not set up in Indigo**. The device needs a key the plugin does not have. Either put the key in **Default API Encryption Key** in the plugin's settings, or add the device yourself as [Getting started](getting-started.md) describes.
- Check **Auto-create Indigo devices on discovery** is ticked in the plugin's settings.

## Status shows "Needs encryption key" or "Bad key"

The device uses an encryption key, and the plugin has no key for it (**Needs encryption key**) or the wrong one (**Bad key**). The plugin has stopped trying until you give it one.

- Double-click the device, paste the key from the `key:` line under `api:` and `encryption:` in its ESPHome configuration into **API Encryption Key (Base64)**, and click **Save**. The plugin connects straight away.
- If you have lost the key, give the device a new one in its ESPHome configuration, load the new firmware onto it — with ESPHome over a cable if the old key stops you doing it over Wi-Fi — and put the new key in Indigo.

## The log says a device "gave up after" so many failed connections

The plugin has tried to connect ten times in a row, or three for a device that is not in Indigo, and has stopped trying for an hour.

- If it is not really an ESPHome device — some makers' products, such as SMLIGHT's Zigbee coordinators, announce themselves as ESPHome devices without being one — add its MAC address, name or network address to **Ignore these devices** in the plugin's settings. It will never be tried or warned about again.
- If it is an ESPHome device that was switched off or away, it is tried again after an hour, or straight away if you add it to Indigo or disable and enable its Indigo device.
- **List Discovered Devices** shows every device given up on, and the error that caused it.

## Status shows "Disconnected"

The plugin has lost its connection to the device and is trying again, with a longer gap each time up to five minutes. Check the device has power and Wi-Fi. When it answers, the plugin reconnects by itself.

## A device came back as a new Indigo device

New firmware changed what kind of device it should be, so the plugin replaced the old Indigo device with one of the right kind, back in the **ESPHome** folder. Move and rename it as you like, and point any triggers, action groups and control pages that used the old device at the new one. The [How it works](how-it-works.md) page explains why.

## A device I deleted has come back

The plugin makes a device for every ESPHome device it connects to while **Auto-create Indigo devices on discovery** is ticked. Add the device to **Ignore these devices** in the plugin's settings and delete it again.

## A voltage reading does not change for a while

That is the **Ignore voltage changes smaller than (V)** setting. A reading in volts only changes when it moves by at least that much, half a volt to start with. Set it to 0 in the plugin's settings to record every reading.

## A new reading on a device has not appeared

The plugin reads the list of what a device offers each time it connects. A reading added in new firmware appears once the device has restarted and the plugin has reconnected, as long as **Auto-create Indigo devices on discovery** is ticked.

## A blind or shutter will not go to a position

The cover has to be able to go to a position in its ESPHome configuration — for a template cover that means `has_position: true` and a position action. Without them, it only opens and closes.

## A firmware upload fails

- **"File looks too small"** — the file is under 100 KB, which is not ESPHome firmware. Check you picked the `.bin` file ESPHome made.
- **"File not found"** — check the path, all of it, from the `/` at the start.
- **"upload failed"** or **"device returned HTTP"** in the log — the device's web page did not take the file. Check it has `web_server:` in its configuration and has no password on its web page.

## Still stuck?

Choose **Plugins → ESPHome Bridge → Show Plugin Info** and **Dump All Entities to Log**, copy the lines they write to the Event Log, and post them on the [Indigo forum](https://forums.indigodomo.com) with a description of what you see. You can also [raise an issue on GitHub](https://github.com/Highsteads/ESPHomeBridge/issues).
