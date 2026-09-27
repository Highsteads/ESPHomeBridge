---
title: How it works
nav_order: 4
---

# How it works

You do not need to know any of this to use the plugin. It is here for anyone who likes to know what is going on.

## Finding devices

Every ESPHome device announces itself on your home network every few minutes, saying what it is, its name and its network address — the four numbers such as `192.168.1.20` that identify it on your network. Apple calls this kind of announcement **Bonjour**, and it is how a Mac finds a printer. The plugin listens for these announcements the whole time it runs, so a new device is found within seconds of joining your Wi-Fi.

Each announcement carries the device's **MAC address**, a number every network device is made with and never changes. The plugin knows each device by that number, not by its name or network address, so renaming a device in Indigo, or your router giving it a new address, changes nothing.

Some other makers' devices borrow ESPHome's announcement without being ESPHome devices — SMLIGHT's Zigbee coordinators are one. The **Ignore these devices** setting tells the plugin to leave them alone, and the plugin also gives up on them by itself, as described below.

## One connection to each device

For each device it finds, the plugin opens a connection and keeps it open. The device sends each reading down that connection when it changes, or as often as its own configuration says, so Indigo shows a change within a second or two without the plugin having to ask. Commands from Indigo go back down the same connection.

While **Auto-create Indigo devices on discovery** is ticked, each time it connects the plugin reads the list of everything the device offers, along with its network address, board and ESPHome version, and brings the Indigo device up to date. So after you load new firmware that adds a reading, the new state appears when the device restarts and the plugin reconnects.

## Making the Indigo device

The first time the plugin connects to a device, it makes the Indigo device, names it after the device, and puts it in a device folder called **ESPHome**. The [Your devices](devices.md) page explains how it picks the kind of device. After that you can rename it and move it wherever you like.

If new firmware changes what kind of device it should be — say you add a relay to what was a sensor — Indigo cannot change a device's kind in place. So the plugin deletes the old Indigo device and makes a new one of the right kind, back in the **ESPHome** folder under the device's own name, keeping its encryption key. Triggers, action groups and control pages that used the old device need pointing at the new one.

If you delete an Indigo device while **Auto-create Indigo devices on discovery** is ticked, the plugin makes it again the next time it connects to that ESPHome device, such as after a restart. To stop that, add the device to **Ignore these devices**.

## When a connection drops

A device that goes quiet, or a connection that fails, is tried again after five seconds, then ten, then twenty, doubling each time up to five minutes between tries. The Event Log gets one warning the first time, and the retries after that are quiet.

After ten failures in a row for a device you have in Indigo, or three for one you do not, the plugin stops trying and says so once in the log — the line says it **gave up after** so many failed connections. That stops a device that is not really an ESPHome device from filling the log for ever. It tries once more an hour later, and straight away if you add the device to Indigo or enable its Indigo device. A device that worked and then drops starts the count again from nothing.

## Encryption keys

ESPHome can scramble everything sent between a device and whatever talks to it, using a key set in the device's configuration. Indigo needs the same key to talk to it. The plugin uses the key in the device's own settings if it has one, and the plugin's **Default API Encryption Key** if it does not.

- **A wrong key, or no key for a device that needs one,** stops the plugin trying, because trying again with the same key only fills the log. It waits for you. As soon as you save a key in the device's settings, or a new default key in the plugin's settings, it tries again.
- **A key for a device that does not use one** is cleared from the device's settings by the plugin, which then connects without it.

## Keeping SQL Logger's history small

Indigo's SQL Logger plugin can keep a history of every state of every device. Some of these states change on almost every message a device sends, and a history row for each would grow the database by tens of thousands of rows a day for each device. So:

- **Last Seen, uptime and the two Wi-Fi signal readings** are added to each device's SQL Logger skip list when the device starts. Anything you already told SQL Logger to skip for that device is kept, and a device you set to skip entirely stays that way. Existing history is not touched.
- **Readings in volts** only change when they move by more than a set amount, half a volt to start with. The mains voltage wobbles by a few hundredths of a volt all the time, and a power-monitoring plug reports it every few seconds. The change is measured from the last value Indigo recorded, so a slow drift that adds up to half a volt is still recorded. Power, current and energy are not affected.

## What goes in the log

The Event Log shows what you might need to know: devices found, connected and lost, devices given up on, key problems, and firmware uploads. Choose **Debug (verbose)** in **Log Level** to see every retry as well, which helps when chasing a problem.
