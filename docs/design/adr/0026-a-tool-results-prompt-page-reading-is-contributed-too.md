# 26. A tool result's prompt-page reading is contributed too

Date: 2026-09-30

## Status

Accepted, implemented 2026-09-30

Extends [ADR 17](0017-a-payload-reading-is-contributed-beside-the-spec-that-names-it.md)
from the turn page to the prompt page.

## Context

The prompt page shows each tool result under the call it answers. For some
tools it adds a **formatted** tab beside the raw body: web_search's hits,
search_files' matches, terminal's facts and output. Those readings were a
fixed table, `RESULT_READERS`, in `plugins/turns/spans.py`, looked up while
the page's sections were built.

That failed ADR 17's ownership test the way `Span.mem0_results` once did.
mem0_search's result — `{"results": [{id, memory, score}], "count": n}` —
could get a formatted tab only by teaching the Turns tab mem0's shape, and a
stranger's tool could not get one at all. The table was also out of reach of
anything contributed: sections are built by a `Span` property that never sees
the merged spec table.

## Decision

**A contribution may carry `RESULT_READERS` — `{tool name: fn(text) ->
reading}` — beside `SCOPES` and `SPAN_READERS`, and the prompt page reads each
result by the merged table.**

    # plugins/memory/mem0/spans.py
    def read_search_result(text): ...
    RESULT_READERS = {"mem0_search": read_search_result}

- **Same lifetime and layering as `SPAN_READERS`.** A `SPEC_ATTRS` entry the
  shell carries unopened; this tab's table is the base, then loaded tabs, then
  modules named in settings. An override is named in the banner (`overriding
  result reader:terminal`); a malformed table skips the whole contribution.
- **Readings are applied by the route, not while sections are built.** Section
  building stamps the call's tool name on its result (`tool`), and the page
  applies the merged table (`apply_result_readings`). Sections stay a pure
  function of the request.
- **A reader gets the result's text as the model received it**, framing and
  all, and returns a dict or None. A raising reader, or a non-dict, is no
  reading: the result keeps its raw body and nothing else changes.
- **The reading shape gains a generic part.** Beside the tool-specific keys
  this tab draws (`search`, `command`), a reading may carry `fields` — `[{key,
  value}]`, drawn as facts — for the whole result and for each row, and a row
  may be text alone (`description`) with no title or url. That is enough to
  draw a contributed tool without the template learning it. The rest of the
  shape is in `fulltext.Section.result`.

## Consequences

- **mem0_search results have a formatted tab**, read in the mem0 plugin: the
  count as a fact, one row per memory with its score and id. Disabling the
  Mem0 tab takes the reading away and the result is a raw dump again.
- **A fourth published surface.** `RESULT_READERS` and the reading shape join
  `SCOPES`, `SPAN_READERS` and the spec vocabulary. Changing a key the
  template draws now needs the care a URL does.
- **A reading is untrusted output**, like the payload it reads: every value is
  autoescaped, `fields` entries that are not `{key, value}` are dropped, and a
  row's url becomes a link only through the same scheme check as this tab's
  own readings (SECURITY.md).
