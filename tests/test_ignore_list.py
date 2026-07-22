#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_ignore_list.py
# Description: Tests for the v0.8.0 ignore list. The live case: SMLIGHT
#              SMHUB/SLZB Zigbee coordinators advertise _esphomelib._tcp but
#              never answer the ESPHome handshake, so without an ignore list
#              the plugin warned about them every hour, forever.
# Author:      CliveS & Claude Opus 4.8
# Date:        22-07-2026
# Version:     1.0

from __future__ import annotations

import asyncio

SMHUB_MAC = "62B42C02FE70"
SMHUB     = {
    "hostname": "rtos-smhub-02fe70", "ip": "192.168.1.172", "port": 6053,
    "version": "", "platform": "", "board": "", "first_seen": 0,
}


# --------------------------------------------------------------------------
# parse_ignore_list
# --------------------------------------------------------------------------

def test_parse_blank_and_junk_is_safe(plugin_mod):
    assert plugin_mod.parse_ignore_list("") == set()
    assert plugin_mod.parse_ignore_list(None) == set()
    assert plugin_mod.parse_ignore_list("  ,  , ") == set()


def test_parse_mac_notations_normalise(plugin_mod):
    for raw in ("62B42C02FE70", "62:b4:2c:02:fe:70", "62-B4-2C-02-FE-70"):
        assert plugin_mod.parse_ignore_list(raw) == {"62B42C02FE70"}, raw


def test_parse_ip_is_not_mistaken_for_mac(plugin_mod):
    # 192.168.100.172 strips to 12 hex-ish chars — it must stay an IP token.
    assert plugin_mod.parse_ignore_list("192.168.100.172") == {"192.168.100.172"}


def test_parse_hostname_lowercased_and_local_stripped(plugin_mod):
    assert plugin_mod.parse_ignore_list("SLZB-06.local") == {"slzb-06"}
    assert plugin_mod.parse_ignore_list("slzb-06") == {"slzb-06"}


def test_parse_mixed_list_with_commas_and_spaces(plugin_mod):
    got = plugin_mod.parse_ignore_list("62:b4:2c:02:fe:70, 192.168.1.173  slzb-06.local")
    assert got == {"62B42C02FE70", "192.168.1.173", "slzb-06"}


# --------------------------------------------------------------------------
# _is_ignored matching
# --------------------------------------------------------------------------

def test_is_ignored_matches_mac_host_and_ip(plugin, plugin_mod):
    plugin.discovered[SMHUB_MAC] = dict(SMHUB)
    for token in (SMHUB_MAC, "rtos-smhub-02fe70", "192.168.1.172"):
        plugin.ignored_tokens = plugin_mod.parse_ignore_list(token)
        assert plugin._is_ignored(SMHUB_MAC), token
    plugin.ignored_tokens = plugin_mod.parse_ignore_list("AABBCC001122 192.168.1.99")
    assert not plugin._is_ignored(SMHUB_MAC)


def test_is_ignored_empty_list_is_cheap_no(plugin):
    plugin.ignored_tokens = set()
    assert not plugin._is_ignored(SMHUB_MAC)


# --------------------------------------------------------------------------
# Gates: no probe, no park, no un-park
# --------------------------------------------------------------------------

def test_should_connect_refuses_ignored(plugin):
    plugin.discovered[SMHUB_MAC] = dict(SMHUB)
    assert plugin._should_connect(SMHUB_MAC)
    plugin.ignored_tokens = {SMHUB_MAC}
    assert not plugin._should_connect(SMHUB_MAC)


def test_connect_task_exits_immediately_when_ignored(plugin):
    plugin.discovered[SMHUB_MAC] = dict(SMHUB)
    plugin.ignored_tokens = {SMHUB_MAC}
    asyncio.run(plugin._connect_to_device(SMHUB_MAC))
    assert SMHUB_MAC not in plugin.connections
    assert SMHUB_MAC not in plugin.parked


def test_sweep_parked_drops_ignored_instead_of_unparking(plugin):
    plugin.discovered[SMHUB_MAC] = dict(SMHUB)
    plugin.parked[SMHUB_MAC] = {"reason": "timeout", "since": 0, "failures": 3}
    plugin.ignored_tokens = {SMHUB_MAC}
    plugin._sweep_parked()   # since=0 means back-off long elapsed
    assert SMHUB_MAC not in plugin.parked
    assert SMHUB_MAC not in plugin.connections


# --------------------------------------------------------------------------
# Prefs change applies without a restart
# --------------------------------------------------------------------------

def test_closed_prefs_reparses_and_drops_parked(plugin):
    plugin.discovered[SMHUB_MAC] = dict(SMHUB)
    plugin.parked[SMHUB_MAC] = {"reason": "timeout", "since": 0, "failures": 3}
    plugin.closedPrefsConfigUi(
        {"autoCreateDevices": True, "logLevel": "DEBUG",
         "ignoredDevices": "62:b4:2c:02:fe:70"},
        userCancelled=False,
    )
    assert plugin.ignored_tokens == {SMHUB_MAC}
    # No running asyncio loop in the test, so apply the loop-side step directly.
    plugin._apply_ignore_list_now()
    assert SMHUB_MAC not in plugin.parked


def test_closed_prefs_cancelled_changes_nothing(plugin):
    plugin.ignored_tokens = {SMHUB_MAC}
    plugin.closedPrefsConfigUi({"ignoredDevices": ""}, userCancelled=True)
    assert plugin.ignored_tokens == {SMHUB_MAC}
