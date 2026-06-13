#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    zoo_manifest.py
# Description: The ESPHome "device zoo" — a declarative table mapping a node's
#              entity set to the Indigo deviceTypeId the classifier
#              `_classify_node_type` must pick, by the primary-type priority
#              Lock > Climate > Switch > Light > Fan > Cover (else esphomeNode).
#              Driven by test_zoo.py (per-case contract + invariants).
#
#              Entities are real aioesphomeapi EntityInfo objects (the classifier
#              uses isinstance), built from a list of entity KINDS. Real cases
#              (real=True) come from CliveS's live devices' entityKeyMap (the
#              kinds the device actually exposes, with the primary kind implied
#              by its deviceTypeId) — faithful because the classifier only looks
#              at entity TYPE. Synthetic cases cover the priority edges + the
#              node fallback.
# Author:      CliveS & Claude Opus 4.8
# Date:        13-06-2026
# Version:     1.0

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

# kind string -> aioesphomeapi EntityInfo class name. Built into objects in
# test_zoo (importing aioesphomeapi there keeps the manifest import-light).
KIND_TO_CLASS = {
    "switch":  "SwitchInfo",
    "light":   "LightInfo",
    "fan":     "FanInfo",
    "cover":   "CoverInfo",
    "climate": "ClimateInfo",
    "lock":    "LockInfo",
    "sensor":  "SensorInfo",
    "binary":  "BinarySensorInfo",
    "text":    "TextSensorInfo",
    "number":  "NumberInfo",
    "select":  "SelectInfo",
    "button":  "ButtonInfo",
}


@dataclass(frozen=True)
class ESPHomeCase:
    name:         str
    entity_kinds: list            # ordered list of kind strings -> built into EntityInfo objects
    expect_type:  str
    real:         bool = False
    note:         str = ""


_REAL_DIR = os.path.join(os.path.dirname(__file__), "zoo_real")


def _real(stem, note=""):
    with open(os.path.join(_REAL_DIR, f"{stem}.json"), encoding="utf-8") as fh:
        d = json.load(fh)
    return ESPHomeCase(f"real_{stem}", d["entity_kinds"], d["deviceTypeId"], real=True, note=note)


CASES = [
    # ── Real-derived from the live estate's entityKeyMap ─────────────────────
    _real("test_rig",
          note="switch+light+fan+cover+... -> esphomeSwitch (switch wins the priority)"),
    _real("buttons_rig",
          note="lock primary + buttons/sensors -> esphomeLock (lock highest; buttons aren't primary)"),
    _real("climate_rig", note="climate + sensors -> esphomeClimate"),
    _real("athom_plug", note="light primary + 21 monitoring entities -> esphomeLight"),

    # ── Synthetic: single primary of each type ───────────────────────────────
    ESPHomeCase("only_lock",    ["lock"],    "esphomeLock"),
    ESPHomeCase("only_climate", ["climate"], "esphomeClimate"),
    ESPHomeCase("only_switch",  ["switch"],  "esphomeSwitch"),
    ESPHomeCase("only_light",   ["light"],   "esphomeLight"),
    ESPHomeCase("only_fan",     ["fan"],     "esphomeFan"),
    ESPHomeCase("only_cover",   ["cover"],   "esphomeCover"),

    # ── Synthetic: priority resolution when several primaries co-exist ───────
    ESPHomeCase("lock_beats_all", ["climate", "switch", "light", "fan", "cover", "lock"],
                "esphomeLock", note="lock is highest priority"),
    ESPHomeCase("switch_beats_light_fan_cover", ["cover", "fan", "light", "switch"],
                "esphomeSwitch", note="switch > light > fan > cover"),
    ESPHomeCase("light_beats_fan_cover", ["cover", "fan", "light"],
                "esphomeLight"),
    ESPHomeCase("fan_beats_cover", ["cover", "fan"], "esphomeFan"),
    ESPHomeCase("climate_beats_switch", ["switch", "climate"], "esphomeClimate"),

    # ── Synthetic: the node fallback (no primary type present) ───────────────
    ESPHomeCase("node_sensors_only", ["sensor", "binary", "text"], "esphomeNode",
                note="no primary control entity -> generic node"),
    ESPHomeCase("node_buttons_only", ["button", "button", "number", "select"], "esphomeNode",
                note="buttons/number/select are not primary types -> node"),
    ESPHomeCase("node_empty", [], "esphomeNode", note="defensive: no entities -> node, no crash"),
]
