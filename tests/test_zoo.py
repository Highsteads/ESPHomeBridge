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
    "esphomeNode", "esphomeSwitch", "esphomeLight", "esphomeFan",
    "esphomeCover", "esphomeClimate", "esphomeLock",
}
# Priority order high->low; the type a node gets when that kind is its top entity.
PRIORITY = ["lock", "climate", "switch", "light", "fan", "cover"]
KIND_TO_TYPE = {
    "lock": "esphomeLock", "climate": "esphomeClimate", "switch": "esphomeSwitch",
    "light": "esphomeLight", "fan": "esphomeFan", "cover": "esphomeCover",
}

_IDS = [c.name for c in CASES]


def _build_entities(kinds):
    """Turn a list of kind strings into real aioesphomeapi EntityInfo objects."""
    out = []
    for i, kind in enumerate(kinds):
        cls = getattr(aioesphomeapi, KIND_TO_CLASS[kind])
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
    """The chosen type matches the HIGHEST-priority primary kind present; if none
    of the six primary kinds is present, it must be the generic node."""
    type_id, _ = _classify(plugin_mod, case.entity_kinds)
    present = [k for k in PRIORITY if k in case.entity_kinds]
    if present:
        assert type_id == KIND_TO_TYPE[present[0]], (
            f"{case.name}: top primary is {present[0]} but got {type_id}"
        )
    else:
        assert type_id == "esphomeNode", f"{case.name}: no primary kind but got {type_id}"


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_invariant_primary_entity_matches(plugin_mod, case):
    """When a primary type is chosen the returned primary entity is an instance
    of the matching class; for a node it is None."""
    type_id, primary = _classify(plugin_mod, case.entity_kinds)
    if type_id == "esphomeNode":
        assert primary is None, f"{case.name}: node should have no primary entity"
    else:
        kind = next(k for k, t in KIND_TO_TYPE.items() if t == type_id)
        cls = getattr(aioesphomeapi, KIND_TO_CLASS[kind])
        assert isinstance(primary, cls), f"{case.name}: primary entity is not a {cls.__name__}"


@pytest.mark.parametrize("case", CASES, ids=_IDS)
def test_invariant_deterministic(plugin_mod, case):
    a, _ = _classify(plugin_mod, case.entity_kinds)
    b, _ = _classify(plugin_mod, case.entity_kinds)
    assert a == b, f"{case.name}: non-deterministic ({a} then {b})"
