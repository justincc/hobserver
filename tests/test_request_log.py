"""Response logging, reloader-notice suppression, and the /_status tally."""

import logging
import re

from request_log import (STATUS_PATH, RequestStats, QuietWerkzeugFilter,
                         format_status, log_error_response)


class FakeLogger:
    """Records (level, formatted message) instead of emitting."""

    def __init__(self):
        self.lines = []

    def log(self, level, msg, *args):
        self.lines.append((level, msg % args))


def test_successful_responses_are_not_logged():
    logger = FakeLogger()
    # every 2xx/3xx is silent, on any path — polled or not, first hit or not
    for path in ("/turns/", "/memory/mem0/", STATUS_PATH):
        for code in (200, 204, 302, 304):
            assert log_error_response(logger, "GET", path, code) is False
    assert logger.lines == []


def test_errors_are_logged_with_the_server_out_of_it():
    # the failures a quiet console must never hide — logged by the app, so
    # they say the same thing whichever server is running
    logger = FakeLogger()
    assert log_error_response(logger, "GET", "/turns/999", 404) is True
    assert log_error_response(logger, "POST", "/memory/mem0/", 500) is True
    assert logger.lines == [(logging.WARNING, "GET /turns/999 -> 404"),
                            (logging.ERROR, "POST /memory/mem0/ -> 500")]


def test_server_error_and_client_error_differ_in_level():
    # a 404 is usually a stale URL; a 500 is this app failing
    logger = FakeLogger()
    log_error_response(logger, "GET", "/turns/", 404)
    log_error_response(logger, "GET", "/turns/", 503)
    assert [level for level, _ in logger.lines] == [logging.WARNING,
                                                    logging.ERROR]


def werkzeug_record(message):
    """One of the reloader's messages, as werkzeug logs it."""
    return logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                             message, None, None)


def test_the_change_that_caused_a_reload_is_kept():
    # the one line worth reading under --dev, and the reason this logger is
    # filtered rather than silenced
    f = QuietWerkzeugFilter()
    assert f.filter(werkzeug_record(
        " * Detected change in '/app/app.py', reloading")) is True


def test_the_restart_notice_is_dropped():
    # it names how files are watched, not anything that happened, and at
    # startup nothing has restarted at all
    f = QuietWerkzeugFilter()
    assert f.filter(werkzeug_record(" * Restarting with stat")) is False
    assert f.filter(werkzeug_record(
        " * Restarting with watchdog (inotify)")) is False


def test_stats_count_paths_and_statuses():
    ticks = iter([100.0, 101.0, 102.0, 103.0, 110.0, 110.0, 110.0, 110.0])
    stats = RequestStats(clock=lambda: next(ticks))
    stats.record("/turns/", 200)
    stats.record("/turns/", 200)
    stats.record("/turns/", 500)
    snap = stats.snapshot()
    assert snap["total"] == 3
    entry = snap["paths"][0]
    assert entry["path"] == "/turns/"
    assert entry["count"] == 3
    assert entry["statuses"] == {200: 2, 500: 1}


def test_status_text_reports_counts_and_staleness():
    ticks = iter([0.0, 1.0, 200.0, 200.0, 200.0])
    stats = RequestStats(clock=lambda: next(ticks))
    stats.record("/turns/", 200)
    text = format_status(stats.snapshot())
    assert "/turns/" in text and "200x1" in text
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
