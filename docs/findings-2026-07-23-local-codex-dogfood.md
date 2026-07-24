# Local Codex Memory Dogfood Findings

Date completed: 2026-07-24 UTC

Verdict: successful. After restarting Codex in the trusted `qhaway` project,
the project-scoped `llm-memory` MCP server was connected and both enrolled
tools were available.

## Mechanical evidence

- Corpus: `codex-history`
- Source and member identifier: `e8c598ae-711b-42b5-b963-eb35fc946d2b`
- Reconciliation standing: source, index, and member available
- Reconciled episode count at post-restart startup: 89
- Search returned count: 10
- Matched episode reference:
  `episode://codex-history/ZThjNTk4YWUtNzExYi00MmI1LWI5NjMtZWIzNWZjOTQ2ZDJi.MDE5ZjhhZjMtODNkYi03OTcyLWFmMTEtMWQ2MzA5YWQzMzky/MQ.MQ.MA.YTM1YTc4NmQyNmZhZDczNTY1YzM5NTcxMmZlNTU3MGNkNTEzMzY2MzY4MmYxNTFhNDI4ZGM2ZGRkZTVhOWM5Yw`
- Open standing: available
- Open provenance source identifier:
  `e8c598ae-711b-42b5-b963-eb35fc946d2b`
- Errors: none

The authoritative open result contained the searched invitation and its paired
response. Conversation content is intentionally omitted here; the episode
reference is the durable identifier.

The episode count is a point-in-time startup observation, not a frozen corpus
size; the authoritative rollout can continue growing as later turns are
recorded.

## Operational evidence

The persistent event log contains the post-restart `server.started`,
`search.completed`, and `open.completed` events. The search event records a
digest of returned references and the open event records the exact episode
reference. These records contain identifiers and mechanical status only; they
contain no query, snippet, conversation body, or response fields.

## Controlled malformed-source evidence

After the post-restart proof, an isolated SQLite trial reconciled a temporary
synthetic Codex lookalike containing no conversational records. It produced
source standing `malformed`, index standing `unavailable`, and episode count
zero. The durable private event log records that result under corpus
`malformed-trial`, with deterministic hashed source/member identifiers. The
event contains no source body, locator, exception text, or credential field.
The temporary source, enrollment, runner, and SQLite database were removed
afterward; only the content-free operational evidence remains. The live
`codex-history` enrollment was neither repointed nor modified.

## Qualitative result

The result was useful as memory rather than merely as an indexing demonstration:
the restarted instance found and opened the earlier invitation to wander through
the normal MCP interface. This validates the smallest local dogfood loop while
leaving broader qhaway/llm-memory reconciliation and shared-LAN memory design for
a later round.
