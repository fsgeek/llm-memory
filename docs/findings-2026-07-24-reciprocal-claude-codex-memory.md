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
- Exact Claude episode reference: `episode://claude-history/NDI4MDM4YjItMDYzYi01ZTUyLTk1MTMtMGM2YjkzNDkwZjlh.ZmI2MTk4MjMtMzE1Yy00MzI2LTg0ZmYtNzViZTUxMmNjNDkw/MQ.MQ.MGQ2ODIzNzgtMTkwMC00ZjU5LThjMmItMzExZDI3N2I3Mzk5.ZTAzZjM0ZThkYTMxMzRjOWNjOWM3MTdiM2NlYzQ1N2ExMTA1N2MzMzJjZDRhMzc0NDNhMTZkYTQ5ZmU2NTFmNw`.
- Claude provenance source ID: `428038b2-063b-5e52-9513-0c6b93490f9a`.

## Privacy and operational evidence

- Recent event names: `enrollment.initialized`, `open.completed`, `provider.initialized`, `reconcile.completed`, `reconcile.started`, `search.completed`, `server.started`, and `server.starting`.
- The recent window contained three `search.completed` and three `open.completed` events, with identifier evidence covering both corpus directions.
- Event-log mode: `0600`.
- No `query`, `snippet`, `body`, `user_message`, `response`, `exception_message`, `password`, `access_token`, or `refresh_token` fields were present in the validated window.
- Fixed diagnostic code: none observed in the validated window.

## Qualitative usefulness

Distinct provenance made each retrieval useful because each model reached an authoritative episode from the other model's enrolled history instead of its own corpus. The successful paired-context checks showed that the retrieved records retained enough conversational structure to provide the other model's perspective while the recorded evidence remained content-free.
