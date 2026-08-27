# 23. Label a skill's origin from hermes' provenance sidecars

Date: 2026-08-27

## Status

Accepted, implemented 2026-08-27

Builds on [ADR 22](0022-view-a-skills-files-confined-to-the-configured-skill-roots.md)
— the skill view and the root containment it reads within — and reuses the
Metadata-box chrome of [ADR 12](0012-open-a-whole-value-on-its-own-page.md).

## Context

The skill view shows a skill's files but not where the skill came from. A reader
looking at a `skill_view` row cannot tell whether hermes shipped the skill, the
user installed it from the hub, hermes' own curator wrote it, the user
configured an external directory, or someone hand-authored it.

hermes knows all of this and records none of it in the SKILL.md. It keeps
provenance in three sidecar files at the root of its runtime skills directory:

- `.bundled_manifest` — `name:hash` per line, skills seeded from the hermes repo
- `.hub/lock.json` — `{"installed": {name: {…}}}`, Skills-Hub installs
- `.usage.json` — `{name: {created_by, created_at, …}}`, curator telemetry

These are hermes-internal state, not a published contract. Reading them is a new
kind of coupling: ADR 22 reads skill *files* but treats them as opaque text to
render; this reads hermes' *records* and acts on their meaning — the first time
the app interprets a hermes state file rather than the ATOF log or the mem0 db.

Two facts bound how honest the labels can be.

- **hermes marks agent authorship only for its autonomous curator.**
  `created_by: "agent"` is set when the background curator creates a skill on
  its own. A skill hermes wrote *because the user asked it to* (a foreground
  `skill_manage` create) is deliberately left unmarked
  (`tools/skill_usage.py`). So "hermes created it" is recorded only for the
  autonomous case; a user-requested one is indistinguishable from a
  hand-authored skill.
- **Provenance lives only in the primary skills dir.** External skill dirs
  (`skills.external_dirs`) sit outside it and carry no sidecars.

## Decision

**Read the three sidecars beside a skill and label its origin from what they
record, best-effort. Five buckets; an origin is a record found, never inferred.**

- **Bundled with hermes** — name in `.bundled_manifest`
- **Hub / URL-installed** — name in `.hub/lock.json` `installed`
- **Agent-created** — `.usage.json` `created_by == "agent"`
- **External dir (user-configured)** — the skill sits under a
  `skills.external_dirs` root; decided *before* any sidecar is read, because
  the origin is the configuration itself and is knowable without one
- **User-added / manually authored** — a sidecar exists but names the skill in
  none of the above

Precedence mirrors hermes' own (`tools/skill_usage.py` checks hub before
bundled), so a skill hermes treats as hub-installed reads as hub here too.

**Best-effort, and honest about the gaps.** A missing, renamed or unparseable
sidecar narrows what can be said rather than breaking the page: a root with no
sidecars at all is **Unknown**, and any one unreadable file just drops its
bucket. The user-added label carries a note that hermes leaves user-requested
agent authorship unmarked, so the bucket is not misread as purely hand-authored.

**Isolated in one module (`skill_provenance.py`), the way a payload reading is.**
Nothing else in the app knows these file formats; when hermes changes them, this
is the one place that changes, and its failure mode is already a shrug, not a
crash. This is the shape the design principles ask of any hermes read — the
coupling is admitted, named, and quarantined behind a degradation path.

**The box is ADR 12's chrome.** A "Metadata" header on the skill page, boxed like
the full-value page's, carrying the origin badge, the created and last-modified
dates, and whatever extra the usage record offers (last used, times used, state,
hub source). Last-modified is the SKILL.md's own mtime — a filesystem fact, not
a hermes claim — so it is present even when the sidecars say nothing.

## Consequences

- **hobserver now interprets hermes state, not just renders hermes files.**
  SECURITY.md's "Reading skills off disk" gains the sidecars as a read. They sit
  inside a trusted root already, are dotfiles the file list excludes, and are
  read-only and non-fatal — no new trust boundary, but the section names the
  wider read.
- **The labels are only as honest as hermes' records.** The user-added bucket
  over-collects (user-requested agent skills land in it), and this cannot be
  fixed from outside hermes; the note says so rather than the box overclaiming.
- **A new coupling to undocumented formats.** If hermes renames a sidecar or
  restructures `.usage.json`, provenance quietly degrades to Unknown until
  `skill_provenance.py` is updated — chosen over a hard dependency that would
  break the page.
- **No new runtime dependency.** The manifest and the JSON are stdlib reads;
  `pyyaml` (ADR 22) is already how the external-dir roots are known.

## Related

- [ADR 22](0022-view-a-skills-files-confined-to-the-configured-skill-roots.md) —
  the skill view and the containment this reads within
- [ADR 12](0012-open-a-whole-value-on-its-own-page.md) — the page-per-thing
  whose Metadata-box chrome this reuses
- [SECURITY.md](../../../SECURITY.md) — the skills-off-disk read this widens
- [docs/design/design-principles.md](../design-principles.md) — reading a hermes
  source without treating it as a contract
