# Mem0 tab

The Mem0 tab browses `jmem0_logged.db`, an SQLite event log of hermes-agent
mem0 activity. Its pages:

- `/memory/mem0/` — all mem0 events, newest first, with a truncated query;
  click an id or query to open the event. The page tails the log: it polls
  `/memory/mem0/fragment/events?since=<last id>` every 3 seconds and prepends
  any new rows to the top of the table.
- `/memory/mem0/fragment/events?since=<id>` — rendered table rows for events
  newer than `<id>`, newest first (used by the index poll).
- `/memory/mem0/event/<id>` — one event, described below.
- `/memory/mem0/search-event?session=<id>&query=<q>[&ts=<µs>]` — redirects to
  the event page for one `mem0_search` call. This is the handoff from a
  mem0_search span in the Turns tab; see
  [span-rendering.md](../../../docs/design/span-rendering.md) for how the two
  logs are matched, and why it is a redirect rather than a lookup.

The event page opens with a metadata block (every field except query and
result), then the query, the result and the context messages as plaintext.

- The query heading is named for what the column actually holds on that event
  type: "Query" on a `prefetch` or `mem0_search`, "Added text" on a
  `mem0_add`, "New text" on a `mem0_update`, "Deleted memory id" on a
  `mem0_delete`.
- An update or delete also gets a "Previous text" section — what the memory
  said before the change, recovered from this app's own event log, with the
  search event it came from and how long before.
- Results that are JSON (e.g. `mem0_search` output) are pretty-printed; others
  (e.g. prefetch markdown) are shown as-is.
- Context messages are the up to 10 preceding prefetch queries logged for the
  same session, each prefixed with its event id, oldest first. Tool-call
  events are excluded — they are not user messages. It approximates the extra
  conversational context mem0 uses during retrieval.
