---
title: Actions and triggers
nav_order: 5
---

# Actions and triggers

## Indigo's usual controls

Each device answers the controls Indigo gives its kind of device — **Turn On**, **Turn Off** and **Toggle** for a switch or lock, brightness for a light, fan or blind, and the thermostat controls for a thermostat — wherever you use them: the device list, a control page, a schedule, a trigger or an action group. The [Your devices](devices.md) page says what each one does on each kind of device.

If the plugin has no connection to the device when you use one, the command is not sent, and the Event Log says there is no active connection.

## The plugin's own actions

Add an action and choose one of these from the ESPHome Bridge actions. Each works on any ESPHome device in Indigo, and a list in the action shows the settings or buttons that device has.

| Action | What it does |
|---|---|
| **Set Number Entity Value** | Sets a number setting on the device. Choose it in **Number Entity** and type the new value in **Value**. It has to be within the lowest and highest values the device accepts. |
| **Set Select Entity Option** | Picks an option from a list setting on the device. Choose the setting in **Select Entity** and type the option in **Option**, exactly as the device spells it, capital letters included. If it is not one of the device's options, the Event Log lists the ones it has. |
| **Press ESPHome Button** | Presses a button the device offers, such as a restart button. Choose it in **Button Entity**. |
| **Lock - Open (latch release)** | For a lock only. Releases the latch, for a lock that can — an electric door strike, for example — rather than locking or unlocking. |

## Triggers

Create a new trigger, set its type to **ESPHome Bridge**, and pick one of these.

| Trigger | When it runs |
|---|---|
| **ESPHome Device Came Online** | Each time the plugin connects to a device, including when it reconnects after a drop. |
| **ESPHome Device Went Offline** | When a working connection to a device drops. |
| **New ESPHome Device Discovered** | The first time the plugin hears from each device after it starts. It runs for every device each time the plugin or Indigo restarts, not only for devices that are new to you. |

For the first two, **MAC Address (blank = any)** limits the trigger to one device. Leave it blank for any device, or type the device's MAC address, with or without colons — **List Discovered Devices** in the Plugins menu shows it.

You can also build a trigger on any state of a device, the same as for any Indigo device — the **Connected** state going false, a motion sensor turning on, or the power reading rising above a level, say.
