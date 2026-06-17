#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_zoo.py
# Description: Drives the ESPHome device zoo (zoo_manifest.CASES). Builds real
#              aioesphomeapi EntityInfo objects from each case's kind list, runs
#              the real classifier, and checks the per-node contract plus the
#              primary-type priority invariants.
# Author:      CliveS & Claude Opus 4.8
# Date:        13-06-2026

from __future__ import annotations

import types

import pytest

from zoo_manifest import CASES, KIND_TO_CLASS

# aioesphomeapi is a runtime dependency of the plugin; skip cleanly if a bare
# checkout doesn't have it installed (CI installs requirements).
aioesphomeapi = pytest.importorskip("aioesphomeapi")

KNOWN_TYPES = {
    "esphomeNode", "esphomeSensor", "esphomeSwitch", "esphomeLight",
    "esphomeFan", "esphomeCover", "esphomeClimate", "esphomeLock",
}
# Priority order high->low; the type a node gets when that kind is its top entity.
PRIORITY = ["lock", "climate", "switch", "light", "fan", "cover"]
KIND_TO_TYPE = {
    "lock": "esphomeLock", "climate": "esphomeClimate", "switch": "esphomeSwitch",
    "light": "esphomeLight", "fan": "esphomeFan", "cover": "esphomeCover",
}

# Sensor device_classes that mark a node as a meter (mirrors the plugin's set).
_METERING = {"power", "energy", "apparent_power", "reactive_power",
             "current", "voltage", "power_factor", "frequency"}

_IDS = [c.name for c in CASES]


def _kinds(entity_kinds):
    """The bare kind strings from a spec list that may contain dicts."""
    return [s["kind"] if isinstance(s, dict) else s for s in entity_kinds]


def _has_metering(entity_kinds):
    return any(isinstance(s, dict) and s.get("kind") == "sensor"
               and (s.get("device_class", "") or "").lower() in _METERING
               for s in entity_kinds)


def _light_is_diag(entity_kinds):
    return any(isinstance(s, dict) and s.get("kind") == "light"
               and int(s.get("entity_category", 0) or 0) != 0
               for s in entity_kinds)


def _has_sensor(entity_kinds):
    return "sensor" in _kinds(entity_kinds)


def _build_entities(specs):
    """Turn a list of entity specs into real aioesphomeapi EntityInfo objects.

    Each spec is either a kind string ("sensor") or a dict carrying the extra
    attributes the classifier inspects: {"kind": "sensor", "device_class":
    "power", "entity_category": 2, "unit": "W", "color_modes": [2]}.
    """
    out = []
    for i, spec in enumerate(specs):
        if isinstance(spec, dict):
            kind = spec["kind"]
        else:
            kind, spec = spec, {}
        cls = getattr(aioesphomeapi, KIND_TO_CLASS[kind])
        kwargs = {"key": i + 1, "name": f"{kind}{i}", "object_id": f"{kind}{i}"}
        if "device_class" in spec:
            kwargs["device_class"] = spec["device_class"]
        if "entity_category" in spec:
            kwargs["entity_category"] = spec["entity_category"]
        if "unit" in spec:
            kwargs["unit_of_measurement"] = spec["unit"]
        if "color_modes" in spec:
            kwargs["supported_color_modes"] = list(spec["color_modes"])
        try:
            out.append(cls(**kwargs))
        except TypeError:
            # This EntityInfo subclass doesn't accept one of the optional
            # kwargs — fall back to base fields (still valid for isinstance).
            out.append(cls(key=i + 1, name=f"{kind}{i}", object_id=f"{kind}{i}"))
    return out


def _classify(plugin_mod, kinds):
    # _classify_node_type reads self._PRIMARY_TYPE_PRIORITY — pass a stub-self
    # carrying that class attribute so the real method logic runs unchanged.
    P = plugin_mod.Plugin
    stub = types.SimpleNamespace(_PRIMARY_TYPE_PRIORITY=P._PRIMARY_TYPE_PRIORITY)
    return P._classify_node_type(stub, _build_entities(kinds))


# ── Per-node contract ────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_zoo_classification(plugin_mod, case):
    type_id, _primary = _classify(plugin_mod, case.entity_kinds)
    assert type_id == case.expect_type, (
        f"{case.name}: got {type_id}, expected {case.expect_type} ({case.note})"
    )


# ── Invariants ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_invariant_only_known_types(plugin_mod, case):
    type_id, _ = _classify(plugin_mod, case.entity_kinds)
    assert type_id in KNOWN_TYPES, f"{case.name}: emitted unknown type {type_id!r}"


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_invariant_priority_respected(plugin_mod, case):
    """Highest-priority genuine control wins; a status-LED Light is demoted on a
    metering or diagnostic node; a control-less node with sensors becomes
    esphomeSensor; otherwise the generic esphomeNode."""
    type_id, _ = _classify(plugin_mod, case.entity_kinds)
    kinds = _kinds(case.entity_kinds)
    present = [k for k in PRIORITY if k in kinds]
    if "light" in present and (_has_metering(case.entity_kinds) or _light_is_diag(case.entity_kinds)):
        present = [k for k in present if k != "light"]
    if present:
        assert type_id == KIND_TO_TYPE[present[0]], (
            f"{case.name}: top primary is {present[0]} but got {type_id}"
        )
    elif _has_sensor(case.entity_kinds):
        assert type_id == "esphomeSensor", (
            f"{case.name}: no control but has a sensor — expected esphomeSensor, got {type_id}"
        )
    else:
        assert type_id == "esphomeNode", (
            f"{case.name}: no control and no sensor — expected esphomeNode, got {type_id}"
        )


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_invariant_primary_entity_matches(plugin_mod, case):
    """When a primary type is chosen the returned primary entity is an instance
    of the matching class; a sensor node's primary is a SensorInfo; a plain node
    has None."""
    type_id, primary = _classify(plugin_mod, case.entity_kinds)
    if type_id == "esphomeNode":
        assert primary is None, f"{case.name}: node should have no primary entity"
    elif type_id == "esphomeSensor":
        assert isinstance(primary, aioesphomeapi.SensorInfo), (
            f"{case.name}: sensor-node primary should be a SensorInfo"
        )
    else:
        kind = next(k for k, t in KIND_TO_TYPE.items() if t == type_id)
        cls = getattr(aioesphomeapi, KIND_TO_CLASS[kind])
        assert isinstance(primary, cls), f"{case.name}: primary entity is not a {cls.__name__}"


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_invariant_deterministic(plugin_mod, case):
    a, _ = _classify(plugin_mod, case.entity_kinds)
    b, _ = _classify(plugin_mod, case.entity_kinds)
    assert a == b, f"{case.name}: non-deterministic ({a} then {b})"
