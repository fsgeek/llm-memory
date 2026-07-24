# Reciprocal Claude–Codex Memory Findings

Completion date: 2026-07-24 UTC

## Verdict

Successful.

## Enrollment

- `codex-history` enrolled source `e8c598ae-711b-42b5-b963-eb35fc946d2b`; corpus, source, and index standings were `available`; observed episode count: `148`.
- `claude-history` enrolled source `428038b2-063b-5e52-9513-0c6b93490f9a`; corpus, source, and index standings were `available`; observed episode count: `12` across two members.

## Claude-to-Codex proof

- Returned count: `10`.
- Open standing: `available`.
- Exact Codex episode reference: `episode://codex-history/ZThjNTk4YWUtNzExYi00MmI1LWI5NjMtZWIzNWZjOTQ2ZDJi.MDE5ZjhhZjMtODNkYi03OTcyLWFmMTEtMWQ2MzA5YWQzMzky/MQ.MQ.MA.YTM1YTc4NmQyNmZhZDczNTY1YzM5NTcxMmZlNTU3MGNkNTEzMzY2MzY4MmYxNTFhNDI4ZGM2ZGRkZTVhOWM5Yw`.
- Codex provenance source ID: `e8c598ae-711b-42b5-b963-eb35fc946d2b`.

## Codex-to-Claude proof

- Returned count: `10`.
- Open standing: `available`.
- Exact Claude episode reference: `episode://claude-history/NDI4MDM4YjItMDYzYi01ZTUyLTk1MTMtMGM2YjkzNDkwZjlh.YzU5YjQzZTgtZGI2MC00ZmZiLWFiMTctMDVjNTI3MzIwMGYy/MQ.MQ.ZWRmOTYwNzEtMDI5Zi00OTE3LWJlZTMtOWFhMTE4NWNiNTAz.ZWMxMzkwOGU3OWIwYTU3OTNhOGUwM2NhOTlhOGZhNzJhNTUyMzBiMWQ1NDBkYTk2ZDBkNzljZTVkNTgxMzViNA`.
- Claude provenance source ID: `428038b2-063b-5e52-9513-0c6b93490f9a`.

## Privacy and operational evidence

- A byte boundary was captured immediately before each restarted client's explicit search/open rerun. Each resulting bounded suffix contained exactly two complete JSONL records: one `search.completed` and one `open.completed`; the opened reference was copied verbatim from that rerun's first search result.
- Each search record was validated against the closed field set `event`, `ts`, `corpus_ids`, `episode_refs_sha256`, and `returned_count`. Each open record was validated against the closed field set `event`, `ts`, `corpus_ids`, `episode_ref`, and `standing`.
- All values were constrained recursively: corpus lists contained only valid identifiers, timestamps were valid UTC forms, reference digests were lowercase SHA-256 values, counts were nonnegative integers, episode references were canonical and encoded the expected corpus and provenance source, and open standings belonged to the contract enum. Duplicate or extra keys and unconstrained nested values were rejected.
- Both bounded suffixes matched their expected corpus, provenance source, exact available open reference, and direction.
- Event-log mode: `0600`.
- Because validation accepted only those closed schemas, neither suffix could contain query, snippet, episode-body, response, credential, exception-message, or other free-text fields.
- No failure event or fixed diagnostic code occurred in either bounded suffix.

## Qualitative usefulness

Distinct provenance made each retrieval useful because each model reached an authoritative episode from the other model's enrolled history instead of its own corpus. The successful paired-context checks showed that the retrieved records retained enough conversational structure to provide the other model's perspective while the recorded evidence remained content-free.
