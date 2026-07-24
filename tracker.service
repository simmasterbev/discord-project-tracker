"""Bulk-load trees and milestones from a YAML file or a spreadsheet export.

Typing twenty slash commands to stand up a tree is miserable and error-prone.
Write the structure once, run this, done.

    python seed.py my_tree.yaml --guild 123456789012345678
    python seed.py my_tree.csv  --guild 123456789012345678

For the spreadsheet route, use these column headers:

    tree, milestone, description, unlocks, requires, xp, auto_close

One row per milestone. Put semicolons between multiple prerequisites.

Re-running is safe: everything upserts by key, so edit the file and run it again
to push changes. Nothing is ever deleted — remove things with the slash commands.

A `requires:` entry naming something the file doesn't define becomes a stub, so
you can sketch a tree top-down and fill the gaps in later passes.

Get your guild ID from Discord: Settings > Advanced > Developer Mode, then
right-click the server icon > Copy Server ID.
"""

from __future__ import annotations

import argparse
import csv
import io
import sys
from pathlib import Path

import yaml

import db

CSV_COLUMNS = ["tree", "milestone", "description", "unlocks", "requires", "xp", "auto_close"]


def csv_to_doc(source) -> dict:
    """A spreadsheet export becomes the same structure as the YAML format.

    One row per milestone. `requires` holds semicolon-separated names. Anything
    named there but not defined in the file becomes a stub, same as in YAML.
    """
    trees: dict[str, dict] = {}
    loose: list[dict] = []
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8-sig")
    else:
        text = source.lstrip("\ufeff")
    problems: list[str] = []
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames:
        known = {c.strip().lower() for c in reader.fieldnames if c}
        if "milestone" not in known and "name" not in known:
            problems.append("No `milestone` column found — check the header row.")
    for i, row in enumerate(reader, start=2):
        if True:
            row = {(k or "").strip().lower(): (v or "").strip()
                   for k, v in row.items() if k}
            name = row.get("milestone") or row.get("name")
            if not name:
                continue
            spec = {
                "key": name.lower().replace(" ", "-")[:20],
                "name": name,
                "description": row.get("description", ""),
                "unlocks": row.get("unlocks", ""),
                "requires": [r.strip() for r in
                             row.get("requires", "").replace(";", ",").split(",") if r.strip()],
            }
            if row.get("xp"):
                try:
                    spec["xp"] = int(row["xp"])
                except ValueError:
                    problems.append(f"row {i}: xp '{row['xp']}' isn't a number, using 100")
            if row.get("auto_close"):
                spec["auto_close"] = row["auto_close"].lower() not in ("false", "no", "0", "n")

            tname = row.get("tree", "").strip()
            if tname:
                t = trees.setdefault(tname, {
                    "key": tname.lower().replace(" ", "-")[:20],
                    "name": tname,
                    "milestones": [],
                })
                t["milestones"].append(spec)
            else:
                loose.append(spec)
    return {"trees": list(trees.values()), "milestones": loose, "problems": problems}


def upsert_project(guild_id: int, spec: dict, owner: int) -> int:
    name = spec["name"]
    existing = db.get_project(guild_id, name)
    if existing:
        db.update_project(existing["id"], description=spec.get("description"))
        return existing["id"]
    return db.create_project(guild_id, name, spec.get("description", ""), owner)


def upsert_tree(guild_id: int, spec: dict) -> int:
    key = spec["key"]
    existing = db.get_tree(guild_id, key)
    if existing:
        db.update_tree(existing["id"], name=spec.get("name"),
                       description=spec.get("description"))
        return existing["id"]
    return db.create_tree(guild_id, key, spec.get("name", key),
                          spec.get("description", ""))


def upsert_milestone(guild_id: int, spec: dict) -> int:
    key = spec["key"]
    existing = db.get_milestone(guild_id, key)
    if existing:
        db.update_milestone(existing["id"], name=spec.get("name"),
                            description=spec.get("description"),
                            unlocks=spec.get("unlocks"), xp=spec.get("xp"),
                            auto_close=None if spec.get("auto_close") is None
                            else int(bool(spec["auto_close"])))
        return existing["id"]
    return db.create_milestone(guild_id, key, spec.get("name", key),
                               spec.get("unlocks", ""), int(spec.get("xp", 100)),
                               spec.get("description", ""),
                               bool(spec.get("auto_close", True)))


def parse(text: str, filename: str) -> dict:
    """Text in, plan structure out. Used by both the CLI and `/tree import`."""
    if filename.lower().endswith((".csv", ".tsv")):
        return csv_to_doc(text)
    return yaml.safe_load(text) or {}


def preview(doc: dict, guild_id: int) -> dict:
    """What applying this would do, without touching anything."""
    specs = [m for t in doc.get("trees", []) for m in t.get("milestones", [])] \
            + doc.get("milestones", [])
    # requires: entries name milestones by display name, so match on both
    defined = {s["key"] for s in specs} | {s.get("name", "").strip().lower() for s in specs}
    new_trees, known_trees = [], []
    for t in doc.get("trees", []):
        (known_trees if db.get_tree(guild_id, t["key"]) else new_trees).append(
            t.get("name", t["key"]))
    created, updated, stubs = [], [], set()
    for spec in specs:
        existing = db.get_milestone(guild_id, spec["key"])
        (updated if existing else created).append(spec.get("name", spec["key"]))
        for req in spec.get("requires", []) or []:
            low = req.strip().lower()
            if low in defined:
                continue
            if not any(m["key"] == low or req.strip().lower() in m["name"].lower()
                       for m in db.list_milestones(guild_id)):
                stubs.add(req.strip())
    return {"new_trees": new_trees, "known_trees": known_trees, "created": created,
            "updated": updated, "stubs": sorted(stubs),
            "problems": doc.get("problems", [])}


def apply_doc(doc: dict, guild_id: int, owner: int) -> list[str]:
    log: list[str] = []

    # projects first — milestones and tasks both point at them
    for spec in doc.get("projects", []):
        pid = upsert_project(guild_id, spec, owner)
        log.append(f"project  {spec['name']}")
        for t in spec.get("tasks", []):
            if isinstance(t, str):
                t = {"title": t}
            existing = [r for r in db.list_tasks(pid) if r["title"] == t["title"]]
            if existing:
                continue
            db.add_task(pid, t["title"], t.get("assignee"),
                        t.get("due"), int(t.get("weight", 1)))
            log.append(f"  task   {t['title']}")

    for spec in doc.get("trees", []):
        tid = upsert_tree(guild_id, spec)
        log.append(f"tree     {spec['key']}")
        for m in spec.get("milestones", []):
            mid = upsert_milestone(guild_id, m)
            db.add_to_tree(tid, mid)
            log.append(f"  node   {m['key']}  ({m.get('xp', 100)} XP)")

    # standalone milestones that belong to no tree
    for m in doc.get("milestones", []):
        upsert_milestone(guild_id, m)
        log.append(f"node     {m['key']} (unfiled)")

    # second pass: dependencies and project links need every milestone to exist
    all_specs = [m for t in doc.get("trees", []) for m in t.get("milestones", [])]
    all_specs += doc.get("milestones", [])
    for m in all_specs:
        node = db.get_milestone(guild_id, m["key"])
        for req in m.get("requires", []) or []:
            pre_id, stubbed = db.find_or_stub(guild_id, req)
            if not db.add_dep(node["id"], pre_id):
                log.append(f"  !! {m['key']} <- {req} skipped: would create a loop")
                continue
            log.append(f"  gate   {m['key']} <- {req}" + ("  (stubbed)" if stubbed else ""))
        for proj in m.get("projects", []) or []:
            p = db.get_project(guild_id, proj)
            if p is None:
                log.append(f"  !! {m['key']} links unknown project '{proj}'")
                continue
            db.link_project(node["id"], p["id"])
            log.append(f"  link   {m['key']} <- {proj}")

    return log


def load(path: Path, guild_id: int, owner: int, dry_run: bool = False) -> None:
    doc = parse(path.read_text(encoding="utf-8-sig"), path.name)
    for p in doc.get("problems", []):
        print(f"  !! {p}")
    log = apply_doc(doc, guild_id, owner)
    print("\n".join(log))
    print(f"\n{len(log)} item(s) applied.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("file", type=Path, help="a .yaml or .csv plan")
    ap.add_argument("--guild", type=int, required=True, help="Discord server ID")
    ap.add_argument("--owner", type=int, default=0,
                    help="Discord user ID recorded as project owner")
    ap.add_argument("--db", type=Path, default=None, help="Path to tracker.db")
    args = ap.parse_args()

    if not args.file.exists():
        sys.exit(f"No such file: {args.file}")
    db.connect(args.db or db.DB_PATH)
    load(args.file, args.guild, args.owner)


if __name__ == "__main__":
    main()
