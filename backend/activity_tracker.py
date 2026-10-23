"""
Records what each signed-in person does, from the requests they make, so the User activity tab covers everything and not only what the pages report.

A request is recorded when it changes something (POST, PUT, PATCH, DELETE), or opens or exports a script. The person is the one named in the request's
user_id / actor_id, or in the X-Actor-Id header every screen now sends. A request that was refused (not allowed, locked module) is recorded too, as
"Action Blocked", so attempts show as well as successes. Actions the server already writes to the log itself are skipped when they succeed so nothing
shows twice, and a request nobody is signed in for is recorded as "Unknown user".
"""

import datetime
import re
from typing import Optional, Tuple

# (method, path pattern, action, what the person did). {name} is filled with the name of the thing the request is about.
ROUTES = [
    ("PUT", r"^/api/classes/(?P<id>\d+)$", "Class Edited", "Edited class '{name}'", "class", "class"),
    ("POST", r"^/api/modules$", "Module Created", "Added a module", None, None),
    ("PUT", r"^/api/modules/(?P<id>\d+)$", "Module Edited", "Changed module '{name}'", "module", "module"),
    ("DELETE", r"^/api/modules/(?P<id>\d+)$", "Module Deleted", "Deleted module '{name}'", "module", None),
    ("POST", r"^/api/tracks$", "Track Created", "Added a track", None, None),
    ("PUT", r"^/api/tracks/(?P<id>\d+)$", "Track Renamed", "Renamed track '{name}'", "track", "track"),
    ("DELETE", r"^/api/tracks/(?P<id>\d+)$", "Track Deleted", "Deleted track '{name}'", "track", None),
    ("POST", r"^/api/programs$", "Program Created", "Added a program", None, None),
    ("PUT", r"^/api/programs/(?P<id>\d+)$", "Program Edited", "Edited program '{name}'", "program", "program"),
    ("DELETE", r"^/api/programs/(?P<id>\d+)$", "Program Deleted", "Deleted program '{name}'", "program", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/outline$", "Outline Generated", "Generated the outline for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/outline/action$", "Outline Updated", "Changed or approved the outline for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/outline/card-assist$", "Outline Card Assist", "Asked the AI about an outline card in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/generate-section$", "Cards Generated", "Generated script cards for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/draft-all$", "Cards Generated", "Drafted all remaining cards for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/review$", "AI Review Run", "Ran the AI review on '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/review/apply$", "AI Fixes Applied", "Applied the AI's fixes to '{name}'", "script", None),
    ("PUT", r"^/api/scripts/(?P<id>\d+)/cards/(?P<card>\d+)$", "Card Edited", "Edited a card in '{name}'", "script", "card"),
    ("POST", r"^/api/scripts/(?P<id>\d+)/chat-assist$", "Card Revised With AI", "Revised a card in '{name}' with the AI", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/cards/\d+/run-code$", "Code Run", "Ran a code example in '{name}'", "script", None),
    ("PUT", r"^/api/scripts/(?P<id>\d+)/cards/\d+/quiz$", "Quiz Edited", "Edited a quiz in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/cards/\d+/quiz/regenerate$", "Quiz Regenerated", "Regenerated a quiz in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/cards/\d+/quiz/approve$", "Quiz Approved", "Approved a quiz in '{name}'", "script", None),
    ("PUT", r"^/api/scripts/(?P<id>\d+)/visuals/[^/]+$", "Visual Edited", "Edited a visual in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/visuals/[^/]+/regenerate$", "Visual Regenerated", "Regenerated a visual in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/visuals/[^/]+/approve$", "Visual Approved", "Approved a visual in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/setup/generate$", "Setup Checklist Made", "Made the class setup checklist for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/sme-items/fill$", "Details Filled In", "Filled in the missing details for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/generate-assignments$", "Assignments Generated", "Generated assignments for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/share/revoke$", "Share Link Revoked", "Switched off the share link for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/share/renew$", "Share Link Renewed", "Renewed the share link for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/share-hackmd$", "Shared To HackMD", "Published '{name}' to HackMD", "script", None),
    ("DELETE", r"^/api/scripts/(?P<id>\d+)$", "Script Deleted", "Deleted the script for '{name}'", "script", None),
    ("GET", r"^/api/scripts/(?P<id>\d+)$", "Script Opened", "Opened the script for '{name}'", "script", None),
    ("GET", r"^/api/scripts/(?P<id>\d+)/export/[a-z]+$", "Script Exported", "Exported the script for '{name}'", "script", None),
]
# Written to the log by the server itself when they succeed, so only a refusal is added here
BLOCKED_ONLY = [
    ("POST", r"^/api/users$", "User Created", "Added a user", None, None),
    ("PUT", r"^/api/users/(?P<id>\d+)$", "User Updated", "Changed a user's access", None, "user"),
    ("DELETE", r"^/api/users/\d+$", "User Deleted", "Deleted a user", None, None),
    ("POST", r"^/api/classes$", "Class Created", "Added a class", None, None),
    ("DELETE", r"^/api/classes/(?P<id>\d+)$", "Class Deleted", "Deleted class '{name}'", "class", None),
    ("POST", r"^/api/modules/(?P<id>\d+)/syllabus$", "Syllabus Parsed", "Uploaded a syllabus", "module", None),
    ("POST", r"^/api/modules/(?P<id>\d+)/plan/submit-review$", "Module Plan In Review", "Reopened a module for review", "module", None),
    ("POST", r"^/api/modules/(?P<id>\d+)/plan/lock$", "Module Plan Locked", "Locked a module plan", "module", None),
    ("POST", r"^/api/scripts/init$", "Script Initialized", "Started a new script", None, None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/approve-gtg$", "Official GTG Sign-Off", "Approved the script for '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/cards/\d+/sources/review$", "Reference Checked", "Checked a reference in '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/revise$", "Script Revision Started", "Started a revision of '{name}'", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/request-changes$", "Changes Requested", "Did not approve '{name}' and asked for changes", "script", None),
    ("POST", r"^/api/scripts/(?P<id>\d+)/submit-mo$", "Submitted to MO Review", "Submitted '{name}' to the Module Owner", "script", None),
    ("POST", r"^/api/auth/login$", "Sign-in Failed", "Tried to sign in with an email that has no account", None, None),
]
COMPILED = [(m, re.compile(p), a, d, k, False, sn) for m, p, a, d, k, sn in ROUTES] + [(m, re.compile(p), a, d, k, True, sn) for m, p, a, d, k, sn in BLOCKED_ONLY]
BLOCKED_STATUSES = (401, 403, 409, 423)
DEDUPE_SECONDS = 5           # one screen can send the same request many times in a moment (saving as you type, reloads); it counts once


def classify(method: str, path: str):
    """(action, text template, kind of thing, its id, only-when-refused, kind to compare before and after, its id) for a request worth recording, else None."""
    for m, rx, action, text, kind, only_blocked, snap in COMPILED:
        if m == method:
            hit = rx.match(path)
            if hit:
                gd = hit.groupdict()
                ident = int(gd["id"]) if gd.get("id") else None
                snap_id = int(gd["card"]) if (snap == "card" and gd.get("card")) else ident
                return action, text, kind, ident, only_blocked, snap, snap_id
    return None


def entity_name(db, kind: Optional[str], ident: Optional[int]) -> str:
    """The name of the thing a request is about (a class title, a module name...), or '#id' when it is gone or unknown."""
    if not kind or ident is None:
        return ""
    from models import Class, Module, Track, Program, Script
    try:
        if kind == "script":
            s = db.query(Script).filter(Script.id == ident).first()
            return (s.custom_class_title or (s.class_entity.title if s.class_entity else "")) if s else f"#{ident}"
        model = {"class": Class, "module": Module, "track": Track, "program": Program}[kind]
        row = db.query(model).filter(model.id == ident).first()
        return (getattr(row, "title", None) or getattr(row, "name", None) or f"#{ident}") if row else f"#{ident}"
    except Exception:
        return f"#{ident}"



# ---- what changed ---------------------------------------------------------------------------------------------------------------------

def _agendas(raw) -> list:
    import json
    try:
        v = json.loads(raw) if isinstance(raw, str) else (raw or [])
        return [str(x) for x in v] if isinstance(v, list) else []
    except ValueError:
        return []


def snapshot(db, kind: Optional[str], ident: Optional[int]) -> Optional[dict]:
    """The fields of a thing worth comparing before and after an edit, with the labels an auditor reads. None when it does not exist."""
    if not kind or ident is None:
        return None
    from models import Class, Module, Track, Program, ScriptCard, User
    try:
        if kind == "class":
            c = db.query(Class).filter(Class.id == ident).first()
            return None if not c else {"Title": c.title, "Duration (min)": c.duration_minutes, "Class type": c.class_type, "Module tag": c.module_type, "Agendas": _agendas(c.agendas)}
        if kind == "module":
            m = db.query(Module).filter(Module.id == ident).first()
            return None if not m else {"Name": m.name, "Module type": m.module_type, "Segment": m.segment, "Duration (months)": m.duration_months,
                                       "Track": m.track.name if m.track else None, "Plan status": m.plan_status}
        if kind == "track":
            t = db.query(Track).filter(Track.id == ident).first()
            return None if not t else {"Name": t.name}
        if kind == "program":
            p = db.query(Program).filter(Program.id == ident).first()
            return None if not p else {"Name": p.name, "Description": p.description if hasattr(p, "description") else None}
        if kind == "card":
            k = db.query(ScriptCard).filter(ScriptCard.id == ident).first()
            if not k:
                return None
            import json
            try:
                quiz = json.loads(k.quiz_json) if k.quiz_json else None
            except ValueError:
                quiz = None
            return {"Title": k.title, "Duration (min)": k.duration_mins, "Card type": k.pedagogical_phase,
                    "Script text": ("words", k.content_script or ""), "Code": ("text", k.code_snippet or ""),
                    "Quiz question": (quiz or {}).get("question") if isinstance(quiz, dict) else None}
        if kind == "user":
            u = db.query(User).filter(User.id == ident).first()
            return None if not u else {"Name": u.name, "Email": u.email, "Role": u.role, "Programs": sorted(p.name for p in u.programs), "Tracks": sorted(t.name for t in u.tracks)}
    except Exception:
        return None
    return None


def _short(v, n: int = 70) -> str:
    t = " ".join(str(v if v is not None else "(empty)").split())
    return t if len(t) <= n else t[:n - 1] + "..."


def diff(before: Optional[dict], after: Optional[dict]) -> list:
    """The changes between two snapshots as short lines: Title: 'A' -> 'B', Agendas: added 'X', removed 'Y'."""
    if not before or not after:
        return []
    out = []
    for label, new in after.items():
        old = before.get(label)
        if isinstance(new, tuple):                              # long text: compare, and say how big, never print it all
            kind, new_text = new
            old_text = old[1] if isinstance(old, tuple) else ""
            if old_text != new_text:
                if kind == "words":
                    a, b = len(old_text.split()), len(new_text.split())
                    out.append(f"{label}: {a} words -> {b} words" if a != b else f"{label}: reworded ({b} words)")
                else:
                    out.append(f"{label}: changed")
        elif isinstance(new, list):
            old_list = old if isinstance(old, list) else []
            added = [x for x in new if x not in old_list]
            removed = [x for x in old_list if x not in new]
            if added or removed:
                bits = [f"added '{_short(x, 50)}'" for x in added[:4]] + [f"removed '{_short(x, 50)}'" for x in removed[:4]]
                out.append(f"{label}: " + ", ".join(bits))
        elif old != new:
            out.append(f"{label}: '{_short(old)}' -> '{_short(new)}'" if not isinstance(new, (int, float)) or isinstance(old, str) else f"{label}: {_short(old)} -> {_short(new)}")
    return out


def details_json(ref: Optional[Tuple[str, int]], changes: Optional[list]) -> Optional[str]:
    """What an entry carries besides its sentence: the thing it is about (so it can be opened) and what changed."""
    import json
    data = {}
    if ref:
        data["ref"] = {"kind": ref[0], "id": ref[1]}
    if changes:
        data["changes"] = changes[:12]
    return json.dumps(data) if data else None


def enrich_explicit(db, action: str, ref, changes) -> bool:
    """For actions the server already logged itself (approvals, user updates): add the link and the changes to that newest entry."""
    from models import UserActivity
    details = details_json(ref, changes)
    if not details:
        return False
    since = datetime.datetime.utcnow() - datetime.timedelta(seconds=15)
    row = db.query(UserActivity).filter(UserActivity.action_type == action, UserActivity.created_at >= since, UserActivity.details.is_(None)) \
        .order_by(UserActivity.id.desc()).first()
    if not row:
        return False
    row.details = details
    db.commit()
    return True


def record(db, method: str, path: str, query: dict, name: str, status: int, header_actor: Optional[str] = None, changes: Optional[list] = None) -> bool:
    """Write the activity for one finished request. Returns True when something was written."""
    found = classify(method, path)
    if not found:
        return False
    action, text, _kind, _ident, only_blocked = found[:5]
    if action == "Sign-in Failed":                     # an email with no account answers 404; that is the attempt itself
        if status < 400:
            return False
        blocked = False
    else:
        blocked = status in BLOCKED_STATUSES
        if only_blocked and not blocked:
            return False                               # the server wrote its own entry for this one (enrich_explicit adds the link and changes to it)
        if status >= 400 and not blocked:
            return False                               # a missing item or a server error is not something the person did
    from models import User, UserActivity
    raw = query.get("user_id") or query.get("actor_id") or header_actor
    try:
        actor = db.query(User).filter(User.id == int(raw)).first() if raw else None
    except (TypeError, ValueError):
        actor = None
    description = text.format(name=name or "")
    if blocked:
        action, description = ("Action Blocked", f"Not allowed ({status}): {description[:1].lower()}{description[1:]}") if action != "Sign-in Failed" else (action, description)
    since = datetime.datetime.utcnow() - datetime.timedelta(seconds=DEDUPE_SECONDS)
    who = actor.name if actor else "Unknown user"
    same = db.query(UserActivity.id).filter(UserActivity.user_name == who, UserActivity.action_type == action,
                                           UserActivity.description == description, UserActivity.created_at >= since).first()
    if same:
        return False
    ref = (found[2], found[3]) if (found[2] and found[3] is not None and "delet" not in action.lower() and status < 400) else None
    db.add(UserActivity(user_name=who, user_role=actor.role if actor else None, action_type=action, description=description, details=details_json(ref, changes)))
    db.commit()
    return True
