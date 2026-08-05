# 1. Consume the NeMo Relay ATOF stream for Hermes prompt-timing analysis

Date: 2026-07-13

## Status

Accepted

## Context

We want to break down where wall-clock time goes between submitting a prompt
to Hermes and receiving the answer: time spent waiting on the model provider,
time spent in tool calls, and everything in between. The breakdown should
eventually span the Hermes WebUI (hermes-webui), which runs the agent
in-process.

hermes-agent already measures the relevant intervals and exposes them through
its plugin observer hooks (`pre/post_api_request` with `api_duration` and
token usage, `pre/post_tool_call` with `duration_ms`, `pre/post_llm_call` as
turn boundaries, plus correlation keys `session_id` / `turn_id` /
`api_request_id` / `tool_call_id`). Three consumption routes were considered:

1. **A custom hook plugin** (the pattern used by the bundled Langfuse plugin
   and by our jmem0 logging wrapper): full access to every hook kwarg, we own
   the schema, no third-party dependency — but we also own the defensive
   serialization, the never-block-the-turn discipline, and a bespoke format
   no other tooling can read.
2. **The bundled Langfuse plugin**: turnkey traces with durations, usage and
   cost, but requires running a Langfuse server and the data lives in
   Langfuse's store rather than something this tool can browse directly.
3. **The bundled NeMo Relay plugin's ATOF JSONL export**
   (`plugins/observability/nemo_relay` in hermes-agent): zero plugin code to
   write, local file output, a documented public format (ATOF event streams,
   ATIF trajectories), with span pairing and subagent embedding solved
   upstream.

Inspection of the nemo_relay plugin showed the ATOF stream carries more than
initially assumed: turn marks (`hermes.turn.start` / `hermes.turn.end`) dump
the full hook kwargs; tool spans close with `duration_ms` and status; LLM/API
spans close with `usage` and `finish_reason` (provider latency is derivable
from span open/close timestamps); and every event carries a `metadata` block
with the correlation keys. Custom-named events are first-class in the format
— the plugin itself emits names like `hermes.subagent.start` — so
Hermes-specific extras can be added later via small data-dict additions to
the bundled plugin (upstreamable) or a companion micro-plugin emitting extra
marks, without forking the format.

Learning the emerging standards in the agent-observability space (NeMo
Relay, ATOF, ATIF) is an explicit goal of this work, independent of whether
the tool is ever made public.

## Decision

This tool will consume the ATOF JSONL stream exported by hermes-agent's
bundled `observability/nemo_relay` plugin (`nemo-relay==0.3`,
`HERMES_NEMO_RELAY_ATOF_ENABLED=1`) as its source of prompt-timing data,
rather than logging via a new custom hook plugin.

Browser-side (WebUI client) timings are out of scope for now — the browser
leg is suspected to contribute little to overall latency. If that assumption
proves wrong, the preferred design is a separate beacon store joined to the
ATOF stream in this viewer on `session_id` plus time window, keeping the
ATOF stream purely agent-lifecycle; client-measured durations would be
carried as data rather than trusting cross-machine timestamps.

## Consequences

- The reader must parse ATOF JSONL (append-mode files, one event per line)
  and reconstruct turn waterfalls from span open/close events and marks,
  joined on the `metadata` correlation keys.
- We take a dependency on the `nemo-relay==0.3` package and on the bundled
  hermes-agent plugin. The plugin deliberately fails open: a missing SDK or
  misconfiguration produces no data and no error, so the viewer should
  surface "no recent events" prominently rather than silently showing an
  empty timeline.
- Timing granularity is capped at what the hooks emit. Sub-request detail
  such as time-to-first-token lives only in `agent/stream_diag.py` logging,
  not in ATOF; if needed later it must be added via extra marks.
- This repository currently only browses `jmem0_logged.db` (mem0 event log).
  Adopting this decision means restructuring it into a more general browser
  where mem0 browsing and the new prompt-timing view are separate plugins.
  That restructuring is deliberately sequenced after this record.
