"""One way to write a timestamp on screen, shared by every tab.

Times reach templates as epoch — microseconds on an ATOF span (`start_us`),
seconds in the mem0 db (`ts_epoch`) — and were being rendered ad hoc, in UTC,
with the zone spelled out in the template beside each call. That drifts: two
tabs showing the same instant two ways, and no one place to change how a time
reads.

So the format lives here and is registered on the app in `create_app`: the
filters `local_time` (microseconds) and `local_time_s` (seconds), plus the
`local_zone` global. That puts them in reach of every plugin's templates
without one plugin importing another's (design principle 2). The rendering is
the machine's **local** zone, so a reader of a log from a resident agent on
this host sees wall-clock time, not UTC arithmetic.

**Where the zone label goes.** A lone timestamp carries its own zone
(`… 12:36:14 BST`). A column of them does not: the cells stay bare and the
column header names the zone once (`start (BST)`) — pass `zone=False` to the
filter for the cells and `local_zone()` to the header. So call the filter
plain for a single value, with `zone=False` inside a table of many.

Local, not the viewer's browser: hobserver is server-rendered and read on the
same trusted host it runs on (SECURITY.md), so the server's clock is the
reader's clock. A remote viewer would want browser-local, which would mean
rendering client-side; that trade is not worth making until there is one.
"""

from __future__ import annotations

from datetime import datetime, timezone

_WITH_ZONE = "%Y-%m-%d %H:%M:%S %Z"
_BARE = "%Y-%m-%d %H:%M:%S"
_MISSING = "—"


def _local(epoch_seconds: float) -> datetime:
    """The instant as an aware datetime in the machine's local zone."""
    return datetime.fromtimestamp(epoch_seconds, tz=timezone.utc).astimezone()


def _format(epoch_seconds: float, zone: bool) -> str:
    return _local(epoch_seconds).strftime(_WITH_ZONE if zone else _BARE)


def local_time(us, zone: bool = True) -> str:
    """Epoch microseconds → local wall-clock string, or an em dash if None.

    `zone=False` drops the trailing zone, for a table whose header names it.
    """
    if us is None:
        return _MISSING
    return _format(us / 1_000_000, zone)


def local_time_s(seconds, zone: bool = True) -> str:
    """Epoch seconds → local wall-clock string, or an em dash if None."""
    if seconds is None:
        return _MISSING
    return _format(seconds, zone)


def local_zone() -> str:
    """The current local zone abbreviation (e.g. `BST`), for a column header.

    Reads the zone as of now, so a table that straddles a DST change labels
    itself by the reader's present offset rather than each row's — the values
    stay correct wall-clock either way; only the one header word is a summary.
    """
    return datetime.now(timezone.utc).astimezone().strftime("%Z")
