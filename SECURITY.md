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

The skill view (ADR 22) also reads skill files off disk, but only from within
the configured skill roots — see [Reading skills off disk](#reading-skills-off-disk)
for the containment that bounds it.

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
- **Skill roots** — hermes' `config.yaml` (`skills.external_dirs`) and, when
  set, a `skill_roots` list in `hobserver.toml`. These name the directories the
  skill view is allowed to read from, so they are trusted the same way: a root
  you did not intend is a root hobserver will serve files out of.

## Reading skills off disk

The skill view resolves a skill from a `skill_view` / `skill_manage` scope and
renders its files (ADR 22). The path in that scope comes from the log, which is
**not trusted** — so the view never reads a path just because a payload named
it. These rules **must be preserved**; they are the whole reason an untrusted
path cannot become an arbitrary-file read:

- **Roots come only from trusted config** — the skill directories derived from
  `HERMES_HOME` and the `external_dirs` in hermes' `config.yaml`, or an explicit
  `skill_roots` in `hobserver.toml`. Never from the log.
- **Every served file's `realpath` must fall under a root.** Symlinks are
  resolved *before* the check, so a link inside a skill cannot point out of it,
  and are never followed out.
- **The file list is the skill's own directory scanned on disk** — not anything
  the payload said. That is why the sidebar opens no read surface the SKILL.md
  view does not already have.
- **Only text is rendered.** A file is shown as markdown (its `.html` is the one
  `|safe` string, from the renderer with raw HTML off) or raw through
  autoescaping; anything else is listed but not opened.

A skill outside every root, a traversal, or a symlink out is a 404; with no
roots configured the view says it is unavailable rather than reading anything.

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
