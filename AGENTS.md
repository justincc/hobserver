# AGENTS.md

## Project
hermes-observer (formerly jmem0-logged-browser): Flask webapp for observing
hermes-agent activity. Views are plugins rendered as horizontal tabs: memory (`jmem0_logged.db`, the mem0 event log) and prompt
timing (per-turn waterfalls from the NeMo Relay ATOF JSONL). See README.md
for pages, layout, and data-source path resolution; see docs/adr/ for the
architecture decisions (ATOF as timing source, direct JSONL reading with no
ETL, blueprints-as-plugins).

- Run: `uv run python app.py` — no arguments or env vars needed when
  `HERMES_HOME` is exported: both the memory db and the ATOF log default to
  paths under `hermes_config_dir()` (normalized `$HERMES_HOME`, else a
  literal fallback), overridable by argv/`JMEM0_DB` and `ATOF_LOG`
  respectively. Startup prints the resolved paths and whether each exists
  (once, in the reloader supervisor — the worker sets WERKZEUG_RUN_MAIN),
  and `plugins.memory.check_db` gates the memory db before serving: exists,
  is a regular file, and a row reads from `events` over a read-only
  connection, else the banner marks it UNUSABLE (dropping the "listening"
  lines, since it will not) and main exits naming the problem. An existence
  check alone was not enough — `app.py .` made DB_PATH the *directory*,
  which exists, and sqlite reads the file header at connect, so every
  request died with a bare `disk I/O error` (EISDIR) that reads like failing
  hardware rather than a wrong path. The ATOF log stays exists-only: it is
  allowed to be missing and the timing tab says so itself.
  `request_log.py` keeps the console usable despite the 2-3 s live polls:
  a `logging.Filter` (`SuppressSuccessFilter`) on the werkzeug access logger
  (dev server only) drops every successful (2xx/3xx) response on every path,
  so only errors are logged. This was deliberately simplified from an
  earlier per-path first-success-plus-pointer scheme — if fine-grained
  logging is ever wanted back, add a setting rather than reviving the
  complexity. The startup banner's `status` line tells the user successes
  are not logged and points to `/_status`. The filter parses werkzeug's
  `'"%s" %s %s' % (request_line, code, size)` record args — coupled to the
  dev server's log shape, so check it on a werkzeug major bump. The
  always-on tally behind `/_status` (an `after_request` hook into
  `app.extensions["request_stats"]`, excluding `/_status` itself) is what
  answers "is anything still reaching the server?" once the log is quiet:
  no response = server down, stale last-seen = browser stopped polling,
  non-200s = polls failing. `/_status` self-refreshes via a `Refresh`
  header (not the live-poll script — keeps the body plain text for curl,
  no template); its header line carries a
  clock because counts look the same live or frozen.
  Serves on port 5090; template and
  .py edits are picked up without a restart — template auto-reload plus the
  Werkzeug reloader; debug stays off so the interactive debugger is never
  exposed on 0.0.0.0. Producer-side setup (nemo-relay install,
  plugin enable, `HERMES_NEMO_RELAY_*` in `~/.hermes/.env`) is documented
  in docs/setup-prompt-timing.md.
- Test: `uv run pytest`
- Every view is read-only over a log produced by another process; the
  browser never owns or mutates data.
- `app.py` is the shell: app factory (`create_app(db_path, atof_path=None)`)
  so tests can point it at temporary sources, plugin registration, tab list,
  and legacy-URL redirects. Plugins live in `plugins/<name>` (module or
  package), each exposing a blueprint `bp` (registered under `/<bp.name>/`)
  and a `TAB_LABEL`; their templates live in `templates/<name>/`. The ATOF
  reader is three layers in `plugins/timing/`: `tailer.py` (byte-offset
  incremental read; app-lifetime instance in `app.extensions`; split the
  chunk on `"\n"` only — `str.splitlines()` also breaks on U+0085/U+2028/
  U+2029, which JSON does not require escaping and hermes' assistant text
  contains verbatim, shredding whole records into unparseable fragments;
  when the shredded record was a `hermes.turn.end` its turn stayed open
  forever, so a long-finished turn kept polling at 2 s),
  `atof_reader.py` (parser; fixtures include the verbatim example lines
  from the ATOF v0.1 spec) and `assembler.py` (span pairing by uuid, turns
  bounded by hermes.turn.start/end marks, span→turn assignment by turn_id
  with a timestamp-containment fallback, session via metadata or
  parent_uuid; the turn-start mark's data carries the hermes hook kwargs —
  user_message becomes Turn.user_message, shown as prompt snippets in the
  turn list / in-flight strip and collapsible on the turn page. Span start
  payloads carrying `command`/`workdir` (terminal tool scopes; the
  command wraps in full in detail mode, line breaks kept — code takes
  `pre-wrap`, unlike the `normal` prose details wrap with), `path`
  (file tool scopes, left-ellipsized to keep the tail), `query`
  (web_search/mem0_search scopes; wraps in full in detail
  mode). session_search is its own scope with four modes (checked against
  `$h/tools/session_search_tool.py`), rendered mode-aware in one branch:
  `Span.session_search_mode` prefers the end payload's explicit `mode`,
  falling back to inferring it from the start-payload keys with the tool's own
  dispatch precedence (an `around_message_id` anchor ⇒ scroll, else a
  `session_id` ⇒ read, else a `query` ⇒ discover, else browse) so a still-open
  span resolves too. A muted `.mode-tag` names the mode; `session_search
  _summary` is the inline one-liner from the start payload (discover: the
  query; scroll: `session <id> · around msg <n> · window <w>`; read: `session
  <id>`; browse: `recent sessions`); `session_search_stats` is a list of
  {label,value,tooltip} rows drawn from the end payload, shown on detail-only
  `.list-item` rows, one per count the mode reports — discover: `count`
  (sessions actually returned, lower than searched when a match is title-only
  or its anchored view can't be built) + `sessions searched` (distinct
  matching sessions deduped by lineage and capped at the limit, NOT the corpus
  scanned, so count ≤ sessions_searched ≤ limit); scroll: `before`/`after`
  (messages outside the returned window); read: `messages` total plus a
  `truncated` flag; browse: `sessions` listed. The tooltip on each label
  carries the same explanation. The `patch` scope is likewise one branch
  over two modes (checked against patch_tool in `$h/tools/file_tools.py`),
  named by a `.mode-tag`: "replace" (the default — `path` plus
  `old_string`/`new_string`, so it must be matched *before* the plain-path
  branch it would otherwise fall into) and "patch" (a V4A multi-file
  `patch` text and no top-level path; `Span.patch_paths` lists the files
  from its "*** <Op> File:" headers, first path plus a "+N more" count).
  `Span.patch_mode` prefers the payload's explicit `mode`, falling back to
  the keys present (a `patch` text ⇒ patch, else the tool's own default,
  replace). Detail mode adds the payload the summary line can't carry:
  the two replaced sides for replace, the whole V4A text for patch —
  never the end payload's `diff`/`files_modified`/`lint`, which nothing
  reads today. `urls` (web_extract scopes; first url
  plus a "+N more" count inline, every url on its own line in detail
  mode), `code` (execute_code scopes; first line inline, the whole
  program in detail mode),
  `image_url`/`question` (vision_analyze scopes; the image path/URL
  left-ellipsized inline in the summary, the question added on its own
  line in detail mode only),
  `content` (mem0_add scopes; wraps in full in detail mode — mem0_update
  carries the same fact under `text` instead, so `Span.memory_content`
  reads whichever key the scope uses and the fact leads the summary line
  either way. mem0_update and mem0_delete also carry a `memory_id`, kept
  to a detail-only row (faint mono, copy button — the lookup key back to a
  mem0_search result): on the summary line it reads as a second uuid
  beside the span's own, which lands there as soon as the row is expanded.
  A mem0_delete therefore shows nothing until opened — the id is its
  entire payload. The four mem0 tools are defined in
  `$h/plugins/memory/mem0/__init__.py`, i.e. in the memory plugin, NOT in
  `$h/tools/` where the rest live), `todos`
  (todo scopes; first item's
  content plus a "+N more" count inline, every item on its own line in
  detail mode), `tasks`/`goal`/`context` (delegate_task scopes; first
  goal plus count inline, full goals with contexts nested beneath in
  detail mode — hermes.subagent.start marks show their `child_goal`
  the same way, subagent.stop marks show `child_status` plus the
  echoed goal, and both carry a per-turn #N tag pairing start with
  stop via `child_session_id`) or, on
  skill_view/skill_manage scopes, `name`/`file_path`/`action` plus the
  `category` a skill_manage "create" carries (shown before the skill name,
  middot-separated; absent on patch/write_file, which have no category) and
  the `absorbed_into` a "delete" that merged the skill elsewhere carries
  (rendered "→ absorbed into <skill>") and the `old_string`/`new_string` a
  "patch" carries — note skill_manage patches replace text, they never
  carry a V4A patch text the way the file tools' patch scope does in patch
  mode; the pair matches that scope's *replace* mode instead, and both
  render through the one `diff_rows` macro in `templates/timing/turn.html`.
  The two sides
  render on their own detail-mode-only rows (`.list-item`), marked − and +
  (glyph first, tint second — never color alone); `new_string` may be the
  empty string, a patch that deletes the matched text (both tools document
  passing "" for that), so `Span._new_string` — shared by
  `skill_new_string` and `patch_new_string` — reads the payload directly
  instead of `_start_str`,
  which folds "" into None, and the empty side renders in words. They stay
  out of the summary line because a real `new_string` runs to kilobytes of
  markdown. skill_manage's six actions are
  create/edit/patch/delete/write_file/remove_file (checked against
  `$h/tools/skill_manager_tool.py`); the ATOF log to date only exercises
  create/patch/write_file, so edit/delete/remove_file rendering is covered
  by test alone. The `file_path` (skill_view, skill_manage write_file and
  remove_file, and an optional patch target) is middot-
  separated from the skill name and left-ellipsized (`.tail`, like the file
  tools' path) so the filename end survives when the summary line is tight.
  These
  render
  on the turn page — trailing the span name on one ellipsized line by
  default, on their own line when the details switch is on; clicking a
  row toggles just that row's detailed layout (tr.detail-open,
  remembered by uuid across live-poll swaps); the span or
  mark uuid, muted after
  the name with a copy icon, is the raw-JSONL lookup key
  (tool_call_id/api_request_id not shown). The
  `platform` kwarg is `webui` today but hermes has other frontends, e.g.
  a TUI — never assume webui). Pages self-update via the poll-and-swap script in
  `templates/base.html`: wrap content in `data-live-poll="<ms>"` ("0" =
  static; timing index 3 s, in-flight turn 2 s) — no SSE/WebSockets,
  reuses the per-request tailer. Timing pages add an in-flight strip
  (`_inflight.html`, >10 min silent = stale). Turn.end marks go missing
  often, so an open turn is not evidence of running work: "live" is
  `Turn.is_live` = open and not proven finished, and only live turns reach
  the strip, mark `data-inflight-current`, or poll at 2 s. Two proofs
  override open, both exact and immediate where a staleness clock is
  neither: `Turn.superseded` (a later turn started in the same session —
  set in `_build_turns` where that anomaly is already detected) and
  `Assembly.finished_subagent_sessions` (a `hermes.subagent.stop` names
  the session). Never invent an `end_us` for these — the duration was
  never observed (ADR 2); they render "no end mark". Liveness only:
  the turn table and turn pages keep everything. Note a permanently-live
  turn also freezes follow mode, which won't leave a live turn — that
  was the bug supersession fixed, and no time threshold could have,
  since the turn had been silent 2 minutes. Plus a follow-mode toggle
  (localStorage) that auto-opens newly started turns — never while
  already watching an in-flight turn, never to stale entries; the strip
  data-attributes (`data-inflight-start-us`, `data-stale`,
  `data-inflight-current`, `data-turn-start-us`) drive that JS. The
  toggle rides the turn page's event-nav row (all/prev/next), which is
  *inside* the live region, so base.html must never hold a reference to
  it across a swap: it re-resolves the element on each use, resyncs its
  checked state after every swap, and uses a delegated change listener.
  A captured reference detaches on the first poll and the switch dies
  silently. On the index it stands alone above the region. That nav row
  is the `item_nav` macro in `templates/_item_nav.html`, shared by every
  plugin's detail page (timing turn, memory event) so item-by-item
  navigation is identical everywhere: "← all X" first, then muted
  « prev / next » steppers grouped beside it, prev always meaning older.
  New plugins import it rather than rolling their own row; pages wanting
  something else on the row (the follow toggle) pass it via `{% call %}`,
  which space-between drops on the right.
  Waterfall series colors were validated with the dataviz
  six-checks palette validator against the light surface: llm `#2a78d6`,
  tool `#eb6834`, other `#4a3aa7`; span identity is always also in text,
  never color alone.
- Workflow preference: start simple and build functionality up as needed.
- Architecture decisions are recorded as ADRs in docs/adr/ (sequentially
  numbered markdown files).

## General Instructions
- Documentation must be kept up to date with any relevant changes to the project. 
- Keep documentation organized and concise. A new file in docs/ can be split off and referenced from here when necessary.
- All new functionality must have an accompanying passing unit test.
- Document and code should follow the DRY (Don't Repeat Yourself) principle when reasonable.
- Tests should always be run after making any changes and any fails fixed.

## Documentation
- When you learn something important about this project (build commands, architecture decisions, code conventions, debugging insights, workflow preferences. etc), update this file and other documentation to record it.
