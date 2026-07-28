# hermes-observer

A small Flask webapp for observing hermes-agent activity (formerly
jmem0-logged-browser). Views are plugins, shown as horizontal tabs:

- **Prompts** — per-turn latency waterfalls from the NeMo Relay ATOF
  JSONL stream exported by the hermes-agent `observability/nemo_relay`
  plugin: where each turn's time went (llm vs tool vs overhead), with a
  span timeline per turn. See `docs/adr/` for the design.
- **Mem0** — browses `jmem0_logged.db`, the SQLite event log produced by
  the hermes-agent mem0 logging wrapper.

Tabs read left to right in the order `plugins.PLUGINS` lists them, and the
first is where `/` lands. Prompts leads because a turn is the unit of
activity — what hermes was asked and everything it did about it, memory
calls included — while the mem0 log covers one tool. Mem0 is named for the
provider rather than for memory in general: hermes also keeps its own
in-prompt stores (MEMORY.md / USER.md), and other external providers may be
tried, and each of those would arrive as its own tab beside this one.

Every view is read-only over a log another process produces, so it is safe
to point at live files while hermes is writing to them.

## Running

```bash
uv run python app.py [observer.toml]
```

Then open http://127.0.0.1:5090. The server binds to `0.0.0.0`, so it is
also reachable from other machines on the local network at
`http://<host-ip>:5090`.

Edits are picked up without a manual restart: templates re-render from
disk on the next request (`TEMPLATES_AUTO_RELOAD`), and the Werkzeug
reloader restarts the server when a `.py` file changes. Debug mode stays
off — the Werkzeug interactive debugger allows arbitrary code execution
and must never be exposed on `0.0.0.0`.

### Which tabs are served

`observer.toml` lists them, in tab order — the first one is where `/` lands:

```toml
[[tabs]]
module = "plugins.prompts"

[[tabs]]
module = "plugins.mem0"
enabled = false            # one line to turn a tab off
```

`module` is any importable path, so a tab written elsewhere and installed
alongside is added the same way, with no fork of this repo — see
[docs/writing-a-plugin.md](docs/writing-a-plugin.md). The config file is taken
from the first command-line argument, else `$OBSERVER_CONFIG`, else
`./observer.toml`; with none of them present the two tabs above are served by
default, so a fresh checkout runs with no setup.

### Where the data comes from

Each tab resolves its own source: its `settings` in the config file, then an
environment variable, then a default under the hermes-agent config directory.
So with `HERMES_HOME` exported nothing has to be configured at all.
`HERMES_HOME` is normalized, since the agent conventionally exports it as
`<checkout>/hermes-agent/../config`; if it is unset, a built-in literal path
stands in.

| tab | setting | env | default |
| --- | --- | --- | --- |
| Prompts | `atof_log` | `ATOF_LOG` | `$HERMES_HOME/nemo-relay/atof/hermes-atof.jsonl` |
| Mem0 | `db` | `JMEM0_DB` | `$HERMES_HOME/jmem0_logged.db` |

The Prompts source is the events JSONL written by the nemo_relay plugin's ATOF
exporter. It is allowed to be absent — hermes may simply not have run with the
exporter on — and the tab says so, naming the path it tried, rather than
showing an empty page.

The Mem0 source is checked before the tab is served: the path must exist, be a
regular file, and yield a row from an `events` table when opened read-only.
Existence alone was not enough — a stray argument (`app.py .`, the current
directory) passed an exists check and then failed per request with a bare
sqlite `disk I/O error`, which reads like a failing disk rather than a wrong
path. A database that fails the check takes **that tab** out of service: the
tab is marked in the bar and serves a page naming the problem, while every
other tab carries on.

On startup the app prints what it resolved — the config file, `HERMES_HOME`,
and every tab with its sources, each marked ok, MISSING or UNUSABLE and
labelled with the rule that supplied it — because a missing source is the
usual reason a tab looks empty. It prints once at launch, not on reloader
restarts.

### Console noise and observer status

An **observer status** link sits at the right of the tab row on every page,
opening `/_status` in a new tab — new rather than in place because the tally
is for checking *while* a page sits there polling. It is deliberately not
called "requests": every other view in this app shows the agent's LLM and
tool requests, and this one shows none of that, only HTTP traffic to the web
app itself. The page repeats the distinction in its own header.


The live-poll pages refetch every 2-3 s, which would bury the console. So
successful (2xx/3xx) responses are **not logged at all**, on any path. Every
non-2xx/3xx response keeps logging as usual — a quiet log must never hide an
error.

The running tally lives at `/_status` (plain text, so it reads the same
curled or in a browser): per path, request count, how long since the last
one, and a status-code breakdown. Fetch it when a page stops updating —
it separates the three failure modes at a glance. No response means the
server is down; a stale *last* time means the browser stopped polling;
non-200 counts mean polls are arriving but failing.

It can be left open: a `Refresh` header re-requests it every 3 s (curl
ignores the header, so the body stays plain text and needs no template),
and the header line carries a clock, since counts alone look identical
whether the page is live or frozen. Its own requests are excluded from the
tally, so watching the page never looks like traffic or refreshes its own
last-seen time.

End-to-end setup — enabling the exporter in hermes-agent (producing) and
pointing this tool at its output (consuming) — is in
[docs/setup-prompt-timing.md](docs/setup-prompt-timing.md).

## Pages

Each tab is served under its own prefix — `plugins.<name>.URL_PREFIX`, which
is independent of the blueprint name the code uses, so a tab can be renamed
or moved without touching a single `url_for`. The mem0 tab is namespaced
under `memory/` because it is one memory system of several to come: another
provider, or hermes' own in-prompt stores, would be `/memory/<name>/`, and
`/memory/` itself is left free to become an index over them.

`/` redirects to the first tab (`/prompts/`). Nothing else is served: a URL
that moves is not redirected from its old address. This is a single-user
tool, and carrying compatibility routes for one reader costs more in reading
than it saves in typing — expect a page open across a rename to 404 until it
is reloaded.

### Mem0 tab

- `/memory/mem0/` — all mem0 events, newest first, with a truncated query;
  click an id or query to open the event. The page tails the log: it polls
  `/memory/mem0/fragment/events?since=<last id>` every 3 seconds and prepends
  any new rows to the top of the table.
- `/memory/mem0/fragment/events?since=<id>` — rendered table rows for events
  newer than `<id>`, newest first (used by the index poll).
- `/memory/mem0/event/<id>` — one event, described below.
- `/memory/mem0/search-event?session=<id>&query=<q>[&ts=<µs>]` — redirects to
  the event page for one `mem0_search` call. This is the handoff from a
  mem0_search span in the Prompts tab; see
  [docs/span-rendering.md](docs/span-rendering.md) for how the two logs are
  matched, and why it is a redirect rather than a lookup.

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

### Prompts tab

- `/prompts/` — all turns, newest first: start time, session, a one-line
  prompt snippet (from the turn-start mark's `user_message`; ellipsized so
  long prompts never widen the table, em dash when absent), total / llm /
  tool / overhead durations (overhead is the residual), model-call and span
  counts. In-flight turns are marked. Parse errors and assembly anomalies are
  shown above the table, folded closed and on this page only — never dropped.
  Updates itself every 3 s.
- `/prompts/turn/<session>/<start_us>` — one turn, described below.

The turn page runs top to bottom:

1. The nav row — "all turns", then muted « prev / next » stepping one turn
   older / newer through the same interleaved-by-start-time ordering the turn
   list uses, so they cross session boundaries just as it does. Each is titled
   with the neighbour's prompt, and greys out at either end.
2. The in-flight strip.
3. The turn id / session / started heading; a muted icon beside the turn id
   copies it to the clipboard.
4. The prompt — whole when short, collapsed to its first couple of lines with
   the full text a click away when long.
5. Summary stats.
6. The span waterfall: llm blue, tool orange, other violet, open spans faded,
   with offsets and durations as text.

The turn's non-boundary marks (approvals, session end) are instantaneous, so
they interleave with the spans as rows whose bar is a zero-width tick at their
offset, clamped to the track — session end fires just after the turn-end mark.

Each span row also shows what the call was *for*, drawn from its payload and
rendered per tool scope: the command a terminal call ran, the path a file tool
touched, a mem0_search's top hits, and so on. That is a subject of its own —
see [docs/span-rendering.md](docs/span-rendering.md).

While the turn is in flight the page updates itself every 2 s; once it ends
the page is static.

### Live updating

Timing pages self-update by polling and swapping, show an in-flight strip, and
offer a follow-mode toggle that opens new turns as they start. Because hermes
drops `hermes.turn.end` marks often, an open turn is poor evidence of running
work, and liveness is decided by proof rather than by a clock.

See [docs/live-pages.md](docs/live-pages.md) for all of it.


## Tests

```bash
uv run pytest
```

## Layout

- `app.py` — app shell: app factory `create_app(tabs)`, tab registration, the
  tab bar, the root redirect, CLI entry. Imports no plugin.
- `tabs.py` — reads `observer.toml` and loads the modules it names; the
  collision check and the per-tab failure handling live here
- `observer.toml` — which tabs are served, in tab order
- `hermes_paths.py` — where hermes-agent keeps things, for the in-tree
  plugins' default paths
- `plugins/` — one module or package per in-tree view; each exposes
  `PLUGIN_API`, a Flask blueprint `bp` (registered under `/<URL_PREFIX>/`), a
  `TAB_LABEL` and a `URL_PREFIX`, plus optional `init_app` and `sources`
  hooks. See [docs/writing-a-plugin.md](docs/writing-a-plugin.md) for the
  contract, [docs/plugins-and-urls.md](docs/plugins-and-urls.md) for how
  plugins reach each other (by link or published accessor, never by opening
  another's data source — ADR 4).
- `plugins/prompts/` — a package holding the full ATOF reader (ADR 2):
  `tailer.py` (incremental read), `atof_reader.py` (JSONL line → typed event,
  fail-soft) and `assembler.py` (events → sessions → turns → waterfall, with
  overhead as the residual of turn duration minus llm and tool time). See
  [docs/atof-reader.md](docs/atof-reader.md).
- `templates/` — `base.html` (shared chrome + tab bar), `_item_nav.html` (the
  `item_nav` macro every detail page uses), plus one subdirectory per plugin
  (`mem0/`, `prompts/`)
- `tests/` — `conftest.py` (shared fixtures), `test_app.py` (shell),
  `test_mem0.py`, `test_prompts.py`, `test_tabs.py`, `test_atof_reader.py`,
  `test_assembler.py`, `test_tailer.py`
- `docs/` — [writing-a-plugin.md](docs/writing-a-plugin.md),
  [plugins-and-urls.md](docs/plugins-and-urls.md),
  [startup-and-console.md](docs/startup-and-console.md),
  [atof-reader.md](docs/atof-reader.md),
  [span-rendering.md](docs/span-rendering.md),
  [live-pages.md](docs/live-pages.md),
  [setup-prompt-timing.md](docs/setup-prompt-timing.md), and `adr/` for the
  architecture decision records
