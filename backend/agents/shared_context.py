"""
Shared context for every agent (Outliner, Writer, Reviewers, Moderator).

Two blocks go into every prompt:

  Block A  the facts about THIS script. It changes every time: the class, the audience, the syllabus, what was
           taught before and what comes next, the stack, and the sources the model may rely on.
  Block B  the Good Script Rules. They are the same for every script.

Block B is defined once, as data (RULES and LIMITS). The prompt text is rendered from that data, and the same data is
what a rule checker reads later, so the prompt and the checks cannot drift apart. Each rule is tagged:

  auto    can be checked by code (counts, ranges, patterns)
  review  needs a reader's judgement (teaching quality, tone, accuracy)

Block A is built in two steps: gather_block_a() reads the database into a plain dict, and render_block_a() turns that
dict into prompt text. build_block_a_context() does both and keeps the name and signature the agents already use.

The text produced here never contains an em dash, because rule R8 forbids them in scripts.
"""

import json
import os
import json
import re
from collections import namedtuple
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.orm import Session

from models import Class, MemoryLedger, Module, Script, ScriptCard

# ==============================================================================
# BLOCK B: THE GOOD SCRIPT RULES, AS DATA
# ==============================================================================

# Numbers used by the rules. Prompts and checks both read these.
LIMITS = {
    "teaching_card_minutes": (4, 10),    # concept, problem, walkthrough, faq, recap
    "quiz_card_minutes": (2, 5),
    "note_card_minutes": (1, 3),         # instructor_note, welcome, agenda
    "minutes_per_card": (5.0, 6.4),      # a class needs duration/6.4 to duration/5 cards (90 min: 14 to 18)
    "min_predict_moments": 3,
    "min_quizzes": 2,
    "min_diagrams": 4,
    "min_traces": 2,
    "max_diagram_nodes": 7,
    "paragraph_sentences": (2, 4),
    "subheadings_per_teaching_card": (2, 4),
    "instructor_note_sentences": (1, 2),
    "words_per_minute": 90,              # spoken words a card holds per minute: live teaching has pauses, chat replies and screen time
    "length_tolerance": (0.5, 1.15),     # a card may hold 50% to 115% of its word budget
}

TEACHING_CARD_TYPES = ("concept", "problem", "walkthrough", "faq", "recap")
CARD_TYPES = ("instructor_note", "welcome", "agenda", "concept", "problem", "walkthrough", "quiz", "faq", "recap")
CALLOUTS = ("Key", "Mistake", "Remember", "Real-world", "Predict")

Rule = namedtuple("Rule", "id group check text")


def word_budget(duration_mins: float) -> Tuple[int, int]:
    """(target, most) spoken words for a card that runs this long. The card's time slot is the limit, not the topic's appetite."""
    target = LIMITS["words_per_minute"] * float(duration_mins or 0)
    return int(round(target / 5.0) * 5), int(round(target * LIMITS["length_tolerance"][1] / 5.0) * 5)


def cards_for_duration(duration_minutes: int) -> Tuple[int, int]:
    """Card count range for a class of this length (S1)."""
    slow, fast = LIMITS["minutes_per_card"][1], LIMITS["minutes_per_card"][0]
    return round(duration_minutes / slow), round(duration_minutes / fast)


_t_lo, _t_hi = LIMITS["teaching_card_minutes"]
_q_lo, _q_hi = LIMITS["quiz_card_minutes"]
_n_lo, _n_hi = LIMITS["note_card_minutes"]
_p_lo, _p_hi = LIMITS["paragraph_sentences"]
_h_lo, _h_hi = LIMITS["subheadings_per_teaching_card"]
_i_lo, _i_hi = LIMITS["instructor_note_sentences"]

RULES: List[Rule] = [
    # ---- Structure
    Rule("S1", "STRUCTURE", "auto",
         f"A script is a deck of small cards, one idea per card. Concept, problem, walkthrough, faq and recap cards run "
         f"{_t_lo} to {_t_hi} minutes, quiz cards {_q_lo} to {_q_hi}, instructor_note, welcome and agenda cards {_n_lo} to {_n_hi}. "
         f"A class of D minutes needs about D/{LIMITS['minutes_per_card'][1]} to D/{LIMITS['minutes_per_card'][0]:g} cards "
         f"(90 minutes: 14 to 18)."),
    Rule("S2", "STRUCTURE", "auto",
         "Card order: card 1 is always the instructor_note titled Instructor Introduction (the system writes its text), card 2 is the welcome card titled Welcome & Introductions, card 3 is the agenda card titled Today's Agenda (only the agenda), then concept, problem and walkthrough cards with quiz cards "
         "between them, then faq, then recap."),
    Rule("S3", "STRUCTURE", "review", "The recap is one visual mental model of the whole class."),
    Rule("S4", "STRUCTURE", "auto", "Card durations sum exactly to the class duration in Block A."),
    Rule("S8", "STRUCTURE", "auto",
         f"A card's spoken words fit its time: about {LIMITS['words_per_minute']} words per minute of the card's duration, and never more than "
         f"{round(LIMITS['length_tolerance'][1] * 100)}% of that (a 5 minute card holds about {word_budget(5)[0]} words, at most {word_budget(5)[1]}). "
         f"Fit every agenda item and outcome the card serves into that space: one clear idea, one example, one check. Cut repeats, "
         f"recaps inside a card, extra examples and restated points. Depth is not length."),
    Rule("S5", "STRUCTURE", "review",
         "Cover every agenda item and learning outcome. Add nothing outside the agenda."),
    Rule("S6", "STRUCTURE", "review",
         "Do not teach anything listed under upcoming classes. A one-line forward pointer is allowed."),
    Rule("S7", "STRUCTURE", "auto",
         "Code to content ratio: about the share of cards stated in Block A carry code or a code trace (within 10 points)."),

    # ---- Teaching shape
    Rule("T1", "TEACHING SHAPE", "review",
         "Each concept follows Why, then What, then How, then Where, then What breaks. Never open with a definition. "
         "This is the order of the ideas, not headings: write it as natural speech and never print the words "
         "'Why', 'What', 'How', 'Where' or 'What breaks' as labels."),
    Rule("T2", "TEACHING SHAPE", "review",
         "Show the obvious approach working at small scale, break it with a quantified failure, then name the fix."),
    Rule("T3", "TEACHING SHAPE", "review",
         "Every claim gets its 'so what' (a number or a consequence) in the next sentence."),
    Rule("T4", "TEACHING SHAPE", "review",
         "Use one running example for the whole class, grown visually, with identical names every time. If Block A "
         "gives a running example from the previous class, continue it. Do not reset it."),
    Rule("T5", "TEACHING SHAPE", "review",
         "Bring an idea back in a new form each time (words, then diagram, then quiz, then applied). Never repeat a form."),
    Rule("T6", "TEACHING SHAPE", "review",
         "Teach a term before using it anywhere, including quizzes, diagrams and code comments. Terms from past "
         "classes in Block A count as already taught."),
    Rule("T7", "TEACHING SHAPE", "review",
         "Climb from the current level to the target level across the cards, and signpost each step up."),
    Rule("T8", "TEACHING SHAPE", "review",
         "Apply the content mix in Block A. The beginner share gets plain-language scaffolding and a motivating example "
         "before any definition. The advanced share gets edge cases, internals and trade-offs, introduced under a "
         "'Going deeper' sub-heading so beginners know they can skim it."),

    # ---- Engagement
    Rule("E1", "ENGAGEMENT", "auto",
         f"Include at least {LIMITS['min_predict_moments']} predict-before-reveal moments. Mark the question with a "
         f"Predict callout and the answer with a line that starts 'Reveal:'."),
    Rule("E2", "ENGAGEMENT", "auto",
         f"Include at least {LIMITS['min_quizzes']} quizzes with tempting wrong options. Explain every wrong option."),
    Rule("E3", "ENGAGEMENT", "review",
         "Interactions must work online: learners type answers in chat, and thumbs up or down is used only for "
         "binary checks. No show of hands, no pair work."),
    Rule("E4", "ENGAGEMENT", "review",
         "Use analogies from learners' daily life (UPI, WhatsApp, food delivery, train booking, cinema tickets, "
         "groceries, cricket and the IPL, banks, e-commerce). Use one per concept, carry it through, and name where it breaks. Never use abstract or exotic "
         "analogies (rockets, particle physics, nuclear reactors)."),

    # ---- Readability
    Rule("R1", "READABILITY", "auto",
         f"Keep paragraphs to {_p_lo} to {_p_hi} sentences."),
    Rule("R13", "READABILITY", "auto",
         f"A teaching card has {_h_lo} to {_h_hi} short sub-headings, so a learner can skim it."),
    Rule("R2", "READABILITY", "auto",
         f"Use callouts ({', '.join(CALLOUTS)}). Every teaching card has at least one Key callout."),
    Rule("R3", "READABILITY", "auto", "Bold a term only where it is defined."),
    Rule("R4", "READABILITY", "auto",
         f"Include at least {LIMITS['min_diagrams']} diagrams per class, at least one that branches or compares."),
    Rule("R5", "READABILITY", "auto",
         f"Keep every diagram to {LIMITS['max_diagram_nodes']} nodes or fewer. Show before and after as two diagram "
         f"steps, and draw flows one step per stage. A diagram is a flowchart of boxes joined by arrows: no axes, curves, grids, icons, markers or colours; put the detail in the "
         f"box text. Something that moves or changes over time is an animation, not a diagram."),
    Rule("R6", "READABILITY", "auto",
         f"Include at least {LIMITS['min_traces']} step-by-step traces per class, written in code blocks."),
    Rule("R7", "READABILITY", "auto",
         "No tables, raw HTML or embedded images in card text. A visual is written only as a [[VISUAL: ...]] "
         "placeholder (mermaid, animation or image) that the visuals agent resolves."),
    Rule("R8", "READABILITY", "auto",
         "No em dashes. Vary sentence length. Address the learner as 'you'."),
    Rule("R9", "READABILITY", "review",
         "Depth is not length. Add examples, traces and consequences, never more paragraphs."),
    Rule("R10", "READABILITY", "auto",
         f"Instructor notes are {_i_lo} to {_i_hi} sentences of delivery hints only."),

    # ---- Voice
    Rule("V1", "VOICE", "auto",
         "A card's text is the exact words the instructor says aloud, in first person ('I', 'we') to the learners ('you'). Never write "
         "a direction to the instructor ('Greet everyone', 'Ask the learners to...', 'Pause and watch the chat', 'Move on to the next card') "
         "and never talk about the learners in the third person ('the learners', 'their first session'). The only text that is not "
         "spoken is a short bracketed cue of at most 8 words, such as [pause for replies]."),

    # ---- Accuracy
    Rule("A1", "ACCURACY", "review",
         "Qualify any, every, all, no and never claims about compatibility, safety or automation by naming the boundary."),
    Rule("A2", "ACCURACY", "review",
         "If a sequence is simplified, say so in the first sentence of the first card that shows it."),
    Rule("A3", "ACCURACY", "auto",
         "Code must run. State the platform and version beside it, using the stack in Block A. If Block A says the "
         "stack is not specified, choose the standard one for this module, state it on the first card, and list it "
         "under assumptions."),
    Rule("A4", "ACCURACY", "review", "Add code or datasets only if the class needs them."),
    Rule("A5", "ACCURACY", "review",
         "Never invent files, code, datasets or outputs from earlier classes. Use a placeholder: "
         "[[FROM_CLASS: <class title> | <artefact>]]."),
    Rule("A6", "ACCURACY", "review",
         "Every walkthrough step states what to run, what success looks like, and the likely failure. Each walkthrough "
         "names at least one failure mode."),
    Rule("A7", "ACCURACY", "auto",
         "Cite current official documentation for every API, command, flag or tool you teach, from the approved sources "
         "in Block A first. A source found elsewhere is labelled 'UNVERIFIED: needs SME validation'. State no "
         "version-specific fact without a source. Every reference carries its URL and the date it was retrieved."),
]

RULE_PRIORITY = "correctness, then difficulty arc, then room mix, then time"

CARD_FORMAT = """\
title: <short, clean>
description: <one line>
duration: <seconds>
card_type: <instructor_note | welcome | agenda | concept | problem | walkthrough | quiz | faq | recap>
<body in Markdown>

Quiz body:
  question, numbered choices (one per line), the correct choice, why each wrong choice is wrong, and an explanation.

Callouts: a blockquote line that starts with the label in bold, for example "> **Key:** one sentence". Labels: Key,
  Mistake, Remember, Real-world, Predict. A predict-before-reveal moment is "> **Predict:** the question", followed
  later by a line that starts "Reveal:" with the answer.

Visual placeholder, anywhere in a body:
  [[VISUAL: <id> | type: mermaid | animation | image | intent: <what it must show> | nodes: <list, at most 7> | step: <n of m>]]
  Use step only for a before and after pair or a flow shown in stages."""


# Rules the review checks that the Writer is not given as writing rules: the opening, quiz and visual checks and what each reviewer looks for. One place, so a
# finding that cites an id always shows the Moderator the same text (see agents/moderator.py), and an id means the same thing in the code, the lenses and the UI.
REVIEW_RULES: List[Rule] = [
    Rule("K1", "OPENING", "auto", "A program's first class mentions every fact its opening kit requires (for example attendance, scoring and support)."),
    Rule("K2", "OPENING", "auto", "A program's first class opens with the approved spoken welcome, word for word."),
    Rule("K3", "OPENING", "auto", "The first spoken card holds the placeholder for the instructor's own introduction."),
    Rule("K4", "OPENING", "auto", "The class has an agenda card that lists every agenda item with its time."),
    Rule("K5", "OPENING", "auto", "The agenda card holds only the agenda, and the welcome card holds no agenda list."),
    Rule("S9", "STRUCTURE", "auto", "No card number is missing, so no card leans on one that is gone."),
    Rule("N2", "VOICE", "auto", "No production wording (option index, zero-based, quiz JSON, Visual V01) in the spoken text."),
    Rule("V2", "VOICE", "auto", "A card picks up where the one before it stopped, and does not greet the learners again."),
    Rule("V3", "VISUALS", "auto", "An animation is complete: one that was cut off while being written shows nothing and must be made again."),
    Rule("V4", "VISUALS", "auto", "A diagram, animation or picture is about its own card and uses only names the script uses."),
    Rule("V6", "VISUALS", "auto", "A diagram can be drawn: the text of every box is in quotes, so a | ( ) { } ; # & < > inside it does not show 'Syntax error in text'."),
    Rule("V5", "VISUALS", "review", "A diagram, animation or picture says the right thing for its card: right order, right cause and effect, nothing that would mislead."),
    Rule("R11", "VISUALS", "auto", "The recap card has exactly one visual."),
    Rule("R12", "VISUALS", "auto", "At least one diagram in the class branches or compares."),
    Rule("Q1", "QUESTIONS", "auto", "The quiz on screen is exactly the question and options the card text reads out, with the same right answer."),
    Rule("Q2", "QUESTIONS", "auto", "A quiz has exactly one keyed correct answer, and every option is different."),
    Rule("Q3", "QUESTIONS", "auto", "A quiz asks only about what this script teaches."),
    Rule("Q4", "QUESTIONS", "review", "A quiz tests only what the script has taught by this card. Its keyed answer is right according to the script, and no other option is also right. "
                                       "If you change the question or options in the card text, change the quiz to match, and the other way round."),
    Rule("L1", "REVIEWERS", "review", "Read as a BEGINNER and report every place you got lost: a term used before it is taught, a jump between ideas, a step with no reason, code that is not explained."),
    Rule("I1", "REVIEWERS", "review", "Read as an INTERMEDIATE learner and report every gap between the basics they know and the depth the card jumps to."),
    Rule("I2", "REVIEWERS", "review", "Pacing: the basics explained again at length, or a new idea that arrives with no bridge from what the learner knows."),
    Rule("I3", "REVIEWERS", "review", "Examples are realistic enough to feel like real work, not toy cases only."),
    Rule("I4", "REVIEWERS", "review", "Each new idea says when to use it and what to do when it fails."),
    Rule("D1", "REVIEWERS", "review", "Read as an ADVANCED learner and report every technical error: a wrong statement, a wrong number, a claim without its boundary."),
    Rule("D2", "REVIEWERS", "review", "Shallow depth: a topic explained only at the surface, an example too trivial to teach the idea, an edge case or failure mode that is missing."),
    Rule("D3", "REVIEWERS", "review", "The class climbs to its target level; flag cards that stay too basic for the level the class aims at."),
    Rule("D4", "REVIEWERS", "review", "Every claim about speed, cost or scale has a number or a consequence, not an adjective."),
]
RULES_BY_ID = {r.id: r for r in list(RULES) + REVIEW_RULES}

# What blocker, major and minor mean, for every reviewer. Same words for all of them, so the same problem is rated the same way.
SEVERITY_GUIDE = """\
SEVERITY (use exactly these meanings; if unsure between two, choose the lower one)
- blocker: a learner would be misled or stuck, or the script cannot be taught as it stands. Examples: a wrong fact or number, code that does not run, a term the script never defines, an agenda item or learning outcome no card serves, content from an upcoming class, a card that leans on a missing card, a quiz whose marked answer is wrong.
- major: a learner would be confused, or the instructor could not deliver the card as written. Examples: a claim with no boundary or no "so what", a term used before it is defined, two cards that contradict each other, a quiz that differs from the question read out.
- minor: style, wording, formatting or a nice-to-have. A preference is never major."""


def render_rules(rules: List[Rule] = RULES, include_format: bool = True) -> str:
    """Block B text, rendered from RULES. include_format=False leaves out the card format, for agents that do not write cards."""
    lines = [
        "<good_script_rules>",
        f"When two rules conflict, resolve in this order: {RULE_PRIORITY}.",
        "Tags: [auto] can be checked by code, [review] needs a reader's judgement. Both are mandatory.",
    ]
    group = None
    for r in rules:
        if r.group != group:
            group = r.group
            lines.append("")
            lines.append(group)
        lines.append(f"{r.id} [{r.check}] {r.text}")
    if include_format:
        lines.append("")
        lines.append("<card_format>")
        lines.append(CARD_FORMAT)
        lines.append("</card_format>")
    lines.append("</good_script_rules>")
    return "\n".join(lines)


BLOCK_B_GOOD_SCRIPT_RULES = render_rules()

# What the Outliner needs: how the class is shaped, taught and engaged, and the diagram rules. The wording and accuracy
# rules belong to the Writer and the reviewers, and leaving them out keeps the Outliner's answer room large.
OUTLINER_RULES = render_rules([r for r in RULES if r.group in ("STRUCTURE", "TEACHING SHAPE", "ENGAGEMENT") or r.id in ("R4", "R5", "R6")],
                              include_format=False)


# ==============================================================================
# BLOCK A: THE CONTEXT FOR THIS SCRIPT
# ==============================================================================

# Hook for rules that apply to one program only, for example a required language. Empty on purpose; they win over
# Block B where they conflict. Keyed by program name.
PROGRAM_RULE_OVERRIDES: Dict[str, List[str]] = {}

# What was written into classes before real agendas existed. Two shapes: a fixed four-line template (most classes),
# and lines generated from the class title (DevOps and FDE), which all start with one of these openings.
_PLACEHOLDER_AGENDA_LINES = (
    "Conceptual Foundations of",
    "Real-World Production Architecture & Failure Modes",
    "Hands-On Implementation & Live Problem Walkthrough",
    "Key Misconceptions, FAQs & 90-Minute Mental Model Recap",
)
_PLACEHOLDER_OPENINGS = (
    "Conceptual Foundations of",
    "Core concepts of",
    "Production implementation of",
    "Failure modes and diagnostics for",
    "Enterprise client delivery for",
    "Troubleshooting and failure boundaries for",
)
_PLACEHOLDER_OUTCOME_PREFIX = "Master foundational concepts and industry best practices for"

PAST_DETAIL = {"full": 3, "compact": 0}       # how many recent classes get their agenda shown
UPCOMING_DETAIL = {"full": 8, "compact": 0}      # how many upcoming classes show their agenda (their titles are always shown): what they teach is off limits here
PREVIOUS_SCRIPT_CHARS = 2000


def agenda_is_placeholder(items: List[str]) -> bool:
    """True for an agenda nobody wrote: the four-line template, or lines that only wrap the class title."""
    template_hits = sum(1 for marker in _PLACEHOLDER_AGENDA_LINES if any(marker in (i or "") for i in items))
    opening_hits = sum(1 for i in items if (i or "").strip().startswith(_PLACEHOLDER_OPENINGS))
    return template_hits >= 3 or opening_hits >= 2


def _loads_list(raw) -> List[str]:
    if not raw:
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw]
    try:
        value = json.loads(raw)
        return [str(x) for x in value] if isinstance(value, list) else [str(value)]
    except Exception:
        return [str(raw)]


def _split_sources(raw: Optional[str]) -> List[str]:
    return [s.strip() for s in re.split(r"[,\n;]+", raw or "") if s.strip()]


def _level_of(track_name: str) -> Optional[str]:
    lowered = (track_name or "").lower()
    for level in ("beginner", "intermediate", "advanced"):
        if level in lowered:
            return level.capitalize()
    return None


def _previous_script_text(previous_script_context: Optional[str]) -> Optional[str]:
    """The SME's uploaded previous script, if there is one. The script row stores a JSON memory snapshot in the same
    field, and the upload sits inside it as manual_notes."""
    if not previous_script_context:
        return None
    try:
        value = json.loads(previous_script_context)
    except Exception:
        return previous_script_context.strip() or None
    if isinstance(value, dict):
        return (value.get("manual_notes") or "").strip() or None
    return None


def gather_block_a(
    db: Session,
    class_id: int,
    *,
    duration_minutes: int,
    content_mix_beginner: int,
    code_ratio: int,
    custom_title: Optional[str] = None,
    custom_agendas: Optional[List[str]] = None,
    learning_outcomes: Optional[str] = None,
    api_docs: Optional[str] = None,
    reference_urls: Optional[str] = None,
    additional_resources: Optional[str] = None,
    previous_script_context: Optional[str] = None,
    view: str = "full",
) -> Optional[Dict[str, Any]]:
    """Reads everything Block A needs into a plain dict. Returns None when the class does not exist."""
    cls = db.query(Class).filter(Class.id == class_id).first()
    if not cls:
        return None
    mod = cls.module
    trk = mod.track if mod else None
    prog = trk.program if trk else None

    agenda = list(custom_agendas) if custom_agendas else _loads_list(cls.agendas)
    outcomes = (learning_outcomes or cls.learning_outcomes or "").strip()

    # ---- where this class sits in its module
    siblings = []
    if mod:
        siblings = [c for c in db.query(Class).filter(Class.module_id == mod.id).order_by(Class.sequence_number).all()
                    if c.class_type != "Archived" and c.sequence_number < 1000]
    past = [c for c in siblings if c.sequence_number < cls.sequence_number]
    upcoming = [c for c in siblings if c.sequence_number > cls.sequence_number]
    ledgers = {l.class_id: l for l in db.query(MemoryLedger).filter(MemoryLedger.class_id.in_([c.id for c in past])).all()} if past else {}

    def past_entry(c: Class, detailed: bool) -> Dict[str, Any]:
        entry = {"number": c.sequence_number, "title": c.title, "type": c.class_type or "Live"}
        if detailed:
            lines = _loads_list(c.agendas)
            entry["agenda"] = [] if agenda_is_placeholder(lines) else lines[:5]
            ledger = ledgers.get(c.id)
            entry["terms"] = _loads_list(ledger.terms_mastered) if ledger else []
        return entry

    n_past = PAST_DETAIL[view]
    n_next = UPCOMING_DETAIL[view]
    past_out = [past_entry(c, i >= len(past) - n_past) for i, c in enumerate(past)]
    upcoming_out = []
    for i, c in enumerate(upcoming):
        entry = {"number": c.sequence_number, "title": c.title, "type": c.class_type or "Live"}
        if i < n_next:
            lines = _loads_list(c.agendas)
            entry["agenda"] = [] if agenda_is_placeholder(lines) else lines[:5]
        upcoming_out.append(entry)

    # ---- the class just before this one: its approved script's record, if the Module Owner approved one
    previous_class = None
    if past:
        last = past[-1]
        ledger = ledgers.get(last.id)
        try:
            memory = json.loads(ledger.summary_json) if ledger is not None and ledger.summary_json else None
        except ValueError:
            memory = None
        approved = (db.query(Script).filter(Script.class_id == last.id, Script.status == "GTG").order_by(Script.id.desc()).first())
        if not (isinstance(memory, dict) and memory.get("points")):          # no approved record: follow the class's newest draft, labelled as a draft
            from agents.class_memory import latest_script_memory
            memory = latest_script_memory(db, last.id, cls.title)
        previous_class = {"number": last.sequence_number, "title": last.title, "memory": memory if isinstance(memory, dict) else None,
                          "approved": approved is not None, "approved_by": approved.approved_by_name if approved is not None else None}

    # ---- continuity: only what a real earlier class or module recorded, never a guess
    running_example = None
    cliffhanger = None
    bridge = None
    if past:
        prev_ledger = ledgers.get(past[-1].id)
        if prev_ledger:
            running_example = prev_ledger.anchor_analogy
            cliffhanger = prev_ledger.cliffhanger
    else:
        from agents.memory_engine import retrieve_context_memory
        memory = retrieve_context_memory(db, class_id=cls.id, manual_context=None)
        bridge = memory.get("previous_module_bridge")
        if bridge and bridge.get("running_system_used"):
            running_example = bridge["running_system_used"]

    # ---- sources
    approved = _split_sources(mod.approved_sources) if mod else []
    sme_sources = _split_sources(reference_urls)

    # ---- earlier scripts of this class (reference only)
    upload = _previous_script_text(previous_script_context)
    earlier_titles: List[str] = []
    earlier_status = None
    earlier = (db.query(Script).filter(Script.class_id == cls.id, Script.status.in_(["GTG", "MO Approval", "SME Review"]))
               .order_by(Script.id.desc()).first())
    if earlier:
        earlier_status = earlier.status
        earlier_titles = [c.title for c in db.query(ScriptCard).filter(ScriptCard.script_id == earlier.id)
                          .order_by(ScriptCard.card_index).limit(25).all()]

    track_name = trk.name if trk else ""

    # ---- is this the first class of its track? of the whole program? (first class of the first module)
    track_opening = program_opening = program_first = False
    if mod and trk and prog and not past:
        first_module = db.query(Module).filter(Module.track_id == trk.id).order_by(Module.sequence_order, Module.id).first()
        track_opening = bool(first_module and first_module.id == mod.id
                             and cls.sequence_number == min((c.sequence_number for c in siblings), default=cls.sequence_number))
        first_track = min(prog.tracks, key=lambda t: t.id) if prog.tracks else None
        program_first = bool(track_opening and first_track and first_track.id == trk.id)
        program_opening = track_opening          # the first class of EVERY track is an opening class: onboarding and teaching together
    opening_kit = load_opening_kit(prog.name if prog else "", track_name) if track_opening else None
    welcome = (opening_kit or {}).get("welcome_text") or (OPENING_WELCOME if (track_opening and program_first) else track_welcome_text(track_name))
    return {
        "program": prog.name if prog else "",
        "program_opening": program_opening,
        "program_first_class": program_first,
        "opening_welcome": welcome if track_opening else None,
        "track_opening": track_opening,
        "opening_kit": opening_kit,
        "track": track_name,
        "level": _level_of(track_name),
        "module": mod.name if mod else "",
        "segment": mod.segment if mod else None,
        "module_tag": mod.module_type if mod else None,
        "class_title": custom_title or cls.title,
        "class_number": cls.sequence_number,
        "classes_in_module": len(siblings),
        "class_type": cls.class_type or "Live",
        "junction": cls.junction_number,
        "duration_minutes": duration_minutes,
        "content_mix_beginner": content_mix_beginner,
        "code_ratio": code_ratio,
        "agenda": agenda,
        "agenda_is_placeholder": agenda_is_placeholder(agenda),
        "outcomes": outcomes,
        "outcomes_are_placeholder": (not outcomes) or outcomes.startswith(_PLACEHOLDER_OUTCOME_PREFIX),
        "stack": (api_docs or "").strip() or None,
        "past": past_out,
        "previous_class": previous_class,
        "upcoming": upcoming_out,
        "running_example": running_example,
        "cliffhanger": cliffhanger,
        "module_bridge": bridge,
        "approved_sources": approved,
        "sme_sources": sme_sources,
        "additional_resources": (additional_resources or "").strip() or None,
        "previous_script_upload": upload[:PREVIOUS_SCRIPT_CHARS] if upload else None,
        "previous_script_upload_truncated": bool(upload and len(upload) > PREVIOUS_SCRIPT_CHARS),
        "earlier_script_status": earlier_status,
        "earlier_script_cards": earlier_titles,
        "overrides": PROGRAM_RULE_OVERRIDES.get(prog.name, []) if prog else [],
    }


def _bullets(items: List[str], indent: str = "  - ") -> str:
    return "\n".join(f"{indent}{i}" for i in items)


def render_block_a(d: Dict[str, Any], kit_focus: Optional[str] = None) -> str:
    """Block A text, rendered from gather_block_a()."""
    lo, hi = cards_for_duration(d["duration_minutes"])
    mix_b, mix_a = d["content_mix_beginner"], 100 - d["content_mix_beginner"]
    out: List[str] = ["<context>"]

    where = f"Program: {d['program']}\nTrack: {d['track']}"
    if d["level"]:
        where += f" (learner level: {d['level']})"
    where += f"\nModule: {d['module']}"
    tags = [t for t in (d["segment"] if d["segment"] != d["module"] else None, d["module_tag"]) if t]
    if tags:
        where += f" [{', '.join(tags)}]"
    out.append("<class>")
    out.append(where)
    out.append(f"Class title: {d['class_title']}")
    out.append(f"Position: class {d['class_number']} of {d['classes_in_module']} in this module")
    out.append(f"Class type: {d['class_type']}, delivered live online")
    out.append(f"Duration: {d['duration_minutes']} minutes (about {lo} to {hi} cards)")
    out.append("</class>")

    out.append("<audience>")
    out.append(f"Content mix: {mix_b}% beginner, {mix_a}% advanced")
    out.append(f"Code to content ratio: about {d['code_ratio']}% of cards carry code, {100 - d['code_ratio']}% are concepts and visuals")
    out.append("</audience>")

    out.append("<syllabus>")
    if d["agenda_is_placeholder"]:
        out.append("Agenda status: PLACEHOLDER. The Module Owner has not provided a real agenda for this class. Build the "
                   "agenda from the class title and the neighbouring classes, and list every topic you add under "
                   "assumptions so the SME can correct it.")
    else:
        out.append("Agenda status: official")
    out.append("Agenda:")
    if d["agenda"] and not d["agenda_is_placeholder"]:
        out.append(_bullets(d["agenda"]))
    else:
        out.append("  - (none provided)")
    if d["outcomes_are_placeholder"]:
        out.append("Learning outcomes: NOT PROVIDED. Derive them from the agenda and state them on the agenda card.")
    else:
        out.append(f"Learning outcomes: {d['outcomes']}")
    out.append("</syllabus>")

    out.append("<stack>")
    if d["stack"]:
        out.append(f"Language, platform and version, as given by the SME: {d['stack']}")
    else:
        out.append(f"Language, platform and version: NOT SPECIFIED. Choose the standard stack for the module '{d['module']}' "
                   "and state it on the first card and under assumptions (rule A3).")
    out.append("</stack>")

    out.append("<curriculum_position>")
    if d["past"]:
        out.append("Taught in this module so far (learners know this):")
        for p in d["past"]:
            line = f"  {p['number']}. {p['title']}" + (f" [{p['type']}]" if p["type"] != "Live" else "")
            out.append(line)
            if p.get("agenda"):
                out.append("       covered: " + "; ".join(p["agenda"]))
            if p.get("terms"):
                out.append("       terms introduced: " + ", ".join(p["terms"]))
    else:
        out.append("This is the FIRST class of the module. Nothing from this module has been taught.")
        if d["module_bridge"]:
            out.append("Previous module: " + d["module_bridge"].get("bridge_directive", ""))
    if d["upcoming"]:
        out.append("Planned next (do NOT teach these here; a one-line forward pointer is allowed):")
        for u in d["upcoming"]:
            out.append(f"  {u['number']}. {u['title']}" + (f" [{u['type']}]" if u["type"] != "Live" else ""))
            if u.get("agenda"):
                out.append("       will cover: " + "; ".join(u["agenda"]))
    else:
        out.append("This is the LAST class of the module.")
    out.append("</curriculum_position>")
    if d.get("previous_class"):
        from agents.class_memory import render_previous_class     # imported here: class_memory needs the rule checker, which needs this module
        pc = d["previous_class"]
        out.append(render_previous_class(pc["number"], pc["title"], pc.get("memory"), pc.get("approved_by")))

    if d.get("program_opening"):                      # the first class of any track: onboarding and teaching together, with that track's own welcome
        out.append(PROGRAM_OPENING_GUIDANCE.format(program=d["program"] or "this program", welcome=d.get("opening_welcome") or OPENING_WELCOME))
        out.append(opening_level_note(d))
    if d.get("track_opening") and d.get("opening_kit"):
        out.append(render_opening_kit(d["opening_kit"], kit_focus))

    out.append("<continuity>")
    if d["running_example"]:
        out.append(f"Running example from the previous class: {d['running_example']}. Continue it with the same names.")
    else:
        out.append("Running example: none yet. Choose one everyday system for the whole class (rules T4 and E4).")
    if d["cliffhanger"]:
        out.append(f"How the previous class ended: {d['cliffhanger']}")
    out.append("</continuity>")

    out.append("<references>")
    if d["approved_sources"] or d["sme_sources"]:
        out.append("Approved documentation (use these first, cite with URL and retrieval date):")
        out.append(_bullets(d["approved_sources"] + [f"{s} (given by the SME)" for s in d["sme_sources"]]))
    else:
        out.append("Approved documentation: NONE for this module. Any source you use is labelled "
                   "'UNVERIFIED: needs SME validation' (rule A7).")
    if d["additional_resources"]:
        out.append(f"Additional resources from the SME: {d['additional_resources']}")
    if d["previous_script_upload"]:
        out.append("Earlier script of this class, uploaded by the SME. Reference only: take intent and examples, never "
                   "copy APIs, versions or facts without checking them against the approved documentation.")
        out.append(d["previous_script_upload"] + ("\n[truncated]" if d["previous_script_upload_truncated"] else ""))
    if d["earlier_script_cards"]:
        out.append(f"An earlier {d['earlier_script_status']} script for this class had these cards (reference only): "
                   + "; ".join(d["earlier_script_cards"]))
    out.append("</references>")

    if d["overrides"]:
        out.append("<program_overrides>")
        out.append("These win over the good script rules where they conflict:")
        out.append(_bullets(d["overrides"]))
        out.append("</program_overrides>")

    out.append("</context>")
    out.append("")
    out.append(CONTEXT_RULES)
    return "\n".join(out)


OPENING_KIT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "opening_kits")


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")


def load_opening_kit(program: str, track: str, directory: str = OPENING_KIT_DIR) -> Optional[Dict[str, Any]]:
    """The reusable onboarding facts for a program's first class (PSP, attendance, links, contacts), or None.
    One JSON file per program and track: data/opening_kits/<program>__<track>.json."""
    path = os.path.join(directory, f"{_slug(program)}__{_slug(track)}.json")
    try:
        with open(path, encoding="utf-8") as f:
            kit = json.load(f)
    except (OSError, ValueError):
        return None
    return kit if kit.get("enabled", True) else None      # "enabled": false switches a kit off without deleting it


def _headline(text: str, limit: int) -> str:
    """The start of a fact, cut at a sentence or word boundary, so a long kit can be listed in a few lines."""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[:stop + 1] if stop > limit * 0.5 else cut.rsplit(" ", 1)[0]) + " ..."


def render_opening_kit(kit: Dict[str, Any], focus: Optional[str] = None, headline_chars: int = 120, full_facts: int = 3,
                       full: bool = False, full_chars: int = 3000) -> str:
    """The kit as prompt text, sized to the job. With no focus (the Outliner) every fact is a one-line headline, so the
    whole flow is visible cheaply (full=True gives every fact in full). With a focus (the text of the card being written) the few facts that match it are
    given in full, because the Writer must use their numbers and links exactly, and the rest stay as headlines."""
    from agents.rule_checker import words
    facts = kit.get("facts", {})
    need = words(focus) if focus else set()
    scored = []
    for key, value in facts.items():
        score = len(words(key.replace("_", " ") + " " + str(value)) & need) if need else 0
        scored.append((score, key, value))
    full_keys = set(facts)
    if not full:
        # the best matches, in full, until the character budget is used; this keeps a card's prompt inside a small token limit
        full_keys, used = set(), 0
        for sc, k, v in sorted(scored, key=lambda t: -t[0])[:full_facts]:
            if sc < 3 or (full_keys and used + len(str(v)) > full_chars):
                continue
            full_keys.add(k)
            used += len(str(v))
    lines = ["<opening_kit>",
             "Approved facts for this class. Where a fact is given in full, use it exactly: never change a number, time, link or "
             "address, and add no policy that is not listed. A fact ending in '...' is a headline only; use just its gist.",
             "Topics to cover, each in its own card, in a warm spoken voice: " + "; ".join(kit.get("onboarding_cards", []))]
    for score, key, value in scored:
        text = (str(value) if (full or len(str(value)) <= full_chars * 1.3) else _headline(value, int(full_chars * 1.3))) if key in full_keys \
            else _headline(value, headline_chars)
        lines.append(f"- {key.replace('_', ' ')}: {text}")
    if kit.get("existing_animations"):
        lines.append("Ready-made animations (name them in the matching visual card instead of drawing a new one): "
                     + "; ".join(f"{a['topic']} = {a['url']}" for a in kit["existing_animations"]))
    lines.append("</opening_kit>")
    return "\n".join(lines)


# The words a program's first class starts with, approved by the program team. The Writer must use them word for word.
OPENING_WELCOME = (
    "Hey Everyone, welcome to your first session at Scaler!\n"
    "I hope that you have a great time learning and interacting here.\n\n"
    "I want to know about everyone here, so please send 2 lines about yourself in chat!\n\n"
    "[pause for replies]\n\n"
    "Alright, to begin with let me start with my Introduction.\n\n"
    "[[FROM_SME: instructor introduction]]"
)


# Card 1 of every class. It is a note for the instructor, not spoken words, and the system writes it so it is always the same.
INTRO_NOTE_CUE = "Prepare a one-minute introduction of yourself before you go live. You give it right after the welcome and the chat question."
INTRO_NOTE_BODY = ("Introduce yourself to the learners in about a minute: who you are, what you do, how long you have worked in this field, and why you teach this course.\n\n"
                   "[[FROM_SME: instructor name, current role, years of experience, and one line on why you teach this]]")


def track_welcome_text(track: str) -> str:
    """The welcome for the first class of a track that is not the program's first (Intermediate, Advanced and the like). Proposed wording: the program team can
    replace it for one track by adding "welcome_text" to that track's opening kit."""
    return (f"Hey Everyone, welcome to the {track or 'next track'}!\n"
            "It is great to have you here. You already know how things run, and this track builds on what you have learned.\n\n"
            "I want to know about everyone here, so please send 2 lines about yourself in chat: what you have built or learned so far, and what you want from this track!\n\n"
            "[pause for replies]\n\n"
            "Alright, to begin with let me start with my Introduction.\n\n"
            "[[FROM_SME: instructor introduction]]")


def has_opening_welcome(body: str) -> bool:
    """True when a card's text carries an opening welcome: it welcomes the learners and asks them for 2 lines about themselves in chat."""
    low = re.sub(r"\s+", " ", body or "").lower()
    return "2 lines about yourself" in low and "welcome to" in low[:400]


def ensure_opening_welcome(body: str, welcome: str = OPENING_WELCOME) -> str:
    """The card text with the opening welcome in front of it if it is missing. Whatever the Writer wrote follows it."""
    return body if has_opening_welcome(body) else welcome + "\n\n" + (body or "").lstrip()


PROGRAM_OPENING_GUIDANCE = """\
<program_opening>
This is the FIRST class of a track of {program}. It is the onboarding AND the first real lesson, both in one class: do not spend the whole class
on onboarding, and do not skip it. About a quarter of the minutes onboard the learners (welcome, introduction, agenda, how the track and program
run); the rest teach the first topics of today's agenda in full, to the same depth as any other class. Nothing from this track has been taught, so do
not refer to an earlier class of it. Shape the script like a good opening session:
- The class opens with three cards. Card 1 is the short instructor note "Instructor Introduction" (card_type instructor_note); the system
  writes its text, so keep yours to one line. Card 2 is "Welcome & Introductions" (card_type welcome): the instructor's own spoken welcome, and
  never an instruction to the instructor. Card 2's body begins with exactly this text, word for word, with nothing before it:
<welcome>
{welcome}
</welcome>
  The introduction is the one prepared in card 1; card 2 holds its placeholder. Card 3 is "Today's Agenda" (card_type agenda): ONLY the agenda, read aloud as a
  short numbered list with the minutes for each item. No welcome, no introduction and no teaching in card 3.
- Then onboard: explain how the class and the track run (how to ask questions, the norms, where things live). Use only facts the SME gave in the
  additional resources or the opening kit. For any policy, score, date, link or contact you were not given, write [[FROM_SME: <what is needed>]]. Never invent them.
- Set expectations for the module: what it is for and what it is not.
- Then teach, from the agenda's first topics. Ask before you teach: put a question to learners in chat, then explain.
- Teach in tiny steps. After each new idea or rule, give a short "what will the output be?" check, then reveal it.
- Use quizzes more often than the minimum. When the idea is code, the quiz question holds two to four lines of code in
  a fenced block and asks "What will the output be?", with short options such as an output, a different output and
  "Error", the correct option, and one sentence on why each wrong option fails.
- Interaction is online: learners answer in chat. Use a chat poll where a script would say "show of hands".
- Do not teach anything from the next class. The only mention of it is one closing line in the recap.
- Keep the tone warm and conversational, with light humour. Keep jargon out until it is defined.
- Mention AI only if the agenda or the SME's resources mention it. In that case have a short honest conversation about
  how learners will and will not use it.
- Close with a short numbered summary of what was taught.
</program_opening>
"""

# What is specific to each track's opening class: its onboarding differs, its teaching does not
LEVEL_OPENING_NOTES = {
    "Beginner": ("Beginner opening: these learners are new to the program and to coding. Onboard them fully from the opening kit (the common terms, attendance, contests, "
                 "the module description and expectations, the FAQs, the dashboard walkthrough, the AI cards), kept short and friendly, then teach from the very beginning of the first topic."),
    "Intermediate": ("Intermediate opening: these learners know the platform from the Beginner Track, so onboard them for THIS track: what it covers and how it differs from the "
                     "Beginner Track, what is expected of them, the tools they need, and how they use AI here. Recall what they already know in a few lines, then move fast. "
                     "If the opening kit gives a pre-lecture message, the first card is that short message. Open the teaching with a story-led hook from a relatable company or "
                     "app that shows the problem in numbers before any definition."),
    "Advanced": ("Advanced opening: these learners are comfortable with the program, so onboard them for THIS track: set expectations (what the class is and what it is NOT), "
                 "give a checklist of the tools and accounts to set up before they need them, and say how they use AI here. Then teach the first vocabulary and topics, with a "
                 "short checkpoint quiz after each."),
}


def opening_level_note(d: Dict[str, Any]) -> str:
    """The part of the opening guidance that belongs to this track: the level's note, and the opening kit's own pattern when it has one."""
    level = d.get("level") or ("Beginner" if d.get("program_first_class") else "Intermediate")
    note = LEVEL_OPENING_NOTES.get(level, LEVEL_OPENING_NOTES["Intermediate"])
    return (f"<track_opening_note>\nTrack: {d.get('track') or 'this track'}. {note}\n"
            "Teach in the pattern the opening kit gives under teaching pattern or section pattern. With no kit, teach intuition, the obvious way, what it costs in numbers, "
            "the improvement, then the edge case. Before each reveal, ask the learners to predict or notice something in chat. "
            "An instructor note lists the topics of this module, taken only from the curriculum position in the context; write [[FROM_SME: module topics]] if they are not there.\n"
            "</track_opening_note>")


CONTEXT_RULES = """\
<context_rules>
C1 Anything listed as taught in this module may be used as known. Anything not listed is unknown to learners and must
   be taught before use.
C2 Anything listed as planned next must not be taught here.
C3 Earlier scripts are for intent and examples only. Never copy outdated APIs, versions or facts from them. Check them
   against the approved documentation.
C4 Never invent files, code, datasets or outputs from earlier classes. Use [[FROM_CLASS: <class title> | <artefact>]].
C5 If the agenda or learning outcomes are marked placeholder or not provided, say what you assumed.
C6 Program overrides, when present, win over the good script rules.
</context_rules>"""


def build_block_a_context(
    db: Session,
    class_id: int,
    duration_minutes: int,
    content_mix_beginner: int,
    code_ratio: int,
    custom_title: Optional[str] = None,
    custom_agendas: Optional[List[str]] = None,
    learning_outcomes: Optional[str] = None,
    api_docs: Optional[str] = None,
    reference_urls: Optional[str] = None,
    additional_resources: Optional[str] = None,
    previous_script_context: Optional[str] = None,
    view: str = "full",
    kit_focus: Optional[str] = None,
) -> str:
    """Block A as prompt text. view="compact" drops the past and upcoming agendas, for agents that only need titles.
    kit_focus is the text of the card being written; it decides which opening-kit facts are given in full."""
    data = gather_block_a(
        db, class_id,
        duration_minutes=duration_minutes, content_mix_beginner=content_mix_beginner, code_ratio=code_ratio,
        custom_title=custom_title, custom_agendas=custom_agendas, learning_outcomes=learning_outcomes,
        api_docs=api_docs, reference_urls=reference_urls, additional_resources=additional_resources,
        previous_script_context=previous_script_context, view=view,
    )
    return render_block_a(data, kit_focus) if data else ""


def build_canonical_agent_prompt(block_a_context: str, specific_task: str) -> str:
    """Block A, then Block B, then the agent's own task."""
    return f"""{block_a_context}

{BLOCK_B_GOOD_SCRIPT_RULES}

<agent_task>
{specific_task}
</agent_task>
"""
