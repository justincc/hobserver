"""Response logging, access-log suppression, and the /_status tally."""

import logging
import re

from request_log import (STATUS_PATH, RequestStats, SuppressAccessLogFilter,
                         format_status, log_error_response)


class FakeLogger:
    """Records (level, formatted message) instead of emitting."""

    def __init__(self):
        self.lines = []

    def log(self, level, msg, *args):
        self.lines.append((level, msg % args))


def access_record(path, code=200, method="GET"):
    """A record shaped exactly as werkzeug 3.x emits one: the request line
    and code are logging args, and the code is a string."""
    return logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=1,
        msg='127.0.0.1 - - [20/Jul/2026 12:00:00] "%s" %s %s',
        args=(f"{method} {path} HTTP/1.1", str(code), "-"), exc_info=None)


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


def test_every_access_line_is_dropped():
    f = SuppressAccessLogFilter()
    # including the failures: the app logs those itself, and one response
    # reported twice is what --dev would otherwise print
    for code in (200, 302, 404, 500):
        assert f.filter(access_record("/turns/", code=code)) is False


def test_non_access_records_pass_through():
    f = SuppressAccessLogFilter()
    # the reloader's own messages are the reason --dev keeps this logger
    for message in ("Restarting with stat", "Detected change in 'app.py'"):
        record = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                                   message, None, None)
        assert f.filter(record) is True


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
