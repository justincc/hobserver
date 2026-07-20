"""Dev-server request visibility: quiet console, tally on demand.

The live-poll pages refetch every 2-3 s (timing index 3 s, a live turn 2 s,
the memory fragment 3 s), which buries the console in identical lines. So a
successful poll of a path is logged once and then suppressed, and the
running tally moves to /_status.

The point of keeping the tally at all: when a page stops updating, the first
question is whether requests are still arriving, and a silenced log cannot
answer it. Fetching /_status answers it three ways at once — no response
means the server is down, a stale "last" time means the browser stopped
polling, and non-200 statuses mean the polls are arriving but failing.

Only successful (2xx/3xx) polls are ever suppressed. Errors always reach the
console, since those are what a quiet log must never hide.
"""

from __future__ import annotations

import logging
import re
import threading
import time

STATUS_PATH = "/_status"
# The status page refreshes itself, so watching it does not mean watching a
# frozen number. Same cadence as the timing index.
REFRESH_SECONDS = 3

# The polled routes. /timing/turn/<session>/<start_us> is per turn, so each
# turn page announces itself once before going quiet. /_status is here for
# the same reason as the rest: left open it polls, and its own refreshes
# must not be what fills the console.
_POLL_PATHS = (
    re.compile(r"^/timing/$"),
    re.compile(r"^/timing/turn/[^/]+/\d+$"),
    re.compile(r"^/memory/fragment/events$"),
    re.compile(rf"^{re.escape(STATUS_PATH)}$"),
)
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def is_poll_path(path: str) -> bool:
    path = path.split("?", 1)[0]
    return any(p.match(path) for p in _POLL_PATHS)


def _humanize(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


class RequestStats:
    """Per-path request counts, thread-safe: the dev server serves requests
    on threads, and /_status reads while they write."""

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


def _parse(record: logging.LogRecord):
    """(path, status) from a werkzeug access record, or None if it is not
    one. Werkzeug logs '"%s" %s %s' % (requestline, code, size), the code as
    a string and the request line ANSI-styled for non-200 responses."""
    args = record.args
    if not isinstance(args, tuple) or len(args) != 3:
        return None
    request_line, code = _ANSI.sub("", str(args[0])), str(args[1])
    parts = request_line.split(" ")
    if len(parts) != 3 or not code.isdigit():
        return None
    return parts[1], int(code)


class PollLogFilter(logging.Filter):
    """Logs a path's first successful poll, then one pointer to /_status,
    then nothing. Anything that is not a successful poll passes through."""

    def __init__(self, status_url: str):
        super().__init__()
        self.status_url = status_url
        self._lock = threading.Lock()
        self._seen: dict = {}
        self._announced = False

    def filter(self, record: logging.LogRecord) -> bool:
        parsed = _parse(record)
        if parsed is None:
            return True
        path, status = parsed
        if not (200 <= status < 400) or not is_poll_path(path):
            return True
        with self._lock:
            first = path not in self._seen
            self._seen[path] = True
            if first:
                return True
            if self._announced:
                return False
            self._announced = True
        # Rewriting this record rather than emitting a second one keeps the
        # notice inside the filter, with no re-entrant logging call.
        record.msg = ("  ...further successful polls are suppressed. "
                      f"Request counts: {self.status_url}")
        record.args = ()
        return True
