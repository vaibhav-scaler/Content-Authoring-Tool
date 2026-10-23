import os
import json
import re
import uuid
import datetime
from typing import Any, Dict, List, Optional
from fastapi import FastAPI, Depends, HTTPException, Query, status, Body
from pydantic import BaseModel
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session
from sqlalchemy import select, or_, func

from database import Base, sync_engine, SyncSessionLocal
from models import Program, Track, Module, Class, Script, ScriptCard, ReviewAudit, MemoryLedger, VisualAsset, User, UserActivity, ScriptEvent, APIUsageLog
from schemas import (
    LoginRequest, QuizEditRequest, QuizRegenerateRequest, CodeTryRequest,
    ProgramOut, TrackOut, ModuleOut, ClassOut,
    ScriptCreateRequest, OutlineResponse, OutlineApprovalRequest,
    MultiAgentReviewResponse, ScriptDetailResponse, RequestChangesBody, SourceReviewRequest, ScriptCardOut,
    CardUpdateRequest, ChatAssistRequest, AssignmentResponse,
    ClassUpdate, ClassCreate, ModuleUpdate,
    ProgramCreate, ProgramUpdate, TrackCreate, TrackUpdate, ModuleCreate,
    UserOut, UserCreate, UserUpdate, ActivityLogCreate, ActivityBatch, UserActivityOut,
    OutlineCardAssistRequest
)
from agents.memory_engine import retrieve_context_memory, save_memory_ledger
from agents.writer import generate_outline, draft_card_batch
from agents.reviewer import run_multi_lens_reviews, run_single_lens_review
from agents.moderator import reconcile_and_repair
from agents.attributes_generator import generate_mermaid_diagram, generate_html_animation, generate_image_asset

# Create DB tables
Base.metadata.create_all(bind=sync_engine)

# Lightweight migration for FR-3/4/6 module columns
def _migrate_module_columns():
    from sqlalchemy import text
    with sync_engine.begin() as conn:
        existing = {row[1] for row in conn.execute(text("PRAGMA table_info(modules)"))}
        for col, ddl in [
            ("plan_source", "ALTER TABLE modules ADD COLUMN plan_source VARCHAR(20) DEFAULT 'manual'"),
            ("plan_status", "ALTER TABLE modules ADD COLUMN plan_status VARCHAR(20) DEFAULT 'Locked'"),
            ("owner_name", "ALTER TABLE modules ADD COLUMN owner_name VARCHAR(255)"),
            ("segment", "ALTER TABLE modules ADD COLUMN segment VARCHAR(100)"),
            ("duration_months", "ALTER TABLE modules ADD COLUMN duration_months FLOAT"),
            ("kickoff_at", "ALTER TABLE modules ADD COLUMN kickoff_at DATETIME"),
        ]:
            if col not in existing:
                conn.execute(text(ddl))
        for table, col, ddl in [
            ("classes", "content_change_description", "ALTER TABLE classes ADD COLUMN content_change_description TEXT"),
            ("programs", "tat_target_days", "ALTER TABLE programs ADD COLUMN tat_target_days INTEGER"),
            ("classes", "reference_script", "ALTER TABLE classes ADD COLUMN reference_script TEXT"),
            ("classes", "reference_script_name", "ALTER TABLE classes ADD COLUMN reference_script_name VARCHAR(255)"),
        ]:
            cols = {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}
            if col not in cols:
                conn.execute(text(ddl))
        track_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(tracks)"))}
        if "mapped" not in track_cols:
            conn.execute(text("ALTER TABLE tracks ADD COLUMN mapped BOOLEAN DEFAULT 0"))
        script_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(scripts)"))}
        if "share_expires_at" not in script_cols:
            conn.execute(text("ALTER TABLE scripts ADD COLUMN share_expires_at DATETIME"))
        ledger_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(memory_ledgers)"))}
        if ledger_cols and "summary_json" not in ledger_cols:
            conn.execute(text("ALTER TABLE memory_ledgers ADD COLUMN summary_json TEXT"))
        if "learning_outcomes" not in script_cols:
            conn.execute(text("ALTER TABLE scripts ADD COLUMN learning_outcomes TEXT"))
        if "setup_json" not in script_cols:
            conn.execute(text("ALTER TABLE scripts ADD COLUMN setup_json TEXT"))
        if "share_revoked_at" not in script_cols:
            conn.execute(text("ALTER TABLE scripts ADD COLUMN share_revoked_at DATETIME"))
        for col, ddl in (("revision_note", "TEXT"), ("revision_by", "VARCHAR(255)"), ("revision_at", "DATETIME"), ("regenerate_pending", "BOOLEAN DEFAULT 0"),
                          ("review_lenses", "TEXT"), ("review_json", "TEXT"), ("review_at", "DATETIME"), ("review_passed", "BOOLEAN"), ("review_score", "FLOAT")):
            if col not in script_cols:
                conn.execute(text(f"ALTER TABLE scripts ADD COLUMN {col} {ddl}"))
        usage_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(api_usage_logs)"))}
        if usage_cols:
            if "cost_source" not in usage_cols:
                conn.execute(text("ALTER TABLE api_usage_logs ADD COLUMN cost_source VARCHAR(20) DEFAULT 'table'"))
            if "error_message" not in usage_cols:
                conn.execute(text("ALTER TABLE api_usage_logs ADD COLUMN error_message VARCHAR(300)"))
        card_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(script_cards)"))}
        if "sources_json" not in card_cols:
            conn.execute(text("ALTER TABLE script_cards ADD COLUMN sources_json TEXT"))
        if "code_run_json" not in card_cols:
            conn.execute(text("ALTER TABLE script_cards ADD COLUMN code_run_json TEXT"))
        class_cols = {row[1] for row in conn.execute(text("PRAGMA table_info(classes)"))}
        if "junction_number" not in class_cols:
            conn.execute(text("ALTER TABLE classes ADD COLUMN junction_number INTEGER"))

_migrate_module_columns()

from fastapi.responses import JSONResponse
from agents.llm import LLMUnavailable, bind_usage_script, call_llm_chat, call_llm_json, llm_configured

app = FastAPI(
    title="Scaler AI Lecture Script Generator API",
    description="Multi-agent authoring, memory ledger, and quality audit engine for Scaler masterclass scripts.",
    version="1.0.0"
)

@app.exception_handler(LLMUnavailable)
async def llm_unavailable_handler(request, exc):
    return JSONResponse(status_code=503, content={"detail": str(exc)})

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Static files for visual assets
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
os.makedirs(STATIC_DIR, exist_ok=True)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
from agents.image_gen import GENERATED_DIR as _GENERATED_DIR
os.makedirs(_GENERATED_DIR, exist_ok=True)
app.mount("/generated", StaticFiles(directory=_GENERATED_DIR), name="generated")     # pictures made by the image model

def get_db():
    db = SyncSessionLocal()
    try:
        yield db
    finally:
        db.close()

# ==============================================================================
# 1. CURRICULUM & CASCADING SELECTION ENDPOINTS
# ==============================================================================

@app.get("/api/health")
def health_check():
    return {"status": "ok", "service": "Scaler Script Generator API", "version": "1.0.0"}

# ---- AI providers: what is configured, and a one-call check that each key works (Administrators only)

@app.get("/api/system/ai-status")
def ai_status(actor_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Which provider does which job, its model, and whether its key is set. Never returns a key."""
    _require_admin(db, actor_id, "view the AI settings")
    from agents.llm import provider_status
    return {
        **provider_status(),
        "execution_mode": os.getenv("EXECUTION_MODE", "hybrid"),
        "how_to_switch_on": "Add OPENROUTER_API_KEY (recommended for Sonnet) or ANTHROPIC_API_KEY to backend/.env and restart.",
    }


@app.get("/api/llm/usage")
def get_llm_usage(actor_id: Optional[int] = None, days: int = 0, db: Session = Depends(get_db)):
    """Token use, time and cost of the model calls, by script, agent and model. days=0 is all time, otherwise the last N days.
    Every call is counted exactly once: calls that belong to no script are shown on their own 'not linked' row, so the rows add up
    to the totals."""
    import datetime as _dt
    from sqlalchemy import case, func
    from models import APIUsageLog
    from agents.llm import budget_status, price_list

    days = max(0, min(int(days or 0), 3650))
    since = _dt.datetime.utcnow() - _dt.timedelta(days=days) if days else None

    def scoped(query):
        return query.filter(APIUsageLog.created_at >= since) if since else query

    failed = APIUsageLog.status != "success"
    t = scoped(db.query(
        func.count(APIUsageLog.id), func.coalesce(func.sum(APIUsageLog.prompt_tokens), 0),
        func.coalesce(func.sum(APIUsageLog.completion_tokens), 0), func.coalesce(func.sum(APIUsageLog.total_tokens), 0),
        func.coalesce(func.sum(APIUsageLog.estimated_cost_usd), 0.0), func.coalesce(func.avg(case((failed, None), else_=APIUsageLog.latency_seconds)), 0.0),
        func.coalesce(func.sum(case((failed, 1), else_=0)), 0),
        func.coalesce(func.sum(case(((APIUsageLog.cost_source == "unknown") & ~failed, 1), else_=0)), 0),
    )).one()
    total_calls, total_prompt, total_completion, total_tokens, total_cost, avg_latency, error_calls, unknown_price_calls = t

    def agg(*group_cols):
        return scoped(db.query(
            *group_cols, func.count(APIUsageLog.id).label("calls"),
            func.coalesce(func.sum(APIUsageLog.prompt_tokens), 0).label("prompt_tokens"),
            func.coalesce(func.sum(APIUsageLog.completion_tokens), 0).label("completion_tokens"),
            func.coalesce(func.sum(APIUsageLog.total_tokens), 0).label("total_tokens"),
            func.coalesce(func.sum(APIUsageLog.estimated_cost_usd), 0.0).label("cost_usd"),
            func.coalesce(func.sum(case((failed, 1), else_=0)), 0).label("errors"),
        )).group_by(*group_cols)

    def row_out(row, **extra):
        return {**extra, "calls": row.calls, "prompt_tokens": int(row.prompt_tokens), "completion_tokens": int(row.completion_tokens),
                "total_tokens": int(row.total_tokens), "cost_usd": round(float(row.cost_usd), 5), "errors": int(row.errors)}

    by_agent = [row_out(r, agent=r.agent or "general") for r in agg(APIUsageLog.agent).all()]
    by_agent.sort(key=lambda r: r["cost_usd"], reverse=True)

    by_model_rows = agg(APIUsageLog.provider, APIUsageLog.model, APIUsageLog.cost_source).all()
    by_model = [row_out(r, provider=r.provider, model=r.model, cost_source=r.cost_source or "table") for r in by_model_rows]
    by_model.sort(key=lambda r: r["cost_usd"], reverse=True)

    # per script, with what each agent did for it
    script_rows = agg(APIUsageLog.script_id).filter(APIUsageLog.script_id.isnot(None)).all()
    touch_rows = agg(APIUsageLog.script_id, APIUsageLog.agent).filter(APIUsageLog.script_id.isnot(None)).all()
    touchpoints: Dict[int, List[Dict[str, Any]]] = {}
    for r in touch_rows:
        touchpoints.setdefault(r.script_id, []).append(row_out(r, agent=r.agent or "general"))
    scripts = {sc.id: sc for sc in db.query(Script).filter(Script.id.in_([r.script_id for r in script_rows])).all()} if script_rows else {}
    by_script = []
    for r in script_rows:
        sc = scripts.get(r.script_id)
        title = (sc.class_entity.title if sc is not None and sc.class_entity is not None else f"Script #{r.script_id} (deleted)")
        by_script.append(row_out(r, script_id=r.script_id, title=title, status=(sc.status if sc is not None else "Deleted"),
                                 agent_touchpoints=sorted(touchpoints.get(r.script_id, []), key=lambda x: -x["cost_usd"])))
    by_script.sort(key=lambda r: r["cost_usd"], reverse=True)
    attributed_calls = sum(r["calls"] for r in by_script)
    attributed_cost = sum(r["cost_usd"] for r in by_script)
    unlinked = agg(APIUsageLog.script_id).filter(APIUsageLog.script_id.is_(None)).first()
    if unlinked is not None and unlinked.calls:
        by_script.append(row_out(unlinked, script_id=None, title="Not linked to a script (setup checks, syllabus import, anything outside a script)",
                                 status="-", agent_touchpoints=[]))

    recent_rows = scoped(db.query(APIUsageLog)).order_by(APIUsageLog.id.desc()).limit(25).all()
    recent = [{
        "id": l.id, "script_id": l.script_id, "provider": l.provider, "model": l.model, "agent": l.agent,
        "prompt_tokens": l.prompt_tokens, "completion_tokens": l.completion_tokens, "total_tokens": l.total_tokens,
        "estimated_cost_usd": round(l.estimated_cost_usd or 0.0, 6), "cost_source": l.cost_source or "table",
        "latency_seconds": round(l.latency_seconds or 0.0, 2), "status": l.status, "error_message": l.error_message,
        "created_at": l.created_at.isoformat() if l.created_at else None,
    } for l in recent_rows]

    # the prices behind the numbers: only for models that actually appear
    prices = price_list()
    pricing = []
    for m in by_model:
        known = prices.get(m["model"])
        pricing.append({"model": m["model"], "provider": m["provider"], "calls": m["calls"], "source": m["cost_source"],
                        "input_per_million": known[0] if known else None, "output_per_million": known[1] if known else None})

    return {
        "summary": {
            "days": days, "since": since.isoformat() if since else None,
            "total_scripts_in_system": db.query(Script).count(), "total_scripts_measured": len(scripts),
            "total_calls": total_calls, "total_prompt_tokens": int(total_prompt), "total_completion_tokens": int(total_completion),
            "total_tokens": int(total_tokens), "total_cost_usd": round(float(total_cost), 4), "avg_latency_seconds": round(float(avg_latency), 2),
            "error_calls": int(error_calls), "error_rate": round(error_calls / total_calls, 3) if total_calls else 0.0,
            "unknown_price_calls": int(unknown_price_calls),
            "budget": budget_status(),
            "unlinked_calls": total_calls - attributed_calls, "unlinked_cost_usd": round(float(total_cost) - attributed_cost, 5),
        },
        "pricing": pricing,
        "by_script": by_script,
        "by_agent": by_agent,
        "by_model": by_model,
        "recent_logs": recent,
    }


@app.post("/api/system/ai-test")
def ai_test(actor_id: Optional[int] = None, image: bool = False, db: Session = Depends(get_db)):
    """Sends one tiny request to Claude and to Gemini (and makes one small picture when image=true). Use it right after
    adding a key. Reports what answered and how long it took, or why it could not."""
    import time as _time
    _require_admin(db, actor_id, "run the AI check")
    from agents.llm import LLMUnavailable as _Down, call_llm_chat, claude_model, gemini_text_model

    def ping(provider: str, model: str):
        started = _time.time()
        try:
            reply = call_llm_chat(messages=[{"role": "user", "content": "Reply with exactly the word OK."}],
                                  max_tokens=50, temperature=0, provider=provider, agent="ai_test")
            return {"ok": True, "model": model, "seconds": round(_time.time() - started, 1), "reply": reply[:40]}
        except _Down as e:
            return {"ok": False, "model": model, "problem": str(e)}

    result = {"script_and_animation": ping("anthropic", claude_model()), "mermaid_flows": ping("gemini", gemini_text_model()), "images": None}
    if image:
        from agents.image_gen import ImageUnavailable, build_image_prompt, generate_image, image_configured, image_model
        started = _time.time()
        if not image_configured():
            result["images"] = {"ok": False, "model": image_model(), "problem": "GEMINI_API_KEY is not configured."}
        else:
            try:
                data, mime = generate_image(build_image_prompt({"intent": "a simple three-box flow: Question, Query, Answer", "nodes": ["Question", "Query", "Answer"]}))
                result["images"] = {"ok": True, "model": image_model(), "bytes": len(data), "mime": mime, "seconds": round(_time.time() - started, 1)}
            except ImageUnavailable as e:
                result["images"] = {"ok": False, "model": image_model(), "problem": str(e)}
    return result


# ==============================================================================
# DASHBOARD METRICS & SCRIPTS QUEUE ENDPOINTS (PRD USERS & ROLES)
# ==============================================================================

@app.get("/api/metrics/tat")
def script_turnaround(user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Script TAT and author effort per program (PRD Success metrics). Administrators and Program Managers see every program, a Module Owner their own."""
    import tat
    actor = _require_actor(db, user_id)
    if actor.role not in ("admin", "pm", "mo"):
        raise HTTPException(status_code=403, detail="Turnaround figures are for Administrators, Program Managers and Module Owners.")
    ids = [p.id for p in actor.programs] if actor.role == "mo" else None
    return tat.compute(db, ids)


@app.get("/api/dashboard/stats")
def get_dashboard_stats(db: Session = Depends(get_db)):
    """Returns high-level curriculum and script progress metrics for PM/Admin/MO dashboards."""
    total_progs = db.query(Program).count()
    total_tracks = db.query(Track).count()
    total_mods = db.query(Module).filter(or_(Module.module_type.is_(None), Module.module_type != "Archived")).count()
    total_classes = (
        db.query(Class).join(Module, Module.id == Class.module_id)
        .filter(or_(Class.class_type.is_(None), Class.class_type != "Archived"),
                or_(Module.module_type.is_(None), Module.module_type != "Archived"))
        .count()
    )
    total_scripts = db.query(Script).count()
    gtg_scripts = db.query(Script).filter(Script.status == "GTG").count()
    wip_scripts = db.query(Script).filter(Script.status.in_(["Script WIP", "SME Review", "Not Started"])).count()
    
    return {
        "total_programs": total_progs,
        "total_tracks": total_tracks,
        "total_modules": total_mods,
        "total_classes": total_classes,
        "total_scripts": total_scripts,
        "gtg_scripts": gtg_scripts,
        "wip_scripts": wip_scripts,
        "gtg_completion_pct": round((gtg_scripts / max(1, total_classes)) * 100, 2)
    }

# ==============================================================================
# USER AND ROLE ACCESS MANAGEMENT (PRD USERS & ROLES)
# ==============================================================================

@app.get("/api/users", response_model=List[UserOut])
def list_users(role: Optional[str] = None, actor_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Lists registered users with their programs and tracks. Admin only: it exposes every email address."""
    _require_admin(db, actor_id, "view the user directory")
    query = db.query(User)
    if role:
        query = query.filter(User.role == role)
    return [_user_out(u) for u in query.order_by(User.id).all()]

@app.post("/api/auth/login", response_model=UserOut)
def login_by_email(payload: LoginRequest, db: Session = Depends(get_db)):
    """Looks up one account by company email, so the browser never has to download the user directory."""
    email = (payload.email or "").strip().lower()
    u = db.query(User).filter(func.lower(User.email) == email).first() if email else None
    if not u:
        raise HTTPException(status_code=404, detail="No account found for that email.")
    return _user_out(u)

@app.get("/api/users/{user_id}", response_model=UserOut)
def get_user(user_id: int, actor_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Returns one user. A user can read their own profile; Admins can read anyone's."""
    actor = _require_actor(db, actor_id)
    if actor.id != user_id and actor.role != "admin":
        raise HTTPException(status_code=403, detail="You can only view your own profile.")
    u = db.query(User).filter(User.id == user_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="User not found")
    return _user_out(u)

@app.post("/api/users", response_model=UserOut)
def create_user(payload: UserCreate, actor_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Creates a new user with role and joined programs. Admin only."""
    _require_admin(db, actor_id, "manage users")
    existing = db.query(User).filter(User.email == payload.email).first()
    if existing:
        raise HTTPException(status_code=400, detail="User with this email already exists")
    if payload.role not in ("sme", "mo", "pm", "admin"):
        raise HTTPException(status_code=400, detail="Role must be sme, mo, pm or admin.")
    user = User(
        name=payload.name,
        email=payload.email,
        role=payload.role
    )
    _apply_memberships(db, user, payload.program_ids, payload.track_ids)
    db.add(user)
    db.commit()
    db.refresh(user)
    log_activity(db, user_name="Admin", user_role="admin", action_type="User Created", description=f"Enrolled new user '{user.name}' ({user.role.upper()}) with {len(user.programs)} programs and {len(user.tracks)} tracks")
    return _user_out(user)

@app.put("/api/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, actor_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Updates user details, role, and joined program memberships. Admin only."""
    _require_admin(db, actor_id, "manage users")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.role is not None and payload.role != "admin" and user.role == "admin" \
            and db.query(User).filter(User.role == "admin", User.id != user.id).count() == 0:
        raise HTTPException(status_code=400, detail="At least one Administrator must remain.")
    if payload.role is not None and payload.role not in ("sme", "mo", "pm", "admin"):
        raise HTTPException(status_code=400, detail="Role must be sme, mo, pm or admin.")
    if payload.name is not None:
        user.name = payload.name
    if payload.email is not None:
        user.email = payload.email
    if payload.role is not None:
        user.role = payload.role
    if payload.program_ids is not None or payload.track_ids is not None or payload.role is not None:
        program_ids = payload.program_ids if payload.program_ids is not None else [p.id for p in user.programs]
        track_ids = payload.track_ids if payload.track_ids is not None else [t.id for t in user.tracks]
        _apply_memberships(db, user, program_ids, track_ids)
    db.commit()
    db.refresh(user)
    log_activity(db, user_name="Admin", user_role="admin", action_type="User Updated", description=f"Updated scope for user '{user.name}' ({user.role.upper()})")
    return _user_out(user)

@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, actor_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Removes a user. Admin only."""
    _require_admin(db, actor_id, "manage users")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.role == "admin" and db.query(User).filter(User.role == "admin", User.id != user.id).count() == 0:
        raise HTTPException(status_code=400, detail="At least one Administrator must remain.")
    name = user.name
    db.delete(user)
    db.commit()
    log_activity(db, user_name="Admin", user_role="admin", action_type="User Deleted", description=f"Deleted user account: {name}")
    return {"success": True, "message": f"User '{name}' deleted successfully."}

def log_activity(db: Session, user_name: str, action_type: str, description: str, user_role: Optional[str] = None, details: Optional[str] = None):
    try:
        act = UserActivity(
            user_name=user_name,
            user_role=user_role,
            action_type=action_type,
            description=description,
            details=details
        )
        db.add(act)
        db.commit()
    except Exception as e:
        print(f"Error logging activity: {e}")

@app.middleware("http")
async def track_user_activity(request, call_next):
    """Record what each signed-in person does (see activity_tracker.py), with what changed and a link to the item. It never changes the response."""
    from activity_tracker import classify, entity_name, record, snapshot, diff, enrich_explicit
    path, method = request.url.path, request.method
    found = classify(method, path) if path.startswith("/api/") else None
    name, before = "", None
    if found and (method == "DELETE" or found[5]):          # after a delete the name is gone, and an edit needs the old values: read them first
        db0 = SyncSessionLocal()
        try:
            if method == "DELETE":
                name = entity_name(db0, found[2], found[3])
            before = snapshot(db0, found[5], found[6]) if found[5] else None
        finally:
            db0.close()
    response = await call_next(request)
    if found:
        db1 = SyncSessionLocal()
        try:
            if not name:
                name = entity_name(db1, found[2], found[3])
            ok = response.status_code < 400
            changes = diff(before, snapshot(db1, found[5], found[6])) if (before and ok) else None
            if found[4] and ok:                              # the server wrote its own entry; add the link and what changed to it
                ref = (found[2], found[3]) if (found[2] and found[3] is not None and "delet" not in found[0].lower()) else None
                enrich_explicit(db1, found[0], ref, changes)
            else:
                record(db1, method, path, dict(request.query_params), name, response.status_code, request.headers.get("x-actor-id"), changes)
        except Exception as e:
            print(f"Activity tracking skipped: {e}")
        finally:
            db1.close()
    return response


def _activity_program_id(db: Session, act: UserActivity, cache: dict) -> Optional[int]:
    """The program an activity entry is about: from the thing it links to (script, class, module, track, program), else from "script #N" in its sentence.
    None when it cannot be told (sign-ins, clicks, things since deleted), and such entries are never shown to a Module Owner."""
    import json
    kind = ref_id = None
    try:
        info = json.loads(act.details) if act.details else {}
        ref = info.get("ref") or {}
        kind, ref_id = ref.get("kind"), ref.get("id")
    except (ValueError, AttributeError):
        pass
    if not kind:
        m = re.search(r"script #(\d+)", act.description or "", re.I)
        if m:
            kind, ref_id = "script", int(m.group(1))
    if not kind or not isinstance(ref_id, int):
        return None
    key = (kind, ref_id)
    if key in cache:
        return cache[key]
    prog = None
    if kind == "program":
        prog = ref_id if db.query(Program.id).filter(Program.id == ref_id).first() else None
    elif kind == "track":
        t = db.query(Track).filter(Track.id == ref_id).first()
        prog = t.program_id if t else None
    elif kind == "module":
        mo = db.query(Module).filter(Module.id == ref_id).first()
        prog = mo.track.program_id if mo and mo.track else None
    elif kind == "class":
        c = db.query(Class).filter(Class.id == ref_id).first()
        prog = c.module.track.program_id if c and c.module and c.module.track else None
    elif kind == "script":
        sc = db.query(Script).filter(Script.id == ref_id).first()
        prog = _script_program_id(sc) if sc else None
    cache[key] = prog
    return prog


@app.get("/api/activities", response_model=List[UserActivityOut])
def get_user_activities(limit: int = 200, since_id: Optional[int] = None, types: Optional[str] = None, for_user: Optional[int] = None, db: Session = Depends(get_db)):
    """Returns recent activities done across the platform. `since_id` returns only entries newer than that id; `types` is a comma-separated list of
    action-type beginnings (for example "Script Initialized,Official GTG"), so a screen can ask for just the work it shows.
    `for_user` narrows the list for a Module Owner to entries about the programs that person is assigned to."""
    query = db.query(UserActivity)
    if since_id:
        query = query.filter(UserActivity.id > since_id)
    prefixes = [t.strip().lower() for t in (types or "").split(",") if t.strip()]
    if prefixes:
        query = query.filter(or_(*[func.lower(UserActivity.action_type).like(pre.replace("%", "") + "%") for pre in prefixes]))
    limit = max(1, min(limit, 5000))
    viewer = db.query(User).filter(User.id == for_user).first() if for_user else None
    if for_user and viewer is None:
        raise HTTPException(status_code=401, detail="Sign in required: unknown user.")
    if viewer is None or viewer.role != "mo":
        return query.order_by(UserActivity.id.desc()).limit(limit).all()
    mine = {p.id for p in viewer.programs}
    out, cache, last_id = [], {}, None
    while len(out) < limit:                                      # read the log in pages, newest first, keeping only this owner's programs
        page_q = query if last_id is None else query.filter(UserActivity.id < last_id)
        page = page_q.order_by(UserActivity.id.desc()).limit(500).all()
        if not page:
            break
        last_id = page[-1].id
        out += [a for a in page if _activity_program_id(db, a, cache) in mine][:limit - len(out)]
        if len(page) < 500 or len(cache) > 20000:
            break
    return out

@app.post("/api/activities", response_model=UserActivityOut)
def record_user_activity(payload: ActivityLogCreate, db: Session = Depends(get_db)):
    """Allows front-end clients to log notable user actions directly."""
    act = UserActivity(
        user_name=payload.user_name,
        user_role=payload.user_role,
        action_type=payload.action_type,
        description=payload.description,
        details=payload.details
    )
    db.add(act)
    db.commit()
    db.refresh(act)
    return act

CLIENT_ACTIVITY_TYPES = ("Click", "Filter Changed", "Search")      # what the browser may report in bulk; approvals and the like are only ever written by the server


@app.post("/api/activities/batch")
def record_activity_batch(payload: ActivityBatch, db: Session = Depends(get_db)):
    """The clicks, filter changes and searches a signed-in person makes on the screens, sent a few at a time.
    The name and role come from the user's account, not from the browser."""
    actor = db.query(User).filter(User.id == payload.actor_id).first()
    if actor is None:
        raise HTTPException(status_code=401, detail="Sign in required: no such user.")
    written = 0
    for item in payload.items[:50]:
        if item.action_type not in CLIENT_ACTIVITY_TYPES or not item.description.strip():
            continue
        db.add(UserActivity(user_name=actor.name, user_role=actor.role, action_type=item.action_type, description=item.description.strip()[:300]))
        written += 1
    db.commit()
    return {"recorded": written}


@app.get("/api/dashboard/scripts")
def list_dashboard_scripts(
    user_id: Optional[int] = None,
    program_id: Optional[int] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Returns detailed scripts list with hierarchy, scoped by user role and joined programs."""
    query = db.query(Script).join(Class, Script.class_id == Class.id)\
                            .join(Module, Class.module_id == Module.id)\
                            .join(Track, Module.track_id == Track.id)\
                            .join(Program, Track.program_id == Program.id)
    
    if user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.role in ["sme", "mo"]:
            prog_ids = [p.id for p in user.programs]
            query = query.filter(Program.id.in_(prog_ids))

    if program_id:
        query = query.filter(Program.id == program_id)
    if status:
        query = query.filter(Script.status == status)
    viewer = db.query(User).filter(User.id == user_id).first() if user_id else None
    if not (viewer and viewer.role == "admin"):
        # An outline with no script written yet is not a draft: only scripts that have cards (or were approved) are listed. Administrators still see every row.
        query = query.filter(or_(Script.status == "GTG", db.query(ScriptCard.id).filter(ScriptCard.script_id == Script.id).exists()))

    scripts = query.order_by(Script.id.desc()).all()
    all_users = db.query(User).all()
    role_by_name = {u.name.strip().lower(): u.role for u in all_users if u.name}      # for scripts that only kept the person's name
    owners_by_program = {}                                                          # the Module Owners each program has, so a waiting script can say who it is waiting for
    for u in all_users:
        if u.role == "mo":
            for pr in u.programs:
                owners_by_program.setdefault(pr.id, []).append(u.name)
    actor = db.query(User).filter(User.id == user_id).first() if user_id else None
    is_admin = bool(actor and actor.role == "admin")
    plain = lambda n: re.sub(r"\s*\((?:SME|MO|PM|Admin)\)\s*$", "", n or "", flags=re.I).strip() or None      # older scripts saved "Ankit Sharma (SME)"
    import tat as _tat_module
    kicks = _tat_module.module_kickoffs(db)                   # when each module's work began, for script TAT
    ids = [s.id for s in scripts]
    cost_by, usage_last, event_last, basis_by, submitted_at = {}, {}, {}, {}, {}
    if ids:
        if is_admin:                                    # what a script cost to generate is shown to administrators only
            for sid, total, last in db.query(APIUsageLog.script_id, func.sum(APIUsageLog.estimated_cost_usd), func.max(APIUsageLog.created_at)) \
                    .filter(APIUsageLog.script_id.in_(ids)).group_by(APIUsageLog.script_id).all():
                cost_by[sid], usage_last[sid] = float(total or 0), last
            for sid, source, n in db.query(APIUsageLog.script_id, APIUsageLog.cost_source, func.count(APIUsageLog.id)) \
                    .filter(APIUsageLog.script_id.in_(ids), APIUsageLog.cost_source != "provider").group_by(APIUsageLog.script_id, APIUsageLog.cost_source).all():
                basis_by.setdefault(sid, {})[source or "unknown"] = n          # calls the provider did not price itself
        else:
            for sid, last in db.query(APIUsageLog.script_id, func.max(APIUsageLog.created_at)).filter(APIUsageLog.script_id.in_(ids)).group_by(APIUsageLog.script_id).all():
                usage_last[sid] = last
        for sid, last in db.query(ScriptEvent.script_id, func.max(ScriptEvent.created_at)).filter(ScriptEvent.script_id.in_(ids)).group_by(ScriptEvent.script_id).all():
            event_last[sid] = last
        for sid, at in db.query(ScriptEvent.script_id, func.max(ScriptEvent.created_at)).filter(ScriptEvent.script_id.in_(ids), ScriptEvent.event_type == "Submitted for MO Review").group_by(ScriptEvent.script_id).all():
            submitted_at[sid] = at
    out = []
    for s in scripts:
        c = s.class_entity
        mod = c.module if c else None
        trk = mod.track if mod else None
        prog = trk.program if trk else None
        out.append({
            "id": s.id,
            "uuid": s.shareable_uuid,
            "status": s.status,
            "duration_minutes": s.duration_minutes,
            "class_id": s.class_id,
            "class_title": c.title if c else "Unknown",
            "module_name": mod.name if mod else "Unknown",
            "track_name": trk.name if trk else "Unknown",
            "program_name": prog.name if prog else "Unknown",
            "program_id": prog.id if prog else None,
            "owners": sorted(owners_by_program.get(prog.id, [])) if prog else [],
            "sme_name": plain(s.sme_name) or (s.user.name if s.user else None),
            "sme_role": (s.user.role if s.user else role_by_name.get((plain(s.sme_name) or "").lower())),
            "approved_by_role": role_by_name.get((plain(s.approved_by_name) or "").lower()) if s.status == "GTG" else None,
            "created_at": s.created_at.isoformat() if s.created_at else None,
            "submitted_at": submitted_at[s.id].isoformat() if s.id in submitted_at else None,       # when it was last sent to the Module Owner
            "approved_at": s.approved_at.isoformat() if s.approved_at else None,
            "approved_by_name": plain(s.approved_by_name) if (s.status == "GTG") else None,
            "cards_count": len(s.cards) if s.cards else 0,
            "cards_planned": _planned_cards(s),
            "last_activity_at": (max([t for t in (s.updated_at, s.created_at, usage_last.get(s.id), event_last.get(s.id)) if t]).isoformat()),
            "approved_by_mo": (s.status == "GTG"),
            "tat_days": _tat_module.tat_days(s.approved_at, kicks.get(mod.id, (None, None))[0]) if s.status == "GTG" and mod else None,
            "review_passed": s.review_passed, "review_score": s.review_score, "review_at": s.review_at.isoformat() if s.review_at else None,
            "review_open": _open_findings(s),
            "revision_note": s.revision_note if s.status != "GTG" else None,          # set while the author still owes the changes the Module Owner asked for
            "revision_by": plain(s.revision_by) if (s.revision_note and s.status != "GTG") else None,
            "revision_at": s.revision_at.isoformat() if (s.revision_note and s.revision_at and s.status != "GTG") else None,
            **({"cost_usd": round(cost_by.get(s.id, 0.0), 4),
                # "reported": every call was priced by the provider itself (the real charge); "estimated": some came from our price list; "partial": some had no price at all
                "cost_basis": ("partial" if basis_by.get(s.id, {}).get("unknown") else "estimated" if basis_by.get(s.id) else "reported")} if is_admin else {})
        })
    return out


def _planned_cards(script) -> Optional[int]:
    """How many cards the approved outline plans for this script (None when there is no outline yet)."""
    try:
        outline = json.loads(script.outline_json) if script.outline_json else None
    except ValueError:
        return None
    return len(_outline_card_list(outline)) if isinstance(outline, dict) and outline.get("sections") else None

@app.get("/api/programs", response_model=List[ProgramOut])
def list_programs(user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Returns Scaler programs. Filtered to joined programs for SME and MO roles."""
    if user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.role in ["sme", "mo"]:
            return sorted(user.programs, key=lambda p: p.id)
    return db.query(Program).order_by(Program.id).all()

@app.get("/api/tracks", response_model=List[TrackOut])
def list_tracks(program_id: int, db: Session = Depends(get_db)):
    """Cascading Selector: Tracks for a selected program."""
    return db.query(Track).filter(Track.program_id == program_id).order_by(Track.id).all()

# Official Metadata mapping for all Scaler programs modules (Segments, Types, Durations)
SCALER_MODULE_META = {
    # Program 1: Scaler Academy (modules 1..29)
    1: {"segment": "Programming Fundamentals", "type": "Core", "duration": "1 month"},
    2: {"segment": "Programming Fundamentals", "type": "Core", "duration": "1 month"},
    3: {"segment": "Programming Fundamentals", "type": "Core", "duration": "1 month"},
    4: {"segment": "Agentic AI", "type": "Core", "duration": "1 month"},
    5: {"segment": "Advanced DSA", "type": "Core", "duration": "1 month"},
    6: {"segment": "Advanced DSA", "type": "Core", "duration": "1 month"},
    7: {"segment": "Advanced DSA", "type": "Core", "duration": "1 month"},
    8: {"segment": "Advanced DSA", "type": "Core", "duration": "1 month"},
    9: {"segment": "Databases & SQL", "type": "Core", "duration": "1 month"},
    10: {"segment": "Backend", "type": "Specialisation", "duration": "1 month"},
    11: {"segment": "Backend", "type": "Specialisation", "duration": "1 month"},
    12: {"segment": "Backend", "type": "Specialisation", "duration": "1 month"},
    13: {"segment": "Backend", "type": "Specialisation", "duration": "1 month"},
    14: {"segment": "Backend", "type": "Elective", "duration": "1.5 months"},
    15: {"segment": "Backend", "type": "Elective", "duration": "1 month"},
    16: {"segment": "Fullstack", "type": "Specialisation", "duration": "0.5 month"},
    17: {"segment": "Fullstack", "type": "Specialisation", "duration": "1.5 months"},
    18: {"segment": "Fullstack", "type": "Specialisation", "duration": "1 month"},
    19: {"segment": "Fullstack", "type": "Specialisation", "duration": "1 month"},
    20: {"segment": "Fullstack", "type": "Elective", "duration": "0.5 month"},
    21: {"segment": "Fullstack", "type": "Elective", "duration": "0.5 month"},
    22: {"segment": "Fullstack", "type": "Elective", "duration": "0.5 month"},
    23: {"segment": "Fullstack", "type": "Elective", "duration": "1 month"},
    24: {"segment": "System Design", "type": "Elective", "duration": "1.5 months"},
    25: {"segment": "Interview Preparation", "type": "Core", "duration": "1.5 weeks"},
    26: {"segment": "Data Engineering", "type": "Data Engineering", "duration": "2 months"},
    27: {"segment": "Advanced DSA", "type": "Elective", "duration": "1 month"},
    28: {"segment": "Product Management", "type": "Elective", "duration": "1 month"},
    29: {"segment": "Gen AI", "type": "Elective", "duration": "1.5 months"},

    # Program 2: Scaler DSML (modules 30..51)
    30: {"segment": "Data Foundations", "type": "Core", "duration": "2 months"},
    31: {"segment": "Data Foundations", "type": "Core", "duration": "1 month"},
    32: {"segment": "Programming", "type": "Core", "duration": "1.5 months"},
    33: {"segment": "Analytics", "type": "Core", "duration": "1 month"},
    34: {"segment": "AI & GenAI", "type": "Core", "duration": "1 month"},
    35: {"segment": "Career", "type": "Core", "duration": "1 month"},
    36: {"segment": "Analytics", "type": "Core", "duration": "1.5 months"},
    37: {"segment": "Math & Stats", "type": "Core", "duration": "1.5 months"},
    38: {"segment": "Analytics", "type": "Core", "duration": "1 month"},
    39: {"segment": "Math & Stats", "type": "Core", "duration": "1 month"},
    40: {"segment": "Machine Learning", "type": "Core", "duration": "1.5 months"},
    41: {"segment": "Cloud & Data", "type": "Elective", "duration": "1 month"},
    42: {"segment": "Analytics", "type": "Elective", "duration": "1 month"},
    43: {"segment": "AI Foundations", "type": "Elective", "duration": "1 month"},
    44: {"segment": "Deep ML", "type": "Core", "duration": "1.5 months"},
    45: {"segment": "Deep ML", "type": "Core", "duration": "1.5 months"},
    46: {"segment": "Specialized ML", "type": "Core", "duration": "1 month"},
    47: {"segment": "MLOps", "type": "Core", "duration": "1 month"},
    48: {"segment": "Deep Learning", "type": "Core", "duration": "1.5 months"},
    49: {"segment": "Vision & NLP", "type": "Specialisation", "duration": "1.5 months"},
    50: {"segment": "Vision & NLP", "type": "Specialisation", "duration": "1.5 months"},
    51: {"segment": "Agentic AI", "type": "Specialisation", "duration": "2 months"},

    # Program 3: Scaler DevOps & Cloud (modules 130..146)
    130: {"segment": "Programming", "type": "Core", "duration": "1 month"},
    131: {"segment": "Linux & Systems", "type": "Core", "duration": "1 month"},
    132: {"segment": "Linux & Systems", "type": "Core", "duration": "1 month"},
    133: {"segment": "CI/CD & Containers", "type": "Core", "duration": "1.5 months"},
    134: {"segment": "Kubernetes & IaC", "type": "Core", "duration": "1.5 months"},
    135: {"segment": "Cloud Infrastructure", "type": "Core", "duration": "1 month"},
    136: {"segment": "Cloud Infrastructure", "type": "Core", "duration": "1 month"},
    137: {"segment": "SRE & Security", "type": "Core", "duration": "1 month"},
    138: {"segment": "MLOps", "type": "Core", "duration": "1 month"},
    139: {"segment": "GenAI & Agents", "type": "Core", "duration": "1 month"},
    140: {"segment": "GenAI & Agents", "type": "Core", "duration": "1 month"},
    141: {"segment": "DSA & Problem Solving", "type": "Elective", "duration": "1 month"},
    142: {"segment": "DSA & Problem Solving", "type": "Elective", "duration": "1 month"},
    143: {"segment": "DSA & Problem Solving", "type": "Elective", "duration": "1 month"},
    144: {"segment": "DSA & Problem Solving", "type": "Elective", "duration": "1 month"},
    145: {"segment": "DSA & Problem Solving", "type": "Elective", "duration": "1.5 months"},
    146: {"segment": "System Design", "type": "Elective", "duration": "1.5 months"},

    # Program 4: Scaler AI FDE (modules 147..160)
    147: {"segment": "Data & Ingestion", "type": "Core", "duration": "1 month"},
    148: {"segment": "Data & Ingestion", "type": "Core", "duration": "1 month"},
    149: {"segment": "Backend Systems", "type": "Core", "duration": "1 month"},
    150: {"segment": "Frontend Engineering", "type": "Core", "duration": "1 month"},
    151: {"segment": "Enterprise AI & RAG", "type": "Core", "duration": "1 month"},
    152: {"segment": "Agentic Systems", "type": "Core", "duration": "1 month"},
    153: {"segment": "Scale & System Design", "type": "Core", "duration": "1 month"},
    154: {"segment": "Scale & System Design", "type": "Core", "duration": "1 month"},
    155: {"segment": "Infra & Containers", "type": "Core", "duration": "1 month"},
    156: {"segment": "Infra & Containers", "type": "Core", "duration": "1 month"},
    157: {"segment": "Cloud & Reliability", "type": "Elective", "duration": "1.5 months"},
    158: {"segment": "Cloud & Reliability", "type": "Elective", "duration": "1 month"},
    159: {"segment": "Security & Governance", "type": "Elective", "duration": "1 month"},
    160: {"segment": "Capstone", "type": "Elective", "duration": "1 month"},

    # Program 5: Scaler AI/ML & Agentic AI
    109: {"segment": "Generative AI", "type": "Core", "duration": "2 months"},
    110: {"segment": "Programming Fundamentals", "type": "Core", "duration": "1 month"},
    102: {"segment": "Data Foundations", "type": "Core", "duration": "1 month"},
    113: {"segment": "Machine Learning & Deep Learning", "type": "Core", "duration": "5 months"},
    114: {"segment": "Big Data", "type": "Core", "duration": "1 month"},
    115: {"segment": "Reinforcement Learning", "type": "Core", "duration": "1 month"},
    116: {"segment": "DSA", "type": "Optional", "duration": "4 months"},
}
SCALER_ACADEMY_MODULE_META = SCALER_MODULE_META

def _segment_key(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "").lower() if ch.isalpha())

def _meta_by_name(db: Session, program_id: int) -> dict:
    """Built-in module metadata looked up by module name, for modules whose ID is not in it
    (for example copies made when a mapping was synced). Cached per request session."""
    cache = db.info.setdefault("meta_by_name", {})
    if program_id not in cache:
        rows = (
            db.query(Module.id, Module.name).join(Track)
            .filter(Track.program_id == program_id, Module.id.in_(list(SCALER_MODULE_META)))
            .order_by(Module.id).all()
        )
        cache[program_id] = {}
        for mid, name in rows:
            cache[program_id].setdefault(" ".join((name or "").lower().split()), SCALER_MODULE_META[mid])
    return cache[program_id]

def _module_display_meta(m: Module, db: Session):
    """(segment, module type, duration label) shown for a module."""
    track = m.track
    meta = SCALER_MODULE_META.get(m.id)
    if track is not None and track.mapped:
        # Mapped tracks: the database wins for segment, duration and the Core/Elective/Specialisation tag.
        if meta is None:
            meta = _meta_by_name(db, track.program_id).get(" ".join((m.name or "").lower().split()))
        seg = m.segment or (meta or {}).get("segment") or track.name
        mod_type = m.module_type or (meta or {}).get("type") or "Core"
        duration = (meta or {}).get("duration", "1 month")
    elif meta:
        seg, mod_type, duration = meta["segment"], (m.module_type or meta["type"]), meta["duration"]
    else:
        seg = m.segment or (track.name if track else "Core")
        mod_type = m.module_type or "Core"
        duration = "1 month"
    if m.duration_months:
        duration = f"{m.duration_months:g} month" + ("" if m.duration_months == 1 else "s")
    return seg, mod_type, duration

def get_track_modules(db: Session, track: Track, specialisation: Optional[str] = None) -> List[Module]:
    """Returns the progressive curriculum modules for a given track/level, optionally filtered by specialisation."""
    if not track:
        return []

    if track.mapped:
        # Synced from an official mapping: the track owns its modules, in the mapped order.
        mods = (
            db.query(Module)
            .filter(Module.track_id == track.id, or_(Module.module_type.is_(None), Module.module_type != "Archived"))
            .order_by(Module.sequence_order, Module.id)
            .all()
        )
        if track.program_id == 1 and specialisation:
            # One specialisation per learner: hide the other one's modules.
            spec_key, hide = _segment_key(specialisation), None
            if "backend" in spec_key:
                hide = "fullstack"
            elif "fullstack" in spec_key:
                hide = "backend"
            if hide:
                mods = [m for m in mods if _segment_key(m.segment) != hide]
        return mods

    prog_id = track.program_id
    track_name_lower = (track.name or "").lower()
    track_slug_lower = (track.slug or "").lower()

    target_ids = None

    if prog_id == 1:  # Scaler Academy (3 Levels: Beginner, Intermediate, Advanced + 2 Specialisations)
        if "beginner" in track_slug_lower or "beginner" in track_name_lower:
            # Beginner attends all 29 modules from Fundamentals through Electives
            target_ids = list(range(1, 30))
        elif "intermediate" in track_slug_lower or "intermediate" in track_name_lower:
            # Intermediate skips beginner foundation (modules 1 & 2), starts at Intermediate DSA (3 to 29)
            target_ids = list(range(3, 30))
        elif "advanced" in track_slug_lower or "advanced" in track_name_lower:
            # Advanced skips foundation & Intermediate DSA, starts at AI & Agents (4 to 29)
            target_ids = list(range(4, 30))
        elif "backend" in track_slug_lower or "backend" in track_name_lower:
            target_ids = [10, 11, 12, 13, 14, 15]
        elif "fullstack" in track_slug_lower or "fullstack" in track_name_lower:
            target_ids = [16, 17, 18, 19, 20, 21, 22, 23]

        # Apply Specialisation filtering for Scaler Academy
        if target_ids is not None and specialisation:
            spec_lower = specialisation.lower().strip()
            if "backend" in spec_lower:
                # Learner opted for Backend Specialisation: exclude Fullstack modules 16..23
                fullstack_ids = set(range(16, 24))
                target_ids = [mid for mid in target_ids if mid not in fullstack_ids]
            elif "fullstack" in spec_lower:
                # Learner opted for Fullstack Specialisation: exclude Backend modules 10..15
                backend_ids = set(range(10, 16))
                target_ids = [mid for mid in target_ids if mid not in backend_ids]

    elif prog_id == 2:  # Scaler DSML (2 Levels: Beginner Track, Intermediate Track)
        if "beginner" in track_slug_lower or "beginner" in track_name_lower:
            # All 22 modules: 6 foundational (30..35) + 16 analytics/ML (36..51)
            target_ids = list(range(30, 52))
        elif "intermediate" in track_slug_lower or "intermediate" in track_name_lower:
            # 16 modules: skips 6 foundational, starts at 36..51
            target_ids = list(range(36, 52))

    elif prog_id == 3:  # Scaler DevOps & Cloud (1 Track: Core & Electives Track)
        # All 17 modules (130 to 146)
        target_ids = list(range(130, 147))

    elif prog_id == 4:  # Scaler AI FDE (1 Track: Enterprise Deployment Track)
        # All 14 modules (147 to 160)
        target_ids = list(range(147, 161))

    elif prog_id == 5:  # Scaler AI/ML & Agentic AI (2 Levels: Beginner Track, Advanced Track)
        if "beginner" in track_slug_lower or "beginner" in track_name_lower:
            # All active deep-dive modules
            target_ids = [109, 110, 102, 113, 114, 115, 116]
        elif "advanced" in track_slug_lower or "advanced" in track_name_lower:
            # Skips no-code AI & basic python
            target_ids = [102, 113, 114, 115, 116]

    if target_ids is not None:
        modules = db.query(Module).filter(Module.id.in_(target_ids)).all()
        id_map = {m.id: m for m in modules}
        return [id_map[mid] for mid in target_ids if mid in id_map]
    
    return db.query(Module).filter(Module.track_id == track.id).order_by(Module.sequence_order).all()

def annotate_module_meta(m: Module, db: Session) -> ModuleOut:
    seg, mod_type, duration = _module_display_meta(m, db)

    type_rows = (
        db.query(Class.class_type, func.count(Class.id))
        .filter(Class.module_id == m.id, or_(Class.class_type.is_(None), Class.class_type != "Archived"))
        .group_by(Class.class_type).all()
    )
    class_type_counts = {(t or "Live"): n for t, n in type_rows}
    count = sum(class_type_counts.values())

    return ModuleOut(
        id=m.id,
        track_id=m.track_id,
        name=m.name,
        slug=m.slug,
        sequence_order=m.sequence_order,
        is_locked=m.is_locked,
        module_type=mod_type,
        segment=seg,
        class_count=count,
        class_type_counts=class_type_counts,
        duration_label=duration
    )

@app.get("/api/modules", response_model=List[ModuleOut])
def list_modules(
    track_id: Optional[int] = None,
    program_id: Optional[int] = None,
    specialisation: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """Cascading Selector: Locked modules for a selected track/level or program with progressive level and specialisation mapping."""
    if track_id is not None:
        track = db.query(Track).filter(Track.id == track_id).first()
        if track:
            mods = get_track_modules(db, track, specialisation=specialisation)
            return [annotate_module_meta(m, db) for m in mods]
        return []

    if program_id is not None:
        if program_id == 1:
            target_ids = list(range(1, 30))
            if specialisation:
                spec_lower = specialisation.lower().strip()
                if "backend" in spec_lower:
                    target_ids = [mid for mid in target_ids if mid not in set(range(16, 24))]
                elif "fullstack" in spec_lower:
                    target_ids = [mid for mid in target_ids if mid not in set(range(10, 16))]
            mods = db.query(Module).filter(Module.id.in_(target_ids)).order_by(Module.id).all()
        else:
            mods = db.query(Module).join(Track).filter(Track.program_id == program_id).order_by(Module.sequence_order).all()
        return [annotate_module_meta(m, db) for m in mods]

    mods = db.query(Module).order_by(Module.id).all()
    return [annotate_module_meta(m, db) for m in mods]

@app.get("/api/classes", response_model=List[ClassOut])
def list_classes(module_id: int, db: Session = Depends(get_db)):
    """Cascading Selector: Classes within a locked module."""
    classes = (
        db.query(Class)
        .filter(Class.module_id == module_id, or_(Class.class_type.is_(None), Class.class_type != "Archived"))
        .order_by(Class.sequence_number).all()
    )
    results = []
    for c in classes:
        try:
            agendas_list = json.loads(c.agendas) if c.agendas else []
        except:
            agendas_list = [c.agendas]
        results.append(ClassOut(
            id=c.id,
            module_id=c.module_id,
            sequence_number=c.sequence_number,
            title=c.title,
            duration_minutes=c.duration_minutes,
            agendas=agendas_list,
            learning_outcomes=c.learning_outcomes,
            class_type=c.class_type,
            module_type=c.module_type,
            junction_number=c.junction_number,
            content_change_pct=c.content_change_pct or 0,
            content_change_description=c.content_change_description
        ))
    return results

@app.get("/api/class/{class_id}")
def get_class_details(class_id: int, db: Session = Depends(get_db)):
    """Returns class agendas + automated Class N-1 continuity memory preview."""
    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    try:
        agendas = json.loads(cls.agendas) if cls.agendas else []
    except:
        agendas = [cls.agendas]

    memory_preview = retrieve_context_memory(db, class_id)

    latest_script = db.query(Script).filter(Script.class_id == class_id).order_by(Script.id.desc()).first()
    latest_script_data = None
    if latest_script:
        latest_script_data = {
            "id": latest_script.id,
            "status": latest_script.status,
            "is_gtg": (latest_script.status == "GTG"),
            "shareable_uuid": latest_script.shareable_uuid,
            "duration_minutes": latest_script.duration_minutes,
            "cards_count": len(latest_script.cards) if latest_script.cards else 0
        }

    return {
        "class_id": cls.id,
        "title": cls.title,
        "sequence_number": cls.sequence_number,
        "duration_minutes": cls.duration_minutes,
        "agendas": agendas,
        "learning_outcomes": cls.learning_outcomes,
        "class_type": cls.class_type,
        "module_type": cls.module_type,
        "junction_number": cls.junction_number,
        "content_change_pct": cls.content_change_pct or 0,
        "content_change_description": cls.content_change_description,
        "reference_script_name": cls.reference_script_name,
        "has_reference_script": bool((cls.reference_script or "").strip()),
        "context_memory_preview": memory_preview,
        "latest_script": latest_script_data
    }

# ==============================================================================
# 1.1 MODULE OWNER (MO) / ADMIN CURRICULUM MANAGEMENT ENDPOINTS
# ==============================================================================

@app.get("/api/curriculum/full-tree")
def get_full_curriculum_tree(user_id: Optional[int] = None, program_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Returns structured curriculum hierarchy for Module Owner (MO) management, scoped to joined programs."""
    query = db.query(Program)
    if user_id:
        user = db.query(User).filter(User.id == user_id).first()
        if user and user.role in ["sme", "mo"]:
            prog_ids = [p.id for p in user.programs]
            query = query.filter(Program.id.in_(prog_ids))
    if program_id:
        query = query.filter(Program.id == program_id)
    programs = query.order_by(Program.id).all()
    
    tree = []
    for prog in programs:
        tracks = db.query(Track).filter(Track.program_id == prog.id).order_by(Track.id).all()
        track_list = []
        for trk in tracks:
            modules = get_track_modules(db, trk)
            mod_list = []
            for mod in modules:
                classes = (
                    db.query(Class)
                    .filter(Class.module_id == mod.id, or_(Class.class_type.is_(None), Class.class_type != "Archived"))
                    .order_by(Class.sequence_number).all()
                )
                cls_list = []
                for c in classes:
                    try:
                        agendas_arr = json.loads(c.agendas) if c.agendas else []
                    except:
                        agendas_arr = [c.agendas]
                    cls_list.append({
                        "id": c.id,
                        "sequence_number": c.sequence_number,
                        "title": c.title,
                        "duration_minutes": c.duration_minutes,
                        "agendas": agendas_arr,
                        "learning_outcomes": c.learning_outcomes,
                        "class_type": c.class_type,
                        "module_type": c.module_type,
                        "junction_number": c.junction_number,
                        "content_change_pct": c.content_change_pct or 0,
                        "content_change_description": c.content_change_description,
                        "reference_script_name": c.reference_script_name
                    })
                seg, mod_type, duration = _module_display_meta(mod, db)
                mod_list.append({
                    "id": mod.id,
                    "name": mod.name,
                    "segment": seg,
                    "duration_label": duration,
                    "module_type": mod_type,
                    "is_locked": mod.is_locked,
                    "plan_status": mod.plan_status or ("Locked" if mod.is_locked else "Draft"),
                    "plan_source": mod.plan_source or "manual",
                    "owner_name": mod.owner_name,
                    "segment_label": mod.segment,
                    "duration_months": mod.duration_months,
                    "approved_sources": mod.approved_sources or "",
                    "kickoff_date": mod.kickoff_at.date().isoformat() if mod.kickoff_at else None,
                    "completeness_issues": evaluate_module_completeness(mod, ignore_lock=True)["gaps"],
                    "plan_gaps": evaluate_module_completeness(mod)["gaps"] if module_is_locked(mod) else [],
                    "class_count": len(cls_list),
                    "class_type_counts": {t: sum(1 for x in cls_list if (x["class_type"] or "Live") == t)
                                          for t in sorted({(x["class_type"] or "Live") for x in cls_list})},
                    "classes": cls_list
                })
            track_list.append({
                "id": trk.id,
                "name": trk.name,
                "module_count": len(mod_list),
                "modules": mod_list
            })
        tree.append({
            "id": prog.id,
            "name": prog.name,
            "tat_target_days": prog.tat_target_days,
            "slug": prog.slug,
            "description": prog.description,
            "track_count": len(track_list),
            "tracks": track_list
        })
    return tree

def _require_actor(db: Session, user_id: Optional[int]) -> User:
    """The signed-in user behind a write request. There is no login token yet, so this trusts the user_id
    the client sends; it enforces the PRD role rules for honest clients but is not authentication."""
    if not user_id:
        raise HTTPException(status_code=401, detail="Sign in required: no user was identified for this request.")
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required: unknown user.")
    return user

def _require_admin(db: Session, user_id: Optional[int], action: str) -> User:
    user = _require_actor(db, user_id)
    if user.role != "admin":
        raise HTTPException(status_code=403, detail=f"Only Administrators can {action}.")
    return user

def _user_out(u: User) -> UserOut:
    return UserOut(
        id=u.id, name=u.name, email=u.email, role=u.role,
        program_ids=[p.id for p in u.programs], program_names=[p.name for p in u.programs],
        track_ids=[t.id for t in u.tracks],
        track_names=[f"{t.program.name} / {t.name}" for t in u.tracks],
    )

def _apply_memberships(db: Session, user: User, program_ids: List[int], track_ids: List[int]):
    """FR-1: SMEs and Program Managers join a program and, where the program has tracks, a track in it.
    Single-track programs are joined automatically. Access itself stays program-wide (FR-2)."""
    progs = db.query(Program).filter(Program.id.in_(program_ids)).all() if program_ids else []
    joined = {p.id for p in progs}
    tracks = db.query(Track).filter(Track.id.in_(track_ids)).all() if track_ids else []
    for t in tracks:
        if t.program_id not in joined:
            raise HTTPException(status_code=400, detail=f"Track '{t.name}' belongs to a program this user has not joined.")
    chosen = {t.id: t for t in tracks}
    if user.role in ("sme", "pm"):
        for p in progs:
            if len(p.tracks) == 1:
                chosen[p.tracks[0].id] = p.tracks[0]
            elif len(p.tracks) > 1 and not any(t.program_id == p.id for t in chosen.values()):
                raise HTTPException(status_code=400, detail=f"Choose at least one track in '{p.name}'.")
    user.programs = progs
    user.tracks = list(chosen.values())

def _script_program_id(script: Script) -> Optional[int]:
    cls = script.class_entity
    return cls.module.track.program_id if cls and cls.module and cls.module.track else None

def _reopen_if_approved(db: Session, script_id: int, actor: User) -> bool:
    """A script that was approved and is then changed is no longer the version the Module Owner approved: it becomes a draft again, with a note, and has to be
    submitted for approval again. Nothing happens to a script that is not approved."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script or script.status != "GTG":
        return False
    script.status = "Script WIP"
    script.revision_note = "The script was edited after it was approved. Review the changes, then submit it to the Module Owner for approval again."
    script.revision_by = "Approval withdrawn"
    script.revision_at = datetime.datetime.utcnow()
    db.add(ScriptEvent(script_id=script.id, user_id=actor.id if actor else None, user_name=actor.name if actor else None, user_role=actor.role if actor else None, event_type="Edited After Approval"))
    log_activity(db, user_name=actor.name if actor else "Someone", user_role=actor.role if actor else None, action_type="Approved Script Edited",
                 description=f"Edited the approved script #{script.id} for '{_effective_title(script)}'. It is a draft again and needs the Module Owner's approval")
    return True


def _require_script_access(db: Session, user_id: Optional[int], script: Script, write: bool, allow_admin_write: bool = False) -> User:
    """Scripts depend on the programs a person has joined (PRD). Admins and PMs can read every script; only
    SMEs and MOs of the script's program can change it. Admins may delete scripts but not author them."""
    user = _require_actor(db, user_id)
    if user.role == "admin":
        if write and not allow_admin_write:
            raise HTTPException(status_code=403, detail="Administrators manage the platform and cannot author or change scripts.")
        return user
    if user.role == "pm":
        if write:
            raise HTTPException(status_code=403, detail="Program Managers have read-only access.")
        return user
    prog_id = _script_program_id(script)
    if prog_id and prog_id not in [p.id for p in user.programs]:
        raise HTTPException(status_code=403, detail="You have not joined the program of this script.")
    return user

def script_guard(write: bool, allow_admin_write: bool = False):
    """Dependency for script endpoints: loads the script from the path and checks the signed-in user's access."""
    def dep(script_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)) -> User:
        script = db.query(Script).filter(Script.id == script_id).first()
        if not script:
            raise HTTPException(status_code=404, detail="Script not found")
        return _require_script_access(db, user_id, script, write, allow_admin_write)
    return dep

def module_is_locked(mod) -> bool:
    return (mod.plan_status or ("Locked" if mod.is_locked else "Draft")) == "Locked"


def _norm_outcomes(text: Optional[str]) -> str:
    """Learning outcomes as they count for comparing: the generic text written when nobody gave any is the same as empty."""
    from agents.shared_context import _PLACEHOLDER_OUTCOME_PREFIX
    t = (text or "").strip()
    return "" if t.startswith(_PLACEHOLDER_OUTCOME_PREFIX) else t


def reopen_for_plan_edit(db: Session, mod, user_id: Optional[int], what: str = "changed") -> bool:
    """FR-5: Module Owners and Admins can change a module and its classes after it is locked. The plan then goes back to review and has to be locked again
    (FR-4), so a locked plan is never silently different from the one that was reviewed. Returns True when the plan was reopened."""
    if mod is None or not module_is_locked(mod):
        return False
    mod.plan_status = "In Review"
    mod.is_locked = False
    actor = db.query(User).filter(User.id == user_id).first() if user_id else None
    db.add(UserActivity(user_name=actor.name if actor else "Someone", user_role=actor.role if actor else None, action_type="Locked Module Edited",
                        description=f"{what}. The plan of module '{mod.name}' went back to In Review and needs to be locked again"))
    return True


def verify_curriculum_edit_permission(db: Session, user_id: Optional[int], program_id: Optional[int] = None):
    """Enforces PRD role permissions: PM is read-only, MO within assigned programs, Admin global."""
    user = _require_actor(db, user_id)
    if user.role == "pm":
        raise HTTPException(status_code=403, detail="Program Managers have read-only access and cannot edit curriculum.")
    if user.role == "sme":
        raise HTTPException(status_code=403, detail="SMEs cannot modify curriculum structures.")
    if user.role == "mo" and program_id:
        user_prog_ids = [p.id for p in user.programs]
        if program_id not in user_prog_ids:
            raise HTTPException(status_code=403, detail="Module Owners can only make edits within programs assigned to them by the Administrator.")

@app.put("/api/classes/{class_id}")
def update_class_by_mo(class_id: int, payload: ClassUpdate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to edit class title, agendas, duration, and metadata."""
    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    prog_id = cls.module.track.program_id if cls.module and cls.module.track else None
    verify_curriculum_edit_permission(db, user_id, prog_id)
    def _same_agendas() -> bool:
        try:
            return payload.agendas is None or list(payload.agendas) == list(json.loads(cls.agendas or "[]"))
        except ValueError:
            return False
    plan_changed = (not _same_agendas()
                    or (payload.title is not None and payload.title != cls.title)
                    or (payload.duration_minutes is not None and payload.duration_minutes != cls.duration_minutes)
                    or (payload.learning_outcomes is not None and _norm_outcomes(payload.learning_outcomes) != _norm_outcomes(cls.learning_outcomes))
                    or (payload.class_type is not None and payload.class_type != cls.class_type)
                    or (payload.module_type is not None and payload.module_type != cls.module_type)
                    or (payload.sequence_number is not None and payload.sequence_number != cls.sequence_number)
                    or (payload.module_id is not None and payload.module_id != cls.module_id)
                    or (payload.content_change_pct is not None and payload.content_change_pct != (cls.content_change_pct or 0)))
    if plan_changed:                                          # saving with nothing changed, or attaching reference material, does not touch a locked plan
        reopen_for_plan_edit(db, cls.module, user_id, f"Class '{cls.title}' was changed")
    new_pct = payload.content_change_pct if payload.content_change_pct is not None else (cls.content_change_pct or 0)
    new_desc = payload.content_change_description if payload.content_change_description is not None else cls.content_change_description
    if new_pct > 0 and not (new_desc or "").strip():
        raise HTTPException(status_code=400, detail="Describe what changed when the content change is above 0%.")
    cls.content_change_pct = new_pct
    cls.content_change_description = (new_desc or "").strip() or None
    
    if payload.title is not None:
        cls.title = payload.title
    if payload.duration_minutes is not None:
        cls.duration_minutes = payload.duration_minutes
    if payload.agendas is not None:
        cls.agendas = json.dumps(payload.agendas)
    if payload.learning_outcomes is not None and _norm_outcomes(payload.learning_outcomes) != _norm_outcomes(cls.learning_outcomes):
        cls.learning_outcomes = payload.learning_outcomes.strip() or None      # the generic text nobody wrote stays until real outcomes replace it
    if payload.class_type is not None:
        if payload.class_type != cls.class_type and payload.class_type not in CLASS_TYPES:
            raise HTTPException(status_code=400, detail="Class type must be Live, Lab or Inverted.")
        cls.class_type = payload.class_type
    if payload.module_type is not None:
        cls.module_type = payload.module_type
    if payload.sequence_number is not None:
        cls.sequence_number = payload.sequence_number
    if payload.module_id is not None:
        cls.module_id = payload.module_id
    if payload.reference_script is not None:                      # the Module Owner attaches (or removes) a previous script of this class
        cls.reference_script = payload.reference_script.strip() or None
        cls.reference_script_name = (payload.reference_script_name or "").strip()[:255] or None if cls.reference_script else None

    db.commit()
    db.refresh(cls)
    
    try:
        agendas_arr = json.loads(cls.agendas) if cls.agendas else []
    except:
        agendas_arr = [cls.agendas]
        
    return {
        "success": True,
        "message": f"Class '{cls.title}' updated successfully.",
        "class": {
            "id": cls.id,
            "module_id": cls.module_id,
            "sequence_number": cls.sequence_number,
            "title": cls.title,
            "duration_minutes": cls.duration_minutes,
            "agendas": agendas_arr,
            "learning_outcomes": cls.learning_outcomes,
            "class_type": cls.class_type,
            "module_type": cls.module_type,
            "content_change_pct": cls.content_change_pct or 0,
            "content_change_description": cls.content_change_description,
            "reference_script_name": cls.reference_script_name
        }
    }

CLASS_TYPES = ("Live", "Lab", "Inverted")        # the only types an author can choose; classes that arrived with another type keep it


@app.post("/api/classes")
def create_class_by_mo(payload: ClassCreate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to append a new class to an existing module."""
    mod = db.query(Module).filter(Module.id == payload.module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    prog_id = mod.track.program_id if mod.track else None
    verify_curriculum_edit_permission(db, user_id, prog_id)
    reopen_for_plan_edit(db, mod, user_id, f"A class was added to '{mod.name}'")
    if payload.content_change_pct > 0 and not (payload.content_change_description or "").strip():
        raise HTTPException(status_code=400, detail="Describe what changed when the content change is above 0%.")
        
    if payload.class_type not in CLASS_TYPES:
        raise HTTPException(status_code=400, detail="Class type must be Live, Lab or Inverted.")
    seq = payload.sequence_number
    if not seq or seq >= 99:          # the screen sends 99 for "at the end": number it after the module's last class
        seq = (db.query(func.max(Class.sequence_number)).filter(Class.module_id == payload.module_id).scalar() or 0) + 1
    new_cls = Class(
        module_id=payload.module_id,
        sequence_number=seq,
        title=payload.title,
        duration_minutes=payload.duration_minutes,
        agendas=json.dumps(payload.agendas),
        learning_outcomes=payload.learning_outcomes,
        class_type=payload.class_type,
        module_type=payload.module_type,
        content_change_pct=payload.content_change_pct,
        content_change_description=(payload.content_change_description or "").strip() or None,
        reference_script=(payload.reference_script or "").strip() or None,
        reference_script_name=((payload.reference_script_name or "").strip()[:255] or None) if (payload.reference_script or "").strip() else None
    )
    db.add(new_cls)
    db.commit()
    db.refresh(new_cls)

    editor_name = "Admin/MO"
    editor_role = "mo"
    if user_id:
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            editor_name = u.name
            editor_role = u.role
    log_activity(
        db,
        user_name=editor_name,
        user_role=editor_role,
        action_type="Class Created",
        description=f"Added new class '{new_cls.title}' (#{new_cls.sequence_number}) under module '{mod.name}'"
    )

    return {
        "success": True,
        "class_id": new_cls.id,
        "class": {
            "id": new_cls.id,
            "module_id": new_cls.module_id,
            "sequence_number": new_cls.sequence_number,
            "title": new_cls.title,
            "duration_minutes": new_cls.duration_minutes
        },
        "message": f"Class '{new_cls.title}' created."
    }

@app.delete("/api/classes/{class_id}")
def delete_class_by_mo(class_id: int, user_id: Optional[int] = None, confirm_name: Optional[str] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to remove a class from a module."""
    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    prog_id = cls.module.track.program_id if cls.module and cls.module.track else None
    verify_curriculum_edit_permission(db, user_id, prog_id)
    reopen_for_plan_edit(db, cls.module, user_id, f"Class '{cls.title}' was deleted")
    _require_delete_confirmation(_delete_impact(db, "class", cls), confirm_name)
    title = cls.title
    db.delete(cls)
    db.commit()

    editor_name = "Admin/MO"
    editor_role = "mo"
    if user_id:
        u = db.query(User).filter(User.id == user_id).first()
        if u:
            editor_name = u.name
            editor_role = u.role
    log_activity(
        db,
        user_name=editor_name,
        user_role=editor_role,
        action_type="Class Deleted",
        description=f"Deleted canonical class '{title}' (ID: #{class_id})"
    )
    return {"success": True, "message": f"Class '{title}' deleted."}

@app.put("/api/modules/{module_id}")
def update_module_by_mo(module_id: int, payload: ModuleUpdate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to update module details, lock status, level/track assignment, and approved sources."""
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    prog_id = mod.track.program_id if mod.track else None
    verify_curriculum_edit_permission(db, user_id, prog_id)
    plan_changed = ((payload.name is not None and payload.name != mod.name) or (payload.module_type is not None and payload.module_type != mod.module_type)
                    or (payload.segment is not None and (payload.segment.strip() or None) != mod.segment)
                    or (payload.duration_months is not None and payload.duration_months != mod.duration_months)
                    or (payload.track_id is not None and payload.track_id != mod.track_id) or (payload.sequence_order is not None and payload.sequence_order != mod.sequence_order))
    if plan_changed:
        reopen_for_plan_edit(db, mod, user_id, f"Module '{mod.name}' was changed")        # the approved sources, the owner and the kickoff date are not part of the plan, so they do not reopen it
    if payload.module_type is not None and payload.module_type != mod.module_type:
        _require_admin(db, user_id, "change module-type tags (Core, Elective, Specialisation)")
    if payload.segment is not None and (payload.segment.strip() or None) != mod.segment:
        _require_admin(db, user_id, "change a module's segment label")
    if payload.duration_months is not None and payload.duration_months != mod.duration_months:
        _require_admin(db, user_id, "change a module's duration")
    if payload.track_id is not None and payload.track_id != mod.track_id:
        target = db.query(Track).filter(Track.id == payload.track_id).first()
        if not target:
            raise HTTPException(status_code=404, detail="Target track not found")
        verify_curriculum_edit_permission(db, user_id, target.program_id)
        
    if payload.name is not None:
        mod.name = payload.name
    if payload.module_type is not None:
        mod.module_type = payload.module_type
    if payload.is_locked is not None:
        if payload.is_locked:
            report = evaluate_module_completeness(mod, ignore_lock=True)       # the plan is locked only when it has no gaps
            if not report["is_complete"]:
                raise HTTPException(status_code=400, detail={"message": "Module plan cannot be locked", "issues": report["gaps"]})
            mod.plan_status = "Locked"
        else:
            mod.plan_status = "In Review"
        mod.is_locked = payload.is_locked
    if payload.approved_sources is not None:
        mod.approved_sources = payload.approved_sources
    if payload.owner_name is not None:
        mod.owner_name = payload.owner_name.strip() or None
    if payload.kickoff_date is not None:
        if payload.kickoff_date.strip():
            try:
                mod.kickoff_at = datetime.datetime.strptime(payload.kickoff_date.strip()[:10], "%Y-%m-%d")
            except ValueError:
                raise HTTPException(status_code=400, detail="The kickoff date must look like 2026-10-16.")
        else:
            mod.kickoff_at = None
    if payload.segment is not None:
        mod.segment = payload.segment.strip() or None
    if payload.duration_months is not None:
        mod.duration_months = payload.duration_months
    if payload.track_id is not None:
        mod.track_id = payload.track_id
    if payload.sequence_order is not None:
        mod.sequence_order = payload.sequence_order
        
    db.commit()
    db.refresh(mod)
    return {"success": True, "message": f"Module '{mod.name}' updated successfully."}

@app.post("/api/modules")
def create_module_by_mo(payload: ModuleCreate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to append a new module to a track."""
    track = db.query(Track).filter(Track.id == payload.track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    verify_curriculum_edit_permission(db, user_id, track.program_id)
        
    slug_val = payload.slug or payload.name.lower().strip().replace(" ", "-")
    existing_mod = db.query(Module).filter(Module.track_id == payload.track_id, Module.slug == slug_val).first()
    if existing_mod:
        slug_val = f"{slug_val}-{uuid.uuid4().hex[:6]}"

    new_mod = Module(
        track_id=payload.track_id,
        name=payload.name.strip(),
        slug=slug_val,
        module_type=payload.module_type,
        is_locked=payload.is_locked,
        plan_status="Locked" if payload.is_locked else "In Review",
        approved_sources=payload.approved_sources,
        owner_name=(payload.owner_name or "").strip() or None
    )
    if payload.segment or payload.duration_months:
        _require_admin(db, user_id, "set a module's segment or duration")
        new_mod.segment = (payload.segment or "").strip() or None
        new_mod.duration_months = payload.duration_months
    db.add(new_mod)
    db.commit()
    db.refresh(new_mod)
    return {"success": True, "module_id": new_mod.id, "message": f"Module '{new_mod.name}' created."}

def _delete_impact(db: Session, kind: str, entity) -> dict:
    """What deleting a program, track or module would remove, so the UI can warn before it happens."""
    if kind == "class":
        scripts_q = db.query(Script).filter(Script.class_id == entity.id)
        return {"kind": "class", "name": entity.title, "tracks": None, "modules": 0, "classes": 1,
                "scripts": scripts_q.count(), "approved_scripts": scripts_q.filter(Script.status == "GTG").count(),
                "users_enrolled": None}
    if kind == "program":
        modules = [m for t in entity.tracks for m in t.modules]
    elif kind == "track":
        modules = list(entity.modules)
    else:
        modules = [entity]
    mod_ids = [m.id for m in modules]
    classes = scripts = approved = 0
    if mod_ids:
        classes = db.query(Class).filter(Class.module_id.in_(mod_ids)).count()
        scripts_q = db.query(Script).join(Class, Class.id == Script.class_id).filter(Class.module_id.in_(mod_ids))
        scripts = scripts_q.count()
        approved = scripts_q.filter(Script.status == "GTG").count()
    return {
        "kind": kind, "name": entity.name,
        "tracks": len(entity.tracks) if kind == "program" else None,
        "modules": len(modules), "classes": classes,
        "scripts": scripts, "approved_scripts": approved,
        "users_enrolled": len(entity.users) if kind in ("program", "track") else None,
    }

def _require_delete_confirmation(impact: dict, confirm_name: Optional[str]):
    """Anything that holds scripts is only deleted when the caller types its exact name."""
    if impact["scripts"] > 0 and (confirm_name or "").strip().lower() != impact["name"].strip().lower():
        raise HTTPException(status_code=400, detail=(
            f"Deleting '{impact['name']}' would also delete {impact['modules']} modules, {impact['classes']} classes and "
            f"{impact['scripts']} scripts ({impact['approved_scripts']} approved). Confirm by passing its exact name as confirm_name."))

@app.get("/api/tracks/{track_id}/impact")
def track_delete_impact(track_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")
    _require_admin(db, user_id, "configure tracks and levels")
    return _delete_impact(db, "track", track)

@app.get("/api/modules/{module_id}/impact")
def module_delete_impact(module_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")
    verify_curriculum_edit_permission(db, user_id, mod.track.program_id if mod.track else None)
    return _delete_impact(db, "module", mod)

@app.get("/api/classes/{class_id}/impact")
def class_delete_impact(class_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")
    verify_curriculum_edit_permission(db, user_id, cls.module.track.program_id if cls.module and cls.module.track else None)
    return _delete_impact(db, "class", cls)

@app.get("/api/programs/{program_id}/impact")
def program_delete_impact(program_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    prog = db.query(Program).filter(Program.id == program_id).first()
    if not prog:
        raise HTTPException(status_code=404, detail="Program not found")
    _require_admin(db, user_id, "delete curriculum programs")
    return _delete_impact(db, "program", prog)

@app.delete("/api/modules/{module_id}")
def delete_module_by_mo(module_id: int, user_id: Optional[int] = None, confirm_name: Optional[str] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to remove a module and its classes."""
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")

    prog_id = mod.track.program_id if mod.track else None
    verify_curriculum_edit_permission(db, user_id, prog_id)
    _require_delete_confirmation(_delete_impact(db, "module", mod), confirm_name)

    name = mod.name
    db.delete(mod)
    db.commit()
    return {"success": True, "message": f"Module '{name}' deleted."}

@app.post("/api/tracks")
def create_track_by_mo(payload: TrackCreate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to add a new track to a program."""
    prog = db.query(Program).filter(Program.id == payload.program_id).first()
    if not prog:
        raise HTTPException(status_code=404, detail="Program not found")

    _require_admin(db, user_id, "configure tracks and levels")
        
    slug_val = payload.slug or payload.name.lower().strip().replace(" ", "-")
    existing_track = db.query(Track).filter(Track.program_id == payload.program_id, Track.slug == slug_val).first()
    if existing_track:
        slug_val = f"{slug_val}-{uuid.uuid4().hex[:6]}"

    new_track = Track(
        program_id=payload.program_id,
        name=payload.name.strip(),
        slug=slug_val
    )
    db.add(new_track)
    db.commit()
    db.refresh(new_track)
    return {"success": True, "track_id": new_track.id, "message": f"Track '{new_track.name}' created."}

@app.put("/api/tracks/{track_id}")
def update_track_by_mo(track_id: int, payload: TrackUpdate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to rename or update track details."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    _require_admin(db, user_id, "configure tracks and levels")
        
    if payload.name is not None and payload.name.strip():
        track.name = payload.name.strip()
    if payload.slug is not None:
        track.slug = payload.slug
        
    db.commit()
    db.refresh(track)
    return {"success": True, "message": f"Track '{track.name}' updated successfully."}

@app.delete("/api/tracks/{track_id}")
def delete_track_by_mo(track_id: int, user_id: Optional[int] = None, confirm_name: Optional[str] = None, db: Session = Depends(get_db)):
    """Allows MO/Admin to delete a track and its modules."""
    track = db.query(Track).filter(Track.id == track_id).first()
    if not track:
        raise HTTPException(status_code=404, detail="Track not found")

    _require_admin(db, user_id, "configure tracks and levels")
    _require_delete_confirmation(_delete_impact(db, "track", track), confirm_name)

    name = track.name
    db.delete(track)
    db.commit()
    return {"success": True, "message": f"Track '{name}' deleted."}

@app.post("/api/programs")
def create_program_by_admin(payload: ProgramCreate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Only Administrators can create a new curriculum program. Module Owners manage assigned programs."""
    _require_admin(db, user_id, "create curriculum programs")
    if db.query(Program).filter(Program.name == payload.name.strip()).first():
        raise HTTPException(status_code=400, detail=f"A program named '{payload.name.strip()}' already exists.")

    slug_val = payload.slug or payload.name.lower().strip().replace(" ", "-")
    existing_prog = db.query(Program).filter(Program.slug == slug_val).first()
    if existing_prog:
        slug_val = f"{slug_val}-{uuid.uuid4().hex[:6]}"

    new_prog = Program(
        name=payload.name.strip(),
        slug=slug_val,
        description=payload.description or ""
    )
    db.add(new_prog)
    db.commit()
    db.refresh(new_prog)

    # Automatically associate with all Admins and PMs for visibility
    all_gov_users = db.query(User).filter(User.role.in_(["admin", "pm"])).all()
    for u in all_gov_users:
        if new_prog not in u.programs:
            u.programs.append(new_prog)
    db.commit()

    return {"success": True, "program_id": new_prog.id, "message": f"Program '{new_prog.name}' created."}

@app.put("/api/programs/{program_id}")
def update_program_by_admin(program_id: int, payload: ProgramUpdate, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Only Administrators can update program details."""
    _require_admin(db, user_id, "edit curriculum programs")

    prog = db.query(Program).filter(Program.id == program_id).first()
    if not prog:
        raise HTTPException(status_code=404, detail="Program not found")
        
    if payload.name is not None and payload.name.strip():
        prog.name = payload.name.strip()
    if payload.slug is not None:
        prog.slug = payload.slug
    if payload.description is not None:
        prog.description = payload.description
    if payload.tat_target_days is not None:
        prog.tat_target_days = payload.tat_target_days or None

    db.commit()
    db.refresh(prog)
    return {"success": True, "message": f"Program '{prog.name}' updated successfully."}

@app.delete("/api/programs/{program_id}")
def delete_program_by_admin(program_id: int, user_id: Optional[int] = None, confirm_name: Optional[str] = None, db: Session = Depends(get_db)):
    """Only Administrators can delete a program."""
    _require_admin(db, user_id, "delete curriculum programs")

    prog = db.query(Program).filter(Program.id == program_id).first()
    if not prog:
        raise HTTPException(status_code=404, detail="Program not found")
    _require_delete_confirmation(_delete_impact(db, "program", prog), confirm_name)
    name = prog.name
    
    prog.users.clear()
    db.delete(prog)
    db.commit()
    return {"success": True, "message": f"Program '{name}' deleted."}

# ==============================================================================
# FR-3 / FR-4 / FR-6: MODULE PLAN INTAKE, REVIEW LOCK, AND COMPLETENESS GATE
# ==============================================================================

def _parse_agendas(raw) -> list:
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return [a for a in val if str(a).strip()] if isinstance(val, list) else [str(val)]
    except Exception:
        return [raw] if str(raw).strip() else []

def _plan_gaps(module: Module) -> list:
    """FR-6: what stops a module being complete. Every class needs a title and an agenda list, and the module needs an owner or an SME. Learning outcomes are optional."""
    from agents.shared_context import _PLACEHOLDER_OUTCOME_PREFIX
    gaps = []
    classes = [c for c in (module.classes or []) if (c.sequence_number or 0) < 1000 and c.class_type != "Archived"]
    if not classes:
        gaps.append("Module has no classes")
    for c in classes:
        label = f"Class {c.sequence_number}"
        if not (c.title or "").strip():
            gaps.append(f"{label}: missing title")
        if not _parse_agendas(c.agendas):
            gaps.append(f"{label}: missing agenda list")
    owner = (module.owner_name or "").strip()
    if not owner and module.track and module.track.program:
        owner = next((u.name for u in module.track.program.users if u.role in ("sme", "mo")), "")
    if not owner:
        gaps.append("No owner or SME is assigned to this module")
    return gaps


def evaluate_module_completeness(module: Module, ignore_lock: bool = False) -> dict:
    """FR-6. A module is complete when its plan is Locked, and a plan can only be Locked once it has no gaps (so the lock check itself uses ignore_lock=True).
    Modules locked before this check existed stay complete, so scripts keep working, and show what they are missing as `gaps`."""
    status_val = module.plan_status or ("Locked" if module.is_locked else "Draft")
    classes = list(module.classes or [])
    gaps = _plan_gaps(module)
    owner = (module.owner_name or "").strip()
    if not owner and module.track and module.track.program:
        owner = next((u.name for u in module.track.program.users if u.role in ("sme", "mo")), "")
    locked = (module.is_locked or status_val == "Locked") and not ignore_lock
    issues = [] if locked else list(gaps) + ([f"Plan is '{status_val}', must be reviewed and Locked"] if status_val != "Locked" and not ignore_lock else [])
    return {
        "module_id": module.id,
        "module_name": module.name,
        "plan_status": "Locked" if locked else status_val,
        "plan_source": module.plan_source or "manual",
        "owner": owner or None,
        "class_count": len(classes),
        "is_complete": not issues,
        "issues": issues,
        "gaps": gaps,
    }

@app.get("/api/programs/{program_id}/generation-gate")
def get_generation_gate(program_id: int, db: Session = Depends(get_db)):
    """FR-6: tells the UI whether script creation is enabled for this program and why."""
    prog = db.query(Program).filter(Program.id == program_id).first()
    if not prog:
        raise HTTPException(status_code=404, detail="Program not found")
    reports = [evaluate_module_completeness(m) for t in prog.tracks for m in t.modules]
    complete = [r for r in reports if r["is_complete"]]
    return {
        "program_id": prog.id,
        "program_name": prog.name,
        "script_creation_enabled": bool(complete),
        "complete_modules": len(complete),
        "total_modules": len(reports),
        "incomplete_modules": [r for r in reports if not r["is_complete"]][:25],
    }

@app.get("/api/modules/{module_id}/completeness")
def get_module_completeness(module_id: int, db: Session = Depends(get_db)):
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")
    return evaluate_module_completeness(mod)

def _require_plan_editor(db: Session, user_id: Optional[int], module: Optional[Module] = None) -> str:
    """Only Admin or an enrolled MO may edit/review/lock module plans."""
    user = _require_actor(db, user_id)
    if user.role not in ("admin", "mo"):
        raise HTTPException(status_code=403, detail="Only Admins or Module Owners can manage module plans.")
    if user.role == "mo" and module and module.track:
        if module.track.program_id not in [p.id for p in user.programs]:
            raise HTTPException(status_code=403, detail="Module Owner has not joined this program.")
    return user.name

def _parse_syllabus_text(text_in: str) -> list:
    """
    FR-3: AI syllabus parser. Uses Claude when available; otherwise a deterministic
    parser that understands 'Class N: Title', '- agenda', and 'LO:' / 'Outcomes:' lines.
    """
    execution_mode = os.getenv("EXECUTION_MODE", "hybrid")

    if llm_configured() and execution_mode != "mock":
        try:
            prompt_content = (
                "Parse this syllabus into a JSON array of classes. Each item: "
                '{"title": str, "agendas": [str], "learning_outcomes": str, "duration_minutes": int}. '
                "Return ONLY JSON.\n\nSYLLABUS:\n" + text_in
            )
            raw = call_llm_chat(messages=[{"role": "user", "content": prompt_content}], max_tokens=4000, agent="syllabus_parser")

            if "```" in raw:
                raw = raw.split("```")[1].removeprefix("json").strip()
            parsed = json.loads(raw)
            if isinstance(parsed, list) and parsed:
                return parsed
        except Exception as e:
            print(f"Syllabus AI parse failed, using rule-based parser: {e}")

    import re
    classes, current = [], None
    for line in text_in.splitlines():
        s = line.strip()
        if not s:
            continue
        m = re.match(r"^(?:class|lecture|session|day)\s*\d+\s*[:.\-]\s*(.+)$", s, re.I)
        if m:
            current = {"title": m.group(1).strip(), "agendas": [], "learning_outcomes": "", "duration_minutes": 90}
            classes.append(current)
            continue
        if current is None:
            continue
        lo = re.match(r"^(?:lo|los|outcomes?|learning outcomes?)\s*[:\-]\s*(.+)$", s, re.I)
        if lo:
            current["learning_outcomes"] = (current["learning_outcomes"] + " " + lo.group(1)).strip()
        elif re.match(r"^[-*\u2022]|^\d+[.)]", s):
            current["agendas"].append(re.sub(r"^([-*\u2022]|\d+[.)])\s*", "", s))
    return classes

@app.post("/api/modules/{module_id}/syllabus")
def upload_module_syllabus(module_id: int, payload: dict, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """
    FR-3: Upload syllabus text; AI parses it into classes.
    mode='replace' -> parsed plan only (source=syllabus); mode='merge' -> keep manual classes (source=both).
    The module moves to 'In Review' (FR-4) and cannot be used for generation until locked.
    """
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")
    actor = _require_plan_editor(db, user_id, mod)

    text_in = (payload.get("syllabus_text") or "").strip()
    if not text_in:
        raise HTTPException(status_code=400, detail="syllabus_text is required")
    mode = (payload.get("mode") or "merge").lower()

    parsed = _parse_syllabus_text(text_in)
    if not parsed:
        raise HTTPException(status_code=422, detail="Could not parse any classes from the syllabus.")

    had_manual = len(mod.classes) > 0
    if mode == "replace":
        for c in list(mod.classes):
            if not c.scripts:
                db.delete(c)
        db.flush()
    start_seq = max([c.sequence_number for c in mod.classes] or [0]) + 1

    for i, item in enumerate(parsed):
        db.add(Class(
            module_id=mod.id,
            sequence_number=start_seq + i,
            title=str(item.get("title", "")).strip() or f"Class {start_seq + i}",
            agendas=json.dumps(item.get("agendas") or []),
            learning_outcomes=item.get("learning_outcomes") or None,
            duration_minutes=int(item.get("duration_minutes") or 90),
        ))

    mod.plan_source = "both" if (had_manual and mode == "merge") else "syllabus"
    mod.plan_status = "In Review"
    mod.is_locked = False
    db.commit()
    db.refresh(mod)

    log_activity(db, user_name=actor, user_role="mo", action_type="Syllabus Parsed",
                 description=f"Parsed {len(parsed)} classes into module '{mod.name}' ({mod.plan_source}); sent to review")
    return {"parsed_classes": len(parsed), "completeness": evaluate_module_completeness(mod)}

@app.post("/api/modules/{module_id}/plan/submit-review")
def submit_module_plan_for_review(module_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """FR-4: manual or parsed plan enters review. Unlocks the module for edits."""
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")
    actor = _require_plan_editor(db, user_id, mod)
    mod.plan_status = "In Review"
    mod.is_locked = False
    db.commit()
    log_activity(db, user_name=actor, user_role="mo", action_type="Module Plan In Review",
                 description=f"Module '{mod.name}' plan submitted for review")
    return evaluate_module_completeness(mod)

@app.post("/api/modules/{module_id}/plan/lock")
def lock_module_plan(module_id: int, payload: Optional[dict] = None, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """FR-4: reviewer locks the plan. Lock is refused unless all FR-6 content checks pass."""
    mod = db.query(Module).filter(Module.id == module_id).first()
    if not mod:
        raise HTTPException(status_code=404, detail="Module not found")
    actor = _require_plan_editor(db, user_id, mod)

    if payload and payload.get("owner_name"):
        mod.owner_name = payload["owner_name"].strip()

    # Evaluate as if locked, so the only remaining issues are content/ownership
    prev_status = mod.plan_status
    mod.plan_status = "Locked"
    report = evaluate_module_completeness(mod)
    if not report["is_complete"]:
        mod.plan_status = prev_status
        db.rollback()
        raise HTTPException(status_code=400, detail={"message": "Module plan cannot be locked", "issues": report["issues"]})

    mod.is_locked = True
    db.commit()
    log_activity(db, user_name=actor, user_role="mo", action_type="Module Plan Locked",
                 description=f"Reviewed and locked module '{mod.name}' ({report['class_count']} classes)")
    return report

@app.post("/api/scripts/init")
def initialize_script(payload: ScriptCreateRequest, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Accepts the 10 Canonical Inputs and creates a WIP script record."""
    cls = db.query(Class).filter(Class.id == payload.class_id).first()
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    actor = _require_actor(db, user_id)
    if actor.role == "pm":
        raise HTTPException(status_code=403, detail="Program Managers have read-only access and cannot generate scripts.")
    if actor.role == "admin":
        raise HTTPException(status_code=403, detail="Administrators manage the platform and cannot author scripts.")
    prog = cls.module.track.program if cls.module and cls.module.track else None
    if prog and prog.id not in [p.id for p in actor.programs]:
        raise HTTPException(status_code=403, detail=f"User has not joined program '{prog.name}'. Access denied.")

    # FR-6 Gate: program must have at least one complete, locked module
    if not cls.module or not cls.module.track:
        raise HTTPException(status_code=400, detail="Cannot initialize script: Class does not belong to a valid module.")

    program = cls.module.track.program
    complete_modules = [
        m for t in program.tracks for m in t.modules
        if evaluate_module_completeness(m)["is_complete"]
    ]
    if not complete_modules:
        raise HTTPException(
            status_code=400,
            detail=f"Script creation is disabled for '{program.name}': no complete module exists yet. "
                   "A module is complete when every class has a title and agendas, "
                   "an SME/owner is assigned, and the plan is locked after review."
        )

    this_module = evaluate_module_completeness(cls.module)
    if not this_module["is_complete"]:
        raise HTTPException(
            status_code=400,
            detail=f"Module '{cls.module.name}' is not complete: " + "; ".join(this_module["issues"][:5])
        )

    # Pull Class N-1 memory
    # a previous script the author uploads wins; otherwise the one the Module Owner attached to the class is the reference (FR-19)
    memory = retrieve_context_memory(db, payload.class_id, (payload.previous_script_context or "").strip() or cls.reference_script)

    # The SME's learning outcomes are kept with THIS script. The class keeps the Module Owner's, and is not changed by a script.

    author_user = db.query(User).filter(User.id == user_id).first() if user_id else None
    sme_author_name = author_user.name if author_user else "Aryan (SME)"

    custom_agendas_json = json.dumps(payload.custom_agendas) if payload.custom_agendas else None
    effective_title = payload.custom_class_title or (cls.title if cls else "Lecture Script")

    script = Script(
        class_id=cls.id,
        duration_minutes=payload.duration_minutes,
        content_mix_beginner=payload.content_mix_beginner,
        content_mix_advanced=payload.content_mix_advanced,
        code_ratio=payload.code_ratio,
        additional_resources=payload.additional_resources,
        learning_outcomes=(payload.learning_outcomes or "").strip() or None,
        api_docs=payload.api_docs,
        reference_urls=payload.reference_urls,
        previous_script_context=json.dumps(memory),
        custom_agendas=custom_agendas_json,
        custom_class_title=payload.custom_class_title,
        review_lenses=json.dumps(_clean_lenses(payload.review_lenses)) if payload.review_lenses else None,      # none picked: the track decides (see _script_lenses)
        user_id=author_user.id if author_user else None,
        sme_name=sme_author_name,
        status="Script WIP"
    )
    db.add(script)
    db.commit()
    db.refresh(script)

    log_activity(
        db,
        user_name=sme_author_name,
        user_role="sme",
        action_type="Script Initialized",
        description=f"Initialized draft script #{script.id} for '{effective_title}' ({cls.duration_minutes} mins)"
    )

    return {
        "script_id": script.id,
        "shareable_uuid": script.shareable_uuid,
        "status": script.status,
        "message": "Script initialized with 10 canonical inputs. Ready for outline generation.",
        "memory_ledger": memory
    }

# ==============================================================================
# 3. OUTLINE GENERATION & HUMAN GATE ([GTG], [Edit], [ReThink])
# ==============================================================================

@app.post("/api/scripts/{script_id}/outline", response_model=OutlineResponse)
def create_script_outline(script_id: int, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Outliner Agent generates card outline grouped into sections summing to exact duration."""
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    cls = script.class_entity
    effective_title = script.custom_class_title or (cls.title if cls else "Lecture Script")

    if script.custom_agendas:
        try:
            agendas = json.loads(script.custom_agendas)
        except:
            agendas = [script.custom_agendas]
    else:
        try:
            agendas = json.loads(cls.agendas) if cls and cls.agendas else []
        except:
            agendas = [cls.agendas] if cls else []

    try:
        memory = json.loads(script.previous_script_context) if script.previous_script_context else {}
    except:
        memory = {}

    from agents.shared_context import build_block_a_context
    from agents.outliner import generate_outline as generate_sections_outline

    block_a = build_block_a_context(
        db=db,
        class_id=script.class_id,
        duration_minutes=script.duration_minutes,
        content_mix_beginner=script.content_mix_beginner,
        code_ratio=script.code_ratio,
        custom_title=effective_title,
        custom_agendas=agendas,
        learning_outcomes=_effective_outcomes(script),
        api_docs=script.api_docs,
        reference_urls=script.reference_urls,
        additional_resources=_resources_with_feedback(script),
        previous_script_context=script.previous_script_context
    )

    trk_name = cls.module.track.name if cls and cls.module and cls.module.track else "Core"

    # Call the Outliner agent directly to get section-grouped outline
    outline_data = generate_sections_outline(
        class_title=effective_title,
        agendas=agendas,
        duration_minutes=script.duration_minutes,
        content_mix_beginner=script.content_mix_beginner,
        code_ratio=script.code_ratio,
        memory_context=memory,
        block_a_context=block_a,
        learning_outcomes=_effective_outcomes(script),
        additional_resources=_resources_with_feedback(script),
        api_docs=script.api_docs,
        reference_urls=script.reference_urls,
        track=trk_name
    )

    # The outline is checked by code. When it fails on something that matters, ask for a corrected one (up to twice) and keep the
    # best. What is still wrong on the card count is then fixed by code, by merging the weakest cards into their neighbours.
    from agents.outliner import enforce_card_count, ensure_animations, min_animations_for, _normalize_outline_duration, normalize_visual_cards
    best, best_serious = outline_data, [c for c in _outline_checks(script, outline_data) if c.get("level") in ("blocker", "major")]
    for _attempt in range(2):
        if not best_serious:
            break
        try:
            retry = generate_sections_outline(
                class_title=effective_title, agendas=agendas, duration_minutes=script.duration_minutes,
                content_mix_beginner=script.content_mix_beginner, code_ratio=script.code_ratio, memory_context=memory,
                block_a_context=block_a, learning_outcomes=_effective_outcomes(script),
                additional_resources=_resources_with_feedback(script), api_docs=script.api_docs,
                reference_urls=script.reference_urls, fix_problems=[c["problem"] for c in
                                                                     _outline_checks(script, best) if c.get("level") in ("blocker", "major")],
                track=trk_name, script_id=script.id)
            retry_serious = [c for c in _outline_checks(script, retry) if c.get("level") in ("blocker", "major")]
            if len(retry_serious) < len(best_serious):
                best, best_serious = retry, retry_serious
        except Exception as e:      # a failed retry keeps the best outline so far, with its checks shown
            print(f"Outline correction skipped: {e}")
            break
    outline_data = best
    if isinstance(outline_data, dict) and outline_data.get("sections"):
        from agents.outliner import merge_opening_cards
        outline_data = merge_opening_cards(outline_data, int(script.duration_minutes), program_opening=_program_opening(script))
        outline_data = enforce_card_count(outline_data, int(script.duration_minutes))
        outline_data = ensure_animations(outline_data, min_animations_for(trk_name))
        from agents.outliner import finalize_plan
        outline_data = finalize_plan(outline_data, script.content_mix_beginner, script.code_ratio)    # the SME's split and code ratio, planned per card

    # Save outline JSON to database (stores sections, cards, and running example)
    script.outline_json = json.dumps(outline_data)
    db.commit()

    # Flatten cards for legacy OutlineCardItem response compatibility
    flat_cards = []
    card_idx = 1
    sections = outline_data.get("sections", [])
    for sec in sections:
        for c in sec.get("cards", []):
            dur_mins = round(c.get("duration_sec", 300) / 60.0, 1)
            agendas_preview = ", ".join(c.get("agenda_items", [])) if c.get("agenda_items") else ""
            concept_val = agendas_preview or c.get("teaching_shape_note") or c.get("title", "")
            if concept_val == "Why -> What -> How -> Where -> What breaks":
                concept_val = c.get("title", "")

            flat_cards.append({
                "card_index": card_idx,
                "title": c.get("title", f"Card {card_idx}"),
                "duration_mins": dur_mins,
                "pedagogical_phase": c.get("card_type", "Concept").title(),
                "key_concept": concept_val,
                "visual_type": "mermaid" if c.get("visuals_planned") else "none",
                "level": c.get("level"), "has_code": c.get("has_code")
            })
            card_idx += 1

    total_target = float(script.duration_minutes)
    if flat_cards:
        current_sum = round(sum(c.get("duration_mins", 0) for c in flat_cards), 1)
        rem = round(total_target - current_sum, 1)
        if rem != 0:
            mid = len(flat_cards) // 2
            flat_cards[mid]["duration_mins"] = round(flat_cards[mid]["duration_mins"] + rem, 1)

    return OutlineResponse(
        script_id=script.id,
        class_title=effective_title,
        total_duration_mins=total_target,
        total_cards=len(flat_cards),
        cards=flat_cards,
        sections=sections,
        visual_cards=outline_data.get('visual_cards') if isinstance(outline_data, dict) else None,
        checks=_outline_checks(script, outline_data),
        overlap_flags=_outline_overlaps(script, outline_data),
        plan_summary=outline_data.get("plan_summary") if isinstance(outline_data, dict) else None,
        continuity=_continuity_summary(script, db),
        running_example=outline_data.get("running_example"),
        assumptions=outline_data.get("assumptions"),
        overlap_analysis=f"{len(outline_data.get('overlap_flags', []))} overlap flags. {len(outline_data.get('visual_cards') or [])} visual cards planned."
    )


def _resources_with_feedback(script) -> Optional[str]:
    """The SME's reference material, plus what the Module Owner asked to change while the script is being revised, so the outline and the cards answer it."""
    base = script.additional_resources
    if script.revision_note and script.status != "GTG":
        ask = (f"\n\nMODULE OWNER FEEDBACK ON THE PREVIOUS VERSION (this version must fix every point; do not repeat the problem):\n{script.revision_note.strip()}")
        return ((base or "").rstrip() + ask).strip()
    return base


def _effective_outcomes(script) -> Optional[str]:
    """The learning outcomes this script works to: the Module Owner's for the class, plus the SME's own for this script (each line once)."""
    from agents.shared_context import _PLACEHOLDER_OUTCOME_PREFIX
    cls = script.class_entity
    mo = ((cls.learning_outcomes or "") if cls else "").strip()
    if mo.startswith(_PLACEHOLDER_OUTCOME_PREFIX):
        mo = ""                                              # the generic text written when nobody gave any
    sme = (getattr(script, "learning_outcomes", None) or "").strip()
    if mo and sme and sme.lower() not in mo.lower():
        return mo + "\nThe SME also wants: " + sme
    return mo or sme or None


def _continuity_context(script, db: Session) -> dict:
    """What the classes around this one mean for it: the class before (and whether its script is approved, with the record of what it taught),
    the classes after (their topics, which this script must not teach), and every term the earlier classes taught."""
    from agents.shared_context import agenda_is_placeholder, _loads_list
    cls = script.class_entity
    mod = cls.module if cls is not None else None
    if mod is None:
        return {"previous": None, "upcoming": [], "known_terms": set()}
    siblings = [c for c in db.query(Class).filter(Class.module_id == mod.id).order_by(Class.sequence_number).all() if c.class_type != "Archived" and c.sequence_number < 1000]
    past = [c for c in siblings if c.sequence_number < cls.sequence_number]
    upcoming = []
    for c in siblings:
        if c.sequence_number > cls.sequence_number:
            lines = _loads_list(c.agendas)
            upcoming.append({"number": c.sequence_number, "title": c.title, "items": [] if agenda_is_placeholder(lines) else [str(x) for x in lines if isinstance(x, str)]})
    ledgers = {l.class_id: l for l in db.query(MemoryLedger).filter(MemoryLedger.class_id.in_([c.id for c in past])).all()} if past else {}
    known = set()
    for l in ledgers.values():
        try:
            known |= {str(t).strip().lower() for t in json.loads(l.terms_mastered or "[]") if str(t).strip()}
        except ValueError:
            pass
    previous = None
    if past:
        last = past[-1]
        approved = db.query(Script).filter(Script.class_id == last.id, Script.status == "GTG").order_by(Script.id.desc()).first()
        try:
            memory = json.loads(ledgers[last.id].summary_json) if last.id in ledgers and ledgers[last.id].summary_json else None
        except ValueError:
            memory = None
        if not (memory and memory.get("points")):                      # no approved record: the class's newest draft stands in, and the screen says so
            from agents.class_memory import latest_script_memory
            memory = latest_script_memory(db, last.id, cls.title)
        previous = {"number": last.sequence_number, "title": last.title, "approved": approved is not None, "draft": bool(memory and memory.get("draft_status")),
                    "approved_by": approved.approved_by_name if approved is not None else None,
                    "approved_at": approved.approved_at.strftime("%Y-%m-%d") if approved is not None and approved.approved_at else None,
                    "ending": (memory or {}).get("ending") or "", "has_record": bool(memory and memory.get("points"))}
        if memory and memory.get("draft_status"):
            known |= {str(t).strip().lower() for t in memory.get("terms") or [] if str(t).strip()}      # a draft's terms count as known too
    return {"previous": previous, "upcoming": upcoming, "known_terms": known}


def _continuity_summary(script, db: Session) -> dict:
    """The part of the continuity the outline screen shows."""
    c = _continuity_context(script, db)
    return {"previous": {k: v for k, v in (c["previous"] or {}).items() if k in ("number", "title", "approved", "approved_by", "approved_at", "has_record", "draft")} or None,
            "upcoming": [{"number": u["number"], "title": u["title"]} for u in c["upcoming"]]}


def _agenda_timing_for(script, outline) -> list:
    """The class agenda, each item with the minutes of the cards that teach it, for the agenda card."""
    from agents.timeline import agenda_timing
    if not isinstance(outline, dict) or not outline.get("sections"):
        return []
    _title, agendas, _outcomes = _script_scope(script)
    return agenda_timing([c for _sid, c in _outline_card_list(outline)], agendas)


def _effective_title(script) -> str:
    cls = script.class_entity
    return script.custom_class_title or (cls.title if cls else "Lecture Script")


def _script_scope(script):
    """(title, agenda lines, learning outcomes) the script must stay inside."""
    cls = script.class_entity
    title = script.custom_class_title or (cls.title if cls else "Lecture Script")
    try:
        agendas = json.loads(script.custom_agendas) if script.custom_agendas else (json.loads(cls.agendas) if cls and cls.agendas else [])
    except Exception:
        agendas = [cls.agendas] if cls and cls.agendas else []
    from agents.shared_context import agenda_is_placeholder
    agendas = [a for a in agendas if isinstance(a, str)]
    if agenda_is_placeholder(agendas):
        agendas = []                       # a template agenda is not a scope to check against
    outcomes = _effective_outcomes(script) or ""
    return title, agendas, outcomes


def _opening_kit(script):
    """The program kit when this class is a track opening that has one, else None."""
    from sqlalchemy.orm import object_session
    from agents.shared_context import gather_block_a
    db = object_session(script)
    if db is None or script.class_entity is None:
        return None
    data = gather_block_a(db, script.class_id, duration_minutes=script.duration_minutes,
                          content_mix_beginner=script.content_mix_beginner, code_ratio=script.code_ratio)
    return data.get("opening_kit") if data else None


def _open_findings(script) -> Optional[int]:
    """How many blocker and major findings the last AI review left open (None when it was never run)."""
    if not script.review_json:
        return None
    try:
        reviews = json.loads(script.review_json).get("reviews") or []
    except ValueError:
        return None
    return sum(1 for r in reviews for f in (r.get("findings") or []) if f.get("severity") in ("blocker", "major"))


def _clean_lenses(chosen) -> list:
    """The review lenses the SME picked, only real ones, in the usual order; none picked means all of them."""
    from agents.reviewer import ALL_LENSES, DEFAULT_LENSES
    picked = [k for k in ALL_LENSES if k in (chosen or [])]
    return picked or list(DEFAULT_LENSES)


def _program_of(script) -> Optional[str]:
    cls = script.class_entity
    return cls.module.track.program.name if cls is not None and cls.module is not None and cls.module.track is not None else None


def _track_of(script) -> Optional[str]:
    cls = script.class_entity
    return cls.module.track.name if cls is not None and cls.module is not None and cls.module.track is not None else None


def _script_lenses(script) -> list:
    """The reviewers the author picked; when none were picked, the ones that fit the script's track (a Beginner Track gets the beginner reader, and so on)."""
    from agents.reviewer import lenses_for_track
    try:
        chosen = json.loads(script.review_lenses) if script.review_lenses else None
    except ValueError:
        chosen = None
    if chosen:
        return _clean_lenses(chosen)
    return list(lenses_for_track(_track_of(script), _program_of(script)))


def _opening_welcome(script) -> str:
    """The welcome that opens this class if it is the first class of its track (each track has its own), else the program's approved one."""
    from sqlalchemy.orm import object_session
    from agents.shared_context import gather_block_a, OPENING_WELCOME
    db = object_session(script)
    if db is None or script.class_entity is None:
        return OPENING_WELCOME
    data = gather_block_a(db, script.class_id, duration_minutes=script.duration_minutes,
                          content_mix_beginner=script.content_mix_beginner, code_ratio=script.code_ratio)
    return (data or {}).get("opening_welcome") or OPENING_WELCOME


def _program_opening(script) -> bool:
    """True when this class is the first class of its whole program (learners have never been in a Scaler class)."""
    from sqlalchemy.orm import object_session
    from agents.shared_context import gather_block_a
    db = object_session(script)
    if db is None or script.class_entity is None:
        return False
    data = gather_block_a(db, script.class_id, duration_minutes=script.duration_minutes,
                          content_mix_beginner=script.content_mix_beginner, code_ratio=script.code_ratio)
    return bool(data and data.get("program_opening"))


def _opening_kit_topics(script):
    """The topics an opening class must cover, when this class is a track opening that has a kit."""
    kit = _opening_kit(script)
    return list(kit.get("onboarding_cards", [])) if kit else None


def _track_name(script) -> str:
    cls = script.class_entity
    return cls.module.track.name if cls is not None and cls.module is not None and cls.module.track is not None else ""


from sqlalchemy.orm import object_session


def _outline_overlaps(script, outline_data) -> list:
    """FR-10: what in this outline is also taught by another class of the module. For each teaching card: a topic that belongs to an UPCOMING class (the
    card must not teach it) or one an EARLIER class already covered (learners know it, so it should be a reminder at most). The Outliner's own flags are kept too."""
    from sqlalchemy.orm import object_session
    from agents.rule_checker import upcoming_overlap, covered, _topic_words, _without_exclusions, class_scope_words
    out = []
    db = object_session(script)
    if not isinstance(outline_data, dict) or db is None:
        return out
    title, agendas, outcomes = _script_scope(script)
    cont = _continuity_context(script, db)
    own = class_scope_words(agendas, outcomes, title, cont["upcoming"])
    cls = script.class_entity
    past = []
    if cls is not None and cls.module is not None:
        from agents.shared_context import agenda_is_placeholder, _loads_list
        for c in db.query(Class).filter(Class.module_id == cls.module_id, Class.sequence_number < cls.sequence_number).order_by(Class.sequence_number).all():
            lines = _loads_list(c.agendas)
            if c.class_type != "Archived" and not agenda_is_placeholder(lines):
                past.append({"number": c.sequence_number, "title": c.title, "items": [str(x) for x in lines if isinstance(x, str)]})
    n = 0
    for sec in outline_data.get("sections", []) or []:
        for card in sec.get("cards", []) or []:
            n += 1
            if card.get("card_type") not in ("concept", "problem", "walkthrough"):
                continue
            text = " ".join([card.get("title") or "", _without_exclusions(card.get("teaching_shape_note"))] + list(card.get("agenda_items") or []))     # a sentence that says what NOT to teach is no overlap
            hit = upcoming_overlap(text, cont["upcoming"], own, sentences_needed=0) if cont["upcoming"] else None
            if hit:
                out.append({"kind": "upcoming", "card_index": n, "card_title": card.get("title"), "class_number": hit["number"], "class_title": hit["class"], "topic": hit["item"],
                            "note": f"Class {hit['number']} \"{hit['class']}\" teaches this later, so this class should not."})
                continue
            for pc in past:
                item = next((i for i in pc["items"] if covered(i, [text], threshold=0.75) and not covered(i, [" ".join(agendas or [])], threshold=0.75)), None)
                if item:
                    out.append({"kind": "past", "card_index": n, "card_title": card.get("title"), "class_number": pc["number"], "class_title": pc["title"], "topic": item,
                                "note": f"Class {pc['number']} \"{pc['title']}\" already covered this, so learners know it. Keep it to a reminder."})
                    break
    for f in outline_data.get("overlap_flags") or []:               # what the Outliner itself noticed
        if isinstance(f, str) and f.strip():
            out.append({"kind": "outliner", "note": f.strip()})
        elif isinstance(f, dict):
            out.append({"kind": "outliner", "note": str(f.get("note") or f.get("problem") or f.get("overlap") or f.get("description") or json.dumps(f))[:300]})
    return out


def _outline_checks(script, outline_data):
    if not isinstance(outline_data, dict):
        return []
    from agents.rule_checker import check_outline
    from agents.outliner import min_animations_for
    title, agendas, outcomes = _script_scope(script)
    return check_outline(outline_data, int(script.duration_minutes), agendas, outcomes, title,
                         required_topics=_opening_kit_topics(script), min_animations=min_animations_for(_track_name(script)),
                         beginner_pct=script.content_mix_beginner, code_ratio=script.code_ratio,
                         upcoming=_continuity_context(script, object_session(script))["upcoming"] if object_session(script) is not None else None)

@app.get("/api/scripts/{script_id}/outline", response_model=OutlineResponse)
def get_script_outline(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """Retrieves the currently saved card outline for a script."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not script.outline_json:
        raise HTTPException(status_code=404, detail="No outline found for this script")

    outline_data = json.loads(script.outline_json)
    sections = outline_data.get("sections", []) if isinstance(outline_data, dict) else []
    cls = script.class_entity
    effective_title = script.custom_class_title or (cls.title if cls else "Lecture Script")

    flat_cards = []
    card_idx = 1
    if sections:
        for sec in sections:
            for c in sec.get("cards", []):
                dur_mins = round(c.get("duration_sec", 300) / 60.0, 1)
                agendas_preview = ", ".join(c.get("agenda_items", [])) if c.get("agenda_items") else ""
                concept_val = agendas_preview or c.get("teaching_shape_note") or c.get("title", "")
                if concept_val == "Why -> What -> How -> Where -> What breaks":
                    concept_val = c.get("title", "")

                flat_cards.append({
                    "card_index": card_idx,
                    "title": c.get("title", f"Card {card_idx}"),
                    "duration_mins": dur_mins,
                    "pedagogical_phase": c.get("card_type", "Concept").title(),
                    "key_concept": concept_val,
                    "visual_type": "mermaid" if c.get("visuals_planned") else "none",
                "level": c.get("level"), "has_code": c.get("has_code")
                })
                card_idx += 1
    elif isinstance(outline_data, list):
        flat_cards = outline_data

    total_target = float(script.duration_minutes)
    if flat_cards:
        current_sum = round(sum(c.get("duration_mins", 0) for c in flat_cards), 1)
        rem = round(total_target - current_sum, 1)
        if rem != 0:
            mid = len(flat_cards) // 2
            flat_cards[mid]["duration_mins"] = round(flat_cards[mid]["duration_mins"] + rem, 1)

    return OutlineResponse(
        script_id=script.id,
        class_title=effective_title,
        total_duration_mins=total_target,
        total_cards=len(flat_cards),
        cards=flat_cards,
        sections=sections,
        visual_cards=outline_data.get('visual_cards') if isinstance(outline_data, dict) else None,
        checks=_outline_checks(script, outline_data),
        overlap_flags=_outline_overlaps(script, outline_data),
        plan_summary=outline_data.get("plan_summary") if isinstance(outline_data, dict) else None,
        continuity=_continuity_summary(script, db),
        running_example=outline_data.get("running_example") if isinstance(outline_data, dict) else None,
        assumptions=outline_data.get("assumptions") if isinstance(outline_data, dict) else None,
        overlap_analysis="Loaded active outline."
    )

def _save_flat_edits(script, modified_cards):
    """The outline screen sends its cards as a flat list. Fold the edits into the structured outline instead of replacing it, so the
    sections, running example, agenda mapping and visual cards (everything the Writer is prompted with) are kept."""
    from agents.outliner import apply_flat_edits
    edits = [c.dict() for c in modified_cards]
    try:
        current = json.loads(script.outline_json) if script.outline_json else None
    except ValueError:
        current = None
    if isinstance(current, dict) and current.get("sections"):
        from agents.outliner import finalize_plan
        script.outline_json = json.dumps(finalize_plan(apply_flat_edits(current, edits), script.content_mix_beginner, script.code_ratio, reorder=not script.cards))
    else:
        script.outline_json = json.dumps(edits)          # an older flat outline has no structure to keep


@app.post("/api/scripts/{script_id}/outline/action")
def handle_outline_action(script_id: int, payload: OutlineApprovalRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Human Gate: SME selects [GTG], [Edit], or [ReThink]."""
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    action = payload.action.upper()
    if action == "GTG":
        if script.regenerate_pending:                               # a revision: the version the Module Owner saw is replaced by the one now written from this outline
            card_ids = [c.id for c in script.cards]
            if card_ids:
                db.query(VisualAsset).filter(VisualAsset.card_id.in_(card_ids)).delete(synchronize_session=False)
            for c in list(script.cards):
                db.delete(c)
            script.regenerate_pending = False
            db.flush()
        script.outline_approved = True
        if payload.modified_sections:
            try:
                curr = json.loads(script.outline_json) if script.outline_json else {}
                curr["sections"] = payload.modified_sections
                from agents.outliner import normalize_visual_cards
                script.outline_json = json.dumps(normalize_visual_cards(curr))
            except Exception:
                pass
        elif payload.modified_cards:
            _save_flat_edits(script, payload.modified_cards)
        db.commit()
        return {"status": "SUCCESS", "message": "Outline locked as GTG. Section-by-section generation unlocked."}

    elif action == "EDIT":
        if payload.modified_sections:
            try:
                curr = json.loads(script.outline_json) if script.outline_json else {}
                curr["sections"] = payload.modified_sections
                script.outline_json = json.dumps(curr)
            except Exception:
                pass
        elif payload.modified_cards:
            _save_flat_edits(script, payload.modified_cards)
        db.commit()
        return {"status": "SUCCESS", "message": "Outline modifications saved."}

    elif action == "RETHINK":
        script.outline_approved = False
        db.commit()
        # Regenerate with SME rethink feedback
        cls = script.class_entity
        effective_title = script.custom_class_title or (cls.title if cls else "Lecture Script")
        try:
            agendas = json.loads(script.custom_agendas) if script.custom_agendas else (json.loads(cls.agendas) if cls and cls.agendas else [])
        except:
            agendas = [cls.agendas] if cls else []
        try:
            memory = json.loads(script.previous_script_context) if script.previous_script_context else {}
        except:
            memory = {}

        from agents.shared_context import build_block_a_context
        from agents.outliner import generate_outline as generate_sections_outline

        block_a = build_block_a_context(
            db=db,
            class_id=script.class_id,
            duration_minutes=script.duration_minutes,
            content_mix_beginner=script.content_mix_beginner,
            code_ratio=script.code_ratio,
            custom_title=effective_title,
            custom_agendas=agendas,
            learning_outcomes=_effective_outcomes(script),
            api_docs=script.api_docs,
            reference_urls=script.reference_urls,
            additional_resources=_resources_with_feedback(script),
            previous_script_context=script.previous_script_context
        )
        trk_name = cls.module.track.name if cls and cls.module and cls.module.track else "Core"

        outline_data = generate_sections_outline(
            class_title=effective_title,
            agendas=agendas,
            duration_minutes=script.duration_minutes,
            content_mix_beginner=script.content_mix_beginner,
            code_ratio=script.code_ratio,
            memory_context=memory,
            block_a_context=block_a,
            learning_outcomes=_effective_outcomes(script),
            additional_resources=_resources_with_feedback(script),
            api_docs=script.api_docs,
            reference_urls=script.reference_urls,
            rethink_feedback=payload.revision_feedback,
            track=trk_name,
            script_id=script.id
        )
        if isinstance(outline_data, dict) and outline_data.get("sections"):
            from agents.outliner import enforce_card_count, ensure_animations, min_animations_for
            from agents.outliner import merge_opening_cards
            outline_data = merge_opening_cards(outline_data, int(script.duration_minutes), program_opening=_program_opening(script))
            outline_data = ensure_animations(enforce_card_count(outline_data, int(script.duration_minutes)), min_animations_for(trk_name))
            from agents.outliner import finalize_plan
            outline_data = finalize_plan(outline_data, script.content_mix_beginner, script.code_ratio)
        script.outline_json = json.dumps(outline_data)
        db.commit()

        flat_cards = []
        card_idx = 1
        sections = outline_data.get("sections", [])
        for sec in sections:
            for c in sec.get("cards", []):
                dur_mins = round(c.get("duration_sec", 300) / 60.0, 1)
                agendas_preview = ", ".join(c.get("agenda_items", [])) if c.get("agenda_items") else ""
                concept_val = agendas_preview or c.get("teaching_shape_note") or c.get("title", "")
                if concept_val == "Why -> What -> How -> Where -> What breaks":
                    concept_val = c.get("title", "")

                flat_cards.append({
                    "card_index": card_idx,
                    "title": c.get("title", f"Card {card_idx}"),
                    "duration_mins": dur_mins,
                    "pedagogical_phase": c.get("card_type", "Concept").title(),
                    "key_concept": concept_val,
                    "visual_type": "mermaid" if c.get("visuals_planned") else "none",
                "level": c.get("level"), "has_code": c.get("has_code")
                })
                card_idx += 1

        total_target = float(script.duration_minutes)
        if flat_cards:
            current_sum = round(sum(c.get("duration_mins", 0) for c in flat_cards), 1)
            rem = round(total_target - current_sum, 1)
            if rem != 0:
                mid = len(flat_cards) // 2
                flat_cards[mid]["duration_mins"] = round(flat_cards[mid]["duration_mins"] + rem, 1)

        return OutlineResponse(
            script_id=script.id,
            class_title=effective_title,
            total_duration_mins=total_target,
            total_cards=len(flat_cards),
            cards=flat_cards,
            sections=sections,
            visual_cards=outline_data.get('visual_cards') if isinstance(outline_data, dict) else None,
            checks=_outline_checks(script, outline_data),
        overlap_flags=_outline_overlaps(script, outline_data),
        plan_summary=outline_data.get("plan_summary") if isinstance(outline_data, dict) else None,
        continuity=_continuity_summary(script, db),
            running_example=outline_data.get("running_example"),
            assumptions=outline_data.get("assumptions"),
            overlap_analysis=f"ReThink applied with feedback: '{payload.revision_feedback or 'Divergent structure requested'}'"
        )

    raise HTTPException(status_code=400, detail="Invalid action. Must be GTG, Edit, or ReThink.")

@app.post("/api/scripts/{script_id}/outline/card-assist")
def outline_card_assist(
    script_id: int,
    payload: OutlineCardAssistRequest,
    _actor: User = Depends(script_guard(True)),
    db: Session = Depends(get_db)
):
    """
    SME Chat / Prompt Assist for a single outline card before locking GTG.
    Revises the targeted card in place according to SME instructions.
    """
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script or not script.outline_json:
        raise HTTPException(status_code=404, detail="Script or outline not found")

    outline_data = json.loads(script.outline_json)
    sections = outline_data.get("sections", [])
    
    # Locate targeted card by card_id or card_index
    target_card = None
    target_sec = None
    curr_idx = 1
    for sec in sections:
        for c in sec.get("cards", []):
            if (payload.card_id and c.get("card_id") == payload.card_id) or (payload.card_index and curr_idx == payload.card_index):
                target_card = c
                target_sec = sec
                break
            curr_idx += 1
        if target_card:
            break

    if not target_card:
        # Fallback if outline is flat cards
        if isinstance(outline_data, list):
            for c in outline_data:
                if c.get("card_index") == payload.card_index:
                    target_card = c
                    break

    if not target_card:
        raise HTTPException(status_code=404, detail="Target outline card not found")

    # Apply inline assist with Claude
    execution_mode = os.getenv("EXECUTION_MODE", "hybrid")

    if llm_configured() and execution_mode != "mock":
        try:
            import copy
            before_edit = copy.deepcopy(target_card)
            prompt = f"""You are the Outliner Agent assistant.
Revise this outline card based on the SME directive:

CARD TO REVISE:
{json.dumps(target_card, indent=2)}

SME DIRECTIVE:
{payload.user_prompt}

RULES:
- Keep duration inside the range for the card type: {_assist_ranges()}.
- Everyday daily-life analogies only (UPI, WhatsApp, train booking, food delivery).
- Return ONLY valid JSON representing the updated card object.
"""
            raw = call_llm_chat(messages=[{"role": "user", "content": prompt}], max_tokens=2000, agent="outline_assist")

            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()
            updated_card = json.loads(raw)
            target_card.update(updated_card)
            _keep_what_was_not_asked(before_edit, target_card, payload.user_prompt)
        except Exception as e:
            # Fallback intelligent local editor
            _apply_local_card_directive(target_card, payload.user_prompt)
    else:
        _apply_local_card_directive(target_card, payload.user_prompt)

    _rebalance_after_edit(outline_data, target_card, int(script.duration_minutes))      # the minutes still add up to the class length, and the card is inside its range
    script.outline_json = json.dumps(outline_data)
    db.commit()
    return {"status": "SUCCESS", "message": "Card updated successfully", "updated_card": target_card}


def _assist_ranges() -> str:
    from agents.shared_context import LIMITS
    t, q, n = LIMITS["teaching_card_minutes"], LIMITS["quiz_card_minutes"], LIMITS["note_card_minutes"]
    return f"{t[0]}-{t[1]} minutes for concept, problem, walkthrough, faq and recap cards, {q[0]}-{q[1]} for quiz cards, {n[0]}-{n[1]} for instructor note, welcome and agenda cards"


def _keep_what_was_not_asked(before: dict, card: dict, directive: str) -> None:
    """The Chat for a card is asked for one change, but the AI hands the whole card back. Whatever the author's words did not ask about stays as it was: the agenda items and
    outcomes (the author's own), the planned pictures, the duration and the card's id and type."""
    low = (directive or "").lower()
    card["card_id"] = before.get("card_id", card.get("card_id"))
    if not re.search(r"agenda|outcome|objective|learning goal", low):
        card["agenda_items"], card["outcomes"] = before.get("agenda_items", []), before.get("outcomes", [])
    if not re.search(r"diagram|visual|animation|picture|image|chart|figure", low):
        card["visuals_planned"] = before.get("visuals_planned", [])
    if not re.search(r"minute|\bmins?\b|duration|longer|shorter|\btime\b|seconds", low):
        card["duration_sec"] = before.get("duration_sec", card.get("duration_sec"))
    if not re.search(r"\btype\b|turn (it )?into|change (it )?to a|make (it )?a (quiz|concept|walkthrough)", low):
        card["card_type"] = before.get("card_type", card.get("card_type"))


def _rebalance_after_edit(outline_data: dict, edited: dict, target_minutes: int) -> None:
    """After an edit: the edited card is held inside the range for its type, and the other teaching and quiz cards take up the difference in 6 second steps so the class
    adds up to exactly its length again. Notes, welcome and agenda cards keep their time. If there is no room, nothing is forced."""
    from agents.shared_context import LIMITS, TEACHING_CARD_TYPES
    cards = [c for s in outline_data.get("sections", []) or [] for c in s.get("cards", []) or []]
    if not cards:
        return

    def span(c):
        k = str(c.get("card_type") or "")
        lo, hi = LIMITS["teaching_card_minutes"] if k in TEACHING_CARD_TYPES else LIMITS["quiz_card_minutes"] if k == "quiz" else LIMITS["note_card_minutes"]
        return int(lo * 60), int(hi * 60)
    lo, hi = span(edited)
    edited["duration_sec"] = max(lo, min(hi, int(edited.get("duration_sec") or lo)))
    others = [c for c in cards if c is not edited and str(c.get("card_type")) in set(TEACHING_CARD_TYPES) | {"quiz"}]
    for _ in range(2000):
        gap = target_minutes * 60 - sum(int(c.get("duration_sec") or 0) for c in cards)
        if abs(gap) < 3:
            break
        up = gap > 0
        room = [(span(c)[1] - c["duration_sec"]) if up else (c["duration_sec"] - span(c)[0]) for c in others]
        if not others or max(room) < 6:
            break
        best = max(range(len(others)), key=lambda i: room[i])
        others[best]["duration_sec"] += 6 if up else -6
    outline_data["total_duration_sec"] = sum(int(c.get("duration_sec") or 0) for c in cards)


def _apply_local_card_directive(target_card: dict, user_prompt: str):
    """
    Applies user chat directives (e.g., 'remove pacing', 'change title to X',
    'make it 5 mins') directly to target card fields without wiping out existing text.
    """
    prompt_lower = user_prompt.lower().strip()
    
    # 1. Removal directives (e.g. "remove pacing", "remove setup", "remove X")
    if prompt_lower.startswith("remove ") or prompt_lower.startswith("delete "):
        target_term = prompt_lower.replace("remove ", "").replace("delete ", "").strip()
        # Look in teaching_shape_note
        curr_note = target_card.get("teaching_shape_note", "")
        if target_term in curr_note.lower():
            # Remove the phrase cleanly
            import re
            cleaned = re.sub(re.escape(target_term), "", curr_note, flags=re.IGNORECASE)
            cleaned = cleaned.replace(" ,", ",").replace(",,", ",").strip(" ,.-")
            target_card["teaching_shape_note"] = cleaned
        # Look in agenda_items
        new_agendas = []
        for ag in target_card.get("agenda_items", []):
            if target_term not in ag.lower():
                new_agendas.append(ag)
            else:
                cleaned_ag = re.sub(re.escape(target_term), "", ag, flags=re.IGNORECASE).strip(" ,.-")
                if cleaned_ag:
                    new_agendas.append(cleaned_ag)
        target_card["agenda_items"] = new_agendas
        return

    # 2. Timing directives (e.g. "make it 5 minutes", "duration 6m")
    import re
    time_match = re.search(r"(\d+(\.\d+)?)\s*(min|m|minute)", prompt_lower)
    if time_match:
        mins = float(time_match.group(1))
        target_card["duration_sec"] = int(mins * 60)

    # 3. Title rename directives (e.g. "title: X" or "rename to X")
    if "rename to " in prompt_lower or "title: " in prompt_lower:
        new_title = re.split(r"rename to |title:\s*", user_prompt, flags=re.IGNORECASE)[-1].strip()
        if new_title:
            target_card["title"] = new_title
        return

    # 4. Content addition / refinement: append or update concept specifically
    curr_concept = target_card.get("teaching_shape_note", "")
    if curr_concept:
        target_card["teaching_shape_note"] = f"{curr_concept} | {user_prompt}"
    else:
        target_card["teaching_shape_note"] = user_prompt

# ==============================================================================
# 4. SECTION-BY-SECTION SCRIPT GENERATION & ATTRIBUTES ENRICHMENT
# ==============================================================================

def _card_flow(earlier, start_card: int, flat_outline) -> dict:
    """Where this card sits in the class: the words the previous card ended with and the card that comes next, so it joins them
    instead of standing alone. `earlier` are the cards already written, `flat_outline` is [(section, card spec)] for the whole class."""
    from agents.rule_checker import spoken_tail
    previous = next((c for c in reversed(earlier) if c.card_index == start_card - 1), None)
    prev_spec = flat_outline[start_card - 2][1] if start_card >= 2 and start_card - 2 < len(flat_outline) else None
    next_spec = flat_outline[start_card][1] if start_card < len(flat_outline) else None
    if previous is not None:
        prev = {"title": previous.title, "type": previous.pedagogical_phase, "ended_with": spoken_tail(previous.content_script, 60)}
    elif prev_spec is not None:                      # the card before it is not written yet: say what it is about
        prev = {"title": prev_spec.get("title"), "type": prev_spec.get("card_type"), "ended_with": None}
    else:
        prev = None
    here = flat_outline[start_card - 1][1] if 0 <= start_card - 1 < len(flat_outline) else {}
    prev_level = None                                      # the level of the nearest earlier card that has one
    for _sec, spec in reversed(flat_outline[:max(0, start_card - 1)]):
        if spec.get("level"):
            prev_level = spec["level"]
            break
    return {
        "position": f"{start_card} of {len(flat_outline)}", "level": here.get("level"), "previous_level": prev_level, "previous": prev,
        "next": ({"title": next_spec.get("title"), "type": next_spec.get("card_type"), "covers": "; ".join((next_spec.get("agenda_items") or [])[:2])}
                 if next_spec else None),
    }


@app.post("/api/scripts/{script_id}/generate-section")
def generate_section(
    script_id: int,
    start_card: int = Query(1, ge=1),
    end_card: int = Query(4, ge=1),
    section_id: Optional[str] = Query(None),
    user_prompt: Optional[str] = Query(None),
    _actor: User = Depends(script_guard(True)),
    db: Session = Depends(get_db)
):
    """
    Drafts cards in section batches using Claude Sonnet (Writer Agent).
    Automatically resolves [[VISUAL]] placeholders into live Mermaid diagrams
    and interactive HTML animations using Attributes Generator.
    """
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    if not script.outline_json:
        raise HTTPException(status_code=400, detail="Must generate outline first.")

    outline_raw = json.loads(script.outline_json)
    if isinstance(outline_raw, dict) and outline_raw.get("sections") and any("has_code" not in c for _s, c in _outline_card_list(outline_raw)):
        from agents.outliner import finalize_plan          # an outline made before the plan existed gets its levels and code plan now
        outline_raw = finalize_plan(outline_raw, script.content_mix_beginner, script.code_ratio, reorder=not script.cards)
        script.outline_json = json.dumps(outline_raw)
        db.commit()
    try:
        memory = json.loads(script.previous_script_context) if script.previous_script_context else {}
    except:
        memory = {}

    from agents.shared_context import build_block_a_context
    cls = script.class_entity
    effective_title = script.custom_class_title or (cls.title if cls else "Lecture Script")
    try:
        agendas = json.loads(script.custom_agendas) if script.custom_agendas else (json.loads(cls.agendas) if cls and cls.agendas else [])
    except:
        agendas = [cls.agendas] if cls else []

    block_a = build_block_a_context(
        db=db,
        class_id=script.class_id,
        duration_minutes=script.duration_minutes,
        content_mix_beginner=script.content_mix_beginner,
        code_ratio=script.code_ratio,
        custom_title=effective_title,
        custom_agendas=agendas,
        learning_outcomes=_effective_outcomes(script),
        api_docs=script.api_docs,
        reference_urls=script.reference_urls,
        additional_resources=_resources_with_feedback(script),
        previous_script_context=script.previous_script_context
    )

    from agents.writer import write_section, write_card, draft_card_batch, live_llm_available
    from agents.attributes_generator import generate_visuals_batch

    # Determine running example
    running_example = (
        (outline_raw.get("running_example") if isinstance(outline_raw, dict) else None)
        or memory.get("anchor_analogy")
        or "Real-time Distributed Booking Engine"
    )

    # If outline is structured as sections
    if isinstance(outline_raw, dict) and outline_raw.get("sections"):
        target_sec_id = section_id or f"sec_{max(1, (start_card - 1) // 4 + 1)}"
        flat_outline = [(sec, c) for sec in outline_raw["sections"] for c in sec.get("cards", [])]
        if start_card == end_card and live_llm_available() and 1 <= start_card <= len(flat_outline):
            # One card at a time, with what was already written and the terms already defined, so the card continues
            # the script and the prompt stays small.
            import re as _re
            sec, spec = flat_outline[start_card - 1]
            focus = " ".join([spec.get("title") or "", spec.get("teaching_shape_note") or ""] + list(spec.get("agenda_items") or []) + list(spec.get("outcomes") or []))
            block_a = build_block_a_context(
                db=db, class_id=script.class_id, duration_minutes=script.duration_minutes,
                content_mix_beginner=script.content_mix_beginner, code_ratio=script.code_ratio, custom_title=effective_title,
                custom_agendas=agendas, learning_outcomes=_effective_outcomes(script), api_docs=script.api_docs,
                reference_urls=script.reference_urls, additional_resources=_resources_with_feedback(script), kit_focus=focus,
                previous_script_context=script.previous_script_context)
            planned_visuals = [v for v in (outline_raw.get("visual_cards") or []) if v.get("parent_card_id") == spec.get("card_id")]
            spec = {**spec, "visuals": [{k: v.get(k) for k in ("visual_id", "kind", "title", "prompt")} for v in planned_visuals]}
            if spec.get("card_type") == "agenda":
                spec["agenda_timing"] = _agenda_timing_for(script, outline_raw)        # the agenda card reads these items and minutes aloud
            earlier = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index < start_card) \
                .order_by(ScriptCard.card_index).all()
            from agents.rule_checker import spoken_tail, spoken_text
            digest = [{"n": c.card_index, "title": c.title, "type": c.pedagogical_phase,
                       "gist": spoken_text(c.content_script)[:220], "ended_with": spoken_tail(c.content_script, 30)} for c in earlier[-8:]]
            flow = _card_flow(earlier, start_card, flat_outline)
            terms = list(dict.fromkeys(t.strip() for c in earlier for t in _re.findall(r"\*\*([^*\n]{2,60})\*\*", c.content_script or "")))[:40]
            # Approved pages (the SME's links and the module's approved sources) are fetched now, and the passages that match
            # this card go to the Writer, so its claims come from the documentation and not from memory.
            from agents.source_fetcher import grounding_excerpts
            from agents.sources import provided_urls
            approved_urls = provided_urls(script.reference_urls, script.api_docs, script.additional_resources,
                                          cls.module.approved_sources if cls and cls.module else None)
            excerpts = grounding_excerpts(approved_urls, focus)

            def _write(block):
                return write_card(
                    card_spec=spec, section_goal=sec.get("goal", ""),
                    outline_overview=[c.get("title") for _, c in flat_outline],
                    written_so_far=digest, term_register=terms, running_example=running_example, block_a_context=block,
                    reference_excerpts=excerpts, flow=flow)
            sec_out = _write(block_a)
            for written in sec_out.get("cards", []):
                written["card_id"] = spec.get("card_id")
            # The visual requests come from the approved outline, not from the writer: each visual card keeps its own
            # prompt, the agenda item or outcome it serves, and its place in the scope.
            sec_out["visual_requests"] = [_visual_request(v, spec.get("card_id")) for v in planned_visuals]
        else:
            sec_out = write_section(
                section_id=target_sec_id,
                approved_outline=outline_raw,
                approved_sections=[],
                term_register=[],
                block_a_context=block_a,
                class_title=effective_title,
                memory_context=memory,
                content_mix_beginner=script.content_mix_beginner,
                code_ratio=script.code_ratio,
                script_id=script.id
            )
        all_drafted_cards = sec_out.get("cards", [])
        visual_requests = sec_out.get("visual_requests", [])

        # Process and generate visual assets for placeholders
        visual_assets = generate_visuals_batch(
            visual_requests=visual_requests,
            cards=all_drafted_cards,
            running_example=running_example,
            block_a_context=block_a,
            script_id=script.id
        )
        vis_map = {v.get("id"): v for v in visual_assets}

        # If requesting a single card or specific range within a section
        if start_card == end_card:
            # Map start_card index to the corresponding card within this section
            sec_card_idx = (start_card - 1) % max(1, len(all_drafted_cards))
            if 0 <= sec_card_idx < len(all_drafted_cards):
                drafted_cards = [all_drafted_cards[sec_card_idx]]
            else:
                drafted_cards = all_drafted_cards[:1]
        else:
            drafted_cards = all_drafted_cards

        # Adapt cards to DB model format
        persisted_cards = []
        code_jobs = []
        for idx, c in enumerate(drafted_cards, start=start_card):
            from agents.rule_checker import normalise_typography
            from agents.rule_checker import clean_spoken
            body = clean_spoken(c.get("body_markdown") or c.get("content_script") or "")
            if idx == 1 and str(c.get("card_type") or "").lower() == "instructor_note":
                from agents.shared_context import INTRO_NOTE_BODY, INTRO_NOTE_CUE
                body, c["instructor_note"], c["title"] = INTRO_NOTE_BODY, INTRO_NOTE_CUE, "Instructor Introduction"      # card 1 is always the same short note
            elif idx <= 3 and str(c.get("card_type") or "").lower() == "welcome" and _program_opening(script):
                from agents.shared_context import ensure_opening_welcome
                body = ensure_opening_welcome(body, _opening_welcome(script))         # a track's first welcome card starts with that track's spoken welcome
            if str(c.get("card_type") or "").lower() == "agenda" and isinstance(outline_raw, dict):
                from agents.timeline import ensure_agenda_block
                body = ensure_agenda_block(body, _agenda_timing_for(script, outline_raw))      # after the welcome: every agenda item, with its time
            c["title"] = normalise_typography(c.get("title") or "")
            # Link visual asset if present
            card_vis = None
            for req in visual_requests:
                if req.get("card_id") == c.get("card_id") and req.get("id") in vis_map:
                    card_vis = vis_map[req.get("id")]
                    break

            card_obj = db.query(ScriptCard).filter(
                ScriptCard.script_id == script_id,
                ScriptCard.card_index == idx
            ).first()

            dur = round(c.get("duration_sec", 300) / 60.0, 1) if c.get("duration_sec") else c.get("duration_mins", 5.0)

            from agents.source_fetcher import resolve_sources
            sources_val = json.dumps(resolve_sources(c.get("sources"), [script.reference_urls, script.api_docs, script.additional_resources,
                                                                        cls.module.approved_sources if cls and cls.module else None]))
            stage_note = c.get("instructor_note") or c.get("stage_directions") or c.get("teaching_shape_note")
            from agents.quiz_format import clean_quiz
            quiz_val = json.dumps(clean_quiz(c.get("quiz_json"))) if isinstance(c.get("quiz_json"), dict) else (c.get("quiz_json") if isinstance(c.get("quiz_json"), str) else None)

            if card_obj:
                card_obj.title = c.get("title", f"Card {idx}")
                card_obj.duration_mins = dur
                card_obj.pedagogical_phase = c.get("card_type", "Concept").title()
                card_obj.stage_directions = stage_note
                card_obj.content_script = body
                card_obj.code_snippet = c.get("code_snippet")
                card_obj.code_language = c.get("code_language")
                card_obj.quiz_json = quiz_val
                card_obj.visual_asset_json = json.dumps(card_vis) if card_vis else None
                card_obj.sources_json = sources_val
                card_obj.is_approved = False
            else:
                card_obj = ScriptCard(
                    script_id=script_id,
                    card_index=idx,
                    title=c.get("title", f"Card {idx}"),
                    duration_mins=dur,
                    target_words=__import__("agents.shared_context", fromlist=["word_budget"]).word_budget(dur)[0],
                    pedagogical_phase=c.get("card_type", "Concept").title(),
                    stage_directions=stage_note,
                    content_script=body,
                    code_snippet=c.get("code_snippet"),
                    code_language=c.get("code_language"),
                    quiz_json=quiz_val,
                    visual_asset_json=json.dumps(card_vis) if card_vis else None,
                    sources_json=sources_val,
                    is_approved=False
                )
                db.add(card_obj)
            persisted_cards.append(card_obj)
            if (c.get("code_snippet") or "").strip():
                code_jobs.append((card_obj, (c.get("code_expected_error") or None)))

    else:
        # Fallback to batch drafter if outline is flat
        flat_cards = outline_raw if isinstance(outline_raw, list) else outline_raw.get("cards", [])
        drafted_cards = draft_card_batch(
            class_title=effective_title,
            outline_cards=flat_cards,
            batch_start_idx=start_card,
            batch_end_idx=min(end_card, len(flat_cards)),
            memory_context=memory,
            code_ratio=script.code_ratio,
            content_mix_beginner=script.content_mix_beginner,
            block_a_context=block_a
        )
        persisted_cards = []
        for c in drafted_cards:
            card_obj = db.query(ScriptCard).filter(
                ScriptCard.script_id == script_id,
                ScriptCard.card_index == c.get("card_index")
            ).first()
            if card_obj:
                card_obj.title = c.get("title")
                card_obj.duration_mins = c.get("duration_mins")
                card_obj.content_script = c.get("content_script")
                card_obj.code_snippet = c.get("code_snippet")
                card_obj.is_approved = False
            else:
                card_obj = ScriptCard(
                    script_id=script_id,
                    card_index=c.get("card_index"),
                    title=c.get("title"),
                    duration_mins=c.get("duration_mins"),
                    target_words=__import__("agents.shared_context", fromlist=["word_budget"]).word_budget(c.get("duration_mins") or 5.0)[0],
                    pedagogical_phase=c.get("pedagogical_phase", "Concept"),
                    content_script=c.get("content_script"),
                    code_snippet=c.get("code_snippet"),
                    is_approved=False
                )
                db.add(card_obj)
            persisted_cards.append(card_obj)

    db.commit()

    # The code on each new card is run, and a failure gets one repair attempt, so a card never ships code that does not work.
    for card_obj, expected in (code_jobs if "code_jobs" in locals() else []):
        _fix_and_run_card_code(card_obj, expected)
    db.commit()

    # Is the whole script written? The outline says how many cards there are; the database says how many have text.
    total_cards = 0
    if isinstance(outline_raw, dict) and outline_raw.get("sections"):
        total_cards = sum(len(sec.get("cards", [])) for sec in outline_raw["sections"])
    elif isinstance(outline_raw, list):
        total_cards = len(outline_raw)
    written_cards = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.content_script != "").count()
    if total_cards and written_cards >= total_cards:
        _make_total_exact(script_id, db)               # the whole script is written: its cards fill the class time exactly
    return {
        "script_id": script_id,
        "batch_range": f"{start_card}-{end_card}",
        "cards_drafted_count": len(persisted_cards),
        "total_cards": total_cards,
        "cards_written": written_cards,
        "script_complete": bool(total_cards) and written_cards >= total_cards,
        "status": "DRAFTED_AND_ATTACHED"
    }



def _make_total_exact(script_id: int, db: Session) -> bool:
    """Card minutes are saved to a tenth, so they can end up a few tenths away from the class length (119.5 of 120). When the gap is small, hand it out in
    0.1 minute steps to the cards with the most room, so a 120 minute class is covered completely. Returns True when something changed."""
    from agents.moderator import rebalance_durations
    script = db.query(Script).filter(Script.id == script_id).first()
    rows = db.query(ScriptCard).filter(ScriptCard.script_id == script_id).order_by(ScriptCard.card_index).all()
    if not script or not rows:
        return False
    gap = round(float(script.duration_minutes) - sum(float(r.duration_mins or 0) for r in rows), 1)
    if abs(gap) < 0.05 or abs(gap) > 3:
        return False
    work = [{"pedagogical_phase": r.pedagogical_phase, "duration_mins": float(r.duration_mins or 0)} for r in rows]
    step = 0.1 if gap > 0 else -0.1
    from agents.moderator import _kind
    from agents.shared_context import LIMITS, TEACHING_CARD_TYPES

    def room(c, up):
        k = _kind(c)
        lo, hi = (LIMITS["teaching_card_minutes"] if k in TEACHING_CARD_TYPES else LIMITS["quiz_card_minutes"] if k == "quiz" else LIMITS["note_card_minutes"])
        return (hi - c["duration_mins"]) if up else (c["duration_mins"] - lo)
    for _ in range(100):
        left = round(float(script.duration_minutes) - sum(c["duration_mins"] for c in work), 1)
        if abs(left) < 0.05:
            break
        best = max(range(len(work)), key=lambda i: room(work[i], left > 0))
        if room(work[best], left > 0) < 0.1 - 1e-9:
            break
        work[best]["duration_mins"] = round(work[best]["duration_mins"] + (0.1 if left > 0 else -0.1), 1)
    changed = False
    for r, w in zip(rows, work):
        if abs(float(r.duration_mins or 0) - w["duration_mins"]) > 1e-9:
            r.duration_mins, changed = w["duration_mins"], True
    if changed:
        db.commit()
    return changed


# ---- running the code a card shows (rule A3) ----------------------------------------------------------------------

def _run_card_code(card: ScriptCard, expected_error: Optional[str] = None) -> dict:
    """Run the card's code and save the result with the card. A snippet that is meant to fail keeps its expected error."""
    from agents.code_runner import run_code
    if expected_error is None and card.code_run_json:
        try:
            expected_error = (json.loads(card.code_run_json) or {}).get("expected_error")
        except ValueError:
            pass
    result = run_code(card.code_snippet, card.code_language, expected_error)
    card.code_run_json = json.dumps(result)
    return result


def _fix_and_run_card_code(card: ScriptCard, expected_error: Optional[str] = None, allow_repair: bool = True) -> dict:
    """Run the code; when it fails, ask Claude once for a fix and run that. A fix that works replaces the code (and the
    same code inside the card text); one that does not is thrown away and the original failure is what gets reported."""
    result = _run_card_code(card, expected_error)
    if not (allow_repair and result["status"] == "failed" and llm_configured() and os.getenv("EXECUTION_MODE", "hybrid") != "mock"):
        return result
    from agents.writer import repair_code
    old_code, old_body, old_run = card.code_snippet, card.content_script, card.code_run_json
    try:
        fixed = repair_code(card.code_language, old_code, result.get("stderr") or result.get("reason") or "", old_body or "", result.get("expected_error"))
    except LLMUnavailable:
        return result
    if not fixed or fixed.strip() == (old_code or "").strip():
        return result
    card.code_snippet = fixed
    if old_code and old_code in (old_body or ""):
        card.content_script = old_body.replace(old_code, fixed)
    second = _run_card_code(card, result.get("expected_error"))
    if second["status"] in ("passed", "skipped"):
        second["repaired"] = True
        card.code_run_json = json.dumps(second)
        return second
    card.code_snippet, card.content_script, card.code_run_json = old_code, old_body, old_run      # the fix did not work either
    return result


MAX_TRY_CODE_CHARS = 20000


@app.post("/api/scripts/{script_id}/cards/{card_index}/run-code")
def run_card_code_now(script_id: int, card_index: int, repair: bool = False, payload: CodeTryRequest = Body(default=CodeTryRequest()),
                      _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Run this card's code. With `code` in the body, that text (what is in the editor, not saved yet) is run and nothing is changed: the
    answer is what happened. Without it, the saved code is run again, and repair=true also asks Claude to fix a failure."""
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    card = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index == card_index).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if payload.code is not None:
        from agents.code_runner import run_code
        if len(payload.code) > MAX_TRY_CODE_CHARS:
            raise HTTPException(status_code=400, detail=f"That is too much code to run here (the limit is {MAX_TRY_CODE_CHARS} characters).")
        if not payload.code.strip():
            raise HTTPException(status_code=400, detail="There is no code to run.")
        expected = None
        try:
            expected = (json.loads(card.code_run_json) or {}).get("expected_error") if card.code_run_json else None
        except ValueError:
            pass
        return {"card_index": card_index, "code_snippet": payload.code, "code_run": run_code(payload.code, payload.language or card.code_language, expected), "saved": False}
    if not (card.code_snippet or "").strip():
        raise HTTPException(status_code=400, detail="This card has no code.")
    result = _fix_and_run_card_code(card, allow_repair=repair)
    db.commit()
    return {"card_index": card_index, "code_snippet": card.code_snippet, "code_run": result, "saved": True}

# ==============================================================================
# VISUAL CARDS: edit, regenerate and approve one visual at a time
# ==============================================================================

VISUAL_KINDS = ("diagram", "animation", "image")


def _visual_request(v: dict, card_id: Optional[str] = None, extra_instruction: Optional[str] = None, skip_web: bool = False) -> dict:
    """A visual card from the outline, as the request the generators take."""
    from agents.outliner import VISUAL_KIND_TO_TYPE
    intent = v.get("prompt") or v.get("title")
    return {
        "id": v["visual_id"], "type": VISUAL_KIND_TO_TYPE.get(v.get("kind"), "mermaid"), "intent": intent,
        "extra": (extra_instruction or "").strip() or None,         # an extra instruction for this one making: in the prompt, never in the caption
        "nodes": v.get("elements") or [], "card_id": card_id or v.get("parent_card_id"), "title": v.get("title"),
        "serves": "; ".join((v.get("serves_agenda_items") or []) + (v.get("serves_outcomes") or [])),
        "rejected_images": v.get("rejected_images") or [], "skip_web": skip_web,
    }


def _visual_lookup(script: Script, db: Session, visual_id: str):
    """(outline, spec, card_index, card row or None) for one visual card, or a 404."""
    try:
        outline = json.loads(script.outline_json) if script.outline_json else {}
    except ValueError:
        outline = {}
    spec = next((v for v in (outline.get("visual_cards") or []) if v.get("visual_id") == visual_id), None) if isinstance(outline, dict) else None
    if spec is None:
        raise HTTPException(status_code=404, detail=f"Visual card {visual_id} not found in this outline.")
    index = 0
    card_index = None
    for sec in outline.get("sections", []):
        for c in sec.get("cards", []):
            index += 1
            if c.get("card_id") == spec.get("parent_card_id"):
                card_index = index
    row = db.query(ScriptCard).filter(ScriptCard.script_id == script.id, ScriptCard.card_index == card_index).first() if card_index else None
    return outline, spec, card_index, row


def _visual_summary(spec: dict, card_index: Optional[int], row: Optional[ScriptCard]) -> dict:
    asset = None
    if row is not None and row.visual_asset_json:
        try:
            asset = json.loads(row.visual_asset_json)
        except ValueError:
            asset = None
    mine = bool(asset) and asset.get("id") == spec.get("visual_id")
    return {
        **{k: spec.get(k) for k in ("visual_id", "parent_card_id", "kind", "title", "prompt", "elements", "serves_agenda_items", "serves_outcomes")},
        "card_index": card_index,
        "status": ("approved" if mine and asset.get("approved") else ("stale" if mine and asset.get("stale") else ("generated" if mine else "planned"))),
        "source": asset.get("source") if mine else None, "generated": asset.get("generated") if mine else None,
        "note": asset.get("note") if mine else None,
    }


@app.get("/api/scripts/{script_id}/visuals")
def list_visuals(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    script = db.query(Script).filter(Script.id == script_id).first()
    try:
        outline = json.loads(script.outline_json) if script.outline_json else {}
    except ValueError:
        outline = {}
    out = []
    for v in (outline.get("visual_cards") or []) if isinstance(outline, dict) else []:
        _, spec, card_index, row = _visual_lookup(script, db, v["visual_id"])
        out.append(_visual_summary(spec, card_index, row))
    return out


class VisualEditRequest(BaseModel):
    title: Optional[str] = None
    kind: Optional[str] = None
    prompt: Optional[str] = None
    elements: Optional[List[str]] = None
    serves_agenda_items: Optional[List[str]] = None


class VisualRegenerateRequest(BaseModel):
    instruction: Optional[str] = None      # an extra instruction for this one regeneration, for example "show it left to right"
    skip_web: bool = False                 # an image visual: do not look on the web, go straight to Gemini or the placeholder


@app.put("/api/scripts/{script_id}/visuals/{visual_id}")
def edit_visual(script_id: int, visual_id: str, payload: VisualEditRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Change a visual card's prompt, kind, title or elements. A picture already made from the old prompt is marked stale."""
    script = db.query(Script).filter(Script.id == script_id).first()
    outline, spec, card_index, row = _visual_lookup(script, db, visual_id)
    if payload.kind is not None:
        from agents.image_gen import images_enabled
        allowed = VISUAL_KINDS if images_enabled() else tuple(k for k in VISUAL_KINDS if k != "image")
        if payload.kind not in allowed:
            raise HTTPException(status_code=400, detail=f"kind must be one of: {', '.join(allowed)}."
                                + ("" if images_enabled() else " Pictures are switched off (IMAGES_ENABLED=0)."))
        spec["kind"] = payload.kind
    if payload.title is not None:
        spec["title"] = payload.title.strip()[:200]
    if payload.prompt is not None:
        if not payload.prompt.strip():
            raise HTTPException(status_code=400, detail="The prompt cannot be empty.")
        spec["prompt"] = payload.prompt.strip()[:1200]
    if payload.elements is not None:
        spec["elements"] = [e.strip()[:60] for e in payload.elements if e.strip()][:7]       # rule R5: at most 7 elements
    if payload.serves_agenda_items is not None:
        spec["serves_agenda_items"] = payload.serves_agenda_items
    script.outline_json = json.dumps(outline)
    if row is not None and row.visual_asset_json:
        asset = json.loads(row.visual_asset_json)
        if asset.get("id") == visual_id:
            asset["stale"], asset["approved"] = True, False
            row.visual_asset_json = json.dumps(asset)
    _reopen_if_approved(db, script_id, _actor)
    db.commit()
    return _visual_summary(spec, card_index, row)


def _regenerate_visual_asset(script, db: Session, visual_id: str, instruction: Optional[str] = None, skip_web: bool = False):
    """Make one visual again from its prompt and the text of its card, and save it (not approved). Returns (asset, outline_spec, card_index, row).
    Used by the Regenerate button and by the review loop when a visual does not fit its card."""
    from agents.attributes_generator import generate_visual
    outline, spec, card_index, row = _visual_lookup(script, db, visual_id)
    if row is None:
        raise HTTPException(status_code=409, detail="Write the card this visual sits on first; the visual is made from its text.")
    running_example = (outline.get("running_example") if isinstance(outline, dict) else None) or ""
    # Asking again means the SME did not like the last web picture: remember it so the search never offers it again
    try:
        previous = json.loads(row.visual_asset_json) if row.visual_asset_json else {}
    except ValueError:
        previous = {}
    if previous.get("id") == visual_id and previous.get("source") == "web_search" and previous.get("source_url"):
        spec["rejected_images"] = ((spec.get("rejected_images") or []) + [previous["source_url"]])[-20:]
        script.outline_json = json.dumps(outline)
    asset = generate_visual(_visual_request(spec, extra_instruction=instruction, skip_web=skip_web), row.content_script or "", running_example,
                            script_id=script.id)
    asset["id"], asset["approved"], asset["stale"] = visual_id, False, False
    row.visual_asset_json = json.dumps(asset)
    row.is_approved = False
    return asset, spec, card_index, row


@app.post("/api/scripts/{script_id}/visuals/{visual_id}/regenerate")
def regenerate_visual(script_id: int, visual_id: str, payload: VisualRegenerateRequest = Body(default=VisualRegenerateRequest()),
                      _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Make this visual again from its current prompt: a flow by Gemini, a picture by Gemini, an animation by Claude."""
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    script = db.query(Script).filter(Script.id == script_id).first()
    asset, spec, card_index, row = _regenerate_visual_asset(script, db, visual_id, payload.instruction, payload.skip_web)
    _reopen_if_approved(db, script_id, _actor)
    db.commit()
    summary = _visual_summary(spec, card_index, row)
    if asset.get("source") == "placeholder":
        summary["warning"] = asset.get("note") or "A placeholder was used because the generator could not answer."
    return summary


@app.post("/api/scripts/{script_id}/visuals/{visual_id}/approve")
def approve_visual(script_id: int, visual_id: str, approved: bool = True, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Mark a generated visual as approved (or take the approval back with approved=false)."""
    script = db.query(Script).filter(Script.id == script_id).first()
    outline, spec, card_index, row = _visual_lookup(script, db, visual_id)
    asset = json.loads(row.visual_asset_json) if row is not None and row.visual_asset_json else None
    if not asset or asset.get("id") != visual_id:
        raise HTTPException(status_code=409, detail="This visual has not been generated yet.")
    if approved and asset.get("stale"):
        raise HTTPException(status_code=409, detail="The prompt changed after this picture was made. Regenerate it before approving.")
    if approved and asset.get("type") == "animation":
        from agents.safe_html import animation_is_complete
        if not animation_is_complete(asset.get("content")):
            raise HTTPException(status_code=409, detail="This animation was cut off and cannot play. Regenerate it before approving.")
    if approved and asset.get("source") == "placeholder":
        raise HTTPException(status_code=409, detail="This is a placeholder, not a real visual. Regenerate it once the model is available.")
    asset["approved"] = bool(approved)
    row.visual_asset_json = json.dumps(asset)
    db.commit()
    return _visual_summary(spec, card_index, row)

# ==============================================================================
# 5. MULTI-AGENT REVIEW AUDIT & MODERATOR REPAIR (PRD FR-17)
# ==============================================================================

def _scope_digest(script) -> str:
    """The short scope text reviewers and the Moderator need: what the script must cover, what it may use and what it must not teach."""
    title, agendas, outcomes = _script_scope(script)
    cls = script.class_entity
    from agents.reviewer import TRACK_LEVEL_NOTE, track_level
    level = track_level(_track_of(script), _program_of(script))
    lines = [f"SCOPE. The script must stay inside this.", f"Program: {_program_of(script) or 'unknown'}", f"Track: {_track_of(script) or 'unknown'}"
             + (f" (level: {level})" if level else ""),
             TRACK_LEVEL_NOTE[level] if level else "TRACK LEVEL: not stated in the track's name; judge the class by its content mix below.",
             f"Module: {cls.module.name if cls is not None and cls.module is not None else 'unknown'}",
             f"Class: {title} ({script.duration_minutes} minutes)",
             f"Content mix: {script.content_mix_beginner}% beginner, {100 - int(script.content_mix_beginner or 0)}% advanced, climbing from beginner to advanced. "
             f"About {script.code_ratio}% of the cards carry code."]
    lines.append("Agenda: " + ("; ".join(agendas) if agendas else "none provided; derive it from the class title and say what you assumed"))
    lines.append("Learning outcomes: " + (outcomes.strip() or "none provided; derive them from the title"))
    if script.additional_resources:
        lines.append("SME additional resources (use them; do not contradict them): " + script.additional_resources.strip()[:700])
    if cls is not None and cls.module is not None:
        later = sorted((c for c in cls.module.classes if (cls.sequence_number or 0) < (c.sequence_number or 0) < 1000),
                       key=lambda c: c.sequence_number)[:3]
        if later:
            lines.append("Upcoming classes (do not teach these): " + "; ".join(c.title for c in later))
    return "\n".join(lines)


def _review_cards_dicts(db: Session, script_id: int):
    """The stored cards as plain dicts, with quiz, sources and visual, for the checks and the reviewers. Each carries its planned level and
    whether it was planned to carry code, from the outline."""
    out = []
    plan = {}
    script_row = db.query(Script).filter(Script.id == script_id).first()
    try:
        outline = json.loads(script_row.outline_json) if script_row is not None and script_row.outline_json else None
    except ValueError:
        outline = None
    if isinstance(outline, dict):
        plan = {n: spec for n, (_sec, spec) in enumerate(_outline_card_list(outline), start=1)}
    for c in db.query(ScriptCard).filter(ScriptCard.script_id == script_id).order_by(ScriptCard.card_index).all():
        vis = json.loads(c.visual_asset_json) if c.visual_asset_json else None
        quiz = json.loads(c.quiz_json) if c.quiz_json else None
        out.append({
            "card_index": c.card_index, "title": c.title, "duration_mins": c.duration_mins,
            "pedagogical_phase": c.pedagogical_phase, "content_script": c.content_script,
            "code_snippet": c.code_snippet, "code_language": c.code_language,
            "code_run": json.loads(c.code_run_json) if c.code_run_json else None,
            "quiz": quiz if isinstance(quiz, dict) else None,
            "sources": json.loads(c.sources_json) if c.sources_json else [],
            "visual_type": vis.get("type") if isinstance(vis, dict) else None,
            "visual_content": vis.get("content") if isinstance(vis, dict) else None,
            "visual_caption": " ".join(str(vis.get(k) or "") for k in ("caption", "alt_text")) if isinstance(vis, dict) else None,
            "stage_directions": c.stage_directions,
            "level": plan.get(c.card_index, {}).get("level"), "planned_code": plan.get(c.card_index, {}).get("has_code"),
            "covers": plan.get(c.card_index, {}).get("agenda_items") or [],
        })
    return out


MAX_REPAIR_PASSES = 2      # PRD FR-17: the AI fixes and re-checks at most 2 times; the loop stops sooner when no blockers are left or it stops improving


@app.get("/api/scripts/{script_id}/review/progress")
def review_progress(script_id: int, _actor: User = Depends(script_guard(False))):
    """What the running review is doing right now (the UI polls this while it waits)."""
    from agents.progress import current
    return current(script_id)


VISUAL_FIT_RULES = ("V3", "V4", "V5", "V6", "R5")      # a visual that is cut off, not about its card, says the wrong thing, or has too many nodes: fixed by making it again
MAX_VISUAL_REPAIRS = 8                      # per repair pass, so one pass stays cheap; the rest are left for the next pass or the SME


def _repair_visuals_and_quizzes(script, db: Session, reviews: list, notes: list, all_severities: bool = False) -> set:
    """What the Moderator cannot fix by editing text. A visual flagged as cut off, off-topic or wrong is made again from its card's text; a quiz on screen that is
    not the question the card reads out is rewritten to match it. Returns the card numbers changed, so the next round reads them again."""
    from agents.fit_checker import align_quiz
    from agents.progress import report
    findings = [f for r in reviews for f in (r.get("findings") or []) if f.get("severity") in (("blocker", "major", "minor") if all_severities else ("blocker", "major"))]

    def cards_with(rules):
        return sorted({int(f["card_id"]) for f in findings if f.get("rule") in rules and str(f.get("card_id")).isdigit()})
    visual_cards, quiz_cards = cards_with(VISUAL_FIT_RULES), cards_with(("Q1",))
    if not visual_cards and not quiz_cards:
        return set()
    rows = {r.card_index: r for r in db.query(ScriptCard).filter(ScriptCard.script_id == script.id, ScriptCard.card_index.in_(visual_cards + quiz_cards)).all()}
    changed: set = set()
    for n, idx in enumerate(visual_cards[:MAX_VISUAL_REPAIRS]):
        row = rows.get(idx)
        try:
            visual_id = (json.loads(row.visual_asset_json) or {}).get("id") if row is not None and row.visual_asset_json else None
            if not visual_id:
                continue
            report(script.id, f"Making the visual on card {idx} again so it fits the card ({n + 1} of {min(len(visual_cards), MAX_VISUAL_REPAIRS)})")
            asset, _spec, _idx, _row = _regenerate_visual_asset(
                script, db, visual_id, "Show only what this card teaches, with the card's own names and steps in the same order. Nothing from outside the script."
                + (" Use at most 7 nodes: merge or drop the least important ones." if any(f.get("rule") == "R5" and str(f.get("card_id")) == str(idx) for f in findings) else ""), skip_web=True)
            db.commit()
            changed.add(idx)
            from agents.progress import mark_card
            mark_card(script.id, idx, "applied")
            notes.append(f"Card {idx}: made the {asset.get('type')} again so it fits the card" + (" (a simple step player was used because the AI's animation was cut off)" if asset.get("source") == "placeholder" else "") + ".")
        except Exception as e:
            db.rollback()
            notes.append(f"Card {idx}: the visual could not be made again ({str(e)[:90]}). It is left for the SME.")
    for idx in quiz_cards:
        row = rows.get(idx)
        if row is None or not row.quiz_json:
            continue
        report(script.id, f"Making the quiz on card {idx} the same as the question the card reads out")
        quiz = align_quiz({"content_script": row.content_script, "quiz": _quiz_of(row)}, script_id=script.id)
        if quiz:
            _save_quiz(row, quiz)
            db.commit()
            changed.add(idx)
            from agents.progress import mark_card
            mark_card(script.id, idx, "applied")
            notes.append(f"Card {idx}: the quiz on screen now matches the question the card reads out.")
        else:
            notes.append(f"Card {idx}: the quiz could not be matched to the card's question. It is left for the SME.")
    return changed


def _cards_hash(db: Session, script_id: int) -> str:
    """A fingerprint of what the review read (card text, code, quiz, visual), so fixes are only applied to the script the findings were about."""
    import hashlib
    h = hashlib.sha1()
    for c in db.query(ScriptCard).filter(ScriptCard.script_id == script_id).order_by(ScriptCard.card_index).all():
        try:
            q = json.loads(c.quiz_json) if c.quiz_json else {}
            q = {k: q.get(k) for k in ("question", "options", "correct_option_index")} if isinstance(q, dict) else {}
        except ValueError:
            q = {}
        try:
            v = json.loads(c.visual_asset_json) if c.visual_asset_json else {}
            v = {k: v.get(k) for k in ("type", "content")} if isinstance(v, dict) else {}
        except ValueError:
            v = {}
        h.update(json.dumps([c.card_index, c.title, c.content_script, c.code_snippet, q, v], sort_keys=True, default=str).encode())
    return h.hexdigest()


class ReviewApplyBody(BaseModel):
    cards: Optional[List[int]] = None       # apply the fixes for these cards only; None means every fix the review proposed


@app.post("/api/scripts/{script_id}/review", response_model=MultiAgentReviewResponse)
def execute_multi_agent_review(script_id: int, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Step 1 of the review: FIND. The rule checks (code), the reviewer lenses and the meaning check read the whole script and list what needs fixing.
    Nothing in the script is changed. The author reads the findings and then chooses to apply the fixes (see apply_review_fixes)."""
    return _run_review(script_id, _actor, db, "find")


@app.post("/api/scripts/{script_id}/review/apply", response_model=MultiAgentReviewResponse)
def apply_review_fixes(script_id: int, payload: ReviewApplyBody = Body(default=ReviewApplyBody()),
                       _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Step 2: APPLY, only when the author asks. The AI fixes what the last review found (all of it, or the cards chosen), then re-reads only the
    cards it changed, up to MAX_REPAIR_PASSES times. The review is stored again with what was done."""
    return _run_review(script_id, _actor, db, "apply", set(payload.cards) if payload.cards else None)


def _compare_findings(before: list, after: list) -> dict:
    """What happened to the problems an earlier review listed: how many are gone, how many are still there (same card and same rule or mostly the same words) and how
    many are new. Only blockers and majors count."""
    words = lambda t: set(re.findall(r"[a-z]{5,}", (t or "").lower()))
    keep = lambda fs: [f for f in fs if f.get("severity") in ("blocker", "major")]
    before, after = keep(before), keep(after)
    still = 0
    for f in after:
        if any(str(f.get("card_id")) == str(g.get("card_id")) and (f.get("rule") == g.get("rule") or len(words(f.get("problem")) & words(g.get("problem"))) >= 5) for g in before):
            still += 1
    matched = sum(1 for g in before if any(str(f.get("card_id")) == str(g.get("card_id")) and (f.get("rule") == g.get("rule") or len(words(f.get("problem")) & words(g.get("problem"))) >= 5) for f in after))
    return {"before": len(before), "fixed": len(before) - matched, "still_open": matched, "new": max(0, len(after) - still), "now": len(after)}


def _build_checklist(before: list, after: list, notes: list) -> list:
    """The fix checklist: every blocker and major from the earlier review marked Fixed or Still open, then the new ones, each with what was done to its card
    (read from the review's own notes). Fixed and still open are told apart by the same card and rule, or mostly the same words."""
    words = lambda t: set(re.findall(r"[a-z]{5,}", (t or "").lower()))
    keep = lambda fs: [f for f in fs if f.get("severity") in ("blocker", "major", "minor")]
    same = lambda f, g: str(f.get("card_id")) == str(g.get("card_id")) and (f.get("rule") == g.get("rule") or len(words(f.get("problem")) & words(g.get("problem"))) >= 5)
    before, after = keep(before), keep(after)
    actions: dict = {}
    for n in notes:
        for card, what in re.findall(r"Card (\d+): ([^.;]+)", n or ""):
            actions.setdefault(card, []).append(what.strip())
    out, seen = [], set()
    for g in before:
        key = (str(g.get("card_id")), g.get("rule"), (g.get("problem") or "")[:60])
        if key in seen:
            continue
        seen.add(key)
        out.append({"card_id": g.get("card_id"), "rule": g.get("rule"), "severity": g.get("severity"), "problem": (g.get("problem") or "")[:170],
                    "fix": (g.get("suggested_fix") or "")[:170], "status": "open" if any(same(f, g) for f in after) else "fixed",
                    "action": "; ".join(dict.fromkeys(actions.get(str(g.get("card_id")), [])))[:140] or None})
    for f in after:
        if not any(same(f, g) for g in before):
            out.append({"card_id": f.get("card_id"), "rule": f.get("rule"), "severity": f.get("severity"), "problem": (f.get("problem") or "")[:170],
                        "fix": (f.get("suggested_fix") or "")[:170], "status": "new", "action": None})
    return out


def _checklist_from_findings(findings: list) -> list:
    """Every problem a review found, blocker then major then minor, each once, as a to-do list the AI works through when the author asks for the fixes.
    A problem about the whole script has no card to rewrite, so it is marked as needing the author."""
    words = lambda t: set(re.findall(r"[a-z]{5,}", (t or "").lower()))
    rank = {"blocker": 0, "major": 1, "minor": 2}
    out: list = []
    for f in sorted((f for f in findings if f.get("severity") in rank), key=lambda f: (rank[f["severity"]], str(f.get("card_id")))):
        card = f.get("card_id")
        if any(str(g["card_id"]) == str(card) and (len(words(f.get("problem")) & words(g["problem"])) >= 5 or (f.get("rule") == g["rule"] and (f.get("problem") or "")[:50] == g["problem"][:50])) for g in out):
            continue
        out.append({"card_id": card, "rule": f.get("rule"), "severity": f["severity"], "problem": (f.get("problem") or "")[:170], "fix": (f.get("suggested_fix") or "")[:170],
                    "status": "todo" if str(card).isdigit() else "manual", "action": None})
    return out


def _run_review(script_id: int, _actor, db: Session, mode: str = "find", only_cards=None):
    """mode "find": round 1 only, nothing changed. mode "apply": start from the stored review and repair (all cards, or only_cards), then re-read what changed.
    mode "full": both in one go (find, then repair loop), used by tests. The loop stops as soon as no blockers are left, when two rounds in a row make
    no progress, or at MAX_REPAIR_PASSES. Whatever is still open is left for the SME."""
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    from agents.rule_checker import check_script, as_review, kit_required_items
    from agents.moderator import moderate_and_repair
    from agents.reviewer import merge_lens_reviews

    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not db.query(ScriptCard).filter(ScriptCard.script_id == script_id).first():
        raise HTTPException(status_code=400, detail="No cards drafted yet to review.")
    from agents.progress import set_checklist, mark_card
    set_checklist(script_id, None)
    fix_all = mode == "apply"              # when the author asks for the fixes, every problem is fixed: blockers, majors and minors
    stored_lens, stored_fit = [], []
    if mode == "apply":
        try:
            stored = json.loads(script.review_json) if script.review_json else None
        except ValueError:
            stored = None
        if not stored or not stored.get("reviews"):
            raise HTTPException(status_code=409, detail="Run the AI review first. There is nothing to apply yet.")
        if stored.get("incomplete"):
            raise HTTPException(status_code=409, detail="The last review did not finish (a reviewer could not run). Run the review again before applying fixes.")
        if stored.get("cards_hash") != _cards_hash(db, script_id):
            raise HTTPException(status_code=409, detail="The script changed after the review, so its findings may be out of date. Run the review again, then apply the fixes.")
        if not any(f.get("severity") in ("blocker", "major", "minor") and str(f.get("card_id")).isdigit() for r in stored["reviews"] for f in (r.get("findings") or [])):
            raise HTTPException(status_code=409, detail="The review found nothing to fix.")
        queue = stored.get("checklist") or _checklist_from_findings([f for r in stored["reviews"] for f in (r.get("findings") or [])])
        set_checklist(script_id, [{**i, "status": "todo" if i.get("status") in ("open", "new", "applied") else i.get("status")} for i in queue])
        stored_lens = [r for r in stored["reviews"] if r.get("lens_name") != "Rule Check (automatic)"]
        stored_fit = [f for r in stored["reviews"] if r.get("lens_name") == "Rule Check (automatic)" for f in (r.get("findings") or []) if f.get("rule") in ("V5", "Q4")]
    do_prep = mode in ("apply", "full") and only_cards is None       # the free fixes and the setup checklist are changes: only on apply

    from agents.progress import report
    title, agendas, outcomes = _script_scope(script)
    scope = _scope_digest(script)
    kit = _opening_kit(script)
    required_content = kit_required_items(kit)[0] if kit else {}
    opening = _program_opening(script)
    continuity = _continuity_context(script, db)
    try:
        agenda_timing_now = _agenda_timing_for(script, json.loads(script.outline_json) if script.outline_json else None)
    except ValueError:
        agenda_timing_now = []
    previous_findings: list = []
    try:
        previous_findings = [f for r in (json.loads(script.review_json).get("reviews") or []) for f in (r.get("findings") or [])] if script.review_json else []
    except ValueError:
        previous_findings = []
    earlier_findings: list = []            # what the last review of this script reported, so a fixed script is not marked again for the same things
    try:
        for r in (json.loads(script.review_json).get("reviews") or []) if script.review_json else []:
            if not str(r.get("lens_name") or "").startswith("Rule Check"):
                earlier_findings += [f"Card {f.get('card_id')}: {f.get('problem')}" for f in (r.get("findings") or []) if f.get("severity") in ("blocker", "major") and f.get("problem")]
    except ValueError:
        pass
    notes: list = []
    reviews: list = []
    lens_reviews: list = []
    all_passed = False
    passes_done = 0
    not_run: list = []
    changed_since = None            # None: nothing reviewed yet, so read every card
    previous_blockers, stalled = None, 0
    fit_findings: list = []         # the model's meaning check of visuals and quizzes, kept for cards not read again

    def read_cards(cards):
        """The three lenses over these cards; one more try, after a pause, when a lens could not run (usually a rate limit)."""
        got = run_multi_lens_reviews(class_title=title, cards=cards, target_duration_mins=script.duration_minutes,
                                     content_mix_beginner=script.content_mix_beginner, scope_digest=scope, script_id=script_id, lens_keys=_script_lenses(script), program=_program_of(script),
                                     already_reported=earlier_findings)
        if any(r.get("ran") is False and not r.get("passed", False) for r in got):
            import time as _time
            report(script_id, "The model's per-minute limit was reached; trying the reviewers again in 30 seconds")
            _time.sleep(30)
            got = run_multi_lens_reviews(class_title=title, cards=cards, target_duration_mins=script.duration_minutes,
                                         content_mix_beginner=script.content_mix_beginner, scope_digest=scope, script_id=script_id, lens_keys=_script_lenses(script), program=_program_of(script),
                                         already_reported=earlier_findings)
        return got

    if do_prep and _load_setup(script) is None:  # a programming class gets its before-the-class checklist made along the way (best effort)
        from agents.class_setup import is_programming
        if is_programming(_review_cards_dicts(db, script_id)):
            try:
                report(script_id, "Making the before-the-class setup checklist")
                _make_setup(script, db)
                db.commit()
                notes.append("Made the before-the-class setup checklist.")
            except Exception as e:
                print(f"Setup checklist skipped: {e}")

    try:                             # a free code fix: the agenda card lists every agenda item with its time
        if not do_prep:
            raise ValueError("review only")
        from agents.timeline import ensure_agenda_block
        outline_now = json.loads(script.outline_json) if script.outline_json else None
        timing_now = _agenda_timing_for(script, outline_now)
        for agenda_card in db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.pedagogical_phase.ilike("agenda")).all():
            fixed_body = ensure_agenda_block(agenda_card.content_script or "", timing_now)
            if fixed_body != agenda_card.content_script:
                agenda_card.content_script = fixed_body
                db.commit()
                notes.append("Added the agenda, with the time for each item, to the agenda card.")
    except ValueError:
        pass

    if opening and do_prep:          # a free code fix: the first card of a program's first class carries the approved welcome
        from agents.shared_context import ensure_opening_welcome, has_opening_welcome
        first_card = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ~ScriptCard.pedagogical_phase.ilike("instructor%")).order_by(ScriptCard.card_index).first()
        if first_card is not None and not has_opening_welcome(first_card.content_script):
            first_card.content_script = ensure_opening_welcome(first_card.content_script, _opening_welcome(script))
            db.commit()
            notes.append(f"Added the approved spoken welcome to the start of card {first_card.card_index}.")

    for attempt in range(MAX_REPAIR_PASSES + 1):
        cards_dicts = _review_cards_dicts(db, script_id)
        reuse = mode == "apply" and attempt == 0            # fixing what the last review found: its findings are used, not read again
        if reuse:
            lens_reviews, fit_findings = list(stored_lens), list(stored_fit)
        elif changed_since is None:
            report(script_id, "Review round 1: checking the rules and reading every card")
            lens_reviews = read_cards(cards_dicts)
        else:
            report(script_id, f"Review round {attempt + 1}: re-reading the {len(changed_since)} repaired cards")
            fresh = read_cards([c for c in cards_dicts if c["card_index"] in changed_since])
            lens_reviews = merge_lens_reviews(lens_reviews, fresh, changed_since)
        from agents.fit_checker import judge_fit
        if reuse:
            fresh_fit, fit_problem = [], None
        else:
            report(script_id, "Checking that the diagrams, animations and quizzes say the right thing")
            fresh_fit, fit_problem = judge_fit(cards_dicts, scope, script_id, only=changed_since)
            fit_findings = [f for f in fit_findings if changed_since is not None and f["card_id"] not in changed_since] + fresh_fit
        if fit_problem and not any("meaning check" in n for n in notes):
            notes.append(f"The meaning check of diagrams and quizzes could not run ({fit_problem}); only the word check was done.")
        rule_review = as_review(check_script(cards_dicts, int(script.duration_minutes), agendas, outcomes, script.code_ratio,
                                             required_content=required_content, program_opening=opening, class_title=title,
                                             upcoming=continuity["upcoming"], known_terms=continuity["known_terms"], agenda_timing=agenda_timing_now,
                                             previous_class=continuity["previous"] if (continuity["previous"] or {}).get("has_record") else None,
                                             running_example=(json.loads(script.outline_json).get("running_example") if script.outline_json and isinstance(json.loads(script.outline_json), dict) else None)) + fit_findings)
        from agents.progress import add_partial
        add_partial(script_id, rule_review)
        reviews = [rule_review] + lens_reviews
        not_run = [r for r in lens_reviews if r.get("ran") is False and not r.get("passed", False)]
        if not_run:
            why = sorted({(r.get("summary", "").split("(", 1)[-1].rstrip(").")) for r in not_run})
            notes.append(f"{len(not_run)} of {len(lens_reviews)} reviewers could not run: {'; '.join(why)[:200]}. Run the review again.")
        # a script only passes when every lens actually ran and passed
        all_passed = not not_run and all(r.get("passed", False) for r in reviews)
        blockers = sum(int(r.get("blockers", 0)) for r in reviews)
        majors = sum(int(r.get("majors", 0)) for r in reviews)
        open_cards = [f for r in reviews for f in (r.get("findings") or []) if f.get("severity") in ("blocker", "major", "minor") and str(f.get("card_id")).isdigit()]
        if all_passed and not (fix_all and open_cards):
            notes.append("All review lenses and automatic rule checks passed." if attempt == 0 else f"Passed after {attempt} repair pass(es).")
            break
        if not_run:
            break                                   # repairing on a partial review would be guessing
        if mode == "find":
            notes.append(f"Nothing in the script was changed. The review found {blockers} blockers and {majors} major problems. Read them and fix what you agree with.")
            break
        if not fix_all and blockers == 0 and (attempt >= 1 or majors == 0):
            notes.append(f"No blockers left after {attempt} repair pass(es); the {majors} remaining major findings are for the SME.")
            break
        if attempt >= MAX_REPAIR_PASSES:
            notes.append(f"Stopped after {MAX_REPAIR_PASSES} repair passes with {blockers} blockers left; they are for the SME.")
            break
        stalled = stalled + 1 if (previous_blockers is not None and blockers >= previous_blockers) else 0
        if stalled >= 2:
            notes.append(f"Stopped: two repair passes in a row did not reduce the {blockers} blockers; they are for the SME.")
            break
        previous_blockers = blockers

        chosen = [{**r, "findings": [f for f in (r.get("findings") or []) if only_cards is None or str(f.get("card_id")) in {str(c) for c in only_cards}]} for r in reviews]
        fit_changed = _repair_visuals_and_quizzes(script, db, chosen, notes, all_severities=fix_all)     # visuals and quizzes are made again; the Moderator edits text
        for_moderator = [{**r, "findings": [f for f in (r.get("findings") or []) if f.get("rule") not in VISUAL_FIT_RULES + ("Q1",)]} for r in chosen]
        cards_dicts = _review_cards_dicts(db, script_id)
        repaired, _gtg, info = moderate_and_repair(
            cards=cards_dicts, reviews=for_moderator, target_duration_mins=int(script.duration_minutes),
            iteration=attempt + 1, scope_digest=scope, script_id=script_id, max_passes=MAX_REPAIR_PASSES,
            **({"severities": ("blocker", "major", "minor"), "max_cards": 40, "on_card": lambda idx, ok: mark_card(script_id, idx, "applied" if ok else "open")} if fix_all else {}))
        notes.append(info["summary"] + (" " + "; ".join(info["fixed"]) if info.get("fixed") else ""))
        passes_done = attempt + 1
        if not info["changed"] and not fit_changed:
            notes.append("The Moderator could not change anything more; the remaining findings are for the SME.")
            break                               # nothing changed, so another review would repeat itself
        by_index = {c["card_index"]: c for c in repaired}
        for row in db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index.in_(info["changed"])).all():
            new = by_index[row.card_index]
            row.title = new.get("title") or row.title
            row.duration_mins = new.get("duration_mins") or row.duration_mins
            row.content_script = new.get("content_script") or row.content_script
            row.stage_directions = new.get("stage_directions") or row.stage_directions
            code_before = row.code_snippet
            row.code_snippet = new.get("code_snippet")
            row.code_language = new.get("code_language") or row.code_language
            if (row.code_snippet or "").strip() and row.code_snippet != code_before:
                _run_card_code(row)
            if new.get("quiz"):
                row.quiz_json = json.dumps(new["quiz"])
            if new.get("sources") is not None:
                from agents.source_fetcher import resolve_sources
                row.sources_json = json.dumps(resolve_sources(new["sources"], [script.reference_urls, script.api_docs, script.additional_resources]))
        db.commit()
        if opening:                      # a rewrite must never drop the approved welcome of a track's first class: it is put back by code
            from agents.shared_context import ensure_opening_welcome, has_opening_welcome
            first_spoken = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ~ScriptCard.pedagogical_phase.ilike("instructor%")).order_by(ScriptCard.card_index).first()
            if first_spoken is not None and not has_opening_welcome(first_spoken.content_script):
                first_spoken.content_script = ensure_opening_welcome(first_spoken.content_script, _opening_welcome(script))
                db.commit()
                notes.append(f"Card {first_spoken.card_index}: the approved welcome was put back after the rewrite.")
        changed_since = set(info["changed"]) | fit_changed

    for r in reviews:
        db.add(ReviewAudit(
            script_id=script_id, lens_name=r.get("lens_name"), reviewer_persona=r.get("reviewer_persona"),
            findings_json=json.dumps(r.get("detailed_findings", [])), score=r.get("score", 9.0),
            passed=r.get("passed", True), retry_iteration=passes_done))

    ran_reviews = [r for r in reviews if r.get("ran") is not False]                 # a reviewer that did not run has no score: it must not drag the average to zero
    overall = round(sum(r.get("score", 9.0) for r in ran_reviews) / len(ran_reviews), 1) if ran_reviews else 0.0
    missing_lenses = [r.get("lens_name") for r in reviews if r.get("ran") is False]
    moderator_notes = " ".join(notes)
    finished = not not_run                                   # every lens actually ran: the script now goes to the SME, with whatever is still open
    script.review_passed, script.review_score, script.review_at = all_passed, overall, datetime.datetime.utcnow()
    script.review_json = json.dumps({"passed": all_passed, "score": overall, "incomplete": missing_lenses, "repairs": passes_done, "notes": moderator_notes,
                                     "at": script.review_at.isoformat(), "lenses": _script_lenses(script), "reviews": reviews,
                                     "mode": mode, "cards_hash": _cards_hash(db, script_id),
                                     "since_last": _compare_findings(previous_findings, [f for r in reviews for f in (r.get("findings") or [])]) if previous_findings else None,
                                     "checklist": (_build_checklist(previous_findings, [f for r in reviews for f in (r.get("findings") or [])], notes) if (previous_findings and mode != "find")
                                                   else _checklist_from_findings([f for r in reviews for f in (r.get("findings") or [])]))}, default=str)
    if finished and script.status in ("Script WIP", "SME Review", "Not Started"):
        script.status = "SME Review"
    log_activity(db, user_name=getattr(_actor, "name", None) or "The AI", user_role=getattr(_actor, "role", None), action_type="AI Fixes Applied" if mode == "apply" else "AI Review Run",
                 description=(f"AI fixes applied to script #{script_id} for '{title}': " if mode == "apply" else f"AI review of script #{script_id} for '{title}': ") + f"{'passed' if all_passed else 'findings left for the SME'} ({overall}/10, {passes_done} repair pass{'es' if passes_done != 1 else ''})")
    db.commit()
    report(script_id, "Review finished", done=True)

    return MultiAgentReviewResponse(
        script_id=script_id, overall_score=overall, is_gtg=all_passed, retry_count=passes_done, reviews=reviews, moderator_notes=moderator_notes)


@app.get("/api/scripts/{script_id}/review/latest")
def latest_review(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """The last AI review of this script, for the author and the Module Owner: every lens's findings, the score and what the Moderator fixed."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not script.review_json:
        return {"ran": False, "lenses": _script_lenses(script)}
    try:
        data = json.loads(script.review_json)
    except ValueError:
        return {"ran": False, "lenses": _script_lenses(script)}
    # a review of an earlier version of the script (the cards changed since, or it was made before reviews were fingerprinted) is marked stale
    return {"ran": True, **data, "stale": data.get("cards_hash") != _cards_hash(db, script_id)}

# ==============================================================================
# 5.1 THE OLD ONE-SHOT PIPELINE (RETIRED)
# ==============================================================================

@app.post("/api/scripts/{script_id}/run-graph")
def retired_run_graph(script_id: int):
    """The earlier all-in-one pipeline wrote scripts with an older writer, skipped the opening kit, the rule checker and the usage
    log, and nothing in the page calls it. Use the steps that do: outline, generate-section (or draft-all), review."""
    raise HTTPException(status_code=410, detail="run-graph has been retired. Use /outline, /draft-all and /review.")

# ==============================================================================
# 6. OFFICIAL MO GTG SIGN-OFF & SNAPSHOT PERSISTENCE
# ==============================================================================

@app.get("/api/scripts/{script_id}/inputs")
def get_script_inputs(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """What the author entered when the script was made, so a revision starts from the same inputs."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    cls = script.class_entity
    mod = cls.module if cls else None
    trk = mod.track if mod else None
    try:
        agendas = json.loads(script.custom_agendas) if script.custom_agendas else (json.loads(cls.agendas) if cls and cls.agendas else [])
    except ValueError:
        agendas = []
    return {
        "script_id": script.id, "status": script.status, "class_id": cls.id if cls else None, "module_id": mod.id if mod else None,
        "track_id": trk.id if trk else None, "program_id": trk.program_id if trk else None, "class_title": _effective_title(script),
        "agendas": agendas, "learning_outcomes": (script.learning_outcomes or "").strip() or None, "duration_minutes": script.duration_minutes,
        "content_mix_beginner": script.content_mix_beginner, "code_ratio": script.code_ratio,
        "reference_urls": script.reference_urls or script.additional_resources or "", "api_docs": script.api_docs or "",
        "revision_note": script.revision_note, "revision_by": script.revision_by,
        "revision_at": script.revision_at.isoformat() if script.revision_at else None,
        "cards_count": len(script.cards or []),
    }


@app.post("/api/scripts/{script_id}/revise")
def revise_script(script_id: int, payload: ScriptCreateRequest, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """The author starts again on a script the Module Owner did not approve: the inputs are updated and the outline is made again. The same script is kept
    (its history, its note); the written cards are replaced only when the new outline is approved."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    actor = _require_script_access(db, user_id, script, write=True)
    if script.status != "Script WIP" or not script.revision_note:
        raise HTTPException(status_code=409, detail="Only a script the Module Owner sent back for changes can be revised this way.")
    if script.user_id and script.user_id != actor.id:
        raise HTTPException(status_code=403, detail="Only the author of this script can revise it.")
    if payload.class_id != script.class_id:
        raise HTTPException(status_code=400, detail="A revision keeps the same class.")
    script.duration_minutes = payload.duration_minutes
    script.content_mix_beginner = payload.content_mix_beginner
    script.content_mix_advanced = payload.content_mix_advanced
    script.code_ratio = payload.code_ratio
    script.additional_resources = payload.additional_resources
    script.api_docs = payload.api_docs
    script.reference_urls = payload.reference_urls
    script.learning_outcomes = (payload.learning_outcomes or "").strip() or None
    script.custom_agendas = json.dumps(payload.custom_agendas) if payload.custom_agendas else None
    script.custom_class_title = payload.custom_class_title
    script.outline_json = None
    script.outline_approved = False
    script.regenerate_pending = True
    cls = script.class_entity
    log_activity(db, user_name=actor.name, user_role=actor.role, action_type="Script Revision Started",
                 description=f"Started a revision of script #{script.id} for '{_effective_title(script)}' to address the Module Owner's changes")
    db.add(ScriptEvent(script_id=script.id, user_id=actor.id, user_name=actor.name, user_role=actor.role, event_type="Revision Started"))
    db.commit()
    return {"script_id": script.id, "shareable_uuid": script.shareable_uuid, "status": script.status}


@app.post("/api/scripts/{script_id}/request-changes")
def request_script_changes(script_id: int, body: RequestChangesBody, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """The Module Owner does not approve a script: it goes back to its author as a draft, with the reason and what to change."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    user = _require_actor(db, user_id)
    if user.role == "pm":
        raise HTTPException(status_code=403, detail="Program Managers have read-only access and cannot review scripts.")
    if user.role != "mo":
        raise HTTPException(status_code=403, detail="Only the Module Owner of this program can send a script back for changes.")
    prog_id = _script_program_id(script)
    if prog_id and prog_id not in [p.id for p in user.programs]:
        raise HTTPException(status_code=403, detail="Module Owner is not assigned to the program of this script.")
    if script.status != "MO Approval":
        raise HTTPException(status_code=409, detail="This script is not waiting for your review.")
    reason = (body.reason or "").strip()
    if len(reason) < 10:
        raise HTTPException(status_code=400, detail="Please write what needs to change (at least a short sentence) so the author knows what to fix.")
    if len(reason) > 4000:
        raise HTTPException(status_code=400, detail="The note is too long. Keep it under 4000 characters.")

    script.status = "Script WIP"
    script.revision_note = reason
    script.revision_by = user.name
    script.revision_at = datetime.datetime.utcnow()
    db.add(ScriptEvent(script_id=script.id, user_id=user.id, user_name=user.name, user_role="mo", event_type="Changes Requested"))
    cls = script.class_entity
    log_activity(db, user_name=user.name, user_role="mo", action_type="Changes Requested",
                 description=f"Did not approve script #{script.id} for '{cls.title if cls else 'Script'}' and sent it back to the author. Reason: {reason[:300]}")
    db.commit()
    return {"status": script.status, "script_id": script.id, "revision_note": script.revision_note}


@app.post("/api/scripts/{script_id}/approve-gtg")
def approve_script_gtg(script_id: int, user_id: Optional[int] = None, db: Session = Depends(get_db)):
    """Module Owner (MO) applies the official GTG badge and saves memory for Class N+1."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    user = _require_actor(db, user_id)
    if user.role == "pm":
        raise HTTPException(status_code=403, detail="Program Managers have read-only access and cannot approve scripts.")
    if user.role == "sme":
        raise HTTPException(status_code=403, detail="SMEs/Authors cannot apply official MO GTG marks.")
    if user.role != "mo":
        raise HTTPException(status_code=403, detail="Only the Module Owner of this program can approve a script and add the GTG mark.")
    prog_id = _script_program_id(script)
    if prog_id and prog_id not in [p.id for p in user.programs]:
        raise HTTPException(status_code=403, detail="Module Owner is not assigned to the program of this script.")

    if script.status == "GTG":
        raise HTTPException(status_code=409, detail="This script is already approved.")
    if script.status != "MO Approval":                     # the author sends a script to the Module Owner first; a draft is not theirs to approve yet
        raise HTTPException(status_code=409, detail="This script has not been sent to the Module Owner yet. The author must submit it for review first.")
    if not script.cards:
        raise HTTPException(status_code=400, detail="This script has no cards, so there is nothing to approve.")

    script.status = "GTG" # Good To Go

    script.approved_at = datetime.datetime.utcnow()
    if user_id:
        mo_user = db.query(User).filter(User.id == user_id).first()
        if mo_user:
            script.approved_by_name = mo_user.name
    if not script.approved_by_name:
        script.approved_by_name = "Rohan Gupta (MO)"
    db.commit()

    cls = script.class_entity
    cls_title = cls.title if cls else "Script"
    log_activity(
        db,
        user_name=script.approved_by_name,
        user_role="mo",
        action_type="Official GTG Sign-Off",
        description=f"Approved script #{script.id} for '{cls_title}' with official GTG sign-off and archived memory ledger snapshot"
    )

    # The record the next class reads: built from this approved script itself (its cards, its outline), never from a stand-in
    from agents.class_memory import build_class_memory
    cls = script.class_entity
    cards_for_memory = _review_cards_dicts(db, script_id)
    try:
        outline_for_memory = json.loads(script.outline_json) if script.outline_json else None
    except ValueError:
        outline_for_memory = None
    next_cls = db.query(Class).filter(
        Class.module_id == cls.module_id,
        Class.sequence_number > cls.sequence_number,
        Class.sequence_number < 1000
    ).order_by(Class.sequence_number.asc()).first()
    memory = build_class_memory(_effective_title(script), cards_for_memory, outline_for_memory, next_cls.title if next_cls else None)
    pointer = memory.get("next_pointer") or (f"Next class: {next_cls.title}." if next_cls else "")
    save_memory_ledger(
        db=db,
        class_id=cls.id,
        module_id=cls.module_id,
        terms=memory["terms"],
        abstractions=[a["name"] for a in memory["artefacts"]],
        analogy=memory["running_example"],          # None when the class had none: the next class chooses its own
        cliffhanger=pointer or None,
        summary=memory
    )

    if not script.share_revoked_at:                        # a link an owner switched off stays off
        from models import default_share_expiry
        script.share_expires_at = default_share_expiry()
        db.commit()
    return {
        "status": "OFFICIALLY_APPROVED",
        "script_id": script.id,
        "gtg_badge": True,
        "shareable_url": f"/script/{script.shareable_uuid}",
        "message": "Script approved with official GTG mark. Memory ledger snapshot archived for next class."
    }

# ==============================================================================
# 7. SCRIPT DETAILS & SHAREABLE PERMALINK
# ==============================================================================

@app.get("/api/scripts/{script_id}", response_model=ScriptDetailResponse)
def get_script_detail(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """Full script cards, visual diagrams, and metadata."""
    return _build_script_detail(script_id, db)

def _approved_urls(script) -> list:
    """Every link the SME supplied: reference URLs, API docs, additional resources, and the links in the program kit."""
    from agents.sources import provided_urls
    texts = [script.reference_urls, script.api_docs, script.additional_resources]
    try:
        kit = _opening_kit(script)
        texts += [str(v) for v in (kit or {}).get("facts", {}).values()]
    except Exception:
        pass
    return provided_urls(*texts)


OUTSIDE_KINDS = ("fetched", "search", "link")        # found by the AI outside the module's approved list: the SME checks these before they are relied on (FR-14)


def _card_sources(script, card) -> list:
    """The references a card shows: what is stored plus a line for each link in its text."""
    from agents.sources import body_links, norm_url
    stored = json.loads(card.sources_json) if card.sources_json else None
    in_text = {norm_url(u) for _l, u in body_links(card.content_script)}
    shown = _with_link_sources(card.content_script, stored, _approved_urls(script)) or []
    return [x for x in shown if not (x.get("rejected") and norm_url(x.get("url") or "") not in in_text)]      # a rejected link the author has since taken out of the text is gone


def _references_to_check(script) -> list:
    """[(card_index, source)] for every outside reference the SME has not accepted yet, and every one they rejected that is still in the card's text."""
    out = []
    for card in sorted(script.cards or [], key=lambda c: c.card_index):
        for src in _card_sources(script, card):
            if src.get("url_kind") in OUTSIDE_KINDS and not src.get("validated"):
                out.append((card.card_index, src))
    return out


@app.post("/api/scripts/{script_id}/cards/{card_index}/sources/review")
def review_card_source(script_id: int, card_index: int, payload: SourceReviewRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """The SME checks one reference the AI found: accept it (it is then marked as checked by them), or remove it. A link that is also written in the card's
    text cannot disappear from the list on its own, so it is marked rejected and the SME edits the card to take it out."""
    from agents.sources import norm_url
    script = db.query(Script).filter(Script.id == script_id).first()
    card = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index == card_index).first()
    if not script or not card:
        raise HTTPException(status_code=404, detail="Card not found")
    if payload.decision not in ("accept", "remove"):
        raise HTTPException(status_code=400, detail="The decision must be accept or remove.")
    key = norm_url(payload.url)
    shown = _card_sources(script, card)
    target = next((x for x in shown if norm_url(x.get("url") or "") == key), None)
    if target is None:
        raise HTTPException(status_code=404, detail="That reference is not on this card.")
    stored = [x for x in (json.loads(card.sources_json) if card.sources_json else []) if norm_url(x.get("url") or "") != key]
    in_text = any(norm_url(u) == key for _l, u in __import__("agents.sources", fromlist=["body_links"]).body_links(card.content_script))
    now = datetime.datetime.utcnow().isoformat()
    if payload.decision == "accept":
        stored.append({**target, "validated": True, "rejected": False, "validated_by": _actor.name, "validated_at": now})
    elif in_text:
        stored.append({**target, "validated": False, "rejected": True, "validated_by": _actor.name, "validated_at": now})
    card.sources_json = json.dumps(stored)
    _reopen_if_approved(db, script_id, _actor)
    log_activity(db, user_name=_actor.name, user_role=_actor.role, action_type="Reference Checked",
                 description=f"{'Accepted' if payload.decision == 'accept' else 'Removed'} a reference on card {card_index} of script #{script_id}: {(target.get('source') or target.get('url') or '')[:80]}")
    db.commit()
    return {"status": "OK", "decision": payload.decision, "still_in_text": in_text and payload.decision == "remove",
            "left_to_check": len(_references_to_check(db.query(Script).filter(Script.id == script_id).first()))}


def _with_link_sources(body, sources, approved_urls):
    """The card's sources plus an entry for each link its text gives, so every link shows its source."""
    from agents.sources import add_link_sources
    return add_link_sources(body, sources, approved_urls)


def _put_agenda_in_welcome(script, db: Session) -> None:
    """The welcome card carries the class agenda with each item's time, right after the welcome, and the card's talk continues after it. Free and
    repeatable: it changes the card only when the agenda or a time is missing."""
    from agents.timeline import ensure_agenda_block
    try:
        outline = json.loads(script.outline_json) if script.outline_json else None
    except ValueError:
        return
    timing = _agenda_timing_for(script, outline)
    card = next((c for c in sorted(script.cards, key=lambda c: c.card_index) if (c.pedagogical_phase or "").lower() == "agenda" and (c.content_script or "").strip()), None)
    if not timing or card is None:
        return
    body = ensure_agenda_block(card.content_script, timing)
    if body != card.content_script:
        card.content_script = body
        db.commit()


def _build_script_detail(script_id: int, db: Session):
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    _put_agenda_in_welcome(script, db)
    approved_urls = _approved_urls(script)

    cls = script.class_entity
    mod = cls.module
    prog = mod.track.program

    from agents.timeline import build_timeline
    from agents.rule_checker import spoken_words as _spoken_words
    from agents.shared_context import word_budget as _word_budget
    clock_by_card = {t["card_index"]: t for t in build_timeline([{"card_index": c.card_index, "duration_mins": c.duration_mins} for c in script.cards])}
    cards_out = []
    for c in sorted(script.cards, key=lambda card: card.card_index):
        visual_data = json.loads(c.visual_asset_json) if c.visual_asset_json else None
        if isinstance(visual_data, str):
            try:
                visual_data = json.loads(visual_data)
            except Exception:
                pass

        quiz_data = json.loads(c.quiz_json) if c.quiz_json else None
        if isinstance(quiz_data, str):
            try:
                quiz_data = json.loads(quiz_data)
            except Exception:
                pass

        cards_out.append(ScriptCardOut(
            id=c.id,
            card_index=c.card_index,
            title=c.title,
            duration_mins=c.duration_mins,
            target_words=c.target_words,
            pedagogical_phase=c.pedagogical_phase,
            stage_directions=c.stage_directions,
            content_script=__import__("agents.rule_checker", fromlist=["strip_card_headers"]).strip_card_headers(c.content_script),
            code_snippet=c.code_snippet,
            code_language=c.code_language,
            quiz=quiz_data,
            visual_type=visual_data.get("type") if visual_data else None,
            visual_content=visual_data.get("content") if visual_data else None,
            visual_caption=visual_data.get("caption") if visual_data else None,
            visual_id=visual_data.get("id") if visual_data else None,
            visual_srcdoc=(__import__("agents.safe_html", fromlist=["animation_document"]).animation_document(visual_data.get("content"))
                           if visual_data and visual_data.get("type") == "animation"
                           and __import__("agents.safe_html", fromlist=["animation_is_complete"]).animation_is_complete(visual_data.get("content")) else None),
            visual_approved=bool(visual_data.get("approved")) if visual_data else False,
            visual_stale=bool(visual_data.get("stale")) if visual_data else False,
            code_run=json.loads(c.code_run_json) if getattr(c, "code_run_json", None) else None,
            visual_source=visual_data.get("source") if visual_data else None,
            visual_note=(visual_data.get("note") or
                         ("This animation was cut off while it was being written, so it cannot play. Press Regenerate."
                          if visual_data and visual_data.get("type") == "animation"
                          and not __import__("agents.safe_html", fromlist=["animation_is_complete"]).animation_is_complete(visual_data.get("content")) else None))
                        if visual_data else None,
            sources=_with_link_sources(c.content_script, json.loads(c.sources_json) if c.sources_json else None, approved_urls),
            is_approved=c.is_approved,
            starts_at_sec=clock_by_card[c.card_index]["start_sec"], ends_at_sec=clock_by_card[c.card_index]["end_sec"],
            spoken_words=(_spoken_words(c.content_script) if (c.pedagogical_phase or "").lower() not in ("instructor_note", "instructor note") else None),
            word_budget=_word_budget(c.duration_mins)[0], words_max=_word_budget(c.duration_mins)[1]
        ))

    return ScriptDetailResponse(
        id=script.id,
        class_id=cls.id,
        class_title=_effective_title(script),
        module_name=mod.name,
        track_name=mod.track.name if (mod and mod.track) else "Standard",
        program_name=prog.name,
        status=script.status,
        shareable_uuid=script.shareable_uuid,
        duration_minutes=script.duration_minutes,
        cards_count=len(cards_out),
        created_at=script.created_at,
        cards=cards_out,
        agenda=[{"item": t["item"], "minutes": t["minutes"]} for t in _agenda_timing_for(script, json.loads(script.outline_json) if script.outline_json else None)],
        setup=_load_setup(script),
        revision_note=script.revision_note if script.status != "GTG" else None,
        revision_by=script.revision_by if (script.revision_note and script.status != "GTG") else None,
        revision_at=script.revision_at if (script.revision_note and script.status != "GTG") else None,        author_name=re.sub(r"\s*\((?:SME|MO|PM|Admin)\)\s*$", "", script.sme_name or (script.user.name if script.user else ""), flags=re.I).strip() or None,
        review_passed=script.review_passed, review_score=script.review_score, review_at=script.review_at,
        approved_by_name=(re.sub(r"\s*\((?:SME|MO|PM|Admin)\)\s*$", "", script.approved_by_name or "", flags=re.I).strip() or None) if script.status == "GTG" else None,
        approved_at=script.approved_at if script.status == "GTG" else None,
    )


# ---- share links: they expire and can be revoked ------------------------------------------------------------------

def _share_state(script: Script) -> dict:
    """Is the script's public link working, and until when?"""
    now = datetime.datetime.utcnow()
    if script.share_revoked_at:
        status = "revoked"
    elif script.share_expires_at and script.share_expires_at <= now:
        status = "expired"
    else:
        status = "active"
    days_left = None
    if script.share_expires_at and status == "active":
        days_left = max(0, (script.share_expires_at - now).days)
    return {
        "url": f"/script/{script.shareable_uuid}", "status": status, "active": status == "active",
        "expires_at": script.share_expires_at.isoformat() if script.share_expires_at else None,
        "days_left": days_left, "never_expires": script.share_expires_at is None,
        "revoked_at": script.share_revoked_at.isoformat() if script.share_revoked_at else None,
    }


def _closed_link_message(state: dict) -> str:
    return ("This link has been switched off by the script owner." if state["status"] == "revoked"
            else "This link has expired. Ask the script owner for a new one.")


@app.get("/api/scripts/{script_id}/share")
def get_share_state(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    script = db.query(Script).filter(Script.id == script_id).first()
    return _share_state(script)


@app.post("/api/scripts/{script_id}/share/revoke")
def revoke_share_link(script_id: int, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Switch the public link off now. Anyone holding it sees a 'switched off' page."""
    script = db.query(Script).filter(Script.id == script_id).first()
    script.share_revoked_at = datetime.datetime.utcnow()
    db.commit()
    return _share_state(script)


@app.post("/api/scripts/{script_id}/share/renew")
def renew_share_link(script_id: int, days: Optional[int] = None, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Make a NEW link (the old address stops working for good) valid for `days` (default SHARE_LINK_DAYS; 0 = never)."""
    import uuid as _uuid
    from models import default_share_expiry
    if days is not None and (days < 0 or days > 365):
        raise HTTPException(status_code=400, detail="days must be between 0 (never expires) and 365.")
    script = db.query(Script).filter(Script.id == script_id).first()
    script.shareable_uuid = str(_uuid.uuid4())
    script.share_revoked_at = None
    script.share_expires_at = default_share_expiry() if days is None else (
        datetime.datetime.utcnow() + datetime.timedelta(days=days) if days > 0 else None)
    db.commit()
    return _share_state(script)

@app.get("/api/scripts/share/{shareable_uuid}")
def get_shared_script(shareable_uuid: str, db: Session = Depends(get_db)):
    """Public read-only permalink for live instructors and MOs."""
    script = db.query(Script).filter(Script.shareable_uuid == shareable_uuid).first()
    if not script:
        raise HTTPException(status_code=404, detail="Shared script not found")
    state = _share_state(script)
    if not state["active"]:
        raise HTTPException(status_code=410, detail=_closed_link_message(state))
    return _build_script_detail(script.id, db)

@app.delete("/api/scripts/{script_id}")
def delete_script(
    script_id: int,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """Deletes a generated script and all of its associated cards, visual assets, and audits."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    user = _require_script_access(db, user_id, script, write=True, allow_admin_write=True)
    if user.role == "sme" and script.user_id and script.user_id != user.id:
        raise HTTPException(status_code=403, detail="SMEs can only delete scripts they authored.")
    if user.role in ("sme", "mo") and script.status in ("GTG", "MO Approval"):
        raise HTTPException(status_code=409, detail="A script that was approved, or is waiting for the Module Owner, cannot be deleted. Ask an administrator.")

    class_title = script.class_entity.title if script.class_entity else f"Script #{script_id}"
    db.delete(script)
    db.commit()
    return {"success": True, "message": f"Script #{script_id} ('{class_title}') deleted successfully."}


# ==============================================================================
# 8. INLINE CARD EDITING & CHAT-BASED CO-PILOT (PRD FR-11 & FR-21)
# ==============================================================================

@app.put("/api/scripts/{script_id}/cards/{card_id}")
def update_script_card(script_id: int, card_id: int, payload: CardUpdateRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Allows SME to manually edit any card in the deck (PRD FR-11)."""
    card = db.query(ScriptCard).filter(ScriptCard.id == card_id, ScriptCard.script_id == script_id).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    if payload.title is not None:
        card.title = payload.title
    if payload.duration_mins is not None:
        card.duration_mins = payload.duration_mins
    if payload.pedagogical_phase is not None:
        card.pedagogical_phase = payload.pedagogical_phase
    if payload.stage_directions is not None:
        card.stage_directions = payload.stage_directions
    if payload.content_script is not None:
        from agents.rule_checker import strip_card_headers
        card.content_script = strip_card_headers(payload.content_script)
    code_changed = (payload.code_snippet is not None and payload.code_snippet != card.code_snippet) or \
                   (payload.code_language is not None and payload.code_language != card.code_language)
    if payload.code_snippet is not None:
        card.code_snippet = payload.code_snippet
    if payload.code_language is not None:
        card.code_language = payload.code_language
    if payload.is_approved is not None:
        card.is_approved = payload.is_approved
    if code_changed and (card.code_snippet or "").strip():
        _run_card_code(card)

    if any(v is not None for v in (payload.title, payload.duration_mins, payload.pedagogical_phase, payload.stage_directions, payload.content_script, payload.code_snippet, payload.code_language)):
        _reopen_if_approved(db, script_id, _actor)          # approving a card is not a change to the script; editing it is
    db.commit()
    db.refresh(card)
    continuity_note = None
    if payload.is_approved:                                  # two cards approved side by side are read together: does this one pick up where the one before stopped?
        before = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index == card.card_index - 1).first()
        if before is not None and before.is_approved and (card.pedagogical_phase or "").lower() not in ("instructor_note", "quiz"):
            from agents.rule_checker import continuity_problem
            spoken_before = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index < card.card_index,
                                                        ~ScriptCard.pedagogical_phase.ilike("instructor%")).count() > 0
            continuity_note = continuity_problem(card.content_script or "", {"title": before.title, "content_script": before.content_script}, spoken_before)
    return {"status": "UPDATED", "card_id": card.id, "is_approved": card.is_approved, "continuity_note": continuity_note, "message": "Card changes saved successfully."}

_CODE_WORDS = re.compile(r"\b(code|query|snippet|program|script|function|variable|loop|run|runs|output|print|error|bug|fix|select|sql|python|java|javascript|syntax|line)\b", re.I)


# ==============================================================================
# THE SETUP CHECKLIST of a programming class: what must work before the class starts
# ==============================================================================

def _load_setup(script):
    """The saved setup checklist, with where each tool is really used in the class's cards (tools no card uses are left out)."""
    from sqlalchemy.orm import object_session
    from agents.class_setup import annotate_usage
    data = _load_setup_raw(script)
    db = object_session(script)
    return annotate_usage(data, _review_cards_dicts(db, script.id)) if (data and db is not None) else data


def _load_setup_raw(script):
    try:
        data = json.loads(script.setup_json) if getattr(script, "setup_json", None) else None
    except ValueError:
        data = None
    return data if isinstance(data, dict) and data.get("items") else None


def _make_setup(script, db: Session) -> dict:
    """Build, check and save the setup checklist from the class's own code. Raises LLMUnavailable or ValueError."""
    from agents.class_setup import build_setup
    cls = script.class_entity
    cards = _review_cards_dicts(db, script.id)
    ctx = {"title": script.custom_class_title or (cls.title if cls else "Programming class"), "minutes": script.duration_minutes,
           "program": cls.module.track.program.name if cls and cls.module and cls.module.track and cls.module.track.program else "",
           "track": _track_name(script),
           "notes": " ".join(filter(None, [script.additional_resources, script.api_docs, script.reference_urls]))}
    kit = _opening_kit(script)
    if kit:
        env = {k: v for k, v in (kit.get("facts") or {}).items() if re.search(r"environment|ide|colab|setup|tools?", str(k), re.I)}
        ctx["notes"] += " " + " ".join(f"{k}: {v}" for k, v in env.items())
    setup = build_setup(ctx, cards, lambda prompt: call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=2800, temperature=0.2,
                                                                  agent="class_setup", script_id=script.id))
    script.setup_json = json.dumps(setup)
    return setup


@app.get("/api/scripts/{script_id}/setup")
def get_setup(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """The setup checklist (if made) and whether this class needs one (it has code)."""
    from agents.class_setup import is_programming, setup_html, setup_markdown
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    setup = _load_setup(script)
    return {"is_programming": is_programming(_review_cards_dicts(db, script_id)), "setup": setup, "html": setup_html(setup), "markdown": setup_markdown(setup) or ""}


@app.post("/api/scripts/{script_id}/setup/generate")
def generate_setup(script_id: int, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Make (or remake) the setup checklist: tools, versions, how to check each works, and a checked source link for each."""
    bind_usage_script(script_id)
    from agents.class_setup import is_programming, setup_html
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not is_programming(_review_cards_dicts(db, script_id)):
        raise HTTPException(status_code=409, detail="This class has no code yet, so there is nothing to set up. Write the code cards first.")
    if not (llm_configured() and os.getenv("EXECUTION_MODE", "hybrid") != "mock"):
        raise HTTPException(status_code=503, detail="The AI is not configured, so it cannot make the checklist. Add the AI key in backend/.env and restart.")
    try:
        setup = _make_setup(script, db)
    except LLMUnavailable:
        raise
    except Exception as e:
        print(f"Setup checklist failed ({e}).")
        raise HTTPException(status_code=502, detail="The AI could not make the checklist. Nothing was changed; try again.")
    db.commit()
    setup = _load_setup(script)
    return {"is_programming": True, "setup": setup, "html": setup_html(setup)}


# ==============================================================================
# QUIZZES: edit, regenerate and approve one quiz at a time
# ==============================================================================

def _quiz_card(db: Session, script_id: int, card_index: int) -> ScriptCard:
    card = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index == card_index).first()
    if card is None:
        raise HTTPException(status_code=404, detail="Card not found")
    return card


def _quiz_of(card: ScriptCard):
    try:
        quiz = json.loads(card.quiz_json) if card.quiz_json else None
    except ValueError:
        quiz = None
    return quiz if isinstance(quiz, dict) else None


def _save_quiz(card: ScriptCard, quiz: dict, approved: bool = False) -> dict:
    quiz = {k: v for k, v in quiz.items() if k != "approved"}
    if approved:
        quiz["approved"] = True
    card.quiz_json = json.dumps(quiz)
    card.is_approved = False                          # the card changed, so the card's own approval is taken back
    return quiz


@app.put("/api/scripts/{script_id}/cards/{card_index}/quiz")
def edit_quiz(script_id: int, card_index: int, payload: QuizEditRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Save the SME's own edit of a quiz: question, options, the right answer, the explanation and why each wrong option is wrong.
    An edited quiz is no longer approved."""
    from agents.quiz_format import quiz_from_edit, quiz_problems
    card = _quiz_card(db, script_id, card_index)
    quiz = quiz_from_edit(payload.question, payload.options, payload.correct_option_index, payload.explanation, payload.reasons)
    # an edit may be unfinished (a reason still to write), so it is saved; the problems are returned and approval will insist on them
    if not str(quiz.get("question") or "").strip() or len([o for o in quiz.get("options", []) if str(o).strip()]) < 2:
        raise HTTPException(status_code=400, detail="A quiz needs a question and at least two options.")
    if not 0 <= payload.correct_option_index < len(quiz["options"]):
        raise HTTPException(status_code=400, detail="Choose which option is the right answer.")
    saved = _save_quiz(card, quiz)
    _reopen_if_approved(db, script_id, _actor)
    db.commit()
    return {"card_index": card_index, "quiz": saved, "problems": quiz_problems(saved)}


@app.post("/api/scripts/{script_id}/cards/{card_index}/quiz/regenerate")
def regenerate_quiz(script_id: int, card_index: int, payload: QuizRegenerateRequest = Body(default=QuizRegenerateRequest()),
                    _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Make this card's quiz again. The AI is shown what the nearby cards taught, so the quiz tests only that, and the right answer is moved
    to a different place so it is not always the same letter."""
    bind_usage_script(script_id)
    from agents.quiz_format import clean_quiz, move_correct_option, quiz_problems
    from agents.rule_checker import clean_spoken, spoken_text
    card = _quiz_card(db, script_id, card_index)
    script = db.query(Script).filter(Script.id == script_id).first()
    if not (llm_configured() and os.getenv("EXECUTION_MODE", "hybrid") != "mock"):
        raise HTTPException(status_code=503, detail="The AI is not configured, so it cannot make a new quiz. Add the AI key in backend/.env and restart.")
    taught = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index < card_index) \
        .order_by(ScriptCard.card_index.desc()).limit(3).all()
    taught_text = "\n\n".join(f'Card {c.card_index}, "{c.title}": {spoken_text(c.content_script)[:900]}' + (f"\nIts code:\n{c.code_snippet[:500]}" if c.code_snippet else "")
                                for c in reversed(taught)) or "(nothing before this card)"
    old = _quiz_of(card)
    prompt = (
        f"You write ONE quiz question for a live class.\n{_scope_digest(script)}\n\n"
        f"WHAT THE CLASS JUST TAUGHT (test only this, using only terms taught here or earlier):\n{taught_text}\n\n"
        f'THIS QUIZ CARD: "{card.title}". {spoken_text(card.content_script)[:300]}\n'
        + (f"THE CURRENT QUIZ (write a different one): {json.dumps({k: old.get(k) for k in ('question', 'options')})}\n" if old else "")
        + (f"THE SME ASKS: {payload.instruction.strip()[:400]}\n" if (payload.instruction or "").strip() else "")
        + "\nRules:\n"
          "- question: one or two short sentences. If it uses code, put the code in a fenced block, and the code must run as written (no input(), no network, no files).\n"
          "- options: exactly 4, each one short line of at most 12 words, with NO letter in front. The wrong options are the mistakes learners really make, tempting but clearly wrong once explained. No 'all of the above', no 'none of the above'.\n"
          "- correct_option_index: 0 to 3.\n"
          "- explanation: one or two sentences on why the right option is right. Do not name a letter.\n"
          "- distractor_explanations: for EACH wrong option, keyed by its letter A to D, one short sentence (at most 20 words) on why it is wrong.\n"
          "- No em dashes. Do not use the letters A to D inside the explanations.\n"
          'Return ONLY JSON: {"question": string, "options": [string], "correct_option_index": number, "explanation": string, "distractor_explanations": {"A": string}}'
    )
    try:
        out = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=1500, temperature=0.6, agent="quiz_regenerate", script_id=script_id)
    except LLMUnavailable:
        raise
    except Exception as e:
        print(f"Quiz regeneration failed ({e}).")
        raise HTTPException(status_code=502, detail="The AI could not make a new quiz. The old quiz is unchanged; try again.")
    quiz = clean_quiz(out) if isinstance(out, dict) else None
    problems = quiz_problems(quiz)
    if problems:
        raise HTTPException(status_code=502, detail="The AI's quiz was not complete (" + " ".join(problems) + ") so it was not used. The old quiz is unchanged; try again.")
    n = len(quiz["options"])
    shift = 1 + (card_index + int((old or {}).get("correct_option_index", 0) or 0)) % max(1, n - 1)      # 1 to n-1: always a different place
    quiz = move_correct_option(quiz, (int(quiz["correct_option_index"]) + shift) % n)
    saved = _save_quiz(card, quiz)
    _reopen_if_approved(db, script_id, _actor)
    db.commit()
    return {"card_index": card_index, "quiz": saved, "problems": []}


@app.post("/api/scripts/{script_id}/cards/{card_index}/quiz/approve")
def approve_quiz(script_id: int, card_index: int, approved: bool = True, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Mark a quiz as checked and good to teach (or take that back with approved=false). A quiz can only be approved when it is complete."""
    from agents.quiz_format import quiz_problems
    card = _quiz_card(db, script_id, card_index)
    quiz = _quiz_of(card)
    if quiz is None:
        raise HTTPException(status_code=409, detail="This card has no quiz.")
    if approved:
        problems = quiz_problems(quiz)
        if problems:
            raise HTTPException(status_code=409, detail="This quiz cannot be approved yet. " + " ".join(problems))
    quiz.pop("approved", None)
    if approved:
        quiz["approved"] = True
    card.quiz_json = json.dumps(quiz)
    db.commit()
    return {"card_index": card_index, "quiz": quiz, "approved": bool(approved)}


def _chat_context(db: Session, script: Script, card: ScriptCard) -> str:
    """What the AI co-pilot needs to know to change one card sensibly: the class, what it must cover, where the card sits, what the cards around it say, and
    anything the Module Owner asked to change. Without this it only sees one card and tends to repeat or contradict its neighbours."""
    from agents.rule_checker import spoken_text
    cls = script.class_entity
    mod = cls.module if cls else None
    prog = mod.track.program if mod and mod.track else None
    try:
        agendas = json.loads(script.custom_agendas) if script.custom_agendas else (json.loads(cls.agendas) if cls and cls.agendas else [])
    except ValueError:
        agendas = []
    cards = db.query(ScriptCard).filter(ScriptCard.script_id == script.id).order_by(ScriptCard.card_index).all()
    def gist(c, first=True, words=28):
        sentences = re.split(r"(?<=[.!?])\s+", spoken_text(c.content_script or "").strip())
        pick = (sentences[0] if first else " ".join(sentences[-2:])) if sentences else ""
        w = pick.split()
        return " ".join(w[:words]) + ("..." if len(w) > words else "")
    order = "; ".join(f"{c.card_index}. {c.title} ({c.pedagogical_phase})" + (" <- THIS CARD" if c.id == card.id else "") for c in cards)
    prev = next((c for c in reversed(cards) if c.card_index < card.card_index), None)
    nxt = next((c for c in cards if c.card_index > card.card_index), None)
    lines = [f"<script_context>",
             f"Class: {_effective_title(script)}" + (f" (program: {prog.name}; module: {mod.name})" if prog and mod else ""),
             f"Length: {script.duration_minutes} minutes; this card is {card.duration_mins} minutes, so its spoken text should fit that time.",
             f"Beginner to advanced mix: {script.content_mix_beginner}% beginner, climbing from beginner to advanced; about {script.code_ratio}% of cards carry code."]
    if agendas:
        lines.append("The class must cover: " + "; ".join(str(a) for a in agendas[:12]))
    outcomes = _effective_outcomes(script)
    if outcomes:
        lines.append("Learning outcomes: " + outcomes.replace("\n", " ")[:600])
    lines.append("Card order: " + order)
    if prev:
        lines.append(f"The card before this one ends with: \"{gist(prev, first=False)}\"")
    if nxt:
        lines.append(f"The card after this one begins with: \"{gist(nxt)}\" (so do not teach what it teaches)")
    if script.revision_note and script.revision_by != "Approval withdrawn":
        lines.append(f"The Module Owner asked for these changes to the script; respect them: {script.revision_note.strip()[:800]}")
    lines.append("Stay consistent with the other cards: keep the same running example, use the same terms, and do not repeat what the previous card already said.")
    lines.append("</script_context>")
    return "\n".join(lines)


@app.post("/api/scripts/{script_id}/chat-assist")
def chat_assist_card(script_id: int, payload: ChatAssistRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Inline AI Chat Co-Pilot to revise a card (PRD FR-11 & FR-21). On a card with code, or when the instruction is about code, the AI
    sees the code and how it last ran, may change both the words and the code, and a changed snippet is run again straight away."""
    bind_usage_script(script_id)       # every model call in this request is recorded against this script
    card = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index == payload.card_index).first()
    if not card:
        raise HTTPException(status_code=404, detail="Card not found")

    execution_mode = os.getenv("EXECUTION_MODE", "hybrid")
    if not (llm_configured() and execution_mode != "mock"):
        raise HTTPException(status_code=503, detail="The AI is not configured, so it cannot revise the card. Add the AI key in backend/.env and restart.")

    from agents.rule_checker import clean_spoken
    chat_context = _chat_context(db, db.query(Script).filter(Script.id == script_id).first(), card)
    has_code = bool((card.code_snippet or "").strip())
    new_code = new_lang = None
    code_changed = False
    code_run = None
    rules = (f"- Keep the same card structure and pedagogical phase\n"
             f"- Write only what the instructor says aloud: 'I' and 'we' speaking to 'you', never a direction to the instructor\n"
             f"- No em dashes, keep paragraphs 2-4 sentences\n")
    try:
        if has_code or _CODE_WORDS.search(payload.user_prompt or ""):
            run = json.loads(card.code_run_json) if card.code_run_json else None
            last_run = (f"status {run.get('status')}; output: {(run.get('stdout') or '').strip()[:600] or '(none)'}; error: {(run.get('stderr') or run.get('reason') or '').strip()[:400] or '(none)'}"
                        if run else "not run yet")
            prompt_body = (
                f"You are an inline AI co-pilot for Scaler lecture scripts.\n"
                f"The SME has asked you to revise one card. The card's text is the exact words the instructor says aloud.\n\n"
                f"{chat_context}\n\n"
                f'<card title="{card.title}" phase="{card.pedagogical_phase}">\n{card.content_script}\n</card>\n\n'
                f'<code language="{card.code_language or ""}">\n{card.code_snippet or "(this card has no code yet)"}\n</code>\n'
                f"Last run of the code: {last_run}\n\n"
                f"SME INSTRUCTION: {payload.user_prompt}\n\n"
                f"Rules:\n{rules}"
                f"- The code is RUN by the system and must work as written: no input(), no network, no files, no installs. SQL must create and fill its own small tables first (CREATE TABLE, INSERT), then query them.\n"
                f"- If the instruction does not need a code change, return code_snippet as null. If it does, return the WHOLE revised code, and make the spoken text match it (names, numbers, output).\n"
                f"- Do not repeat the title or the phase. Add no labels or markdown wrapper.\n\n"
                f'Return ONLY JSON: {{"content_script": string, "code_snippet": string or null, "code_language": string or null}}'
            )
            out = call_llm_json(messages=[{"role": "user", "content": prompt_body}], max_tokens=3500, temperature=0.3, agent="chat_assist", script_id=script_id)
            if not isinstance(out, dict) or not str(out.get("content_script") or "").strip():
                raise ValueError("the reply had no card text")
            refined_script = clean_spoken(str(out["content_script"]))
            if isinstance(out.get("code_snippet"), str) and out["code_snippet"].strip() and out["code_snippet"].strip() != (card.code_snippet or "").strip():
                new_code, new_lang, code_changed = out["code_snippet"].strip("\n"), (out.get("code_language") or card.code_language or None), True
        else:
            prompt_body = (
                f"You are an inline AI co-pilot for Scaler lecture scripts.\n"
                f"The SME has asked you to revise one card. The card's text is the exact words the instructor says aloud.\n\n"
                f"{chat_context}\n\n"
                f'<card title="{card.title}" phase="{card.pedagogical_phase}">\n{card.content_script}\n</card>\n\n'
                f"SME INSTRUCTION: {payload.user_prompt}\n\n"
                f"Rules:\n{rules}"
                f"- Return ONLY the revised spoken text of the card. Do not repeat the title or the phase, add no labels, no JSON, no markdown wrapper"
            )
            refined_script = clean_spoken(call_llm_chat(messages=[{"role": "user", "content": prompt_body}], max_tokens=1500, temperature=0.3,
                                                         agent="chat_assist", script_id=script_id))
    except LLMUnavailable:
        raise      # the SME must see that the AI did not answer, not get their own instruction pasted into the card
    except Exception as e:
        print(f"AI chat-assist failed ({e}).")
        raise HTTPException(status_code=502, detail="The AI could not revise this card. Your card is unchanged; try again.")

    card.content_script = refined_script
    if code_changed:
        card.code_snippet, card.code_language = new_code, new_lang
        code_run = _fix_and_run_card_code(card)          # run it now, and let the AI fix it once if it does not run
    _reopen_if_approved(db, script_id, _actor)
    db.commit()
    db.refresh(card)

    return {
        "status": "REVISED",
        "card_index": card.card_index,
        "new_content_script": card.content_script,
        "code_changed": code_changed,
        "code_snippet": card.code_snippet,
        "code_language": card.code_language,
        "code_run": code_run,
        "stage_directions": card.stage_directions,
        "message": f"Card {card.card_index} revised based on prompt: '{payload.user_prompt}'"
    }

def _outline_card_list(outline_raw):
    """[(section_id, card)] in order, from either outline shape (sections, or the older flat list)."""
    if isinstance(outline_raw, dict):
        return [(sec.get("section_id") or f"sec_{n + 1}", c) for n, sec in enumerate(outline_raw.get("sections", [])) for c in sec.get("cards", [])]
    if isinstance(outline_raw, list):
        return [(f"sec_{max(1, i // 4 + 1)}", c) for i, c in enumerate(outline_raw)]
    return []


@app.get("/api/scripts/{script_id}/draft-plan")
def draft_plan(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """The cards still to write, so the page can write them one at a time and show progress and errors."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not script.outline_json:
        raise HTTPException(status_code=400, detail="Must generate outline first.")
    planned = _outline_card_list(json.loads(script.outline_json))
    written = {c.card_index for c in script.cards if (c.content_script or "").strip()}
    return {"total": len(planned), "written": len(written),
            "remaining": [{"card_index": n, "section_id": sid, "title": c.get("title")} for n, (sid, c) in enumerate(planned, start=1) if n not in written]}


@app.post("/api/scripts/{script_id}/draft-all")
def draft_all_remaining_sections(script_id: int, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Writes every card that is not written yet, one card at a time. A card that fails is reported and the rest carry on,
    except when the budget is used up, which stops the run."""
    bind_usage_script(script_id)
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    if not script.outline_json:
        raise HTTPException(status_code=400, detail="Must generate outline first.")
    planned = _outline_card_list(json.loads(script.outline_json))
    written = {c.card_index for c in script.cards if (c.content_script or "").strip()}
    todo = [(n, sid) for n, (sid, _c) in enumerate(planned, start=1) if n not in written]
    failed, stopped, drafted = [], None, 0
    for n, sid in todo:
        try:
            generate_section(script_id=script_id, start_card=n, end_card=n, section_id=sid, user_prompt=None, _actor=_actor, db=db)
            drafted += 1
        except Exception as e:
            detail = getattr(e, "detail", None) or str(e)
            failed.append({"card_index": n, "error": str(detail)[:300]})
            if "budget" in str(detail).lower():
                stopped = "The AI budget is used up, so drafting stopped."
                break
    return {"total": len(planned), "already_written": len(written), "drafted": drafted, "failed": failed, "stopped": stopped}


# ==============================================================================
# THINGS ONLY THE SME CAN SUPPLY: [[FROM_SME: ...]] placeholders and program facts the kit lacks
# ==============================================================================

@app.get("/api/scripts/{script_id}/sme-items")
def sme_items(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """Everything the SME still has to supply, on one screen: each [[FROM_SME]] placeholder in the cards, and the opening-class facts
    the program kit does not hold (PSP, attendance, contests, FAQs, dashboard video, support contact)."""
    from agents.rule_checker import collect_sme_items, kit_required_items
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")
    kit = _opening_kit(script)
    return {"items": collect_sme_items(_review_cards_dicts(db, script_id)),
            "kit_gaps": kit_required_items(kit)[1] if kit else []}


class SmeFillRequest(BaseModel):
    card_index: int
    placeholder: str
    value: str


@app.post("/api/scripts/{script_id}/sme-items/fill")
def fill_sme_item(script_id: int, payload: SmeFillRequest, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Replace one [[FROM_SME: ...]] placeholder with what the SME typed."""
    import re as _re
    from agents.rule_checker import collect_sme_items
    value = payload.value.strip()
    if not value or len(value) > 500:
        raise HTTPException(status_code=400, detail="Give a value of 1 to 500 characters.")
    if not _re.fullmatch(r"\[\[FROM_SME:?[^\]]*\]\]", payload.placeholder):
        raise HTTPException(status_code=400, detail="That is not a [[FROM_SME: ...]] placeholder.")
    card = db.query(ScriptCard).filter(ScriptCard.script_id == script_id, ScriptCard.card_index == payload.card_index).first()
    if card is None or payload.placeholder not in (card.content_script or ""):
        raise HTTPException(status_code=404, detail="That placeholder is not on that card (it may already be filled).")
    card.content_script = card.content_script.replace(payload.placeholder, value.replace("[[", "").replace("]]", ""))
    card.is_approved = False
    db.commit()
    return {"remaining": len(collect_sme_items(_review_cards_dicts(db, script_id)))}


# ==============================================================================
# 9. EXPORTS & ASSIGNMENTS GENERATION (PRD FR-23)
# ==============================================================================

@app.get("/api/scripts/{script_id}/export/markdown")
def export_script_markdown(script_id: int, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """Generates clean, canonical Markdown export of the entire lecture script."""
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    cls = script.class_entity
    mod = cls.module
    prog = mod.track.program

    md_lines = [
        f"# {_effective_title(script)}",
        f"**Program:** {prog.name} | **Module:** {mod.name} | **Duration:** {script.duration_minutes} Mins",
        f"**Status:** {script.status} | **Shareable UUID:** `{script.shareable_uuid}`",
        "\n---\n"
    ]
    from agents.class_setup import setup_markdown
    setup_md = setup_markdown(_load_setup(script))
    if setup_md:
        md_lines.append(setup_md + "\n---\n")

    cards = db.query(ScriptCard).filter(ScriptCard.script_id == script_id).order_by(ScriptCard.card_index).all()
    from agents.timeline import build_timeline, clock
    clock_of = {t["card_index"]: (t["start_sec"], t["end_sec"]) for t in build_timeline([{"card_index": k.card_index, "duration_mins": k.duration_mins} for k in cards])}
    for c in cards:
        md_lines.append(f"## Card {c.card_index}: {c.title}")
        md_lines.append(f"*{clock(clock_of[c.card_index][0])} - {clock(clock_of[c.card_index][1])} on the class clock ({c.duration_mins} min)*\n")
        md_lines.append(f"**Duration:** {c.duration_mins} mins | **Phase:** {c.pedagogical_phase}\n")
        
        if c.stage_directions:
            md_lines.append(f"> **Stage Direction:** {c.stage_directions}\n")
        
        md_lines.append(f"### Spoken Script:\n{c.content_script}\n")

        if c.visual_asset_json:
            try:
                vis = json.loads(c.visual_asset_json)
                if vis.get("type") == "mermaid":
                    md_lines.append(f"```mermaid\n{vis.get('content')}\n```\n*{vis.get('caption')}*\n")
                elif vis.get("type") == "image":
                    md_lines.append(f"![{vis.get('caption')}]({vis.get('content')})\n*{vis.get('caption')}*\n")
                elif vis.get("type") == "animation":
                    md_lines.append(f"*[Interactive animation: {vis.get('caption') or 'see the script page'}. It plays in the web page version of this script (Download, Web page).]*\n")
            except:
                pass

        if c.code_snippet:
            md_lines.append(f"```{c.code_language or 'text'}\n{c.code_snippet}\n```\n")
            if c.code_run_json:
                try:
                    run = json.loads(c.code_run_json)
                    shown = (run.get("stdout") or "").strip() or (run.get("stderr") or "").strip()
                    label = {"passed": "Output", "failed": "Error when run", "timeout": "Stopped after the time limit", "skipped": "Not run"}.get(run.get("status"), "Result")
                    if shown:
                        md_lines.append(f"**{label}:**\n```text\n{shown[:2000]}\n```\n")
                    elif run.get("reason"):
                        md_lines.append(f"*{label}: {run['reason']}*\n")
                except ValueError:
                    pass

        if c.quiz_json:
            try:
                from agents.quiz_format import quiz_markdown
                md_lines.append(quiz_markdown(json.loads(c.quiz_json)))
            except:
                pass

        md_lines.append("\n---\n")

    full_markdown = "\n".join(md_lines)
    return {"script_id": script.id, "filename": re.sub(r"[^A-Za-z0-9_-]+", "_", _effective_title(script))[:60].strip("_") + ".md", "markdown": full_markdown, "setup_markdown": setup_md or ""}


@app.get("/api/scripts/{script_id}/export/html")
def export_script_html(script_id: int, print: bool = False, _actor: User = Depends(script_guard(False)), db: Session = Depends(get_db)):
    """The whole script as one web page you can keep: every card, the diagrams, the animations (they play), the code with what it printed, the quizzes and
    the setup checklist. `print=true` gives a light page without the navigation bar, for printing to PDF."""
    detail = _build_script_detail(script_id, db)
    page = _render_shared_script_html(detail)
    trace_js = ""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend", "code_trace.js"), encoding="utf-8") as fh:
            trace_js = fh.read()
    except OSError:
        pass
    if trace_js:                                                   # a saved copy has no server beside it, so the code-trace script travels inside the page
        page = page.replace('<script src="/code_trace.js?v=2.0"></script>', "<script>" + trace_js.replace("</script", "<\\/script") + "</script>", 1)
    if print:
        light = ("<style>:root{--bg-main:#ffffff;--bg-card:#ffffff;--bg-card-subtle:#f8fafc;--border:#e2e8f0;--text-primary:#0f172a;--text-secondary:#475569;--text-muted:#64748b}"
                 ".top-navbar,.nav-actions,.copy-btn,.fs-banner{display:none !important}body{padding-bottom:0}"
                 ".card-section{page-break-inside:auto;box-shadow:none}.visual-wrap,.code-box,.quiz2{page-break-inside:avoid}"
                 ".card-body,.card-body *,.dialogue-p,.card-para,.card-heading,.card-title,.doc-title,.stage-cue-box p{color:#0f172a !important}"
                 ".stage-cue-box{background:#fff !important;border:1px solid #bbb !important}.cue-label{color:#444 !important}.stage-cue{color:#444}"
                 ".card-link,.setup2-link,.quiz2-label,.breadcrumb{color:#111 !important}.card-link{text-decoration:underline}"
                 ".card-callout,.callout-mistake,.callout-remember,.callout-realworld,.callout-predict{border-left:3px solid #888 !important;background:#fff !important}.callout-label{color:#111 !important}"
                 ".quiz2,.setup2-item,.visual-wrap,.code-box{background:#fff !important}.quiz2:not(.hide-answers) .quiz2-opt.right{background:#fff !important;border-color:#444 !important}"
                 ".quiz2-expl{border-left-color:#888 !important;background:#fff !important}.quiz2-letter,.quiz2-tag,.setup2-chip,.setup2-chip.ok,.setup2-chip.ver{background:#e5e5e5 !important;color:#111 !important}"
                 ".visual-wrap{background:#f8fafc;border-color:#e2e8f0}.visual-caption{color:#475569}"
                 ".code-box{background:#f8fafc;border-color:#cbd5e1}.code-box-header{background:#e2e8f0;color:#0f172a}.code-box pre,.code-box code{color:#0f172a}"
                 ".setup2 code{background:#e2e8f0;color:#0f172a}.setup2-lead,.setup2-foot,.quiz2-why{color:#475569}</style>")
        page = page.replace("</head>", light + "</head>", 1).replace("theme: 'dark'", "theme: 'default'")
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", _effective_title(script := db.query(Script).filter(Script.id == script_id).first()))[:60].strip("_") or "Lecture_Script"
    return {"script_id": script_id, "filename": f"{safe}_Script.html", "html": page}

@app.post("/api/scripts/{script_id}/share-hackmd")
def share_script_to_hackmd(script_id: int, payload_body: dict = Body(default={}), _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Publishes the current script directly to HackMD.io as a clean, shareable document."""
    import urllib.request
    import urllib.error
    
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    token = os.getenv("HACKMD_API_TOKEN", "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="HACKMD_API_TOKEN is not configured in backend/.env")

    cls = script.class_entity
    custom_content = (payload_body.get("markdown_content") or "").strip()

    if custom_content:
        content_str = custom_content
    else:
        cards = db.query(ScriptCard).filter(ScriptCard.script_id == script_id).order_by(ScriptCard.card_index).all()
        if not cards:
            raise HTTPException(status_code=400, detail="No script sections have been drafted yet.")

        # Build clean HackMD formatted document
        md_lines = [
            f"# {_effective_title(script)}",
            f"> **Duration:** {script.duration_minutes} Mins | **Sections:** {len(cards)}",
            "\n---\n"
        ]
        from agents.class_setup import setup_markdown
        setup_md = setup_markdown(_load_setup(script))
        if setup_md:
            md_lines.append(setup_md + "\n---\n")

        from agents.timeline import build_timeline, clock
        clock_of = {t["card_index"]: (t["start_sec"], t["end_sec"]) for t in build_timeline([{"card_index": k.card_index, "duration_mins": k.duration_mins} for k in cards])}
        for c in cards:
            md_lines.append(f"## {c.card_index}. {c.title}  ({clock(clock_of[c.card_index][0])} - {clock(clock_of[c.card_index][1])}, {c.duration_mins} min)")
            if c.stage_directions:
                md_lines.append(f"> **Instructor Note:** {c.stage_directions}\n")
            
            md_lines.append(f"{c.content_script}\n")

            if c.visual_asset_json:
                try:
                    vis = json.loads(c.visual_asset_json)
                    if vis.get("type") == "mermaid":
                        md_lines.append(f"```mermaid\n{vis.get('content')}\n```\n*{vis.get('caption', '')}*\n")
                    elif vis.get("type") == "image":
                        md_lines.append(f"![{vis.get('caption', '')}]({vis.get('content')})\n")
                except:
                    pass

            if c.code_snippet:
                md_lines.append(f"```{c.code_language or 'python'}\n{c.code_snippet}\n```\n")

            if c.quiz_json:
                try:
                    from agents.quiz_format import quiz_markdown
                    md_lines.append(quiz_markdown(json.loads(c.quiz_json)))
                except:
                    pass

            md_lines.append("\n---\n")

        content_str = "\n".join(md_lines)

    payload = {
        "title": _effective_title(script),
        "content": content_str,
        "readPermission": "guest",
        "writePermission": "owner",
        "commentPermission": "everyone"
    }

    req_data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        "https://api.hackmd.io/v1/notes",
        data=req_data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "ScriptGenerator/1.0"
        },
        method="POST"
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            note_id = data.get("id")
            publish_link = data.get("publishLink")
            note_url = publish_link or f"https://hackmd.io/{note_id}"
            return {
                "success": True,
                "note_id": note_id,
                "url": note_url,
                "title": cls.title
            }
    except urllib.error.HTTPError as e:
        err_msg = e.read().decode("utf-8", errors="replace")
        raise HTTPException(status_code=500, detail=f"HackMD API Error: {err_msg}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to publish note to HackMD: {str(e)}")

@app.post("/api/scripts/{script_id}/generate-assignments", response_model=AssignmentResponse)
def generate_assignments_and_interviews(script_id: int, _actor: User = Depends(script_guard(True)), db: Session = Depends(get_db)):
    """Creates assignments documentation and mock interview questions (PRD FR-23)."""
    bind_usage_script(script_id)       # its model calls are recorded against this script
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    cls = script.class_entity

    homework = [
        {
            "problem_id": 1,
            "title": f"Production Implementation: Resilient {cls.title} Handler",
            "difficulty": "Medium",
            "time_limit_mins": 45,
            "description": f"Design and implement a thread-safe worker pipeline that executes the architectural concepts taught in {cls.title}. Handle connection dropouts and buffer overflows.",
            "rubric": "40% Correctness, 30% Concurrency/Thread-safety, 30% Error boundary handling"
        },
        {
            "problem_id": 2,
            "title": f"Edge-Case Simulation: Deadlock & Backpressure Test",
            "difficulty": "Hard",
            "time_limit_mins": 60,
            "description": "Simulate a scenario where the downstream datastore slows to 5000ms latency. Implement exponential backoff retry and circuit breaker logic.",
            "rubric": "50% Zero message loss, 50% Clean failover & alerting"
        }
    ]

    interviews = [
        {
            "question_id": 1,
            "company": "Amazon / Google",
            "question": f"How would you design a high-throughput event processing system applying the principles of {cls.title}?",
            "expected_depth": "Candidate should walk through trade-offs between sync vs async, partition hashing, and consumer rebalancing."
        },
        {
            "question_id": 2,
            "company": "Uber / Swiggy",
            "question": "What happens when your event stream experiences message ordering anomalies across partitions?",
            "expected_depth": "Explain partition key assignment, idempotency keys, and deduplication tables."
        }
    ]

    return AssignmentResponse(
        script_id=script.id,
        class_title=cls.title,
        homework_problems=homework,
        interview_questions=interviews,
        industry_context="Calibrated against 2026 Tier-1 tech company hiring expectations."
    )

# ==============================================================================
# SUBMIT TO MODULE OWNER & SHAREABLE PERMALINK ROUTES
# ==============================================================================

@app.post("/api/scripts/{script_id}/submit-mo")
def submit_script_to_mo(
    script_id: int,
    user_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    """
    SME submits a drafted script to the Module Owner (MO) for formal review and sign-off.
    Transitions script status to 'MO Approval'.
    """
    script = db.query(Script).filter(Script.id == script_id).first()
    if not script:
        raise HTTPException(status_code=404, detail="Script not found")

    actor = _require_script_access(db, user_id, script, write=True)       # Program Managers (read-only), Administrators and people outside the program cannot submit
    if not script.cards or len(script.cards) == 0:
        raise HTTPException(status_code=400, detail="Cannot submit an empty script. Please draft cards first.")
    left = _references_to_check(script)
    if left and script.status != "MO Approval":
        raise HTTPException(status_code=409, detail=f"{len(left)} reference{'s' if len(left) != 1 else ''} the AI found outside the approved list still need{'s' if len(left) == 1 else ''} your check. "
                                                    "Open each card's References, then accept the ones you have checked and remove the rest.")
    # PRD flow: Script WIP, then the AI review, then SME Review, then MO Approval. A script the owner sent back, or one edited after approval, was already reviewed.
    if script.status not in ("SME Review", "MO Approval") and not (script.status == "Script WIP" and script.revision_note):
        raise HTTPException(status_code=409, detail="Run the AI review first. A script goes to the Module Owner after the AI review has checked it and you have read what it found.")

    script.status = "MO Approval"
    actor_name = actor.name

    cls = script.class_entity
    cls_title = cls.title if cls else f"Script #{script.id}"

    log_activity(
        db,
        user_name=actor_name,
        user_role=actor.role,
        action_type="Submitted to MO Review",
        description=f"Submitted script #{script.id} for '{cls_title}' ({len(script.cards)} cards) to Module Owner for official GTG approval"
    )

    # Record script event
    event = ScriptEvent(
        script_id=script.id,
        user_id=user_id,
        user_name=actor_name,
        user_role="sme",
        event_type="Submitted for MO Review"
    )
    db.add(event)
    db.commit()
    db.refresh(script)

    return {
        "status": script.status,
        "message": f"Script #{script.id} successfully submitted to Module Owner for review.",
        "script_id": script.id,
        "cards_count": len(script.cards)
    }


def _render_shared_script_html(detail: ScriptDetailResponse) -> str:
    from agents.safe_html import esc, format_card_text, format_spoken, safe_url, visual_html
    from agents.class_setup import setup_html
    from agents.timeline import clock
    setup_block = setup_html(detail.setup)
    timeline_block = ""                                  # the agenda lives inside the welcome card
    cards_html = []
    for c in detail.cards:
        # Dialogue paragraphs (escaped; only bold, italics and inline code are allowed back in)
        paras_html = format_card_text(c.content_script, c.sources)

        # Stage directions cue
        stage_cue_html = ""
        if c.stage_directions:
            stage_cue_html = f'''
            <div class="stage-cue-box">
                <span class="cue-label">INSTRUCTOR STAGE CUE</span>
                <p>{format_spoken(c.stage_directions)}</p>
            </div>
            '''

        # Code snippet, and what happened when it was run
        code_html = ""
        if c.code_snippet:
            lang = c.code_language or "python"
            run = c.code_run if isinstance(c.code_run, dict) else None
            run_html = ""
            if run:
                status = run.get("status", "skipped")
                label = {"passed": "Ran successfully", "failed": "Failed to run", "timeout": "Took too long", "skipped": "Not run"}.get(status, status)
                output = (run.get("stdout") or "").strip() or (run.get("stderr") or "").strip() or run.get("reason") or ""
                run_html = (f'<div class="run-result run-{esc(status)}"><span class="run-label">{esc(label)}</span>'
                            + (f'<pre class="run-output">{esc(output[:4000])}</pre>' if output else "") + '</div>')
            trace_html = ""
            if run and isinstance(run.get("trace"), dict):
                payload = {**run["trace"], "stdout": run.get("stdout") or "", "status": run.get("status")}
                trace_html = f'<div class="code-trace" data-trace="{esc(json.dumps(payload))}"></div>'
            code_html = f'''
            <div class="code-box">
                <div class="code-box-header">
                    <span>Live Implementation ({esc(lang)})</span>
                    <button class="copy-btn" onclick="copySnippet(this)">Copy Code</button>
                </div>
                <pre><code>{esc(c.code_snippet)}</code></pre>
                {run_html}
                {trace_html}
            </div>
            '''

        # Visuals: diagrams are escaped text, animations run only inside a sandboxed frame, pictures are plain <img>
        vis_html = visual_html(c.visual_type, c.visual_content, c.visual_caption or "Conceptual Diagram", frame_id=str(c.card_index))

        # Quiz: the same view as the script page (options A to D, the right one marked, why each wrong one is wrong)
        from agents.quiz_format import quiz_html
        quiz_html_out = quiz_html(c.quiz) if c.quiz and isinstance(c.quiz, dict) else ""

        # Sources
        sources_html = ""
        if c.sources and isinstance(c.sources, list):
            src_items = []
            for src in c.sources:
                if isinstance(src, dict):
                    name = src.get("source") or "Documentation"
                    claim = src.get("claim", "")
                    url = safe_url(src.get("url"))
                    claim_text = f": {esc(claim)}" if claim else ""
                    label = "official page, checked" if src.get("verified") else ("search for this source, not checked" if src.get("url_kind") == "search" else ("link provided by the SME" if src.get("url_kind") == "provided" else ("link in the card, not checked" if src.get("url_kind") == "link" else "page found, claim not confirmed")))
                    link_html = f'<a href="{esc(url)}" target="_blank" rel="noopener noreferrer">[{label}]</a>' if url != "#" else ""
                    evidence = f'<div class="src-evidence">The page says: &ldquo;{esc(src.get("evidence"))}&rdquo;</div>' if src.get("verified") and src.get("evidence") else ""
                    src_items.append(f'<li><span class="src-name">{"&#10003; " if src.get("verified") else ""}{esc(name)}</span>{claim_text} {link_html}{evidence}</li>')
            if src_items:
                sources_html = f'''
                <div class="sources-box">
                    <span class="sources-label">Official Sources & References</span>
                    <ul>{"".join(src_items)}</ul>
                </div>
                '''

        cards_html.append(f'''
        <article class="card-section" id="card-{int(c.card_index)}">
            <div class="card-section-header">
                <div class="card-title-group">
                    <span class="card-num-badge">#{int(c.card_index)}</span>
                    <h2 class="card-title">{esc(c.title)}</h2>
                </div>
                <div class="card-meta-badges">
                    <span class="badge duration-badge" title="Where this card sits on the class clock">{clock(c.starts_at_sec or 0)} &ndash; {clock(c.ends_at_sec or 0)}</span>
                    <span class="badge duration-badge">{esc(c.duration_mins)} mins</span>
                    <span class="badge phase-badge">{esc(c.pedagogical_phase)}</span>
                </div>
            </div>
            {stage_cue_html}
            <div class="card-body">
                {paras_html}
            </div>
            {vis_html}
            {code_html}
            {quiz_html_out}
            {sources_html}
        </article>
        ''')

    status_cls = "gtg" if detail.status == "GTG" else ("mo-approval" if detail.status == "MO Approval" else "wip")
    status_label = "Official GTG Approved Deck" if detail.status == "GTG" else ("Pending MO Review" if detail.status == "MO Approval" else (detail.status or "Draft WIP"))

    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{esc(detail.class_title)} - Scaler Masterclass Lecture Script</title>
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&family=JetBrains+Mono:wght@400;500;600&display=swap" rel="stylesheet">
    <script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
    <style>
        :root {{
            --bg-main: #0a0e17;
            --bg-card: #111827;
            --bg-card-subtle: #1a2234;
            --border: #1f293d;
            --border-hover: #334155;
            --text-primary: #f8fafc;
            --text-secondary: #94a3b8;
            --text-muted: #64748b;
            --primary: #0284c7;
            --primary-glow: #38bdf8;
            --success: #10b981;
            --warning: #f59e0b;
            --purple: #a855f7;
            --purple-light: #c084fc;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; }}
        body {{
            background: var(--bg-main);
            color: var(--text-primary);
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            line-height: 1.65;
            padding-bottom: 80px;
        }}
        .top-navbar {{
            position: sticky;
            top: 0;
            z-index: 100;
            background: rgba(10, 14, 23, 0.85);
            backdrop-filter: blur(14px);
            border-bottom: 1px solid var(--border);
            padding: 14px 28px;
            display: flex;
            align-items: center;
            justify-content: space-between;
        }}
        .brand-logo {{
            font-size: 0.95rem;
            font-weight: 800;
            letter-spacing: 0.5px;
            display: flex;
            align-items: center;
            gap: 10px;
            color: var(--text-primary);
        }}
        .brand-badge {{
            background: linear-gradient(135deg, #0284c7, #2563eb);
            color: white;
            font-size: 0.72rem;
            font-weight: 700;
            padding: 2px 8px;
            border-radius: 4px;
            text-transform: uppercase;
        }}
        .nav-actions {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}
        .btn {{
            font-family: inherit;
            font-size: 0.84rem;
            font-weight: 600;
            padding: 7px 16px;
            border-radius: 6px;
            cursor: pointer;
            transition: all 0.2s ease;
            border: 1px solid transparent;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .btn-copy {{
            background: var(--bg-card-subtle);
            color: var(--text-primary);
            border-color: var(--border);
        }}
        .btn-copy:hover {{ background: #243048; border-color: var(--primary-glow); }}
        .btn-print {{
            background: transparent;
            color: var(--text-secondary);
            border-color: var(--border);
        }}
        .btn-print:hover {{ color: var(--text-primary); border-color: var(--text-secondary); }}
        .copied-toast {{
            position: fixed;
            bottom: 24px;
            right: 24px;
            background: #10b981;
            color: white;
            font-weight: 600;
            padding: 10px 20px;
            border-radius: 8px;
            box-shadow: 0 10px 25px rgba(0,0,0,0.4);
            opacity: 0;
            transform: translateY(10px);
            transition: all 0.25s ease;
            pointer-events: none;
            z-index: 1000;
        }}
        .copied-toast.show {{ opacity: 1; transform: translateY(0); }}

        .container {{
            max-width: min(1200px, 96vw);
            margin: 0 auto;
            padding: 32px 20px 0 20px;
        }}
        html:fullscreen .container, html:-webkit-full-screen .container {{ max-width: none; padding-left: 5vw; padding-right: 5vw; }}
        html:fullscreen .card-body, html:fullscreen .card-para, html:fullscreen .card-list, html:-webkit-full-screen .card-body {{ font-size: 1.15rem; line-height: 1.85; }}
        .fs-banner {{ display: none; align-items: center; gap: 12px; margin: 0 auto; padding: 10px 20px; background: rgba(56, 189, 248, 0.12); border-bottom: 1px solid rgba(56, 189, 248, 0.4); font-size: 0.92rem; justify-content: center; flex-wrap: wrap; }}
        .fs-banner button {{ cursor: pointer; border-radius: 6px; padding: 5px 14px; font-weight: 700; border: 1px solid #38bdf8; background: #38bdf8; color: #06090e; }}
        .fs-banner .fs-close {{ background: transparent; color: inherit; border-color: transparent; font-size: 1.1rem; padding: 0 6px; }}

        .doc-header {{
            margin-bottom: 36px;
            padding-bottom: 24px;
            border-bottom: 1px solid var(--border);
        }}
        .breadcrumb {{
            font-size: 0.82rem;
            color: var(--primary-glow);
            font-weight: 600;
            margin-bottom: 10px;
            display: flex;
            align-items: center;
            gap: 6px;
            flex-wrap: wrap;
        }}
        .doc-title {{
            font-size: 2.2rem;
            font-weight: 800;
            letter-spacing: -0.5px;
            color: #ffffff;
            margin-bottom: 14px;
            line-height: 1.25;
        }}
        .doc-meta-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 12px;
        }}
        .meta-pills {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
        }}
        .meta-pill {{
            font-size: 0.8rem;
            color: var(--text-secondary);
            background: var(--bg-card);
            border: 1px solid var(--border);
            padding: 4px 12px;
            border-radius: 20px;
        }}
        .status-badge {{
            font-size: 0.82rem;
            font-weight: 700;
            padding: 5px 14px;
            border-radius: 20px;
            display: inline-flex;
            align-items: center;
            gap: 6px;
        }}
        .status-badge.gtg {{
            background: rgba(16, 185, 129, 0.15);
            color: var(--success);
            border: 1px solid rgba(16, 185, 129, 0.35);
        }}
        .status-badge.mo-approval {{
            background: rgba(168, 85, 247, 0.15);
            color: var(--purple-light);
            border: 1px solid rgba(168, 85, 247, 0.35);
        }}
        .status-badge.wip {{
            background: rgba(245, 158, 11, 0.15);
            color: var(--warning);
            border: 1px solid rgba(245, 158, 11, 0.35);
        }}

        .card-section {{
            background: var(--bg-card);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 26px 30px;
            margin-bottom: 24px;
            box-shadow: 0 4px 16px rgba(0, 0, 0, 0.25);
            transition: border-color 0.2s ease;
        }}
        .card-section:hover {{ border-color: var(--border-hover); }}
        .card-section-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            margin-bottom: 18px;
            padding-bottom: 14px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.05);
            flex-wrap: wrap;
            gap: 10px;
        }}
        .card-title-group {{
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .card-num-badge {{
            font-family: 'JetBrains Mono', monospace;
            background: rgba(2, 132, 199, 0.18);
            color: var(--primary-glow);
            border: 1px solid rgba(2, 132, 199, 0.35);
            font-size: 0.8rem;
            font-weight: 700;
            padding: 3px 8px;
            border-radius: 6px;
        }}
        .card-title {{
            font-size: 1.25rem;
            font-weight: 700;
            color: #ffffff;
        }}
        .card-meta-badges {{
            display: flex;
            align-items: center;
            gap: 8px;
        }}
        .badge {{
            font-size: 0.75rem;
            font-weight: 600;
            padding: 3px 10px;
            border-radius: 6px;
        }}
        .duration-badge {{
            background: rgba(148, 163, 184, 0.1);
            color: var(--text-secondary);
            border: 1px solid var(--border);
        }}
        .phase-badge {{
            background: rgba(56, 189, 248, 0.1);
            color: var(--primary-glow);
            border: 1px solid rgba(56, 189, 248, 0.25);
        }}

        .stage-cue-box {{
            background: rgba(168, 85, 247, 0.08);
            border-left: 3px solid var(--purple);
            border-radius: 0 8px 8px 0;
            padding: 12px 16px;
            margin-bottom: 18px;
        }}
        .cue-label {{
            font-size: 0.72rem;
            font-weight: 700;
            color: var(--purple-light);
            text-transform: uppercase;
            letter-spacing: 0.6px;
            display: block;
            margin-bottom: 4px;
        }}
        .stage-cue-box p {{
            font-size: 0.88rem;
            color: #e2e8f0;
            font-style: italic;
        }}

        .card-body {{
            font-size: 0.98rem;
            line-height: 1.75;
            color: #cbd5e1;
        }}
        .dialogue-p {{
            margin-bottom: 16px;
        }}
        .dialogue-p:last-child {{ margin-bottom: 0; }}
        .card-para {{ margin: 0 0 14px; }}
        .card-heading {{ margin: 22px 0 8px; font-size: 1.05rem; font-weight: 700; color: #f1f5f9; }}
        .card-list {{ margin: 0 0 14px; padding-left: 22px; }}
        .card-list li {{ margin: 4px 0; }}
        .stage-cue {{ margin: 0 0 14px; color: #94a3b8; font-style: italic; font-size: 0.9rem; }}
        .reveal {{ font-weight: 600; }}
        .card-callout {{ margin: 0 0 14px; padding: 10px 14px; border-left: 4px solid #38bdf8; border-radius: 0 8px 8px 0; background: rgba(56, 189, 248, 0.08); }}
        .callout-label {{ font-weight: 700; margin-right: 6px; text-transform: uppercase; font-size: 0.74rem; letter-spacing: 0.5px; }}
        .callout-mistake {{ border-color: #ef4444; background: rgba(239, 68, 68, 0.08); }}
        .callout-remember {{ border-color: #a78bfa; background: rgba(167, 139, 250, 0.08); }}
        .callout-realworld {{ border-color: #34d399; background: rgba(52, 211, 153, 0.08); }}
        .callout-predict {{ border-color: #fbbf24; background: rgba(251, 191, 36, 0.08); }}
        .card-link {{ color: #38bdf8; text-decoration: underline; word-break: break-word; }}
        .link-source {{ display: inline-block; margin-left: 4px; padding: 0 6px; border-radius: 4px; background: rgba(148, 163, 184, 0.16); color: #94a3b8; font-size: 0.78em; }}
        .sme-gap {{ background: #fde68a; color: #78350f; padding: 0 5px; border-radius: 4px; font-size: 0.85em; }}

        .code-box {{
            margin: 20px 0;
            border-radius: 8px;
            overflow: hidden;
            background: #06090e;
            border: 1px solid var(--border);
        }}
        .code-box-header {{
            background: #0d121d;
            padding: 8px 14px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            font-size: 0.78rem;
            color: var(--text-muted);
            border-bottom: 1px solid var(--border);
        }}
        .copy-btn {{
            background: transparent;
            border: 1px solid var(--border);
            color: var(--text-secondary);
            border-radius: 4px;
            padding: 2px 8px;
            font-size: 0.72rem;
            cursor: pointer;
        }}
        .copy-btn:hover {{ color: white; border-color: var(--text-muted); }}
        .code-box pre {{
            padding: 16px;
            overflow-x: auto;
            font-family: 'JetBrains Mono', monospace;
            font-size: 0.88rem;
            color: #e2e8f0;
            line-height: 1.55;
        }}

        .visual-wrap {{
            margin: 22px 0;
            padding: 18px;
            background: #0a0e17;
            border: 1px solid var(--border);
            border-radius: 8px;
            text-align: center;
        }}
        .visual-caption {{
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 10px;
        }}
        .visual-wrap .mermaid svg {{ width: 100% !important; max-width: none !important; height: auto !important; min-height: 320px; max-height: 720px; }}
        .visual-wrap iframe.animation-frame {{ width: 100%; min-height: 460px; }}

        .quiz2 {{ margin: 18px 0; padding: 16px; border: 1px solid var(--border); border-radius: 12px; background: rgba(148, 163, 184, 0.06); }}
        .quiz2-head {{ display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }}
        .clock-summary {{ margin: 0 0 22px; padding: 12px 16px; border: 1px solid var(--border); border-radius: 12px; background: rgba(148, 163, 184, 0.07); font-size: 0.92rem; line-height: 1.6; }}
        .setup2 {{ margin: 0 0 28px; padding: 18px; border: 1px solid #f59e0b; border-radius: 14px; background: rgba(245, 158, 11, 0.07); }}
        .setup2-head {{ font-size: 1.15rem; font-weight: 800; margin-bottom: 4px; }}
        .setup2-lead, .setup2-foot {{ font-size: 0.86rem; color: #94a3b8; margin-bottom: 12px; }}
        .setup2-item {{ margin: 10px 0; padding: 12px 14px; border: 1px solid var(--border); border-radius: 10px; background: rgba(148, 163, 184, 0.06); }}
        .setup2-top {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 4px; }}
        .setup2-tool {{ font-weight: 700; font-size: 1rem; }}
        .setup2-chip {{ padding: 1px 8px; border-radius: 10px; font-size: 0.74rem; background: rgba(148, 163, 184, 0.2); }}
        .setup2-chip.ok {{ background: #16a34a; color: #fff; }}
        .setup2-chip.ver {{ background: #2563eb; color: #fff; }}
        .setup2-row {{ margin: 4px 0; font-size: 0.9rem; line-height: 1.5; }}
        .setup2-steps {{ margin: 4px 0 4px 20px; padding: 0; font-size: 0.9rem; line-height: 1.55; }}
        .setup2-link {{ color: #38bdf8; text-decoration: underline; }}
        .setup2 code {{ background: #06090e; color: #e2e8f0; padding: 1px 6px; border-radius: 4px; }}
        .quiz2-actions {{ display: flex; gap: 6px; }}
        .quiz2-label {{ font-size: 0.72rem; font-weight: 800; letter-spacing: 1px; text-transform: uppercase; color: #38bdf8; }}
        .quiz2-toggle {{ border: 1px solid var(--border); background: transparent; color: inherit; border-radius: 6px; padding: 3px 10px; font-size: 0.78rem; cursor: pointer; }}
        .quiz2-q {{ font-size: 1.05rem; font-weight: 600; line-height: 1.55; margin-bottom: 12px; }}
        .quiz2-q .card-para {{ margin: 0 0 8px; }}
        .quiz2-opts {{ display: flex; flex-direction: column; gap: 8px; }}
        .quiz2-opt {{ display: flex; gap: 12px; align-items: flex-start; padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; }}
        .quiz2-letter {{ flex: none; width: 28px; height: 28px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 0.85rem; background: rgba(148, 163, 184, 0.2); }}
        .quiz2-text {{ line-height: 1.5; }}
        .quiz2-body {{ min-width: 0; }}
        .quiz2-ans {{ margin-top: 6px; font-size: 0.86rem; line-height: 1.5; }}
        .quiz2-why {{ color: #94a3b8; }}
        .quiz2-tag {{ display: inline-block; padding: 1px 8px; border-radius: 10px; font-weight: 700; font-size: 0.78rem; background: #16a34a; color: #fff; }}
        .quiz2:not(.hide-answers) .quiz2-opt.right {{ border-color: #16a34a; background: rgba(22, 163, 74, 0.1); }}
        .quiz2:not(.hide-answers) .quiz2-opt.right .quiz2-letter {{ background: #16a34a; color: #fff; }}
        .quiz2-expl {{ margin-top: 12px; padding: 10px 12px; border-left: 4px solid #16a34a; border-radius: 0 8px 8px 0; background: rgba(22, 163, 74, 0.08); }}
        .quiz2.hide-answers .quiz2-ans {{ display: none; }}
        .card-code {{ margin: 8px 0 12px; padding: 10px 12px; border-radius: 8px; background: #06090e; color: #e2e8f0; font: 13px/1.55 ui-monospace, Menlo, Consolas, monospace; overflow: auto; white-space: pre; }}

        .distractor-note {{
            font-size: 0.82rem;
            color: var(--text-muted);
        }}
        .quiz-exp {{
            font-size: 0.85rem;
            color: #cbd5e1;
            padding-top: 10px;
            border-top: 1px dashed rgba(16, 185, 129, 0.25);
        }}

        .sources-box {{
            margin-top: 18px;
            padding: 10px 14px;
            background: rgba(255, 255, 255, 0.02);
            border-left: 3px solid var(--text-muted);
            border-radius: 0 6px 6px 0;
            font-size: 0.82rem;
            color: var(--text-secondary);
        }}
        .sources-label {{
            font-weight: 700;
            color: var(--text-primary);
            display: block;
            margin-bottom: 4px;
        }}
        .sources-box ul {{
            padding-left: 18px;
        }}
        .sources-box a {{
            color: var(--primary-glow);
            text-decoration: none;
            margin-left: 4px;
        }}
        .sources-box a:hover {{ text-decoration: underline; }}

        @media print {{
            body {{ background: white; color: black; }}
            .top-navbar, .nav-actions, .copy-btn {{ display: none !important; }}
            .card-section {{ background: white; color: black; border: 1px solid #ccc; box-shadow: none; page-break-inside: avoid; }}
            .card-body, .dialogue-p {{ color: #222; }}
            .doc-title {{ color: black; }}
        }}
    </style>
</head>
<body>
    <header class="top-navbar">
        <div class="brand-logo">
            <span>SCALER</span>
            <span class="brand-badge">Lecture Studio</span>
        </div>
        <div class="nav-actions">
            <button class="btn btn-copy" id="fsBtn" onclick="toggleFullScreen()" title="Read this script full screen (or press F)" style="display:none;">
                <span id="fsBtnLabel">Full screen</span>
            </button>
            <button class="btn btn-copy" onclick="copyPermalink()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"></path><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"></path></svg>
                <span>Copy Link</span>
            </button>
            <button class="btn btn-print" onclick="window.print()">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 6 2 18 2 18 9"></polyline><path d="M6 18H4a2 2 0 0 1-2-2v-5a2 2 0 0 1 2-2h16a2 2 0 0 1 2 2v5a2 2 0 0 1-2 2h-2"></path><rect x="6" y="14" width="12" height="8"></rect></svg>
                <span>Print / PDF</span>
            </button>
        </div>
    </header>

    <div class="copied-toast" id="copiedToast">Permalink copied to clipboard!</div>

    <div class="fs-banner" id="fsBanner">
        <span>Read this script full screen.</span>
        <button type="button" onclick="toggleFullScreen()">Go full screen</button>
        <span style="opacity:.7;">or press F at any time</span>
        <button type="button" class="fs-close" aria-label="Close this message" onclick="document.getElementById('fsBanner').remove()">&times;</button>
    </div>
    <main class="container">
        <section class="doc-header">
            <div class="breadcrumb">
                <span>{esc(detail.program_name)}</span> &gt;
                <span>{esc(detail.track_name)}</span> &gt;
                <span>{esc(detail.module_name)}</span>
            </div>
            <h1 class="doc-title">{esc(detail.class_title)}</h1>
            <div class="doc-meta-row">
                <div class="meta-pills">
                    <span class="meta-pill">Duration: {detail.duration_minutes} Mins</span>
                    <span class="meta-pill">{detail.cards_count} Spoken Cards</span>
                </div>
                <span class="status-badge {status_cls}">{esc(status_label)}</span>
            </div>
        </section>

        {timeline_block}
        {setup_block}
        <section class="cards-flow">
            {"".join(cards_html)}
        </section>
    </main>

    <script src="/code_trace.js?v=2.0"></script>
    <script>
        if (window.initCodeTrace) initCodeTrace(document);
        // Each animation runs in its own sandboxed frame and reports its height, so it is shown whole.
        window.addEventListener('message', function(ev) {{
            if (!ev.data || ev.data.type !== 'scaler-animation-height') return;
            document.querySelectorAll('iframe.animation-frame').forEach(function(f) {{
                if (f.contentWindow === ev.source) f.style.height = Math.min(Math.max(ev.data.height + 8, 120), 1400) + 'px';
            }});
        }});
        // The diagram library comes from a CDN; the page still works (without diagrams) if it does not load.
        if (window.mermaid) {{ mermaid.initialize({{ startOnLoad: true, theme: 'dark', securityLevel: 'strict', themeVariables: {{ fontSize: '18px' }}, flowchart: {{ useMaxWidth: true, nodeSpacing: 50, rankSpacing: 60, padding: 16 }} }}); }}
        // Full screen. A browser only allows it after a click or a key press, so the page offers a button, a banner and the F key.
        (function () {{
            var root = document.documentElement;
            var supported = !!(root.requestFullscreen || root.webkitRequestFullscreen);
            var active = function () {{ return !!(document.fullscreenElement || document.webkitFullscreenElement); }};
            window.toggleFullScreen = function () {{
                try {{
                    var r = active() ? (document.exitFullscreen || document.webkitExitFullscreen).call(document) : (root.requestFullscreen || root.webkitRequestFullscreen).call(root);
                    if (r && r.catch) r.catch(function () {{}});
                }} catch (e) {{}}
            }};
            var sync = function () {{
                var label = document.getElementById('fsBtnLabel'), banner = document.getElementById('fsBanner');
                if (label) label.textContent = active() ? 'Exit full screen' : 'Full screen';
                if (banner) banner.style.display = (!active() && supported && !sessionStorage.getItem('fsBannerSeen')) ? 'flex' : 'none';
            }};
            if (supported) {{
                document.getElementById('fsBtn').style.display = '';
                document.addEventListener('fullscreenchange', function () {{ sessionStorage.setItem('fsBannerSeen', '1'); sync(); }});
                document.addEventListener('webkitfullscreenchange', function () {{ sessionStorage.setItem('fsBannerSeen', '1'); sync(); }});
                document.addEventListener('keydown', function (ev) {{
                    var t = ev.target;
                    if ((ev.key === 'f' || ev.key === 'F') && !ev.metaKey && !ev.ctrlKey && !ev.altKey && !(t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName)))) {{ ev.preventDefault(); window.toggleFullScreen(); }}
                }});
            }}
            sync();
        }})();
        function toggleQuizAnswers(btn) {{
            var box = btn.closest('.quiz2');
            var hidden = box.classList.toggle('hide-answers');
            btn.textContent = hidden ? 'Show answers' : 'Hide answers';
        }}
        function copyPermalink() {{
            navigator.clipboard.writeText(window.location.href).then(function() {{
                const toast = document.getElementById('copiedToast');
                toast.classList.add('show');
                setTimeout(() => toast.classList.remove('show'), 2200);
            }});
        }}
        function copySnippet(btn) {{
            const code = btn.closest('.code-box').querySelector('code').innerText;
            navigator.clipboard.writeText(code).then(function() {{
                const orig = btn.innerText;
                btn.innerText = 'Copied!';
                setTimeout(() => btn.innerText = orig, 1800);
            }});
        }}
    </script>
</body>
</html>'''


def _render_not_found_html(shareable_uuid: str, message: Optional[str] = None) -> str:
    from agents.safe_html import esc
    return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Link unavailable - Scaler Lecture Studio</title>
    <style>
        body {{ background: #0a0e17; color: #f8fafc; font-family: -apple-system, BlinkMacSystemFont, sans-serif; display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; text-align: center; }}
        .card {{ background: #111827; border: 1px solid #1f293d; padding: 40px; border-radius: 12px; max-width: 480px; box-shadow: 0 10px 25px rgba(0,0,0,0.5); }}
        h1 {{ font-size: 1.6rem; color: #38bdf8; margin-bottom: 12px; }}
        p {{ color: #94a3b8; font-size: 0.95rem; margin-bottom: 24px; line-height: 1.5; }}
        a {{ background: #0284c7; color: white; padding: 10px 20px; border-radius: 6px; text-decoration: none; font-weight: 600; display: inline-block; }}
        a:hover {{ background: #0369a1; }}
    </style>
</head>
<body>
    <div class="card">
        <h1>{"Link Unavailable" if message else "Script Not Found"}</h1>
        <p>{esc(message) if message else "The requested lecture script link could not be found or may have been deleted."}</p>
        <a href="/">Return to Studio Home</a>
    </div>
</body>
</html>'''


@app.get("/script/{shareable_uuid}", response_class=HTMLResponse)
def view_public_script(shareable_uuid: str, db: Session = Depends(get_db)):
    """
    Public read-only masterclass script viewer.
    Renders a responsive, dark-mode permalink document with interactive Mermaid diagrams,
    animations, and quizzes without requiring login.
    """
    script = db.query(Script).filter(Script.shareable_uuid == shareable_uuid).first()
    if not script:
        return HTMLResponse(content=_render_not_found_html(shareable_uuid), status_code=404)
    state = _share_state(script)
    if not state["active"]:
        return HTMLResponse(content=_render_not_found_html(shareable_uuid, _closed_link_message(state)), status_code=410)

    detail = _build_script_detail(script.id, db)
    return HTMLResponse(content=_render_shared_script_html(detail))


# Mount Frontend UI at root (Must be after all API routes)
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "frontend")
if os.path.exists(FRONTEND_DIR):
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8002, reload=True)

