import datetime
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

# ==============================================================================
# CURRICULUM & CASCADING SCHEMAS
# ==============================================================================

class ProgramOut(BaseModel):
    id: int
    name: str
    slug: str
    description: Optional[str] = None

    class Config:
        from_attributes = True

class TrackOut(BaseModel):
    id: int
    program_id: int
    name: str
    slug: str

    class Config:
        from_attributes = True

class ModuleOut(BaseModel):
    id: int
    track_id: int
    name: str
    slug: str
    sequence_order: Optional[int] = 1
    is_locked: bool
    module_type: str
    segment: Optional[str] = None
    class_count: Optional[int] = None
    class_type_counts: Optional[Dict[str, int]] = None
    duration_label: Optional[str] = None

    class Config:
        from_attributes = True

class ClassOut(BaseModel):
    id: int
    module_id: int
    sequence_number: int
    title: str
    duration_minutes: int
    agendas: List[str]
    learning_outcomes: Optional[str] = None
    class_type: str
    module_type: str
    junction_number: Optional[int] = None
    content_change_pct: Optional[int] = 0
    content_change_description: Optional[str] = None

    class Config:
        from_attributes = True

class ClassUpdate(BaseModel):
    title: Optional[str] = None
    duration_minutes: Optional[int] = None
    agendas: Optional[List[str]] = None
    learning_outcomes: Optional[str] = None
    class_type: Optional[str] = None
    module_type: Optional[str] = None
    content_change_pct: Optional[int] = Field(None, ge=0, le=100)
    content_change_description: Optional[str] = None
    sequence_number: Optional[int] = None
    module_id: Optional[int] = None
    reference_script: Optional[str] = Field(None, max_length=200000)       # a previous script of this class, as text; an empty string removes it
    reference_script_name: Optional[str] = None

class ClassCreate(BaseModel):
    module_id: int
    title: str
    sequence_number: int
    duration_minutes: int = 90
    agendas: List[str]
    learning_outcomes: Optional[str] = None
    class_type: str = "Live"
    module_type: str = "Core"
    content_change_pct: int = Field(0, ge=0, le=100)
    content_change_description: Optional[str] = None
    reference_script: Optional[str] = Field(None, max_length=200000)
    reference_script_name: Optional[str] = None

class ProgramCreate(BaseModel):
    name: str
    slug: Optional[str] = None
    description: Optional[str] = None

class ProgramUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None
    description: Optional[str] = None
    tat_target_days: Optional[int] = Field(None, ge=0, le=3650)      # 0 clears the target

class TrackCreate(BaseModel):
    program_id: int
    name: str
    slug: Optional[str] = None

class TrackUpdate(BaseModel):
    name: Optional[str] = None
    slug: Optional[str] = None

class ModuleCreate(BaseModel):
    track_id: int
    name: str
    slug: Optional[str] = None
    sequence_order: Optional[int] = 1
    module_type: str = "Core"
    is_locked: bool = True
    approved_sources: Optional[str] = None
    owner_name: Optional[str] = None
    segment: Optional[str] = Field(None, max_length=100)
    duration_months: Optional[float] = Field(None, gt=0, le=60)

class ModuleUpdate(BaseModel):
    name: Optional[str] = None
    module_type: Optional[str] = None
    is_locked: Optional[bool] = None
    approved_sources: Optional[str] = None
    owner_name: Optional[str] = None
    kickoff_date: Optional[str] = None                                # YYYY-MM-DD; an empty text clears it
    track_id: Optional[int] = None
    sequence_order: Optional[int] = None
    segment: Optional[str] = Field(None, max_length=100)     # Admin only
    duration_months: Optional[float] = Field(None, gt=0, le=60)   # Admin only

# ==============================================================================
# SCRIPT CREATION (THE 10 CANONICAL INPUTS)
# ==============================================================================

class ScriptCreateRequest(BaseModel):
    class_id: int = Field(..., description="Selected Class ID from locked module")
    duration_minutes: int = Field(90, description="Target total duration in minutes (e.g. 60, 90, 105)")
    content_mix_beginner: int = Field(70, description="Percentage of beginner-focused intuitive content (0-100)")
    content_mix_advanced: int = Field(30, description="Percentage of advanced deep-dive content (0-100)")
    code_ratio: int = Field(20, description="Percentage of cards requiring code (0-100)")
    additional_resources: Optional[str] = Field(None, description="SME custom directives, batch context, war stories")
    api_docs: Optional[str] = Field(None, description="Official documentation links and versions (e.g. Spring Boot 3.2)")
    reference_urls: Optional[str] = Field(None, description="Supplemental reference URLs and starter links")
    previous_script_context: Optional[str] = Field(None, description="Uploaded or manually pasted prior class script")
    custom_class_title: Optional[str] = Field(None, description="Optional custom or refined class title by SME")
    custom_agendas: Optional[List[str]] = Field(None, description="Optional customized class agendas provided by SME")
    learning_outcomes: Optional[str] = Field(None, description="Learning outcomes provided by SME if not set by MO")
    review_lenses: Optional[List[str]] = Field(None, description="The review lenses the SME chose; empty means all four")

# ==============================================================================
# CARD & OUTLINE SCHEMAS
# ==============================================================================

class OutlineCardItem(BaseModel):
    card_index: int
    title: str
    duration_mins: float
    pedagogical_phase: str # "Instructor Note", "Agenda", "Everyday Analogy", "Concept", "Failure Mode", "Code Along", "Quiz", "Recap"
    key_concept: str
    visual_type: Optional[str] = "mermaid" # "mermaid", "image", "animation", "none"
    level: Optional[str] = None            # "beginner", "bridge" or "advanced": planned from the SME's beginner/advanced split
    has_code: Optional[bool] = None        # planned from the SME's code ratio: does this card carry code?

class OutlineSectionOut(BaseModel):
    section_id: str
    goal: str
    cards: List[Dict[str, Any]]

class OutlineResponse(BaseModel):
    script_id: int
    class_title: str
    total_duration_mins: float
    total_cards: int
    cards: List[OutlineCardItem]
    sections: Optional[List[Dict[str, Any]]] = None
    running_example: Optional[str] = None
    assumptions: Optional[List[str]] = None
    visual_cards: Optional[List[Dict[str, Any]]] = None
    checks: Optional[List[Dict[str, Any]]] = None
    plan_summary: Optional[Dict[str, Any]] = None      # the SME's beginner/advanced split and code ratio, as planned
    continuity: Optional[Dict[str, Any]] = None        # the class before this one (and whether its script is approved) and the upcoming classes not to teach
    overlap_analysis: Optional[str] = "No overlap detected with previous/subsequent classes in module."
    overlap_flags: Optional[List[Dict[str, Any]]] = None   # cards that plan something an earlier or a later class of the module teaches (FR-10)

class OutlineApprovalRequest(BaseModel):
    action: str = Field(..., description="'GTG' to lock outline, 'Edit' to submit modifications, 'ReThink' to regenerate")
    modified_cards: Optional[List[OutlineCardItem]] = None
    modified_sections: Optional[List[Dict[str, Any]]] = None
    revision_feedback: Optional[str] = None

class OutlineCardAssistRequest(BaseModel):
    card_id: Optional[str] = None
    card_index: Optional[int] = None
    user_prompt: str = Field(..., description="SME instruction, e.g. 'Change analogy to UPI QR codes' or 'Increase duration to 8m'")

class QuizData(BaseModel):
    question: str
    options: List[str]
    correct_option_index: int
    explanation: str
    distractor_explanations: Dict[str, str]

class ScriptCardOut(BaseModel):
    id: int
    card_index: int
    title: str
    duration_mins: float
    target_words: int
    pedagogical_phase: str
    stage_directions: Optional[str] = None
    content_script: str
    code_snippet: Optional[str] = None
    code_language: Optional[str] = None
    quiz: Optional[Dict[str, Any]] = None
    visual_type: Optional[str] = None
    visual_content: Optional[str] = None # Mermaid syntax, Image URL or HTML animation
    visual_caption: Optional[str] = None
    visual_id: Optional[str] = None
    visual_srcdoc: Optional[str] = None   # animations: a complete, sandbox-ready page (agents/safe_html.py)
    visual_approved: bool = False
    visual_stale: bool = False            # the prompt changed after this visual was made
    code_run: Optional[Dict[str, Any]] = None   # what happened when the code was run
    visual_source: Optional[str] = None   # gemini | claude | placeholder
    visual_note: Optional[str] = None     # why a placeholder was used
    sources: Optional[List[Dict[str, Any]]] = None
    is_approved: bool = False
    starts_at_sec: Optional[float] = None      # where this card starts on the class clock, counted from the first word of the class
    ends_at_sec: Optional[float] = None
    spoken_words: Optional[int] = None         # the words the instructor actually says (code, cues and markers excluded)
    word_budget: Optional[int] = None          # the words this card's minutes can hold
    words_max: Optional[int] = None            # the most it may hold (15% over)

    class Config:
        from_attributes = True

# ==============================================================================
# REVIEW & MODERATOR AUDIT SCHEMAS
# ==============================================================================

class ReviewFinding(BaseModel):
    lens_name: str # "Technical Accuracy", "Learner Clarity", "Syllabus Coverage"
    reviewer_persona: str # "Beginner", "Advanced", "SOP Linter"
    score: float # 0.0 - 10.0
    passed: bool
    summary: str
    detailed_findings: List[str]
    ran: Optional[bool] = True
    findings: Optional[List[Dict[str, Any]]] = None

class MultiAgentReviewResponse(BaseModel):
    script_id: int
    overall_score: float
    is_gtg: bool
    retry_count: int
    reviews: List[ReviewFinding]
    moderator_notes: Optional[str] = None

# ==============================================================================
# SCRIPT METADATA & STATUS
# ==============================================================================

class SourceReviewRequest(BaseModel):
    url: str
    decision: str          # accept | remove


class RequestChangesBody(BaseModel):
    reason: str


class ScriptDetailResponse(BaseModel):
    id: int
    class_id: int
    class_title: str
    module_name: str
    track_name: Optional[str] = None
    program_name: str
    status: str # "Not Started", "Script WIP", "SME Review", "MO Approval", "GTG"
    shareable_uuid: str
    duration_minutes: int
    cards_count: int
    created_at: datetime.datetime
    cards: List[ScriptCardOut] = []
    agenda: List[Dict[str, Any]] = []            # what the class covers, each item with its minutes (shown above the cards)
    setup: Optional[Dict[str, Any]] = None       # the before-the-class setup checklist, when the class has code
    revision_note: Optional[str] = None          # the Module Owner's reason for not approving, until the author resubmits
    revision_by: Optional[str] = None
    revision_at: Optional[datetime.datetime] = None
    author_name: Optional[str] = None
    review_passed: Optional[bool] = None
    review_score: Optional[float] = None
    review_at: Optional[datetime.datetime] = None
    approved_by_name: Optional[str] = None
    approved_at: Optional[datetime.datetime] = None

    class Config:
        from_attributes = True

# ==============================================================================
# INLINE ASSIST, EDITING & OUTPUT GENERATION (PRD FR-11, FR-21, FR-23)
# ==============================================================================

class CardUpdateRequest(BaseModel):
    title: Optional[str] = None
    duration_mins: Optional[float] = None
    pedagogical_phase: Optional[str] = None
    stage_directions: Optional[str] = None
    content_script: Optional[str] = None
    code_snippet: Optional[str] = None
    code_language: Optional[str] = None
    is_approved: Optional[bool] = None


class QuizEditRequest(BaseModel):
    question: str
    options: List[str]
    correct_option_index: int
    explanation: str = ""
    reasons: List[str] = []          # reasons[i] says why option i is wrong (ignored for the right one)


class QuizRegenerateRequest(BaseModel):
    instruction: Optional[str] = None      # for example "test the difference between a database and a DBMS, with code"


class CodeTryRequest(BaseModel):
    code: Optional[str] = None           # the code as it is in the editor, not saved yet; None runs the card's saved code
    language: Optional[str] = None


class ChatAssistRequest(BaseModel):
    card_index: int
    user_prompt: str # e.g. "Simplify this analogy", "Add a failure mode for network timeout"

class AssignmentResponse(BaseModel):
    script_id: int
    class_title: str
    homework_problems: List[Dict[str, Any]]
    interview_questions: List[Dict[str, Any]]
    industry_context: str

# ==============================================================================
# USER & ROLE ACCESS GOVERNANCE SCHEMAS (PRD USERS & ROLES)
# ==============================================================================

class UserOut(BaseModel):
    id: int
    name: str
    email: str
    role: str
    program_ids: List[int] = []
    program_names: List[str] = []
    track_ids: List[int] = []
    track_names: List[str] = []

    class Config:
        from_attributes = True

class UserCreate(BaseModel):
    name: str
    email: str
    role: str  # "sme", "mo", "pm", "admin"
    program_ids: List[int] = []
    track_ids: List[int] = []

class UserUpdate(BaseModel):
    name: Optional[str] = None
    email: Optional[str] = None
    role: Optional[str] = None
    program_ids: Optional[List[int]] = None
    track_ids: Optional[List[int]] = None

class LoginRequest(BaseModel):
    email: str

class ActivityLogCreate(BaseModel):
    user_name: str
    user_role: Optional[str] = None
    action_type: str
    description: str
    details: Optional[str] = None

class ActivityBatchItem(BaseModel):
    action_type: str
    description: str

class ActivityBatch(BaseModel):
    actor_id: int
    items: List[ActivityBatchItem]

class UserActivityOut(BaseModel):
    id: int
    user_name: str
    user_role: Optional[str] = None
    action_type: str
    description: str
    details: Optional[str] = None
    created_at: datetime.datetime

    class Config:
        from_attributes = True


