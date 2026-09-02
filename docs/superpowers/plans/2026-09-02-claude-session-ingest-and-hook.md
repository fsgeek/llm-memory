# Claude-Session Ingest and SessionEnd Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `claude-session` ingest command, keyed per amendment A4, that a Claude Code `SessionEnd` hook runs so every finished session lands in the episodes store within seconds.

**Architecture:** Extend the existing pure mapper in `llm_memory/ingest.py` (uuid key, host/machine identity, subagent files, label derivation) and add an argparse `main` to the same module so `python -m llm_memory.ingest claude-session` works both with a path and as a stdin-fed hook. A one-off migration script rekeys the existing 9,656 episodes so the new key scheme does not duplicate them. A shell script installs the hook into `~/.claude/settings.json`.

**Tech Stack:** Python 3.14, python-arango, pytest (live ArangoDB at 192.168.111.127:8529 via `config/db-config.ini`, as the existing tests already use), bash, Codex CLI 0.151 for test authoring.

**Spec:** `docs/superpowers/specs/2026-09-01-khipumaq-design.md`, sections D3, D4 (layer 1), and Amendment A4. Companion evidence: `docs/findings-2026-09-02-codex-rollout-format.md` (not implemented here).

## Global Constraints

- **No write tool.** Episodes are written by ingestion of the faithful record only. Nothing in this plan adds an MCP write path.
- **Required episode fields, all paths:** `host` (hostname), `machine_id` (`/etc/machine-id`, 32 hex chars, matching the existing data's un-hyphenated form), canonical `source_file` (the path on the machine of origin, never a staging copy).
- **Label derivation** (spec D3): strip `-home-tony-projects-`; fold `--worktrees-<x>` and `-.worktrees-<x>` into the parent project; map scratchpad-launched dirs (`-tmp-claude-1000-…-<project>-<uuid>-…`) to `<project>`; keep the existing labels `yanantin_construction` and `quantumos`.
- **Subagent files** `<session>/subagents/*.jsonl` are ingested (D3).
- **Episode `_key` is the assistant message uuid alone** (A4).
- **Hook behavior** (D4): latency seconds; fails loudly to stderr when the DB is unreachable; does not retry (the nightly sweep, a later plan, is the retry).
- **Code/test separation** (project rule, Tony): Claude implements; Codex authors the validating tests in its own commit. The one exception is an existing test that asserts superseded behavior; the implementer updates that assertion in the same commit as the behavior change and says so in the message.
- **Package stays `llm_memory`.** D1's rename is a separate change.
- Commit messages end with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`. The post-commit hook stamps every non-`ots:` commit; that is expected.

---

## File Structure

- Modify `llm_memory/ingest.py`
  - `label_from_project_dir(name: str) -> str` — pure, derives the label from a `~/.claude/projects/<name>` directory name.
  - `read_machine_id(path=Path("/etc/machine-id")) -> str` — 32 hex chars.
  - `claude_session_to_episodes(path, experiment_label, host=None, machine_id=None)` — existing generator, changed key and new fields.
  - `claude_session_files(path) -> list[Path]` — the session file plus its `subagents/*.jsonl`.
  - `ingest_claude_session(db, path, experiment_label, dry_run=False, host=None, machine_id=None)` — existing, now walks `claude_session_files`.
  - `main(argv=None) -> int` — argparse CLI; `if __name__ == "__main__"`.
- Modify `tests/test_ingest.py:14-49` — the legacy-shape test's expected `_key` and new fields.
- Create `scripts/migrate-episode-keys.py` — one-off A4 rekey of existing episodes.
- Create `scripts/install-session-end-hook.sh` — idempotent edit of `~/.claude/settings.json`.
- Create (Codex) `tests/test_claude_session_ingest.py`.

---

### Task 1: Label derivation

**Files:**
- Modify: `llm_memory/ingest.py` (add near the top, after imports)

**Interfaces:**
- Produces: `label_from_project_dir(name: str) -> str`

- [ ] **Step 1: Write the smoke check (throwaway, not committed)**

Save as `/tmp/claude-1000/-home-tony-projects-llm-memory/f92629c7-2c77-4577-9cbb-1e8088cd8fe3/scratchpad/label_check.py`:

```python
from llm_memory.ingest import label_from_project_dir as L
cases = {
    "-home-tony-projects-hamutay": "hamutay",
    "-home-tony-projects-hamutay--worktrees-turboquant-r1": "hamutay",
    "-home-tony-projects-hamutay-.worktrees-turboquant-r1": "hamutay",
    "-home-tony-projects-wamason.com-.claude-worktrees-blog-migration": "wamason-com",
    "-home-tony-projects-wamason.com": "wamason-com",
    "-home-tony-projects-wamason-com": "wamason-com",
    "-home-tony-projects-fsgeek.ca": "fsgeek-ca",
    "-home-tony-projects-yanantin": "yanantin_construction",
    "-home-tony-projects-quantumos": "quantumos",
    "-home-tony-projects-llm-memory": "llm-memory",
    "-tmp-claude-1000--home-tony-projects-research-program-182440b0-4275-4b22-884d-6d4ec0594a26-scratchpad-probe": "research-program",
    "-home-tony": "home",
    "-home-tony--codex": "home-codex",
    "-mnt-c-Users-TonyMason-Documents-Claude-Projects-Gig Work-ayllu": "mnt-c-users-tonymason-documents-claude-projects-gig-work-ayllu",
}
bad = {k: (L(k), v) for k, v in cases.items() if L(k) != v}
print("FAIL" if bad else "OK", bad)
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python /tmp/claude-1000/-home-tony-projects-llm-memory/f92629c7-2c77-4577-9cbb-1e8088cd8fe3/scratchpad/label_check.py`
Expected: `ImportError: cannot import name 'label_from_project_dir'`

- [ ] **Step 3: Implement**

Add to `llm_memory/ingest.py` after the imports:

```python
import re
import socket
from pathlib import Path

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Existing labels that differ from the project directory name (spec D3).
_LABEL_OVERRIDES = {"yanantin": "yanantin_construction"}


def label_from_project_dir(name):
    """Derive the experiment label from a `~/.claude/projects/<name>` directory
    name. Strips the projects prefix, folds worktree dirs into their project,
    maps scratchpad-launched dirs to the project they were launched from, and
    normalizes dots to dashes so `wamason.com` and `wamason-com` agree."""
    m = re.search(r"-projects-(.+)$", name)
    if m:
        rest = m.group(1)
        rest = re.split(rf"-{_UUID}", rest)[0]              # scratchpad dirs
        rest = re.split(r"-+\.?(?:claude-)?worktrees-", rest)[0]  # worktrees
        rest = rest.replace(".", "-")
        return _LABEL_OVERRIDES.get(rest, rest)
    m = re.match(r"-home-[^-]+(.*)$", name)
    if m:
        rest = m.group(1).strip("-.").replace(".", "-")
        return f"home-{rest}" if rest else "home"
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
```

- [ ] **Step 4: Run the smoke check**

Run: `uv run python /tmp/claude-1000/-home-tony-projects-llm-memory/f92629c7-2c77-4577-9cbb-1e8088cd8fe3/scratchpad/label_check.py`
Expected: `OK {}`

- [ ] **Step 5: Run the existing suite for the touched module**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: `5 passed`

- [ ] **Step 6: Commit**

```bash
git add llm_memory/ingest.py
git commit -m "feat: derive episode label from Claude project directory name

Spec D3: strip the projects prefix, fold worktrees, map scratchpad dirs
to their project, keep yanantin_construction, normalize dots.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: uuid key and host identity on Claude-session episodes

**Files:**
- Modify: `llm_memory/ingest.py` — `claude_session_to_episodes`, `ingest_claude_session`; add `read_machine_id`
- Modify: `tests/test_ingest.py:14-49` (superseded assertion; the one permitted exception)

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `read_machine_id(path: Path = Path("/etc/machine-id")) -> str`
  - `claude_session_to_episodes(path, experiment_label, host=None, machine_id=None)` yielding dicts whose `_key` is the assistant `uuid`, with `host`, `machine_id`, and `agent_id` (from the record's `agentId`, else `None`).
  - `ingest_claude_session(db, path, experiment_label, dry_run=False, host=None, machine_id=None) -> int`

- [ ] **Step 1: Update the superseded assertion in the existing test**

In `tests/test_ingest.py`, in `test_claude_session_legacy_shape_is_preserved`, change the expected dict:

```python
    assert episode == {
        "_key": "assistant-1",
        "session_id": "session-legacy",
        "ts": "2026-07-12T10:00:01Z",
        "model": "claude-test",
        "experiment_label": "project-history",
        "source_file": str(path),
        "user_message": "question",
        "user_ts": "2026-07-12T10:00:00Z",
        "response": "answer",
        "state": {},
        "state_text": "",
        "activity_log": [],
        "host": None,
        "machine_id": None,
        "agent_id": None,
    }
```

(Keep every other line of the test as it is. Read lines 14-49 first; the keys above must match the file's existing order for the other fields.)

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_ingest.py::test_claude_session_legacy_shape_is_preserved -q`
Expected: FAIL, assertion diff showing `_key: 'session-legacy-assistant-1'` and missing `host`/`machine_id`/`agent_id`.

- [ ] **Step 3: Implement**

In `llm_memory/ingest.py`, add after `label_from_project_dir`:

```python
def read_machine_id(path=Path("/etc/machine-id")):
    """The 32-hex machine id, un-hyphenated, matching the existing episodes."""
    return path.read_text(encoding="utf-8").strip()
```

Change `claude_session_to_episodes`:

```python
def claude_session_to_episodes(path, experiment_label, host=None, machine_id=None):
    """Yield one episode per assistant turn in a Claude Code project JSONL.

    Each line is one event; `type` is `user`/`assistant`/etc. An episode pairs an
    assistant turn (`response`) with the most recent preceding user turn
    (`user_message`), mirroring the gateway mapper. Claude Code has no authored
    state, so state is empty. Keyed by the assistant uuid alone (amendment A4):
    fork/resume copies a session's history into a new file under a new
    sessionId but keeps the uuid, so the uuid is the identity of the message.
    `experiment_label` is caller-supplied so a project's construction history
    partitions distinctly from pichay-captured `claude_code` traffic. `host` and
    `machine_id` are the machine of origin (spec D3); the caller supplies them
    because a staged copy on another machine must not claim to be local."""
    session = "unknown"
    last_user = ""
    last_user_ts = None
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            msg = rec.get("message")
            if not isinstance(msg, dict):
                continue
            session = rec.get("sessionId") or session
            if rtype == "user":
                last_user = _turn_text(msg.get("content"))
                last_user_ts = rec.get("timestamp")
                continue
            if rtype != "assistant":
                continue
            response = _turn_text(msg.get("content"))
            if not response.strip():
                continue  # tool-use-only turn with no prose; skip
            uuid = rec.get("uuid") or ""
            yield {
                "_key": uuid,
                "session_id": session,
                "ts": rec.get("timestamp"),
                "model": msg.get("model"),
                "experiment_label": experiment_label,
                "source_file": str(path),
                "user_message": last_user,
                "user_ts": last_user_ts,
                "response": response,
                "state": {},
                "state_text": "",
                "activity_log": [],
                "host": host,
                "machine_id": machine_id,
                "agent_id": rec.get("agentId"),
            }
```

Change `ingest_claude_session`'s signature and the call inside it:

```python
def ingest_claude_session(db, path, experiment_label, dry_run=False, host=None, machine_id=None):
    """Load one Claude Code project JSONL into the episodes collection. One
    episode per prose assistant turn. Idempotent per assistant uuid. When
    dry_run, counts what WOULD be inserted without writing. Returns the count."""
    col = db.collection(EPISODES)
    count = 0
    for episode in claude_session_to_episodes(path, experiment_label, host=host, machine_id=machine_id):
        if not dry_run:
            col.insert(episode, overwrite=True)
        count += 1
    return count
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: `5 passed`

- [ ] **Step 5: Commit**

```bash
git add llm_memory/ingest.py tests/test_ingest.py
git commit -m "feat: key Claude-session episodes by assistant uuid; carry host and machine_id

Amendment A4: fork/resume copies history under a new sessionId with the
original uuid, producing 231 duplicate episodes across 5 file pairs. The
uuid is the message's identity. Spec D3 requires host and machine_id on
every episode. The legacy-shape test's expected key is updated in this
commit because it asserted the superseded key scheme.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Subagent transcripts

**Files:**
- Modify: `llm_memory/ingest.py` — add `claude_session_files`; `ingest_claude_session` walks it

**Interfaces:**
- Consumes: `claude_session_to_episodes(path, experiment_label, host, machine_id)` from Task 2.
- Produces: `claude_session_files(path: Path) -> list[Path]` — `[path] + sorted(path.with_suffix("") / "subagents" / "*.jsonl")`; `ingest_claude_session` now sums over all of them.

Subagent files live at `<projects>/<project>/<session-uuid>/subagents/agent-<id>.jsonl`, carry the same `type`/`uuid`/`sessionId`/`timestamp`/`message` fields as the parent, plus `agentId` and `isSidechain: true`. Their `sessionId` is the parent session's id. 52 such files exist on WAM-THREADRIPPER today and are currently skipped.

- [ ] **Step 1: Write the smoke check (throwaway)**

Save as `.../scratchpad/subagent_check.py`:

```python
import json
from pathlib import Path
from llm_memory.ingest import claude_session_files, claude_session_to_episodes
root = Path("/home/tony/.claude/projects")
parent = next(p for p in root.glob("*/*.jsonl") if (p.with_suffix("") / "subagents").is_dir())
files = claude_session_files(parent)
print(len(files), "files;", files[0].name, "+", [f.name for f in files[1:3]])
eps = [e for f in files[1:] for e in claude_session_to_episodes(f, "x")]
print(len(eps), "subagent episodes; agent_id set on all:", all(e["agent_id"] for e in eps))
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run python .../scratchpad/subagent_check.py`
Expected: `ImportError: cannot import name 'claude_session_files'`

- [ ] **Step 3: Implement**

Add to `llm_memory/ingest.py` before `ingest_claude_session`:

```python
def claude_session_files(path):
    """A Claude Code session is its project JSONL plus any subagent transcripts
    under `<session-uuid>/subagents/*.jsonl` beside it (spec D3)."""
    path = Path(path)
    subagents = path.with_suffix("") / "subagents"
    return [path] + sorted(subagents.glob("*.jsonl"))
```

Replace the body of `ingest_claude_session`:

```python
def ingest_claude_session(db, path, experiment_label, dry_run=False, host=None, machine_id=None):
    """Load one Claude Code session (project JSONL plus its subagent
    transcripts) into the episodes collection. One episode per prose assistant
    turn. Idempotent per assistant uuid. When dry_run, counts what WOULD be
    inserted without writing. Returns the count."""
    col = db.collection(EPISODES)
    count = 0
    for file in claude_session_files(path):
        for episode in claude_session_to_episodes(file, experiment_label, host=host, machine_id=machine_id):
            if not dry_run:
                col.insert(episode, overwrite=True)
            count += 1
    return count
```

- [ ] **Step 4: Run the smoke check and the suite**

Run: `uv run python .../scratchpad/subagent_check.py && uv run pytest tests/test_ingest.py -q`
Expected: a line like `3 files; <uuid>.jsonl + ['agent-….jsonl', …]`, then `N subagent episodes; agent_id set on all: True`, then `5 passed`.

- [ ] **Step 5: Commit**

```bash
git add llm_memory/ingest.py
git commit -m "feat: ingest subagent transcripts with their parent session

Spec D3: <session>/subagents/*.jsonl were silently skipped (52 files on
this machine). Same record shape; agent_id carried from agentId.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: CLI with path and hook (stdin) modes

**Files:**
- Modify: `llm_memory/ingest.py` — add `main` and the `__main__` guard at the end

**Interfaces:**
- Consumes: `label_from_project_dir` (Task 1), `read_machine_id`, `ingest_claude_session` (Tasks 2-3), `get_database` from `llm_memory.db`.
- Produces: `main(argv=None) -> int`. Invocation: `python -m llm_memory.ingest claude-session [PATH] [--label L] [--host H] [--machine-id M] [--dry-run]`. With no PATH, reads the Claude Code hook JSON from stdin and uses its `transcript_path`.

Claude Code's `SessionEnd` hook passes JSON on stdin with at least `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `reason`.

- [ ] **Step 1: Write the smoke check (throwaway)**

Save as `.../scratchpad/cli_check.sh`:

```bash
set -e
T=/home/tony/.claude/projects/-home-tony-projects-llm-memory/f92629c7-2c77-4577-9cbb-1e8088cd8fe3.jsonl
echo "-- path mode, dry run"
uv run python -m llm_memory.ingest claude-session "$T" --dry-run
echo "-- hook mode, dry run"
printf '{"session_id":"x","transcript_path":"%s","hook_event_name":"SessionEnd","reason":"other"}' "$T" \
  | uv run python -m llm_memory.ingest claude-session --dry-run
echo "-- unreachable DB fails loudly"
printf '{"transcript_path":"%s"}' "$T" | uv run python -m llm_memory.ingest claude-session --db-host 10.255.255.1 --timeout 2; echo "exit=$?"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `bash .../scratchpad/cli_check.sh`
Expected: first command fails with `No module named llm_memory.ingest.__main__; 'llm_memory.ingest' is a package and cannot be directly executed` or `python -m` reporting the module ran with no output (no `main`). Either way nothing is printed by our code.

- [ ] **Step 3: Implement**

Add to the end of `llm_memory/ingest.py`:

```python
def main(argv=None):
    """`python -m llm_memory.ingest claude-session [PATH]`. Without PATH, reads
    the Claude Code hook JSON from stdin and ingests its `transcript_path`
    (SessionEnd hook mode, spec D4). Fails loudly and does not retry: a
    traceback on stderr and a non-zero exit are the contract; the sweep is
    the retry."""
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m llm_memory.ingest")
    sub = parser.add_subparsers(dest="command", required=True)
    cs = sub.add_parser("claude-session", help="ingest one Claude Code session (path or hook JSON on stdin)")
    cs.add_argument("path", nargs="?", help="project JSONL; omit to read hook JSON from stdin")
    cs.add_argument("--label", help="experiment label (default: derived from the project directory name)")
    cs.add_argument("--host", help="originating hostname (default: this machine)")
    cs.add_argument("--machine-id", help="originating /etc/machine-id (default: this machine)")
    cs.add_argument("--dry-run", action="store_true", help="count without writing")
    args = parser.parse_args(argv)

    if args.path:
        path = Path(args.path)
    else:
        hook = json.load(sys.stdin)
        path = Path(hook["transcript_path"])
    if not path.is_file():
        print(f"claude-session: transcript not found: {path}", file=sys.stderr)
        return 2
    label = args.label or label_from_project_dir(path.parent.name)
    host = args.host or socket.gethostname()
    machine_id = args.machine_id or read_machine_id()

    from llm_memory.db import get_database

    db = get_database()
    count = ingest_claude_session(db, path, label, dry_run=args.dry_run, host=host, machine_id=machine_id)
    verb = "would ingest" if args.dry_run else "ingested"
    print(f"claude-session: {verb} {count} episodes from {path} (label={label}, host={host})")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
```

Remove the `--db-host`/`--timeout` lines from the smoke check's third command; it is replaced by the check in Step 4b. (Those flags were a sketch and are not implemented: the DB address lives in `config/db-config.ini`, and an unreachable host is exercised by pointing at a bad config.)

- [ ] **Step 4a: Run the smoke check**

Run: `bash .../scratchpad/cli_check.sh` (with the third command removed)
Expected, twice: `claude-session: would ingest N episodes from /home/tony/.claude/projects/-home-tony-projects-llm-memory/f92629c7-….jsonl (label=llm-memory, host=WAM-THREADRIPPER)` with the same N both times.

- [ ] **Step 4b: Verify fail-loudly on an unreachable database**

```bash
cp config/db-config.ini .../scratchpad/bad.ini
sed -i 's/^host *=.*/host = 10.255.255.1/' .../scratchpad/bad.ini
printf '{"transcript_path":"/home/tony/.claude/projects/-home-tony-projects-llm-memory/f92629c7-2c77-4577-9cbb-1e8088cd8fe3.jsonl"}' \
  | timeout 30 uv run python -c "
import sys, llm_memory.db as d
from pathlib import Path
d._CONFIG_PATH = Path('.../scratchpad/bad.ini')
from llm_memory.ingest import main
sys.exit(main(['claude-session']))"; echo "exit=$?"
```

Expected: a python-arango connection error traceback on stderr, `exit=1` (or `exit=124` from `timeout` if the address black-holes; both are loud, neither retries). The hook install in Task 6 wraps the command in `timeout 30` for exactly this case.

- [ ] **Step 5: Run the suite**

Run: `uv run pytest tests/test_ingest.py -q`
Expected: `5 passed`

- [ ] **Step 6: Commit**

```bash
git add llm_memory/ingest.py
git commit -m "feat: claude-session ingest CLI with SessionEnd hook mode

python -m llm_memory.ingest claude-session [PATH]; without PATH reads the
Claude Code hook JSON from stdin (spec D3/D4). Label derived from the
project directory, host and machine_id from this machine unless given.
Fails loudly, no retry.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Migrate existing episode keys (A4)

**Files:**
- Create: `scripts/migrate-episode-keys.py`

**Interfaces:**
- Consumes: `get_database` from `llm_memory.db`, `EPISODES` from `llm_memory.index`.
- Produces: a runnable script with `--dry-run`; idempotent.

Why this must precede the hook: the new mapper writes `_key = uuid`. The 9,656 existing episodes from Claude sessions are keyed `<session_id>-<uuid>`. A resumed session, or the future sweep, would insert a second copy of every message under the new key. Rekeying first makes overwrite semantics hold. The 231 fork/resume duplicates collapse as a side effect: when two old keys map to one new key, the second is dropped.

Gateway episodes (`<session>-0001`) and taste_open episodes (`000123`) are untouched: their tails are not uuids.

- [ ] **Step 1: Record the before-state**

```bash
uv run python -c "
from llm_memory.db import get_database
db = get_database()
q = lambda a: next(db.aql.execute(a))
print('total', q('RETURN LENGTH(episodes)'))
print('old-form claude keys', q('''RETURN LENGTH(FOR e IN episodes
  FILTER e.session_id != null AND LIKE(e._key, CONCAT(e.session_id, '-________-____-____-____-____________'))
  RETURN 1)'''))"
```

Expected (2026-09-02): `total 9656`, `old-form claude keys 8435` (all non-gateway Claude episodes; the exact number is recorded here when the step runs, and total minus 1,221 gateway minus taste_open should equal it).

- [ ] **Step 2: Write the script**

`scripts/migrate-episode-keys.py`:

```python
"""One-off: rekey Claude-session episodes from <session_id>-<uuid> to <uuid>
(khipumaq amendment A4). Idempotent; --dry-run reports without writing.

Two old keys can map to one new key (fork/resume copies): the first one
migrated wins and the second is deleted. Gateway (<session>-NNNN) and
taste_open (NNNNNN) keys do not match the uuid tail and are untouched."""
import re
import sys

from llm_memory.db import get_database
from llm_memory.index import EPISODES

UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def main(argv):
    dry_run = "--dry-run" in argv
    db = get_database()
    col = db.collection(EPISODES)
    cursor = db.aql.execute(
        "FOR e IN episodes FILTER e.session_id != null RETURN {key: e._key, session: e.session_id}",
        batch_size=1000,
    )
    moved = dropped = skipped = 0
    for row in cursor:
        prefix = row["session"] + "-"
        if not row["key"].startswith(prefix):
            skipped += 1
            continue
        new_key = row["key"][len(prefix):]
        if not UUID.match(new_key):
            skipped += 1
            continue
        if col.has(new_key):
            dropped += 1
            if not dry_run:
                col.delete(row["key"])
            continue
        moved += 1
        if not dry_run:
            doc = col.get(row["key"])
            doc = {k: v for k, v in doc.items() if k not in ("_id", "_rev")}
            doc["_key"] = new_key
            col.insert(doc)
            col.delete(row["key"])
    verb = "would move" if dry_run else "moved"
    print(f"{verb} {moved}, dropped {dropped} duplicates, skipped {skipped} non-uuid keys")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
```

- [ ] **Step 3: Dry run**

Run: `uv run python scripts/migrate-episode-keys.py --dry-run`
Expected: `would move 8204, dropped 231 duplicates, skipped 1221 non-uuid keys` — the middle number must be exactly 231 (the measured duplicate count) and the last must be 1,221 (gateway episodes; taste_open episodes have no `session_id` and are not returned by the query). If the middle number is not 231, stop and investigate before running for real.

- [ ] **Step 4: Run for real, then verify idempotence and counts**

```bash
uv run python scripts/migrate-episode-keys.py
uv run python scripts/migrate-episode-keys.py --dry-run
uv run python -c "
from llm_memory.db import get_database
db = get_database(); q = lambda a: next(db.aql.execute(a))
print('total', q('RETURN LENGTH(episodes)'))
print('remaining old-form', q('''RETURN LENGTH(FOR e IN episodes
  FILTER e.session_id != null AND LIKE(e._key, CONCAT(e.session_id, '-________-____-____-____-____________')) RETURN 1)'''))
print('uuid-keyed', q('RETURN LENGTH(FOR e IN episodes FILTER REGEX_TEST(e._key, \"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$\") RETURN 1)'))"
```

Expected: first run prints `moved 8204, dropped 231 …`; second (dry) prints `would move 0, dropped 0 duplicates, …`; then `total 9425`, `remaining old-form 0`, `uuid-keyed 8204`.

- [ ] **Step 5: Confirm search still works after rekey**

Run: `uv run pytest tests/test_search.py tests/test_recall.py -q`
Expected: `5 passed` (the ArangoSearch view links the collection; inserts and deletes are indexed automatically).

- [ ] **Step 6: Commit**

```bash
git add scripts/migrate-episode-keys.py
git commit -m "chore: rekey existing Claude-session episodes to assistant uuid (A4)

Run once on 2026-09-02: moved 8204, dropped 231 fork/resume duplicates,
gateway and taste_open keys untouched. 9656 -> 9425 episodes.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

(Fill in the actual numbers from Step 4 if they differ.)

---

### Task 6: Install the SessionEnd hook and prove the round trip

**Files:**
- Create: `scripts/install-session-end-hook.sh`

**Interfaces:**
- Consumes: the CLI from Task 4.
- Produces: an entry in `~/.claude/settings.json` under `hooks.SessionEnd[0].hooks[]`:
  `{"type": "command", "command": "timeout 30 <repo>/.venv/bin/python -m llm_memory.ingest claude-session"}`.

The existing SessionEnd hook (`uvx qhaway session-end`) stays; ours is appended to the same group. The `timeout 30` bounds a black-holed database so a session exit never hangs.

- [ ] **Step 1: Write the installer**

`scripts/install-session-end-hook.sh`:

```bash
#!/bin/bash
# Install the khipumaq/llm-memory SessionEnd ingest hook into
# ~/.claude/settings.json (spec D4, layer 1). Idempotent. Run on each machine
# whose sessions should land in the store; the repo, its .venv, and
# config/db-config.ini must exist there.
set -e
GIT_ROOT=$(git rev-parse --show-toplevel)
PY="$GIT_ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "FATAL: $PY not found. Run: uv sync" >&2; exit 1; }
CMD="timeout 30 $PY -m llm_memory.ingest claude-session"
SETTINGS="$HOME/.claude/settings.json"
python3 - "$SETTINGS" "$CMD" <<'EOF'
import json, sys
path, cmd = sys.argv[1], sys.argv[2]
s = json.load(open(path))
groups = s.setdefault("hooks", {}).setdefault("SessionEnd", [])
if not groups:
    groups.append({"hooks": []})
hooks = groups[0].setdefault("hooks", [])
if any(h.get("command") == cmd for h in hooks):
    print("already installed:", cmd)
else:
    hooks.append({"type": "command", "command": cmd})
    json.dump(s, open(path, "w"), indent=2)
    print("installed:", cmd)
EOF
```

- [ ] **Step 2: Run it twice**

Run: `chmod +x scripts/install-session-end-hook.sh && scripts/install-session-end-hook.sh && scripts/install-session-end-hook.sh`
Expected: `installed: timeout 30 /home/tony/projects/llm-memory/.venv/bin/python -m llm_memory.ingest claude-session` then `already installed: …`.

- [ ] **Step 3: Confirm settings.json shape**

Run: `python3 -c "import json; print(json.dumps(json.load(open('/home/tony/.claude/settings.json'))['hooks']['SessionEnd'], indent=1))"`
Expected: one group with two command hooks, qhaway's first, ours second.

- [ ] **Step 4: Round-trip freshness (spec verification 2), without waiting for the session to end**

Feed the hook command the current session's transcript by hand, then search for a phrase only this session contains:

```bash
printf '{"session_id":"f92629c7-2c77-4577-9cbb-1e8088cd8fe3","transcript_path":"/home/tony/.claude/projects/-home-tony-projects-llm-memory/f92629c7-2c77-4577-9cbb-1e8088cd8fe3.jsonl","hook_event_name":"SessionEnd","reason":"other"}' \
  | timeout 30 .venv/bin/python -m llm_memory.ingest claude-session
uv run python -c "
from llm_memory.db import get_database
from llm_memory.search import search
hits = search(get_database(), 'Susan Calvin Herbie Liar', scope='llm-memory', limit=3)
print(len(hits), [h.get('key', h.get('_key'))[:8] for h in hits])"
```

Expected: `claude-session: ingested N episodes from … (label=llm-memory, host=WAM-THREADRIPPER)` with N > 100, then at least one hit. (Check `llm_memory/search.py` for the exact `search` signature before running; adjust the call, not the expectation.)

- [ ] **Step 5: Re-run the same ingest and confirm idempotence**

Run the `printf … | … claude-session` line again, then:
`uv run python -c "from llm_memory.db import get_database; db=get_database(); print(next(db.aql.execute(\"RETURN LENGTH(FOR e IN episodes FILTER e.session_id=='f92629c7-2c77-4577-9cbb-1e8088cd8fe3' RETURN 1)\")))"`
Expected: the same N as Step 4, not 2N.

- [ ] **Step 6: Commit**

```bash
git add scripts/install-session-end-hook.sh
git commit -m "feat: SessionEnd hook installer for claude-session ingest

Spec D4 layer 1. Appends a timeout-bounded ingest command to the
SessionEnd group in ~/.claude/settings.json beside qhaway's; idempotent.
Round trip verified on the live session that wrote this.

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: Codex authors the validating tests

**Files:**
- Create (Codex): `tests/test_claude_session_ingest.py`
- Codex commits it as `test: validate claude-session ingest, label derivation, uuid key, hook mode`

**Interfaces:**
- Consumes: everything from Tasks 1-6 as committed on `main`.

- [ ] **Step 1: Write the brief**

Save as `.../scratchpad/codex-brief.md`:

```markdown
Repository: /home/tony/projects/llm-memory (shared working tree; stage ONLY the file you create).
Project rule: code and tests come from SEPARATE instances. Claude implemented the commits
from "feat: derive episode label from Claude project directory name" through
"feat: SessionEnd hook installer for claude-session ingest" (see git log). Your job is an
independent, adversarial validating test module. Do not modify llm_memory/ or scripts/ or
existing tests. Read docs/superpowers/specs/2026-09-01-khipumaq-design.md sections D3, D4,
and Amendment A4 first; the tests validate the spec, not the implementation's happy path.

Create tests/test_claude_session_ingest.py using pytest and tmp_path. The existing
tests/test_ingest.py shows the style and that a live ArangoDB (config/db-config.ini) is
available; when you write to it, use unique session ids and uuids (uuid4) and delete what
you wrote in a finally block.

Cover, at minimum, and add whatever else the spec implies:
1. label_from_project_dir: the plain project dir; both worktree spellings
   (`<p>--worktrees-<x>`, `<p>-.worktrees-<x>`) and `.claude-worktrees`; the scratchpad
   form `-tmp-claude-1000--home-tony-projects-<p>-<uuid>-scratchpad-...`; dots normalized;
   `yanantin` -> `yanantin_construction`; `quantumos` unchanged; `-home-tony` -> `home`.
2. claude_session_to_episodes: `_key` equals the assistant uuid; `host`, `machine_id`,
   `agent_id` present; tool-only assistant turns skipped; malformed lines skipped;
   user_message pairs with the most recent preceding user turn.
3. Fork/resume dedupe end to end: write two transcripts that share assistant uuids but
   differ in sessionId; ingest both with ingest_claude_session; assert exactly one
   episode per shared uuid exists, and that each file's unique tail is present.
4. claude_session_files and subagent ingestion: a session file plus
   `<stem>/subagents/agent-x.jsonl`; assert the subagent's episodes are ingested with
   agent_id set and the parent's are too.
5. main(): path mode with --dry-run prints "would ingest N" and writes nothing (check
   the DB); hook mode reads {"transcript_path": ...} from stdin (monkeypatch sys.stdin);
   missing transcript returns exit 2 with a message on stderr; --label overrides the
   derived label.
6. read_machine_id reads and strips the given path.

Run `uv run pytest tests/test_claude_session_ingest.py -q` until it passes or until a
failure is an implementation bug; in that case leave the test as is and report the
failure in your final message with the exact assertion. Commit only your test file with
message: "test: validate claude-session ingest, label derivation, uuid key, hook mode".
```

- [ ] **Step 2: Dispatch Codex**

```bash
codex exec -C /home/tony/projects/llm-memory -s workspace-write \
  -o .../scratchpad/codex-last.md "$(cat .../scratchpad/codex-brief.md)"
cat .../scratchpad/codex-last.md
```

Expected: Codex's final message names the test file, the test count, and either "all passing" or a list of implementation failures with assertions. `git log -1` shows Codex's commit.

- [ ] **Step 3: Run the tests yourself**

Run: `uv run pytest tests/test_claude_session_ingest.py tests/test_ingest.py -q`
Expected: all passed. If Codex reported implementation failures, verify each against the spec: a real bug is fixed in `llm_memory/ingest.py` in a new `fix:` commit by Claude; a wrong test is sent back with `codex exec resume --last "<what the spec says and why the test is wrong>"`.

- [ ] **Step 4: Run the whole suite**

Run: `timeout 600 uv run pytest -q 2>&1 | tail -3`
Expected: no new failures relative to `main` before this plan. (Contract-layer tests that need `config/sources.yaml` were already failing or skipped on this machine; record the before/after counts in the commit message if they differ.)

- [ ] **Step 5: Push**

Only when Tony says so. `git push` publishes to fsgeek/llm-memory.

---

## Self-Review

**Spec coverage.** D3 `claude-session <path>` and hook mode: Task 4. Required fields host/machine_id/source_file: Task 2 (source_file was already canonical for local ingest; the `--host`/`--machine-id` flags exist so a future staging sweep can pass the origin's). Label derivation incl. worktrees, scratchpad, kept labels: Task 1. Subagent files: Task 3. D4 layer 1 hook, fail loudly, no retry, latency seconds: Tasks 4 and 6. A4 key: Tasks 2 and 5. Verification 2 (round trip): Task 6 Step 4. Not covered, by design: `sweep`, `codex` subcommands, D4 layer 2 nightly timer, D5 instructions, D2 deletion, observability events (spec says keep a content-free slice; the current `observability.py` is contract-shaped and is rewritten when D2 lands). The hostless 4,258 stay hostless.

**Placeholder scan.** The `.../scratchpad/` prefix stands for the session scratchpad directory named in Task 1 Step 1; every command using it should substitute that absolute path. Task 5's expected `moved` count (8,204) is derived (9,656 total − 1,221 gateway − 231 duplicates), not measured; Step 3's dry run measures it and the commit message takes the measured number. Task 6 Step 4 flags that `search`'s signature must be checked before running.

**Type consistency.** `claude_session_to_episodes(path, experiment_label, host=None, machine_id=None)` in Tasks 2, 3, 4 and the Codex brief. `ingest_claude_session(db, path, experiment_label, dry_run=False, host=None, machine_id=None)` in Tasks 2, 3, 4, brief. `claude_session_files(path) -> list[Path]` in Tasks 3, brief. `label_from_project_dir(name) -> str` in Tasks 1, 4, brief. `read_machine_id(path=Path("/etc/machine-id")) -> str` in Tasks 2, 4, brief. `main(argv=None) -> int` in Tasks 4, 6, brief.
