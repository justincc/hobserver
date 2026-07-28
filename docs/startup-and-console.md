# Startup, data sources and the console

## Resolving the two sources

No arguments or env vars are needed when `HERMES_HOME` is exported: both the
memory db and the ATOF log default to paths under `hermes_config_dir()`
(normalized `$HERMES_HOME`, else a literal fallback).

- memory db — first argv, else `JMEM0_DB`, else the default
- ATOF log — `ATOF_LOG`, else the default

README.md gives the exact paths.

## Checking the db before serving

`plugins.memory.check_db` gates the memory db: it must exist, be a regular
file, and yield a row from `events` over a read-only connection. Otherwise the
banner marks it UNUSABLE — dropping the "listening" lines, since it will not —
and main exits naming the problem.

An existence check alone was not enough. `app.py .` made DB_PATH the
*directory*, which exists; sqlite reads the file header at connect, so every
request died with a bare `disk I/O error` (EISDIR) that reads like failing
hardware rather than a wrong path.

The ATOF log stays exists-only: it is allowed to be missing, and the Prompts
tab says so itself.

## The startup banner

Prints the resolved paths and whether each exists, once — in the reloader
supervisor, since the worker sets `WERKZEUG_RUN_MAIN`. Its `status` line tells
the user successful requests are not logged, and points at `/_status`.

## Console noise

`request_log.py` keeps the console usable despite the 2-3 s live polls: a
`logging.Filter` (`SuppressSuccessFilter`) on the werkzeug access logger (dev
server only) drops every successful (2xx/3xx) response on every path, so only
errors are logged.

This was deliberately simplified from an earlier per-path
first-success-plus-pointer scheme. If fine-grained logging is ever wanted back,
add a setting rather than reviving the complexity.

The filter parses werkzeug's `'"%s" %s %s' % (request_line, code, size)` record
args, so it is coupled to the dev server's log shape — check it on a werkzeug
major bump.

## /_status

An always-on tally: an `after_request` hook into
`app.extensions["request_stats"]`, excluding `/_status` itself. It answers "is
anything still reaching the server?" once the log is quiet:

- no response — server down
- stale last-seen — browser stopped polling
- non-200s — polls are failing

It self-refreshes via a `Refresh` header rather than the live-poll script,
which keeps the body plain text for curl and needs no template. The header line
carries a clock, because counts look the same live or frozen.

## Serving

Port 5090. Template and `.py` edits are picked up without a restart — template
auto-reload plus the Werkzeug reloader. Debug stays off, so the interactive
debugger is never exposed on 0.0.0.0.

Producer-side setup (nemo-relay install, plugin enable, `HERMES_NEMO_RELAY_*`
in `~/.hermes/.env`) is in [setup-prompt-timing.md](setup-prompt-timing.md).
