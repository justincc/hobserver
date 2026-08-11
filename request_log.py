"""Request visibility: quiet console, tally on demand.

The live-poll pages refetch every 2-3 s (turns index 3 s, a live turn 2 s,
the memory fragment 3 s), which buries the console in identical lines. So
successful (2xx/3xx) responses are not logged at all, and the running tally
of every request — successes included — moves to /_status.

The point of keeping the tally at all: when a page stops updating, the first
question is whether requests are still arriving, and a silenced log cannot
answer it. Fetching /_status answers it three ways at once — no response
means the server is down, a stale "last" time means the browser stopped
polling, and non-200 statuses mean the polls are arriving but failing.

Only successful responses are suppressed. Errors always reach the console,
since those are what a quiet log must never hide.

**The app logs those errors itself** (`log_error_response`, called from the
`after_request` hook), rather than leaving them to whatever is serving it.
Waitress logs no requests at all — not even a 404, which Flask handles before
the server ever sees a failure — so a console rule built on the server's
access log would say nothing in the mode this app normally runs in. Logging a
response where the app already has one keeps the rule true under both servers
and in the same shape, and leaves `SuppressAccessLogFilter` one job: dropping
the development server's duplicate access lines (ADR 14).
"""

from __future__ import annotations

import logging
import re
import threading
import time

STATUS_PATH = "/_status"
# The status page refreshes itself, so watching it does not mean watching a
# frozen number. Same cadence as the turns index.
REFRESH_SECONDS = 3

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def _humanize(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class RequestStats:
    """Per-path request counts, thread-safe: waitress serves requests on a
    pool of threads, and /_status reads while they write."""

    def __init__(self, clock=time.time):
        self._clock = clock
        self._lock = threading.Lock()
        self._paths: dict = {}
        self.started_at = clock()

    def record(self, path: str, status: int) -> None:
        now = self._clock()
        with self._lock:
            entry = self._paths.setdefault(
                path, {"count": 0, "last": now, "statuses": {}})
            entry["count"] += 1
            entry["last"] = now
            entry["statuses"][status] = entry["statuses"].get(status, 0) + 1

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "now": self._clock(),
                "uptime": self._clock() - self.started_at,
                "total": sum(e["count"] for e in self._paths.values()),
                "paths": [
                    {"path": path, "count": e["count"],
                     "since": self._clock() - e["last"],
                     "statuses": dict(e["statuses"])}
                    for path, e in sorted(self._paths.items(),
                                          key=lambda kv: -kv[1]["count"])
                ],
            }


def format_status(snapshot: dict) -> str:
    """Plain text, so it reads the same curled or opened in a browser."""
    # A ticking clock is what distinguishes a live page from a frozen one —
    # the counts alone look identical either way when traffic has stopped.
    stamp = time.strftime("%H:%M:%S", time.localtime(snapshot["now"]))
    lines = [
        f"hermes-observer status          {stamp}"
        f"  (refreshes every {REFRESH_SECONDS}s)",
        "  HTTP requests reaching this web app itself — not agent activity.",
        "",
        f"  uptime   {_humanize(snapshot['uptime'])}",
        f"  total    {snapshot['total']} requests"
        f"   (this page's own requests are excluded)",
        "",
    ]
    if not snapshot["paths"]:
        lines.append("  no requests recorded yet")
        return "\n".join(lines) + "\n"
    lines.append(f"  {'path':<48} {'count':>7}  {'last':>8}  statuses")
    for entry in snapshot["paths"]:
        statuses = " ".join(f"{code}x{n}"
                            for code, n in sorted(entry["statuses"].items()))
        lines.append(
            f"  {entry['path']:<48} {entry['count']:>7}  "
            f"{_humanize(entry['since']) + ' ago':>8}  {statuses}")
    return "\n".join(lines) + "\n"


def log_error_response(logger, method: str, target: str, status: int) -> bool:
    """Log a response the console must not hide. Returns whether it logged.

    Successes are the silent case — the tally at /_status is what accounts for
    those. 5xx is logged at error and 4xx at warning, so the two can be told
    apart by level: a 404 is usually a stale URL after a rename, while a 500 is
    this app failing to read what it was pointed at.
    """
    if 200 <= status < 400:
        return False
    logger.log(logging.ERROR if status >= 500 else logging.WARNING,
               "%s %s -> %s", method, target, status)
    return True


def _is_access_record(record: logging.LogRecord) -> bool:
    """Whether this is a werkzeug access line rather than one of its own
    messages ("Restarting with stat", "Detected change in ...").

    Werkzeug logs '"%s" %s %s' % (requestline, code, size), the code as a
    string and the request line ANSI-styled for non-200 responses. Only the
    shape is read, never the status: what to do with a request is decided by
    `log_error_response`, on the app's side of the log, where it holds for
    every server.
    """
    args = record.args
    if not isinstance(args, tuple) or len(args) != 3:
        return False
    request_line, code = _ANSI.sub("", str(args[0])), str(args[1])
    return len(request_line.split(" ")) == 3 and code.isdigit()


class SuppressAccessLogFilter(logging.Filter):
    """Drops every access-log line from the development server, successes and
    failures alike, leaving its own messages (the reloader's, notably) alone.

    All of them, not just the successful ones: the app logs its own errors now
    (`log_error_response`), so anything werkzeug adds here is the same response
    reported twice. Dropping the lot is what makes `--dev` and ordinary running
    print the same console.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not _is_access_record(record)
