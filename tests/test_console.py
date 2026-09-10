"""Post-banner console lines carry the same clock as the logged error lines."""

import logging
import re

from console import CLOCK_FORMAT, LOG_FORMAT, console


def test_console_stamps_the_line_with_a_clock(capsys):
    console("Turns: ATOF index reused from cache (662,786 lines)")
    out = capsys.readouterr().out
    assert re.fullmatch(
        r"\[\d\d:\d\d:\d\d\] Turns: ATOF index reused from cache \(662,786 lines\)\n",
        out,
    )


def test_the_clock_matches_the_error_log_format():
    # One format for both sides so a timestamped print and a logged 404 line
    # up; app.py feeds CLOCK_FORMAT straight into logging.basicConfig.
    record = logging.LogRecord("x", logging.WARNING, "", 0, "m", None, None)
    stamped = logging.Formatter("[%(asctime)s]", datefmt=CLOCK_FORMAT).format(record)
    assert re.fullmatch(r"\[\d\d:\d\d:\d\d\]", stamped)


def test_the_error_log_names_its_emitting_logger():
    # The whole point of LOG_FORMAT over a bare message: a line says where it
    # came from, so waitress's own warnings can be told from this app's.
    line = logging.Formatter(LOG_FORMAT, datefmt=CLOCK_FORMAT).format(
        logging.LogRecord("waitress.queue", logging.WARNING, "", 0,
                          "Task queue depth is 1", None, None))
    assert re.fullmatch(
        r"\[\d\d:\d\d:\d\d\] WARNING waitress\.queue: Task queue depth is 1",
        line,
    )
