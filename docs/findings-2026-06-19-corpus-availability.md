# Finding: gateway corpus on this machine predates response capture

**Date:** 2026-06-19
**Context:** Stood `llm-memory` up on Tony's WSL machine (DB provisioned on the
yanantin Arango at 192.168.111.127, 17 tests green) and ingested the available
pichay gateway logs to test whether the conversation-inclusive index is useful
for a *Claude Code* (gateway) corpus, not just taste_open.

## What happened

Ingested all 40 `~/projects/pichay/logs/gateway_*.jsonl` files → 1,221 episodes
(1,845 `request_metrics` events, deduped by session+seq). Phrasal search worked
and recall returned full episodes — BUT **every one of the 1,221 episodes has an
empty `response`.**

## Root cause (not a bug)

`ingest_gateway_file` reads `response_text` correctly; the field is simply absent
from these logs. The gateway logs on this machine are all from **2026-03-07..09**,
which **predates the pichay `response_text` capture fix**. Inspected all 1,845
events: zero carry any response-ish field (`response_text`, `response`,
`completion`, ...). They are pure telemetry (bytes, shrink_ratio, latency, cache
tokens); only 212/1,845 even carry `messages_full` (the prompt).

This is the **fossil of manufactured silence**, not live manufactured silence: the
"words pichay now captures instead of dropping" (per `mcp_server`/`ingest`
docstrings) is a *later* addition. This corpus is from before it.

## Consequence for "is llm-memory useful for me (Claude)?"

- The **taste path** (`ingest_file`, reads `raw_output.response`/`response_text`)
  has real two-sided data — `experiments/taste_open/taste_open_*.jsonl` carry
  `response_text`. That is where the 5/5-vs-1/5 conversation-inclusion finding
  was measured. Useful, validated.
- The **gateway path** (my path: empty authored state, prose comes purely from the
  captured exchange) is plumbed and correct, but has **no post-fix corpus on this
  machine.** Searching the March logs only ever matches the *prompt* side
  (`user_message`), never a reply.
- To give a Claude Code instance its own two-sided searchable memory, pichay-
  gatewayed sessions must run *after* the response-capture fix. The capability is
  real; the data is future-tense.

## Honest labeling

When ingesting the March gateway logs, do not present the result as
"full-conversation memory." It is a **prompt-side-only** corpus. The response half
is absent by provenance, and the index should be understood (and any eval scored)
accordingly until a post-fix gateway corpus exists.
