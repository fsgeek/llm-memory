# khipumaq

*Quechua: one who makes a khipu, the knotted-cord record.* The name states the
job: keep the record, so it can be read back.

`khipumaq` keeps every Claude Code and Codex session you have had — your words
and the assistant's responses, not summaries — in one searchable store, and
gives the instances that come after a tool to search it.

## The problem

A coding agent starts every session without its past. What was decided last
week, what you already explained, what a prior instance got wrong: all of it
is in transcripts on disk, and none of it is reachable from inside a session.
The agent asks you again, or guesses, or searches the filesystem and finds
nothing. The information exists; the path to it does not. And Claude Code
deletes transcripts after about 30 days, so the record does not even wait.

## What it does

- **Ingests** Claude Code transcripts (including subagents and Cowork tasks)
  and Codex CLI rollouts into an ArangoDB collection: one episode per
  assistant turn, paired with the user turn before it, labelled by project,
  with the host, machine id, and source path it came from.
- **Stays current** without you: a SessionEnd hook for Claude Code and Codex
  ingests each session as it closes, and a daily sweep catches anything a hook
  missed. On WSL the sweep also reads the Windows side (Claude Code, Cowork,
  Codex on Windows) through `/mnt/c`.
- **Serves** it read-only over MCP: `search` (BM25 over both sides of the
  conversation, filterable by project and date, returning how many matched),
  `recall` (one whole episode), and `describe` (what the store holds). At
  start the server says one sentence about itself — how much it holds, how
  fresh it is, and when to reach for it — and says plainly when ingestion
  looks stale.

There is no write tool: episodes come only from the transcripts, and nothing
the server offers can change them. That is not tamper-proofing. An instance
with a shell can read the database password in your config file, or edit its
own transcript before the session ends, and the next ingest will store what
it finds.

## Install

khipumaq runs on Linux and on WSL; it uses systemd, `/etc/machine-id`, and
`fcntl`, so macOS and native Windows are not supported. You need Python 3.11
or newer, [uv](https://docs.astral.sh/uv/), and an ArangoDB 3.12
server you control (a local container is fine) with a database and a user
that can read and write it. khipumaq creates its collection and search view on
first use.

1. Write `~/.config/khipumaq/db-config.ini` (mode 0600):

   ```ini
   [database]
   host = 127.0.0.1
   port = 8529
   database = khipumaq
   user_name = khipumaq
   user_password = ...
   ```

   `$KHIPUMAQ_CONFIG` points elsewhere if you prefer. An optional
   `[khipumaq]` section with `person = <your name>` lets the server's
   sentence name you ("Before asking <name> what happened…"); the default
   is "the user".

2. Wire this machine:

   ```sh
   uvx khipumaq install
   uvx khipumaq sweep      # first run ingests everything on disk
   ```

   `install` adds the Claude Code SessionEnd hook, the MCP server entry, the
   Codex hooks (and the trust Codex requires before running them), a daily
   systemd user timer for the sweep, and — on WSL — the MCP entry for Claude
   Code and Claude Desktop on Windows. `khipumaq uninstall` removes all of it
   and leaves the store alone.

   Codex asks before running a new hook; `install` records its own hooks as
   trusted, so running `install` is that approval. The hooks and the server
   are pinned to the version you installed, so a new release never runs on
   your transcripts until you choose it: `uvx khipumaq@latest install`.

3. Restart Claude Code. Run `install` on each machine whose sessions should
   land in the store; they can all point at one database.

Remote or hosted ArangoDB (ArangoDB Cloud, a server across a WAN) should work
through the same config file but is **not tested**; pull requests welcome.

## Privacy

The store holds your conversations in full. It lives where you put it and
nowhere else: khipumaq sends nothing to any service but your database. Its
operational event log (`~/.local/state/llm-memory/events.jsonl`) records
identifiers, digests, and counts, never query text or episode content;
queries are digested with a random key kept on your machine, so a digest
cannot be reversed by hashing guesses. The store itself also keeps how it
is used: each search's text, window, match count and returned keys, and
which hit was opened afterwards, in a `queries` collection beside the
episodes, so search can be improved from real use. The tools never search
it. Point it at a database on a network you trust.

## Status

Pre-alpha, and used daily. Design notes and the record of how it got here are
in the [design spec](https://github.com/fsgeek/llm-memory/blob/main/docs/superpowers/specs/2026-09-01-khipumaq-design.md).

khipumaq was written by Claude (Anthropic) in Claude Code, with its tests
written separately by Codex (OpenAI), under the direction of its human
author, who decided what it should be. The commit history records which
hand wrote what.

## License

MIT. Contributions are accepted under the Developer Certificate of Origin; see
[CONTRIBUTING.md](https://github.com/fsgeek/llm-memory/blob/main/CONTRIBUTING.md).
