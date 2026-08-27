# 24. Show a skill's effective description, read from the prompt

Date: 2026-08-27

## Status

Accepted, implemented 2026-08-27

Extends [ADR 23](0023-label-a-skills-origin-from-hermes-provenance-sidecars.md)
(the skill page's Metadata box).

## Context

hermes lists every skill in the system prompt as a one-line index entry, and
**truncates a long description there** — currently to ~60 chars
(`agent/skill_utils.py`). The model routes on that truncated line unless it
opens the full skill with `skill_view`, so a description whose trigger falls
past the cut is invisible to routing. That is worth surfacing on the skill
page: the reader should see what hermes actually routes on, beside the full
description the frontmatter already shows.

The obvious way — reconstruct the cut in hobserver (`desc[:57] + "..."`) — ties
this app to a hermes constant that can change and leave hobserver silently
wrong. Importing hermes' own `extract_skill_description` was considered and
rejected outright: hobserver reads *files* another process wrote and has **no
hermes code dependency** — even its path logic is a local reimplementation
(`hermes_paths.py`), not an import — and it must keep running read-only against
a log, possibly on another machine, with no hermes install. A version-matched,
importable hermes package would be a far bigger and more brittle coupling than
the number it avoids.

## Decision

**Read the actual truncated entry hermes wrote, from a recent system prompt in
the log. Never reconstruct it — so the cut length is not encoded here and
cannot drift.**

The mechanism is content recognition, not a formula
(`plugins/turns/skill_index.py`):

- The system prompt is `annotated_request.instructions` (or a `system`
  message) on a recent llm call — payloads hobserver already reads (ADR 12).
  A recent call is hydrated to get it; any call carries the same index.
- An entry is recognised as *this* skill's by its name, and confirmed a
  genuine truncation by **the entry text (minus hermes' trailing `...`) being
  a proper prefix of the skill's full SKILL.md description**. That both rules
  out the prompt's many other `- key: value` lines (guidance, examples) and an
  author who ended a description with `...` of their own.

**If hermes changes the cut length, the prefix check still holds.** If it
changes the index line shape past recognition, no entry is found and the row
is simply omitted — fail-open, the same posture as the provenance read in ADR
23. An untruncated entry also yields nothing: the frontmatter below already
shows the full text, so the row appears only when it tells the reader
something the page does not.

The value leads the Metadata box as **Effective description** — it is the fact
most likely to surprise, and the reason the box is worth reading before the
skill's own words.

## Consequences

- **A new reading of an existing payload, isolated in one module.** The system
  prompt is already a payload hobserver reads; this adds one interpretation of
  it (the skill index), quarantined in `skill_index.py` behind a fail-open
  path — nothing else knows the index format. No new trust surface: the read
  is the same log, within the same containment.
- **The only coupling is the `- name: desc` line shape, and it is soft.** The
  truncation judgment carries no hermes constant; recognition degrades to
  "no row" rather than a wrong claim if the shape ever changes.
- **A recent llm call is hydrated on a skill-page load.** One payload read,
  and the assembly caches the hydration, so repeat loads are free. A skill
  absent from the latest index (disabled, or no prompt in the log yet) shows
  no row — which is honest: hermes is not routing on it.
- **No new dependency, runtime or dev-time.** No hermes import, and no
  test-time comparison against hermes code — the recognition is self-contained
  and self-correcting.

## Related

- [ADR 23](0023-label-a-skills-origin-from-hermes-provenance-sidecars.md) — the
  Metadata box this adds a row to, and the same fail-open coupling posture
- [ADR 12](0012-open-a-whole-value-on-its-own-page.md) — the request payloads
  (`annotated_request`) this reads the system prompt from
- [docs/design/design-principles.md](../design-principles.md) — reading a
  hermes source without treating it as a contract
