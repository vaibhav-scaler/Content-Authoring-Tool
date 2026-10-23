"""
Agent 1: Outliner Agent
Proposes card-level outline for a class, grouped into sections.
Handles GTG / Edit / ReThink flows from SME.
Uses Claude Sonnet for LLM-backed outline generation.
"""

import os
import re
import json
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

from agents.shared_context import CARD_TYPES, LIMITS, TEACHING_CARD_TYPES, cards_for_duration

from agents.llm import call_llm_json, llm_configured

EXECUTION_MODE = os.getenv("EXECUTION_MODE", "hybrid")


# ── Outliner System Prompt Template ──────────────────────────────────────────

OUTLINER_SYSTEM = """\
You are the Outliner for Scaler's script authoring tool.
{block_a}
{block_b}
"""

OUTLINER_TASK = """\
TASK
Propose a card-level outline for {class_title}. Do not write card content yet.

STEPS
1. Map every agenda item and learning outcome to one or more cards.
2. Dynamically pick an authentic, relatable running system and analogies tailored to what this class's agenda covers.
   The running example and analogy do not have to be Swiggy; they can be ANY relatable everyday system that anyone can immediately understand
   (for example: UPI payment transfers, BookMyShow seat booking, WhatsApp messaging, train/bus reservation, grocery delivery, parcel courier tracking, or ATM withdrawals).
   STRICT REQUIREMENT: The analogy MUST come from everyday real-world life that any student or engineer encounters daily.
   NEVER use esoteric, academic, or abstract analogies (such as rockets, fighter jets, particle accelerators, or nuclear reactors).
   State clearly where each analogy breaks.
3. Plan the difficulty climb for Track: {track} (Beginner | Intermediate | Advanced) to the target outcome,
   honouring the content mix ({beginner_pct}% beginner / {advanced_pct}% advanced).
4. Compare against PAST_CLASSES and UPCOMING_CLASSES and flag overlap.
5. Group cards into sections of 2 to 5 cards each, for section-by-section
   drafting and review.
6. Plan {min_cards} to {max_cards} cards in total (rule S1). Allocate durations that sum exactly to {duration_min} minutes
   (i.e. {duration_sec} seconds), with each card inside its range: concept, problem, walkthrough, faq and recap
   {t_lo} to {t_hi} minutes, quiz {q_lo} to {q_hi}, instructor_note and agenda {n_lo} to {n_hi}.
   card_type must be one of: instructor_note, agenda, concept, problem, walkthrough, quiz, faq, recap.
7. QUIZ FIRST: a quiz card comes BEFORE the cards that teach its topic, never after. It asks the learner to think, and the next cards answer it.
   Give a quiz the same agenda_items as the cards it comes before.
8. SCOPE: teach only what the agenda and learning outcomes in the context ask for, and use the SME's additional
   resources and reference material where given. Every card names the agenda items and outcomes it serves, using their
   exact wording. Add nothing else. The class title, module name and track name are LABELS, not scope: a topic that is in the title
   but not in the agenda or outcomes is not taught. Every concept, problem and walkthrough card must serve at least one agenda item or outcome.
   Never plan a card about anything an UPCOMING_CLASSES entry lists in its AGENDA; the only mention of an upcoming class is one closing
   line inside the recap. An upcoming class's title is only a label: a topic that is in its title but not in its agenda is not off limits. When one of this class's agenda items
   is close to an item in a later class's agenda, keep to what this class's agenda asks for and teach nothing of the later item itself.
Keep every text field short: teaching_shape_note at most 15 words. A long answer will not fit, so brevity matters.

OUTPUT (JSON only)
{{
  "assumptions": [string],
  "running_example": string,
  "sections": [{{
    "section_id": string,
    "goal": string,
    "cards": [{{
      "card_id": string, "card_type": string, "title": string,
      "agenda_items": [string],
      "teaching_shape_note": string,
      "duration_sec": number
    }}]
  }}],
  "total_duration_sec": number
}}
agenda_items lists the exact agenda items or learning outcomes (their wording) that the card serves.
Visual cards are planned separately, so leave them out.
"""

RETHINK_ADDENDUM = """\
The SME chose ReThink.
RETHINK FEEDBACK:
{rethink_feedback}

Produce a STRUCTURALLY DIFFERENT outline, not a reworded one.
Change section grouping, card order, analogy, or running example.
"""

EDIT_ADDENDUM = """\
The SME chose Edit and returned the modified outline below.
EDITED OUTLINE:
{edited_outline}

Validate the edited outline against ALL Block B rules and Block A context.
Return it with any new overlap_flags or coverage_check warnings.
"""


FIX_ADDENDUM = """\
CHECKER FEEDBACK
Your previous outline failed these checks, which were run by code:
{problems}

Return a corrected outline. Keep everything that is already good and change only what the checks require.
"""


def generate_outline(
    class_title: str,
    agendas: List[str],
    duration_minutes: int,
    content_mix_beginner: int,
    code_ratio: int,
    memory_context: Dict[str, Any],
    block_a_context: Optional[str] = None,
    learning_outcomes: Optional[str] = None,
    additional_resources: Optional[str] = None,
    api_docs: Optional[str] = None,
    reference_urls: Optional[str] = None,
    rethink_feedback: Optional[str] = None,
    edited_outline: Optional[str] = None,
    fix_problems: Optional[List[str]] = None,
    level: str = "intermediate",
    track: Optional[str] = None,
    script_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Generates a structured outline grouped into sections.
    Returns the full Outliner JSON schema (sections, overlap_flags, coverage_check, etc.).
    Handles ReThink and Edit flows when feedback is provided.
    """
    if not llm_configured() or EXECUTION_MODE == "mock":
        from agents.llm import LLMUnavailable, mock_fallback_allowed
        if not mock_fallback_allowed():                       # no key: say so, rather than invent a sample outline
            raise LLMUnavailable("The AI is not configured, so the outline cannot be made. Add the AI key in backend/.env and restart. Sample text is never written in its place.")
        from agents.mock_provider import get_mock_outline
        flat_cards = get_mock_outline(
            class_title=class_title,
            duration_mins=duration_minutes,
            agendas=agendas,
            rethink_feedback=rethink_feedback,
            additional_resources=additional_resources
        )
        return _wrap_flat_cards_as_sections(flat_cards, class_title, duration_minutes)

    try:
        from agents.shared_context import OUTLINER_RULES as BLOCK_B_GOOD_SCRIPT_RULES

        # Build context header
        if block_a_context:
            context_header = block_a_context
        else:
            context_header = (
                "<context>\n"
                f"Class title: {class_title}\n"
                f"Duration: {duration_minutes} minutes\n"
                f"Agenda (nested): {json.dumps(agendas, indent=2)}\n"
                f"Learning outcomes: {learning_outcomes or 'Comprehensive mastery.'}\n"
                f"Content mix: {content_mix_beginner}% beginner, "
                f"{100 - content_mix_beginner}% advanced\n"
                f"Code to content ratio: {code_ratio}% Code\n"
                "</context>"
            )

        system_prompt = OUTLINER_SYSTEM.format(
            block_a=context_header,
            block_b=BLOCK_B_GOOD_SCRIPT_RULES
        )

        task_prompt = OUTLINER_TASK.format(
            class_title=class_title,
            track=track or "Core Track",
            level=level,
            beginner_pct=content_mix_beginner,
            advanced_pct=100 - content_mix_beginner,
            duration_min=duration_minutes,
            duration_sec=duration_minutes * 60,
            min_diagrams=LIMITS["min_diagrams"],
            min_cards=cards_for_duration(duration_minutes)[0], max_cards=cards_for_duration(duration_minutes)[1],
            t_lo=LIMITS["teaching_card_minutes"][0], t_hi=LIMITS["teaching_card_minutes"][1],
            q_lo=LIMITS["quiz_card_minutes"][0], q_hi=LIMITS["quiz_card_minutes"][1],
            n_lo=LIMITS["note_card_minutes"][0], n_hi=LIMITS["note_card_minutes"][1]
        )

        from agents.image_gen import without_image_option
        task_prompt = without_image_option(task_prompt)
        b_pct = max(0, min(100, int(content_mix_beginner)))
        task_prompt += (
            f"\n\nTHE SME'S PLAN (follow it): {b_pct}% beginner and {100 - b_pct}% advanced, and about {code_ratio}% of the cards carry code.\n"
            + ("- DIFFICULTY ARC: the class climbs. The first " + str(b_pct) + "% of the teaching minutes is beginner level (plain words, one small idea at a time), then one bridge card "
               "where the class steps up, then the remaining " + str(100 - b_pct) + "% at advanced level (edge cases, how it works inside, trade-offs). Order the cards from the basics to the depth: "
               "no advanced idea before the basics it needs.\n" if 0 < b_pct < 100 else
               ("- Everything stays at beginner level: no internals or edge cases beyond the basics.\n" if b_pct >= 100 else "- Everything is advanced level: the learners already know the basics.\n"))
            + f"- CODE: plan code on about {code_ratio}% of the cards, mostly walkthrough cards, spread through the class. Code comes after the idea it shows. "
              f"{'Put more of it in the advanced part.' if 0 < b_pct < 100 else ''}\n")
        n_anim = min_animations_for(track)
        if n_anim:
            task_prompt += (f"\n\nANIMATIONS: this is a beginner track, so at least {n_anim} of the visual cards must be kind \"animation\" "
                            "(a step-by-step change over time that a learner watches), for example a query running or a request travelling.")

        # Append ReThink or Edit addendum if applicable
        if rethink_feedback:
            task_prompt += "\n\n" + RETHINK_ADDENDUM.format(
                rethink_feedback=rethink_feedback
            )
        elif edited_outline:
            task_prompt += "\n\n" + EDIT_ADDENDUM.format(
                edited_outline=edited_outline
            )
        elif fix_problems:
            task_prompt += "\n\n" + FIX_ADDENDUM.format(problems="\n".join(f"- {p}" for p in fix_problems))

        # Whichever model the client is set to use (Claude / OpenRouter Sonnet by default) writes the outline.
        outline = call_llm_json(
            system=system_prompt,
            messages=[{"role": "user", "content": task_prompt}],
            max_tokens=4000,
            temperature=0.3,
            agent="outliner",
            script_id=script_id
        )

        outline = fill_card_defaults(outline)
        outline = _normalize_outline_duration(outline, duration_minutes)
        outline = normalize_visual_cards(outline)
        outline = ensure_visual_cards(outline, agendas, learning_outcomes, min_animations=min_animations_for(track))
        return ensure_animations(outline, min_animations_for(track))

    except Exception as e:
        from agents.llm import mock_fallback_allowed
        if not mock_fallback_allowed():
            raise      # a failed model call must be seen, not hidden behind sample content
        print(f"Warning: Outliner agent failed ({e}). Falling back to dynamic mock.")
        from agents.mock_provider import get_mock_outline
        flat_cards = get_mock_outline(
            class_title=class_title,
            duration_mins=duration_minutes,
            agendas=agendas,
            rethink_feedback=rethink_feedback,
            additional_resources=additional_resources
        )
        return _wrap_flat_cards_as_sections(flat_cards, class_title, duration_minutes)


VISUAL_PLANNER_TASK = """\
You plan the visual cards for a lecture outline. Do not change the cards.
Running example: {running_example}
Scope. Agenda: {agenda}. Learning outcomes: {outcomes}

CARDS (id, type, title, the agenda items it serves)
{cards}

Plan {need} to {most} visual cards (at least {need}). Each is attached to one teaching card (concept, walkthrough, problem or recap), at
most one per card, and only where a picture teaches better than words. At least one must branch or compare, and one
should be a recap that shows the whole class as one mental model.
- kind: "diagram" (flow, comparison or architecture), "animation" (a step-by-step change over time) or "image".
- It serves an agenda item or outcome, written in the exact words above, and shows nothing outside the scope.
- Use names from the running example. "prompt" is one or two sentences saying what to draw or animate.
  "elements" lists at most {max_nodes} labels.
- A "diagram" is drawn as a flowchart of boxes joined by arrows (at most {max_nodes} boxes in total, counting every box). It cannot draw axes, curves, grids, icons, markers
  or colours, so never ask for them; put the detail inside the text of the boxes. Anything that moves or changes over time is an "animation", not a diagram.

Return ONLY JSON:
{{"visual_cards": [{{"visual_id": "V01", "parent_card_id": string, "kind": string, "title": string,
  "serves_agenda_items": [string], "serves_outcomes": [string], "prompt": string, "elements": [string]}}]}}
"""


def min_animations_for(track: Optional[str]) -> int:
    """Beginner tracks need more to look at: at least 2 animations. Other tracks have no minimum."""
    return 2 if "beginner" in (track or "").lower() else 0


_MOTION = ("step", "sequence", "process", "flow", "trace", "query", "execut", "request", "cycle", "loop", "lifecycle", "journey", "pipeline",
           "handshake", "travel", "happens")
_STILL = ("compar", "versus", " vs ", "architecture", "recap", "mental model", "overview", "table", "difference")


def ensure_animations(outline: Dict[str, Any], minimum: int) -> Dict[str, Any]:
    """Make sure at least `minimum` visual cards are animations. When the model planned too few, the diagrams that describe a process
    over time (steps, flows, queries) are turned into animations; comparisons, architectures and recaps stay diagrams. No model call."""
    visuals = outline.get("visual_cards") or []
    have = sum(1 for v in visuals if v.get("kind") == "animation")
    if minimum <= 0 or have >= minimum:
        return outline

    def score(v):
        text = f" {v.get('title', '')} {v.get('prompt', '')} ".lower()
        return sum(w in text for w in _MOTION) - 2 * sum(w in text for w in _STILL)

    candidates = sorted((v for v in visuals if v.get("kind") != "animation"), key=score, reverse=True)
    converted = []
    for v in candidates[:minimum - have]:
        v["kind"] = "animation"
        converted.append(v.get("visual_id"))
    if converted:
        outline.setdefault("auto_adjustments", []).append(f"Turned visual card(s) {', '.join(map(str, converted))} into animations (beginner tracks need at least {minimum}).")
    return outline


def ensure_visual_cards(outline: Dict[str, Any], agendas: Optional[List[str]], outcomes: Optional[str], min_animations: int = 0) -> Dict[str, Any]:
    """When the outline came back with fewer visual cards than the rules need, plan them in a separate small request.
    The big outline request sometimes skips them when its prompt is large; this one only has that job."""
    existing = outline.get("visual_cards") or []
    need = LIMITS["min_diagrams"] - len(existing)
    cards = [c for sec in outline.get("sections", []) for c in sec.get("cards", [])]
    if need <= 0 or not cards or not (llm_configured() and EXECUTION_MODE != "mock"):
        return outline
    taken = {v.get("parent_card_id") for v in existing}
    listing = [{"id": c.get("card_id"), "type": c.get("card_type"), "title": c.get("title"),
                "agenda_items": c.get("agenda_items") or []} for c in cards if c.get("card_id") not in taken
               and str(c.get("card_type")) in ("concept", "walkthrough", "problem", "recap")]
    if not listing:
        return outline
    prompt = VISUAL_PLANNER_TASK.format(
        running_example=outline.get("running_example") or "none", agenda="; ".join(agendas or []) or "derive from the class title",
        outcomes=(outcomes or "").strip() or "derive from the class title", cards=json.dumps(listing, separators=(",", ":")),
        need=need, most=max(need, min(need + 2, len(listing))), max_nodes=LIMITS["max_diagram_nodes"])
    from agents.image_gen import without_image_option
    prompt = without_image_option(prompt)
    if min_animations:
        prompt += f"\nThis is a beginner track: at least {min_animations} visual cards in total must be kind \"animation\"."
    try:
        planned = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=1800, temperature=0.3, agent="visual_planner")
    except Exception as e:           # the outline stays valid without them; its checks will say visuals are missing
        print(f"Visual planner skipped: {e}")
        return outline
    added = [v for v in (planned.get("visual_cards") if isinstance(planned, dict) else []) or [] if isinstance(v, dict)
             and v.get("parent_card_id") not in taken]
    outline["visual_cards"] = list(existing) + added
    return normalize_visual_cards(outline)


VISUAL_KIND_TO_TYPE = {"diagram": "mermaid", "mermaid": "mermaid", "animation": "animation", "image": "image"}


def fill_card_defaults(outline: Dict[str, Any]) -> Dict[str, Any]:
    """The Outliner writes only the essentials of each card, to fit a small token budget. The rest is filled in here."""
    n = 0
    for sec in outline.get("sections", []):
        for c in sec.get("cards", []):
            n += 1
            c.setdefault("card_id", f"C{n:02d}")
            c.setdefault("agenda_items", [])
            c.setdefault("outcomes", [])
            c.setdefault("analogy", None)
            c.setdefault("predict_moment", False)
            c.setdefault("visuals_planned", [])
            c.setdefault("teaching_shape_note", "")
    outline.setdefault("overlap_flags", [])
    return outline


def normalize_visual_cards(outline: Dict[str, Any]) -> Dict[str, Any]:
    """Make the visual_cards list safe to build on: every entry points at a real card, has a known kind, at most 7
    elements and an agenda or outcome it serves (inherited from its parent card when the model left it empty), and
    there are no more than one per two cards. Each card's visuals_planned lists the ids of its visuals."""
    cards = {c.get("card_id"): c for s in outline.get("sections", []) for c in s.get("cards", [])}
    for c in cards.values():             # a made-up card type (for example "closing") becomes a concept card
        c["card_type"] = str(c.get("card_type") or "concept").lower().replace(" ", "_")
        if c["card_type"] not in CARD_TYPES:
            c["card_type"] = "concept"
    raw = outline.get("visual_cards")
    visuals, seen = [], set()
    for v in raw if isinstance(raw, list) else []:
        if not isinstance(v, dict) or v.get("parent_card_id") not in cards:
            continue
        parent = cards[v["parent_card_id"]]
        vid = str(v.get("visual_id") or f"V{len(visuals) + 1:02d}")
        if vid in seen:
            vid = f"V{len(visuals) + 1:02d}"
        seen.add(vid)
        kind = str(v.get("kind") or "diagram").lower()
        from agents.image_gen import images_enabled
        if kind == "image" and not images_enabled():
            kind = "diagram"                 # pictures are switched off: draw it as a flow diagram instead
        visuals.append({
            "visual_id": vid,
            "parent_card_id": v["parent_card_id"],
            "kind": kind if kind in VISUAL_KIND_TO_TYPE else "diagram",
            "title": v.get("title") or parent.get("title") or vid,
            "serves_agenda_items": v.get("serves_agenda_items") or parent.get("agenda_items") or [],
            "serves_outcomes": v.get("serves_outcomes") or parent.get("outcomes") or [],
            "prompt": v.get("prompt") or v.get("title") or "",
            "elements": [str(e) for e in (v.get("elements") or [])][:LIMITS["max_diagram_nodes"]],
            "status": "planned",
        })
    limit = max(LIMITS["min_diagrams"], (len(cards) + 1) // 2)
    visuals = visuals[:limit]
    outline["visual_cards"] = visuals
    for c in cards.values():
        c["visuals_planned"] = [v["visual_id"] for v in visuals if v["parent_card_id"] == c.get("card_id")]
    return outline


INTRO_TITLE = "Instructor Introduction"


def merge_opening_cards(outline: Dict[str, Any], target_minutes: int, program_opening: bool = True) -> Dict[str, Any]:
    """Every class opens the same way: the mandatory setup, then three cards. Card 1 is a short instructor note "Instructor Introduction" (the instructor
    prepares to introduce themselves). Card 2 is "Welcome & Introductions" (card_type welcome): the spoken welcome and the introduction. Card 3 is
    "Today's Agenda" (card_type agenda): only the agenda with its times. The Outliner's own first note card becomes card 1 and an agenda card it made
    becomes card 3 (it keeps the agenda items); whatever of the three is missing is made. Safe to run twice."""
    sections = outline.get("sections") or []
    if not sections:
        return outline
    cards = [c for s_ in sections for c in s_.get("cards", [])]
    if not cards:
        return outline
    host_cards = sections[0].setdefault("cards", [])

    def put_after(anchor, card):
        for s_ in sections:
            if anchor in s_.get("cards", []):
                s_["cards"].insert(s_["cards"].index(anchor) + 1, card)
                return
        host_cards.insert(0, card)
    first = cards[0]
    if first.get("card_type") != "instructor_note":
        first = {"card_id": "INTRO", "card_type": "instructor_note", "title": INTRO_TITLE, "duration_sec": 60, "agenda_items": [], "outcomes": []}
        host_cards.insert(0, first)
    first["title"], first["duration_sec"] = INTRO_TITLE, 60
    first["teaching_shape_note"] = "A note for the instructor, written by the system: prepare your introduction; you give it right after the welcome."
    carried = list(first.get("agenda_items") or [])
    first["agenda_items"] = []
    cards = [c for s_ in sections for c in s_.get("cards", [])]
    idx = cards.index(first)
    rest = cards[idx + 1: idx + 3]
    welcome = next((c for c in rest if c.get("card_type") == "welcome"), None)
    agenda = next((c for c in rest if c.get("card_type") == "agenda"), None)
    welcome_title = "Welcome & Introductions" if program_opening else "Welcome back"
    if welcome is None:
        welcome = {"card_id": "WELCOME", "card_type": "welcome", "title": welcome_title, "duration_sec": 120, "agenda_items": [], "outcomes": []}
        put_after(first, welcome)
    if agenda is None:
        agenda = {"card_id": "AGENDA", "card_type": "agenda", "title": "Today's Agenda", "duration_sec": 90, "agenda_items": [], "outcomes": []}
    else:
        for s_ in sections:                                   # the agenda card comes straight after the welcome, wherever the Outliner put it
            if agenda in s_.get("cards", []):
                s_["cards"].remove(agenda)
    put_after(welcome, agenda)
    welcome["title"], welcome["card_type"] = welcome_title, "welcome"
    welcome["agenda_items"] = []
    welcome["teaching_shape_note"] = ("The approved spoken welcome, a first question in chat, then the instructor's introduction." if program_opening
                                      else "A warm welcome back and a few sentences on where the last class stopped.")
    agenda["title"] = "Today's Agenda"
    agenda["duration_sec"] = min(float(agenda.get("duration_sec") or 90), 120)
    agenda["teaching_shape_note"] = "Only the agenda: read aloud as a short numbered list, each item with about how many minutes it takes."
    agenda["agenda_items"] = list(dict.fromkeys(carried + list(agenda.get("agenda_items") or [])))
    adj = outline.setdefault("auto_adjustments", [])
    note = "The class opens with the instructor note (Instructor Introduction), the spoken Welcome & Introductions card, then a card with only the agenda."
    if note not in adj:
        adj.append(note)
    return _normalize_outline_duration(outline, target_minutes)


# ---- the SME's inputs, turned into a plan for every card -------------------------------------------------------------------------------------
LEVEL_CARD_TYPES = ("concept", "problem", "walkthrough")      # the cards that teach, so the ones that have a level and can carry code
LEVELS = ("beginner", "bridge", "advanced")


def _cards_in_order(outline: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [c for s_ in outline.get("sections", []) for c in s_.get("cards", [])]


def plan_levels(outline: Dict[str, Any], beginner_pct: Optional[int]) -> Dict[str, Any]:
    """Give every teaching card a level so the class climbs from beginner to advanced in the SME's proportion, measured in minutes.
    The first `beginner_pct` percent of the teaching minutes is beginner, the card that crosses the boundary is the one bridge (where the class steps
    up), the rest is advanced. 100 means all beginner, 0 means all advanced. A quiz takes the level of the card before it; notes, agenda, FAQ and
    recap have none. The order of the cards is not changed: the Outliner is told to order them from basics to depth, and the Writer is told each
    card's level."""
    cards = _cards_in_order(outline)
    teaching = [c for c in cards if c.get("card_type") in LEVEL_CARD_TYPES]
    for c in cards:
        c.pop("level", None)
    total = sum(float(c.get("duration_sec") or 0) for c in teaching)
    if beginner_pct is None or not teaching or total <= 0:
        return outline
    b = max(0, min(100, int(beginner_pct))) / 100.0
    elapsed = 0.0
    for c in teaching:
        start, end = elapsed / total, (elapsed + float(c.get("duration_sec") or 0)) / total
        elapsed += float(c.get("duration_sec") or 0)
        if b >= 0.999 or end <= b + 1e-9:
            c["level"] = "beginner"
        elif b <= 0.001 or start >= b - 1e-9:
            c["level"] = "advanced"
        else:
            c["level"] = "bridge"                                     # this card crosses the boundary
    last = None
    for c in cards:                                                   # a quiz tests what was just taught, so it has that card's level
        if c.get("card_type") in LEVEL_CARD_TYPES:
            last = c.get("level")
        elif c.get("card_type") == "quiz" and last:
            c["level"] = last
    return outline


def _spread(items: List[Any], k: int) -> List[Any]:
    """k of the items, evenly spaced from first to last."""
    if k <= 0 or not items:
        return []
    if k >= len(items):
        return list(items)
    if k == 1:
        return [items[len(items) // 2]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


_CODE_WORDS = re.compile(r"\b(query|queries|sql|select|code|coding|program|programs|script|function|functions|variable|variables|loop|loops|print|syntax|command|commands|install|"
                         r"index|indexes|indices|join|joins|table|tables|schema|insert|python|java|javascript|trace|debug|debugging|error|errors|compile|api|class|classes|method|"
                         r"array|arrays|list|string|strings|algorithm|implement|build|b-tree|run|execute|statement)\b", re.I)
_ORIENT_WORDS = re.compile(r"\b(welcome|agenda|career|careers|expectation|expectations|dashboard|journey|close|closing|recap|roadmap|motivation|story|history|hook|session|"
                           r"introduction to the program|what is dsml)\b", re.I)


def _code_fit(card: Dict[str, Any]) -> int:
    """How well a teaching card suits having code: walkthroughs and problems most, a concept card that is about something you can run, then the rest;
    a card about the program, careers or expectations least. Judged from the card's type, title, note and agenda items."""
    text = " ".join([card.get("title") or "", card.get("teaching_shape_note") or ""] + [str(x) for x in (card.get("agenda_items") or [])])
    score = {"walkthrough": 4, "problem": 3}.get(card.get("card_type"), 0)
    score += 2 * min(2, len(_CODE_WORDS.findall(text)))
    if _ORIENT_WORDS.search(text) and not _CODE_WORDS.search(text):
        score -= 3
    if card.get("level") in ("advanced", "bridge"):               # the deeper part of a class carries more of its code
        score += 1
    return score


def plan_code(outline: Dict[str, Any], code_ratio: Optional[int]) -> Dict[str, Any]:
    """Decide exactly which cards carry code, so the share matches the SME's ratio. The target is that percent of ALL cards (the rule checker measures it
    the same way). Only teaching cards can carry code. Cards are taken best-suited first (see _code_fit), and within the same suitability evenly spread
    through the class. The Writer is told, per card, whether it must have code or must not."""
    cards = _cards_in_order(outline)
    for c in cards:
        c["has_code"] = False
    if code_ratio is None or not cards:
        return outline
    target = round(max(0, min(100, int(code_ratio))) / 100.0 * len(cards))
    eligible = [c for c in cards if c.get("card_type") in LEVEL_CARD_TYPES]
    tiers: Dict[int, List[Dict[str, Any]]] = {}
    for c in eligible:
        tiers.setdefault(_code_fit(c), []).append(c)
    chosen: List[Dict[str, Any]] = []
    for score in sorted(tiers, reverse=True):
        if len(chosen) >= target:
            break
        chosen += _spread(tiers[score], min(len(tiers[score]), target - len(chosen)))
    for c in chosen:
        c["has_code"] = True
    return outline


def quiz_before_topic(outline: Dict[str, Any]) -> Dict[str, Any]:
    """A quiz asks first and the teaching answers it. A quiz card that sits after the teaching cards it tests moves to just before the first of them
    (the run of concept, problem and walkthrough cards right before it that serve the same agenda items). Sections keep their sizes and every card keeps
    its time. Only call this when no card of the script is written yet, because card numbers change."""
    if not isinstance(outline, dict) or not outline.get("sections"):
        return outline
    sizes = [len(s_.get("cards", [])) for s_ in outline["sections"]]
    cards = _cards_in_order(outline)

    def items(c):
        return {str(a).strip().lower() for a in (c.get("agenda_items") or []) if str(a).strip()}
    q = 0
    while q < len(cards):
        if cards[q].get("card_type") == "quiz":
            mine, j = items(cards[q]), q
            while j > 0 and cards[j - 1].get("card_type") in ("concept", "problem", "walkthrough") and mine & items(cards[j - 1]):
                j -= 1
            if j < q:
                cards.insert(j, cards.pop(q))
        q += 1
    n = 0
    for s_, size in zip(outline["sections"], sizes):
        s_["cards"] = cards[n:n + size]
        n += size
    return outline


def finalize_plan(outline: Dict[str, Any], beginner_pct: Optional[int], code_ratio: Optional[int], reorder: bool = True) -> Dict[str, Any]:
    """Apply the SME's beginner/advanced split and code ratio to the finished outline. Run it after the card list is final (after any merge or edit).
    `reorder` puts each quiz before the topic it tests; turn it off when cards are already written."""
    if not isinstance(outline, dict) or not outline.get("sections"):
        return outline
    if reorder:
        quiz_before_topic(outline)
    plan_levels(outline, beginner_pct)
    plan_code(outline, code_ratio)
    cards = _cards_in_order(outline)
    minutes = {lv: round(sum(float(c.get("duration_sec") or 0) for c in cards if c.get("level") == lv and c.get("card_type") in LEVEL_CARD_TYPES) / 60.0, 1) for lv in LEVELS}
    outline["plan_summary"] = {"beginner_pct": beginner_pct, "advanced_pct": None if beginner_pct is None else 100 - int(beginner_pct), "code_ratio": code_ratio,
                               "coded_cards": sum(1 for c in cards if c.get("has_code")), "cards": len(cards), "level_minutes": minutes}
    return outline


def _displayed_concept(card: Dict[str, Any]) -> str:
    """What the outline screen shows as a card's 'key concept'. A save sends it back, and only a change from this is the SME's edit."""
    shown = ", ".join(card.get("agenda_items") or []) or card.get("teaching_shape_note") or card.get("title", "")
    return card.get("title", "") if shown == "Why -> What -> How -> Where -> What breaks" else shown


def apply_flat_edits(outline: Dict[str, Any], edits: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold the outline screen's edits (a flat list: card_index, title, duration_mins, pedagogical_phase, key_concept, visual_type) into
    the structured outline, so everything the Writer is prompted with survives: sections, running example, agenda and outcome mapping,
    visual cards. Only what the SME changed is applied: a new title, time or card type, their own words for the card (which become the
    card's teaching note, so the Writer follows them), or 'none' to drop its visual."""
    cards = [c for s_ in outline.get("sections", []) for c in s_.get("cards", [])]
    for e in edits:
        try:
            card = cards[int(e["card_index"]) - 1]
        except (KeyError, TypeError, ValueError, IndexError):
            continue
        if e.get("title") and e["title"].strip() != card.get("title"):
            card["title"] = e["title"].strip()[:200]
        if e.get("duration_mins"):
            secs = int(round(float(e["duration_mins"]) * 60))
            if secs > 0 and round(secs / 60.0, 1) != round((card.get("duration_sec") or 0) / 60.0, 1):     # the screen shows tenths of a minute
                card["duration_sec"] = secs
        phase = str(e.get("pedagogical_phase") or "")
        if phase and phase.lower().replace(" ", "_") != str(card.get("card_type") or "").lower():
            norm = phase.lower().replace(" ", "_")
            card["card_type"] = norm if norm in CARD_TYPES else _phase_to_card_type(phase)
        concept = (e.get("key_concept") or "").strip()
        if concept and concept != _displayed_concept(card):
            card["teaching_shape_note"] = concept[:600]
        if str(e.get("visual_type") or "").lower() == "none":
            outline["visual_cards"] = [v for v in outline.get("visual_cards") or [] if v.get("parent_card_id") != card.get("card_id")]
            card["visuals_planned"] = []
    if outline.get("visual_cards") is not None:
        outline = normalize_visual_cards(outline)
    return outline


def enforce_card_count(outline: Dict[str, Any], target_minutes: int) -> Dict[str, Any]:
    """When the outline still has more cards than the class length allows (rule S1), merge the weakest cards into a neighbour until it fits.
    A merged card's agenda items, outcomes and visual move to the card that takes it over, so nothing in the scope is lost. Never touches
    the first two cards or the closing recap and FAQ, and keeps enough quizzes. Durations are rebalanced afterwards. No model call."""
    lo, hi = cards_for_duration(target_minutes)
    sections = outline.get("sections", [])
    flat = lambda: [(si, ci, c) for si, s_ in enumerate(sections) for ci, c in enumerate(s_.get("cards", []))]
    notes = []
    for _ in range(60):
        cards = flat()
        if len(cards) <= hi:
            break
        quizzes = sum(1 for _, _, c in cards if c.get("card_type") == "quiz")
        protected = {0, 1, len(cards) - 1, len(cards) - 2}
        scored = []
        for n, (si, ci, c) in enumerate(cards):
            kind = c.get("card_type")
            if n in protected or kind in ("instructor_note", "welcome", "agenda", "recap", "faq") or (kind == "quiz" and quizzes <= LIMITS["min_quizzes"]):
                continue
            weakness = -len(c.get("agenda_items") or []) - len(c.get("outcomes") or []) - (2 if c.get("predict_moment") else 0) \
                       - (3 if kind == "quiz" else 0) \
                       - (1 if any(v.get("parent_card_id") == c.get("card_id") for v in outline.get("visual_cards") or []) else 0) \
                       - (c.get("duration_sec") or 300) / 600.0
            scored.append((weakness, n, si, ci, c))
        if not scored:
            break
        _, n, si, ci, weak = max(scored, key=lambda t: t[0])
        # the neighbour that takes it over: the previous teaching card in the same section, else the next card
        sibling = sections[si]["cards"]
        into = sibling[ci - 1] if ci > 0 and sibling[ci - 1].get("card_type") in TEACHING_CARD_TYPES else (sibling[ci + 1] if ci + 1 < len(sibling) else cards[n - 1][2])
        for key in ("agenda_items", "outcomes"):
            into[key] = list(dict.fromkeys(list(into.get(key) or []) + list(weak.get(key) or [])))
        into["teaching_shape_note"] = (into.get("teaching_shape_note") or "").rstrip(". ") + f". Also cover: {weak.get('title')}."
        into["duration_sec"] = (into.get("duration_sec") or 300) + (weak.get("duration_sec") or 300)
        for v in list(outline.get("visual_cards") or []):
            if v.get("parent_card_id") == weak.get("card_id"):
                if any(o.get("parent_card_id") == into.get("card_id") for o in outline["visual_cards"]):
                    outline["visual_cards"].remove(v)
                else:
                    v["parent_card_id"] = into.get("card_id")
        sibling.remove(weak)
        sections[:] = [s_ for s_ in sections if s_.get("cards")]
        notes.append(f"Merged \"{weak.get('title')}\" into \"{into.get('title')}\" to fit {hi} cards.")
    if notes:
        outline.setdefault("auto_adjustments", []).extend(notes)
        outline = _normalize_outline_duration(outline, target_minutes)
        outline = normalize_visual_cards(outline)
    return outline


def _normalize_outline_duration(outline: Dict[str, Any], target_duration_mins: int) -> Dict[str, Any]:
    """Make card durations sum to the class duration, with every card inside the range for its type (rule S1)."""
    all_cards = [c for s in outline.get("sections", []) for c in s.get("cards", [])]
    if not all_cards:
        return outline
    from agents.moderator import rebalance_durations
    rows = [{"pedagogical_phase": c.get("card_type"), "duration_mins": (c.get("duration_sec") or 300) / 60.0} for c in all_cards]
    rebalance_durations(rows, target_duration_mins)
    for card, row in zip(all_cards, rows):
        card["duration_sec"] = int(round(row["duration_mins"] * 60))
    total = sum(c["duration_sec"] for c in all_cards)
    outline["total_duration_sec"] = total
    return outline


def _wrap_flat_cards_as_sections(
    flat_cards: List[Dict[str, Any]],
    class_title: str,
    duration_minutes: int
) -> Dict[str, Any]:
    """Wraps legacy flat card list into the new section-based Outliner schema."""
    sections = []
    section_cards = []
    section_idx = 1

    for i, card in enumerate(flat_cards):
        card_entry = {
            "card_id": f"C{i+1:02d}",
            "card_type": _phase_to_card_type(card.get("pedagogical_phase", "concept")),
            "title": card.get("title", f"Card {i+1}"),
            "agenda_items": [card.get("key_concept", "")],
            "outcomes": [],
            "teaching_shape_note": card.get("key_concept") or card.get("concept") or card.get("title", ""),
            "analogy": None,
            "predict_moment": card.get("pedagogical_phase") == "Quiz",
            "visuals_planned": [card.get("visual_type", "none")] if card.get("visual_type", "none") != "none" else [],
            "duration_sec": int(card.get("duration_mins", 5.0) * 60)
        }
        section_cards.append(card_entry)

        # Group into sections of 3-4 cards
        if len(section_cards) >= 4 or i == len(flat_cards) - 1:
            sections.append({
                "section_id": f"S{section_idx:02d}",
                "goal": f"Section {section_idx}: {section_cards[0]['title']} through {section_cards[-1]['title']}",
                "cards": section_cards
            })
            section_cards = []
            section_idx += 1

    total_sec = sum(
        c["duration_sec"]
        for s in sections
        for c in s["cards"]
    )

    from agents.memory_engine import _infer_domain_anchor_from_title
    running_example = _infer_domain_anchor_from_title(class_title)

    return {
        "assumptions": [f"Outline for {class_title}"],
        "running_example": running_example,
        "sections": sections,
        "overlap_flags": [],
        "coverage_check": {"agenda_items_unmapped": [], "outcomes_unmapped": []},
        "rule_counts_planned": {
            "predict_moments": 3,
            "tricky_quizzes": 2,
            "diagrams": 4,
            "traces": 2
        },
        "total_duration_sec": total_sec
    }


def _phase_to_card_type(phase: str) -> str:
    """Maps legacy pedagogical_phase to card_type enum."""
    mapping = {
        "Instructor Note": "instructor_note",
        "Metadata & Lock": "instructor_note",
        "Agenda": "agenda",
        "Welcome": "welcome",
        "Concept": "concept",
        "Everyday Analogy": "concept",
        "Why/What": "concept",
        "Failure Mode": "problem",
        "Code Along": "walkthrough",
        "Code Trace": "walkthrough",
        "Tracing": "walkthrough",
        "Quiz": "quiz",
        "FAQ": "faq",
        "Recap": "recap",
    }
    return mapping.get(phase, "concept")
