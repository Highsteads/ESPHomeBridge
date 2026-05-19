# ESPHome Bridge — Indigo Plugin

Bridges [ESPHome](https://esphome.io/) devices into [Indigo Domotics](https://www.indigodomo.com/)
2025.2+ as native device types via ESPHome's **Native API** (port 6053).

Auto-discovers ESPHome devices on your LAN via mDNS, connects directly to each
device over TCP (no MQTT broker needed), and surfaces each ESPHome entity
(sensor, switch, light, fan, cover, climate, lock, button, ...) as a queryable
Indigo device with native controls.

**No cloud. No MQTT broker required. Purely local.**

## Status

**Pre-release / scaffold.** Currently in development.

## Companion plugins

This plugin is part of an MQTT-and-local-API collection for Indigo:

- [TasmotaBridge](https://github.com/Highsteads/TasmotaBridge) — Tasmota devices via MQTT
- [Zigbee2MQTTBridge](https://github.com/Highsteads/Zigbee2MQTTBridge) — Zigbee devices via MQTT
- [ShellyDirect](https://github.com/Highsteads/ShellyDirect) — Shelly Gen2/3 over local HTTP
- [Ecowitt](https://github.com/Highsteads/Ecowitt) — Ecowitt weather stations
- **ESPHomeBridge** *(this plugin)* — ESPHome devices via native API

## License

MIT
