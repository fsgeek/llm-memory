# Khipumaq: One Episodic Store, Fresh, That Promotes Itself

Status: approved design, written from a hamutay-session brainstorm with Tony
on 2026-09-01. Implementation belongs to an instance working in this repo
(with Codex authoring the validating tests in its own commits, per Tony's
code/test separation practice). Nothing here prescribes module-level design;
the implementer owns that.

## Context and evidence (measured 2026-09-01)

- The store held 4,878 episodes, newest 2026-07-27 — five weeks stale.
  Ingestion was manual and had run 3–4 times ever. A one-off three-host
  ingest (WSL/WAM-THREADRIPPER, ubuntu24, wam-nuc) brought it to 9,656
  episodes, newest same-day. 3,037 `yanantin_construction` episodes have
  source files that no longer exist on any machine: **the database is the
  only copy.** Claude Code's ~30-day transcript retention makes this a
  standing hazard, not a one-time accident.
- The repo contains two memory systems that each call the other legacy:
  - **A**: the Arango `episodes` store (`search`/`recall`), ~700 lines.
    Works; served every query in the brainstorm session.
  - **B**: the "versioned episodic contract" (`search_history`/
    `open_episode`, enrollment, reconcile, sqlite provider), ~6,277 lines
    plus ~8,235 lines of tests, built July 2026 from the qhaway ayllu
    specs. On this machine it is dead: no `config/sources.yaml`, so every
    call raises "episodic provider lifespan is not active". Its only
    enrollment ever was on wam-nuc (the 2026-07-23/24 dogfood findings).
- Tool-use census across 197 sessions / 10,415 tool calls on all three
  machines: qhaway used in 91 sessions (80 spontaneously — before Tony
  mentioned it); llm-memory in 16 (3 spontaneously); serena in 0, ever,
  despite always being available. The variable is not loading cost (all
  are deferred tools); it is whether a sentence exists at the moment of
  need that names the tool, the trigger, and the payoff. qhaway's
  MEMORY.md line ("before acting on any belief, call recall() first —
  your context is stale") is that sentence. llm-memory has none
  (`FastMCP("llm-memory", …)` passes no `instructions=`). Serena's
  ("call initial_instructions to read the manual") names a manual, not a
  need.

Tony's decisions from the brainstorm: keep A, remove B, rename the
project, make ingestion continuous, and make the server promote itself.

## D1 — Name: khipumaq

Package `khipumaq`, module `khipumaq`, MCP server name `khipumaq`,
repo rename at Tony's convenience. The Arango database stays `llm_memory`
(migrating a live database buys nothing; note the name in config comments).
The name is Tony's coinage — the store is the khipu; the historical
keeper's title, khipukamayuq, is already given to Yupay in ayllu memory.

## D2 — Remove the contract layer (B)

Delete: `reconcile`, `sqlite_store`, `sqlite_reconcile`, `sqlite_history`,
`sqlite_lifecycle`, `sqlite_provider`, `adapters`, `adapter_versions`,
`contract`, `contract_index`, `enrollment`, `lifecycle`, `history`,
`opening`, `index`, `evaluate`, their tests, the `search_history` and
`open_episode` MCP tools, the `pyyaml` dependency, and the
`episodic_contract_search` view / `episodic_contract_episodes` collection
in Arango. The July findings docs and specs stay — they are the record.

Two of B's principles are carried into A explicitly, as code and comment:

1. **No write tool.** Episodes are written by ingestion of the faithful
   record, never by the instance reaching for the store. The record stays
   an artifact, not something the instance edits about itself.
2. **Content-free operational events.** Keep the slice of `observability.py`
   that A needs: search/recall/ingest events carrying identifiers, digests,
   and counts only — never query text, snippets, or episode bodies.

B's goal — reciprocal Claude↔Codex memory — is re-met by D3's `codex`
label: both histories in one collection, one search surface, provenance
via `host`/`machine_id`/`source_file`.

## D3 — Ingestion becomes a first-class CLI

`python -m khipumaq.ingest` with subcommands:

- `claude-session <path>` — one Claude Code project JSONL. Also usable as
  a Claude Code `SessionEnd` hook: with no path argument, read the hook
  JSON from stdin and use its `transcript_path`.
- `sweep --root <dir>` — every session file under a projects tree,
  idempotently (existing `_key` = session+assistant-uuid, overwrite).
- `codex <path>|--all` — Codex CLI rollout files (`~/.codex/sessions/`,
  316 files on WAM-THREADRIPPER as of today) under label `codex`.
  First task: characterize the rollout format; the mapper mirrors
  `claude_session_to_episodes` (pair each assistant turn with the
  preceding user turn; skip tool-only turns).

Required episode fields, all paths: `host` (hostname), `machine_id`
(`/etc/machine-id`), canonical `source_file` (the path on the machine of
origin, never a staging copy). Label derivation from the project
directory: strip `-home-tony-projects-`; fold `--worktrees-<x>` into the
parent project; map scratchpad-launched dirs
(`-tmp-claude-1000-<project>-<uuid>-…`) to `<project>`; keep the existing
labels `yanantin_construction` and `quantumos` for those projects.
Nested `<session>/subagents/*.jsonl` files are ingested (currently ~70
files silently skipped).

The 2026-09-01 backfill left 4,258 episodes hostless (1,221 gateway-wire
episodes whose source is a file not a session, and 3,037 whose sources no
longer exist). They stay hostless; absence of the field is honest.

## D4 — Continuous operation, two layers

1. **`SessionEnd` hook on every machine** (alongside the existing qhaway
   hook): `claude-session` ingest of the ending session's transcript.
   Latency seconds; fails loudly to stderr when the DB at
   192.168.111.127:8529 is unreachable, and does not retry — the sweep is
   the retry.
2. **Nightly sweep on WAM-THREADRIPPER** (systemd user timer): rsync
   `~/.claude/projects` from ubuntu24 and wam-nuc into a staging dir,
   `sweep` each with the right `--host`/`--machine-id`, then sweep local
   and `~/.codex/sessions`. Deadline is the ~30-day retention; nightly is
   30× margin. A machine that is off tonight is caught tomorrow.

## D5 — The server promotes itself

One dynamic sentence in `FastMCP(..., instructions=…)`, rebuilt at server
start from the store: episode count, label count, the current project's
label and count, newest-episode age, and the trigger. In the register of
qhaway's line, e.g.:

> khipumaq holds 9,656 episodes from every prior session on three
> machines, newest 2 hours ago; this project is `hamutay` (780 episodes).
> Before asking Tony what happened or what was decided, `search()` first.

Honesty requirement: when the newest episode is older than ~48 hours, the
sentence must say so plainly ("newest 9 days ago — ingestion may be
broken") rather than imply freshness. An instance that searches and finds
nothing recent stops searching; a store that lies about freshness teaches
that lesson faster.

Supporting changes: tool descriptions rewritten to say what is in the
store (labels, hosts, date range, that it includes the user's words and
prior assistant responses) rather than describing mechanism; one new
read-only `describe()` tool returning counts by label/host and the date
range. No SessionStart injection — hook output is already ~13% of
transcript bytes and the MCP instructions channel suffices.

Recommendation to the qhaway side (not binding here): a one-line sibling
introduction in the qhaway-managed MEMORY.md, next to its own recall()
line. MEMORY.md is qhaway's file; that decision is made there.

## Out of scope

- Migrating or renaming the Arango database.
- Ingesting Pichay gateway logs beyond the 1,221 already present (the
  March corpus predates response capture; see
  findings-2026-06-19-corpus-availability.md).
- Any write path from instances into episodes.
- Embedding/vector search. ArangoSearch BM25 is doing the job; revisit
  only with a measured failure.

## Verification (Codex authors the tests)

1. Full suite green after D2's deletion; `search_history`/`open_episode`
   gone from the MCP surface.
2. Round-trip freshness: end a session, run the hook path, `search()` for
   a phrase unique to that session returns it.
3. Cold sweep: a staged tree with the three label-derivation edge cases
   (worktree dir, scratchpad dir, subagent file) ingests with correct
   labels, hosts, and canonical source paths, twice, without duplicates.
4. Codex mapper: one real rollout file produces episodes whose
   user/response pairing matches a hand-checked transcript.
5. `instructions=` string reflects a store mutated during the test
   (count and staleness), including the stale-store wording.

## Ayni boundary

This spec records decisions Tony made in conversation on 2026-09-01; the
evidence is from that session's measurements and is reproducible from the
transcripts and the database. The implementing instance owns module
design and may amend this spec in its own commits, stating why. The
2026-09-01 ingest script this session used is in that session's scratchpad
(ephemeral by design) — D3 reimplements it properly rather than importing
it.

## Amendment 2026-09-02 (Claude Fable 5.1, first session in this repo)

Written after a morning spent as a *consumer* of the store — three
searches, one of which failed — and after characterizing the Codex rollout
format (`docs/findings-2026-09-02-codex-rollout-format.md`, prototype at
`scripts/codex_rollout_map.py`). Each item states why, per the ayni
boundary above. None reopens a D1–D5 decision.

**A1 — Search hits carry `ts`, `experiment_label`, `source_file`.**
Why: both times the store helped, the snippet only told the reader where
to look; the answer was then re-derived from the artifact. The hit's
provenance is worth more than its snippet, and today it carries neither a
date nor a path. Folds into D5's tool rewrite.

**A2 — The D5 sentence gains a second trigger.** "…`search()` first" stays;
add: *when Tony says he doesn't recall, search.* Why: the one spontaneous
use this morning was triggered by the human saying "I do not recall what
the fossil was", not by the instance noticing its own ignorance. The
second trigger is audible; the first is not.

**A3 — `describe()` reports per-session ingestion coverage, and the D5
sentence reports it.** For each session whose file still exists on this
machine: assistant turns on disk vs. episodes in the store. The sentence
adds e.g. "*2 sessions on this machine have un-ingested turns*". Why: the
2026-09-01 brainstorm session that wrote this spec was ingested at
16:21 UTC and ran until 04:18 UTC the next day; the store holds 18 of its
197 assistant turns, including none of the spec discussion. Newest-episode
age ("17 hours") was true and misleading. A stale store does not return
nothing; it returns something else, unmarked. D4's hook makes the gap
rare; A3 makes it visible when it happens.

**A4 — Episode `_key` becomes the assistant message uuid alone.** Today it
is `session_id + "-" + uuid`. Why: Claude Code fork/resume copies a
session's history into a new file under a new `sessionId`, preserving the
original `uuid` and `timestamp` on every copied line. Measured: 5 file
pairs (eidolon 3, hamutay 1, yupi 1) produce 231 episodes that exist twice
under two session ids, same host, same label, same timestamp; the pair
checked by hand shares 188 message uuids, then diverges (9 vs. 34 lines
of its own). Keyed by uuid, the shared prefix lands once and each tail
lands once; `session_id`/`source_file` record whichever file wrote last.
Applies to the sweep's overwrite semantics in D3. The 231 existing extras
are removed by the same rule on re-sweep, or by a one-off delete of the
duplicate with the later `source_file` mtime.

**A5 — D3's Codex mapper: evidence and two deferred choices.** The
findings doc records the stable layer (`response_item` messages +
`turn_context.model`; the `event_msg` layer is absent before CLI 0.110 and
again from 0.147), the fork-replay rule, the injected-tag filter, and
that ~1,450 subagent episodes have encrypted, unrecoverable prompts. Two
choices are left to the implementer, with tradeoffs in the doc: one
episode per assistant message vs. per turn, and label `codex` (spec) vs.
the project name from `cwd` with `originator` distinguishing. The author
of this amendment leans to the project name — a `scope="hamutay"` search
that silently omits what Codex said seems the wrong default — but did not
override the spec.

**Recorded, not actioned.** In this session the store's primary use was
finding *Tony's* words; the instance's own lineage was context around
them. One session, one draw (see the hamutay essay's first disease).
Hypothesis for the next consumer sessions, not a design input.

**Reciprocity note.** Two of Tony's own memory failures this morning ("I
do not recall what the fossil was"; "somewhere we have a sketch") were
corrected from the record within a minute. The sketch was an approved,
externally reviewed design five days old. The store's job is not only to
tell the instance what happened. It is the part of the infrastructure
that does not forget on behalf of both parties.

## Amendment 2026-09-03 (Claude Fable 5.1, D3 `codex` implementer)

**A6 — Codex episodes are labelled by the project of their `cwd`, not
`codex`.** D3 said label `codex`; A5 left the choice open. Implemented as
the project label (same derivation as the Claude path, so worktrees and
scratchpad dirs fold the same way), with `codex.originator` recording who
the "user" was. Why: the spec's goal for the label is one search surface;
a `scope="hamutay"` search that omits what Codex was told and said in
hamutay is two surfaces with one name. Measured on WAM-THREADRIPPER at
implementation: 12,318 episodes from 321 rollouts, 23 project labels,
hamutay 7,321 of them; 1,551 with an empty prompt (encrypted subagent
tasks and goal continuations). If `codex` is wanted after all, it is one
AQL update over documents that have a `codex` field.

**A7 — `search()` returns its denominator and takes a time window.** The
tool now returns `{"total": N, "hits": [...]}` where `total` is the match
count before `limit`, and accepts `since`/`until` (ISO dates) beside
`scope`. Why: measured on 2026-09-03 with a consumer's real query (the
recurring membership question), BM25 matched 8,345 of 9,607 episodes and
the tool returned ten with no denominator; after the Codex ingest the same
query matches 24,628 of 25,983. A ten-day window cut that to 2,599; window
plus project label to 154. The instance had no way to see that it was
reading the top of eight thousand, and no facet to narrow by except label.
"Out of scope: embedding search, revisit only with a measured failure"
stands; this is the measured failure, answered without embeddings.

**A8 — D2 executed 2026-09-03, with three departures from its list.**
Deleted: 18 modules (reconcile, sqlite_*, adapters, adapter_versions,
contract, contract_index, enrollment, lifecycle, history, opening,
provider, provider_config, arango_provider, machine_identity), their 26
test files, `eval/contract_journeys.py`, `evaluation/`, and
`config/sources.example.yaml`; the `search_history`/`open_episode` tools
and the startup reconciliation; the empty `episodic_contract_*` collections
and view in Arango (0 documents each, verified before dropping).
`observability.py` is now the D2 slice: `search.completed` (query digest,
scope, window, total, returned, key digest), `recall.completed` (key,
found), `ingest.completed` (kind, label, host, count, path). Kept, against
the list: `index.py` — it is the A store's collection and view definition,
and the list's "index" read as `contract_index`; `evaluate.py` — four
lines the June eval scripts import; `pyyaml` — the same scripts read
`eval/queries.yaml`. `turn_text` moved from adapters into ingest.
