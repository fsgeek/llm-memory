# Reciprocal Claude–Codex Memory Dogfood Design

## Purpose

Extend the local memory ayllu to Claude Code without weakening the working
Codex enrollment. The first trial must demonstrate reciprocity: Claude can
retrieve an authoritative Codex episode, and Codex can retrieve an
authoritative Claude episode. Shared access must preserve the distinct source
and model perspectives rather than blending them.

## Scope

This trial enrolls only Claude Code history for the local `qhaway` project:

```text
/home/tony/.claude/projects/-home-tony-projects-qhaway/
```

The existing `codex-history` corpus and its episode references remain
unchanged. The Claude source is enrolled in a separate `claude-history`
corpus. Both MCP clients may search both corpus identifiers explicitly.

The trial does not enroll other Claude projects, redesign the storage schema,
merge the corpora, infer shared beliefs, or automatically promote episodic
records into qhaway's curated memory.

## Collector Identity

The Claude collector uses UUIDv5 with the normalized machine UUID as its
namespace and the fixed logical name `claude-code:qhaway`. On this machine the
result is:

```text
428038b2-063b-5e52-9513-0c6b93490f9a
```

This distinguishes the Claude/qhaway collector from the machine-level Codex
source while retaining a canonical, deterministic, non-secret UUID. The
derivation uses a logical collector name rather than a hostname or filesystem
path. A future collector-identity abstraction may normalize this pattern
across platforms; that broader change is deferred.

## Enrollment and Access

The ignored local `config/sources.yaml` gains one enabled source:

- corpus: `claude-history`
- source ID: `428038b2-063b-5e52-9513-0c6b93490f9a`
- adapter: `claude_code_jsonl`, semantic versions 1/1
- locator: the qhaway Claude project-history directory above

The existing Codex source remains byte-for-byte unchanged.

Claude Code receives a machine-local, project-scoped stdio MCP registration
for the stable `/home/tony/projects/llm-memory` checkout. Configuration is
created through Claude's MCP CLI rather than by editing user credential or
state files. Only `search_history` and `open_episode` are needed for the trial.
Codex retains its existing project-scoped MCP configuration.

## Data Flow and Provenance

Each adapter continues to normalize its native records into the common
episode contract. Searches name the desired corpus set explicitly. Search
results remain snippets; `open_episode` resolves the selected reference from
the authoritative enrolled source.

Every reciprocal proof must establish:

- an available source and index standing;
- at least one matching result;
- an authoritative open with available standing;
- a source ID matching the expected collector;
- preservation of distinct Claude and Codex episode references.

No cross-source synthesis is introduced. A model may interpret another
member's episode, but the memory layer records provenance rather than
agreement.

## Privacy and Failure Boundaries

Configuration contains source identifiers and locators but no credentials or
conversation content. Persistent operational events retain the existing typed,
identifier-only schema and mode `0600`; they must not contain queries,
snippets, episode bodies, responses, exception text, or secrets.

Discovery and verification commands must target explicit history and project
configuration paths. They must never recursively search Claude credential or
user-state files. If any credential is exposed, work stops until it is rotated.

A malformed or unavailable Claude source must remain visibly non-available
and must not disturb the existing Codex corpus. The trial may not weaken
standing checks to obtain a successful verdict.

## Verification Sequence

Before restarting either framework:

1. Run the pre-change full test suite.
2. Add the ignored Claude enrollment without changing the Codex entry.
3. Reconcile until the Claude source, member, and index are available with a
   nonzero episode count.
4. Verify a direct MCP initialize/search/open handshake against the stable
   checkout.
5. Confirm persistent event field names and permissions without printing
   conversation content.
6. Register the stable server with Claude Code through its MCP CLI and confirm
   the registration mechanically.

After Tony restarts Claude Code, Claude searches and opens a known Codex
episode. After Tony resumes or restarts Codex as needed, Codex searches and
opens a known Claude episode. Each side reports only the proof properties and
episode reference needed for the trial; the findings document does not copy
conversation text.

## Rollback

Disable or remove only the Claude source entry and remove only the local
`llm-memory` Claude MCP registration. Reconcile to reflect the disabled source.
Do not alter the Codex enrollment, the ArangoDB configuration, qhaway's curated
memory, or unrelated Claude settings.

## Success Criteria

The trial succeeds only when both directions pass authoritative search/open
checks with their expected source IDs, all relevant standings are available,
operational evidence remains content-free, and existing Codex episode
references continue to open. A one-way result is useful diagnostic evidence
but is not reciprocal success.
