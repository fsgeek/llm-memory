# DuckDB Prototype Archival Design

**Status:** approved for implementation

## Decision

The `proto/` DuckDB experiment is an archival research artifact. It preserves
the observations that informed the memory model, but it is not part of the
supported `llm-memory` runtime and is not promised to remain executable.

## Changes

- Remove DuckDB from the project's required dependencies and regenerated lock
  state.
- Add an archival notice to `proto/MODEL.md` and the module docstring in
  `proto/memory_model.py`.
- Preserve the prototype source and its findings without modifying its behavior.

## Boundaries

- Do not replace DuckDB with SQLite inside the prototype.
- Do not add an optional prototype dependency or a separate prototype
  environment.
- Do not change the Arango or SQLite production providers.
- Do not claim that archival code is tested, supported, or currently runnable.

## Verification

- The dependency manifest and lockfile contain no DuckDB package.
- DuckDB references remain only in the explicitly archival `proto/` artifact.
- The complete supported test suite passes without DuckDB as a project
  dependency.

## Declared Loss

A fresh project installation will no longer make `proto/memory_model.py`
directly executable. Reconstructing the historical experiment requires an
independent environment and a compatible DuckDB release. The retained source
and findings preserve the rationale; environment reproducibility is not
preserved.
