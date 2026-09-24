"""One-off: relabel Codex episodes by the project their cwd sits in, so a
directory below a project folds into it (khipumaq amendment A15). Before this,
`/home/tony/projects/cpsc416/tmp/capstone/<student>` was its own label, one
per student: 176 labels for 8 projects. The cwd itself is kept on every
episode (`codex.cwd`), so nothing is lost. Idempotent; --dry-run reports
without writing. Claude-session episodes are untouched: their label comes from
an encoded directory name that has no separators left to fold on."""
import sys
from collections import Counter

from khipumaq.db import get_database
from khipumaq.index import EPISODES
from khipumaq.ingest import label_from_path


def main(argv):
    dry_run = "--dry-run" in argv
    db = get_database()
    groups = db.aql.execute(
        "FOR d IN @@col FILTER d.codex.cwd != null "
        "COLLECT cwd = d.codex.cwd, label = d.experiment_label WITH COUNT INTO n "
        "RETURN [cwd, label, n]",
        bind_vars={"@col": EPISODES},
    )
    moves = Counter()
    for cwd, old, n in groups:
        new = label_from_path(cwd)
        if new == old:
            continue
        moves[(old, new)] += n
        if not dry_run:
            db.aql.execute(
                "FOR d IN @@col FILTER d.codex.cwd == @cwd AND d.experiment_label == @old "
                "UPDATE d WITH {experiment_label: @new} IN @@col",
                bind_vars={"@col": EPISODES, "cwd": cwd, "old": old, "new": new},
            )
    verb = "would relabel" if dry_run else "relabelled"
    print(f"{verb} {sum(moves.values())} episodes from {len({o for o, _ in moves})} labels "
          f"into {len({n for _, n in moves})}")
    for new in sorted({n for _, n in moves}):
        print(f"  {new}: {sum(c for (_, n), c in moves.items() if n == new)}")


if __name__ == "__main__":
    main(sys.argv[1:])
