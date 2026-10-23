"""
Sign-in with Google through Firebase Authentication, for Scaler accounts only, and the request-access flow.

AUTH_MODE=google switches it on. Without it the app keeps its older behaviour (sign in by typing an email), which is only meant for local work.

How it works when it is on:
  * the browser signs in with Google through Firebase and gets an ID token; /api/auth/google has Firebase check it, checks the email ends in @scaler.com,
    and sets a signed cookie
  * every other /api request needs that cookie. The user id comes from the cookie, never from the browser: a middleware replaces any user_id / actor_id
    in the query with the signed-in person's own id, so the routes keep working unchanged and nobody can act as someone else
  * a person seen for the first time has no access yet. They pick a role (and programs), an administrator approves or rejects, and only then do the
    rest of the routes open for them
  * emails in ADMIN_EMAILS become administrators on their first sign-in, so there is always someone who can approve
"""

import base64
import contextvars
import datetime
import hashlib
import hmac
import json
import os
import time
from typing import List, Optional
from urllib.parse import parse_qsl, urlencode

import requests
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.responses import JSONResponse

from database import SyncSessionLocal, get_db
from models import Program, User

COOKIE = "scaler_session"
SESSION_HOURS = 8
REQUESTABLE_ROLES = ("sme", "mo", "pm")

current_user_id: contextvars.ContextVar = contextvars.ContextVar("current_user_id", default=None)

router = APIRouter(prefix="/api/auth", tags=["auth"])


# ---- settings, read when used so a change in the environment takes effect

def mode() -> str:
    return os.getenv("AUTH_MODE", "demo").strip().lower()


def enforced() -> bool:
    return mode() == "google"


def allowed_domain() -> str:
    return (os.getenv("ALLOWED_EMAIL_DOMAIN") or "scaler.com").strip().lower().lstrip("@")


def admin_emails() -> set:
    return {e.strip().lower() for e in (os.getenv("ADMIN_EMAILS") or "").split(",") if e.strip()}


def _secret() -> bytes:
    s = os.getenv("SESSION_SECRET", "")
    if len(s) < 16:
        raise HTTPException(status_code=500, detail="SESSION_SECRET is not set (use at least 16 random characters). Sign-in is switched off until it is.")
    return s.encode()


# ---- the signed session

def make_session(user_id: int, hours: float = SESSION_HOURS) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"u": int(user_id), "e": int(time.time() + hours * 3600)}).encode()).decode().rstrip("=")
    sig = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def read_session(token: Optional[str]) -> Optional[int]:
    """The user id in a session cookie, or None when it is missing, altered or expired."""
    if not token or "." not in token:
        return None
    body, _, sig = token.rpartition(".")
    try:
        good = hmac.new(_secret(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, good):
            return None
        data = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        return int(data["u"]) if data.get("e", 0) > time.time() else None
    except (ValueError, KeyError, TypeError, HTTPException):
        return None


# ---- Firebase (Google sign-in)

def firebase_web_config() -> dict:
    """What the page needs to start Firebase. These values are public by design (they are in every Firebase web page)."""
    return {"apiKey": (os.getenv("FIREBASE_API_KEY") or "").strip(), "authDomain": (os.getenv("FIREBASE_AUTH_DOMAIN") or "").strip(),
            "projectId": (os.getenv("FIREBASE_PROJECT_ID") or "").strip(), "appId": (os.getenv("FIREBASE_APP_ID") or "").strip()}


def verify_sign_in(id_token: str) -> dict:
    """Ask Firebase whether this ID token is real, current and for this project, and who it belongs to. Returns {"email", "name"}.
    Firebase checks the signature and the expiry; only a token made for this project's web key is accepted."""
    key = firebase_web_config()["apiKey"]
    if not key:
        raise HTTPException(status_code=500, detail="FIREBASE_API_KEY is not set, so sign-in is not available.")
    try:
        r = requests.post("https://identitytoolkit.googleapis.com/v1/accounts:lookup", params={"key": key}, json={"idToken": id_token}, timeout=10)
    except requests.RequestException:
        raise HTTPException(status_code=503, detail="Could not reach Firebase to check the sign-in. Try again.")
    if r.status_code != 200:
        raise HTTPException(status_code=401, detail="Firebase did not accept this sign-in. Try again.")
    users = r.json().get("users") or []
    u = users[0] if users else {}
    if not u.get("email") or not u.get("emailVerified"):
        raise HTTPException(status_code=401, detail="This email address has not been verified.")
    if not any(p.get("providerId") == "google.com" for p in u.get("providerUserInfo") or []):
        raise HTTPException(status_code=401, detail="Sign in with your Google account.")
    return {"email": str(u["email"]).strip().lower(), "name": u.get("displayName") or ""}


# ---- the gate: runs before every /api route

OPEN_PREFIXES = ("/api/health", "/api/auth/config", "/api/auth/google", "/api/auth/login", "/api/scripts/share/")
UNAPPROVED_OK = ("/api/auth/me", "/api/auth/logout", "/api/auth/request-access", "/api/auth/programs")


async def auth_gate(request: Request, call_next):
    path = request.url.path
    if not enforced() or not path.startswith("/api/") or request.method == "OPTIONS" or any(path.startswith(p) for p in OPEN_PREFIXES):
        return await call_next(request)
    try:
        uid = read_session(request.cookies.get(COOKIE))
    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"detail": e.detail})
    db = SyncSessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first() if uid else None
        state = (user.status or "active") if user else None
    finally:
        db.close()
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Sign in required."})
    if state != "active" and not any(path.startswith(p) for p in UNAPPROVED_OK):
        return JSONResponse(status_code=403, content={"detail": "Your access has not been approved yet.", "status": state})
    # the routes read "who is asking" from user_id / actor_id: make sure that is the signed-in person and nobody else
    keep = [(k, v) for k, v in parse_qsl(request.scope.get("query_string", b"").decode(), keep_blank_values=True) if k not in ("user_id", "actor_id", "for_user")]
    asked_for_user = any(k == "for_user" for k, _ in parse_qsl(request.scope.get("query_string", b"").decode(), keep_blank_values=True))
    keep += [("user_id", str(uid)), ("actor_id", str(uid))] + ([("for_user", str(uid))] if asked_for_user else [])
    request.scope["query_string"] = urlencode(keep).encode()
    request.scope["headers"] = [(k, v) for k, v in request.scope.get("headers", []) if k != b"x-actor-id"] + [(b"x-actor-id", str(uid).encode())]
    token = current_user_id.set(uid)
    try:
        return await call_next(request)
    finally:
        current_user_id.reset(token)


# ---- routes

class GoogleBody(BaseModel):
    credential: str


class AccessRequestBody(BaseModel):
    role: str
    program_ids: List[int] = []


class DecisionBody(BaseModel):
    role: Optional[str] = None
    program_ids: Optional[List[int]] = None
    reason: Optional[str] = None


def _me(db: Session) -> User:
    uid = current_user_id.get()
    user = db.query(User).filter(User.id == uid).first() if uid else None
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required.")
    return user


def _admin(db: Session) -> User:
    user = _me(db)
    if user.role != "admin" or (user.status or "active") != "active":
        raise HTTPException(status_code=403, detail="Only an administrator can do this.")
    return user


def _out(user: User) -> dict:
    import main
    data = main._user_out(user).model_dump()
    data["status"] = user.status or "active"
    data["requested_role"] = user.requested_role
    ids = json.loads(user.requested_program_ids or "[]")
    data["requested_program_ids"] = ids
    data["requested_program_names"] = [p.name for p in main_programs(ids)] if ids else []
    return data


def main_programs(ids: List[int]) -> List[Program]:
    db = SyncSessionLocal()
    try:
        by_id = {p.id: p for p in db.query(Program).filter(Program.id.in_(ids)).all()}
        return [by_id[i] for i in ids if i in by_id]
    finally:
        db.close()


def _log(db: Session, who: str, role: Optional[str], action: str, text: str):
    import main
    main.log_activity(db, user_name=who, user_role=role, action_type=action, description=text)


@router.get("/config")
def auth_config():
    return {"mode": mode(), "firebase": firebase_web_config() if enforced() else None, "domain": allowed_domain()}


@router.post("/google")
def google_sign_in(body: GoogleBody, request: Request, response: Response, db: Session = Depends(get_db)):
    if not enforced():
        raise HTTPException(status_code=400, detail="Google sign-in is not switched on.")
    _secret()                                                       # fail clearly before anything is created
    info = verify_sign_in(body.credential)
    email = info["email"]
    if not email.endswith("@" + allowed_domain()):
        raise HTTPException(status_code=403, detail=f"Only @{allowed_domain()} accounts can sign in.")
    user = db.query(User).filter(func.lower(User.email) == email).first()
    if user is None:
        user = User(name=info["name"] or email.split("@")[0], email=email, role="sme", status="new")
        db.add(user)
        db.commit()
        db.refresh(user)
    if email in admin_emails() and (user.role != "admin" or (user.status or "active") != "active"):
        user.role, user.status = "admin", "active"
        db.commit()
        db.refresh(user)
    secure = request.headers.get("x-forwarded-proto", "").startswith("https") or request.url.scheme == "https"
    response.set_cookie(COOKIE, make_session(user.id), max_age=SESSION_HOURS * 3600, httponly=True, samesite="lax", secure=secure, path="/")
    return {"user": _out(user), "status": user.status or "active"}


@router.get("/me")
def whoami(db: Session = Depends(get_db)):
    if not enforced():
        raise HTTPException(status_code=400, detail="Google sign-in is not switched on.")
    return {"user": _out(_me(db)), "status": _me(db).status or "active"}


@router.post("/logout")
def sign_out(response: Response):
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/programs")
def programs_for_request(db: Session = Depends(get_db)):
    """Every program, so a person can say which ones they need. Only names, nothing else."""
    _me(db)
    return [{"id": p.id, "name": p.name} for p in db.query(Program).order_by(Program.id).all()]


@router.post("/request-access")
def request_access(body: AccessRequestBody, db: Session = Depends(get_db)):
    user = _me(db)
    if (user.status or "active") not in ("new", "rejected"):
        raise HTTPException(status_code=409, detail="You already have access or a request is waiting.")
    if body.role not in REQUESTABLE_ROLES:
        raise HTTPException(status_code=400, detail="Choose Author, Module Owner or Program Manager.")
    ids = sorted({int(i) for i in body.program_ids})
    found = {p.id for p in db.query(Program).filter(Program.id.in_(ids)).all()} if ids else set()
    if found != set(ids):
        raise HTTPException(status_code=400, detail="One of the programs does not exist.")
    if body.role in ("sme", "mo") and not ids:
        raise HTTPException(status_code=400, detail="Choose at least one program.")
    user.requested_role = body.role
    user.requested_program_ids = json.dumps(ids)
    user.requested_at = datetime.datetime.utcnow()
    user.status = "pending"
    user.reject_reason = None
    db.commit()
    _log(db, user.name, None, "Access Requested", f"{user.name} ({user.email}) asked for the {body.role.upper()} role")
    return {"status": "pending", "user": _out(user)}


def _request_row(db: Session, u: User) -> dict:
    ids = json.loads(u.requested_program_ids or "[]")
    names = {p.id: p.name for p in db.query(Program).filter(Program.id.in_(ids)).all()} if ids else {}
    return {"id": u.id, "name": u.name, "email": u.email, "requested_role": u.requested_role, "program_ids": ids,
            "program_names": [names[i] for i in ids if i in names], "requested_at": u.requested_at.isoformat() if u.requested_at else None}


@router.get("/access-requests")
def list_access_requests(db: Session = Depends(get_db)):
    _admin(db)
    rows = db.query(User).filter(User.status == "pending").order_by(User.requested_at).all()
    return [_request_row(db, u) for u in rows]


def _pending_user(db: Session, user_id: int) -> User:
    u = db.query(User).filter(User.id == user_id).first()
    if not u or u.status != "pending":
        raise HTTPException(status_code=404, detail="There is no waiting request for this person.")
    return u


@router.post("/access-requests/{user_id}/approve")
def approve_request(user_id: int, body: DecisionBody, db: Session = Depends(get_db)):
    admin = _admin(db)
    u = _pending_user(db, user_id)
    role = body.role or u.requested_role
    if role not in REQUESTABLE_ROLES:
        raise HTTPException(status_code=400, detail="Choose Author, Module Owner or Program Manager.")
    ids = body.program_ids if body.program_ids is not None else json.loads(u.requested_program_ids or "[]")
    if role in ("sme", "mo") and not ids:
        raise HTTPException(status_code=400, detail="Choose at least one program.")
    import main
    u.role, u.status = role, "active"
    main._apply_memberships(db, u, list(ids), [])
    u.decided_by, u.decided_at, u.reject_reason = admin.name, datetime.datetime.utcnow(), None
    db.commit()
    _log(db, admin.name, "admin", "Access Approved", f"Approved {u.name} ({u.email}) as {role.upper()} with {len(u.programs)} program(s)")
    return {"ok": True, "user": _out(u)}


@router.post("/access-requests/{user_id}/reject")
def reject_request(user_id: int, body: DecisionBody, db: Session = Depends(get_db)):
    admin = _admin(db)
    u = _pending_user(db, user_id)
    u.status, u.decided_by, u.decided_at = "rejected", admin.name, datetime.datetime.utcnow()
    u.reject_reason = (body.reason or "").strip()[:300] or None
    db.commit()
    _log(db, admin.name, "admin", "Access Rejected", f"Rejected the access request of {u.name} ({u.email})")
    return {"ok": True}
