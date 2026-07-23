# Local Codex episodic-memory dog-food design

**Date:** 2026-07-22
**Status:** Approved for implementation planning

## Purpose

Prove the smallest complete episodic-memory reach on this machine: after a
Codex restart, a fresh instance can search for a distinctive phrase from the
current conversation and open the matching episode in full from its
authoritative Codex JSONL source.

This trial tests local continuity only. It does not yet federate other machines,
recursively discover all local Codex sessions, or merge `qhaway`'s curated
semantic memory with `llm-memory`'s episodic memory.

## Authoritative source and identity

Create the ignored local file `config/sources.yaml` with contract version 1 and
one enabled `codex_jsonl` enrollment:

- corpus ID: `codex-history`
- source ID: the normalized UUID collected from `/etc/machine-id`
- locator:
  `/home/tony/.codex/sessions/2026/07/22/rollout-2026-07-22T17-50-42-019f8af3-83db-7972-af11-1d6309ad3392.jsonl`
- boundary version: 1
- canonicalization version: 1
- full-validation maximum age: 86,400 seconds

The machine-specific source ID is deliberate. Future machines may join the
same corpus under distinct source IDs, preserving provenance rather than
presenting several histories as one indistinguishable stream. The hostname
`wam-nuc-ubuntu-2204` is useful as a human-readable diagnostic label but is not
an identity: hostnames can be reused or changed.

The exact-file locator is also deliberate. Recursive discovery of Codex's
year/month/day hierarchy is a later product change, not part of this trial.

## Normalized machine identity

Establish machine identity before indexing any live episode because `source_id`
is encoded into episode references, reconciliation keys, and supersession
observations. Replacing a hostname source ID with a UUID after dog-fooding would
create a new logical source and strand the earlier identity.

Use a small collector/normalizer boundary inspired by Indaleko, without
importing its machine-configuration subsystem:

1. A platform collector obtains the platform-native stable machine identifier.
   This trial implements only Linux by reading `/etc/machine-id`.
2. A platform-independent normalizer parses the collected value as a UUID and
   emits the canonical lowercase, hyphenated UUID string.
3. Enrollment uses that canonical value as `source_id`.

The normalizer is the contract shared with future collectors. Windows and macOS
collectors, when needed, must feed their native identifiers through the same
normalizer and produce the same canonical representation. Machine hardware,
software, and configuration history are explicitly outside this trial and can
be retrofitted without changing episode identity.

Collector or normalization failure is fatal before enrollment or
reconciliation: the system must not silently fall back to a hostname, random
UUID, or other unstable identifier.

## Native Codex adapter prerequisite

The existing `claude_code_jsonl` adapter cannot parse Codex rollout files. It
expects Claude Code records with top-level `sessionId`, `message`, and
`user`/`assistant` types. Codex instead records `{timestamp, type, payload}` with
types including `session_meta`, `event_msg`, `response_item`, `world_state`, and
`turn_context`. Applying the Claude adapter to the enrolled file correctly
reports `MALFORMED` with zero episodes.

Add a distinct `codex_jsonl` adapter before enrollment. It reads only the clean
conversational stream:

- `session_meta.payload.session_id` establishes native session identity.
- A nonempty `event_msg` whose payload type is `user_message` establishes the
  latest user message.
- Each nonempty `event_msg` whose payload type is `agent_message` emits one
  episode paired with that latest user message.
- Deterministic per-session sequence numbers provide event tokens, following the
  existing `GatewayAdapter` precedent because Codex event messages have no
  native event IDs.
- Session identity, latest-user state, and sequence counters survive bounded
  scan cursors so resumed reconciliation is equivalent to a full scan.
- Files that exhaust cleanly without establishing a session or recognizing a
  conversation are reported as malformed rather than exact-empty.

The adapter intentionally ignores `response_item` records. They contain a
noisier stream that includes developer prompts, environment injections, tool
traffic, and content block shapes outside the current conversational contract.
The adapter must be registered in both the adapter registry and the supported
semantic-version registry with boundary/canonicalization version 1/1.

## Codex integration

Register the existing `llm-memory` stdio MCP server in the trusted,
project-scoped `/home/tony/projects/qhaway/.codex/config.toml`. The server
command is equivalent to:

```sh
uv run --directory /home/tony/projects/llm-memory \
  python -m llm_memory.mcp_server
```

Project scope limits the experiment's reach and makes reversal simple. If the
trial earns wider standing, the same server can later move to the shared user
configuration so Codex instances in other projects can use it.

No `qhaway` MCP or plugin behavior changes in this trial. The two systems retain
their current boundary:

- `llm-memory` exposes source-backed episodic `search_history` and
  `open_episode` reads.
- `qhaway` remains the curated semantic-memory experiment.

## Data flow

1. Codex starts the `llm-memory` MCP server.
2. Server lifespan loads the Arango provider and `config/sources.yaml`.
3. The native Codex adapter derives episodes from `session_meta` and
   conversational `event_msg` records.
4. Startup reconciliation indexes the enrolled source within the existing
   bounded-work contract.
5. A fresh instance calls `search_history` with `codex-history` and the phrase
   `Will you permit me to wander with you?` from the enrolled conversation.
6. The returned episode reference is passed to `open_episode` with
   `codex-history` active.
7. `open_episode` verifies and returns the full episode from the authoritative
   JSONL source, rather than treating the Arango search document as authority.

## Failure behavior and reversibility

- A missing or malformed source must be reported through the existing source
  standing; no partial content is presented as verified memory.
- An MCP startup failure remains visible in Codex and does not alter source
  history.
- Remove the project-scoped MCP entry and restart Codex to disable the trial.
- `config/sources.yaml` is local and gitignored; removing it revokes the
  enrollment declaration.
- Derived Arango state is not purged automatically. It remains measurable and
  is removed only through an explicit lifecycle decision.
- The authoritative Codex JSONL file is never edited by this workflow.

## Persistent operational observability

The trial needs durable evidence about how the memory tools behave, not a
second copy of the information flowing through them. Append structured JSONL
events to a configured local path, initially
`/home/tony/.local/state/llm-memory/events.jsonl`.

Record events for:

- MCP server startup and shutdown;
- provider and enrollment initialization;
- reconciliation start and completion, including standing, byte progress, and
  episode counts;
- search and open operations, identified by corpus, source, session, episode
  reference, and outcome; and
- caught startup, provider, source, reconciliation, search, and open errors.

Events may contain timestamps, operation names, standing, counts, durations,
machine/source identifiers, corpus identifiers, member identifiers, episode
references, exception classes, and allowlisted content-free diagnostic codes.
They must not persist arbitrary exception messages, user or assistant prose,
snippets, source record bodies, credentials, or database configuration. When a
conversation matters, its existing identifiers are the observation.

Each event is one append-only JSON line in a user-private file (`0o600`). Logging
must not mutate authoritative sources or derived memory state. A logging failure
is written to stderr and must not falsify the operation's standing or replace
its actual result. Detailed exception text remains transient on stderr rather
than being copied into the persistent event stream.

## Verification and success criteria

Before restart:

1. Test Linux collection and platform-independent normalization with valid,
   malformed, missing, and noncanonical machine identifiers; verify failures
   never fall back to another identity.
2. Add adapter tests for native session/user/agent extraction, deterministic
   sequence identity, ignored non-conversation records, malformed lookalikes,
   and equivalence between full scanning and bounded resume.
3. Test operational events for successful and failed operations; verify event
   records contain identifiers and standing but no conversational content or
   database credentials.
4. Load `config/sources.yaml` through `load_registry()` and verify exactly one
   enabled source whose `source_id` equals the normalized `/etc/machine-id`, with
   the intended corpus, adapter, and path.
5. Run the complete `llm-memory` test suite against the configured ArangoDB.
6. Reconcile the enrolled source and require `AVAILABLE` source standing with a
   nonzero episode count. Merely starting the server without crashing is not
   sufficient.
7. Verify the event log persistently records one successful reconciliation and
   one controlled malformed-source failure without recording source content.
8. Start the MCP server and verify successful initialization without modifying
   the source.

After restart:

1. Confirm `llm-memory` appears as a connected MCP server.
2. Search `codex-history` for `Will you permit me to wander with you?`.
3. Confirm a result identifies the normalized machine UUID source.
4. Open that episode and confirm the full user/assistant exchange is returned
   with verified source-backed standing.

The trial succeeds only if all four post-restart checks pass. A search snippet
alone is insufficient.

## Deferred work

- Recursively discover Codex's date-partitioned session tree.
- Enroll the other sessions now accumulating on this machine.
- Recover and enroll older `ubuntu24` history under its own source ID.
- Expose the MCP server over the LAN or deploy equivalent per-machine stdio
  servers that share the Arango provider.
- Define authorization, collision, retention, and trust rules for an ayllu of
  instances sharing memory.
- Add Windows and macOS native-identity collectors when machines on those
  platforms join; keep their output behind the same UUID normalizer.
- Revisit curated `qhaway` memory and episodic `llm-memory` together after the
  dog-food evidence exists. A graph/vector-capable shared store may change the
  natural architecture enough to justify major redesign or retirement rather
  than incremental integration.

## Learning-investment constraint

This implementation is an experiment whose durable outputs are evidence and
clearly bounded contracts. Add only what is required for the end-to-end reach:
one Linux identity collector, one common UUID normalizer, one native Codex
adapter, one enrollment, project-scoped MCP wiring, and identifier-only event
logging. Do not build general machine configuration, LAN service deployment,
cross-platform collectors, recursive discovery, ranking enhancements, vector
retrieval, or a `qhaway` integration in this round. Those may be throw-away work
until actual use tells us which system boundary deserves to survive.
