# 14. Serve on waitress, and keep one server in development

Date: 2026-08-11

## Status

Accepted

## Context

This app can be bound to the network, so whatever answers that socket should
be written to face one: to survive a slow or malformed client, and to fail a
request rather than the process. That is the whole of the requirement. Load is
not part of it — this is a single-user tool, usually on a home network, and
will never need to scale. *(Later: the bind defaults to loopback and a network
bind is opt-in via `host` in hobserver.toml — but the server still has to be
one that could face a network, so the choice below is unchanged.)*

Three constraints on the choice:

- **Setup cost stays at zero.** `./hobserver` is the entire install
  story. A server needing a separate command, a process manager or a compiler
  would trade a better socket for a worse tool.
- **A `.py` edit restarts the server during development.** Template edits are
  already free (`TEMPLATES_AUTO_RELOAD` is Jinja's, not the server's) —
  though [ADR 15](0015-only-dev-lets-the-checkout-reach-a-running-app.md)
  later gated them behind `--dev` anyway, so that one switch covers every
  kind of edit.
- **The console shows errors and nothing else.** The pages poll every 2-3 s, so
  successful responses are dropped and the tally of everything lives at
  `/_status`.

The last two pull against the first: the servers that are pleasant to develop
against are the ones that warn you not to deploy them.

## Decision

**Serve on waitress.** Pure Python and dependency-free, so `uv sync` installs
it everywhere this app runs and `serve(app, host=..., port=...)` is the whole
integration. Nothing about the run command changes.

Considered and rejected: **gunicorn**, which does not run on Windows and
expects to be its own entry point rather than a call inside `app.py`; **uvicorn
and other ASGI servers**, which would need an `asgiref` wrapper around a WSGI
Flask app — indirection bought nothing here.

**`--dev` adds a reloader to that same server, rather than selecting a
different one.** `werkzeug._reloader.run_with_reloader` is a supervisor around
any callable — it re-execs the process on a change and runs what it was given
in the worker — so it can supervise waitress. Development and ordinary running
then differ in exactly one thing, whether an edit restarts the process, and
there is no serving path that only ever runs in development.

**The app logs its own error responses.** `log_error_response`, called from the
`after_request` hook that already keeps the tally, logs any non-2xx/3xx with
its path and status: warning for 4xx, error for 5xx.

That belongs to the app rather than to the server because waitress logs no
requests at all, and would not see a 404 in any case — Flask answers those
before the server is involved. A console rule reading a server's access log
would therefore say nothing in the mode this app normally runs in.
It also means no access log exists to filter: werkzeug arrives as the reloader
alone, and its dev server never runs. `QuietWerkzeugFilter` is left trimming
what that reloader says — dropping its restart notice, keeping the change line
that explains a restart.

`app.py` also calls `logging.basicConfig` before serving, because
`waitress.serve` calls it otherwise and its default format carries no clock.

## Consequences

The server is one written to face a network, for whenever the bind is opened
up to one, and there is no interactive debugger in existence to expose:
waitress has none.

`werkzeug._reloader` is a private name. It is imported inside the `--dev`
branch, so if it ever moves, `--dev` fails loudly at the point it is asked for
and ordinary running — which never reaches that line — is unaffected. The
fallbacks, in order of preference, are `hupper` (same shape, public API, one
more dependency) or a development-only `Flask.run()`, which is the divergence
this decision exists to avoid. Werkzeug is a dependency either way: it is what
Flask is built on.

The console rule no longer depends on what is serving. Reporting a response
where the app already has one in hand is what makes it hold under both modes,
and would hold under a third server.

Nothing here makes the app safe to expose beyond a trusted network. There is
still no authentication in front of it — this decision is about the quality of
the server, not about who can reach it.
