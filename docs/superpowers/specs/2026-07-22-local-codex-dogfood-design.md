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
one enabled `claude_code_jsonl` enrollment:

- corpus ID: `codex-history`
- source ID: `wam-nuc-ubuntu-2204`
- locator:
  `/home/tony/.codex/sessions/2026/07/22/rollout-2026-07-22T17-50-42-019f8af3-83db-7972-af11-1d6309ad3392.jsonl`
- boundary version: 1
- canonicalization version: 1
- full-validation maximum age: 86,400 seconds

The machine-specific source ID is deliberate. Future machines may join the
same corpus under distinct source IDs, preserving provenance rather than
presenting several histories as one indistinguishable stream.

The exact-file locator is also deliberate. The current adapter only discovers
immediate `*.jsonl` children of a directory; it does not recursively traverse
Codex's year/month/day hierarchy. Recursive discovery is a later product change,
not part of this trial.

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
3. Startup reconciliation indexes the enrolled source within the existing
   bounded-work contract.
4. A fresh instance calls `search_history` with `codex-history` and a distinctive
   phrase from the enrolled conversation.
5. The returned episode reference is passed to `open_episode` with
   `codex-history` active.
6. `open_episode` verifies and returns the full episode from the authoritative
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

## Verification and success criteria

Before restart:

1. Load `config/sources.yaml` through `load_registry()` and verify exactly one
   enabled source with the intended corpus, source identity, adapter, and path.
2. Run the existing `llm-memory` test suite against the configured ArangoDB.
3. Start the MCP server far enough to verify successful initialization and
   reconciliation without modifying the source.

After restart:

1. Confirm `llm-memory` appears as a connected MCP server.
2. Search `codex-history` for a distinctive phrase from this conversation.
3. Confirm a result identifies source `wam-nuc-ubuntu-2204`.
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
- Decide how curated `qhaway` memory and episodic `llm-memory` cooperate without
  collapsing their different authority models.
