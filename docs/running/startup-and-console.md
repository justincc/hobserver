# Startup, data sources and the console

## Loading the tabs

The first command-line argument, else `$OBSERVER_CONFIG`, else
`./observer.toml` names the config file; with none present the built-in
default list is used. `tabs.py` parses it and imports each enabled module. See
[plugins-and-urls.md](../extending/plugins-and-urls.md).

Only two things are fatal: a config file that cannot be parsed or is
internally impossible (two tabs at one URL prefix), and an empty tab bar. A
single broken tab never is.

## Resolving sources

Each plugin resolves its own, in this order: its `settings` from the config
file, then an environment variable, then a default under
`hermes_paths.hermes_config_dir()` — the normalized `$HERMES_HOME`, else
`~/.hermes/config`. So nothing has to be configured when `HERMES_HOME` is
exported, and an installation that keeps hermes in the conventional place
needs nothing either.

| tab | setting | env |
| --- | --- | --- |
| Turns | `atof_log` | `ATOF_LOG` |
| Turns | `index_db` | — |
| Mem0 | `db` | `JMEM0_DB` |

A plugin reports what it resolved through its `sources` hook, which is what
the banner prints — the shell knows none of the above.

## The ATOF index, in the banner

The Turns tab prints a second line, `ATOF index (cache)`, naming the SQLite
file it keeps beside nothing —
[ADR 11](../design/adr/0011-index-the-atof-log-rather-than-hold-it-in-memory.md).
It is listed with the sources because that is where a reader looks for "which
files is this thing touching", but it is not one: it is derived entirely from
the log and **safe to delete at any time**.

Two things to expect from it while running:

- **The first request after a fresh start builds it** — roughly ten seconds
  per gigabyte of log, once. Later starts reuse it, which is the point of
  persisting it.
- **Editing `atof_reader.py`, `assembler.py` or `atof_index.py` forces a
  rebuild** on the next request, because the stored fields mean whatever those
  files meant when they wrote them. With `use_reloader=True` this happens
  during ordinary development, and the pause is the rebuild, not a hang.

## Checking the db before serving

`plugins.mem0.check_db` gates the memory db from its `sources` hook: it must
exist, be a regular file, and yield a row from `events` over a read-only
connection. Since the source is marked required, a failure takes **that tab**
out of service — marked in the bar, serving a page that names the problem —
while the rest of the app runs. Before ADR 5 it exited the process.

An existence check alone was not enough. `app.py .` made the db path the
*directory*, which exists; sqlite reads the file header at connect, so every
request died with a bare `disk I/O error` (EISDIR) that reads like failing
hardware rather than a wrong path.

The ATOF log is not required in that sense: it is allowed to be missing, and
the Turns tab says so itself rather than going out of service.

## The startup banner

Prints the config file, `HERMES_HOME`, and every tab with its sources — each
marked ok, MISSING or UNUSABLE, and labelled with the rule that supplied the
path. Once, in the reloader supervisor, since the worker sets
`WERKZEUG_RUN_MAIN`.

It is the only place a tab's problem is visible without opening the tab, so it
has to carry the whole picture: a tab that is out of service leads with
UNAVAILABLE and the reason. Its `status` line tells the user successful
requests are not logged, and points at `/_status`.

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
