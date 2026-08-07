"""ATOF assembler — the top layer of the reader (docs/design/adr/0002).

Takes parsed AtofEvents and builds the per-turn waterfall model:

- scope start/end events sharing a uuid become one Span;
- ``hermes.turn.start`` / ``hermes.turn.end`` marks bound each Turn;
- llm/tool spans are assigned to turns by ``turn_id`` when both sides carry
  one, else by timestamp containment (the hermes nemo_relay plugin always
  stamps turn_id on spans, but turn marks only carry it when the hook
  kwargs do);
- a span's session comes from its metadata, falling back to its
  ``parent_uuid`` — children of a session scope point at it, so grouping
  survives missing metadata;
- hermes' core runtime describes the same turns a second way, as a scope
  tree (see TURN_SCOPE below). Spans reach their turn by walking that tree,
  which is the only thing that places an llm span — those carry no turn_id.
  A turn scope is merged into the Turn its marks already built rather than
  becoming a second one;
- overhead is the residual: turn duration minus provider (llm) time minus
  tool time. It is reported as-is, even negative — a negative residual
  means spans overlap and should be seen, not clamped away.

Anything that does not assemble cleanly (end without start, turn.end
without turn.start, spans outside any turn) is kept and surfaced, never
dropped silently — the ADR 2 loud-failure rule.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, List, Optional

from plugins.turns.atof_reader import (STREAM_MARK_NAMES, AtofEvent, LineRef,
                                         generic_payload_fields)
from plugins.turns.providers import chunk_usage

TURN_START_MARK = "hermes.turn.start"
TURN_END_MARK = "hermes.turn.end"
SUBAGENT_START_MARK = "hermes.subagent.start"
SUBAGENT_STOP_MARK = "hermes.subagent.stop"
AGENT_CATEGORY = "agent"
LLM_CATEGORY = "llm"
TOOL_CATEGORY = "tool"
UNKNOWN_SESSION = "(unknown session)"

# --- the core runtime's turn tree ----------------------------------------
# The nemo_relay plugin bounded a turn with a pair of marks. The core
# runtime that replaced it (hermes 2026-07-19) emits no such marks; it emits
# a scope tree instead, and the turn is a scope in it:
#
#   agent scope                  the execution surface; empty payload
#     └── hermes.turn            start/end pair, {"outcome": …} on the end
#           ├── hermes.logical_llm_call     one logical call…
#           │     └── llm scope             …and its physical attempt
#           └── tool scopes
#
# So a turn's extent is observed again rather than inferred, and with it the
# overhead residual. Two consequences the assembler has to honour:
#
# - `hermes.logical_llm_call` has category "function", so it counts as
#   neither llm nor tool time while wrapping something that does. Left in,
#   every turn double-counts its model time. It is a container, not work.
# - The turn scope carries no turn_id and no user_message, so a turn takes
#   its session from the spans beneath it and its prompt from the request
#   that opened its first llm call.
TURN_SCOPE = "hermes.turn"
LOGICAL_LLM_SCOPE = "hermes.logical_llm_call"

# The observed tree is turn > logical_llm_call > llm — two hops. The bound
# is not about depth but about malformed data: a parent cycle must not hang
# a request.
MAX_PARENT_WALK = 8

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


@dataclass
class Anomaly:
    message: str
    line_no: Optional[int] = None


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
    # carries an "error" string. That is how the tools in $h/tools/ report
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
    # than it looks (checked against the emit sites in $h/agent/, not just
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
    # (`$h/agent/auxiliary_client.py`), `call_role` being the f-string
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
    # in $h/tools/file_tools.py): "replace" (the default — path plus
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

    # What a mem0_search actually retrieved, from its end payload —
    # {"count": n, "results": [{"id", "memory", "score"}, …]}, ranked by
    # score descending, so the first entries are the top hits. That shape
    # has been uniform in practice, but is still read defensively: payloads
    # are opaque per the ATOF spec. Rendering output at all is the
    # exception here — see Span.memory_stats for the other one — and it
    # earns it because the query alone never says whether the search was
    # any good.
    @property
    def mem0_results(self) -> List[dict]:
        if self.name != "mem0_search":
            return []
        end = _as_dict(self.end_data)
        if end is None:
            return []
        raw = end.get("results")
        if not isinstance(raw, list):
            return []
        results = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            memory = item.get("memory")
            score = item.get("score")
            results.append({
                "id": item.get("id") if isinstance(item.get("id"), str) else None,
                "memory": memory if isinstance(memory, str) else None,
                # ints are valid JSON numbers; bool is an int subclass
                "score": score if isinstance(score, (int, float))
                         and not isinstance(score, bool) else None,
            })
        return results

    # How many memories came back. The payload's own count is authoritative
    # (it is what mem0 reported); fall back to the list length when absent.
    @property
    def mem0_result_count(self) -> Optional[int]:
        if self.name != "mem0_search":
            return None
        end = _as_dict(self.end_data)
        if end is not None:
            count = end.get("count")
            if isinstance(count, int) and not isinstance(count, bool):
                return count
        results = self.mem0_results
        return len(results) if results else None

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

    # mem0 scopes carry the remembered fact under different keys —
    # mem0_add "content", mem0_update "text" (checked against the tool
    # schemas in $h/plugins/memory/mem0/__init__.py: these four tools live
    # in the memory plugin, not $h/tools/). Both keys are far too generic
    # to trust on other scopes.
    @property
    def memory_content(self) -> Optional[str]:
        if self.name == "mem0_add":
            return self._start_str("content")
        if self.name == "mem0_update":
            return self._start_str("text")
        return None

    # mem0_update and mem0_delete name the memory they act on; on a delete
    # it is the whole payload
    @property
    def memory_id(self) -> Optional[str]:
        return (self._start_str("memory_id")
                if self.name in ("mem0_update", "mem0_delete") else None)

    # The `memory` scope is a different tool from the mem0 ones above
    # (checked against $h/tools/memory_tool.py): bounded, file-backed,
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
        `text` is whichever of the two the summary line should carry."""
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
            out.append({"action": action, "content": content,
                        "old_text": old_text,
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
    # $h/tools/skill_manager_tool.py) — not a V4A patch text like the file
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


def request_sections_from_profile(category_profile: Any) -> Optional[list]:
    """A whole llm request as `[{label, text}]` — one entry per message, in
    the order sent.

    The *split* is this app's (design principle 2, derived data says so on
    screen); every `text` is the wire content untouched. Keeping them apart
    is the whole point of the shape: the page draws each label as its own
    chrome, so nothing this app wrote can be mistaken for something the model
    was sent.

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
        for message in messages:
            if not isinstance(message, dict):
                sections.append({
                    "label": "(unreadable)",
                    "text": _fence(json.dumps(message, indent=2, default=str),
                                   "json")})
                continue
            sections.append({"label": _message_label(message),
                             "text": _message_body(message)})
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


@dataclass
class Turn:
    session_id: str
    turn_id: Optional[str]
    start_us: int
    end_us: Optional[int] = None    # None if the end mark never arrived
    # A later turn started in this session while this one was still open: a
    # session runs one turn at a time, so this turn is over even though its
    # end mark never arrived. No end_us is invented for it — the duration
    # was never observed and is not ours to guess (ADR 2).
    superseded: bool = False
    user_message: Optional[str] = None
    spans: List[Span] = field(default_factory=list)
    marks: List[AtofEvent] = field(default_factory=list)

    @property
    def duration_us(self) -> Optional[int]:
        if self.end_us is None:
            return None
        return self.end_us - self.start_us

    @property
    def is_live(self) -> bool:
        """Still running: open, and not proven finished by a later turn.

        An open turn is not evidence of work in progress — hermes drops
        turn.end marks often enough that unclosed turns pile up.
        """
        return self.end_us is None and not self.superseded

    def _category_us(self, category: str) -> int:
        return sum(
            s.duration_us for s in self.spans
            if s.category == category and s.duration_us is not None
        )

    @property
    def llm_us(self) -> int:
        return self._category_us(LLM_CATEGORY)

    @property
    def tool_us(self) -> int:
        return self._category_us(TOOL_CATEGORY)

    @property
    def overhead_us(self) -> Optional[int]:
        if self.duration_us is None:
            return None
        return self.duration_us - self.llm_us - self.tool_us

    @property
    def model_call_count(self) -> int:
        return sum(1 for s in self.spans if s.category == LLM_CATEGORY)

    @property
    def timeline(self) -> List[Any]:
        """Spans and non-boundary marks merged in time order — the turn
        page's waterfall rows; marks render as zero-width ticks."""
        return sorted(
            [*self.spans, *self.marks],
            key=lambda e: e.timestamp_us if e.is_mark else e.start_us,
        )

    # Each subagent gets a small per-turn tag (#1, #2, … in start-mark
    # order) shown on both its start and stop rows so the pair can be
    # matched by eye; stops correlate back via child_session_id, the only
    # key present on both marks.
    @property
    def subagents(self) -> dict:
        out = {}
        for m in sorted(self.marks, key=lambda m: m.timestamp_us):
            sid = m.child_session_id
            if m.name == SUBAGENT_START_MARK and sid and sid not in out:
                out[sid] = {"ordinal": len(out) + 1, "goal": m.child_goal}
        return out

    def subagent_ordinal(self, mark) -> Optional[int]:
        entry = self.subagents.get(mark.child_session_id)
        return entry["ordinal"] if entry else None

    def subagent_goal(self, mark) -> Optional[str]:
        entry = self.subagents.get(mark.child_session_id)
        return entry["goal"] if entry else None

    @property
    def last_activity_us(self) -> int:
        """Timestamp of the last event seen in this turn — distinguishes a
        genuinely running turn from one whose end mark never arrived."""
        edges = [self.start_us]
        if self.end_us is not None:
            edges.append(self.end_us)
        # Span.last_activity_us folds in the last streamed chunk, which is
        # the only sign of life a long model call gives while it is open.
        edges.extend(s.last_activity_us for s in self.spans)
        edges.extend(m.timestamp_us for m in self.marks)
        return max(edges)


@dataclass
class Session:
    session_id: str
    turns: List[Turn] = field(default_factory=list)
    unassigned_spans: List[Span] = field(default_factory=list)
    unassigned_marks: List[AtofEvent] = field(default_factory=list)
    first_us: Optional[int] = None
    last_us: Optional[int] = None

    def _saw(self, timestamp_us: int) -> None:
        if self.first_us is None or timestamp_us < self.first_us:
            self.first_us = timestamp_us
        if self.last_us is None or timestamp_us > self.last_us:
            self.last_us = timestamp_us


@dataclass
class Assembly:
    sessions: List[Session]        # most recent activity first
    anomalies: List[Anomaly]

    @property
    def finished_subagent_sessions(self) -> set:
        """Sessions a parent has reported as stopped, via subagent.stop.

        A subagent's own session often never emits hermes.turn.end, leaving
        its turn open forever; the parent's stop mark is the authoritative
        "this agent is done" signal, and needs no staleness clock.
        """
        stopped = set()
        for session in self.sessions:
            marks = [m for turn in session.turns for m in turn.marks]
            marks.extend(session.unassigned_marks)
            for mark in marks:
                if mark.name == SUBAGENT_STOP_MARK and mark.child_session_id:
                    stopped.add(mark.child_session_id)
        return stopped


def _pair_spans(events, anomalies, stream_activity=None, stream_usage=None):
    """Match scope start/end events by uuid; also map session-scope uuids.

    Returns (spans by uuid in start order, {agent scope uuid: session_id}).

    `stream_activity` and `stream_usage` map a span's uuid to the timestamp
    of the last `llm.chunk` seen beneath it and to the token counts that
    chunk reported — see `assemble`.
    """
    stream_activity = stream_activity or {}
    stream_usage = stream_usage or {}
    spans: dict = {}
    scope_sessions: dict = {}
    for event in events:
        if event.kind != "scope":
            continue
        if event.is_scope_start:
            if event.uuid in spans:
                anomalies.append(Anomaly(
                    f"duplicate scope start for uuid {event.uuid!r}", event.line_no))
                continue
            spans[event.uuid] = Span(
                uuid=event.uuid,
                name=event.name,
                category=event.category,
                session_id="",       # resolved later
                parent_uuid=event.parent_uuid,
                start_us=event.timestamp_us,
                end_us=None,
                metadata=event.metadata,
                category_profile=event.category_profile,
                start_data=event.data,
                end_data=None,
                model_name=event.model_name,
                tool_call_id=event.tool_call_id,
                api_request_id=event.api_request_id,
                turn_id=event.turn_id,
                line_no=event.line_no,
                start_ref=event.payload_ref,
                projection=dict(event.projection),
                payload_elided=event.payload_elided,
                stream_last_us=stream_activity.get(event.uuid),
                stream_usage=stream_usage.get(event.uuid),
            )
            if event.category == AGENT_CATEGORY:
                session_id = event.session_id or event.projected("data_session_id")
                if not session_id and isinstance(event.data, dict):
                    session_id = event.data.get("session_id")
                if session_id:
                    scope_sessions[event.uuid] = session_id
        else:
            span = spans.get(event.uuid)
            if span is None:
                anomalies.append(Anomaly(
                    f"scope end without start for uuid {event.uuid!r}", event.line_no))
                continue
            if span.end_us is not None:
                anomalies.append(Anomaly(
                    f"duplicate scope end for uuid {event.uuid!r}", event.line_no))
                continue
            span.end_us = event.timestamp_us
            span.end_data = event.data
            span.end_ref = event.payload_ref
            span.payload_elided = span.payload_elided or event.payload_elided
            if event.projection:
                span.projection = {**span.projection, **event.projection}
            # end events may carry metadata the start lacked
            span.metadata = {**event.metadata, **span.metadata}
    return spans, scope_sessions


def _build_turns(session: Session, boundary_marks, anomalies) -> None:
    current: Optional[Turn] = None
    for mark in boundary_marks:
        if mark.name == TURN_START_MARK:
            if current is not None:
                anomalies.append(Anomaly(
                    f"turn started before previous turn ended in session "
                    f"{session.session_id!r}", mark.line_no))
                current.superseded = True
                session.turns.append(current)
            current = Turn(
                session_id=session.session_id,
                turn_id=mark.turn_id,
                start_us=mark.timestamp_us,
                user_message=_mark_user_message(mark),
            )
        else:  # TURN_END_MARK
            if current is None:
                anomalies.append(Anomaly(
                    f"turn end without turn start in session "
                    f"{session.session_id!r}", mark.line_no))
                continue
            current.end_us = mark.timestamp_us
            if current.turn_id is None:
                current.turn_id = mark.turn_id
            session.turns.append(current)
            current = None
    if current is not None:   # still in flight (or the end mark was lost)
        session.turns.append(current)


def _enclosing_turn_uuid(span, spans: dict, turn_uuids) -> Optional[str]:
    """The uuid of the turn scope a span sits under, walking parent_uuid.

    Returns None for a span outside any turn scope — including the turn
    scopes and session scopes themselves, which have no turn above them.
    """
    seen = set()
    current = span.parent_uuid
    for _ in range(MAX_PARENT_WALK):
        if current is None or current in seen:
            return None
        if current in turn_uuids:
            return current
        seen.add(current)
        parent = spans.get(current)
        if parent is None:
            return None
        current = parent.parent_uuid
    return None


def _overlaps(turn: Turn, scope) -> bool:
    """Whether a turn and a turn scope describe the same stretch of time.

    An unfinished side is treated as still running, which is what it claims
    to be. Turns within a session do not overlap each other, so this picks
    out at most one.
    """
    turn_end = turn.end_us if turn.end_us is not None else float("inf")
    scope_end = scope.end_us if scope.end_us is not None else float("inf")
    return turn.start_us < scope_end and scope.start_us < turn_end


def _matching_turn(session: Session, scope, turn_ids, taken) -> Optional[Turn]:
    """The already-built turn a turn scope is a second account of, if any.

    The two exporters can both be live — the plugin still emits its marks
    while the core runtime emits this scope tree — and then each real turn
    is described twice, a few milliseconds apart. Building both produces a
    duplicate row whose spans all went to the other one.

    The exact key is `turn_id`: every span under a turn scope that carries
    one carries the *same* one, and it is the id the marks use. Overlap is
    the fallback for a turn whose spans carry no turn_id at all — one with
    only llm calls under it, which have none.
    """
    # `taken` holds ids, not Turns: Turn is a plain dataclass, so `in` would
    # deep-compare every span payload it carries — megabytes per turn.
    for turn in session.turns:
        if id(turn) in taken:
            continue
        if turn.turn_id is not None and turn.turn_id in turn_ids:
            return turn
    for turn in session.turns:
        if id(turn) not in taken and _overlaps(turn, scope):
            return turn
    return None


def _build_scope_turns(spans: dict, turn_uuids, session_for, anomalies) -> dict:
    """Map each `hermes.turn` scope to the Turn it describes.

    Where the marks already built that turn, this returns the existing one —
    the mark is the better account of *what* the turn was (it carries the
    session, the turn_id and hermes' own unwrapped `user_message`), while
    the scope is the better account of *what ran inside it*, which is the
    tree spans are attached by. Only a scope with no mark behind it becomes
    a Turn of its own, prompt reconstructed from its first llm request.
    """
    children = {uuid: [] for uuid in turn_uuids}
    for span in sorted(spans.values(), key=lambda s: (s.start_us, s.line_no)):
        owner = _enclosing_turn_uuid(span, spans, turn_uuids)
        if owner is not None:
            children[owner].append(span)

    # A turn's session, from the spans under it. A turn that ran no span
    # yet — one just started — names nobody, so the agent scope above is
    # asked next: an agent scope is one session by construction, and its
    # other turns have already said which. Without that, a turn opened at
    # the moment of reading would strand itself in (unknown session) and
    # then fail to recognize the mark that describes it.
    own_session = {}
    for uuid in turn_uuids:
        own_session[uuid] = next(
            (s.metadata.get("session_id") for s in children[uuid]
             if s.metadata.get("session_id")),
            None,
        )
    agent_session = {}
    for uuid, session_id in own_session.items():
        parent = spans[uuid].parent_uuid
        if session_id and parent and parent not in agent_session:
            agent_session[parent] = session_id

    turns, taken = {}, set()
    for uuid in sorted(turn_uuids, key=lambda u: spans[u].start_us):
        scope = spans[uuid]
        own = children[uuid]
        session_id = (own_session[uuid]
                      or agent_session.get(scope.parent_uuid))
        if session_id is None:
            session_id = UNKNOWN_SESSION
            anomalies.append(Anomaly(
                f"turn scope {uuid} has no span naming its session",
                scope.line_no))
        session = session_for(session_id)
        turn_ids = {s.turn_id for s in own if s.turn_id}

        existing = _matching_turn(session, scope, turn_ids, taken)
        if existing is not None:
            # One turn, two accounts of it. Take the union of the two
            # intervals: they bracket the same work a few ms apart, and the
            # union is the span that certainly contains it — which matters
            # because the waterfall lays its spans out from the turn's own
            # start.
            existing.start_us = min(existing.start_us, scope.start_us)
            if existing.end_us is not None and scope.end_us is not None:
                existing.end_us = max(existing.end_us, scope.end_us)
            elif scope.end_us is None:
                existing.end_us = None
            turns[uuid] = existing
            taken.add(id(existing))
            continue

        prompt = next((s.request_prompt for s in own
                       if s.category == LLM_CATEGORY and s.request_prompt), None)
        turn = Turn(
            session_id=session_id,
            turn_id=next(iter(turn_ids)) if len(turn_ids) == 1 else None,
            start_us=scope.start_us,
            end_us=scope.end_us,
            user_message=prompt,
        )
        session.turns.append(turn)
        turns[uuid] = turn
        taken.add(id(turn))
    return turns


def _containing_turn(session: Session, timestamp_us: int) -> Optional[Turn]:
    """The turn whose interval holds the timestamp.

    An unended turn's interval runs until the next turn's start (or forever
    if it is the last one).
    """
    for i, turn in enumerate(session.turns):
        if timestamp_us < turn.start_us:
            return None
        end = turn.end_us
        if end is None:
            end = (
                session.turns[i + 1].start_us
                if i + 1 < len(session.turns)
                else None
            )
        if end is None or timestamp_us <= end:
            return turn
    return None


def _turn_for(session: Session, turn_id: Optional[str], timestamp_us: int) -> Optional[Turn]:
    if turn_id is not None:
        for turn in session.turns:
            if turn.turn_id == turn_id:
                return turn
    return _containing_turn(session, timestamp_us)


def assemble(events: Iterable[AtofEvent],
             stream_activity: Optional[dict] = None,
             stream_usage: Optional[dict] = None) -> Assembly:
    """Build the session/turn/span waterfall model from parsed events.

    `stream_activity` and `stream_usage` map a span uuid to the timestamp of
    the last `llm.chunk` emitted beneath it and to the token counts that
    chunk carried. The index passes both because it does not carry chunks as
    events (ADR 11); a caller assembling parsed lines directly passes
    neither, and the chunks are simply in `events` — where the sweep below
    reads the same counts back out, so both paths see one model.
    """
    anomalies: List[Anomaly] = []
    # Defensive sort: the exporter appends in near-real-time order, but the
    # model must not depend on it. line_no breaks timestamp ties.
    ordered = sorted(events, key=lambda e: (e.timestamp_us, e.line_no))

    stream_usage = dict(stream_usage or {})
    for event in ordered:
        if event.is_mark and event.name in STREAM_MARK_NAMES and event.parent_uuid:
            counts = chunk_usage(event.data.get("usage")
                                 if isinstance(event.data, dict) else None)
            if counts:      # only the stream's last chunk carries any
                stream_usage[event.parent_uuid] = counts

    spans, scope_sessions = _pair_spans(ordered, anomalies, stream_activity,
                                        stream_usage)

    # The core runtime's turn tree (see TURN_SCOPE). Both eras can appear in
    # one file, so these are simply empty when only the plugin wrote it.
    turn_uuids = {u for u, s in spans.items() if s.name == TURN_SCOPE}
    # Containers, not work: a wrapper's duration is its child's, counted
    # twice if it is left in, and its category is neither llm nor tool so it
    # would land wholly in overhead.
    container_uuids = turn_uuids | {
        u for u, s in spans.items() if s.name == LOGICAL_LLM_SCOPE}

    sessions: dict = {}

    def session_for(session_id: str) -> Session:
        if session_id not in sessions:
            sessions[session_id] = Session(session_id=session_id)
        return sessions[session_id]

    # Turns first: boundary marks define the intervals spans land in.
    for event in ordered:
        if event.is_mark:
            session_id = event.session_id or scope_sessions.get(event.parent_uuid) \
                or UNKNOWN_SESSION
            session_for(session_id)._saw(event.timestamp_us)
    for session in list(sessions.values()):
        boundary = [
            e for e in ordered
            if e.is_mark and e.name in (TURN_START_MARK, TURN_END_MARK)
            and (e.session_id or scope_sessions.get(e.parent_uuid) or UNKNOWN_SESSION)
            == session.session_id
        ]
        _build_turns(session, boundary, anomalies)

    # Then the turn scopes, which mostly recognize a turn the marks already
    # built rather than adding one. Runs after `_build_turns` for exactly
    # that reason — there has to be something to recognize.
    scope_turns = _build_scope_turns(spans, turn_uuids, session_for, anomalies)

    for session in sessions.values():
        session.turns.sort(key=lambda t: t.start_us)
        # An open turn with a later turn behind it in the same session is
        # over, whatever its missing end mark says — one turn at a time.
        for earlier, later in zip(session.turns, session.turns[1:]):
            if earlier.end_us is None and later.start_us >= earlier.start_us:
                earlier.superseded = True

    # Assign spans (session scopes are containers, not work — skip them,
    # but let them establish their session's activity window).
    for span in spans.values():
        if span.category == AGENT_CATEGORY and span.uuid in scope_sessions:
            session = session_for(scope_sessions[span.uuid])
            session._saw(span.start_us)
            if span.end_us is not None:
                session._saw(span.end_us)
    for span in sorted(spans.values(), key=lambda s: (s.start_us, s.line_no)):
        if span.category == AGENT_CATEGORY or span.uuid in container_uuids:
            continue
        owner = scope_turns.get(_enclosing_turn_uuid(span, spans, turn_uuids))
        session_id = (
            span.metadata.get("session_id")
            or scope_sessions.get(span.parent_uuid)
            or (owner.session_id if owner is not None else None)
            or UNKNOWN_SESSION
        )
        session = session_for(session_id)
        span.session_id = session_id
        session._saw(span.start_us)
        if span.end_us is not None:
            session._saw(span.end_us)
        # The scope tree is a statement of ownership by the exporter, so it
        # beats matching on turn_id or falling back to time containment.
        turn = owner if owner is not None else _turn_for(
            session, span.turn_id, span.start_us)
        if turn is not None:
            turn.spans.append(span)
        else:
            session.unassigned_spans.append(span)
            anomalies.append(Anomaly(
                f"span {span.name!r} ({span.uuid}) falls outside every turn "
                f"in session {session_id!r}", span.line_no))

    # Remaining marks (approvals, subagent events, …) attach by time.
    for event in ordered:
        if not event.is_mark or event.name in (TURN_START_MARK, TURN_END_MARK):
            continue
        session_id = event.session_id or scope_sessions.get(event.parent_uuid) \
            or UNKNOWN_SESSION
        session = session_for(session_id)
        turn = _turn_for(session, event.turn_id, event.timestamp_us)
        if turn is not None:
            turn.marks.append(event)
        else:
            session.unassigned_marks.append(event)

    return Assembly(
        sessions=sorted(
            sessions.values(),
            key=lambda s: s.last_us if s.last_us is not None else 0,
            reverse=True,
        ),
        anomalies=anomalies,
    )
