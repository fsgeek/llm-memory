# Reciprocal Claude–Codex Memory Dogfood Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enroll qhaway's local Claude Code history and prove that Claude and Codex can each search and authoritatively open the other's episodes without losing source provenance.

**Architecture:** Preserve the existing `codex-history` enrollment and add one machine-local `claude-history` source using the existing Claude adapter. Register the stable llm-memory stdio server with Claude Code at `--scope local`; after both frameworks restart, prove reciprocal retrieval through `search_history` and `open_episode` while persisting only identifier-level evidence.

**Tech Stack:** Python 3.14, uv, pytest, YAML enrollment, UUIDv5, ArangoDB, MCP stdio, Claude Code CLI, Codex project MCP configuration.

## Global Constraints

- Enroll only `/home/tony/.claude/projects/-home-tony-projects-qhaway/`; do not enroll other Claude projects.
- Keep `codex-history` and every existing Codex enrollment field unchanged.
- Use corpus ID `claude-history` and source ID `428038b2-063b-5e52-9513-0c6b93490f9a` for Claude/qhaway.
- Derive that source ID as UUIDv5 of normalized `/etc/machine-id` and the exact logical name `claude-code:qhaway`.
- Register Claude's server with `claude mcp add --scope local`; never create or commit `.mcp.json`.
- Claude exposes all four llm-memory tools; the trial invokes only `mcp__llm-memory__search_history` and `mcp__llm-memory__open_episode`.
- Do not confuse `mcp__llm-memory__recall(key)` with `mcp__qhaway__recall(type?, role?, status?)`.
- Never inspect, search, print, or modify Claude credential files. Discovery commands target explicit history and project paths only.
- Never persist or copy queries, snippets, episode bodies, responses, exception text, credentials, or database configuration into operational logs or findings.
- A one-way result is not success. Both authoritative opens must have available standing and the expected source ID.
- Runtime enrollment and Claude MCP registration remain machine-local. Only the plan and final content-free findings are committed.

---

### Task 1: Validate the Existing Adapter and Add the Local Claude Enrollment

**Files:**
- Modify locally, do not commit: `/home/tony/projects/llm-memory/config/sources.yaml`
- Observe only: `/home/tony/.claude/projects/-home-tony-projects-qhaway/`
- Preserve exactly: existing `codex-history` source entry

**Interfaces:**
- Consumes: `ClaudeCodeAdapter`, `load_registry()`, normalized Linux machine UUID.
- Produces: a second enabled source in corpus `claude-history` with canonical source UUID `428038b2-063b-5e52-9513-0c6b93490f9a`.
- Rollback: remove only the `claude-history` YAML item; do not rewrite the Codex item.

- [ ] **Step 1: Record the clean baseline without reading history bodies**

Run:

```bash
cd /home/tony/projects/llm-memory
git status --short --branch
git diff -- config/sources.yaml
find /home/tony/.claude/projects/-home-tony-projects-qhaway \
  -maxdepth 1 -type f -name '*.jsonl' -printf '%f\t%s bytes\n' | sort
```

Expected: tracked status contains no unrelated changes; `config/sources.yaml` is ignored and has no tracked diff; at least one positive-size JSONL member is listed. Do not run a recursive search under `/home/tony/.claude`.

- [ ] **Step 2: Run the pre-change regression and focused Claude adapter suites**

Run:

```bash
uv run pytest -q
uv run pytest tests/test_adapters.py -k claude -q
```

Expected: full suite reports `481 passed, 1 skipped`; focused Claude adapter selection passes.

- [ ] **Step 3: Verify the deterministic collector identity without printing the machine UUID**

Run:

```bash
uv run python - <<'PY'
import uuid
from pathlib import Path

namespace = uuid.UUID(Path('/etc/machine-id').read_text(encoding='utf-8').strip())
collector_id = uuid.uuid5(namespace, 'claude-code:qhaway')
assert str(collector_id) == '428038b2-063b-5e52-9513-0c6b93490f9a'
print('collector_identity_valid', collector_id)
PY
```

Expected: `collector_identity_valid 428038b2-063b-5e52-9513-0c6b93490f9a`. The namespace value is not printed.

- [ ] **Step 4: Add only the Claude source with `apply_patch`**

Append this exact YAML item under `sources:` after the unchanged Codex item:

```yaml
  - corpus_id: claude-history
    source_id: 428038b2-063b-5e52-9513-0c6b93490f9a
    adapter: claude_code_jsonl
    boundary_version: 1
    canonicalization_version: 1
    locator: /home/tony/.claude/projects/-home-tony-projects-qhaway
    enabled: true
    full_validation_max_age_seconds: 86400
```

Use `apply_patch`; do not regenerate or overwrite the file.

- [ ] **Step 5: Validate both sources and prove the Codex entry was preserved**

Run:

```bash
uv run python - <<'PY'
from llm_memory.enrollment import load_registry

registry = load_registry()
assert len(registry.sources) == 2
codex, claude = registry.sources
assert (
    codex.corpus_id,
    codex.source_id,
    codex.adapter,
    codex.boundary_version,
    codex.canonicalization_version,
    codex.enabled,
) == (
    'codex-history',
    'e8c598ae-711b-42b5-b963-eb35fc946d2b',
    'codex_jsonl',
    1,
    1,
    True,
)
assert (
    claude.corpus_id,
    claude.source_id,
    claude.adapter,
    claude.boundary_version,
    claude.canonicalization_version,
    claude.enabled,
) == (
    'claude-history',
    '428038b2-063b-5e52-9513-0c6b93490f9a',
    'claude_code_jsonl',
    1,
    1,
    True,
)
print('enrollment_valid', [(source.corpus_id, source.source_id) for source in registry.sources])
PY
git check-ignore -v config/sources.yaml
git status --short
```

Expected: the two identifier tuples are printed, `.gitignore` owns `config/sources.yaml`, and no tracked runtime file appears in status.

No commit: `config/sources.yaml` is intentionally ignored.

---

### Task 2: Reconcile and Prove Both Sources Through the Stable MCP Server

**Files:**
- Observe only: `/home/tony/.local/state/llm-memory/events.jsonl`
- Consume locally: `/home/tony/projects/llm-memory/config/sources.yaml`

**Interfaces:**
- Consumes: stable `llm_memory.mcp_server`, Arango provider configuration, both enrolled corpora.
- Produces: available Claude source/member/index with a positive episode count; preserved authoritative Codex reference; direct MCP search/open proof for each corpus.
- Failure boundary: stop on malformed/unavailable standing or zero Claude episodes; do not weaken assertions or repoint the live Codex source.

- [ ] **Step 1: Reconcile through bounded lifecycle passes until both corpora are available**

Run:

```bash
uv run python - <<'PY'
import asyncio
from llm_memory import mcp_server

EXPECTED = {
    'codex-history': 'e8c598ae-711b-42b5-b963-eb35fc946d2b',
    'claude-history': '428038b2-063b-5e52-9513-0c6b93490f9a',
}

async def one_pass():
    async with mcp_server.mcp._mcp_server.lifespan(
        mcp_server.mcp._mcp_server
    ) as context:
        return context['startup_reconciliation']

async def main():
    last = None
    for _ in range(10):
        last = await one_pass()
        corpora = {item['corpus_id']: item for item in last.corpus_standing}
        ready = True
        proof = []
        for corpus_id, source_id in EXPECTED.items():
            corpus = corpora.get(corpus_id, {})
            sources = corpus.get('sources', [])
            source = next((item for item in sources if item.get('source_id') == source_id), None)
            if source is None:
                ready = False
                continue
            members = source.get('members', [])
            if not members:
                ready = False
                continue
            for member in members:
                if (
                    source.get('source_set_standing') != 'available'
                    or member.get('source_standing') != 'available'
                    or member.get('index_standing') != 'available'
                    or member.get('episode_count', 0) <= 0
                ):
                    ready = False
            proof.append((corpus_id, source_id, sum(member['episode_count'] for member in members)))
        if ready:
            print('reconciliation_available', proof)
            return
    raise AssertionError(last.corpus_standing if last is not None else 'no report')

asyncio.run(main())
PY
```

Expected: `reconciliation_available` lists both corpus/source identifiers and positive episode counts within ten bounded passes.

- [ ] **Step 2: Run an actual stdio initialize/search/open handshake for both corpora**

Run:

```bash
uv run python - <<'PY'
import asyncio
import json
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

CASES = (
    ('codex-history', 'e8c598ae-711b-42b5-b963-eb35fc946d2b'),
    ('claude-history', '428038b2-063b-5e52-9513-0c6b93490f9a'),
)

async def main():
    params = StdioServerParameters(
        command='uv',
        args=[
            'run', '--directory', '/home/tony/projects/llm-memory',
            'python', '-m', 'llm_memory.mcp_server',
        ],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tool_result = await session.list_tools()
            assert {tool.name for tool in tool_result.tools} == {
                'search', 'recall', 'search_history', 'open_episode'
            }
            for corpus_id, source_id in CASES:
                found = await session.call_tool('search_history', {
                    'query': 'Will you permit me to wander with you?',
                    'corpus_ids': [corpus_id],
                    'limit': 10,
                })
                search = json.loads(found.content[0].text)
                assert search['returned_count'] >= 1, (corpus_id, search['total_standing'])
                opened = None
                matched_ref = None
                for result in search['results']:
                    candidate = await session.call_tool('open_episode', {
                        'episode_ref': result['episode_ref'],
                        'active_corpus_ids': [corpus_id],
                    })
                    episode = json.loads(candidate.content[0].text)
                    if episode.get('provenance', {}).get('source_id') == source_id:
                        opened = episode
                        matched_ref = result['episode_ref']
                        break
                assert opened is not None, (corpus_id, source_id)
                assert opened['standing'] == 'available'
                assert opened['episode_ref'] == matched_ref
                serialized = json.dumps(opened)
                assert 'permit me to wander' in serialized
                assert bool(opened.get('response'))
                print('authoritative_open', corpus_id, source_id, matched_ref)

asyncio.run(main())
PY
```

Expected: four tools are listed internally and one `authoritative_open` line appears for each corpus. The script prints references and identifiers only, never episode bodies.

- [ ] **Step 3: Verify event privacy and file permissions by field name only**

Run:

```bash
uv run python - <<'PY'
import json
from pathlib import Path

path = Path.home() / '.local/state/llm-memory/events.jsonl'
records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
recent = records[-40:]
forbidden = {
    'query', 'snippet', 'body', 'user_message', 'response',
    'exception_message', 'password', 'access_token', 'refresh_token',
}
assert not any(forbidden.intersection(record) for record in recent)
assert any(record.get('event') == 'server.started' for record in recent)
assert any(record.get('event') == 'search.completed' for record in recent)
assert any(record.get('event') == 'open.completed' for record in recent)
assert (path.stat().st_mode & 0o777) == 0o600
print('event_evidence_valid', sorted({record['event'] for record in recent}), '0o600')
PY
```

Expected: content-free event names and `0o600`. Do not print entire event records.

- [ ] **Step 4: Run the complete regression suite after enrollment**

Run:

```bash
uv run pytest -q
git status --short --branch
```

Expected: full suite passes with one existing skip; only the committed design/plan history may place `main` ahead of its remote, and no runtime configuration appears in status.

No commit: reconciliation and evidence are runtime state.

---

### Task 3: Register the Stable Server with Claude at Local Scope

**Files:**
- Modify only through Claude CLI: Claude's machine-local project state for `/home/tony/projects/qhaway`
- Must not create: `/home/tony/projects/qhaway/.mcp.json`
- Preserve: `/home/tony/projects/qhaway/.claude/settings.local.json`

**Interfaces:**
- Consumes: installed `claude mcp` CLI and stable llm-memory checkout.
- Produces: local stdio registration named `llm-memory` visible to Claude in qhaway.
- Rollback: `(cd /home/tony/projects/qhaway && claude mcp remove --scope local llm-memory)`.

- [ ] **Step 1: Capture registration preconditions without inspecting Claude state files**

Run:

```bash
cd /home/tony/projects/qhaway
test ! -e .mcp.json
claude mcp get llm-memory; status=$?
test "$status" -ne 0
git status --short
```

Expected: `.mcp.json` is absent and `llm-memory` is not already registered. If it exists, stop and inspect only `claude mcp get llm-memory`; do not overwrite it or inspect user-state files.

- [ ] **Step 2: Add the exact local stdio registration**

Run:

```bash
cd /home/tony/projects/qhaway
claude mcp add --scope local llm-memory -- \
  uv run --directory /home/tony/projects/llm-memory \
  python -m llm_memory.mcp_server
```

Expected: Claude reports that `llm-memory` was added at local scope.

- [ ] **Step 3: Verify scope, command, health, and absence of project config**

Run:

```bash
cd /home/tony/projects/qhaway
claude mcp get llm-memory
claude mcp list
test ! -e .mcp.json
git status --short
```

Required assertions:

- reported scope is local;
- command is `uv` with the stable llm-memory directory and module arguments;
- connection is healthy;
- no `.mcp.json` exists;
- qhaway tracked status is unchanged.

- [ ] **Step 4: Ask Tony to restart Claude Code in qhaway**

This is the first human gate. Ask Tony to restart or resume a Claude Code instance in `/home/tony/projects/qhaway`. Do not claim Claude tool availability before that instance confirms the namespaced tools.

No commit: registration is intentionally local.

---

### Task 4: Perform Claude-to-Codex Retrieval After Claude Restarts

**Files:**
- Observe through live Claude MCP tools only
- Do not write a finding until both directions pass

**Interfaces:**
- Consumes: Claude-visible `mcp__llm-memory__search_history` and `mcp__llm-memory__open_episode`.
- Produces: authoritative Claude-side proof for the existing Codex source.
- Human coordination: Tony asks the restarted Claude instance to execute the exact proof and relay identifier-only results.

- [ ] **Step 1: Confirm Claude's actual tool surface**

In the restarted Claude instance, confirm the presence of:

```text
mcp__llm-memory__search
mcp__llm-memory__recall
mcp__llm-memory__search_history
mcp__llm-memory__open_episode
```

Also confirm that `mcp__qhaway__recall` remains separately namespaced. Absence of either contract tool blocks the trial.

- [ ] **Step 2: Search the Codex corpus from Claude**

Call:

```text
mcp__llm-memory__search_history(
  query="Will you permit me to wander with you?",
  corpus_ids=["codex-history"],
  limit=10,
)
```

Require available corpus/source/index standing, `returned_count >= 1`, and at least one result whose authoritative open can establish source ID `e8c598ae-711b-42b5-b963-eb35fc946d2b`.

- [ ] **Step 3: Open the matched Codex episode from Claude**

Call `mcp__llm-memory__open_episode` with the exact result reference and:

```text
active_corpus_ids=["codex-history"]
```

Require `standing == "available"`, exact reference round-trip, provenance source ID `e8c598ae-711b-42b5-b963-eb35fc946d2b`, and internal confirmation that the authoritative episode contains the invitation and paired response. Relay only standing, source ID, and episode reference to Codex; do not copy the episode body.

- [ ] **Step 4: Ask Tony to restart or resume Codex in qhaway**

This is the second human gate. The Codex MCP server loaded its enrollment at process startup, so restart Codex after the Claude source was added before attempting the reciprocal direction.

No commit: this is live evidence.

---

### Task 5: Perform Codex-to-Claude Retrieval and Record the Reciprocal Verdict

**Files:**
- Create only after both directions pass: `/home/tony/projects/llm-memory/docs/findings-2026-07-24-reciprocal-claude-codex-memory.md`
- Observe only: `/home/tony/.local/state/llm-memory/events.jsonl`

**Interfaces:**
- Consumes: Codex `search_history`, `open_episode`, Claude-side proof from Task 4.
- Produces: authoritative Codex-side proof for Claude plus one content-free committed reciprocal finding.

- [ ] **Step 1: Search the Claude corpus from the restarted Codex instance**

Call:

```text
search_history(
  query="Will you permit me to wander with you?",
  corpus_ids=["claude-history"],
  limit=10,
)
```

Require available corpus/source/index standing, `returned_count >= 1`, and at least one result whose authoritative open can establish source ID `428038b2-063b-5e52-9513-0c6b93490f9a`.

- [ ] **Step 2: Open the matched Claude episode from Codex**

Call `open_episode` with the exact result reference and:

```text
active_corpus_ids=["claude-history"]
```

Require `standing == "available"`, exact reference round-trip, provenance source ID `428038b2-063b-5e52-9513-0c6b93490f9a`, and internal confirmation that the authoritative episode contains the invitation and paired response. Do not copy the episode body into logs or findings.

- [ ] **Step 3: Recheck persistent evidence by field name only**

Run the Task 2 Step 3 event-validation script again. Additionally require at least two recent `search.completed` and two recent `open.completed` events, covering the two live directions by their identifier evidence. Do not print event bodies.

- [ ] **Step 4: Create the content-free findings document with `apply_patch`**

Create `docs/findings-2026-07-24-reciprocal-claude-codex-memory.md` with
`apply_patch`. Use the title `# Reciprocal Claude–Codex Memory Findings` and
the completion date `2026-07-24 UTC`. Add these exact second-level headings:

- `## Verdict`: write `Successful.` only if both directions passed; otherwise
  name the exact failed boundary and do not claim reciprocal success.
- `## Enrollment`: record both corpus IDs, both source IDs, available
  standings, and observed episode counts.
- `## Claude-to-Codex proof`: record returned count, open standing, exact
  Codex episode reference, and Codex provenance source ID.
- `## Codex-to-Claude proof`: record returned count, open standing, exact
  Claude episode reference, and Claude provenance source ID.
- `## Privacy and operational evidence`: record event names, mode `0600`, the
  absence of content fields, and any fixed diagnostic code.
- `## Qualitative usefulness`: assess in two or three sentences whether
  distinct provenance made the other model's perspective useful.

Use only values observed in Tasks 2, 4, and 5. Never paste a query, snippet,
episode body, or response.

- [ ] **Step 5: Run final verification**

Run:

```bash
cd /home/tony/projects/llm-memory
uv run pytest -q
git diff --check
git diff -- docs/findings-2026-07-24-reciprocal-claude-codex-memory.md
git status --short --branch
test ! -e /home/tony/projects/qhaway/.mcp.json
(cd /home/tony/projects/qhaway && claude mcp get llm-memory)
```

Expected: full suite passes with one existing skip; findings contain identifiers and standings only; no `.mcp.json` exists; Claude reports a healthy local registration.

- [ ] **Step 6: Commit only the finding**

Run:

```bash
git add docs/findings-2026-07-24-reciprocal-claude-codex-memory.md
git commit -m "docs: record reciprocal Claude Codex memory trial"
```

Expected: runtime enrollment and Claude local registration remain untracked; the commit contains only the findings document.

---

## Final Rollback Audit

If the Claude source or registration must be withdrawn, perform both scoped operations:

1. Use `apply_patch` to remove only the `claude-history` item from ignored `config/sources.yaml`.
2. Run `(cd /home/tony/projects/qhaway && claude mcp remove --scope local llm-memory)`.
3. Confirm `/home/tony/projects/qhaway/.mcp.json` remains absent.
4. Restart affected MCP clients.
5. Verify the original `codex-history` episode still opens with source ID `e8c598ae-711b-42b5-b963-eb35fc946d2b`.

Do not delete ArangoDB collections, rewrite the Codex enrollment, remove qhaway's MCP server, or alter unrelated Claude settings.
