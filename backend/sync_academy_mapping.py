"""
Syncs one track to its official mapping file (module order, class order, junction number,
class type, plus an optional per-module segment and duration in months).

Existing modules and classes are updated in place so scripts that point at them keep working.
Modules that the mapping puts under the track but the database only holds under another
track of the same program are copied into it; the originals are not touched.

Usage (from backend/):
    python -m sync_academy_mapping                                  # Beginner, dry run
    python -m sync_academy_mapping --apply                          # Beginner, writes (backs up scripts.db first)
    python -m sync_academy_mapping --track "Intermediate Track" \
        --mapping data/academy_intermediate_mapping.txt [--apply]
    python -m sync_academy_mapping --program "DevOps & Cloud" --track "Core & Electives Track" \
        --mapping data/devops_mapping.txt [--apply]
"""

import argparse
import difflib
import json
import os
import re
import shutil
import sys
from datetime import datetime

from sqlalchemy import text

from database import DB_FILE, SyncSessionLocal, sync_engine
from models import Class, Module, Program, Script, Track

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_PROGRAM = "Academy"

# Mapping module name (normalised) -> older name it may still have in the database, where the two
# differ by more than case. The mapping name is tried first, so re-running after a rename is a no-op.
MODULE_ALIASES = {
    "react, typescript and frontend product architecture deep dive":
        "react and frontend product architecture fundamentals deep dive",
    "advanced dsa: linear data structures":
        "advanced dsa: linear & non-linear structures",
    "beginner python": "python",
}

CANONICAL_TYPES = {"live": "Live", "inverted": "Inverted", "lab": "Lab", "to be planned": "To be planned",
                   "recorded": "Recorded"}

TYPE_BLANK = "Unspecified"


def norm(s: str) -> str:
    s = re.sub(r"\s+", " ", s.strip().lower())
    return re.sub(r"\s*:\s*", ": ", s)


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip())


def parse_mapping(path: str):
    modules = []
    for raw in open(path, encoding="utf-8"):
        line = raw.rstrip("\n")
        if line.startswith("## "):
            name, *extra = [p.strip() for p in line[3:].split(" || ")]
            segment = extra[0] if len(extra) > 0 and extra[0] else None
            months = float(extra[1]) if len(extra) > 1 and extra[1] else None
            modules.append({"name": clean(name), "segment": segment, "months": months, "classes": []})
            continue
        if not line.strip() or line.startswith("#"):
            continue
        title, junction, ctype = [p.strip() for p in line.rsplit("|", 2)]
        modules[-1]["classes"].append({
            "title": clean(title),
            "junction_raw": int(junction) if junction else None,
            "class_type": TYPE_BLANK if ctype in ("", "-") else CANONICAL_TYPES.get(ctype.lower(), ctype),
        })
    for m in modules:  # a junction number carries forward within its module
        current = None
        for c in m["classes"]:
            if c["junction_raw"] is not None:
                current = c["junction_raw"]
            c["junction"] = current
    return modules


def ensure_columns():
    """Additive, nullable columns the models expect. Returns the names that were added."""
    added = []
    with sync_engine.begin() as conn:
        for table, col, ddl in [
            ("classes", "junction_number", "INTEGER"),
            ("modules", "segment", "VARCHAR(100)"),
            ("modules", "duration_months", "FLOAT"),
            ("tracks", "mapped", "BOOLEAN DEFAULT 0"),
            ("classes", "content_change_description", "TEXT"),
        ]:
            cols = {r[1] for r in conn.execute(text(f"PRAGMA table_info({table})"))}
            if col not in cols:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                added.append(f"{table}.{col}")
    return added


def best_match(target_norm, candidates, used, cutoff):
    """candidates: list of (norm_name, obj). Exact normalised match first, then close fuzzy match."""
    for n, obj in candidates:
        if n == target_norm and id(obj) not in used:
            return obj
    # One title is the other plus a trailing qualifier, e.g. "... (with AI Service Integration)".
    prefixed = [obj for n, obj in candidates
                if id(obj) not in used and len(min(n, target_norm, key=len)) >= 20
                and (n.startswith(target_norm) or target_norm.startswith(n))]
    if len(prefixed) == 1:
        return prefixed[0]
    # The mapping adds a "Series: " prefix the database title lacks, and may extend it, e.g.
    # "Contest" -> "Intermediate DSA: Contest & Contest discussion".
    if ": " in target_norm:
        remainder = target_norm.split(": ", 1)[1]
        prefixless = [obj for n, obj in candidates
                      if id(obj) not in used and len(n) >= 4
                      and (remainder == n or remainder.startswith(n + " "))]
        if len(prefixless) == 1:
            return prefixless[0]
    scored = sorted(
        ((difflib.SequenceMatcher(None, target_norm, n).ratio(), obj) for n, obj in candidates if id(obj) not in used),
        key=lambda x: -x[0],
    )
    if scored and scored[0][0] >= cutoff:
        return scored[0][1]
    return None


def main(apply: bool, program_name: str, track_name: str, mapping_path: str, archive_unmapped: bool = False,
         mark_mapped: bool = True):
    mapping = parse_mapping(mapping_path)
    total_classes = sum(len(m["classes"]) for m in mapping)
    print(f"Track: {program_name} / {track_name}")
    print(f"Mapping: {len(mapping)} modules, {total_classes} classes\n")

    if apply:
        backup = os.path.join(os.path.dirname(DB_FILE), f"scripts.db.bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(DB_FILE, backup)
        print(f"Backup written: {backup}")
    # Needed even for the dry run, because the models select these columns.
    for name in ensure_columns():
        print(f"Added column {name}")

    db = SyncSessionLocal()
    try:
        program = db.query(Program).filter(Program.name == program_name).one()
        track = db.query(Track).filter(Track.program_id == program.id, Track.name == track_name).one()
        other_tracks = db.query(Track).filter(Track.program_id == program.id, Track.id != track.id).all()
        other_modules = db.query(Module).filter(Module.track_id.in_([t.id for t in other_tracks])).all()
        other_candidates = [(norm(m.name), m) for m in other_modules]

        beginner_modules = db.query(Module).filter(Module.track_id == track.id).all()
        mod_candidates = [(norm(m.name), m) for m in beginner_modules]
        used_modules = set()

        stats = {"mod_updated": 0, "mod_created": 0, "cls_updated": 0, "cls_created": 0}
        stale = []

        for pos, spec in enumerate(mapping, start=1):
            key = norm(spec["name"])
            module = best_match(key, mod_candidates, used_modules, cutoff=0.92)
            if module is None and key in MODULE_ALIASES:
                module = best_match(MODULE_ALIASES[key], mod_candidates, used_modules, cutoff=0.92)
            source = None
            if module is None:
                source = best_match(key, other_candidates, set(), cutoff=0.92)
            used_modules.add(id(module)) if module else None

            if module is not None:
                action = "update"
                renamed = f" (rename from '{module.name}')" if clean(module.name) != spec["name"] else ""
                stats["mod_updated"] += 1
            else:
                action = f"create (copy of module #{source.id} from track {source.track_id})" if source else "create (new)"
                stats["mod_created"] += 1
                renamed = ""
                module = Module(
                    track_id=track.id,
                    name=spec["name"],
                    slug=re.sub(r"[^\w]+", "-", spec["name"].lower()).strip("-")[:100],
                    module_type=source.module_type if source else "Core",
                    approved_sources=source.approved_sources if source else None,
                    plan_source=source.plan_source if source else "manual",
                    plan_status=source.plan_status if source else "Locked",
                    is_locked=source.is_locked if source else True,
                    owner_name=source.owner_name if source else None,
                    segment=spec["segment"],
                    duration_months=spec["months"],
                )
                if apply:
                    db.add(module)
                    db.flush()
            print(f"[{pos:02d}] {spec['name']}: {action}{renamed}, {len(spec['classes'])} classes")
            if apply:
                module.name = spec["name"]
                module.sequence_order = pos
                if spec["segment"]:
                    module.segment = spec["segment"]
                if spec["months"] is not None:
                    module.duration_months = spec["months"]

            existing = list(module.classes) if module.id else []
            cls_candidates = [(norm(c.title), c) for c in existing]
            src_candidates = [(norm(c.title), c) for c in (source.classes if source else [])]
            used_cls = set()

            for idx, cs in enumerate(spec["classes"], start=1):
                tnorm = norm(cs["title"])
                cls = best_match(tnorm, cls_candidates, used_cls, cutoff=0.9)
                if cls is not None:
                    used_cls.add(id(cls))
                    stats["cls_updated"] += 1
                    if norm(cls.title) != tnorm:
                        print(f"      rename: '{cls.title}' -> '{cs['title']}'")
                else:
                    stats["cls_created"] += 1
                    src = best_match(tnorm, src_candidates, set(), cutoff=0.9)
                    cls = Class(
                        module_id=module.id,
                        sequence_number=idx,
                        title=cs["title"],
                        duration_minutes=src.duration_minutes if src else 90,
                        agendas=src.agendas if src else json.dumps([
                            f"Conceptual Foundations of {cs['title']}",
                            "Real-World Production Architecture & Failure Modes",
                            "Hands-On Implementation & Live Problem Walkthrough",
                            "Key Misconceptions, FAQs & 90-Minute Mental Model Recap",
                        ]),
                        learning_outcomes=src.learning_outcomes if src else
                            f"Master foundational concepts and industry best practices for {cs['title']}.",
                        module_type=module.module_type,
                    )
                    if apply:
                        db.add(cls)
                if apply:
                    if norm(cls.title) != tnorm:  # keep existing wording when only spacing differs
                        cls.title = cs["title"]
                    cls.sequence_number = idx
                    cls.class_type = cs["class_type"]
                    cls.junction_number = cs["junction"]
                    cls.module_type = module.module_type

            for c in existing:
                if id(c) not in used_cls and c.class_type != "Archived":
                    n_scripts = db.query(Script).filter(Script.class_id == c.id).count()
                    stale.append((spec["name"], c.id, c.title, n_scripts))
                    if apply:  # park outside the official sequence without deleting
                        c.sequence_number = 1000 + c.id
                        if n_scripts == 0:  # a class that still has a script keeps its type so it stays reachable
                            c.class_type = "Archived"

        stale_in_unmapped = [m for m in beginner_modules if id(m) not in used_modules]
        if apply and mark_mapped:
            track.mapped = True  # from now on the app lists this track's modules from the database
            # Modules the mapping does not contain sort after the mapped ones instead of floating to the top.
            for offset, m in enumerate(sorted(stale_in_unmapped, key=lambda x: x.id), start=1):
                m.sequence_order = len(mapping) + offset
            if archive_unmapped:  # hide modules the mapping does not contain; scripted ones stay visible
                for m in stale_in_unmapped:
                    if db.query(Script).join(Class).filter(Class.module_id == m.id).count() == 0:
                        m.module_type = "Archived"

        print("\nSummary")
        for k, v in stats.items():
            print(f"  {k}: {v}")
        print(f"  classes in matched modules not in the mapping: {len(stale)}")
        for mname, cid, title, n in stale:
            print(f"    - [{mname}] class #{cid} '{title}' (scripts: {n}{', kept, not archived' if n else ''})")
        print(f"  modules in this track not in the mapping: {len(stale_in_unmapped)}")
        for m in stale_in_unmapped:
            print(f"    - module #{m.id} '{m.name}'" + (" (archived)" if apply and archive_unmapped and m.module_type == "Archived" else ""))

        if apply:
            db.commit()
            print("\nApplied.")
        else:
            db.rollback()
            print("\nDry run only. Re-run with --apply to write these changes.")
    finally:
        db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--program", default=DEFAULT_PROGRAM, help="program name in the database")
    ap.add_argument("--track", default="Beginner Track", help="track name in the database")
    ap.add_argument("--mapping", default=os.path.join("data", "academy_beginner_mapping.txt"),
                    help="mapping file, relative to backend/")
    ap.add_argument("--apply", action="store_true", help="write changes (default is a dry run)")
    ap.add_argument("--no-mark-mapped", action="store_true",
                    help="with --apply, update rows only and keep the app's existing listing for this track")
    ap.add_argument("--archive-unmapped", action="store_true",
                    help="with --apply, hide modules of this track that the mapping does not contain")
    args = ap.parse_args()
    main(args.apply, args.program, args.track, os.path.join(BACKEND_DIR, args.mapping), args.archive_unmapped,
         not args.no_mark_mapped)
