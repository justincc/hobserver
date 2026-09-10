"""The shared timestamp display: local wall-clock, self-labelled, one format."""

import time

import pytest

from timefmt import local_time, local_time_s, local_zone

# 2026-07-10 12:00:00 UTC, as both units the filters take.
_UTC_NOON_S = 1783684800.0
_UTC_NOON_US = int(_UTC_NOON_S * 1_000_000)


@pytest.fixture
def utc_tz(monkeypatch):
    """Pin the process to UTC so the expected string is fixed on any machine."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    # tzset() again on teardown restores the machine's zone from the unset var.
    monkeypatch.undo()
    time.tzset()


def test_microseconds_render_local_wall_clock_with_a_zone(utc_tz):
    assert local_time(_UTC_NOON_US) == "2026-07-10 12:00:00 UTC"


def test_seconds_render_the_same_instant_the_same_way(utc_tz):
    assert local_time_s(_UTC_NOON_S) == local_time(_UTC_NOON_US)


def test_the_zone_follows_the_machine(monkeypatch):
    # The point of the filter: the reader's own clock, not UTC. A fixed
    # non-UTC zone moves the hour and changes the label.
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        assert local_time(_UTC_NOON_US) == "2026-07-10 08:00:00 EDT"
    finally:
        monkeypatch.undo()
        time.tzset()


def test_zone_false_drops_the_label_for_a_table_cell(utc_tz):
    # A column of times names its zone once in the header, so the cells go bare.
    assert local_time(_UTC_NOON_US, zone=False) == "2026-07-10 12:00:00"
    assert local_time_s(_UTC_NOON_S, zone=False) == "2026-07-10 12:00:00"


def test_local_zone_is_the_header_label(utc_tz):
    assert local_zone() == "UTC"


def test_missing_time_is_an_em_dash():
    assert local_time(None) == "—"
    assert local_time_s(None) == "—"
