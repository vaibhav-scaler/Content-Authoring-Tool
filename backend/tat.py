"""Script turnaround (TAT) and the effort the author puts in, for the Success metrics of the PRD.

TAT of a script = days from its module's kickoff to the day the Module Owner approved it. A module's kickoff is the date the Module Owner or Admin set on it;
where none was set, the day the first script of that module was started (the tool says which one it used).

Author effort is not recorded by a timer. It is worked out from the activity log: for one script, the author's actions are grouped into sessions (a gap of more than
10 minutes starts a new one) and each session counts from its first action to its last, plus 2 minutes for the work after the last action. The PRD leaves the
measurement method open, so this is a stated proxy, shown as such."""
import datetime
import re
import statistics
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from models import Class, Module, Program, Script, Track, UserActivity

SESSION_GAP_MIN = 10
TAIL_MIN = 2


def _plain(name: Optional[str]) -> str:
    return re.sub(r"\s*\((?:SME|MO|PM|Admin)\)\s*$", "", name or "", flags=re.I).strip().lower()


def module_kickoffs(db: Session) -> Dict[int, Tuple[datetime.datetime, str]]:
    """{module_id: (kickoff, how it was found)}. Modules with no date and no script yet are left out."""
    out: Dict[int, Tuple[datetime.datetime, str]] = {}
    first_script = dict(db.query(Class.module_id, func.min(Script.created_at)).join(Script, Script.class_id == Class.id).group_by(Class.module_id).all())
    for m in db.query(Module).all():
        if getattr(m, "kickoff_at", None):
            out[m.id] = (m.kickoff_at, "kickoff date")
        elif first_script.get(m.id):
            out[m.id] = (first_script[m.id], "first script started")
    return out


def tat_days(approved_at: Optional[datetime.datetime], kickoff: Optional[datetime.datetime]) -> Optional[float]:
    if not approved_at or not kickoff:
        return None
    return round(max(0.0, (approved_at - kickoff).total_seconds() / 86400.0), 1)


def author_minutes(db: Session, script: Script) -> int:
    """The author's working time on one script, from the activity log (see the module note)."""
    sid = script.id
    rows = (db.query(UserActivity.user_name, UserActivity.created_at)
            .filter(or_(UserActivity.details.like(f'%"kind": "script", "id": {sid}}}%'), UserActivity.description.like(f"%script #{sid} %"), UserActivity.description.like(f"%script #{sid}'%")))
            .order_by(UserActivity.created_at).all())
    author = _plain(script.sme_name or (script.user.name if script.user else ""))
    times = [t for n, t in rows if t and (not author or _plain(n) == author)]
    if not times:
        return 0
    total, start, last = 0.0, times[0], times[0]
    for t in times[1:]:
        if (t - last).total_seconds() / 60.0 > SESSION_GAP_MIN:
            total += (last - start).total_seconds() / 60.0 + TAIL_MIN
            start = t
        last = t
    total += (last - start).total_seconds() / 60.0 + TAIL_MIN
    return int(round(total))


def compute(db: Session, program_ids: Optional[List[int]] = None, now: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    now = now or datetime.datetime.utcnow()
    kicks = module_kickoffs(db)
    q = (db.query(Script, Class, Module, Track, Program).join(Class, Script.class_id == Class.id).join(Module, Class.module_id == Module.id)
         .join(Track, Module.track_id == Track.id).join(Program, Track.program_id == Program.id))
    if program_ids is not None:
        q = q.filter(Program.id.in_(program_ids))
    progs: Dict[int, Dict[str, Any]] = {}
    for prog in db.query(Program).order_by(Program.id).all():
        if program_ids is None or prog.id in program_ids:
            progs[prog.id] = {"id": prog.id, "name": prog.name, "target_days": getattr(prog, "tat_target_days", None), "approved": 0, "days": [], "minutes": [],
                              "open": 0, "waiting_owner": 0, "oldest_open_days": None, "scripts": [], "modules": {}}
    for script, cls, mod, trk, prog in q.all():
        p = progs[prog.id]
        kick, basis = kicks.get(mod.id, (None, None))
        if script.status == "GTG" and script.approved_at:
            days = tat_days(script.approved_at, kick)
            mins = author_minutes(db, script)
            p["approved"] += 1
            if days is not None:
                p["days"].append(days)
            if mins:
                p["minutes"].append(mins)
            p["scripts"].append({"script_id": script.id, "class_title": script.custom_class_title or cls.title, "module": mod.name, "days": days, "author_minutes": mins,
                                 "approved_at": script.approved_at.isoformat()})
            mm = p["modules"].setdefault(mod.id, {"module_id": mod.id, "name": mod.name, "kickoff": kick.isoformat() if kick else None, "basis": basis, "approved": 0, "days": []})
            mm["approved"] += 1
            if days is not None:
                mm["days"].append(days)
        elif script.cards:                                           # a script someone is working on (outlines with no cards are not drafts)
            p["open"] += 1
            if script.status == "MO Approval":
                p["waiting_owner"] += 1
            if kick:
                age = round((now - kick).total_seconds() / 86400.0, 1)
                p["oldest_open_days"] = age if p["oldest_open_days"] is None else max(p["oldest_open_days"], age)
    out_programs = []
    all_days: List[float] = []
    all_minutes: List[int] = []
    for p in progs.values():
        days, mins, target = p["days"], p["minutes"], p["target_days"]
        all_days += days
        all_minutes += mins
        within = (round(100.0 * sum(1 for d in days if d <= target) / len(days)) if (target and days) else None)
        slowest = sorted([s for s in p["scripts"] if s["days"] is not None], key=lambda s: -s["days"])[:5]
        modules = [{**m, "avg_days": round(sum(m["days"]) / len(m["days"]), 1) if m["days"] else None, "days": None} for m in p["modules"].values()]
        out_programs.append({
            "id": p["id"], "name": p["name"], "target_days": target, "approved": p["approved"], "open": p["open"], "waiting_owner": p["waiting_owner"],
            "avg_days": round(sum(days) / len(days), 1) if days else None, "median_days": round(statistics.median(days), 1) if days else None,
            "within_target_pct": within, "avg_author_minutes": round(sum(mins) / len(mins)) if mins else None,
            "oldest_open_days": p["oldest_open_days"], "slowest": slowest, "modules": sorted(modules, key=lambda m: -(m["avg_days"] or 0))[:12],
        })
    return {"generated_at": now.isoformat(), "programs": out_programs,
            "overall": {"approved": sum(p["approved"] for p in out_programs), "avg_days": round(sum(all_days) / len(all_days), 1) if all_days else None,
                        "avg_author_minutes": round(sum(all_minutes) / len(all_minutes)) if all_minutes else None},
            "notes": {"tat": "Days from the module's kickoff date to the day the Module Owner approved the script. Where a module has no kickoff date, from the day its first script was started.",
                      "author_minutes": f"The author's working time on the script, worked out from the activity log: actions less than {SESSION_GAP_MIN} minutes apart count as one session, plus {TAIL_MIN} minutes after the last one. It is an estimate, not a timer."}}
