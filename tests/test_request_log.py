"""Successful-response suppression and the /_status tally."""

import logging
import re

from request_log import (STATUS_PATH, RequestStats, SuppressSuccessFilter,
                         format_status)


def access_record(path, code=200, method="GET"):
    """A record shaped exactly as werkzeug 3.x emits one: the request line
    and code are logging args, and the code is a string."""
    return logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=1,
        msg='127.0.0.1 - - [20/Jul/2026 12:00:00] "%s" %s %s',
        args=(f"{method} {path} HTTP/1.1", str(code), "-"), exc_info=None)


def test_successful_responses_are_dropped():
    f = SuppressSuccessFilter()
    # every 2xx/3xx is silent, on any path — polled or not, first hit or not
    for path in ("/prompts/", "/memory/mem0/", STATUS_PATH):
        assert all(f.filter(access_record(path)) is False for _ in range(5))
    assert f.filter(access_record("/prompts/", code=302)) is False


def test_errors_are_never_suppressed():
    f = SuppressSuccessFilter()
    # the failures a silenced log must never hide
    assert f.filter(access_record("/prompts/", code=500)) is True
    assert f.filter(access_record("/prompts/", code=404)) is True


def test_non_access_records_pass_through():
    f = SuppressSuccessFilter()
    other = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                              "Restarting with stat", None, None)
    assert f.filter(other) is True


def test_stats_count_paths_and_statuses():
    ticks = iter([100.0, 101.0, 102.0, 103.0, 110.0, 110.0, 110.0, 110.0])
    stats = RequestStats(clock=lambda: next(ticks))
    stats.record("/prompts/", 200)
    stats.record("/prompts/", 200)
    stats.record("/prompts/", 500)
    snap = stats.snapshot()
    assert snap["total"] == 3
    entry = snap["paths"][0]
    assert entry["path"] == "/prompts/"
    assert entry["count"] == 3
    assert entry["statuses"] == {200: 2, 500: 1}


def test_status_text_reports_counts_and_staleness():
    ticks = iter([0.0, 1.0, 200.0, 200.0, 200.0])
    stats = RequestStats(clock=lambda: next(ticks))
    stats.record("/prompts/", 200)
    text = format_status(stats.snapshot())
    assert "/prompts/" in text and "200x1" in text
    # a stale "last" is how a stopped browser shows up
    assert "ago" in text
    assert "uptime" in text


def test_status_text_when_nothing_has_arrived():
    stats = RequestStats(clock=lambda: 0.0)
    assert "no requests recorded yet" in format_status(stats.snapshot())


def test_status_text_carries_a_ticking_clock():
    # counts alone look identical whether the page is live or frozen
    stats = RequestStats(clock=lambda: 0.0)
    text = format_status(stats.snapshot())
    assert re.search(r"\d\d:\d\d:\d\d", text)
    assert "refreshes every" in text
    # the page must not be mistaken for a view of agent activity
    assert "not agent activity" in text
