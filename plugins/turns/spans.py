"""What a span is, and how hermes' own spans are read.

The middle of the reader (docs/design/adr/0002): `assembler` builds spans out
of paired ATOF events and hangs them off turns; this says what one *holds* and
what its payloads *mean*. Everything a row or a page shows about a single span
comes from here.

Split from `assembler.py`, which had grown to do both and was named for only
one of them. The rule that decides which file a thing belongs in:

- **Reading** — a fact about one span, worked out from its own payloads.
  Here.
- **Assembly** — anything that needs more than one event to answer: pairing a
  start with an end, placing a span in a turn, bounding a turn, summing a
  session. `assembler.py`.

The dependency runs one way, `assembler` → `spans`, and must keep doing so:
a span does not know what turn it is in, and a test pins that.

**These readings are hermes' tools, and only hermes'.** Another system's spans
are read by whoever owns that system — `plugins/memory/mem0/spans.py` is the worked
example — and reach a spec as contributed readers (docs/design/adr/0017). A
`Span` property is this tab reading its own tool, not the place for
everyone's.

**Payloads are opaque per the ATOF spec**, so every read here type-guards and
none of them raises on a shape it did not expect. See
docs/design/atof-reader.md for the mapping and the traps, and
docs/design/span-rendering.md for what each of these ends up showing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, List, Optional

from plugins.turns.atof_reader import (AtofEvent, LineRef,
                                         generic_payload_fields)

# The ATOF `category` of a span, which is what identifies a model call
# wherever its name is the provider's rather than the tool's.
AGENT_CATEGORY = "agent"
LLM_CATEGORY = "llm"
TOOL_CATEGORY = "tool"

# hermes wraps a prompt before it goes on the wire. The plugin's turn mark
# carried `user_message` already unwrapped; reading the wire message back is
# the only route left, so the two known wrappers come off again here.
# Anything unrecognized is left whole rather than cut at a guess.
WORKSPACE_PREFIX = re.compile(r"\A\[[A-Za-z_]+::v\d+:[^\]]*\]\n+")
MEMORY_CONTEXT = re.compile(r"\n*<memory-context>.*?</memory-context>\n*",
                            re.DOTALL)


# How much of an assistant message shows on the span. It is a start, not the
# message: these routinely run to thousands of characters, so the row shows
# the opening and then says how many there were.
LLM_TEXT_PREVIEW_CHARS = 400



# The token counts, as the tree they actually form rather than the flat line
# they used to be rendered as. The three relations behind the indent do not
# hold equally firmly, and it is worth knowing which is which:
#
#   cache read + in + cache write == prompt   structural, and true by
#       construction rather than by luck: whichever of `prompt` and `in` a
#       producer did not report, `atof_reader` derives from the other and
#       the cache counts, so the relation is the definition of the missing
#       figure. Which one is reported varies by provider convention.
#   reasoning <= out                          observed, not enforced.
#
# So the children of `prompt` are its parts by construction, and `reasoning`
# is a slice of `out` — counted within it, not added to it. A flat list
# would have said these were six peers.
#
# depth is the indent; subset marks a row that is part of its parent rather
# than a share of it, which is the difference between a figure that can be
# added up and one that must not be. Note that "the leaves are reported and
# the parents derived" is *not* reliably true: on Anthropic's convention the
# leaf `in` is the reported figure and the parent `prompt` is the sum.
#
# There is deliberately no `total` row. It is `prompt + out`, both of which
# are here — and it is the one figure the tree could not vouch for, since
# hermes' codex path takes the provider's reported total over the computed
# sum. Nothing on screen now depends on that agreement. Depth 1 is where the
# buckets it used to parent still sit: the template's `tokens` label heads
# the tree in its place, carrying no number of its own.
TOKEN_TREE = (
    ("prompt_tokens", "prompt", 1, False,
     "What the model was sent, however it was served — the rows below it "
     "sum to this. The share is how much of it came from the cache, rounded "
     "to whole percent; it never reads 100% unless every last token did."),
    # cache read leads: how much of a prompt the provider already had is the
    # question these rows are usually being read to answer, and `in` reads as
    # the remainder — what was new — once it follows.
    ("cache_read_tokens", "cache read", 2, False,
     "Prompt tokens the provider served from its cache instead of "
     "processing again. This can be most of a prompt, and is often why one "
     "call is quicker than another."),
    ("input_tokens", "in", 2, False,
     "Prompt tokens sent fresh, for the provider to process on this call."),
    ("cache_write_tokens", "cache write", 2, False,
     "Prompt tokens this call processed and the provider stored for later "
     "calls to read. Only providers with an explicit cache report this; it "
     "stays absent otherwise."),
    ("output_tokens", "out", 1, False,
     "Everything the model generated: its reasoning, its reply, and the "
     "arguments of any tool calls."),
    ("reasoning_tokens", "reasoning", 2, True,
     "Hidden reasoning, counted within out rather than added to it. What is "
     "left of out is not simply the reply — it is the reply plus the "
     "arguments of any tool calls, which are shown on the spans below "
     "rather than here."),
    ("request_count", "requests", 0, False,
     "How many provider API calls these figures cover — 1 unless the usage "
     "of several was summed."),
)

# Counts that keep their row at zero, where every other count loses it.
#
# `cache read` and `in` are the split the cache share is read off — cached
# against fresh — and a reader opening the detail view is there to see that
# split, not to infer it from a missing row. `cache read 0` beside
# `in 18,824` states the cold call; a bare `prompt` leaves the reader
# checking whether the row is missing or the figure is.
#
# `cache write` is not in the set: on the codex route hermes hard-codes it to
# zero rather than measuring it (`codex_runtime.py`), so its zero would be a
# claim this app cannot make. `requests` is here because it is always 1.
ALWAYS_SHOWN = ("cache_read_tokens", "input_tokens", "request_count")




def _preview(text: str) -> dict:
    """A long string as much of it as a row can hold: {text, truncated}.

    One cut for every excerpt on a span row, so two of them side by side end
    at the same place. The whole string never reaches the page — not even in
    a title attribute, where a 26 KB prompt would ride every poll of a live
    turn — it is a click away instead (ADR 12).

    `truncated` drives an ellipsis and nothing else. There was a `chars`
    beside it, for a row reading `start of 2,579 chars`; the row went when
    the text became clickable, and the count went with it rather than
    staying as a value nothing reads.
    """
    return {"text": text[:LLM_TEXT_PREVIEW_CHARS],
            "truncated": len(text) > LLM_TEXT_PREVIEW_CHARS}


def _as_dict(data: Any) -> Optional[dict]:
    """Payload as a dict when it is one — directly or as a JSON string.
    Data is opaque per the ATOF spec, so never assume shape."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


# V4A patch operation headers, per hermes tools/patch_parser.py
_PATCH_FILE_HEADERS = ("*** Update File:", "*** Add File:",
                       "*** Delete File:", "*** Move File:")


@dataclass
class Span:
    uuid: str
    name: str
    category: Optional[str]
    session_id: str
    parent_uuid: Optional[str]
    start_us: int
    end_us: Optional[int]           # None while open (in flight, or end lost)
    metadata: dict
    category_profile: dict          # of the start event
    start_data: Any
    end_data: Any
    model_name: Optional[str]
    tool_call_id: Optional[str]
    api_request_id: Optional[str]
    turn_id: Optional[str]
    line_no: int                    # of the start event

    # --- ADR 11 -----------------------------------------------------------
    # Where each of this span's two lines is in the log, so `hydrate_turn`
    # can read the payloads back for the one turn being viewed; what the
    # index kept when it left them there; and whether they are still out
    # there. A span assembled straight from parsed lines leaves all of these
    # at their defaults and behaves exactly as before.
    start_ref: Optional[LineRef] = None
    end_ref: Optional[LineRef] = None
    projection: dict = field(default_factory=dict)
    payload_elided: bool = False
    payload_problem: Optional[str] = None
    # Last `llm.chunk` seen under this span. Chunks are 94% of the log's
    # lines and reach no template, so the index folds them into these two
    # values rather than carrying them as events (ADR 11): when the stream
    # was last alive, and the token counts the final chunk reported.
    stream_last_us: Optional[int] = None
    stream_usage: Optional[dict] = None
    # Which store entry each of this span's memory ops matched, by position
    # in `memory_ops`: {index: {"entry": str|None, "note": str}}. Filled by
    # `resolve_memory_entries` for the turn being viewed and empty for every
    # other span, because it is read off *other* spans of the same turn.
    memory_entries: dict = field(default_factory=dict)

    @property
    def last_activity_us(self) -> int:
        """The last moment anything was seen of this span."""
        return max(x for x in (self.start_us, self.end_us, self.stream_last_us)
                   if x is not None)

    @property
    def duration_us(self) -> Optional[int]:
        if self.end_us is None:
            return None
        return self.end_us - self.start_us

    @property
    def is_open(self) -> bool:
        return self.end_us is None

    # symmetry with AtofEvent.is_mark so templates branch on one flag
    @property
    def is_mark(self) -> bool:
        return False

    # data payloads are opaque per the ATOF spec and vary in practice —
    # e.g. the nemo_relay plugin emits hermes tool results as raw JSON
    # strings — so every field access must type-guard, never assume dict.
    @property
    def usage(self) -> Optional[dict]:
        """The call's token counts, from wherever this provider reported them.

        The end payload first, then the stream. A provider that reports
        usage only on the final chunk — the openrouter route does, and
        leaves `usage` null on the end event — would otherwise show no
        token counts at all, when the log holds every one of them.
        """
        if isinstance(self.end_data, dict):
            reported = self.end_data.get("usage")
            if isinstance(reported, dict) and reported:
                return reported
        return self.stream_usage or None

    @property
    def finish_reason(self) -> Optional[str]:
        if isinstance(self.end_data, dict):
            value = self.end_data.get("finish_reason")
            if isinstance(value, str):
                return value
        return None

    # Every tool that fails says so the same two ways, whatever the scope:
    # metadata.status is "error" on the end event, and the end payload
    # carries an "error" string. That is how the tools in $HERMES_SOURCE/tools/ report
    # errors, and it has held for every failing call seen here — terminal,
    # patch, read_file, search_files, write_file, execute_code, web_search,
    # skill_view, skill_manage, memory — so this is one generic pair rather
    # than per-scope readers. A failed call used to look exactly like a
    # successful one here.
    @property
    def failed(self) -> bool:
        return self.metadata.get("status") == "error"

    @property
    def error(self) -> Optional[str]:
        """The tool's own failure message. Shown whenever it is present,
        even if the status did not say error — ADR 2's loud-failure rule."""
        end = _as_dict(self.end_data)
        if end is None:
            return None
        value = end.get("error")
        return value if isinstance(value, str) and value else None

    def _start_str(self, key: str) -> Optional[str]:
        data = _as_dict(self.start_data)
        if data is not None:
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    # --- llm scopes ---------------------------------------------------
    # An llm span's end payload holds what the model decided, what it said
    # and what it cost. Read here rather than left to the generic fallback,
    # which only ever reads a start payload — and an llm start carries an
    # empty headers dict and a usually-empty content.
    @property
    def is_llm(self) -> bool:
        return self.category == "llm"

    # What this call was *for*, when it was not the ordinary one. hermes
    # stamps `call_role` on every llm span, and its value space is wider
    # than it looks (checked against the emit sites in $HERMES_SOURCE/agent/, not just
    # the log): "primary", "delegated" for a subagent's call, "fallback"
    # when the configured model failed and a backup answered,
    # "iteration_summary", and "auxiliary:<task>" for work like context
    # compression. The auxiliary tasks are open-ended — hermes exposes
    # `register_auxiliary_task` to plugins — so the value is shown as it
    # arrives rather than matched against a list this app maintains.
    #
    # `primary` is withheld: it is 216 of the 218 calls in the log, and a
    # tag every row carries is one no row is read for. Its absence is what
    # says the call was ordinary.
    #
    # hermes also stamps `auxiliary_task` beside it, which is not read
    # here: the two are built from one value in the same dict literal
    # (`$HERMES_SOURCE/agent/auxiliary_client.py`), `call_role` being the f-string
    # "auxiliary:{task}". They cannot disagree, and only `call_role` is
    # present on the non-auxiliary roles.
    @property
    def call_role(self) -> Optional[str]:
        role = self.metadata.get("call_role")
        if not isinstance(role, str) or not role or role == "primary":
            return None
        return role

    # Which attempt this was, 0-based, within one request — so a non-zero
    # value means the call was retried or walked its fallback chain, which
    # is often the whole reason a span is slow. Zero is withheld for the
    # same reason a zero cache write is: it is every row, and says nothing.
    @property
    def retry_count(self) -> Optional[int]:
        count = self.metadata.get("retry_count")
        if isinstance(count, bool) or not isinstance(count, int) or count <= 0:
            return None
        return count

    @property
    def request_prompt(self) -> Optional[str]:
        """The prompt this llm call was answering, from its own request.

        Only meaningful for the first call of a turn — later ones carry the
        same conversation with the assistant's own work appended. The
        assembler uses it for exactly that, and it is the only route left to
        a turn's prompt: the core runtime's turn scope has an empty payload
        where the plugin's turn mark carried `user_message` outright.

        This is a reconstruction, not a value hermes emitted: it is the last
        user-role message on the wire, with hermes' own wrappers taken back
        off. For an agent-initiated turn (a curator pass, say) that message
        is hermes' own instruction rather than a person's, which is correct
        — it is still the prompt the turn ran on.

        Assembly needs this to name a turn the marks never described, and it
        is buried in the largest payload in the log, so it is one of the two
        strings the index projects (ADR 11). Read from there when it is
        there; the reconstruction below is what put it there in the first
        place, and still runs when the payload is at hand.
        """
        projected = self.projection.get("request_prompt")
        if projected is not None:
            return projected or None
        return request_prompt_from_profile(self.category_profile)

    @property
    def _assistant_message(self) -> Optional[dict]:
        end = _as_dict(self.end_data)
        if end is None:
            return None
        return _as_dict(end.get("assistant_message"))

    @property
    def llm_tool_calls(self) -> List[str]:
        """The tools the model asked for, in the order it asked.

        Names only. The spans below ran these and carry their arguments,
        rendered by a branch written for each tool, so repeating the
        arguments here would restate the waterfall — which is why this row
        was left out to begin with. What it adds is that the calls were one
        decision: 441 assistant turns in the log fan out to two or more, and
        one to sixteen, which a column of separate rows does not say.

        A call whose name is missing still takes a slot. The count is the
        point, so dropping one silently would be the wrong kind of tidy.
        """
        message = self._assistant_message
        if message is None:
            return []
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            return []       # payloads are opaque; a dict or a string here is not a list
        names = []
        for call in calls:
            if not isinstance(call, dict):
                names.append("(unreadable)")
                continue
            name = call.get("name")
            names.append(name if isinstance(name, str) and name else "(unnamed)")
        return names

    @property
    def llm_text(self) -> Optional[dict]:
        """The assistant's own words, as {text, truncated}.

        Empty whenever the model was calling tools rather than talking, and
        long enough otherwise that what shows is the start of it, with the
        whole a click away (ADR 12).
        """
        message = self._assistant_message
        if message is None:
            return None
        text = message.get("content")
        if not isinstance(text, str) or not text:
            return None
        return _preview(text)

    @property
    def request_prompt_excerpt(self) -> Optional[dict]:
        """What this call was asked, as much of it as a row can hold.

        The same shape and the same cut as `llm_text`, because they are the
        same kind of thing on adjacent rows.
        """
        prompt = self.request_prompt
        return _preview(prompt) if prompt else None

    @property
    def reasoning_effort(self) -> Optional[str]:
        """How hard this call asked the model to think, e.g. "low".

        A *request* parameter, so it is read from the call's own start
        payload (`content.reasoning.effort`) rather than from what came
        back — unlike everything else on the llm span, which is in the end
        event. hermes sends it on the codex route as OpenAI's
        `reasoning.effort`, and the values seen are "low", "medium" and
        "high".

        Absent is left absent: a request that named no `reasoning` at all
        gets no value here, and the row does not render. hermes-ui's "None"
        and "Minimal" both reach a model like gpt-5.6 — which has no such
        effort — as an omitted (or "low") reasoning, so an inferred level
        would claim a distinction the payload does not carry. What is shown
        is what was sent.
        """
        content = _as_dict((_as_dict(self.start_data) or {}).get("content"))
        if content is None:
            return None
        reasoning = _as_dict(content.get("reasoning"))
        if reasoning is None:
            return None
        effort = reasoning.get("effort")
        return effort if isinstance(effort, str) and effort else None

    # The two values the excerpts above are excerpts *of*, whole and
    # untruncated, for the page that shows one on its own (ADR 12). Both are
    # in the payloads the index leaves in the log, so both read as nothing
    # until the span is hydrated — which the full page does for the one span
    # it is showing.
    @property
    def llm_response_text(self) -> Optional[str]:
        """Everything the model said, of which `llm_text` shows the start.

        The same string, from the same key: an excerpt that is not a prefix
        of what its link opens would be a quiet lie.
        """
        message = self._assistant_message
        if message is None:
            return None
        text = message.get("content")
        return text if isinstance(text, str) and text else None

    @property
    def llm_request_messages(self) -> Optional[list]:
        """The whole request this call was sent, one labelled section per
        message — see `request_sections_from_profile`.

        Not an excerpt of anything. `request_prompt` beside it is the *last
        user message* of this same request, which is the excerpt the turn
        page shows; this is the conversation it sat at the end of.
        """
        return request_sections_from_profile(self.category_profile)

    @property
    def token_rows(self) -> List[dict]:
        """The call's token counts as rows of the TOKEN_TREE.

        Rows marked `summary` — the top-level buckets, `prompt` and `out` —
        are what a collapsed waterfall row shows; the rest are detail-only,
        because a collapsed row inlines its details onto one clipped line and
        every figure past the cut reads as a real one. Two short figures fit
        where the whole tree would not, and a cache read can be most of a
        prompt, so these belong on the page rather than in the bar's tooltip
        where they were.

        Zero counts are dropped except for ALWAYS_SHOWN — and because the
        leaves partition their parent, dropping one still leaves what is
        shown adding up. On a cold call `prompt` and `in` therefore carry the
        same figure an indent apart; that repetition is the price of the
        detail view always showing the cached/fresh split, rather than
        leaving a reader to infer a zero from an absent row.
        """
        usage = self.usage
        if not usage:
            return []

        def count(key: str) -> Optional[int]:
            value = usage.get(key)
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None     # payloads are opaque; a string here is not a count
            return value

        rows = []
        for key, label, depth, subset, tooltip in TOKEN_TREE:
            value = count(key)
            if value is None:
                continue        # not reported — which is not the same as zero
            if key not in ALWAYS_SHOWN and not value:
                continue        # a zero cache write says nothing worth a row
            rows.append({"label": label, "value": f"{value:,}",
                         "share": self._cache_share(key, value),
                         "depth": depth, "subset": subset,
                         # the buckets sit at depth 1, under the `tokens`
                         # label, and are what a collapsed row keeps;
                         # requests is outside the tree at depth 0 and does
                         # not earn a place on that line.
                         "summary": depth == 1,
                         "tooltip": tooltip})
        return rows

    @property
    def usage_summary(self) -> Optional[str]:
        """The tree's top-level buckets on one line, for the bar's tooltip.

        Built from `token_rows`, so it names the counts the way the rows do
        and can never disagree with them. None when no bucket was reported:
        the tooltip used to interpolate `?` for a missing figure, which read
        as a count the provider had withheld rather than one this app had
        gone looking for in the wrong place.
        """
        parts = [f"{row['label']} {row['value']}"
                 for row in self.token_rows if row["summary"]]
        return " / ".join(parts) + " tokens" if parts else None

    def _cache_share(self, key: str, prompt: int) -> Optional[str]:
        """`89% cached` for the prompt row — how much of it was served.

        On the summary line this is the only place the cache shows at all,
        the parts being detail-only. Whole percent, rounded half up rather
        than to even, since a reader comparing rows expects .5 to go up.

        A cold prompt reads `0% cached` rather than dropping the note: the
        provider reported the zero, and every llm row saying the same thing
        in the same shape is worth more than the character it saves. Absent
        is different from zero — a payload that never reported a cache read
        gets no share at all, since this app cannot tell nothing-cached from
        nothing-said.

        Capped at 99 unless the prompt was cached to the last token: 2.4% of
        the calls in the log round to 100 with hundreds of tokens still
        fresh, and a figure that says everything was cached when it was not
        is worse than one percentage point of imprecision.
        """
        usage = self.usage
        if key != "prompt_tokens" or not usage or prompt <= 0:
            return None
        cached = usage.get("cache_read_tokens")
        if not isinstance(cached, int) or isinstance(cached, bool) or cached < 0:
            return None     # not reported — which is not the same as none
        percent = (cached * 200 + prompt) // (2 * prompt)    # floor(x + 0.5)
        if percent >= 100 and cached < prompt:
            percent = 99
        return f"{percent}% cached"

    @property
    def generic_fields(self) -> List[dict]:
        """The start payload of a scope nothing here renders specially.

        hermes' tool set is not this app's to know: with tools these branches
        have never heard of, a reader would otherwise get a name, a duration
        and nothing about the call. The template falls back to this when no
        scope branch matched — see `generic_payload_fields`.
        """
        return generic_payload_fields(self.start_data)

    # terminal tool scopes carry the invocation in their start payload
    @property
    def command(self) -> Optional[str]:
        return self._start_str("command")

    @property
    def workdir(self) -> Optional[str]:
        return self._start_str("workdir")

    # file tool scopes (patch, read_file, write_file, search_files, …)
    # carry the operated-on path in their start payload
    @property
    def path(self) -> Optional[str]:
        return self._start_str("path")

    # the patch scope has two modes (checked against patch_tool's signature
    # in $HERMES_SOURCE/tools/file_tools.py): "replace" (the default — path plus
    # old_string/new_string) and "patch" (a V4A multi-file patch text, no
    # top-level path). The payload names the mode, but fall back to the
    # keys present so a start-only span still resolves.
    @property
    def patch_mode(self) -> Optional[str]:
        if self.name != "patch" or _as_dict(self.start_data) is None:
            return None
        mode = self._start_str("mode")
        if mode:
            return mode
        return "patch" if self.patch_text else "replace"

    @property
    def patch_text(self) -> Optional[str]:
        return self._start_str("patch") if self.name == "patch" else None

    # patch-mode patch scopes carry no top-level path: the touched files
    # live in the V4A patch text's "*** <Op> File:" headers (see hermes
    # tools/patch_parser.py); a move header keeps its "old -> new" whole
    @property
    def patch_paths(self) -> List[str]:
        text = self.patch_text
        if not text:
            return []
        paths = []
        for line in text.splitlines():
            for header in _PATCH_FILE_HEADERS:
                if line.startswith(header):
                    value = line[len(header):].strip()
                    if value and value not in paths:
                        paths.append(value)
        return paths

    # search_files scopes carry the query in their start payload; "pattern"
    # is too generic a key to trust on other scopes
    @property
    def search_pattern(self) -> Optional[str]:
        return self._start_str("pattern") if self.name == "search_files" else None

    @property
    def file_glob(self) -> Optional[str]:
        return self._start_str("file_glob") if self.name == "search_files" else None

    # web_search and mem0_search scopes carry their search query in the start
    # payload; "query" is too generic a key to trust on other scopes.
    # session_search also has a query but only in one of its four modes, so it
    # is handled by the mode-aware properties below rather than here.
    @property
    def search_query(self) -> Optional[str]:
        if self.name in ("web_search", "mem0_search"):
            return self._start_str("query")
        return None

    # mem0's payloads are read in `plugins/memory/mem0/spans.py`, by the plugin that
    # owns that tool, and reach a spec as span readers (ADR 17). Nothing here
    # knows what a mem0 search result looks like — the properties that did
    # (`mem0_results`, `mem0_result_count`) moved there whole.

    # session_search is a single scope with four modes (see hermes
    # tools/session_search_tool.py): discover (search by query), scroll (a
    # window around an anchor message), read (a whole session), browse (recent
    # sessions). The end payload names the mode outright, but that is absent
    # while the span is open or if its end mark is lost, so fall back to
    # inferring it from the start-payload keys using the tool's own dispatch
    # precedence: an anchor means scroll, else a session_id means read, else a
    # query means discover, else browse.
    @property
    def session_search_mode(self) -> Optional[str]:
        if self.name != "session_search":
            return None
        end = _as_dict(self.end_data)
        if end is not None:
            mode = end.get("mode")
            if isinstance(mode, str) and mode:
                return mode
        start = _as_dict(self.start_data)
        if start is None:
            return None
        if start.get("session_id") and start.get("around_message_id") is not None:
            return "scroll"
        if start.get("session_id"):
            return "read"
        if start.get("query"):
            return "discover"
        return "browse"

    # One-line summary of what a session_search span targets, from the start
    # payload (available while the span is still open). None leaves just the
    # mode tag showing.
    @property
    def session_search_summary(self) -> Optional[str]:
        mode = self.session_search_mode
        if mode is None:
            return None
        start = _as_dict(self.start_data) or {}
        if mode == "discover":
            query = start.get("query")
            return query if isinstance(query, str) and query else None
        if mode == "scroll":
            parts = []
            sid = start.get("session_id")
            if isinstance(sid, str) and sid:
                parts.append(f"session {sid}")
            anchor = start.get("around_message_id")
            if anchor is not None:
                parts.append(f"around msg {anchor}")
            window = start.get("window")
            if isinstance(window, int) and not isinstance(window, bool):
                parts.append(f"window {window}")
            return " · ".join(parts) or None
        if mode == "read":
            sid = start.get("session_id")
            return f"session {sid}" if isinstance(sid, str) and sid else "whole session"
        if mode == "browse":
            return "recent sessions"
        return None

    # Detail-mode result stats for a session_search span, drawn from the end
    # payload — a list of {label, value, tooltip} rows, one per meaningful
    # count the mode reports. Empty while the span is open (no end payload).
    @property
    def session_search_stats(self) -> list:
        if self.name != "session_search":
            return []
        end = _as_dict(self.end_data)
        if end is None:
            return []
        mode = end.get("mode")
        stats: List[dict] = []

        def add_int(key: str, label: str, tooltip: str) -> None:
            value = end.get(key)
            # bool is an int subclass; the payload also carries booleans
            if isinstance(value, bool) or not isinstance(value, int):
                return
            stats.append({"label": label, "value": value, "tooltip": tooltip})

        if mode == "discover":
            add_int("count", "count",
                    "Session entries actually returned — a matched session is "
                    "dropped when it is title-only or its anchored view can't be "
                    "built, so this can be lower than sessions searched.")
            add_int("sessions_searched", "sessions searched",
                    "Distinct matching sessions collected before building result "
                    "views: deduped by session lineage and capped at the requested "
                    "limit — not the size of the corpus scanned. count ≤ sessions "
                    "searched ≤ limit.")
        elif mode == "scroll":
            add_int("messages_before", "before",
                    "Messages in the session before the returned window around the "
                    "anchor message.")
            add_int("messages_after", "after",
                    "Messages in the session after the returned window around the "
                    "anchor message.")
        elif mode == "read":
            add_int("message_count", "messages",
                    "Total messages in the session. When truncated, only the first "
                    "+ last slice is returned; scroll with around_message_id to read "
                    "the middle.")
            if end.get("truncated") is True:
                stats.append({"label": "truncated", "value": "",
                              "tooltip": "The session was longer than the read "
                              "window, so only the first + last slice is shown."})
        elif mode == "browse":
            add_int("count", "sessions",
                    "Number of recent sessions listed.")
        return stats

    # The `memory` scope is a different tool from mem0's
    # (checked against $HERMES_SOURCE/tools/memory_tool.py): bounded, file-backed,
    # §-delimited entries in two char-limited stores under
    # $HERMES_HOME/memories/ — MEMORY.md (the agent's own notes, `target`
    # "memory") and USER.md (who the user is, `target` "user") — injected
    # into the system prompt as a snapshot at session start. mem0 is the
    # searched-on-demand store; this one is always in the prompt, which is
    # why its writes are mostly entries being shortened to fit the budget.
    #
    # Two shapes, dispatched by the tool in this order: an `operations` list
    # of {action, content?, old_text?} applied atomically, else a single
    # top-level {action, content?, old_text?}. Both are normalized to one
    # list here so the turn page renders them the same way.
    @property
    def memory_action(self) -> Optional[str]:
        """The mode tag: add/replace/remove, or batch for the list shape.

        `operations` wins over an explicit `action` because the tool
        dispatches on it first — and a staged batch replayed from the
        approval queue carries both (action "batch" plus the list).
        """
        if self.name != "memory":
            return None
        data = _as_dict(self.start_data)
        if data is None:
            return None
        if isinstance(data.get("operations"), list):
            return "batch"
        action = data.get("action")
        return action if isinstance(action, str) and action else None

    @property
    def memory_target(self) -> Optional[str]:
        # which of the two stores was written: "memory" or "user"
        return self._start_str("target") if self.name == "memory" else None

    @property
    def memory_ops(self) -> list:
        """Every write in the span, batch or single, as {action, old_text,
        content}. old_text is the entry matched (a short unique substring,
        not an id — the tool matches by containment); content is what
        replaces it, so an add has only content and a remove only old_text.
        `text` is whichever of the two the summary line should carry.

        Three more keys come from `resolve_memory_entries`, which is the
        only thing that fills `memory_entries` in: `old_entry` is the whole
        entry the fragment matched where the turn itself said what the store
        held, `old_shown` is what the − side displays (that entry, else the
        fragment as logged), and `old_entry_note` is the provenance line the
        page must print beside it. Unresolved, they are None, None and the
        reason — never a silent fallback to the fragment.
        """
        if self.name != "memory":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        ops = data.get("operations")
        if not isinstance(ops, list):
            ops = [data]
        out = []
        for op in ops:
            if not isinstance(op, dict):
                continue
            action = op.get("action")
            if not (isinstance(action, str) and action):
                continue
            content = op.get("content")
            old_text = op.get("old_text")
            content = content if isinstance(content, str) and content else None
            old_text = old_text if isinstance(old_text, str) and old_text else None
            # keyed by position in this same list, which is why the resolver
            # walks `memory_ops` rather than the raw payload
            found = self.memory_entries.get(len(out)) or {}
            entry = found.get("entry")
            out.append({"action": action, "content": content,
                        "old_text": old_text,
                        "old_entry": entry,
                        "old_shown": entry or old_text,
                        "old_entry_note": found.get("note"),
                        # a remove names only the entry it drops, so that is
                        # the one line worth showing for it
                        "text": content or old_text})
        return out

    # The char budget is the whole story of this tool — a memory span
    # succeeds or fails on it, and the end payload reports it either way
    # (as "97% — 1,335/1,375 chars" on success, a bare "1,338/1,375" on
    # the rejection, so it is shown verbatim rather than reformatted).
    @property
    def memory_stats(self) -> list:
        if self.name != "memory":
            return []
        end = _as_dict(self.end_data)
        if end is None:
            return []
        stats = []
        usage = end.get("usage")
        if isinstance(usage, str) and usage:
            stats.append({"label": "usage", "value": usage,
                          "tooltip": "Characters used of this store's limit "
                          "after the write. Entries must be shortened or "
                          "removed to make room once it is full."})
        count = end.get("entry_count")
        if isinstance(count, int):
            stats.append({"label": "entries", "value": str(count),
                          "tooltip": "Entries in the store after the write."})
        return stats

    # A rejected write comes back with the whole store attached — the error
    # text points at it ("see current_entries below") because the model has
    # to consolidate the existing entries before its own will fit. Detail
    # mode only: it is the store's state, not this write.
    @property
    def memory_current_entries(self) -> list:
        if self.name != "memory":
            return []
        end = _as_dict(self.end_data)
        if end is None:
            return []
        entries = end.get("current_entries")
        if not isinstance(entries, list):
            return []
        return [e for e in entries if isinstance(e, str) and e]

    # execute_code scopes carry the program in their start payload; the
    # first line stands in for it inline, the full text goes in the title
    @property
    def code(self) -> Optional[str]:
        return self._start_str("code") if self.name == "execute_code" else None

    @property
    def code_first_line(self) -> Optional[str]:
        code = self.code
        return code.split("\n", 1)[0] if code else None

    # web_extract scopes carry a list of target urls in their start payload
    @property
    def web_extract_urls(self) -> list:
        if self.name != "web_extract":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        urls = data.get("urls")
        if not isinstance(urls, list):
            return []
        return [u for u in urls if isinstance(u, str) and u]

    # todo scopes carry the full task list in their start payload; a call
    # without "todos" is a read, and merge-mode items may omit content —
    # both render nothing rather than erroring
    @property
    def todo_contents(self) -> list:
        if self.name != "todo":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        todos = data.get("todos")
        if not isinstance(todos, list):
            return []
        return [
            t["content"] for t in todos
            if isinstance(t, dict)
            and isinstance(t.get("content"), str) and t["content"]
        ]

    # delegate_task scopes carry the subagent briefs in their start
    # payload — batch mode as a "tasks" list of {goal, context} dicts,
    # single mode as top-level goal/context keys
    @property
    def delegate_tasks(self) -> list:
        if self.name != "delegate_task":
            return []
        data = _as_dict(self.start_data)
        if data is None:
            return []
        tasks = data.get("tasks")
        if not isinstance(tasks, list):
            tasks = [data]
        out = []
        for t in tasks:
            if not isinstance(t, dict):
                continue
            goal = t.get("goal")
            if not (isinstance(goal, str) and goal):
                continue
            context = t.get("context")
            out.append({
                "goal": goal,
                "context": context if isinstance(context, str) and context else None,
            })
        return out

    @property
    def delegate_goals(self) -> list:
        return [t["goal"] for t in self.delegate_tasks]

    # skill scopes (skill_view/skill_manage) describe the skill touched in
    # their start payload — name, optional file within the skill, and for
    # skill_manage the action; the keys are too generic to trust elsewhere
    @property
    def _is_skill_scope(self) -> bool:
        return self.name in ("skill_view", "skill_manage")

    @property
    def skill_name(self) -> Optional[str]:
        return self._start_str("name") if self._is_skill_scope else None

    @property
    def skill_file_path(self) -> Optional[str]:
        return self._start_str("file_path") if self._is_skill_scope else None

    @property
    def skill_action(self) -> Optional[str]:
        return self._start_str("action") if self.name == "skill_manage" else None

    @property
    def skill_category(self) -> Optional[str]:
        # only skill_manage "create" payloads carry a category; absent on
        # patch/write_file, so this is None for those actions
        return self._start_str("category") if self.name == "skill_manage" else None

    # a skill_manage "patch" carries the replaced text as old_string /
    # new_string (checked against skill_manage's signature in
    # $HERMES_SOURCE/tools/skill_manager_tool.py) — not a V4A patch text like the file
    # tools' patch scope, whose "replace" mode names the same two keys.
    # old_string is required and non-empty in both.
    @property
    def skill_old_string(self) -> Optional[str]:
        return (self._start_str("old_string")
                if self.name == "skill_manage" else None)

    @property
    def skill_new_string(self) -> Optional[str]:
        return self._new_string() if self.name == "skill_manage" else None

    # replace-mode patch scopes carry the same pair; patch mode carries
    # neither, so these are None there and the patch text renders instead
    @property
    def patch_old_string(self) -> Optional[str]:
        return self._start_str("old_string") if self.name == "patch" else None

    @property
    def patch_new_string(self) -> Optional[str]:
        return self._new_string() if self.name == "patch" else None

    def _new_string(self) -> Optional[str]:
        """A patch's replacement text — possibly the empty string.

        Read straight from the payload rather than through `_start_str`,
        which folds "" into None: an empty new_string is a real patch that
        deletes the matched text (both tools document passing "" for that),
        and the turn page says so. None here means the key was absent.
        """
        data = _as_dict(self.start_data)
        value = data.get("new_string") if data is not None else None
        return value if isinstance(value, str) else None

    @property
    def skill_absorbed_into(self) -> Optional[str]:
        # only a skill_manage "delete" that merged the skill elsewhere names
        # the skill it was folded into; absent otherwise
        return (self._start_str("absorbed_into")
                if self.name == "skill_manage" else None)

    # vision_analyze start payloads carry the image looked at (a URL or local
    # path) and the question asked of it; keys are too generic to trust
    # outside the scope, so gate on the span name
    @property
    def vision_image_url(self) -> Optional[str]:
        return (self._start_str("image_url")
                if self.name == "vision_analyze" else None)

    @property
    def vision_question(self) -> Optional[str]:
        return (self._start_str("question")
                if self.name == "vision_analyze" else None)



# --- which entry a memory write matched -----------------------------------
#
# The `memory` tool addresses an entry by a fragment of it, not by an id
# ($HERMES_SOURCE/tools/memory_tool.py: `[e for e in entries if old_text in
# e]`), and logs only the fragment. So the − side of a replace is a few
# words where the store held a whole sentence, and nothing in the span says
# what the rest of it was.
#
# The turn often does, though. A write that fails the char budget comes back
# with `current_entries` — the entire store — and consolidating after such a
# rejection is the routine reason for a replace in the first place, so the
# listing and the write that uses it usually sit seconds apart in one turn.
# Matching the fragment against that listing recovers the entry.
#
# Two limits are deliberate, and both are why the note beside the recovered
# text names the listing rather than presenting the entry as logged
# (design principle 2 — derived data says where it came from):
#
# - **The listing is a snapshot, not a running model of the store.** A write
#   that *succeeded* leaves the store somewhere this app cannot see: the
#   success payload reports a char count and an entry count, never the
#   entries. So a landed write drops the snapshot for that store, and later
#   ops go unresolved until another rejection lists it again. Replaying our
#   own idea of the writes onto it would be a second store, kept by us,
#   diverging silently — the thing ADR 2 exists to prevent.
# - **Within one span it is replayed**, because there the log is complete: a
#   batch is applied atomically to one working list and every operation in it
#   is on the span, so the state each op matched against is a reading of the
#   payload rather than a guess about the world.
MEMORY_SCOPE = "memory"


def resolve_memory_entries(turn) -> None:
    """Recover the whole entry behind each memory write's `old_text`, in place.

    Reads across the spans of one turn, so it is a turn-level pass rather
    than a `Span` property, and it is called for the turn being *viewed* —
    it needs end payloads, which are in the log rather than the index
    (ADR 11), so running it over every turn would hydrate the whole log.

    Idempotent: each span's result is rebuilt from scratch, so a re-render of
    a cached assembly gets the same answer.
    """
    listings = {}                      # target -> (entries, as-of us)
    for span in sorted(turn.spans, key=lambda s: s.start_us):
        if span.name != MEMORY_SCOPE:
            continue
        span.memory_entries = {}
        target = span.memory_target
        listed = span.memory_current_entries
        if listed:
            # Every payload carrying `current_entries` is a rejection, and a
            # rejected write — batch included, it is all-or-nothing — changed
            # nothing. The listing therefore describes the store as this span
            # found it, and holds for this span's own ops as well as later ones.
            listings[target] = (listed, span.end_us or span.start_us)
        listing = listings.get(target)
        if listing is not None:
            entries, as_of_us = listing
            working = list(entries)
            # `memory_ops` is read once here, before the results go in, and
            # rebuilt with them when the page asks for it
            for i, op in enumerate(span.memory_ops):
                found = _match_memory_entry(op, working, as_of_us, span)
                if found is not None:
                    span.memory_entries[i] = found
                _replay_memory_op(op, working)
        if _memory_write_landed(span):
            listings.pop(target, None)


def _memory_write_landed(span) -> bool:
    """Whether this write reached the store — i.e. whether a listing taken
    before it still describes what is there.

    Unknown counts as landed. A span still in flight, or one whose end
    payload could not be read, must not leave later rows quoting a store
    that may have moved on.
    """
    end = _as_dict(span.end_data)
    if end is not None and isinstance(end.get("success"), bool):
        return end["success"]
    return not span.failed


def _match_memory_entry(op, entries, as_of_us, span) -> Optional[dict]:
    """The one listed entry this op's fragment matched, with its provenance.

    None when there is nothing to add: an add (no fragment), or a fragment
    that is already the whole entry. Otherwise the note says what happened,
    including when the listing could not answer — an unmatched or ambiguous
    fragment is stated rather than passed over, since the row would
    otherwise look like one we simply chose not to resolve.
    """
    fragment = (op.get("old_text") or "").strip()
    if not fragment:
        return None
    matched = {e for e in entries if fragment in e}
    listing = _listing_phrase(as_of_us, span)
    if len(matched) > 1:
        # what the tool itself rejects as ambiguous, unless the duplicates
        # are identical — either way, not something to pick between here
        return {"entry": None,
                "note": f"{len(matched)} entries in {listing} contain this "
                        "text — not resolved to one"}
    if not matched:
        return {"entry": None,
                "note": f"no entry in {listing} contains this text"}
    entry = next(iter(matched))
    if entry.strip() == fragment:
        return None                    # the fragment was the whole entry
    # The fragment goes in the note because the entry has taken its place on
    # the row: it is what the payload actually says, and without it the page
    # would show only text this app worked out.
    return {"entry": entry,
            "note": f"matched entry from {listing} · logged as “{fragment}”"}


def _listing_phrase(as_of_us: int, span) -> str:
    """Which store listing answered, and how long before this call it was
    taken — the provenance half of every note above.

    Milliseconds are worth printing: consecutive memory calls in one turn are
    routinely that close, and "0 s earlier" would read as a rounding error
    rather than as the tight loop it is.
    """
    gap_s = (span.start_us - as_of_us) / 1_000_000
    if gap_s <= 0:
        return "the store listing this call returned"
    if gap_s < 1:
        return f"the store listing {gap_s * 1000:.0f} ms earlier in this turn"
    if gap_s < 90:
        return f"the store listing {gap_s:.0f} s earlier in this turn"
    return f"the store listing {gap_s / 60:.0f} min earlier in this turn"


def _replay_memory_op(op, working: list) -> None:
    """Apply one op to the working list, as the tool applies a batch.

    Only ever within a single span (see the note above the module's
    MEMORY_SCOPE). Matching is the tool's own containment rule, so a later op
    in a batch sees what the earlier ones left it rather than the entry one
    of them already replaced.
    """
    action, content = op.get("action"), op.get("content")
    fragment = (op.get("old_text") or "").strip()
    if action == "add":
        if content:
            working.append(content)
        return
    if not fragment:
        return
    idx = next((i for i, e in enumerate(working) if fragment in e), None)
    if idx is None:
        return
    if action == "remove":
        working.pop(idx)
    elif action == "replace" and content:
        working[idx] = content



def _unwrap_prompt(content: str) -> Optional[str]:
    """A wire message with hermes' own envelope taken back off.

    Only the two wrappers hermes is known to add are removed — the
    `[Workspace::v1: …]` header and the recalled-memory block appended after
    the prompt. A message carrying neither comes back whole: cutting at a
    guess would be this app inventing a prompt boundary.
    """
    text = WORKSPACE_PREFIX.sub("", content)
    text = MEMORY_CONTEXT.sub("", text)
    text = text.strip()
    return text or None


def _fence(text: str, lang: str = "") -> str:
    """`text` in a code fence long enough to survive whatever is inside it.

    Tool arguments are JSON and tool results are whatever a tool printed —
    including, routinely, markdown with its own fences. CommonMark closes a
    fence only on a run of backticks at least as long as the opening one, so
    counting the longest run inside and going one better is what keeps a
    result from ending the block that holds it.
    """
    longest = 0
    run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    ticks = "`" * max(3, longest + 1)
    return f"{ticks}{lang}\n{text}\n{ticks}"


def _message_body(message: dict) -> str:
    """One request message as text, whatever kind of message it is.

    The relay's annotator — not hermes, so there is no tool signature to
    check this against (design principle 3) — writes five kinds of message
    into one list, and only two of them use `content`:

        user / assistant   content: str
        tool_call          name, arguments, call_id  (content: None)
        tool_result        output, call_id           (content: None)
        provider_native    kind, provider, value     (content: None)

    So this reads the shape in front of it rather than one key: a string
    body where there is one, a fenced dump where the body is structured, and
    a fenced dump of the whole message where it is something this has never
    seen. Nothing is dropped for being unrecognized — the point of the page
    it feeds is that it holds everything.
    """
    for key in ("content", "output", "text", "value"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    if isinstance(message.get("arguments"), str):
        return _fence(message["arguments"], "json")
    rest = {k: v for k, v in message.items() if k != "role"}
    if not rest:
        return ""
    if len(rest) == 1:
        only = next(iter(rest.values()))
        if isinstance(only, str):
            return only
    return _fence(json.dumps(rest, indent=2, default=str), "json")


def _message_label(message: dict) -> str:
    """`tool_call · read_file` — the role, and what names the message within
    it. A call and its result are otherwise two identical labels with the
    interesting part inside the fence.

    A label, not a heading: it is rendered as page furniture beside the
    message rather than written into the message's own markdown. Written in,
    it was one more `##` among the dozen a system prompt already has, and a
    reader had no way to tell this app's words from the model's.
    """
    role = message.get("role")
    role = role if isinstance(role, str) and role else "(no role)"
    for key in ("name", "kind"):
        value = message.get(key)
        if isinstance(value, str) and value:
            return f"{role} · {value}"
    return role


def _tool_call_ids(messages: list) -> set:
    """The `call_id` of every `tool_call` in a request.

    Pairing only. The labels say nothing about which call a result answers —
    `tool_result` is the whole label — because the layout says it: the
    result is drawn inside its call's card. A label repeating what the box
    around it already shows is one more thing to read and to keep true.
    """
    return {m.get("call_id") for m in messages
            if isinstance(m, dict) and m.get("role") == "tool_call"
            and isinstance(m.get("call_id"), str) and m.get("call_id")}


def _message_sections(messages: list) -> list:
    """A request's messages as sections, with each result under its call.

    **This is not the wire order, and the page says so** (the `prompt`
    Full's `note`, design principle 2). The wire sends every call of a turn
    and then every result — 957 of 957 such requests in the log are blocked
    that way, never interleaved — so five results arrive as five boxes with
    nothing tying them to the five calls above them. `call_id` pairs them
    exactly: 26,944 results in the log, none of them orphaned.

    A result whose `call_id` matches no call in this request stays exactly
    where it arrived. It has never happened here, but inventing a position
    for one would be the reordering doing harm rather than good, and a
    reader who sees an ungrouped result should be able to trust that it
    really was unpaired.
    """
    calls = _tool_call_ids(messages)
    results = {}
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool_result":
            continue
        call_id = message.get("call_id")
        if call_id in calls:
            results.setdefault(call_id, []).append(message)

    def section(message, **flags):
        if not isinstance(message, dict):
            out = {"label": "(unreadable)",
                   "text": _fence(json.dumps(message, indent=2, default=str),
                                  "json")}
        else:
            out = {"label": _message_label(message),
                   "text": _message_body(message)}
        out.update(flags)
        return out

    sections = []
    for message in messages:
        if (isinstance(message, dict)
                and message.get("role") == "tool_result"
                and message.get("call_id") in calls):
            continue            # emitted under its call, below
        owned = (results.get(message.get("call_id"), ())
                 if isinstance(message, dict)
                 and message.get("role") == "tool_call" else ())
        # `nests` and `nested` are two halves of one fact, and both are
        # needed: the pair is drawn as a single card, so the call has to
        # know to run into what follows it and not only the result to know
        # it is inside something.
        sections.append(section(message, **({"nests": True} if owned else {})))
        for result in owned:
            sections.append(section(result, nested=True))
    return sections


def request_sections_from_profile(category_profile: Any) -> Optional[list]:
    """A whole llm request as `[{label, text}]` — one entry per message.

    The *split* is this app's (design principle 2, derived data says so on
    screen); every `text` is the wire content untouched. Keeping them apart
    is the whole point of the shape: the page draws each label as its own
    chrome, so nothing this app wrote can be mistaken for something the model
    was sent.

    The *order* is this app's too, in one respect: a tool result is moved to
    sit under the call it answers — see `_message_sections`.

    `instructions` leads when the request carries one — it is the system
    prompt on the openai_responses path, and sits beside `messages` rather
    than inside it.
    """
    if not isinstance(category_profile, dict):
        return None
    request = category_profile.get("annotated_request")
    if not isinstance(request, dict):
        return None
    sections = []
    instructions = request.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        sections.append({"label": "instructions", "text": instructions})
    messages = request.get("messages")
    if isinstance(messages, list):
        sections.extend(_message_sections(messages))
    return sections or None


def request_prompt_from_profile(category_profile: Any) -> Optional[str]:
    """Reconstruct a turn's prompt from an llm call's request payload.

    See `Span.request_prompt`, whose docstring is the record of what this
    reconstruction is and is not. Module-level because the index runs it at
    index time, on a payload it then leaves in the log (ADR 11) — the value
    has to be derived the same way from either side.
    """
    if not isinstance(category_profile, dict):
        return None
    request = category_profile.get("annotated_request")
    if not isinstance(request, dict):
        return None
    messages = request.get("messages")
    if not isinstance(messages, list):
        return None
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str) and content:
            return _unwrap_prompt(content)
    return None


def user_message_from_data(data: Any) -> Optional[str]:
    """The prompt from a turn-start mark's payload.

    Public because the index projects this value out of a payload it is
    about to leave in the log, and must reach it the same way (ADR 11).
    """
    data = _as_dict(data)
    if data is not None:
        value = data.get("user_message")
        if isinstance(value, str) and value:
            return value
    return None


def _mark_user_message(mark: AtofEvent) -> Optional[str]:
    """The prompt a turn-start mark named, from wherever it survives.

    A turn mark's payload averages 570 KB — it repeats the conversation —
    and this short field is all assembly wants from it, so the index keeps
    the field and leaves the payload in the log (ADR 11).
    """
    return mark.projected("user_message") or user_message_from_data(mark.data)
