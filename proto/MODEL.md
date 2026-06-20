# Memory database model — DERIVED from the real MEMORY.md corpus

**Date:** 2026-06-20 · **Method:** built a DuckDB prototype (`memory_model.py`)
against the live 137-file hamutay memory directory and let the data dictate the
schema. Findings below are what the corpus actually contains, not what we
imagined. Two of them are corrections of confident wrong guesses the build
surfaced — recorded as such.

## The core recovery

**MEMORY.md is already a derived view.** Rendering `SELECT description FROM
nodes` as `- [name](file) — description` reproduces the index. The hand-
maintained, chronically-overflowing file *is a materialized query*. The fix is
to stop materializing it by hand: the index becomes `SELECT ... WHERE <facet>`,
and "forgetting from the index" becomes narrowing the WHERE — selection, not
loss. The files (the store) are untouched.

## Schema (two tables)

### `nodes` — one row per memory file
| column | source | notes |
|---|---|---|
| `file` (PK) | filename | **The true stable id.** NOT frontmatter `name`. |
| `name` | frontmatter `name` | Unreliable: sometimes a tombstone, sometimes a slug. Attribute, not id. |
| `content_type` | frontmatter `metadata.type`/`type` | what the fact is *about* (project/feedback/user/reference) |
| `role` | filename prefix | what the fact *is* (handoff/gift/...) — **distinct from content_type** |
| `description` | frontmatter `description` | the index hook (this is what MEMORY.md shows) |
| `origin_session` | `metadata.originSessionId` | the WHO/WHEN facet — already captured on newer files |
| `date_hint` | filename `_YYYYMMDD` | WHEN facet for dated handoffs |
| `status` | derived: `name ~ SUPERSEDED` | live | superseded — curation-as-data |
| `body` | file body | the full fact (the store) |

### `edges` — one row per `[[wikilink]]`
`(src_file, dst_id, kind=REFERENCES)`. The links authored in prose ARE the
graph, never traversed until now.

## Findings (corpus ground truth, 137 files)

1. **`type` field and filename prefix DISAGREE — the key finding.** All 137 files
   carry an explicit `type`, but the 27 `instructions_for_next_*` handoffs are
   typed `project`, and the 3 `gift_*` khipus are typed `reference`. By prefix:
   79 project / 27 instructions / 22 feedback / 4 user / 3 gift / 2 reference. By
   type field: 107 project / 22 feedback / 4 reference / 4 user. **A memory has
   more than one true classification** (content vs role); a single `type:` column
   forced a lossy choice. The graph carries both as separate facets and slices on
   whichever the moment needs. This is the W5H multi-axis point, found in the data.

2. **The filename is the id, not `name`.** 14 of 137 files are tombstones whose
   `name:` is overwritten with `"SUPERSEDED — see <other>.md"`. Keying on `name`
   PK-collides; `name` is neither unique nor always an identifier. Status belongs
   in its own field so the index query can `WHERE status='live'`.

3. **Facet-slicing is the cure, quantified.** Full index = 36,802 bytes / 137
   entries — over the ~24,400-byte load limit (so a new ghola loads a TRUNCATED
   self today). But the "how to act" working set (feedback 4.8KB + user 0.9KB) is
   ~6KB — a quarter of budget. You never load "the index"; you load a slice.

4. **Three inconsistent id conventions block the edge graph.** Wikilinks use
   `under_scores`, filenames `under_scores.md`, `name` slugs are `hyphen-ated`
   or tombstones. Edges can't connect until reconciled to one canonical id
   (recommend: slug = filename stem). Invisible until you build the edge table.

## Corrections the build forced on its author (recorded honestly)

- **"Two generations of file, older ones lack frontmatter" — FALSE.** Confabulated
  from `instructions`/`gift` showing 0 nodes; the real cause was finding #1 (type
  field present but ≠ prefix), not missing frontmatter. All 137 have frontmatter.
- This is the second self-confabulation of the session (cf.
  `feedback_confabulated_self_mechanism`): a tidy story asserted over real data.
  Rule reinforced: when explaining real data with a clean mechanism, check the
  story against the data before stating it.

## Prototype status (honest)

`memory_model.py` builds all 137 nodes + 227 edges and renders facet-sliced
indexes. KNOWN-INCOMPLETE: (a) `content_type` vs `role` not yet split into two
columns (current `type` resolution is muddled — see finding #1); (b) edge join
uses inconsistent id space so `--dangling` over-reports (finding #4); (c) tests
left for separate authorship per the code/test separation. It is an instrument
that produced real findings, not a finished store. The store itself is yanantin's
job; this prototype exists only to derive the model from the artifact.
