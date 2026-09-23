"""One-off: rekey Claude-session episodes from <session_id>-<uuid> to <uuid>
(khipumaq amendment A4). Idempotent; --dry-run reports without writing.

Two old keys can map to one new key (fork/resume copies): the first one
migrated wins and the second is deleted. Gateway (<session>-NNNN) and
taste_open (NNNNNN) keys do not match the uuid tail and are untouched."""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from khipumaq.db import get_database
from khipumaq.index import EPISODES

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
    seen = set()  # new keys claimed this run, so --dry-run counts duplicates too
    for row in cursor:
        prefix = row["session"] + "-"
        if not row["key"].startswith(prefix):
            skipped += 1
            continue
        new_key = row["key"][len(prefix):]
        if not UUID.match(new_key):
            skipped += 1
            continue
        if new_key in seen or col.has(new_key):
            dropped += 1
            if not dry_run:
                col.delete(row["key"])
            continue
        seen.add(new_key)
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
