import datetime
import os
import uuid
from typing import List, Optional
from sqlalchemy import (
    Column, Integer, String, Text, Boolean, Float, DateTime, ForeignKey, Table
)
from sqlalchemy.orm import relationship
from database import Base

user_programs = Table(
    "user_programs",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("program_id", Integer, ForeignKey("programs.id", ondelete="CASCADE"), primary_key=True)
)

user_tracks = Table(
    "user_tracks",
    Base.metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("track_id", Integer, ForeignKey("tracks.id", ondelete="CASCADE"), primary_key=True)
)

class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), nullable=False)
    email = Column(String(255), unique=True, nullable=False)
    role = Column(String(50), nullable=False)  # "sme", "mo", "pm", "admin"
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    programs = relationship("Program", secondary=user_programs, back_populates="users")
    tracks = relationship("Track", secondary=user_tracks, back_populates="users")

class Program(Base):
    __tablename__ = "programs"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(255), unique=True, nullable=False)
    slug = Column(String(100), unique=True, nullable=False)
    description = Column(Text, nullable=True)
    tat_target_days = Column(Integer, nullable=True)          # the program's target for script TAT, in days from module kickoff to approval

    tracks = relationship("Track", back_populates="program", cascade="all, delete-orphan")
    users = relationship("User", secondary=user_programs, back_populates="programs")

class Track(Base):
    __tablename__ = "tracks"

    id = Column(Integer, primary_key=True, index=True)
    program_id = Column(Integer, ForeignKey("programs.id"), nullable=False)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False)
    # True once the track's modules and classes were synced from an official mapping file; such
    # tracks list their own modules from the database instead of the built-in per-program ID sets.
    mapped = Column(Boolean, default=False)

    program = relationship("Program", back_populates="tracks")
    modules = relationship("Module", back_populates="track", cascade="all, delete-orphan")
    users = relationship("User", secondary=user_tracks, back_populates="tracks")

class Module(Base):
    __tablename__ = "modules"

    id = Column(Integer, primary_key=True, index=True)
    track_id = Column(Integer, ForeignKey("tracks.id"), nullable=False)
    name = Column(String(255), nullable=False)
    slug = Column(String(100), nullable=False)
    sequence_order = Column(Integer, default=1)
    is_locked = Column(Boolean, default=True)
    module_type = Column(String(50), default="Core")
    approved_sources = Column(Text, nullable=True)
    kickoff_at = Column(DateTime, nullable=True)              # when work on this module started: the start of script TAT
    # FR-3 / FR-4 / FR-6: plan provenance, review lifecycle, and ownership
    plan_source = Column(String(20), default="manual")      # manual | syllabus | both
    plan_status = Column(String(20), default="Locked")      # Draft | In Review | Locked
    owner_name = Column(String(255), nullable=True)          # assigned SME or Module Owner
    segment = Column(String(100), nullable=True)             # sub-track label, e.g. DSA, Backend, Full Stack
    duration_months = Column(Float, nullable=True)           # official module length; overrides the built-in label

    track = relationship("Track", back_populates="modules")
    classes = relationship("Class", back_populates="module", cascade="all, delete-orphan")
    memory_ledgers = relationship("MemoryLedger", back_populates="module", cascade="all, delete-orphan")

class Class(Base):
    __tablename__ = "classes"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False)
    sequence_number = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    duration_minutes = Column(Integer, default=90)
    agendas = Column(Text, nullable=False)
    learning_outcomes = Column(Text, nullable=True)
    class_type = Column(String(50), default="Live")
    module_type = Column(String(50), default="Core")
    content_change_pct = Column(Integer, default=0)
    junction_number = Column(Integer, nullable=True)
    content_change_description = Column(Text, nullable=True)   # what changed, when content_change_pct > 0 (FR-18)
    reference_script = Column(Text, nullable=True)             # a previous script of this class attached by the Module Owner, used as reference by the AI draft (FR-19)
    reference_script_name = Column(String(255), nullable=True)

    module = relationship("Module", back_populates="classes")
    scripts = relationship("Script", back_populates="class_entity", cascade="all, delete-orphan")
    memory_ledgers = relationship("MemoryLedger", back_populates="class_entity", cascade="all, delete-orphan")

def default_share_expiry():
    """When a new share link stops working: SHARE_LINK_DAYS from now (default 30). 0 means never."""
    try:
        days = int(os.getenv("SHARE_LINK_DAYS", "30"))
    except ValueError:
        days = 30
    return datetime.datetime.utcnow() + datetime.timedelta(days=days) if days > 0 else None


class Script(Base):
    __tablename__ = "scripts"

    id = Column(Integer, primary_key=True, index=True)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    shareable_uuid = Column(String(64), unique=True, default=lambda: str(uuid.uuid4()))
    share_expires_at = Column(DateTime, nullable=True, default=lambda: default_share_expiry())   # None = never expires
    share_revoked_at = Column(DateTime, nullable=True)                                           # set when the link is switched off
    setup_json = Column(Text, nullable=True)                                                     # the before-the-class setup checklist of a programming class
    status = Column(String(50), default="Not Started")
    duration_minutes = Column(Integer, default=90)
    content_mix_beginner = Column(Integer, default=70)
    content_mix_advanced = Column(Integer, default=30)
    code_ratio = Column(Integer, default=20)
    additional_resources = Column(Text, nullable=True)
    learning_outcomes = Column(Text, nullable=True)       # what THIS script's SME wants learners able to do, kept with the script (the class keeps the Module Owner's)
    api_docs = Column(Text, nullable=True)
    reference_urls = Column(Text, nullable=True)
    previous_script_context = Column(Text, nullable=True)
    outline_json = Column(Text, nullable=True)
    outline_approved = Column(Boolean, default=False)
    custom_agendas = Column(Text, nullable=True)
    custom_class_title = Column(String(255), nullable=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    sme_name = Column(String(255), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approved_by_name = Column(String(255), nullable=True)
    revision_note = Column(Text, nullable=True)           # why the Module Owner did not approve it, and what to change; cleared when the author resubmits
    revision_by = Column(String(255), nullable=True)
    revision_at = Column(DateTime, nullable=True)
    review_lenses = Column(Text, nullable=True)           # the review lenses the SME chose when making the script (JSON list); empty means all
    review_json = Column(Text, nullable=True)             # the latest AI review: score, whether it passed, notes and every lens's findings
    review_at = Column(DateTime, nullable=True)
    review_passed = Column(Boolean, nullable=True)
    review_score = Column(Float, nullable=True)
    regenerate_pending = Column(Boolean, default=False)   # a revision was started: the written cards are replaced when the new outline is approved
    created_at = Column(DateTime, default=datetime.datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.datetime.utcnow, onupdate=datetime.datetime.utcnow)

    user = relationship("User")
    class_entity = relationship("Class", back_populates="scripts")
    cards = relationship("ScriptCard", back_populates="script", cascade="all, delete-orphan")
    audits = relationship("ReviewAudit", back_populates="script", cascade="all, delete-orphan")
    events = relationship("ScriptEvent", back_populates="script", cascade="all, delete-orphan")

class ScriptCard(Base):
    __tablename__ = "script_cards"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False)
    card_index = Column(Integer, nullable=False)
    title = Column(String(255), nullable=False)
    duration_mins = Column(Float, nullable=False)
    target_words = Column(Integer, default=450)
    pedagogical_phase = Column(String(50), default="Concept")
    stage_directions = Column(Text, nullable=True)
    content_script = Column(Text, nullable=False)
    code_snippet = Column(Text, nullable=True)
    code_language = Column(String(50), nullable=True)
    quiz_json = Column(Text, nullable=True)
    visual_asset_json = Column(Text, nullable=True)
    sources_json = Column(Text, nullable=True)      # [{claim, source, url, url_kind, verified}]
    code_run_json = Column(Text, nullable=True)     # what happened when the card's code was run
    is_approved = Column(Boolean, default=False)

    script = relationship("Script", back_populates="cards")
    visual_assets = relationship("VisualAsset", back_populates="card", cascade="all, delete-orphan")

class MemoryLedger(Base):
    __tablename__ = "memory_ledgers"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(Integer, ForeignKey("modules.id"), nullable=False)
    class_id = Column(Integer, ForeignKey("classes.id"), nullable=False)
    terms_mastered = Column(Text, nullable=True)
    code_abstractions = Column(Text, nullable=True)
    anchor_analogy = Column(String(255), nullable=True)
    cliffhanger = Column(Text, nullable=True)
    summary_json = Column(Text, nullable=True)       # what the approved script taught, in order (see agents/class_memory.py)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    module = relationship("Module", back_populates="memory_ledgers")
    class_entity = relationship("Class", back_populates="memory_ledgers")

class ReviewAudit(Base):
    __tablename__ = "review_audits"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("scripts.id"), nullable=False)
    lens_name = Column(String(100), nullable=False)
    reviewer_persona = Column(String(50), nullable=False)
    findings_json = Column(Text, nullable=False)
    score = Column(Float, default=10.0)
    passed = Column(Boolean, default=True)
    retry_iteration = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

    script = relationship("Script", back_populates="audits")

class VisualAsset(Base):
    __tablename__ = "visual_assets"

    id = Column(Integer, primary_key=True, index=True)
    card_id = Column(Integer, ForeignKey("script_cards.id"), nullable=False)
    asset_type = Column(String(50), nullable=False)
    content_data = Column(Text, nullable=False)
    caption = Column(String(255), nullable=True)
    figure_label = Column(String(50), nullable=True)

    card = relationship("ScriptCard", back_populates="visual_assets")

class UserActivity(Base):
    __tablename__ = "user_activities"

    id = Column(Integer, primary_key=True, index=True)
    user_name = Column(String(255), nullable=False)
    user_role = Column(String(50), nullable=True)
    action_type = Column(String(100), nullable=False) # e.g. "Script Initialized", "GTG Approved", "Class Created", "Login"
    description = Column(Text, nullable=False)
    details = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.datetime.utcnow)

class ScriptEvent(Base):
    """One human action on a script. Feeds the TAT and SME/instructor effort metrics."""
    __tablename__ = "script_events"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(Integer, nullable=True)
    user_name = Column(String(255), nullable=True)
    user_role = Column(String(50), nullable=True)
    event_type = Column(String(60), nullable=False)
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    script = relationship("Script", back_populates="events")


class APIUsageLog(Base):
    """Tracks token usage, latency, and estimated cost for every LLM / OpenRouter invocation."""
    __tablename__ = "api_usage_logs"

    id = Column(Integer, primary_key=True, index=True)
    script_id = Column(Integer, ForeignKey("scripts.id", ondelete="SET NULL"), nullable=True, index=True)
    provider = Column(String(50), nullable=False)        # "openrouter", "anthropic", "gemini"
    model = Column(String(100), nullable=False)
    agent = Column(String(100), default="general")      # "outliner", "writer", "reviewer", etc.
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    total_tokens = Column(Integer, default=0)
    estimated_cost_usd = Column(Float, default=0.0)
    latency_seconds = Column(Float, default=0.0)
    status = Column(String(50), default="success")      # "success", "error"
    cost_source = Column(String(20), default="table")   # "provider" (the provider said), "table" (our price list), "unknown" (no price known)
    error_message = Column(String(300), nullable=True)  # why a failed call failed
    created_at = Column(DateTime, default=datetime.datetime.utcnow, index=True)

    script = relationship("Script", foreign_keys=[script_id])




