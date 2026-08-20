"""Post-banner console lines carry the same clock as the logged error lines."""

import logging
import re

from console import CLOCK_FORMAT, console


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
