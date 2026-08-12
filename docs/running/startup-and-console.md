# Startup, data sources and the console

## Loading the tabs

The first command-line argument, else `$HOBSERVE_CONFIG`, else
`./hobserve.toml` names the config file; with none present the built-in
default list is used. `tabs.py` parses it and imports each enabled module. See
[plugins-and-urls.md](../extending/plugins-and-urls.md).

Only two things are fatal: a config file that cannot be parsed or is
internally impossible (two tabs at one URL prefix), and an empty tab bar. A
single broken tab never is.

## Resolving sources

Each plugin resolves its own: its `settings` from the config file, else a
default under `hermes_paths.hermes_config_dir()` — the normalized
`$HERMES_HOME`, else `~/.hermes`, which is what hermes-agent itself falls back
to. So nothing has to be configured when `HERMES_HOME` is exported, and an
installation that keeps hermes in the conventional place needs nothing either.

| tab | setting |
| --- | --- |
| Turns | `atof_log` |
| Turns | `index_db` |
| Mem0 | `db` |

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
  files meant when they wrote them. Under `--dev` this happens during ordinary
  development, and the pause is the rebuild, not a hang.

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

Prints the config file, the hermes config directory with what supplied it, and
every tab with its sources — each marked ok, MISSING or UNUSABLE.
Once: under `--dev` it prints in the reloader supervisor, since the
worker sets `WERKZEUG_RUN_MAIN`, and without it there is only the one process.

Everything on it is something this run resolved, which is the test for adding
a line.

It is the only place a tab's problem is visible without opening the tab, so it
has to carry the whole picture: a tab that is out of service leads with
UNAVAILABLE and the reason. Its `status` line tells the user successful
requests are not logged, and points at `/_status`.

## Console noise

`request_log.py` keeps the console usable despite the 2-3 s live polls:
successful (2xx/3xx) responses are not logged at all, on any path, and only
errors appear.

**The app logs the errors itself**, from the same `after_request` hook that
keeps the tally — `log_error_response`, warning for a 4xx and error for a 5xx,
carrying the query string when there is one, since a failing poll is identified
by its `since=`:

```
[18:10:16] WARNING GET /memory/mem0/fragment/renamed?since=42 -> 404
```

It is the app's job rather than the server's because waitress logs no requests
at all, and never sees a 404 in the first place — Flask answers those itself.
A console rule built on a server's access log would say nothing in the mode
this app normally runs in (ADR 14).

Nothing else logs a request. There is no access log to filter in either mode:
werkzeug is here only as the reloader `--dev` wraps waitress in, and its dev
server never runs.

What that reloader says is trimmed by `QuietWerkzeugFilter`, which drops
`* Restarting with stat` — the strategy it watches files with, not anything
that happened, and at startup nothing has restarted at all. `Detected change
in ..., reloading` survives, which is the line worth reading under `--dev`.

`app.py` calls `logging.basicConfig` before serving. Not optional housekeeping:
`waitress.serve` calls it otherwise, and its format has no clock.

Success logging was deliberately simplified from an earlier per-path
first-success-plus-pointer scheme. If fine-grained logging is ever wanted back,
add a setting rather than reviving the complexity.

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

Port 5090, on **waitress** — a production WSGI server, chosen in
[ADR 14](../design/adr/0014-serve-on-waitress-and-keep-one-server-in-development.md)
because it is pure Python and so costs nothing to install. There is no
interactive debugger to expose if you open the bind up; waitress has none.

The bind address is loopback (`127.0.0.1`) unless `hobserve.toml` sets a
top-level `host` — set one to reach hobserve from other machines. The
banner prints the address it resolved.

A running hobserve serves the checkout as it was when it started. No edit
reaches it — template or `.py` — without `--dev`
([ADR 15](../design/adr/0015-only-dev-lets-the-checkout-reach-a-running-app.md)):

```bash
./hobserve --dev            # every edit lands
./hobserve --dev other.toml # either order; --dev is not a config path
```

Under it a template edit shows up on the next request (Jinja's doing, not the
server's) and a `.py` edit restarts the worker, in about 0.3 s. That restart
is the Werkzeug reloader as a supervisor around **the same waitress server** —
it is not a second server.

Note that under `--dev` an edit to `atof_reader.py`, `assembler.py` or
`atof_index.py` costs an index rebuild on the next request, as above.

Being a real WSGI server is not the same as being safe to expose: there is no
authentication in front of this app, so a `host` that is not loopback puts it
on the network unguarded. That is why the bind defaults to loopback.

Producer-side setup (nemo-relay install, plugin enable, `HERMES_NEMO_RELAY_*`
in `~/.hermes/.env`) is in [setup-prompt-timing.md](setup-prompt-timing.md).
