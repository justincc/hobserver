# 22. View a skill's files, confined to the configured skill roots

Date: 2026-08-20

## Status

Accepted, implemented 2026-08-20

Builds on [ADR 12](0012-open-a-whole-value-on-its-own-page.md) — a page that is
one thing, addressed from a scope, rendered as the markdown it is — and on
[ADR 9](0009-scope-specs-may-link-and-read-published-data.md), which is how a
scope declares a link rather than hard-coding a URL. It also touches the trust
model in [SECURITY.md](../../../SECURITY.md), which this is the first change to
widen.

## Context

A `skill_view` or `skill_manage` scope shows the skill's name and, when the
payload carries one, a file within it. What it cannot show is the skill
itself. A reader who wants to know what a skill actually instructed — the text
the model was handed — has to leave hobserver, find the skill on disk, and open
it by hand. Skills are scattered: a user's own live under the directories in
`skills.external_dirs` in `$HERMES_HOME/config.yaml`, and hermes ships a bundled
set of its own.

Two things stand in the way of just linking to the file.

- **hobserver has never read a file off disk that was not the log or the mem0
  db.** [SECURITY.md](../../../SECURITY.md) says so as a flat statement of what
  the app touches. Showing a skill means reading arbitrary files under
  directories the app has never opened, and rendering their contents.
- **The path comes from the log, and the log is not trusted.** SECURITY.md
  treats log content as attacker-influenceable — hermes ingests web pages,
  email and files it was asked to read, any of which can end up in a payload.
  A route that opens whatever `file_path` a `skill_*` payload names is an
  arbitrary-file-read: a poisoned log could name `~/.ssh/id_rsa` and the link
  would render it in the reader's own browser.

So the feature is small on the rendering side — it reuses ADR 12's page almost
whole — and the whole of its weight is in where it is allowed to read.

## Decision

**A skill view serves files off disk, but only from within the configured skill
roots. The log's path is a candidate, never an authority: it is admitted only
if it resolves to somewhere inside a root, and refused otherwise.**

Five decisions inside that.

**The roots are trusted config, and there are two sources of them.** The allow
list is the union of `skills.external_dirs` from `$HERMES_HOME/config.yaml` and
hermes' own bundled skills directory. Both are the operator's configuration, not
the log — the same trust class SECURITY.md already grants `hobserver.toml`
("anyone who can write them can run code or read files as you"). An operator may
also name `skill_roots` in `hobserver.toml` to override the derivation entirely;
explicit wins, as it does for every other source, and every root is named on the
banner.

**Admission is containment, resolved through symlinks.** A file is served only
if its `realpath` — links resolved first, so a symlink inside a skill cannot
point back out — lies under one of the roots. Everything else is a 404. This is
the one rule the arbitrary-read concern reduces to, and it makes the untrusted
`file_path` safe to use as input: the worst a poisoned path can do is fail the
check.

**The page is ADR 12's page, pointed at a file instead of a payload value.**
SKILL.md renders through `fulltext.render` with raw HTML off, exactly as a
prompt or a response does, and `?raw=1` shows its characters. A skill with no
SKILL.md, or a render that raises, degrades to the text form and says why rather
than failing — the same degradation path fulltext already has. The link also
carries the originating span's uuid, so the page finds the turn that span ran in
(the same `_find_span` the full-value page uses) and links back to it through a
shared `back_to_turn` macro — one "back to the turn" for both pages, not two.
YAML frontmatter is held out of the markdown and shown verbatim: CommonMark
reads a `key:` line closed by `---` as a setext heading, so the whole block
would otherwise render as one bold heading.

**The left-hand file list is built from the filesystem, not from the log.** Like
ADR 12's `sections` contents list, the skill page carries a sidebar; unlike it,
each entry is a real file in the skill's directory, found by scanning that
directory (confined within a root) and not by trusting anything a payload said.
This is why the sidebar is not the security concern it first looks like — it is,
if anything, safer than the file links on the full-prompt page, which are
derived from log content. Each entry re-enters the same route, is
containment-checked in its own right, renders `.md` as markdown and other text
files raw through autoescaping, does not follow a symlink out of the directory,
and does not offer a binary as a download.

**The link is declared, and the page fails closed.** A
`Link(endpoint="turns.skill", …)` on the `SKILL` spec (ADR 9) draws the
affordance, gated on the scope naming a skill — a `skill_*` payload with no
`name` resolves to nothing and gets no link. The root-level fail-closed lives on
the page rather than the link, because whether a skill sits inside a root is a
disk fact the render path deliberately does not consult: a skill outside every
root, a traversal or a symlink out is a 404, and with no roots configured the
page says skill viewing is unavailable rather than reading anything. So the link
may be present for a skill the page then refuses — the refusal, not the link's
absence, is the closed door.

### The dependency this forces

`config.yaml` is YAML, and YAML has no standard-library reader — unlike the TOML
of `hobserver.toml`, which is why `pyproject.toml` can say config reading "should
not need a dependency" and mean it. Reading the roots from hermes' own config,
rather than making the operator restate them, needs a parser.

The decision is to **add `pyyaml` as a fourth runtime dependency, behind the same
degradation path as `markdown-it-py`**: if it is not importable, or the config
cannot be read, skill viewing is simply unavailable and the link does not render
— the rest of the app is untouched. Reading hermes' own config keeps the roots in
step with the agent's with nothing to duplicate; the alternative, a `skill_roots`
list the operator maintains in `hobserver.toml`, was rejected as the default
because it drifts from the config it copies, though it remains available as the
explicit override. The bar ADR 12 set for a dependency — pure-ish, doing
something this tree should not hand-roll, behind a degradation path — is met:
hand-rolling a YAML reader would be our correctness problem forever, for a format
we do not own.

## Consequences

- **hobserver reads a third kind of source, and its trust story changes.**
  SECURITY.md gains the containment rules as must-preserve items: roots come only
  from trusted config, every served file's `realpath` must fall under a root,
  symlinks are resolved before the check and never followed out, and only text is
  rendered. This is the first widening of "reads only the log and the db", and the
  section has to say what now bounds the wider read.
- **A fourth runtime dependency, and a new config read.** `pyyaml`, behind a
  degradation path, plus one parse of `$HERMES_HOME/config.yaml`. The config is
  trusted input; the parse is read-only and its failure is non-fatal.
- **The roots are named on the banner.** The startup source table gains the skill
  roots beside the ATOF log and the mem0 db, resolved through the Turns tab's
  `sources` hook, marked ok / MISSING like the rest — so "which files is this
  thing touching" stays answerable from the banner as the answer grows.
- **The link is declarative, so the mechanism is not skill-only.** Another tab
  whose tool references a file under a known root could declare the same kind of
  link; the route and the roots are this tab's, but the vocabulary is not new.
- **Resolving a skill name to a directory is not just its basename.** The
  `external_dirs` entries point at individual skill source directories whose
  layout is irregular — SKILL.md sits at varying depths and a directory's
  basename is often not the skill's name. So the reliable key is the payload's
  file path validated against the roots; mapping a bare `skill_name` to a
  directory, for a scope that carried no file path, is a fallback that reads each
  root's SKILL.md identity, and is best kept small.
- **Bundled and user skills share one allow list.** It spans hermes' source tree
  and the user's own directories; both are trusted, so a reader can open either
  kind from the same link with the same guarantees.

## Related

- [SECURITY.md](../../../SECURITY.md) — the trust model this widens, and where the
  containment rules become must-preserve
- [ADR 12](0012-open-a-whole-value-on-its-own-page.md) — the page-per-thing and
  its `?raw=1`, sidebar and degradation path, reused here
- [ADR 9](0009-scope-specs-may-link-and-read-published-data.md) — a scope
  declares a `Link` rather than a URL
- [docs/design/span-rendering.md](../span-rendering.md) — the skill rows the link
  joins
- [docs/running/startup-and-console.md](../../running/startup-and-console.md) —
  the source table the roots are added to
