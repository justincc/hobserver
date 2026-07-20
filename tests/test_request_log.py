"""Poll-log suppression and the /_status tally."""

import logging
import re

from request_log import (STATUS_PATH, PollLogFilter, RequestStats,
                         format_status, is_poll_path)


def access_record(path, code=200, method="GET"):
    """A record shaped exactly as werkzeug 3.x emits one: the request line
    and code are logging args, and the code is a string."""
    return logging.LogRecord(
        name="werkzeug", level=logging.INFO, pathname=__file__, lineno=1,
        msg='127.0.0.1 - - [20/Jul/2026 12:00:00] "%s" %s %s',
        args=(f"{method} {path} HTTP/1.1", str(code), "-"), exc_info=None)


def test_poll_paths_recognised():
    assert is_poll_path("/timing/")
    assert is_poll_path("/timing/turn/s7/1784554412841811")
    assert is_poll_path("/memory/fragment/events?since=12")
    # pages that are not polled keep logging every visit
    assert not is_poll_path("/memory/")
    assert not is_poll_path("/memory/event/3")
    # /_status refreshes itself, so it is suppressed like the rest
    assert is_poll_path(STATUS_PATH)


def test_first_poll_logs_then_one_notice_then_silence():
    f = PollLogFilter("http://localhost:5090/_status")
    first = access_record("/timing/")
    assert f.filter(first) is True
    assert "GET /timing/" in first.getMessage()   # logged unchanged

    notice = access_record("/timing/")
    assert f.filter(notice) is True
    assert "suppressed" in notice.getMessage()
    assert "http://localhost:5090/_status" in notice.getMessage()

    # everything after is silent, and the notice is not repeated
    assert all(f.filter(access_record("/timing/")) is False for _ in range(5))


def test_each_polled_path_announces_itself_once():
    f = PollLogFilter("u")
    assert f.filter(access_record("/timing/")) is True
    f.filter(access_record("/timing/"))                    # notice
    # a different turn page is a different path: its first hit still shows
    assert f.filter(access_record("/timing/turn/s7/12")) is True
    assert f.filter(access_record("/timing/turn/s7/12")) is False


def test_errors_are_never_suppressed():
    f = PollLogFilter("u")
    for _ in range(3):
        f.filter(access_record("/timing/"))
    # the failure a silenced log must never hide
    assert f.filter(access_record("/timing/", code=500)) is True
    assert f.filter(access_record("/timing/", code=404)) is True


def test_unpolled_paths_always_log():
    f = PollLogFilter("u")
    assert all(f.filter(access_record("/memory/")) is True for _ in range(5))


def test_non_access_records_pass_through():
    f = PollLogFilter("u")
    other = logging.LogRecord("werkzeug", logging.INFO, __file__, 1,
                              "Restarting with stat", None, None)
    assert f.filter(other) is True


def test_stats_count_paths_and_statuses():
    ticks = iter([100.0, 101.0, 102.0, 103.0, 110.0, 110.0, 110.0, 110.0])
    stats = RequestStats(clock=lambda: next(ticks))
    stats.record("/timing/", 200)
    stats.record("/timing/", 200)
    stats.record("/timing/", 500)
    snap = stats.snapshot()
    assert snap["total"] == 3
    entry = snap["paths"][0]
    assert entry["path"] == "/timing/"
    assert entry["count"] == 3
    assert entry["statuses"] == {200: 2, 500: 1}


def test_status_text_reports_counts_and_staleness():
    ticks = iter([0.0, 1.0, 200.0, 200.0, 200.0])
    stats = RequestStats(clock=lambda: next(ticks))
    stats.record("/timing/", 200)
    text = format_status(stats.snapshot())
    assert "/timing/" in text and "200x1" in text
    # a stale "last" is how a stopped browser shows up
    assert "ago" in text
    assert "uptime" in text


def test_status_text_when_nothing_has_arrived():
    stats = RequestStats(clock=lambda: 0.0)
    assert "no requests recorded yet" in format_status(stats.snapshot())


def test_status_page_repeats_are_suppressed_too():
    # left open it refreshes itself, so its own polls must not fill the
    # console any more than the pages it reports on
    f = PollLogFilter("u")
    assert f.filter(access_record(STATUS_PATH)) is True
    f.filter(access_record(STATUS_PATH))               # notice
    assert all(f.filter(access_record(STATUS_PATH)) is False for _ in range(4))


def test_status_text_carries_a_ticking_clock():
    # counts alone look identical whether the page is live or frozen
    stats = RequestStats(clock=lambda: 0.0)
    text = format_status(stats.snapshot())
    assert re.search(r"\d\d:\d\d:\d\d", text)
    assert "refreshes every" in text
    # the page must not be mistaken for a view of agent activity
    assert "not agent activity" in text
