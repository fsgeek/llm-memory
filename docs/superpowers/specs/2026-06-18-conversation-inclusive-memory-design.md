# Conversation-Inclusive Episodic Memory Search — Design

**Date:** 2026-06-18
**Status:** Approved direction (Tony delegated; proceeding to build)
**Project:** `llm-memory` (clean-slate sandbox)

## Problem

LLM instances (Hamut'ay's `taste_open`) reach for memory search and get nothing
useful, then fall back to bash/read on the filesystem. Root cause — **"manufactured
silence"**: the search index covers only the instance's **state object** and
*excludes the conversational content* (the human↔instance dialogue). Empirically,
in `taste_open_20260331_035903.jsonl`, `search_memory` returned 0 results on 9 of
11 calls; every multi-word *phrasal/dialogue* query returned 0, while single-word
state-field terms returned hits. Smoking gun: at cycle 456/457 the instance
searched twice for "recognition enhancement" — a phrase it had itself said —
failed, and the human confirmed in-log "that search is not as robust as it should be."

## Thesis to confirm

If episodes are stored with **both** conversation and state, and both are indexed
via **ArangoSearch**, the instance's reach for memory starts succeeding. We are
*not* using embeddings/RAG — adding them now would be premature collapse.

## Scope & plan

**(3) Replay → MCP, gated.**
- **Phase 1 (this spec):** offline ingest + conversation-inclusive index + replay
  the real failing queries + measure. Confirms the *mechanism*.
- **Phase 2 (gated, sketched):** wrap `store/search/recall` in a thin MCP server;
  dogfood with Claude Code first, then taste_open integration. Confirms the *reach*.
  Only proceeds if Phase 1 confirms.

## Architecture (Phase 1)

Small, single-purpose modules under `llm_memory/`:

| Module | Purpose | Depends on |
|---|---|---|
| `db.py` | Read `config/db-config.ini` (scoped user), return a python-arango `Database`. Fail-stop if unreachable. | python-arango |
| `schema.py` | Episode document shape; flatten `state` → searchable `state_text`. | — |
| `index.py` | Idempotent creation of `episodes` collection + `episodes_search` ArangoSearch view + analyzer. | db, schema |
| `ingest.py` | jsonl → per-cycle documents → `episodes`; ensure view. | db, schema, index |
| `search.py` | `search(query, scope, limit)` → BM25 over the view. | db |
| `eval/queries.yaml` | Labeled `(query → expected cycles, scope)` fixture from real failures. | — |
| `eval/replay.py` | Run fixture, score hits@k, report confirmation vs. baseline. | search |
| `tests/` | pytest encoding the 4 success classes. | all |

## Data model

Collection `episodes`, one document per **cycle** (natural addressable unit;
matches `recall`/`walk` cycle-keying). `_key` = zero-padded cycle string.

Fields (store the record faithfully — premature-collapse forbids trimming):
- `cycle` (int), `ts`, `model`, `experiment_label`, `source_file`
- `user_message` (str — the human turn)
- `response` (str — the instance reply; from `raw_output.response` / `response_text`)
- `state` (object — full state subdocument)
- `state_text` (str — flattened concatenation of state values, for indexing)
- `activity_log` (array — the `state._activity_log` tool trace)

## The fix: conversation-inclusive index

ArangoSearch view `episodes_search` over `episodes`, indexing **`user_message`,
`response`, and `state_text`** with the built-in **`text_en`** analyzer
(tokenize, lowercase, stem, stopwords) for BM25 relevance over multi-word phrasal
queries. This is the *only* structural difference from the failing status quo
(which indexed state fields alone). Analyzer tuning is deferred — start with
`text_en`, measure, only then refine.

## Search

`search(query, scope="all", limit=10)` → AQL over `episodes_search` with
`BM25(doc)` scoring, `SORT score DESC`, `LIMIT limit`. Returns
`[{cycle, matched_field, snippet, score}]`. Signature mirrors the existing
`search_memory(query, scope, limit)` so the eventual swap into Hamut'ay is
drop-in. In the single-corpus sandbox `scope=session` and `scope=all` span the
same set; the parameter is retained for API parity and future multi-session use.

## Data flow

`ingest(jsonl)` → `episodes` + view → `search(query)` → ranked cycles →
`replay(queries.yaml)` → per-query hit/miss + summary
("N of M previously-zero queries now return the originating turn").

## Evaluation (self-grounding)

Ground truth for each failing query = the cycle(s) whose conversational text
**actually contains** the key terms (verifiable by direct scan of the corpus —
the phrase *is* present; the status quo simply didn't index that field).
Success = our index returns those cycle(s) within top-k. Seed queries from the
documented failures (e.g. "recognition enhancement" → cy 455/456;
"clock time on restart"; "bounded autonomy harness new system"; the cy-444
phrasal cluster). Baseline for all: 0 results today.

**Caveat:** the corpus logged only result *summaries* (hit counts + hash), not
returned content — so we score our index against corpus-derived ground truth, not
against the old tool's output. Trustworthy memory-tool slice is cycles ~418–457.

## Error handling

Fail-stop (Indaleko philosophy + project CLAUDE.md). No silent fallbacks, no mock
data. Raise loudly on: DB unreachable, malformed/duplicate records, missing
expected fields. Research correctness > graceful degradation.

## Testing (TDD)

Tests encode the success criterion, red→green:
1. Phrasal query for a known instance utterance returns its cycle.
2. Query of human-introduced topic returns the `user_message` cycle.
3. The actual logged-zero queries now return their originating turn.
4. Recall of `response` text returns real reply content (today: 0 values).

## Definition of done (Phase 1)

A clear majority of the ~9 logged-zero phrasal queries return their originating
turn within top-k. If not → thesis challenged; report and rethink before Phase 2.

## Out of scope (YAGNI / anti-premature-collapse)

Embeddings/vector search; yanantin's authored-tensor / declared-loss machinery;
multi-session/multi-tenant; storage optimization; analyzer tuning; the MCP server
(Phase 2). Add only when a measured need appears.
