#! /usr/bin/env python
# -*- coding: utf-8 -*-
# Filename:    test_sql_logger_churn.py
# Description: v0.8.5. lastSeen is rewritten on every packet, so SQL Logger stored
#              a history row every couple of seconds per node (4.3 million rows on
#              one freezer plug in three months), which made its daily prune hold
#              the database lock long enough to drop other devices' history.
#              deviceStartComm adds the churn states to the device's
#              sqlLoggerIgnoreStates shared prop, without overriding the user.
# Author:      CliveS & Claude Opus 5.5
# Date:        23-09-2026
# Version:     1.0


def test_an_empty_list_gets_every_churn_state(plugin_mod):
    merged = plugin_mod.merge_sql_logger_ignore("")
    assert [t.strip() for t in merged.split(",")] == list(plugin_mod.SQL_LOGGER_CHURN_STATES)


def test_the_users_entries_are_kept_first(plugin_mod):
    merged = plugin_mod.merge_sql_logger_ignore("voltage, Current")
    parts = [t.strip() for t in merged.split(",")]
    assert parts[:2] == ["voltage", "Current"]
    assert set(plugin_mod.SQL_LOGGER_CHURN_STATES) <= set(parts)


def test_a_complete_list_is_left_alone_whatever_its_case(plugin_mod):
    full = ", ".join(s.upper() for s in plugin_mod.SQL_LOGGER_CHURN_STATES)
    assert plugin_mod.merge_sql_logger_ignore(full) is None


def test_ignore_everything_is_never_narrowed(plugin_mod):
    assert plugin_mod.merge_sql_logger_ignore("*") is None
    assert plugin_mod.merge_sql_logger_ignore(" * ") is None


def test_start_comm_writes_the_list_once(plugin, fake_device, plugin_mod):
    dev = fake_device(device_type_id=sorted(plugin._OUR_DEVICE_TYPES)[0])
    plugin.deviceStartComm(dev)
    assert dev.shared_writes == 1
    assert "lastSeen" in dev.sharedProps["sqlLoggerIgnoreStates"]
    plugin.deviceStartComm(dev)               # a restart re-checks, does not rewrite
    assert dev.shared_writes == 1


def test_start_comm_leaves_a_users_star_alone(plugin, fake_device):
    dev = fake_device(device_type_id=sorted(plugin._OUR_DEVICE_TYPES)[0])
    dev.sharedProps = {"sqlLoggerIgnoreStates": "*"}
    plugin.deviceStartComm(dev)
    assert dev.shared_writes == 0
    assert dev.sharedProps["sqlLoggerIgnoreStates"] == "*"


def test_a_failed_write_is_a_warning_not_a_crash(plugin, fake_device):
    dev = fake_device(device_type_id=sorted(plugin._OUR_DEVICE_TYPES)[0])

    def boom(props):
        raise RuntimeError("server said no")
    dev.replaceSharedPropsOnServer = boom
    plugin.deviceStartComm(dev)               # must not raise
    assert any("SQL Logger ignore list" in r.getMessage() for r in plugin.log_records)
