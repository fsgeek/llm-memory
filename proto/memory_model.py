"""DuckDB prototype: model MEMORY.md + its secondary files as a database.

The point is to show that the hand-maintained MEMORY.md index is already a
*derived view* over a node/edge graph the files implicitly form — and that
"the index" is a QUERY, not a file you whittle. Run against the real memory
directory; no yanantin/Arango dependency.

    uv run python -m proto.memory_model --build
    uv run python -m proto.memory_model --index        # regenerate MEMORY.md as a query
    uv run python -m proto.memory_model --slice feedback
"""

import argparse
import glob
import os
import re

import duckdb
import yaml

MEMORY_DIR = os.path.expanduser(
    "~/.claude/projects/-home-tony-projects-hamutay/memory"
)
DB_PATH = os.path.join(os.path.dirname(__file__), "memory.duckdb")

_FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n?(.*)$", re.DOTALL)
_WIKILINK = re.compile(r"\[\[([a-z0-9_\-]+)\]\]")
_DATE = re.compile(r"_(\d{8})")


def _load_frontmatter(block):
    """Real hand-authored frontmatter isn't always valid YAML — unquoted colons
    in description values are common. Try strict YAML; on failure fall back to a
    tolerant top-level `key: value` line parse (with one nesting level for
    `metadata:`). Never drop the node over a syntax slip — that would be exactly
    the silent loss this whole effort exists to prevent."""
    try:
        return yaml.safe_load(block) or {}
    except yaml.YAMLError:
        fm, cur = {}, None
        for line in block.splitlines():
            if not line.strip():
                continue
            indented = line[0] in " \t"
            key, _, val = line.strip().partition(":")
            val = val.strip()
            if not indented:
                if val == "":
                    cur = {}
                    fm[key] = cur
                else:
                    fm[key] = val
                    cur = None
            elif cur is not None:
                cur[key] = val
        return fm


def parse_file(path):
    """One memory file -> a node dict. Frontmatter becomes columns; the body is
    the full fact; provenance (type, origin session, date hint) are the facets
    that were ALREADY being captured in the files by hand."""
    fname = os.path.basename(path)
    with open(path) as f:
        text = f.read()
    if fname == "MEMORY.md":
        return None  # the index itself is the derived view, not a node
    m = _FRONTMATTER.match(text)
    if m:
        fm = _load_frontmatter(m.group(1))
        body = m.group(2).strip()
    else:
        # Older convention (handoffs, gifts) has NO frontmatter — still a node.
        # Dropping it would silently lose the entire bequest lineage.
        fm, body = {}, text.strip()
    meta = fm.get("metadata") or {}
    # type lives in metadata on newer files, top-level on older, prefix otherwise.
    node_type = meta.get("type") or fm.get("type") or fname.split("_")[0]
    # first non-empty body line doubles as description when frontmatter lacks one
    first_line = next((ln.strip() for ln in body.splitlines() if ln.strip()), "")
    date_hint = None
    if dm := _DATE.search(fname):
        date_hint = dm.group(1)
    return {
        "name": fm.get("name") or fname[:-3],
        "file": fname,
        "type": node_type,
        "description": fm.get("description") or first_line[:200],
        "origin_session": meta.get("originSessionId"),
        "date_hint": date_hint,
        "body": body,
        "links": _WIKILINK.findall(body),
    }


def build(con):
    con.execute("DROP TABLE IF EXISTS edges")
    con.execute("DROP TABLE IF EXISTS nodes")
    # The FILENAME is the true stable id, not frontmatter `name`: some prior
    # gholas overwrote `name` with tombstones ("SUPERSEDED — see ...") so it is
    # neither unique nor always an identifier. Key on file; keep name as an attr.
    con.execute(
        """CREATE TABLE nodes (
            file TEXT PRIMARY KEY, name TEXT, type TEXT, description TEXT,
            origin_session TEXT, date_hint TEXT, body TEXT
        )"""
    )
    con.execute(
        "CREATE TABLE edges (src TEXT, dst TEXT, kind TEXT)"  # kind: REFERENCES (wikilink)
    )
    nodes, edges = [], []
    for path in sorted(glob.glob(os.path.join(MEMORY_DIR, "*.md"))):
        node = parse_file(path)
        if node is None:
            continue
        nodes.append(node)
        for dst in node["links"]:
            edges.append((node["name"], dst, "REFERENCES"))
    con.executemany(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?)",
        [
            (
                n["file"], n["name"], n["type"], n["description"],
                n["origin_session"], n["date_hint"], n["body"],
            )
            for n in nodes
        ],
    )
    con.executemany("INSERT INTO edges VALUES (?,?,?)", edges)
    return len(nodes), len(edges)


def render_index(con, where="", params=None):
    """THE INDEX IS THIS QUERY. MEMORY.md = SELECT description rendered as links,
    optionally facet-filtered. No file to whittle; you narrow the WHERE."""
    rows = con.execute(
        f"""SELECT name, file, description FROM nodes
            {('WHERE ' + where) if where else ''}
            ORDER BY type, name""",
        params or [],
    ).fetchall()
    return "\n".join(f"- [{n}]({f}) — {d}" for n, f, d in rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--index", action="store_true", help="regenerate full index")
    ap.add_argument("--slice", metavar="TYPE", help="index for one facet (type)")
    ap.add_argument("--dangling", action="store_true", help="edges to unwritten nodes")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()

    con = duckdb.connect(DB_PATH)
    if args.build:
        n, e = build(con)
        print(f"built: {n} nodes, {e} edges -> {DB_PATH}")
    if args.stats:
        print("by type:")
        for t, c in con.execute(
            "SELECT type, count(*) FROM nodes GROUP BY type ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {c:4d}  {t}")
        idx = render_index(con)
        print(f"full index: {len(idx)} bytes ({len(idx.splitlines())} entries)")
    if args.slice:
        idx = render_index(con, "type = ?", [args.slice])
        print(f"# index slice: type={args.slice} ({len(idx)} bytes)\n{idx}")
    if args.index:
        print(render_index(con))
    if args.dangling:
        # edges whose dst was never written — the [[link]]s that mark
        # "worth writing later", per the memory convention. Honest gaps.
        rows = con.execute(
            """SELECT e.src, e.dst FROM edges e
               LEFT JOIN nodes n ON e.dst = n.name
               WHERE n.name IS NULL ORDER BY e.dst"""
        ).fetchall()
        print(f"dangling links ({len(rows)}):")
        for src, dst in rows:
            print(f"  {src} -> [[{dst}]]  (unwritten)")
    con.close()


if __name__ == "__main__":
    main()
