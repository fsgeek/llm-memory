# Task 10 Report: Portable Provider Contract and Concurrency Fixtures

## Status

Implemented and verified at implementation commit `0b85164` (`test: verify portable episodic providers`).

## RED / GREEN

### Malformed NUL query

- RED: `test_sqlite_malformed_nul_query_fails_as_contract_error` failed with raw `sqlite3.OperationalError: unterminated string` from SQLite FTS5.
- GREEN: `SearchRequest.create()` now rejects NUL with `ContractError("query must not contain NUL")` before either provider executes a query.
- Focused verification: `49 passed` across the new regression and `tests/test_contract.py`.

### Portable contract

- Initial RED found two fixture assumptions, not provider defects: SQLite's declared OR-segment semantics matched a shared word in the sibling source, and the first rewrite increased source length and therefore resembled append behavior to Arango.
- The fixture now uses a source-unique authorization marker and an equal-length prefix rewrite. No provider retrieval semantics were changed.
- GREEN: the same shared public obligations pass independently against `ArangoProvider` and `SQLiteProvider`.
- Final portable/concurrency verification: `6 passed in 3.53s`.

## Covered Obligations

- Identity preservation and source-backed exact opening.
- Nested corpus/source/member standing.
- Bounded reconciliation and resume to current.
- Exact-or-unknown population reporting.
- Deterministic bounded results without cross-provider score comparison.
- Equal-length rewrite supersession.
- Disabled declaration plus enabled declarations in one corpus, reenable, and unenroll authorization revocation with retained derived data.
- Available-empty source and multiple enabled sources in one corpus.
- Scoped state-class purge, sentinel-scope isolation, rebuild, and full provider removal.
- Explicit selection of each provider's sole declared strategy and rejection of the other provider's strategy; no fallback.
- Scoped and unavailable Arango measurements.
- Synthetic source bytes checked after provider operations and full removal.

## SQLite Concurrency

The concurrency fixture uses eight threads, eight explicitly separate `SQLiteStore.connect()` handles, independently constructed providers, and a barrier start. Outcomes are limited to `available` or retryable `ProviderUnavailable`; a final reconciliation verifies exact active references and document/FTS population for every active generation.

Repeated command output:

```text
run 1: 3 passed in 0.38s
run 2: 3 passed in 0.39s
run 3: 3 passed in 0.36s
run 4: 3 passed in 0.37s
run 5: 3 passed in 0.34s
```

The same file also verifies:

- `BEGIN IMMEDIATE` held past a 30 ms busy timeout becomes `ProviderUnavailable`, not raw `sqlite3.OperationalError`.
- A subprocess exits with code 17 after uncommitted staging state and episode inserts; reopening proves state, document, and FTS rows all rolled back before a valid generation is built.

## Verification

- Existing plus portable provider suites: `104 passed in 18.69s`.
- Final full suite: `410 passed in 31.49s` (exit code 0; baseline was 404).
- `git diff --check`: clean before implementation commit.

## Files

- `llm_memory/contract.py`
- `tests/conftest.py`
- `tests/provider_contract.py`
- `tests/test_provider_contract_arango.py`
- `tests/test_provider_contract_sqlite.py`
- `tests/test_sqlite_concurrency.py`

## Self-Review and Concerns

- No provider strategy, analyzer/tokenizer, match semantics, score magnitude, fallback, dependency, private data, or qhaway behavior was normalized or added.
- The only production change is NUL query validation demonstrated by the RED failure.
- The Arango full-removal obligation intentionally removes all provider-owned contract collections and view. The test suite runs serially, and existing Arango fixtures recreate these objects as needed; this test must not be run concurrently against shared non-test work.
- The local commit hook reported that the `ots` client was absent and skipped timestamping; this did not affect tests or the commit.
