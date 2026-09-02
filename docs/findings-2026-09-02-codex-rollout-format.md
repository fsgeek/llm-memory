# Findings: Codex CLI rollout format (2026-09-02)

Characterization of `~/.codex/sessions/**/rollout-*.jsonl` on WAM-THREADRIPPER,
in service of khipumaq spec D3 ("First task: characterize the rollout format").
Measured over all 316 files, 2025-09-19 to 2026-08-29, 29 distinct `cli_version`
values from 0.0.0 to 0.151.0. Prototype mapper: `scripts/codex_rollout_map.py`.

## Shape

One JSON object per line, each with `timestamp`, `type`, `payload`. Top-level
types and counts across the corpus:

| type | count | role |
|---|---|---|
| `response_item` | 92,545 | the model-visible conversation: `message` (role developer/user/assistant), `function_call`/`_output`, `custom_tool_call`/`_output`, `reasoning` (encrypted), `agent_message` (inter-agent, encrypted), `web_search_call`, `tool_search_call`, `ghost_snapshot` |
| `event_msg` | 59,489 | UI events: `token_count`, `agent_message`, `user_message`, `task_started`/`task_complete` (turn brackets), `item_completed`, `patch_apply_end`, `context_compacted`, `turn_aborted`, `thread_rolled_back`, ... |
| `turn_context` | 2,733 | one per turn: `model`, `cwd`, `turn_id`, sandbox/approval policy, `current_date` |
| `inter_agent_communication_metadata` | 601 | multi-agent only |
| `session_meta` | 317 | first line of every file (one file has two; see Forks) |
| `world_state` | 226 | 0.150+ only; environment snapshot |
| `compacted` | 103 | compaction summary with `replacement_history` |

`session_meta.payload` carries `id`, `timestamp`, `cwd`, `originator`,
`cli_version`, `source`, `git` (commit hash etc.), and in subagent threads
`thread_source: "subagent"`, `agent_nickname`, and `forked_from_id`.

## Who is the "user"

`originator` across the 316 files: `codex_exec` 128, `codex-tui` 127,
`Claude Code` 35, `codex_cli_rs` 26. `source`: `exec` 127, `cli` 60,
`vscode` 35, subagent thread-spawns ~90, unset 5.

So the user-role text in a Codex episode is authored by one of three parties:

1. **Tony**, in `codex-tui` / `codex_cli_rs` sessions.
2. **A Claude instance**, in `Claude Code`-originated sessions (the codex
   plugin) and most `codex_exec` sessions. These prompts typically open with
   `<task>` or "You are the independent test author ...". The code/test
   separation practice is visible here: Claude implements, Codex is handed
   the test-writing task.
3. **A parent Codex agent**, in subagent threads (nicknames like Helmholtz,
   Ptolemy, Noether). The task arrives as a `response_item` of type
   `agent_message` whose payload is `encrypted_content`; only a plaintext
   preamble ("Message Type: NEW_TASK / Task name: /root/task7_reviewer")
   survives. **The prompt text is unrecoverable** for these ~1,450 episodes.

The mapper records `originator`, `thread_source`, `agent_nickname`, and
`cwd` per episode so a reader can tell which case they are looking at.

## Version drift: what is stable and what is not

| layer | present in | verdict |
|---|---|---|
| `response_item` `message` with role user/assistant | every version | **use this** |
| `turn_context.model` | every version | **use this** for `model` |
| `event_msg` `agent_message` with `phase: final_answer` | 0.110 to 0.149 only; no `phase` before 0.110; the `agent_message`/`user_message` events are **absent in 0.147, 0.148, 0.150.1, 0.151.0** | do not depend on it |
| `payload.id` on message items (`msg_...`) | 0.135+ roughly; absent on 8,528 of 12,275 mapped messages | key by `id` when present, else by line number |

A mapper written against the `event_msg` layer (tempting, because
`final_answer` is exactly the "response" one wants) would silently miss the
45 newest files. The `response_item` message stream is the equivalent of
Claude Code's `type: user`/`type: assistant` lines and is the right mirror.

## Injected user-role messages

The harness injects context as user-role messages. Observed leading tags on
user messages, with counts: `environment_context` 257,
`codex_internal_context` 127, `recommended_plugins` 98, `task` 18 (a real
prompt, from Claude), `turn_aborted` 8, `user_instructions` 3, plus singletons.
Two untagged injections: messages starting `# AGENTS.md instructions for`
(44) and `Warning: apply_patch was requested via` (15). The mapper treats
these as not-a-prompt and keeps `<task>` and everything else.

`<codex_internal_context source="goal">` is the thread-goal continuation:
Codex resumes work toward a stored goal with no human prompt. Episodes that
follow it have an empty `user_message`. That is accurate: nobody said
anything. 97 such episodes, all on 0.139 and 0.141.

## Forks

One file (2026-06-01, `forked_from_id` set, nickname Helmholtz) carries two
`session_meta` lines: its own first, the parent's second. It then replays 93
rows of the parent's history, all stamped within 0.1s of the fork timestamp,
before a 6.5s gap and the first live `turn_context`. Those rows are already
episodes of the parent session (verified by text match). The mapper skips
everything in a forked file until the first `turn_context` more than one
second after the fork's own timestamp. Effect: 47 episodes become 18.

`compacted.replacement_history` likewise replays prior prompts. The mapper
never reads it.

## Results of the prototype sweep

- 12,275 episodes from 292 of 316 files; keys unique; 9 same-session
  duplicate response texts (short repeats like "Done.").
- 24 files yield nothing: 5 under 1 KB (empty sessions), a cluster at
  6.7 KB (AGENTS.md + environment context, no prompt, no response), and a
  handful with a prompt and no assistant text (aborted before reply).
- Models: gpt-5.5 7,549; gpt-5.6-sol 3,386; gpt-5.3-codex 1,088;
  gpt-5-codex 105; gpt-5.2 84; gpt-5.4 43; gpt-5.6-terra 41; gpt-5.6-luna 7.
- By `cwd`: hamutay 7,320; qhaway 2,042; yanantin 894; pichay 426;
  arbiter 403; yupi 354. Label derivation for D3 can use `cwd` directly,
  with the same worktree folding as the Claude path
  (`hamutay/.worktrees/turboquant-r1` appears 5 times).
- Hand check (spec verification 4): the 2026-06-27 yanantin file walked by
  hand has assistant prose at lines 9, 19, 33, 36, 43, 51, 59, all following
  the `<task>` prompt at line 5. The mapper yields exactly those seven,
  each paired with that prompt.

## Decisions left to the D3 implementer

- **One episode per assistant message, or per turn?** The prototype mirrors
  the Claude mapper: every prose assistant message is an episode, so a
  turn's commentary ("I'll read the collector first...") lands as separate
  episodes alongside its final answer. That is consistent with how Claude
  sessions are already stored. Collapsing to one episode per `turn_context`
  would cut volume roughly 3x and lose the commentary.
- **Subagent episodes** have no recoverable prompt. Keep them (they are the
  only record of what the named subagents said) but expect empty
  `user_message`.
- **`experiment_label`.** Spec D3 says `codex`. `cwd` is available for a
  project label instead, which would put Claude and Codex episodes for the
  same project under one label with `originator` distinguishing them. The
  spec's stated goal for the label (one search surface, provenance by
  fields) is met either way; the choice is whether `scope="hamutay"` should
  return Codex turns.
