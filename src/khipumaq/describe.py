"""What the store holds, said in one sentence (khipumaq D5).

`describe(db)` is the read-only census: counts by label and host plus the
date range. `instructions(db, ...)` turns that into the sentence the MCP
server says about itself at start, in the register of qhaway's MEMORY.md
line: what is here, how fresh it is, and when to reach for it. The sentence
is honest about staleness on purpose — a store that implies freshness it does
not have teaches the instance to stop searching.
"""

import os
from datetime import UTC, datetime

from khipumaq.index import EPISODES
from khipumaq.ingest import label_from_path

SERVER_NAME = "khipumaq"
STALE_AFTER_HOURS = 48

_STATS_AQL = """
LET total = LENGTH(@@col)
LET newest = MAX(FOR d IN @@col RETURN d.ts)
LET oldest = MIN(FOR d IN @@col RETURN d.ts)
LET labels = (FOR d IN @@col COLLECT l = d.experiment_label WITH COUNT INTO n
              SORT n DESC RETURN {label: l, count: n})
LET hosts = (FOR d IN @@col COLLECT h = d.host WITH COUNT INTO n
             SORT n DESC RETURN {host: h, count: n})
RETURN {episodes: total, newest: newest, oldest: oldest,
        labels: labels, hosts: hosts}
"""


def describe(db):
    """Counts by label and host, total, and the oldest/newest episode
    timestamps. Episodes ingested before host capture have host null."""
    return next(iter(db.aql.execute(_STATS_AQL, bind_vars={"@col": EPISODES})))


def project_label(project_dir=None):
    """Label for the project the *consuming* session runs in. Claude Code
    launches the server with cwd pinned to this repo but exports
    CLAUDE_PROJECT_DIR; map that path to the same label ingest derives from
    the `~/.claude/projects/<name>` directory. None when not under Claude."""
    project_dir = project_dir or os.environ.get("CLAUDE_PROJECT_DIR")
    return label_from_path(project_dir) if project_dir else None


def _parse_ts(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _plural(n, unit):
    return f"{n} {unit}{'' if n == 1 else 's'} ago"


def _age(newest, now):
    delta = now - _parse_ts(newest)
    hours = delta.total_seconds() / 3600
    if hours < 1:
        return _plural(max(int(delta.total_seconds() // 60), 1), "minute")
    if hours < 48:
        return _plural(int(hours), "hour")
    return _plural(int(hours // 24), "day")


def sentence(stats, label=None, now=None):
    """The server's one sentence about itself, from a `describe()` result."""
    now = now or datetime.now(UTC)
    total = stats["episodes"]
    if not total:
        return (
            f"{SERVER_NAME} holds no episodes yet; ingestion has not run. "
            "search() will find nothing until it does."
        )
    hosts = [h["host"] for h in stats["hosts"] if h["host"]]
    newest = stats["newest"]
    hours_old = (now - _parse_ts(newest)).total_seconds() / 3600
    freshness = f"newest {_age(newest, now)}"
    if hours_old > STALE_AFTER_HOURS:
        freshness += " — ingestion may be broken"
    text = (
        f"{SERVER_NAME} holds {total:,} episodes from every prior session "
        f"across {len(stats['labels'])} projects on {len(hosts)} machines, "
        f"{freshness}"
    )
    if label:
        count = next(
            (l["count"] for l in stats["labels"] if l["label"] == label), 0
        )
        text += f"; this project is `{label}` ({count:,} episodes)"
    text += (
        ". It holds the user's words and prior assistant responses, not "
        "summaries. Before asking Tony what happened or what was decided, "
        "search() first; when Tony says he does not recall, search()."
    )
    return text


def instructions(db, project_dir=None, now=None):
    return sentence(describe(db), project_label(project_dir), now)
