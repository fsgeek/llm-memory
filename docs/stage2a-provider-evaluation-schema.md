# Stage 2A Provider Evaluation Schema

This public envelope records synthetic provider mechanics only. Its
`source_basis` is exactly `synthetic_only`, and its decision is exactly
`phase_a_checkpoint_only`.

Phase A proves mechanics only. It does not prove rationale-recovery usefulness,
does not authorize Phase B, and does not authorize inspection of real tool or
conversation histories. Phase B remains unauthorized until a separate
real-source manifest is reviewed and approved.

## Provider Independence

The `providers` object contains separate `arango` and `sqlite` records produced
from separately constructed, injected providers. A failure in one record never
selects, falls back to, suppresses, or changes the other record. The envelope
contains no aggregate score and names no backend winner.

Provider records retain their own provider and implementation version,
strategy, analyzer or tokenizer, indexed fields, match semantics, public score
ordering, and raw score polarity. Arango BM25 and SQLite FTS5 BM25 scores are
provider-local observations. Their magnitudes are not comparable and are not
included in this public Phase A report.

## Top-Level Fields

- `stage`: exactly `2A`.
- `contract_version`: exactly `1`.
- `source_basis`: exactly `synthetic_only`.
- `phase_a_scope`: declares `mechanics_only=true`,
  `rationale_usefulness_proven=false`, and `phase_b_authorized=false`.
- `providers`: exactly one independent `arango` record and one independent
  `sqlite` record.
- `decision`: exactly `phase_a_checkpoint_only`.

## Provider Fields

Each provider record declares:

- `standing`: independent availability for this run.
- `retrieval_basis`: the frozen provider/version/strategy/analyzer/indexed-field
  and match/polarity declaration.
- `schema_readiness`: the provider's own schema or index standing.
- `source_bytes`: authoritative adapter reads charged through work budgets,
  reported separately from provider database work.
- `database_work`: an explicit `not_measured` standing because the provider
  contract does not expose defensible database-work units separately from
  source and inclusive elapsed work.
- `elapsed`: inclusive monotonic wall-clock milliseconds and its basis.
- `search_totals`: opaque query tokens with returned counts and independent
  `exact`, `unknown`, or `unavailable` population standing.
- `derived_state_counts`: allowlisted provider-measurement document or row
  counts and their standing.
- `derived_physical_bytes`: SQLite database, WAL, and SHM byte observations with
  separate artifact standings, or explicit unavailability. Arango serialized
  document sizes are not represented as physical disk use.
- `lock_or_outage`: whether a provider operation raised the classified,
  retryable `ProviderUnavailable` error and the content-free operation name.
  Other malformed evidence can make the provider unavailable without claiming
  lock or outage evidence. Provider exception text is excluded.
- `purge`: state-class count slots and an explicit standing.
- `rebuild`: an explicit standing and measurement basis.
- `full_removal`: residual count, declared losses, basis, and standing.

The runner does not have proof that an injected provider is disposable. It
therefore does not call `purge()` or `remove_all()`. Purge, rebuild, residual,
and removal-execution evidence remains explicitly unavailable. Known losses
declared by each provider contract remain visible as fixed opaque tokens. In
particular, configured or shared Arango state is never passed to `remove_all()`.
A later owned-disposable evaluation may collect destructive evidence outside
this public runner and must retain its unavailable standing when safe ownership
cannot be established.

## Privacy Boundary

The public schema admits only frozen descriptors, opaque query tokens, counts,
standings, booleans, and measurements. It excludes:

- source or conversation prose;
- raw query text;
- paths and project names;
- credentials and identifying ports;
- raw episode references and provider result identifiers;
- participant prompts or evidence-bearing outputs; and
- values declared through `Stage2AExperiment.private_values`.

Provider search results are reduced to returned counts and exactness standing;
snippets, scores, references, identities, and query echoes do not cross the
boundary. The complete public schema and all declared private values are
validated before a report is returned.

`write_report_atomic()` validates the complete schema before serialization or
temporary-file creation. It writes a same-directory temporary file, flushes and
fsyncs it, and then atomically replaces the target. Validation and write
failures leave no partial target or temporary report content.
