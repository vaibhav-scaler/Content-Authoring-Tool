"""
Agent 2: Writer Agent
Writes cards section-by-section from the approved outline.
Maintains a term register for continuity across sections.
Supports inline assist mode for targeted rewrites.
Uses Claude Sonnet for LLM-backed card generation.
"""

import os
import json
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

from agents.llm import call_llm_chat, call_llm_json, llm_configured

EXECUTION_MODE = os.getenv("EXECUTION_MODE", "hybrid")


# ── Writer System Prompt Template ────────────────────────────────────────────

WRITER_SYSTEM = """\
You are the Writer for Scaler's script authoring tool.
{block_a}
{block_b}
"""

WRITER_CARD_TASK = """\
INPUTS
Class outline, titles only (for orientation): {outline_overview}
Section goal: {section_goal}
Running example: {running_example}
CARD TO WRITE NOW, follow this spec: {card_spec}
Cards already written, newest last (continuity only; do not repeat them): {written_so_far}
Terms already defined in this script: {term_register}
{continuity}
{reference_excerpts}

TASK
Write exactly one card: the card above, in the card_format, obeying every rule in the good script rules that applies to
its card_type.
- SCOPE: teach only what this card's agenda_items and outcomes ask for, in the depth the spec gives. Use the SME's
  additional resources and reference material in the context for facts and examples. Add nothing outside the agenda
  and outcomes, and nothing from the upcoming classes.
- WELCOME CARD: when the card_type is welcome, the card is the spoken welcome and the introduction placeholder, nothing else: no agenda and no teaching.
- AGENDA CARD: when the spec has "agenda_timing", this is the agenda card and it holds ONLY the agenda. One sentence that introduces it, then the class agenda read
  aloud as a short numbered list: every item in agenda_timing, in that order, each with its minutes in brackets after the item, for example "Why SQL now (14 minutes)", never written as "about N minutes". Use exactly those items and minutes, add none
  and drop none, then one sentence that moves on to the first topic. No welcome, no introduction, no learning outcomes and no teaching.
- LEVEL: the spec's "level" says how deep this card goes. beginner: plain words, define every term, one small step at a time, a simple everyday example before any
  definition, no internals. bridge: this is where the class steps up: say so in one sentence, recap the simple version, then add the first deeper idea. advanced:
  the learners have the basics, so go deeper: edge cases, how it works inside, trade-offs, failure modes, numbers. Put the deeper material under a "### Going deeper"
  sub-heading, so a beginner can skim it. A card with no level is housekeeping (agenda, FAQ, recap).
- CODE: the spec's "has_code" is the plan. true: the card MUST include runnable code (code_snippet), and the spoken text walks through it. false: return code_snippet as null and
  teach with words and diagrams. The code ratio the SME asked for depends on this.
- CONTINUITY: this card is read straight after the one before it, in one live class. OPEN by picking up exactly where the previous card
  ended (its closing words are given above): one or two spoken sentences that name what was just covered and say what this card does
  next, for example "Alright, so we have seen X. Now let's look at Y." Never greet again, never say "in this card" or "in this section", never
  restate the previous card, and never introduce its example again. CLOSE with one short spoken sentence that leads into the next card
  (given above), for example "Let's see that in action." If the next card is a quiz, lead into it with the topic still unexplained ("Before we explain this, a quick question"). The
  last card of the class closes the class instead. The opening and the closing count in the word budget.
- VOICE: the body is the exact words the instructor will say aloud. Write "I" and "we" speaking to "you". Never write a direction to
  the instructor ("Greet everyone", "Ask the learners to...", "Pause and watch the chat") and never speak of "the learners". The only
  text that is not spoken is a short bracketed cue of at most 8 words, such as [pause for replies].
- FORMAT: light markdown only: "### " sub-headings, "- " bullets, "1. " numbered lists, bold terms, and "> **Key:** ..." callouts. No "---" divider
  lines, no tables, and no em dashes. Short paragraphs of 2 to 4 sentences.
- LINKS: give a link only when you need one, write it as [what it is](https://full-address) (the page shows the source beside it), use only
  addresses from the approved sources and the opening kit, and add the same address to "sources" with its source name. Never invent an address.
- Use the running example with identical names. Introduce no new running example.
- Never write generator or production wording in the spoken text: no "option index", "zero-based", "quiz JSON", "Visual V01", "UNVERIFIED" or notes about how the script was made. The instructor reads only what is meant to be said.
- Keep every term one thing all the way through: if a card calls SQL a language, no other card calls it a tool. Define a term once, in the card that introduces it, and do not repeat the definition in later cards.
- Never write a year or "current release" next to a version unless a source in this card says so; say "a recent version" instead.
- A spoken question and the quiz on screen under it must be the same question.
- A Predict callout puts the question on its own line and each option (A, B, C) on its own line below it, never run together in one line.
- Every time given for an agenda item must add up to the class length, and a card never says "Hour 2" or similar unless the clock is there.
- Use only terms listed as defined, terms from past classes, or terms you define in this card before using them.
- QUIZ FIRST: a quiz card comes BEFORE its topic is explained. Ask it so a learner can reason to an answer from everyday sense or from what earlier cards taught,
  use no term the class has not defined yet, and write the explanation as the answer the next card will teach. The card AFTER a quiz opens by giving the answer
  (name the right option and why) and then explains the topic.
- quiz card: return quiz_json that is easy to read aloud and on screen. question: one or two short sentences (any code goes in a fenced block);
  options: exactly 4, each one short line of at most 12 words, written WITHOUT a letter in front (the page adds A to D); correct_option_index;
  explanation: one or two sentences on why the right option is right; distractor_explanations: one short sentence (at most 20 words) for EACH
  wrong option, keyed by its letter A to D, saying why a learner might pick it and why it is wrong. Make the wrong options tempting, not silly.
- walkthrough or code card: runnable code, the platform and version, what success looks like, and the likely failure.
  The code is RUN by the system and must work as written: no input(), no network, no files, no installs. For SQL, the snippet
  must create and fill its own small tables first (CREATE TABLE, then INSERT with 4 to 8 rows), then run the query on them, in plain SQL
  that SQLite also accepts, so it can be run and its real output shown. Never query a table that the snippet does not create, and never
  use a project or dataset prefix (like dataset.table). If the class teaches a specific database, name it in the platform comment. If a snippet is
  meant to fail so learners can see an error, set "code_expected_error" to the error's name (for example "NameError");
  otherwise leave it null.
- VISUALS: the spec's "visuals" lists the visual cards planned for this card. Put one placeholder
  [[VISUAL: <visual_id>]] in the body at the point it helps, for each of them, using the exact visual_id. Invent no
  other visual. If "visuals" is empty, use no placeholder. Never put a table, raw HTML or an image in the body.
- SOURCES: for every API, command, flag, version or statistic you state, add an entry to "sources" with the claim,
  the source name (for example "PostgreSQL 16 documentation: EXPLAIN"), search_terms that find it, and "url": the
  official documentation page you believe states it (leave it out if you are unsure). Every source is fetched from the
  internet and checked against your claim, so a wrong URL is simply dropped, but a claim the page does not support is
  marked unverified. State only what the REFERENCE EXCERPTS and the approved sources support.
- LENGTH: the card runs {duration_sec} seconds, so write about {target_words} spoken words and never more than {max_words}. Code, quiz and callout text
  count. Cover EVERY agenda item and outcome in the spec inside that space: one clear idea, one example, one check each. Spend words on the
  idea, not on repeats, recaps inside the card, extra examples or restating a point. A card that runs over its time is a failure; so is a
  card that skips one of its topics.
- Cite approved documentation for every API or command you teach. Label anything else 'UNVERIFIED: needs SME validation'.

OUTPUT (JSON only, no text outside it)
{{
  "cards": [{{
    "card_id": string, "card_type": string, "title": string, "description": string, "duration_sec": number,
    "body_markdown": string, "instructor_note": string, "code_snippet": string or null, "code_language": string or null,
    "code_expected_error": string or null,
    "sources": [{{"claim": string, "source": string, "search_terms": string, "url": string}}],
    "quiz_json": {{"question": string, "options": [string], "correct_option_index": int, "explanation": string,
                   "distractor_explanations": {{"A": string}}}} or null
  }}],
  "references": [{{"url": string, "title": string, "retrieved": "YYYY-MM-DD", "approved": boolean}}],
  "new_terms": [{{"term": string}}],
  "visual_requests": [{{"id": string, "type": "mermaid | animation | image", "intent": string, "nodes": [string], "card_id": string}}],
  "self_check": {{"rules_at_risk": [{{"rule": string, "reason": string}}]}}
}}
"""

WRITER_SECTION_TASK = """\
INPUTS
Approved outline: {approved_outline}
Section to write now: {section_id}
Already approved sections (for continuity): {approved_sections}
Terms already introduced in this script: {term_register}

TASK
Write every card in {section_id}, following the outline and card_format exactly.

CRITICAL PEDAGOGICAL REQUIREMENTS:
- CONTINUITY: the cards are read one after another in a live class. Each card opens by picking up where the previous one ended ("Alright, we have just seen X. Now let's...")
  and closes with one short sentence leading into the next. Never greet again after the first card.
- LENGTH: each card holds about 90 spoken words per minute of its duration (a 5 minute card, about 450 words; never more than 15% over). Cover every
  agenda item and outcome the card serves inside that space; cut repeats, recaps and extra examples. Running over time is a failure.
- DO NOT just provide dry theory! Every concept card must weave together explanations, visual models, code/steps, and interactive checkpoint moments.
- VISUAL ASSETS (MANDATORY): For every major concept, state transition, or system architecture card, populate "visual_requests" with at least one visual request (type: "mermaid" for architecture/flowcharts, "animation" for dynamic step-by-step state changes, or "image" for high-impact conceptual diagrams).
- CHECKPOINT QUIZZES: At least 2 cards in each topic deck (or cards flagged as quiz/checkpoint) MUST have a fully structured "quiz_json" with:
  {{
    "question": "Clear question testing intuition or boundary condition",
    "options": ["A) option 1", "B) option 2", "C) option 3", "D) option 4"],
    "correct_option_index": 0,
    "explanation": "Detailed pedagogical explanation of why the correct option wins and why common distractors fail."
  }}
- INTERACTIVE ANIMATIONS & DIAGRAMS: In body_markdown, always embed corresponding visual marker tags such as:
  `[[VISUAL: id_here | type: animation | intent: step-by-step animation of buffer filling | nodes: Empty,Half,Full,Spill]]` or Mermaid diagram blocks so instructors and learners see rich visual explanations instead of plain walls of text.

REFERENCES
- Use APPROVED_DOCS first for every API, command, flag, or tool you teach.
- If an approved source does not cover something, you may search the web.
  Label each such source "UNVERIFIED: needs SME validation".
- Never state a version-specific fact without a source.

CONTINUITY
- Keep running example names identical to APPROVED_SECTIONS.
- Use ONLY grounded everyday daily-life analogies (UPI, WhatsApp, grocery/food delivery, train/movie booking).
  STRICTLY NEVER use abstract/esoteric analogies like fighter jets, rockets, or particle physics.
- Do not reuse a form (words, diagram, quiz, applied) already used for the same idea.
- Signpost the step up when difficulty rises.

OUTPUT
Return ONLY valid JSON with this structure:
{{
  "cards": [
    {{
      "card_id": string,
      "card_type": "instructor_note | welcome | agenda | concept | problem | walkthrough | quiz | faq | recap",
      "title": string,
      "description": string,
      "duration_sec": number,
      "body_markdown": string,
      "instructor_note": string,
      "code_snippet": string or null,
      "code_language": string or null,
      "quiz_json": {{ "question": string, "options": [string], "correct_option_index": int, "explanation": string }} or null
    }}
  ],
  "references": [{{
    "url": string, "title": string, "retrieved": "YYYY-MM-DD",
    "approved": boolean, "used_in": [card_id]
  }}],
  "new_terms": [{{"term": string, "defined_in": "card_id"}}],
  "visual_requests": [{{
    "id": string, "type": "mermaid | animation | image",
    "intent": string, "nodes": [string], "card_id": string
  }}],
  "self_check": {{
    "rules_met": [rule_id],
    "rules_at_risk": [{{"rule": "rule_id", "reason": string}}]
  }}
}}
"""

INLINE_ASSIST_TASK = """\
INLINE ASSIST MODE
Rewrite ONLY the selected text to satisfy the request and all Block B rules.
Return ONLY the replacement text, nothing else.

REQUEST: {inline_request}
SELECTED TEXT:
{selected_text}
"""


def _normalise_section_id(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.lstrip("0") or str(value or "")


def _outline_for_section(outline: Dict[str, Any], section_id: str) -> Dict[str, Any]:
    """The part of the outline the Writer needs for one section: that section in full, the others as a title list.
    Section ids differ between callers (S1, S01, sec_1, section-1), so compare their numbers."""
    sections = outline.get("sections", []) if isinstance(outline, dict) else []
    wanted = _normalise_section_id(section_id)
    chosen = next((s for s in sections if _normalise_section_id(s.get("section_id")) == wanted), None)
    return {
        "running_example": outline.get("running_example"),
        "assumptions": outline.get("assumptions"),
        "this_section": chosen,
        "other_sections": [{"section_id": s.get("section_id"), "goal": s.get("goal"),
                            "cards": [c.get("title") for c in s.get("cards", [])]} for s in sections if s is not chosen],
    }


def live_llm_available() -> bool:
    return bool(llm_configured() and EXECUTION_MODE != "mock")


def continuity_text(flow: Optional[Dict[str, Any]]) -> str:
    """The prompt lines that tell the Writer where it is in the class: where the previous card stopped and what card comes next."""
    flow = flow or {}
    prev, nxt = flow.get("previous"), flow.get("next")
    lines = [f"WHERE YOU ARE: card {flow.get('position') or '?'} of the class."]
    if flow.get("level") and flow.get("previous_level") and flow["level"] != flow["previous_level"]:
        lines.append(f"LEVEL STEP: the card before was {flow['previous_level']} level and this one is {flow['level']}. Signpost the step in your opening, "
                     f"for example \"Now that the basics are clear, let's go a level deeper.\"")
    if prev and prev.get("ended_with"):
        lines.append(f'PREVIOUS CARD ({prev.get("type") or "card"}, "{prev.get("title")}") ENDED WITH THESE WORDS: "{prev.get("ended_with")}"')
    elif prev:
        lines.append(f'PREVIOUS CARD ({prev.get("type") or "card"}, "{prev.get("title")}") is not written yet. Open by linking to that topic, as if it had just been covered, without repeating it.')
    else:
        lines.append("There is no previous card: this one opens the class, so open it as an opening.")
    if nxt:
        lines.append(f'NEXT CARD: {nxt.get("type") or "card"}, "{nxt.get("title")}"' + (f' (it covers: {nxt.get("covers")})' if nxt.get("covers") else ""))
    else:
        lines.append("There is no next card: this one closes the class.")
    return "\n".join(lines)


def build_card_prompts(
    card_spec: Dict[str, Any],
    section_goal: str,
    outline_overview: List[str],
    written_so_far: List[Dict[str, Any]],
    term_register: List[str],
    running_example: Optional[str],
    block_a_context: str,
    reference_excerpts: str = "",
    flow: Optional[Dict[str, Any]] = None,
) -> tuple:
    """The (system, task) prompts for writing one card."""
    from agents.shared_context import BLOCK_B_GOOD_SCRIPT_RULES, word_budget
    system_prompt = WRITER_SYSTEM.format(block_a=block_a_context, block_b=BLOCK_B_GOOD_SCRIPT_RULES)
    task_prompt = WRITER_CARD_TASK.format(
        outline_overview=json.dumps(outline_overview, separators=(",", ":")),
        section_goal=section_goal or "",
        running_example=running_example or "none set; choose one everyday system and keep it",
        card_spec=json.dumps(card_spec, separators=(",", ":")),
        written_so_far=json.dumps(written_so_far, separators=(",", ":")) if written_so_far else "none (this is the first card)",
        term_register=json.dumps(term_register, separators=(",", ":")) if term_register else "[]",
        continuity=continuity_text(flow),
        duration_sec=int(card_spec.get("duration_sec") or 300),
        target_words=word_budget((card_spec.get("duration_sec") or 300) / 60.0)[0], max_words=word_budget((card_spec.get("duration_sec") or 300) / 60.0)[1],
        reference_excerpts=(f"REFERENCE EXCERPTS (from the approved documentation, fetched just now; your facts must agree with them):\n{reference_excerpts}"
                            if reference_excerpts else ""),
    )
    return system_prompt, task_prompt


def write_card(
    card_spec: Dict[str, Any],
    section_goal: str,
    outline_overview: List[str],
    written_so_far: List[Dict[str, Any]],
    term_register: List[str],
    running_example: Optional[str],
    block_a_context: str,
    reference_excerpts: str = "",
    flow: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Writes exactly one card. The prompt holds only that card's spec, a digest of what was already written and the
    terms already defined, so it fits the model's budget and the card continues the script instead of restarting it."""
    system_prompt, task_prompt = build_card_prompts(
        card_spec, section_goal, outline_overview, written_so_far, term_register, running_example, block_a_context, reference_excerpts, flow)
    from agents.image_gen import without_image_option
    return call_llm_json(system=without_image_option(system_prompt), messages=[{"role": "user", "content": without_image_option(task_prompt)}],
                         max_tokens=2500, temperature=0.2, agent="writer_card")


CODE_REPAIR_TASK = """\
The code below was run by the system and did not work. Fix it with the smallest change that makes it work.
Keep the same names, the same teaching idea and the output the card describes. The code must not use input(), the
network, files or installs. {expectation}

LANGUAGE: {language}
CODE:
{code}

WHAT WENT WRONG:
{error}

THE CARD IT BELONGS TO (for context only):
{card_text}

Return ONLY JSON: {{"code": string}}
"""


def repair_code(language: Optional[str], code: str, error: str, card_text: str, expected_error: Optional[str] = None) -> Optional[str]:
    """One attempt to fix code that failed to run. Returns the new code, or None when the model gave nothing usable."""
    expectation = (f"This snippet is MEANT to fail with {expected_error}; make it fail with exactly that error."
                   if expected_error else "It must run without an error.")
    out = call_llm_json(
        messages=[{"role": "user", "content": CODE_REPAIR_TASK.format(
            expectation=expectation, language=language or "unknown", code=code[:6000], error=(error or "")[-1500:],
            card_text=(card_text or "")[:1500])}],
        max_tokens=2500, temperature=0.1, agent="code_repair")
    fixed = out.get("code") if isinstance(out, dict) else None
    return fixed.strip("\n") if isinstance(fixed, str) and fixed.strip() else None


def write_section(
    section_id: str,
    approved_outline: Dict[str, Any],
    approved_sections: List[Dict[str, Any]],
    term_register: List[Dict[str, str]],
    block_a_context: Optional[str] = None,
    class_title: str = "",
    memory_context: Optional[Dict[str, Any]] = None,
    content_mix_beginner: int = 65,
    code_ratio: int = 50,
    script_id: Optional[int] = None
) -> Dict[str, Any]:
    """
    Writes all cards for a given section_id from the approved outline.
    Returns cards + references + new_terms + visual_requests + self_check.
    """
    if not llm_configured() or EXECUTION_MODE == "mock":
        from agents.llm import LLMUnavailable, mock_fallback_allowed
        if not mock_fallback_allowed():                       # no key: say so, rather than write sample text that has nothing to do with this class
            raise LLMUnavailable("The AI is not configured, so the cards cannot be written. Add the AI key in backend/.env and restart. Sample text is never written in its place.")
        return _mock_section_output(section_id, approved_outline, class_title)

    try:
        from agents.shared_context import BLOCK_B_GOOD_SCRIPT_RULES

        # Build context
        if block_a_context:
            context_header = block_a_context
        else:
            anchor = (memory_context or {}).get("anchor_analogy", "Swiggy")
            context_header = (
                "<context>\n"
                f"Class title: {class_title}\n"
                f"Analogy: {anchor}\n"
                f"Audience mix: {content_mix_beginner}% beginner, "
                f"{100 - content_mix_beginner}% advanced\n"
                f"Code ratio: {code_ratio}% Code\n"
                "</context>"
            )

        system_prompt = WRITER_SYSTEM.format(
            block_a=context_header,
            block_b=BLOCK_B_GOOD_SCRIPT_RULES
        )

        # Compact approved sections to just card_ids and titles for context
        approved_summary = []
        for sec in approved_sections:
            for card in sec.get("cards", []):
                approved_summary.append({
                    "card_id": card.get("card_id"),
                    "title": card.get("title"),
                    "card_type": card.get("card_type")
                })

        task_prompt = WRITER_SECTION_TASK.format(
            approved_outline=json.dumps(_outline_for_section(approved_outline, section_id), separators=(",", ":")),
            section_id=section_id,
            approved_sections=json.dumps(approved_summary, indent=2) if approved_summary else "None (first section)",
            term_register=json.dumps(term_register) if term_register else "[]"
        )

        result = call_llm_json(
            system=system_prompt,
            messages=[{"role": "user", "content": task_prompt}],
            max_tokens=4000,
            temperature=0.2,
            agent="writer_section",
            script_id=script_id
        )
        return result

    except Exception as e:
        from agents.llm import mock_fallback_allowed
        if not mock_fallback_allowed():
            raise      # a failed model call must be seen, not hidden behind sample content
        print(f"Warning: Writer agent failed ({e}). Falling back to mock.")
        return _mock_section_output(section_id, approved_outline, class_title)


def inline_assist(
    selected_text: str,
    inline_request: str,
    block_a_context: Optional[str] = None
) -> str:
    """
    Rewrites a selected text snippet per the SME's inline request.
    Returns only the replacement text.
    """
    if not llm_configured() or EXECUTION_MODE == "mock":
        return selected_text  # No-op in mock mode

    try:
        from agents.shared_context import BLOCK_B_GOOD_SCRIPT_RULES

        system_prompt = WRITER_SYSTEM.format(
            block_a=block_a_context or "",
            block_b=BLOCK_B_GOOD_SCRIPT_RULES
        )

        task_prompt = INLINE_ASSIST_TASK.format(
            inline_request=inline_request,
            selected_text=selected_text
        )

        return call_llm_chat(
            system=system_prompt,
            messages=[{"role": "user", "content": task_prompt}],
            max_tokens=1500,
            temperature=0.2,
            agent="writer_inline"
        )

    except Exception as e:
        print(f"Warning: Inline assist failed ({e}).")
        return selected_text


# ── Legacy API compatibility ─────────────────────────────────────────────────
# These functions are imported by workflow_graph.py and main.py

def generate_outline(
    class_title: str,
    agendas: List[str],
    duration_minutes: int,
    content_mix_beginner: int,
    code_ratio: int,
    memory_context: Dict[str, Any],
    additional_resources: Optional[str] = None,
    learning_outcomes: Optional[str] = None,
    api_docs: Optional[str] = None,
    reference_urls: Optional[str] = None,
    block_a_context: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Legacy wrapper: calls Outliner agent and flattens sections into a card list
    for backward-compatible pipeline usage.
    """
    from agents.outliner import generate_outline as outliner_generate
    outline = outliner_generate(
        class_title=class_title,
        agendas=agendas,
        duration_minutes=duration_minutes,
        content_mix_beginner=content_mix_beginner,
        code_ratio=code_ratio,
        memory_context=memory_context,
        block_a_context=block_a_context,
        learning_outcomes=learning_outcomes,
        additional_resources=additional_resources,
        api_docs=api_docs,
        reference_urls=reference_urls
    )
    # Flatten sections into a flat card list for legacy consumers
    flat_cards = []
    idx = 1
    for section in outline.get("sections", []):
        for card in section.get("cards", []):
            flat_cards.append({
                "card_index": idx,
                "card_id": card.get("card_id", f"C{idx:02d}"),
                "title": card.get("title", f"Card {idx}"),
                "duration_mins": round(card.get("duration_sec", 300) / 60.0, 1),
                "card_type": card.get("card_type", "concept"),
                "pedagogical_phase": card.get("card_type", "concept").replace("_", " ").title(),
                "key_concept": ", ".join(card.get("agenda_items", [])),
                "visual_type": card.get("visuals_planned", ["none"])[0] if card.get("visuals_planned") else "none",
                "section_id": section.get("section_id"),
            })
            idx += 1
    return flat_cards


def draft_card_batch(
    class_title: str,
    outline_cards: List[Dict[str, Any]],
    batch_start_idx: int,
    batch_end_idx: int,
    memory_context: Dict[str, Any],
    code_ratio: int,
    content_mix_beginner: int,
    block_a_context: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Legacy wrapper: drafts a batch of cards using the new section-based Writer.
    Groups the batch into a pseudo-section and calls write_section.
    """
    batch_cards = outline_cards[batch_start_idx - 1 : batch_end_idx]

    # Build a pseudo-outline for the batch
    pseudo_outline = {
        "sections": [{
            "section_id": f"BATCH_{batch_start_idx}_{batch_end_idx}",
            "goal": f"Cards {batch_start_idx} to {batch_end_idx}",
            "cards": [
                {
                    "card_id": c.get("card_id", f"C{c.get('card_index', i+1):02d}"),
                    "card_type": c.get("card_type", c.get("pedagogical_phase", "concept")),
                    "title": c.get("title", f"Card {i+1}"),
                    "agenda_items": [c.get("key_concept", "")],
                    "outcomes": [],
                    "teaching_shape_note": "Why -> What -> How -> Where -> What breaks",
                    "analogy": None,
                    "predict_moment": False,
                    "visuals_planned": [c.get("visual_type", "none")] if c.get("visual_type", "none") != "none" else [],
                    "duration_sec": int(c.get("duration_mins", 5.0) * 60)
                }
                for i, c in enumerate(batch_cards)
            ]
        }]
    }

    result = write_section(
        section_id=f"BATCH_{batch_start_idx}_{batch_end_idx}",
        approved_outline=pseudo_outline,
        approved_sections=[],
        term_register=[],
        block_a_context=block_a_context,
        class_title=class_title,
        memory_context=memory_context,
        content_mix_beginner=content_mix_beginner,
        code_ratio=code_ratio
    )

    # Convert section output back to legacy card format
    drafted = []
    for card in result.get("cards", []):
        drafted.append({
            "card_index": batch_start_idx + len(drafted),
            "card_id": card.get("card_id"),
            "title": card.get("title", ""),
            "card_type": card.get("card_type", "concept"),
            "duration_mins": round(card.get("duration_sec", 300) / 60.0, 1),
            "pedagogical_phase": card.get("card_type", "concept").replace("_", " ").title(),
            "instructor_note": card.get("instructor_note", ""),
            "stage_directions": "",
            "content_script": card.get("body_markdown", ""),
            "code_snippet": card.get("code_snippet"),
            "code_language": card.get("code_language"),
            "mermaid_syntax": None,
            "quiz_json": card.get("quiz_json"),
        })
    return drafted


# ── Mock helpers ─────────────────────────────────────────────────────────────

def _mock_section_output(
    section_id: str,
    approved_outline: Dict[str, Any],
    class_title: str
) -> Dict[str, Any]:
    """Generates mock Writer output for a section."""
    from agents.mock_provider import get_mock_cards

    mock_cards = get_mock_cards(class_title)

    # Find the target section – tolerate sec_1/S01/section-1 naming mismatches
    target_section = None
    sections = approved_outline.get("sections", [])
    for section in sections:
        if section.get("section_id") == section_id:
            target_section = section
            break
    if not target_section:
        # Fallback: match by 1-based index embedded in section_id (e.g. "sec_1" → index 1, "S01" → index 1)
        import re
        m = re.search(r"(\d+)", section_id or "")
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(sections):
                target_section = sections[idx]

    cards_out = []
    if target_section:
        for idx, card_meta in enumerate(target_section.get("cards", [])):
            card_id = card_meta.get("card_id", f"C{idx+1:02d}")
            c_type = card_meta.get("card_type", "concept")
            c_title = card_meta.get("title", f"Card {card_id}")
            concept = card_meta.get("teaching_shape_note") or (card_meta.get("agenda_items") or [""])[0] or c_title
            
            # Match against rich template if available, otherwise generate high quality spoken dialogue
            template_match = None
            for mc in mock_cards:
                if mc.get("title", "").lower() in c_title.lower() or c_title.lower() in mc.get("title", "").lower():
                    template_match = mc
                    break

            if template_match:
                script_body = template_match.get("content_script", "")
                stage_cue = template_match.get("stage_directions", "Pace delivery clearly and check learner responses.")
                code_snippet = template_match.get("code_snippet")
                code_lang = template_match.get("code_language")
                quiz_data = template_match.get("quiz_json")
            else:
                # Synthesize pedagogical script based on card type
                if "instructor" in c_title.lower() or c_type == "instructor_note":
                    stage_cue = f"[CAMERA ON INSTRUCTOR. Do not share screen yet. Frame 90-minute expectations for {class_title}.]"
                    script_body = (
                        f"Welcome everyone to today's deep-dive session on {class_title}. "
                        "Before we jump into code or terminal sessions, let us set our expectations for the next 90 minutes. "
                        "We will move from the foundational architectural mechanics to a live, production-grade implementation. "
                        "Keep your chat box open, ask questions whenever a concept feels ambiguous, and pay close attention to the failure patterns we highlight."
                    )
                    code_snippet, code_lang, quiz_data = None, None, None
                elif "agenda" in c_title.lower() or "roadmap" in c_title.lower():
                    stage_cue = "[PROJECT ROADMAP ON SCREEN. Point to the three learning phases.]"
                    script_body = (
                        f"Here is our technical roadmap for {class_title}. We break this journey into three distinct phases: "
                        "First, we analyze the naive implementation and why it collapses under load. "
                        "Second, we design the resilient decoupled pattern step-by-step. "
                        "Finally, we implement the solution in code and inject simulated failure modes to prove its robustness."
                    )
                    code_snippet, code_lang, quiz_data = None, None, None
                elif "analogy" in c_title.lower():
                    stage_cue = "[USE WHITEBOARD. Sketch the real-world operational flow and highlight the physical bottleneck.]"
                    script_body = (
                        f"To build an intuition for {c_title}, let us consider an everyday analogy. "
                        "Imagine an order management counter during peak rush hour where a single clerk tries to take orders, "
                        "verify payment, and hand over packages all at once. A single delayed customer blocks the entire queue. "
                        "In production systems, synchronous blocking operations produce the exact same deadlock. "
                        "By introducing an asynchronous buffer, downstream processing proceeds smoothly without starving the front counter."
                    )
                    code_snippet, code_lang, quiz_data = None, None, None
                elif "quiz" in c_title.lower() or c_type == "quiz":
                    stage_cue = "[LAUNCH POLL IN CHAT. Give learners 45 seconds to submit answers before analyzing distractors.]"
                    script_body = (
                        f"Let us test our intuition on {c_title}. Look closely at the question on your screen. "
                        "Think about what happens at the boundary between client requests and worker threads. "
                        "Select your answer, and then we will examine why the common distractors fail."
                    )
                    code_snippet, code_lang = None, None
                    quiz_data = {
                        "question": f"In {class_title}, what is the primary risk of an unbuffered synchronous request pipeline?",
                        "options": [
                            "A) Operating system thread pool exhaustion and cascading HTTP 504 timeouts",
                            "B) Immediate file descriptor corruption on client disk",
                            "C) Sub-millisecond CPU register cache thrashing",
                            "D) DNS query packet loop at gateway boundary"
                        ],
                        "correct_option_index": 0,
                        "explanation": "Correct answer is A. Synchronous blocking ties up worker threads, exhausting pool capacity and triggering cascading timeouts across upstream services."
                    }
                elif "code" in c_title.lower() or c_type in ["code", "code_along"]:
                    stage_cue = f"[SWITCH TO IDE. Live code the handler for {c_title} and execute the unit test suite.]"
                    script_body = (
                        f"Let us look at the code for {c_title}. Notice how we structure the handler: "
                        "we validate the payload immediately at the boundary, delegate the long-running computation "
                        "to our worker queue, and return an immediate acknowledgment to the caller. "
                        "Let us run this locally and inspect the sub-5ms latency profile."
                    )
                    code_snippet = (
                        f"# Production Implementation for {class_title}\n"
                        "from typing import Dict, Any\n"
                        "import time\n\n"
                        "def handle_workload(payload: Dict[str, Any]) -> Dict[str, Any]:\n"
                        "    # Validate payload integrity\n"
                        "    if not payload.get('id'):\n"
                        "        raise ValueError('Invalid payload identifier')\n\n"
                        "    # Non-blocking async dispatch\n"
                        "    task_id = f'task_{payload[\"id\"]}_{int(time.time())}'\n"
                        "    return {'status': 'ACCEPTED', 'task_id': task_id}\n"
                    )
                    code_lang = "python"
                    quiz_data = None
                else:
                    stage_cue = f"[SHOW ARCHITECTURE SLIDE. Emphasize state transitions and boundary guarantees for {c_title}.]"
                    script_body = (
                        f"Now let us examine {c_title}. When we look at {concept}, the central challenge is ensuring predictability. "
                        "If our components are tightly coupled, any downstream latency ripples backwards through the entire call graph. "
                        "By defining strict boundaries and fail-fast invariants, each node maintains isolated operational safety even under peak load."
                    )
                    code_snippet, code_lang, quiz_data = None, None, None

            cards_out.append({
                "card_id": card_id,
                "card_type": c_type,
                "title": c_title,
                "description": f"Structured card for {class_title}",
                "duration_sec": card_meta.get("duration_sec", 300),
                "body_markdown": script_body,
                "instructor_note": stage_cue,
                "code_snippet": code_snippet,
                "code_language": code_lang,
                "quiz_json": quiz_data
            })
    else:
        # Fallback: use first few mock cards
        for i, mc in enumerate(mock_cards[:4]):
            cards_out.append({
                "card_id": f"C{i+1:02d}",
                "card_type": "concept",
                "title": mc.get("title", f"Card {i+1}"),
                "description": f"Auto-drafted card for {class_title}",
                "duration_sec": int(mc.get("duration_mins", 5.0) * 60),
                "body_markdown": mc.get("content_script", f"Lecture dialogue for {mc.get('title')}"),
                "instructor_note": mc.get("stage_directions", mc.get("instructor_note", "")),
                "code_snippet": mc.get("code_snippet"),
                "code_language": mc.get("code_language"),
                "quiz_json": mc.get("quiz_json")
            })

    return {
        "cards": cards_out,
        "references": [],
        "new_terms": [],
        "visual_requests": [],
        "self_check": {
            "rules_met": ["S1", "S2", "S4", "T1", "R1", "R8"],
            "rules_at_risk": []
        }
    }
