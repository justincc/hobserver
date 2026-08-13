# Security

## Trust model — read this first

**hobserver has no authentication.** Anyone who can reach the port sees
everything it shows and can use every page. Run it only where **only people
you trust** can reach it.

- It binds `127.0.0.1` (loopback) by default — reachable only from the machine
  it runs on.
- Setting a non-loopback `host` in `hobserver.toml` (e.g. `0.0.0.0`) puts it on
  your network with nothing in front of it. Do that only on a network you
  control, never a public or untrusted one.
- The startup banner prints the bind address, so you can see what you exposed.

That trust boundary — who can reach the port — is the whole security model.
hobserver is a single-user tool for observing your own hermes-agent: there is
no login, no user model, no per-page permissions, and none is planned while
that assumption holds.

## What it reads

Read-only over files another process writes — the ATOF log and the mem0 db. It
never writes to them and has no endpoint that mutates data. The only file it
writes is a rebuildable index cache under your cache directory.

The log can contain text hermes ingested from untrusted places (web pages,
email, files it was asked to read). hobserver renders that content safely (see
below), but it will faithfully **show** you whatever is in the log — including
a secret a model may have written into its own output (see Redaction).

## Rendering untrusted log content (XSS)

Because log content is attacker-influenceable, the browser-rendering path is
the main hardening surface. These defenses **must be preserved** when changing
rendering code:

- **Jinja autoescaping is on** (Flask's default for `.html`). Every
  `{{ value }}` from the log is HTML-escaped. Only two places use `| safe`,
  both in `plugins/turns/templates/turns/full.html`, and only on
  markdown-rendered HTML.
- **Markdown is rendered with raw HTML disabled** —
  `MarkdownIt("commonmark", {"html": False})` in `plugins/turns/fulltext.py`.
  A raw `<script>` or `<img onerror>` in log text is escaped, not emitted.
  markdown-it's default link validation rejects `javascript:`, `vbscript:` and
  `data:` URLs, so `[x](javascript:…)` renders as inert text; only `http(s)`
  links become anchors.
- **The `raw` value view** shows the unrendered characters through
  autoescaping (`{{ part.text }}` in a `<pre>`), not as HTML.

Adding a `| safe`, an `{% autoescape false %}`, a `Markup(...)`, or
`html: True` in the renderer removes one of these defenses. Don't, unless the
input provably does not come from the log.

## Trusted input — not everything is untrusted

Some inputs are configuration you provide, and are trusted as such. Anyone who
can write them can run code or read files as you:

- **`hobserver.toml`** names Python modules (`module`, `scope_specs`,
  `provider_specs`) that hobserver **imports** — that is, executes. Treat the
  config file and everything on the Python path as trusted.
- **Source paths** in the config (`atof_log`, `db`, `index_db`) are read and
  displayed; hobserver will read whatever file you point it at.

## Redaction

The generic renderer drops values whose key name looks secret — `token`,
`secret`, `password`, `api_key`, `authorization`, `cookie` and similar
(`GENERIC_SECRET_HINTS` in `plugins/turns/atof_reader.py`). This is
best-effort: it matches key names, in the generic renderer only. It will not
catch a secret a model pastes into free-text prose, or one stored under an
innocuous key. Do not rely on it as a guarantee.

## Everything else

Database access is parameterized throughout. There is no `eval`/`exec`, no
`subprocess`, no debug server, and no sessions, cookies or secret key.

## Reporting

There is no formal vulnerability-reporting process. hobserver is a personal
tool meant to run behind a trust boundary, so its exposure is small by
construction. If that changes, or you find something worth flagging, open an
issue.
