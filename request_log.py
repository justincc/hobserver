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
the server ever sees a failure — so nothing else here would report one. There
is no access log to filter under either mode: werkzeug is present only as the
reloader `--dev` wraps waitress in (ADR 14), and `QuietWerkzeugFilter` trims
what that says.
"""

from __future__ import annotations

import logging
import threading
import time

STATUS_PATH = "/_status"
# The status page refreshes itself, so watching it does not mean watching a
# frozen number. Same cadence as the turns index.
REFRESH_SECONDS = 3


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


# Logged by the reloader supervisor before it spawns each worker.
_RESTART_NOTICE = "* Restarting with "


class QuietWerkzeugFilter(logging.Filter):
    """Drops the reloader's restart notice, leaving "Detected change in ...,
    reloading" — the line worth reading under `--dev`.

    The notice names the strategy files are watched with rather than anything
    that happened, and at startup nothing has restarted at all. Every real
    restart is already announced by the change line that causes it.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.getMessage().lstrip().startswith(_RESTART_NOTICE)
