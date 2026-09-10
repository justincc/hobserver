"""Post-banner console lines carry a clock; the banner does not.

The startup banner (app.py) is the resolved configuration — static, printed
once, deliberately clockless. Everything a run says *after* it — a plugin's
startup line, the ATOF index report — happened at a moment worth knowing, the
same as the error lines `logging` prints. `console()` gives those lines that
`[HH:MM:SS]` stamp so the two read as one console, and `CLOCK_FORMAT` pins the
format in one place so the print side and the logging side cannot drift apart.
"""

from __future__ import annotations

import time

# Shared with `logging.basicConfig(datefmt=...)` in app.py so a timestamped
# print and a logged error line show the clock the same way.
CLOCK_FORMAT = "%H:%M:%S"

# The error-log line format for `logging.basicConfig(format=...)`. `%(name)s`
# is the emitting logger's name, so a line's origin is visible on its face:
# `app` is this web app's own errors, `waitress.*` the server (task-queue
# depth, connection limits), `werkzeug` the `--dev` reloader. Worth the column
# because these share one console and read alike otherwise.
LOG_FORMAT = "[%(asctime)s] %(levelname)s %(name)s: %(message)s"


def console(message: str) -> None:
    """Print one timestamped console line, flushed, matching the error log."""
    print(f"[{time.strftime(CLOCK_FORMAT)}] {message}", flush=True)
