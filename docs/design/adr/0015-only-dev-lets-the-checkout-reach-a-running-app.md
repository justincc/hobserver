# 15. Only `--dev` lets the checkout reach a running app

Date: 2026-08-12

## Status

Accepted

Refines [14](0014-serve-on-waitress-and-keep-one-server-in-development.md),
which noted that template reloading came free and left it always on.

## Context

Two kinds of edit behaved differently on a running server. A `.py` change
needed `--dev`, which puts the Werkzeug reloader around waitress. A template
change needed nothing: `TEMPLATES_AUTO_RELOAD` was set unconditionally, and
Jinja re-read the file on the next request.

Nothing justified the split except how much each cost to implement. Template
reloading is a Jinja stat per render and free; process reloading needs a
supervisor, a second process and a restart. That is a fact about the
implementations, not about who is running the app or what they want from it.

The two audiences want opposite things, and neither was being served
consistently:

- Someone **using** hobserve wants a fixed thing. They are watching an
  agent, and the tool should not change under them because a file in the
  directory they launched from was touched — by an editor, a `git checkout`,
  or the agent they are observing, which can write to files.
- Someone **changing** it wants every edit to land, and already accepts a
  switch to get that.

## Decision

**`--dev` decides whether a running app tracks this checkout, for every kind
of edit.** `create_app` takes it and sets `TEMPLATES_AUTO_RELOAD` from it, so
one switch now means the same thing about `.py` files and templates alike:

- **without `--dev`** — the app is the snapshot that was on disk when it
  started. Nothing in the checkout reaches it until it is restarted.
- **with `--dev`** — every edit lands. A template on the next request, with no
  restart; a `.py` file by restarting the worker, in about 0.3 s.

**The data it observes is not covered by this and never will be.** The ATOF
log, the mem0 db and the index are read live on every request — that is what
the app is for. The rule is about the checkout, not about disk.

Nothing else needed changing to make this true: there is no `static/`
directory in the shell or either plugin (CSS and JS are inline in
`base.html`), `hobserve.toml` is read once in `main()`, and plugin, scope and
provider modules are imported at startup. Templates were the only live
exception.

## Consequences

- Template work now needs `--dev`. Under it, template edits still land
  without a restart, so no one developing loses anything — the plain server
  loses a capability it should not have had.
- ADR 14's "template edits are already free" no longer describes what ships.
  Its actual decision — one server, not a development one alongside — is
  untouched, and `--dev` still wraps that same waitress.
- Rendering without `--dev` skips a stat per template. Not a reason to do
  this, and not measurable here.
- Anything added later that reads the checkout at request time — a config
  file re-read, a plugin hot-load — is covered by the same rule and should
  be gated the same way.
