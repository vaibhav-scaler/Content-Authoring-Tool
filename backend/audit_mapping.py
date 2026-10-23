"""
Checks that what the running app returns matches the official mapping files, for every user.

Compares, per track, the modules, classes, order, class types, junction numbers and durations from
/api/modules, /api/classes and /api/curriculum/full-tree (for each user), and that each user sees the
right programs. Read-only. Prints "No mapping mismatches found." when everything agrees.

Usage (server running, from backend/). The user directory is Admin-only, so pass an Admin's user id:
    python audit_mapping.py                              # http://127.0.0.1:8000, Admin id 6
    python audit_mapping.py http://127.0.0.1:8765 6
"""
import json, os, re, sys, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)
from sync_academy_mapping import parse_mapping, norm

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ADMIN_ID = sys.argv[2] if len(sys.argv) > 2 else "6"
D = os.path.join(HERE, "data") + os.sep
def get(path):
    with urllib.request.urlopen(BASE + path) as r: return json.load(r)

M = {k: parse_mapping(D + f + ".txt") for k, f in {
    "acad_b": "academy_beginner_mapping", "acad_i": "academy_intermediate_mapping", "acad_a": "academy_advanced_mapping",
    "devops": "devops_mapping", "aiml_b": "aiml_beginner_mapping", "aiml_a": "aiml_advanced_mapping",
    "dsml_b": "dsml_beginner_mapping", "dsml_i": "dsml_intermediate_mapping", "fde": "fde_mapping"}.items()}
# Expected module lists per (program name, track name). DSML Beginner is shown as the 6 + the 16 Intermediate modules.
EXPECT = {
    ("Academy", "Beginner Track"): M["acad_b"], ("Academy", "Intermediate Track"): M["acad_i"], ("Academy", "Advanced Track"): M["acad_a"],
    ("DevOps & Cloud", "Core & Electives Track"): M["devops"],
    ("AI / ML & Agentic AI", "Beginner Track"): M["aiml_b"], ("AI / ML & Agentic AI", "Advanced Track"): M["aiml_a"],
    ("Data Science & Machine Learning (DSML)", "Beginner Track"): M["dsml_b"] + M["dsml_i"],
    ("Data Science & Machine Learning (DSML)", "Intermediate Track"): M["dsml_i"],
    ("AI Forward Deployed Engineer (FDE)", "Enterprise Deployment Track"): M["fde"],
}
EXTRA_OK = {"refresher: lab session on strings"}   # scripted class kept outside the official list
UNMAPPED_OK = {("Academy", "Advanced Track"): {"data engineering"}}   # module not in the Advanced table, deliberately kept

problems = []
def bad(where, msg): problems.append(f"{where}: {msg}")

def check_track(where, prog, trk_name, modules, classes_for):
    exp = EXPECT[(prog, trk_name)]
    got_names = [norm(m["name"]) for m in modules]
    allow = UNMAPPED_OK.get((prog, trk_name), set())
    got_core = [n for n in got_names if n not in allow]
    exp_names = [norm(e["name"]) for e in exp]
    if got_core != exp_names:
        missing = [n for n in exp_names if n not in got_core]; extra = [n for n in got_core if n not in exp_names]
        bad(where, f"module list differs (missing {missing[:3]}, extra {extra[:3]}, order_ok={sorted(got_core)==sorted(exp_names)})")
        return 0
    n_classes = 0
    for m in modules:
        if norm(m["name"]) in allow: continue
        e = next(x for x in exp if norm(x["name"]) == norm(m["name"]))
        cls = [c for c in classes_for(m) if norm(c["title"]) not in EXTRA_OK]
        n_classes += len(cls)
        if [norm(c["title"]) for c in cls] != [norm(c["title"]) for c in e["classes"]]:
            bad(where, f"module '{m['name'][:40]}' class titles/order differ ({len(cls)} vs {len(e['classes'])})"); continue
        for c, ec in zip(cls, e["classes"]):
            if (c["class_type"] or "Live") != ec["class_type"]: bad(where, f"'{c['title'][:35]}' type {c['class_type']} != {ec['class_type']}"); break
            if (c.get("junction_number") or None) != (ec["junction"] or None): bad(where, f"'{c['title'][:35]}' junction {c.get('junction_number')} != {ec['junction']}"); break
        if e["months"] is not None and m.get("duration_label"):
            want = f"{e['months']:g} month" + ("" if e["months"] == 1 else "s")
            if m["duration_label"] != want: bad(where, f"module '{m['name'][:30]}' duration {m['duration_label']} != {want}")
    return n_classes

programs = {p["id"]: p for p in get("/api/programs")}
users = get(f"/api/users?actor_id={ADMIN_ID}")
print("users:", [(u["id"], u["name"], u["role"], u["program_ids"]) for u in users])
total_checked = 0

# (a)(b) generator endpoints, per track
for pid, p in programs.items():
    for t in get(f"/api/tracks?program_id={pid}"):
        if (p["name"], t["name"]) not in EXPECT: bad("tracks", f"unexpected track {p['name']} / {t['name']}"); continue
        mods = get(f"/api/modules?track_id={t['id']}")
        total_checked += check_track(f"[generator API] {p['name']} / {t['name']}", p["name"], t["name"], mods,
                                     lambda m: get(f"/api/classes?module_id={m['id']}"))
# (c) full-tree for every user (what Admin / MO / PM / SME curriculum screens use)
for u in users:
    tree = get(f"/api/curriculum/full-tree?user_id={u['id']}")
    seen = {p["id"] for p in tree}
    expect_ids = set(u["program_ids"]) if u["role"] in ("sme", "mo") else set(programs)
    if seen != expect_ids: bad(f"[tree user {u['id']} {u['role']}]", f"programs {sorted(seen)} != expected {sorted(expect_ids)}")
    for p in tree:
        for t in p["tracks"]:
            check_track(f"[tree user {u['id']} {u['role']}] {p['name']} / {t['name']}", p["name"], t["name"], t["modules"], lambda m: m["classes"])
# (d) programs list per user
for u in users:
    got = {p["id"] for p in get(f"/api/programs?user_id={u['id']}")}
    exp = set(u["program_ids"]) if u["role"] in ("sme", "mo") else set(programs)
    if got != exp: bad(f"[programs user {u['id']} {u['role']}]", f"{sorted(got)} != {sorted(exp)}")

print(f"\nclasses compared against mapping files (generator API): {total_checked}")
print("PROBLEMS:" if problems else "No mapping mismatches found.")
for x in problems: print(" -", x)
