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

**A9 — D4 layer 1 for Codex: a native Codex hook, not a wrapper.** Codex
CLI 0.151 has a hooks system (`$CODEX_HOME/hooks.json`, Claude Code's
shape; events include SessionEnd and SubagentStop; the hook JSON carries
`transcript_path` / `agent_transcript_path`, `session_id`, `cwd`). Measured
2026-09-03 in an isolated CODEX_HOME: (1) hooks run only when trusted;
trust is persisted as `[hooks.state."<hooks.json path>:<event>:<i>:<j>"]
trusted_hash = "<sha256 from hooks/list>"` in config.toml, which
`scripts/install-codex-hook.sh` writes by asking `codex app-server` for
`hooks/list`; (2) end-of-session hooks are capped at ~1 s and killed at
exit, and the JSON `timeout`/`async` fields are not honored, so
`scripts/codex-hook-ingest.sh` reads the JSON and detaches the ingest with
setsid — a detached `sleep 5` completed after Codex exited, a same-process
one did not; (3) the largest local rollout (62 MB, 5,296 episodes) parses
in under a second but its writes take longer than the cap, which is why
the detach is not optional. `ingest codex` with no path reads the hook
JSON from stdin, mirroring `claude-session`. A wrapper around the `codex`
binary was rejected: the VS Code extension bundles its own binary (35 local
rollouts), and a wrapper cannot learn the transcript path. The nightly
sweep (D4 layer 2) remains the retry for hook failures.

**A10 — Backup and warm replica (2026-09-04/05).** Not in the original
spec; added because the store was the only copy of thousands of episodes
and lived in one Docker container on one Windows host with no backup. The
primary is ArangoDB 3.12.4 community, container `arango-indaleko-20240118170759`
on WAM-THREADRIPPER's Docker Desktop (host 192.168.111.127); the parallel
container `arango-vector-sandbox` (enterprise 3.12.9, vector index flag) is
now `restart=always` like the original. wam-nuc runs a native ArangoDB
3.12.10 enterprise (`arangodb3` service); its `arangod.conf` had both
`tcp://0.0.0.0:8529` and `tcp://[::]:8529` endpoints, which made the
second bind fail and the service crash-loop — the IPv4 line is commented
out and the web UI is reachable on the LAN. `scripts/backup-store.sh`
(systemd user timer on wam-nuc, 03:30 UTC, installed by
`scripts/install-backup-timer.sh`): arangodump the primary into
`~/backups/llm-memory/<date>` (29 MB compressed, ~5 s), arangorestore into
wam-nuc's own `llm_memory` database (warm replica and intended future
primary), verify counts, restic to `sftp:activitycontext.work:backups/llm-memory-restic`
with 30 daily / 24 monthly retention. The restic password file is
`~/.config/llm-memory/restic-password` on wam-nuc only (0600); it must be
copied somewhere safe. First run 2026-09-05 00:55 UTC: 26,117 episodes,
primary == replica, snapshot pushed. Cutover to wam-nuc as primary is the
`host` line in `config/db-config.ini` on three machines.

## Amendment 2026-09-23 (Claude Opus 5.5, packaging)

Tony asked to take the project toward PyPI the way qhaway went. Decisions
from that conversation, and what was built on it.

**A11 — D1 executed as packaging.** `llm-memory` is taken on PyPI;
`khipumaq` is free. The package is `src/khipumaq` (uv_build); runtime
dependencies are `mcp` and `python-arango` only, and the wheel carries code
only — no config, no data. The database, its user, and the event-log path
keep their `llm_memory` names (D1 already said so for the database).

**A12 — Config leaves the source tree.** `db-config.ini` is read from
`$KHIPUMAQ_CONFIG` (exclusively), else `~/.config/khipumaq/`, else the
checkout's `config/` — the last so machines running from a checkout keep
working unchanged.

**A13 — `khipumaq install` replaces the install scripts.** One command
writes the Claude SessionEnd hook, the user-scope MCP entry, the Codex
hooks with their trust, and a sweep timer, and removes the pre-package
`llm_memory` entries. Migrating a checkout machine is: pull, `uv sync`,
`.venv/bin/khipumaq install`. `codex-hook` replaces the setsid script
(returns in ~60 ms against Codex's ~1 s cap).

**A14 — D4 layer 2 is per machine, not a central rsync.** Why: every
machine already writes to the store directly, so each can retry its own
misses; the central design needed ssh between machines and was never
built. `khipumaq sweep` ingests files modified since its last successful
run (less a day), from a daily systemd user timer with `Persistent=true`.
Measured on wam-desktop: full sweep 2,903 episodes in 25 s, incremental
1.4 s.

**Direction agreed, not yet built.** A container (Docker/Podman) holding
ArangoDB, the read-only MCP server over HTTP, and an HTTP ingest endpoint
used only by hooks — so clients need neither the Arango driver nor
credentials, and Windows Claude can reach the store without a checkout.
The ayllu's primary stays on the LAN; LAN reachability is the access
boundary for now (Tony: two humans, one gateway he controls). A PyPI
install starts clean against the user's own ArangoDB; WAN or ArangoDB
Cloud setups are documented as untested, PRs welcome, which requires a
license (MIT, matching qhaway) and DCO sign-off. The ayllu's memories are
never to leave the organization; the package never phones home. Label
folding (raw project dir kept as a field, labels computed from
configurable rules) belongs to the same packaging work.

**Recorded: a stale checkout re-derived A10.** This work began on
wam-desktop from a checkout 19 days and four commits behind `origin/main`.
In conversation I recommended an encrypted off-site backup; A10 had built
one on 2026-09-04, and neither Tony nor I knew. The store had the episodes
that built it; I did not search for them because I did not know there was
anything to search for. A checkout is a snapshot, the same way context is.

**A15 — A working directory below a project folds into the project.**
`label_from_path` (Codex `cwd`, `CLAUDE_PROJECT_DIR`) cuts a path at the
first component under `projects/`: `…/projects/cpsc416/tmp/capstone/<student>`
is `cpsc416`. Why: automated Codex grading runs, one per student, had made
176 labels for 8 projects, and the D5 sentence reported "219 projects". The
full path is still on every Codex episode (`codex.cwd`), so this folds the
search surface without losing anything; `scripts/relabel-codex-by-project.py`
relabelled the 1,077 existing episodes (219 → 45 labels). Claude-session
labels are unchanged: Claude Code's encoded directory name replaces `/`
with `-`, so `projects/foo/bar` and `projects/foo-bar` cannot be told
apart there. No configurable fold rules: the one rule is structural, and
Tony does not expect to repeat the grading runs.

**A16 — The Windows side of a WSL machine is swept through /mnt/c.**
Claude Code on Windows, Claude Desktop's Cowork, and Codex on Windows keep
transcripts under the Windows profile, where no hook of ours runs. On WSL,
`khipumaq sweep` finds the profile by interop and sweeps it, recording the
Windows path as `source_file` and Windows' MachineGuid as `machine_id`.
Cowork tasks are Claude Code transcripts in the same format, one projects
tree per task, labelled `cowork` by location (`audit.jsonl`, an HMAC-signed
duplicate of the SDK stream, is not read). Labels learn the Windows project
roots `source\repos` and `Documents\Claude\Projects`, and the Codex app's
dated scratch directories (`codex`). First run on WAM-THREADRIPPER: 2,861
Cowork episodes (back to 2026-02-12), 1,105 Windows Codex, 159 Windows
Claude Code; 4 m 15 s, mostly /mnt/c I/O. Why a sweep and not a Windows
install: no Windows Python, hooks, or scheduler to maintain; the cost is up
to a day's latency for Windows sessions. `install` on WSL also registers
the server for Claude Code on Windows and Claude Desktop as
`wsl.exe -e <khipumaq> serve` (MCP initialize from Windows: ~2 s). This
covers the read path the container was going to provide for Windows.

**A17 — `install` creates the store on a fresh database.** Nothing in the
package called `ensure_index`; the ayllu's store exists because it was
made in June. Found while writing the README's install section.

**A18 — The person is configuration.** `[khipumaq] person = Tony` in the
config file; default "the user". The trigger became
`when <person> says "I don't recall", search()` — still audible (A2), no
pronoun.

**A19 — Published.** khipumaq 0.1.2 on PyPI (MIT), after TestPyPI and a
clean-room install in an empty HOME, which found that `mcp>=1.28` with no
upper bound resolved mcp 2.x (FastMCP renamed) and the server could not
start; pinned `<2` until a deliberate migration. Tests throughout were
written by Codex (`codex exec`, test-only commits), 155 passing.
Remaining from the agreed direction: the container (ArangoDB + MCP over
HTTP + ingest endpoint) — now for other people's deployments more than the
ayllu's, since A16 gives Windows its read and write paths.

**A20 — A third trigger: before proposing, search.** The sentence now
says "before proposing a design or a fix, search() for it, because it may
already exist." Why: on 2026-09-23 the implementing instance recommended an
encrypted off-site backup that A10 had built three weeks earlier. The store
held every episode of that work; the instance never searched, because it
was not asking anyone anything, only reasoning toward a design from what it
believed. D5's trigger fires on asking and A2's on hearing "I don't
recall"; neither fires on proposing, which is where re-derivation happens.
Testable the way D5 was: a census of spontaneous search() calls made just
before a design or fix is proposed, before and after this line.

## Amendment 2026-09-24 (Claude Opus 5.5, after an external review)

**A21 — A Claude episode's prompt is the last thing a person said, not the
last `type: user` record.** Claude Code writes tool results as `type: user`
records with no text blocks, and the mapper let each one overwrite the prompt
with "". Any prose after a tool call (most prose, in agentic sessions) was
stored with an empty `user_message`. Measured 2026-09-24 in the live store:
12,668 of 17,997 Claude-session episodes (70%). On wam-desktop's transcripts
on disk: 242 of 312 before the fix, 0 after. `isMeta` records, compaction
summaries, and harness output (`<system-reminder>`, `<local-command-stdout>`,
`<bash-stdout>`, `<task-notification>`, …) are skipped too, mirroring the
Codex path's injected-tag filter. What the person typed stays, including
slash commands, `<bash-input>`, and pasted content. The tag list comes from a
census of every `type: user` record shape on disk, not from memory of the
format.

Why it happened: an outside reviewer, reading 0.1.2 from PyPI, found it and
diagnosed the cause. The Codex mapper was built on a findings document from
real rollouts (A5). The Claude mapper was built on the format as the
implementing instance believed it to be. A Claude instance got its own
transcript format wrong because it was familiar and nobody looked. So the
store's founding failure, manufactured silence, was being manufactured by the
store, for the half of the record D5's sentence tells instances to search.

Repair: `khipumaq sweep --all` on each machine rewrites every episode whose
transcript is still on disk (about 30 days). Older episodes, whose sources
are gone, keep the empty prompt unless repaired from the store itself; that
repair is derived, not faithful, and is recorded separately when done.
