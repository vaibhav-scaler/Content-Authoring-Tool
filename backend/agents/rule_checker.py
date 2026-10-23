"""
Rule checker: the Good Script Rules tagged [auto], checked by code.

It reads the same LIMITS the prompts are built from (shared_context), so a rule and its check cannot drift apart.
It costs no model call, so it runs before the reviewers and its findings go straight to the Moderator.

Each finding has the shape the reviewers use:
  {card_id, rule, severity (blocker | major | minor), quote, problem, suggested_fix, auto_fix (bool)}
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.shared_context import (CALLOUTS, LIMITS, TEACHING_CARD_TYPES, cards_for_duration, has_opening_welcome, word_budget)

EM_DASH = "—"
_STOP = {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "how", "what", "why", "is", "are", "it",
         "its", "by", "as", "at", "be", "this", "that", "from", "into", "your", "you", "can", "use", "using", "when"}


def _kind(card: Dict[str, Any]) -> str:
    raw = str(card.get("pedagogical_phase") or card.get("card_type") or "").lower().replace(" ", "_")
    return raw


def _finding(card_id, rule, severity, problem, fix, quote="", auto_fix=False) -> Dict[str, Any]:
    return {"card_id": card_id, "rule": rule, "severity": severity, "quote": quote[:120], "problem": problem,
            "suggested_fix": fix, "auto_fix": auto_fix}


def words(text: str) -> set:
    """Significant lower-case words of a phrase, with a crude plural strip, for matching an agenda line to card text."""
    out = set()
    for w in re.findall(r"[a-z0-9][a-z0-9+#.-]*", (text or "").lower()):
        if w in _STOP or len(w) < 2:
            continue
        for suffix in ("ing", "ed", "es", "s"):          # converting, converted and converts all count as one word
            if w.endswith(suffix) and len(w) - len(suffix) >= 4:
                w = w[:-len(suffix)]
                break
        out.add(w)
    return out


def covered(item: str, texts: List[str], threshold: float = 0.5) -> bool:
    """True when at least `threshold` of the item's significant words appear together in one card's text."""
    need = words(item)
    if not need:
        return True
    for t in texts:
        have = words(t)
        if len(need & have) / len(need) >= threshold:
            return True
    return False


def _real_outcomes(outcomes):
    """The outcomes an author or Module Owner wrote. The generic text filled in when nobody gave any is not something a card has to cover."""
    from agents.shared_context import _PLACEHOLDER_OUTCOME_PREFIX
    return "" if (outcomes or "").strip().startswith(_PLACEHOLDER_OUTCOME_PREFIX) else outcomes


def split_items(text: Optional[str]) -> List[str]:
    """Learning outcomes or agenda text as a list of lines (bullets and numbering removed)."""
    out = []
    for line in re.split(r"[\n;]+", text or ""):
        line = re.sub(r"^\s*(?:[-*•]|\d+[.)])\s*", "", line).strip()
        if len(line) > 3:
            out.append(line)
    return out


_UNQUOTED_LABEL = re.compile(r'(\b[A-Za-z_][\w-]*)\[(?!["\[(/\\])([^\]\n"]*)\]')
_LABEL_TROUBLE = re.compile(r'[|(){};#&<>]')


def quote_mermaid_labels(code: str) -> str:
    """Mermaid reads ID[text] as a box, but a | ( ) { } ; # & < > inside unquoted text is a syntax error ("Syntax error in text"). This puts the text of every such box in
    quotes (ID["text"]), turning a double quote inside it into a single quote. A diagram that was already valid is left unchanged."""
    def fix(m):
        label = m.group(2)
        return m.group(0) if not _LABEL_TROUBLE.search(label) else f'{m.group(1)}["{label.strip().replace(chr(34), chr(39))}"]'
    return _UNQUOTED_LABEL.sub(fix, code or "")


def mermaid_syntax_problem(code: str) -> Optional[str]:
    """The first thing in a Mermaid diagram that would stop it drawing (an unquoted box with a | ( ) { } ; # & < > in it, or text with no diagram type), else None."""
    text = (code or "").strip()
    if not re.match(r"(?i)^(flowchart|graph|sequenceDiagram|stateDiagram|classDiagram|erDiagram|journey|gantt|pie|mindmap|timeline)\b", text):
        return "the diagram does not start with a diagram type such as flowchart LR"
    bad = [m.group(0) for m in _UNQUOTED_LABEL.finditer(text) if _LABEL_TROUBLE.search(m.group(2))]
    return f"an unquoted box text with a special character: {bad[0][:60]}" if bad else None


def _diagram_nodes(mermaid: str) -> int:
    ids = set(re.findall(r"(?<![\w-])([A-Za-z_][\w]*)\s*(?:\[|\(\(|\(|\{|>)", mermaid or ""))
    ids |= set(re.findall(r"(?:-->|---|==>|-\.->)\s*(?:\|[^|]*\|)?\s*([A-Za-z_][\w]*)", mermaid or ""))
    return len(ids)


def check_script(
    cards: List[Dict[str, Any]],
    target_minutes: int,
    agendas: Optional[List[str]] = None,
    outcomes: Optional[str] = None,
    code_ratio: Optional[int] = None,
    required_content: Optional[Dict[str, Tuple[str, str]]] = None,
    program_opening: bool = False,
    upcoming: Optional[List[Dict[str, Any]]] = None,
    previous_class: Optional[Dict[str, Any]] = None,
    known_terms: Optional[set] = None,
    class_title: str = "",
    agenda_timing: Optional[List[Dict[str, Any]]] = None,
    running_example: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Every [auto] rule that can be judged from the stored cards. `cards` are dicts with card_index, title,
    duration_mins, pedagogical_phase, content_script, code_snippet, quiz, sources, visual_type, visual_content."""
    out: List[Dict[str, Any]] = []
    if not cards:
        return out

    # ---- S1 card count, S4 total
    lo, hi = cards_for_duration(target_minutes)
    if not lo <= len(cards) <= hi:
        out.append(_finding("all", "S1", "major", f"The script has {len(cards)} cards; a {target_minutes} minute class needs {lo} to {hi}.",
                            "Split or merge cards to land in range."))
    total = round(sum(float(c.get("duration_mins") or 0) for c in cards), 1)
    if abs(total - target_minutes) > 0.05:
        out.append(_finding("all", "S4", "blocker" if abs(total - target_minutes) > 2 else "major",
                            f"Card durations add up to {total} minutes, not {target_minutes}.",
                            "Rebalance durations so they sum to the class duration."))

    quizzes_ok = predicts = traces = diagrams = coded = 0
    teach_cards = 0
    own_scope_words = class_scope_words(agendas, outcomes, class_title, upcoming)
    previous_card = None
    spoken_before = False
    opening_card_id = next((c.get("card_index") for c in cards if _kind(c) != "instructor_note"), None)       # the Welcome & Agenda card: nothing comes before it to continue from
    for c in cards:
        before, previous_card = previous_card, c
        had_spoken_before, spoken_before = spoken_before, spoken_before or _kind(c) != "instructor_note"
        cid = c.get("card_index")
        kind = _kind(c)
        body = c.get("content_script") or ""
        dur = float(c.get("duration_mins") or 0)

        # ---- S1 per-card minutes
        rng = (LIMITS["teaching_card_minutes"] if kind in TEACHING_CARD_TYPES else
               LIMITS["quiz_card_minutes"] if kind == "quiz" else LIMITS["note_card_minutes"])
        if not rng[0] - 0.05 <= dur <= rng[1] + 0.05:
            out.append(_finding(cid, "S1", "major" if dur > rng[1] + 2 or dur < rng[0] - 2 else "minor",
                                f"A {kind or 'card'} runs {rng[0]} to {rng[1]} minutes; this one is {dur}.",
                                "Adjust the duration or the content to fit."))

        # ---- the plan for this card: its level and whether it carries code (from the SME's split and ratio)
        level, planned_code = c.get("level"), c.get("planned_code")
        has_code_now = bool((c.get("code_snippet") or "").strip())
        if planned_code is True and not has_code_now and kind in ("walkthrough", "problem", "concept"):
            out.append(_finding(cid, "S7", "major", "The plan says this card carries code, but it has none.",
                                "Add a short, runnable code example that shows the idea, with the platform and version, and tie the spoken text to it."))
        elif planned_code is False and has_code_now and kind in ("walkthrough", "problem", "concept"):
            out.append(_finding(cid, "S7", "minor", "The plan keeps this card free of code, but it has code.", "Teach it with words and a diagram, or accept the extra code."))
        if level == "advanced" and kind in ("concept", "problem", "walkthrough") and not re.search(r"going deeper", body, re.I):
            out.append(_finding(cid, "T8", "major", "This is an advanced card, but it has no 'Going deeper' sub-heading.",
                                "Take the card to the advanced level (edge cases, how it works inside, trade-offs, failure modes) and put that part under a '### Going deeper' sub-heading."))
        elif level == "beginner" and re.search(r"going deeper", body, re.I):
            out.append(_finding(cid, "T8", "minor", "This is a beginner card, so it should not go 'deeper' yet.", "Move the deeper material to a later, advanced card."))

        # ---- S6 nothing from an upcoming class is taught here (a one-sentence pointer is allowed, and so is the recap or FAQ)
        if upcoming and kind in ("concept", "problem", "walkthrough"):
            hit = upcoming_overlap(body, upcoming, own_scope_words)
            if hit:
                out.append(_finding(cid, "S6", "major", f"The card teaches \"{hit['item']}\", which belongs to upcoming class {hit['number']} \"{hit['class']}\".",
                                    "Remove it, or cut it down to one sentence that points forward (\"we will come to that in a later class\"). Teach only what this class's agenda and outcomes list.",
                                    quote=hit["item"]))

        # ---- R3 a term an earlier class already taught is used, not taught again
        if known_terms and kind in ("concept", "problem", "walkthrough"):
            again = [t for t in re.findall(r"\*\*([^*\n]{2,60})\*\*", re.sub(r"```.*?```", " ", body, flags=re.S)) if t.strip().rstrip(":").lower() in known_terms]
            if again:
                out.append(_finding(cid, "R3", "minor", "The card defines " + ", ".join(f'"{t}"' for t in dict.fromkeys(again[:3])) + " in bold, but an earlier class already taught it.",
                                    "Use the term without defining it again (one clause of reminder at most), and spend the time on what is new."))

        # ---- V2 the card picks up where the previous one stopped (not the first card, not a note or a quiz)
        if before is not None and kind not in ("instructor_note", "quiz") and not (program_opening and cid == opening_card_id):
            problem = continuity_problem(body, before, had_spoken_before)
            if problem:
                tail = spoken_tail(before.get("content_script"), 35)
                out.append(_finding(cid, "V2", "major", problem,
                                    f"Open with one or two spoken sentences that pick up from where card {before.get('card_index')} ended"
                                    + (f' ("...{tail}")' if tail else "") + f', name what was just covered and what this card does next. Do not greet again.',
                                    quote=spoken_text(body)[:100]))

        # ---- S8 the spoken words fit the card's time (a note card is a cue, not a speech)
        if kind != "instructor_note" and dur > 0:
            words_in_card = spoken_words(body)
            target, most = word_budget(dur)
            if words_in_card > most:
                out.append(_finding(cid, "S8", "major", f"The card has {words_in_card} spoken words; a {dur:g} minute card holds about {target} (at most {most}).",
                                    f"Cut it to about {target} words by removing repeats, recaps and extra examples. Keep every agenda item and outcome the card serves, "
                                    f"and keep the quiz, code and callouts that teach them."))
            elif words_in_card < target * LIMITS["length_tolerance"][0] and kind in TEACHING_CARD_TYPES:
                out.append(_finding(cid, "S8", "minor", f"The card has only {words_in_card} spoken words for {dur:g} minutes (about {target} fit).",
                                    "Check that every agenda item and outcome this card serves is actually explained, then add what is missing."))

        # ---- V1 the text is what the instructor says aloud: no directions to the instructor, no third person about learners
        # (a note card is a note by design: the instructor-introduction note and any other note are exempt)
        if kind != "instructor_note":
            problem = voice_problem(body)
            if problem:
                out.append(_finding(cid, "V1", "major", problem[0],
                                    "Rewrite it as the exact words the instructor says aloud to the learners ('you'). Keep only short bracketed cues such as [pause for replies].",
                                    quote=problem[1]))

        # ---- R8 em dash (code can fix it)
        if EM_DASH in body or EM_DASH in (c.get("title") or "") or EM_DASH in (c.get("stage_directions") or ""):
            out.append(_finding(cid, "R8", "minor", "The card contains an em dash.", "Replace it with a comma, colon or full stop.",
                                quote=body[max(0, body.find(EM_DASH) - 30): body.find(EM_DASH) + 30], auto_fix=True))

        # A [[FROM_SME: ...]] placeholder is a fact only the SME can give. It is not a defect in the card: see collect_sme_items,
        # which lists them for the SME on one screen.

        # ---- R7 no tables / raw html / images in the body
        if re.search(r"^\s*\|.+\|\s*$", body, re.M) or re.search(r"</?(div|table|img|span|svg)\b", body, re.I) or re.search(r"!\[[^\]]*\]\(", body):
            out.append(_finding(cid, "R7", "major", "The card text holds a table, raw HTML or an image.",
                                "Move it into a [[VISUAL: ...]] placeholder."))

        # ---- R2 a Key callout on every teaching card
        if kind in TEACHING_CARD_TYPES and kind != "recap":
            teach_cards += 1
            if not re.search(r"\bKey\b", body):
                out.append(_finding(cid, "R2", "minor", "A teaching card needs at least one Key callout.", "Add a Key callout."))

        # ---- E1 predict moments (Predict callout, then a line starting 'Reveal:')
        if re.search(r"Predict", body) and re.search(r"^\W*Reveal\b", body, re.M | re.I):
            predicts += 1

        # ---- E2 quiz must explain every wrong option
        if kind == "quiz":
            q = c.get("quiz") or {}
            opts = q.get("options") or []
            dist = q.get("distractor_explanations") or {}
            if len(opts) >= 2 and len(dist) >= len(opts) - 1 and q.get("explanation"):
                quizzes_ok += 1
            else:
                out.append(_finding(cid, "E2", "major", "The quiz does not explain every wrong option.",
                                    "Add one sentence for each wrong option and an overall explanation."))

        # ---- R6 traces and S7 code share
        if c.get("code_snippet") or "```" in body:
            coded += 1
            if kind == "walkthrough":
                traces += 1

        # ---- A3 the code must run: judged from the real run saved with the card
        if (c.get("code_snippet") or "").strip():
            run = c.get("code_run")
            if not run:
                out.append(_finding(cid, "A3", "minor", "The code has not been run yet.", "Run the code from the card."))
            elif run.get("status") == "failed":
                err = (run.get("stderr") or "").strip().splitlines()
                out.append(_finding(cid, "A3", "blocker", f"The code does not run: {run.get('reason') or 'it exited with an error'}",
                                    "Fix the code so it runs as written; the error was: " + (err[-1] if err else "unknown"),
                                    quote=(run.get("stderr") or "")[-120:]))
            elif run.get("status") == "timeout":
                out.append(_finding(cid, "A3", "major", "The code ran past the time limit.", "Make the example finish quickly."))
            elif run.get("status") == "skipped":
                out.append(_finding(cid, "A3", "minor", f"The code was not run: {run.get('reason') or 'no runner'}", "Check it by hand, or install the runtime."))

        # ---- A3 code names a platform and version somewhere in the card
        if c.get("code_snippet") and not re.search(r"\b\d+(\.\d+)+\b|\bv\d+\b|version", body + " " + (c.get("code_snippet") or ""), re.I):
            out.append(_finding(cid, "A3", "major", "The code has no platform or version stated beside it.",
                                "State the platform and version the code runs on."))

        # ---- A7 a card that teaches a command or API cites a source
        if kind in ("walkthrough", "concept", "problem") and c.get("code_snippet") and not [x for x in (c.get("sources") or []) if x.get("url_kind") != "kit"]:
            out.append(_finding(cid, "A7", "major", "The card teaches code but cites no source.",
                                "Add the official documentation source for each API or command used."))

        # ---- R5 diagram size
        if c.get("visual_type") == "mermaid" and c.get("visual_content"):
            n = _diagram_nodes(c["visual_content"])
            diagrams += 1
            if n > LIMITS["max_diagram_nodes"]:
                out.append(_finding(cid, "R5", "major", f"The diagram has {n} nodes; the limit is {LIMITS['max_diagram_nodes']}.",
                                    "Split the diagram into two steps."))
        elif c.get("visual_type") in ("animation", "image"):
            diagrams += 1

    if quizzes_ok < LIMITS["min_quizzes"]:
        out.append(_finding("all", "E2", "major", f"Only {quizzes_ok} complete quizzes; at least {LIMITS['min_quizzes']} are required.",
                            "Add a quiz card with tempting distractors, each explained."))
    if predicts < LIMITS["min_predict_moments"]:
        out.append(_finding("all", "E1", "major", f"Only {predicts} predict-before-reveal moments; at least {LIMITS['min_predict_moments']} are required.",
                            "Add a Predict callout and a 'Reveal:' line in teaching cards."))
    if diagrams < LIMITS["min_diagrams"]:
        out.append(_finding("all", "R4", "major", f"Only {diagrams} visuals; at least {LIMITS['min_diagrams']} diagrams are required.",
                            "Plan more diagrams for the key concepts."))
    if traces < LIMITS["min_traces"]:
        out.append(_finding("all", "R6", "minor", f"Only {traces} step-by-step traces; at least {LIMITS['min_traces']} are required.",
                            "Add a code trace to a walkthrough card."))
    if code_ratio is not None and cards:
        share = 100.0 * coded / len(cards)
        if abs(share - code_ratio) > 10 + 1e-9:
            out.append(_finding("all", "S7", "minor", f"{round(share)}% of cards carry code; the target is about {code_ratio}%.",
                                "Add or remove code on cards to approach the target."))

    # ---- K4 the agenda card lists every agenda item with its time, after the welcome
    if agenda_timing:
        from agents.timeline import agenda_is_complete
        agenda_cards = [c for c in cards if _kind(c) == "agenda"]
        if not agenda_cards:
            out.append(_finding("all", "K4", "major", "The class has no agenda card, so the instructor never states the agenda and its timing.", "Add an agenda card straight after the welcome."))
        elif not any(agenda_is_complete(c.get("content_script") or "", agenda_timing) for c in agenda_cards):
            out.append(_finding(agenda_cards[0].get("card_index"), "K4", "major", "The agenda card does not list every agenda item with its time.",
                                "After the welcome, read the agenda aloud: every item, in order, with about how many minutes it takes: "
                                + "; ".join(f"{t['item']} ({round(t['minutes'])} min)" for t in agenda_timing)))

    # ---- K3 a class after the first opens by picking up where the previous class stopped (when that class has an approved script)
    if previous_class and not program_opening:
        first_spoken = next((c for c in cards if _kind(c) not in ("instructor_note",) and (c.get("content_script") or "").strip()), None)
        if first_spoken is not None and not _RECALL_CUE.search(spoken_text(first_spoken.get("content_script"))[:400]):
            ending = previous_class.get("ending") or ""
            out.append(_finding(first_spoken.get("card_index"), "K3", "major",
                                f"The first card does not connect to class {previous_class.get('number')} \"{previous_class.get('title')}\", which it follows.",
                                "Open with two or three spoken sentences on where we left off" + (f' (that class ended: "...{ending}")' if ending else "")
                                + ", then move to today's agenda. Do not reteach it."))

    # ---- K2 a program's first class opens with the approved spoken welcome
    opening_card = next((c for c in cards if _kind(c) != "instructor_note"), cards[0]) if cards else None
    if program_opening and opening_card is not None and not has_opening_welcome(opening_card.get("content_script") or ""):
        out.append(_finding(opening_card.get("card_index"), "K2", "major", "A program's first class must open with the approved spoken welcome, and this card does not.",
                            "Begin the card with the approved welcome text, word for word, then the instructor introduction, then the agenda."))

    # ---- K1 an opening class says what its kit says it must say (PSP, attendance, contests, FAQs, dashboard video, support)
    for item, (label, fact) in (required_content or {}).items():
        pattern = REQUIRED_ITEMS[item][1]
        if not any(re.search(pattern, f"{c.get('title') or ''} {c.get('content_script') or ''}", re.I) for c in cards):
            out.append(_finding(_home_card(cards, label), "K1", "major", f"The opening class never mentions {label}, and its program kit says it must.",
                                f"Add a short spoken passage about {label}. Use this approved fact exactly, changing no number, time or link: {fact}"))

    # ---- S5 every agenda item and outcome is covered, in the written cards
    texts = [f"{c.get('title') or ''} {c.get('content_script') or ''} {c.get('code_snippet') or ''}" for c in cards]
    for label, items in (("agenda item", agendas or []), ("learning outcome", split_items(_real_outcomes(outcomes)))):
        for item in items:
            if not covered(item, texts):
                out.append(_finding("all", "S5", "blocker", f"The {label} \"{item[:90]}\" is not covered by any card.",
                                    "Write or extend a card so it covers this."))
    out.extend(check_script_craft(cards, running_example))
    return out


# ---- the good-script rules that can be judged from the text itself (PRD "Good script rules"): paragraphs, sub-headings, the recap, a branching diagram,
# the running example, everyday analogies, no opening definition, unqualified claims, instructor notes, the card sequence
_EXOTIC = re.compile(r"\b(fighter jets?|rockets?|spaceships?|particle physics|quantum (?:computer|physics)|black holes?)\b", re.I)
_EVERYDAY = re.compile(r"\b(upi|whatsapp|zomato|swiggy|irctc|train|cricket|ipl|bank|atm|amazon|flipkart|uber|ola|google pay|phonepe|paytm|food delivery|cab|netflix|instagram|youtube|ticket|kirana|metro)\b", re.I)
_DEFINITION_OPEN = re.compile(r"^(?:a|an|the)?\s*[\w' -]{2,40}?\s+(?:is|are|means|refers to)\s+(?:a|an|the|when|used|defined|short for|known as)\b", re.I)
_ABSOLUTE = re.compile(r"\b(?:guaranteed|always works|never fails|100% (?:safe|secure|accurate)|completely safe|fully automatic(?:ally)?|works everywhere|cannot fail)\b", re.I)


def _sentences(text: str) -> List[str]:
    return [x for x in re.split(r"(?<=[.!?])\s+", (text or "").strip()) if x.strip()]


def diagram_branches(mermaid: str) -> bool:
    """True when a Mermaid diagram branches or compares: a decision shape, one node with two or more arrows leaving it, or two parallel groups."""
    src = mermaid or ""
    if re.search(r"\w+\s*\{[^}]+\}", src) or len(re.findall(r"^\s*subgraph\b", src, re.M)) >= 2:
        return True
    out_counts: Dict[str, int] = {}
    for m in re.finditer(r"(?<![\w-])([A-Za-z_]\w*)(?:\s*[\[(\{>][^\]\)\}\n]*[\]\)\}]?)?\s*(?:-->|---|==>|-\.->)", src):
        out_counts[m.group(1)] = out_counts.get(m.group(1), 0) + 1
    return any(n >= 2 for n in out_counts.values())


def check_script_craft(cards: List[Dict[str, Any]], running_example: Optional[str] = None) -> List[Dict[str, Any]]:
    """The craft rules of the PRD that code can judge. Most are 'minor': they guide the author without blocking the script."""
    out: List[Dict[str, Any]] = []
    teaching = [c for c in cards if _kind(c) in ("concept", "problem", "walkthrough")]
    for c in cards:
        cid, kind, body = c.get("card_index"), _kind(c), c.get("content_script") or ""
        prose = re.sub(r"```.*?```", " ", body, flags=re.S)
        if kind in ("concept", "problem", "walkthrough", "recap", "faq", "agenda"):
            # R9 paragraphs of 2 to 4 sentences
            for para in re.split(r"\n\s*\n", prose):
                p = para.strip()
                if not p or re.match(r"^(?:#|>|[-*\u2022]|\d{1,2}[.)]|\|)", p) or p.startswith("[["):
                    continue
                n = len(_sentences(re.sub(r"\[[^\]]*\]", " ", p)))
                if n > 4:
                    out.append(_finding(cid, "R1", "minor", f"A paragraph has {n} sentences; keep paragraphs to 2 to 4.", "Split it, or cut the repeats.", quote=p[:100]))
                    break
        if kind in ("concept", "problem", "walkthrough") and float(c.get("duration_mins") or 0) >= 4:
            # R10 two to four sub-headings on a teaching card
            heads = len(re.findall(r"^\s*#{1,6}\s+\S|^\s*\*\*[^*\n]{2,80}\*\*:?\s*$", prose, re.M))
            if heads < 2 or heads > 4:
                out.append(_finding(cid, "R13", "minor", f"The card has {heads} sub-headings; a teaching card has 2 to 4.",
                                    "Break the card into 2 to 4 short parts, each with a bold sub-heading."))
        if kind == "concept":
            # T1 never open with a definition
            first = _sentences(spoken_text(body))[:1]
            if first and _DEFINITION_OPEN.match(first[0]):
                out.append(_finding(cid, "T1", "minor", "The card opens with a definition.", "Open with the why: a situation or a problem the learner recognises, then the idea.", quote=first[0][:100]))
        if _EXOTIC.search(prose):
            out.append(_finding(cid, "E4", "major", "The card uses an exotic analogy (" + _EXOTIC.search(prose).group(1) + ").",
                                "Use an everyday one: UPI, WhatsApp, food delivery, train booking, cricket or banking."))
        if _ABSOLUTE.search(prose):
            out.append(_finding(cid, "A1", "minor", "The card makes an unqualified claim (" + _ABSOLUTE.search(prose).group(0) + ").",
                                "Name the boundary: when, where and under what conditions it holds.", quote=prose[max(0, _ABSOLUTE.search(prose).start() - 30):_ABSOLUTE.search(prose).end() + 30]))
        # N1 an instructor note is one or two sentences of delivery hints
        cue = re.sub(r"\[\[[^\]]*\]\]", " ", (c.get("stage_directions") or ""))
        note_body = re.sub(r"\[\[[^\]]*\]\]", " ", body) if kind == "instructor_note" else ""
        for label, text in (("stage cue", cue), ("note", note_body)):
            if len(_sentences(text)) > 2:
                out.append(_finding(cid, "R10", "minor", f"The instructor {label} has {len(_sentences(text))} sentences; keep it to 1 or 2.", "Cut it to the delivery hints only."))
                break
    # R11 the recap is one visual mental model
    recaps = [c for c in cards if _kind(c) == "recap"]
    for c in recaps:
        if not c.get("visual_type"):
            out.append(_finding(c.get("card_index"), "R11", "major", "The recap has no visual.", "Give the recap one diagram that shows the whole class as a single mental model."))
        elif len(re.findall(r"\[\[VISUAL", c.get("content_script") or "")) > 1:
            out.append(_finding(c.get("card_index"), "R11", "minor", "The recap has more than one visual.", "Keep one visual: the single mental model of the class."))
    # R12 at least one diagram branches or compares
    mermaids = [c for c in cards if c.get("visual_type") == "mermaid" and c.get("visual_content")]
    if mermaids and not any(diagram_branches(c["visual_content"]) for c in mermaids):
        out.append(_finding("all", "R12", "major", "No diagram branches or compares.", "Make at least one diagram a decision or a comparison: two paths, or two things side by side."))
    # T4 one running example, with the same names, through the class
    if running_example and len(teaching) >= 3:
        terms = [w for w in re.findall(r"[A-Z][a-z]{3,}", running_example)] or [w for w in re.findall(r"[a-z]{5,}", running_example.lower())][:2]
        if terms:
            using = sum(1 for c in teaching if any(t.lower() in f"{c.get('title') or ''} {c.get('content_script') or ''}".lower() for t in terms))
            if using < 0.5 * len(teaching):
                out.append(_finding("all", "T4", "minor", f"The running example \"{running_example[:60]}\" appears in only {using} of {len(teaching)} teaching cards.",
                                    "Carry the same example, with the same names, through the class."))
    # E4 an everyday analogy somewhere in the class
    if len(teaching) >= 3 and not any(_EVERYDAY.search(c.get("content_script") or "") for c in teaching):
        out.append(_finding("all", "E4", "minor", "No analogy from everyday life was found.", "Use one analogy per concept from UPI, WhatsApp, food delivery, train booking or cricket."))
    # A2 sequences are labelled simplified in the first card, when the class draws flows
    first_spoken = next((c for c in cards if _kind(c) != "instructor_note"), None)
    if mermaids and first_spoken is not None and not re.search(r"simplif", " ".join(_sentences(spoken_text(first_spoken.get("content_script")))[:3]), re.I):
        out.append(_finding(first_spoken.get("card_index"), "A2", "minor", "The class draws sequences, but the first card never says they are simplified.",
                            "Say in the first sentences that the sequences shown are simplified."))
    # S2 the card order: faq before the recap, and the recap last
    kinds = [_kind(c) for c in cards]
    if "recap" in kinds and kinds.index("recap") != len(kinds) - 1 and "faq" not in kinds[kinds.index("recap") + 1:] :
        out.append(_finding(cards[kinds.index("recap")].get("card_index"), "S2", "minor", "The recap is not the last card.", "Put the FAQ before the recap and end the class on the recap."))
    if "faq" in kinds and "recap" in kinds and kinds.index("faq") > kinds.index("recap"):
        out.append(_finding(cards[kinds.index("faq")].get("card_index"), "S2", "minor", "The FAQ comes after the recap.", "Put the FAQ before the recap."))
    # K5 the agenda card holds only the agenda, and the welcome card holds no agenda list
    for c in cards:
        body, kind = c.get("content_script") or "", _kind(c)
        if kind == "agenda" and spoken_words(body) > 120:
            out.append(_finding(c.get("card_index"), "K5", "minor", "The agenda card holds more than the agenda.",
                                "Keep only a one-line lead-in, the numbered agenda with minutes, and one line to move on; put anything else in the welcome card."))
        elif kind == "welcome" and len(re.findall(r"^\s*\d+\.\s.*\(?\b\d+ minutes?\b", body, re.M)) >= 2:
            out.append(_finding(c.get("card_index"), "K5", "minor", "The welcome card lists the agenda; the agenda has its own card.",
                                "Take the agenda list out of the welcome card."))
    # S9 no card number is missing (a later card may lean on the one that is gone)
    nums = sorted(int(c["card_index"]) for c in cards if str(c.get("card_index") or "").isdigit())
    gaps = [n for n in range(nums[0], nums[-1]) if n not in nums] if nums else []
    if gaps:
        out.append(_finding("all", "S9", "major", f"Card {', '.join(str(g) for g in gaps)} is missing; the cards jump over it.",
                            "Write the missing card, or renumber and check the card after the gap still makes sense."))
    from agents.safe_html import animation_is_complete
    for c in cards:
        cid, body = c.get("card_index"), c.get("content_script") or ""
        # V6 a diagram that cannot be drawn shows "Syntax error in text" instead
        if c.get("visual_type") == "mermaid" and c.get("visual_content"):
            problem = mermaid_syntax_problem(c["visual_content"])
            if problem:
                out.append(_finding(cid, "V6", "major", f"The diagram cannot be drawn: {problem}.", "Make the diagram again, with the text of every box in quotes."))
        # V3 an animation the model did not finish shows nothing
        if c.get("visual_type") == "animation" and not animation_is_complete(c.get("visual_content")):
            out.append(_finding(cid, "V3", "blocker", "The animation was cut off while it was being written, so it cannot play.", "Regenerate the animation."))
        # N2 generator plumbing must not reach the instructor
        m = re.search(r"zero-based|option index|quiz json|\[Visual V\d+|not asked for, because", body, re.I)
        if m:
            out.append(_finding(cid, "N2", "minor", "Production wording from the generator is in the spoken text.", "Delete it; the instructor does not need it.", quote=body[max(0, m.start() - 30):m.end() + 60]))
    out.extend(check_visuals_and_quizzes(cards))
    return out


_VISUAL_NOISE = {"flowchart", "graph", "subgraph", "end", "classdef", "style", "play", "replay", "reset", "start", "next", "back", "step", "stage", "button", "click",
                 "caption", "animation", "interactive", "figure", "draw", "show", "diagram", "left", "right", "color", "fill", "stroke", "font", "size", "border",
                 "width", "height", "margin", "padding", "display", "flex", "center", "none", "block", "true", "false", "function", "const", "var", "document",
                 "element", "class", "div", "span", "auto", "transition", "opacity", "transform", "background", "text", "html", "body", "head", "title", "meta",
                 "charset", "viewport", "script", "prefers", "scheme", "dark", "light", "media", "label", "box", "item", "node", "arrow", "line"}
_MERMAID_TEXT = re.compile(r'\[\(?"?([^\]"\)]+)|\{([^}]+)\}|\|([^|]+)\|')


def visual_words(c: Dict[str, Any]) -> set:
    """What a diagram, animation or image is about: the words a person reads in it (node labels, captions, stage text), without drawing or code syntax."""
    kind, content = c.get("visual_type"), c.get("visual_content") or ""
    text = ""
    if kind == "mermaid":
        text = " ".join(x for t in _MERMAID_TEXT.findall(re.sub(r"%%.*", " ", content)) for x in t if x)
    elif kind == "animation":
        text = re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", content))
    return _topic_words(text + " " + (c.get("visual_caption") or "")) - _VISUAL_NOISE


def _ratio(a: set, b: set) -> float:
    return len(a & b) / len(a) if a else 1.0


def check_visuals_and_quizzes(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A diagram or animation must be about the card it sits in, and a quiz must be about the script and be the question the instructor reads out.
    Judged by the words they share, so it is free and repeatable."""
    out: List[Dict[str, Any]] = []
    script_words: set = set()
    for c in cards:
        script_words |= _topic_words((c.get("title") or "") + " " + (c.get("content_script") or ""))
    for i, c in enumerate(cards):
        cid, body = c.get("card_index"), c.get("content_script") or ""
        card_words = _topic_words(" ".join([c.get("title") or "", body, c.get("stage_directions") or ""]))
        around = card_words | _topic_words(" ".join((cards[j].get("title") or "") + " " + (cards[j].get("content_script") or "")
                                                    for j in (i - 1, i + 1) if 0 <= j < len(cards)))
        # V4 the diagram, animation or image is about this card
        if c.get("visual_type") in ("mermaid", "animation", "image"):
            vw = visual_words(c)
            if len(vw) >= 3:
                shared, near_shared = len(vw & card_words), len(vw & around)
                if shared == 0 or (shared < 2 and near_shared < 2 and _ratio(vw, script_words) < 0.4):
                    out.append(_finding(cid, "V4", "major", f"The {c.get('visual_type')} is not about this card: almost nothing it shows is in the card's text.",
                                        "Regenerate it from this card's own steps and names, or remove it.", quote=" ".join(sorted(vw))[:100]))
                elif _ratio(vw, script_words) < 0.25:
                    out.append(_finding(cid, "V4", "major", f"The {c.get('visual_type')} shows terms that appear nowhere in the script.",
                                        "Regenerate it using only the names the script uses.", quote=" ".join(sorted(vw - script_words))[:100]))
        # Q1 to Q3 the quiz under the card
        q = c.get("quiz")
        if isinstance(q, dict) and (q.get("question") or q.get("options")):
            opts = [str(o) for o in (q.get("options") or [])]
            idx = q.get("correct_option_index")
            if opts and (not isinstance(idx, int) or isinstance(idx, bool) or not 0 <= idx < len(opts)):
                out.append(_finding(cid, "Q2", "blocker", "The quiz has no valid correct answer.", "Mark exactly one of the options as correct."))
            if len({re.sub(r"\W+", " ", o).strip().lower() for o in opts}) < len(opts):
                out.append(_finding(cid, "Q2", "major", "Two quiz options say the same thing.", "Make every option different."))
            qw = _topic_words(q.get("question") or "")
            if qw:
                hits = [_ratio(_topic_words(o), card_words) for o in opts if _topic_words(o)]
                mix = (_ratio(qw, card_words) + (sum(hits) / len(hits) if hits else 1.0)) / 2
                lines = [l for l in body.splitlines() if l.strip()]
                spoken_opts = re.findall(r"^\W*[A-D][.)]\s", body, re.M)
                if len(spoken_opts) >= 3 and opts:    # the card prints its own options: each on-screen option must be one of them
                    windows = [_topic_words(" ".join(lines[k:k + 2])) for k in range(len(lines))]
                    best = [max((_ratio(_topic_words(o), w) for w in windows), default=0.0) for o in opts if _topic_words(o)]
                    if best and sum(best) / len(best) < 0.75:
                        mix = 0.0
                if mix < 0.6:
                    out.append(_finding(cid, "Q1", "major", "The quiz on screen is not the question the instructor reads out in the card.",
                                        "Make the spoken question and options and the on-screen quiz the same.", quote=(q.get("question") or "")[:100]))
                if _ratio(qw, script_words) < 0.5:
                    out.append(_finding(cid, "Q3", "major", "The quiz asks about something this script does not teach.",
                                        "Write the quiz from what the cards say.", quote=(q.get("question") or "")[:100]))
    return out


_CUE = re.compile(r"(?<!\[)\[(?!\[)([^\[\]]*)\]")
_DIRECTIVE = re.compile(
    r"(?im)(?:^|[.!?:]\s+|\n)\s*(?:greet\b|read out\b|pause and (?:watch|wait|read)|move (?:straight )?(?:on )?to the\b|do a quick\b|"
    r"(?:ask|tell|invite|encourage|remind|get) (?:the )?(?:learners|students|everyone|the class|them)\b|"
    r"make sure (?:you|to|that)\b|remember to\b|be sure to\b|you are live\b|you should now\b|keep the energy\b)")
_THIRD_PERSON = re.compile(r"\b(?:the|these) (?:learners|students)\b|\btheir (?:very )?first (?:session|class)\b|\bthe instructor\b", re.I)
MAX_CUE_WORDS = 8


def spoken_text(body: Optional[str]) -> str:
    """The words the instructor says in a card, as plain text: no code blocks, [[markers]], [cues], markdown symbols or --- dividers. A heading ends with a full
    stop, so it is its own sentence and never runs into the one under it."""
    text = re.sub(r"```.*?```", " ", body or "", flags=re.S)
    text = re.sub(r"\[\[[^\]]*\]\]|\[[^\[\]]*\]", " ", text)
    text = re.sub(r"^[ \t]*([-*_])\1{2,}[ \t]*$", " ", text, flags=re.M)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$", lambda m: m.group(1) + ("" if re.search(r"[.!?:]$", m.group(1)) else "."), text, flags=re.M)
    text = re.sub(r"^[ \t]*(?:>|[-*\u2022]|\d{1,2}[.)])[ \t]+", "", text, flags=re.M)
    text = re.sub(r"[*_`]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


def spoken_tail(body: Optional[str], words: int = 60) -> str:
    """The last `words` words a card says, so the next card can pick up exactly where it stopped."""
    parts = spoken_text(body).split(" ")
    return " ".join(parts[-words:]) if parts and parts[0] else ""


_CONNECTIVE = re.compile(r"\b(now|next|so|alright|all right|okay|ok|right|let'?s|let us|we just|we'?ve|we have|you'?ve|you have|earlier|last|before|previous|"
                         r"so far|moving on|great|good|nice|well|with that|just saw|just learned|just now|having|that was|that gives|that'?s|those|these)\b", re.I)
_GREETING = re.compile(r"^\W*(hey|hi|hello|good (morning|evening|afternoon)|welcome|namaste)\b", re.I)


def _topic_words(text: str) -> set:
    """Significant words with punctuation removed and plurals folded (tables and table are one word), for comparing two stretches of speech."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if w in _STOP or len(w) < 3:
            continue
        out.add(w[:-1] if w.endswith("s") and len(w) > 3 and not w.endswith("ss") else w)
    return out


_EXCLUSION = re.compile(r"(?i)\b(?:do not|don't|never|not|no)\b[^.;]*\b(?:teach|explain|introduce|cover|show|use)\b|\b(?:later|another|next|upcoming) class\b|\bcovered (?:in|by) a later\b|\bbelongs? to (?:a )?later\b")


def _without_exclusions(note: Optional[str]) -> str:
    """The plan note without the sentences that say what the card must NOT teach ("do not teach recursion itself, a later class covers that"). Such a sentence
    names the topic only to keep it out, so it must not count as the card planning it."""
    sentences = re.split(r"(?<=[.!?;])\s+", note or "")
    return " ".join(x for x in sentences if not _EXCLUSION.search(x))


def class_scope_words(agendas, outcomes, class_title: str = "", upcoming=None) -> set:
    """The words that belong to THIS class: its agenda items and learning outcomes. The class title is only a label and does not widen the scope (a topic named in the title
    but not in the agenda is not this class's to teach), so it is used only when no agenda or outcome was given at all. A word that is in most of the later classes' titles too,
    like the course prefix in "DSA: Recursion" and "DSA: Hashing", is not a topic, so it never makes a later class look like it overlaps."""
    words = _topic_words(" ".join(list(agendas or [])) + " " + (outcomes or "")) or _topic_words(class_title or "")
    titles = [u for u in (upcoming or []) if u.get("title")]
    if len(titles) >= 2:
        counts: Dict[str, int] = {}
        for u in titles:
            for w in _topic_words(u["title"]):
                counts[w] = counts.get(w, 0) + 1
        words |= {w for w, n in counts.items() if n >= max(2, len(titles) / 2)}
    return words


def upcoming_overlap(text: str, upcoming: List[Dict[str, Any]], own_words: set, sentences_needed: int = 3) -> Optional[Dict[str, Any]]:
    """The first item of an upcoming class's AGENDA that this text really TEACHES, or None. Only the agenda items count: a later class's title is a label, and a topic that
    is in its title but not in its agenda is free to be taught here. An upcoming class with no real agenda yet has nothing to match, so it is never flagged. An item counts when most
    of its distinctive words (those not already in this class's own agenda and outcomes) appear AND they come up in several sentences: a single forward-pointing sentence is
    allowed (rule S6)."""
    spoken = spoken_text(text)
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", spoken) if s.strip()]
    body = _topic_words(spoken)
    for u in upcoming or []:
        for item in list(u.get("items") or []):
            distinct = _topic_words(item or "") - own_words
            if not distinct:
                continue
            have = distinct & body
            if len(distinct) >= 2 and len(have) / len(distinct) < 0.8:
                continue
            if len(distinct) == 1 and not have:
                continue
            hits = sum(1 for s in sentences if _topic_words(s) & have)
            if hits >= sentences_needed + (1 if len(distinct) == 1 else 0):
                return {"number": u.get("number"), "class": u.get("title"), "item": item}
    return None


_RECALL_CUE = re.compile(r"\b(last (class|time|session|week)|previous (class|session)|we (ended|stopped|left|finished|built|saw|covered|learned|learnt|looked)|welcome back|"
                         r"earlier (class|session)|where we (left|stopped|ended)|picking up|pick up|so far)\b", re.I)


def continuity_problem(body: str, previous: Dict[str, Any], spoken_before: bool = True) -> Optional[str]:
    """A message when a card starts as if nothing came before it: a fresh greeting, or no link at all to where the previous card stopped."""
    start = spoken_text(body)[:260]
    if not start:
        return None
    if _GREETING.match(start) and spoken_before:       # the first spoken card is allowed to welcome the class
        return "The card greets the class again, as if it were the first card."
    if _GREETING.match(start):
        return None
    first = " ".join(start.split(" ")[:40])
    if _CONNECTIVE.search(first):
        return None
    prev_topic = _topic_words((previous.get("title") or "") + " " + spoken_tail(previous.get("content_script"), 40))
    if len(prev_topic & _topic_words(first)) >= 2:
        return None
    return "The card starts cold: its opening does not connect to where the previous card stopped."


def spoken_words(body: str) -> int:
    """How many words the instructor actually says in a card: code blocks, [[markers]] and [bracketed cues] are not spoken."""
    text = re.sub(r"```.*?```", " ", body or "", flags=re.S)
    text = re.sub(r"\[\[[^\]]*\]\]|\[[^\[\]]*\]", " ", text)
    return len(re.findall(r"[A-Za-z0-9][\w'\u2019-]*", text))


def voice_problem(body: str) -> Optional[Tuple[str, str]]:
    """(problem, quote) when a card's text is written to the instructor instead of being what the instructor says; else None.
    Code blocks and [[VISUAL]] / [[FROM_SME]] markers are ignored; a bracketed cue is allowed up to MAX_CUE_WORDS words."""
    text = re.sub(r"```.*?```", " ", body or "", flags=re.S)
    for cue in _CUE.findall(text):
        if len(cue.split()) > MAX_CUE_WORDS:
            return (f"A bracketed stage direction runs {len(cue.split())} words; a cue is at most {MAX_CUE_WORDS}, and the rest must be spoken.", cue[:120])
    spoken = _CUE.sub(" ", re.sub(r"\[\[[^\]]*\]\]", " ", text))
    m = _DIRECTIVE.search(spoken)
    if m:
        return ("The card tells the instructor what to do instead of giving the words to say.", spoken[m.start(): m.end() + 60].strip())
    m = _THIRD_PERSON.search(spoken)
    if m:
        return ("The card talks about the learners in the third person; the instructor speaks to them as 'you'.", spoken[max(0, m.start() - 30): m.end() + 30].strip())
    return None


REQUIRED_ITEMS = {
    # item: (label, regex that shows a card mentions it, regex for the kit fact that holds its details)
    "psp": ("PSP (Problem Solving Percentage)", r"\bPSP\b|problem solving percentage", r"psp"),
    "attendance": ("attendance", r"attendance", r"attendance"),
    "contests": ("contests", r"\bcontests?\b", r"contest"),
    "faq": ("the FAQs", r"\bFAQs?\b|frequently asked", r"faq|notes_and_assignments"),
    "dashboard": ("the dashboard video", r"dashboard", r"dashboard"),
    "support": ("the support contact", r"\bsupport\b|whatsapp|slack|help channel", r"support|getting_help|community"),
}
_PLACEHOLDER = re.compile(r"\[\[FROM_SME:?\s*([^\]]*)\]\]")


def kit_required_items(kit: Optional[Dict[str, Any]]) -> Tuple[Dict[str, Tuple[str, str]], List[str]]:
    """What a program kit obliges the opening class to say, and what the kit cannot supply.
    Returns ({item: (label, approved fact)}, [labels the SME still has to provide]). An item is required only when the kit holds a real fact
    for it; one the kit lacks (or holds as a [[FROM_SME]] placeholder) goes to the SME's list instead of failing the script."""
    required: Dict[str, Tuple[str, str]] = {}
    gaps: List[str] = []
    facts = (kit or {}).get("facts") or {}
    for item, (label, _present, key_rx) in REQUIRED_ITEMS.items():
        real = [str(v) for k, v in facts.items() if re.search(key_rx, str(k), re.I) and "[[FROM_SME" not in str(v)]
        if real:
            required[item] = (label, " ".join(real)[:400])
        else:
            gaps.append(label)
    return required, gaps


def _home_card(cards: List[Dict[str, Any]], label: str) -> Any:
    """The card a missing item belongs on: the one whose title is closest to it, else the agenda card, else the first card."""
    want = words(label)
    best = max(cards, key=lambda c: len(want & words(c.get("title") or "")), default=None)
    if best is not None and want & words(best.get("title") or ""):
        return best.get("card_index")
    for c in cards:
        if _kind(c) == "agenda":
            return c.get("card_index")
    return cards[0].get("card_index") if cards else "all"


def collect_sme_items(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every [[FROM_SME: ...]] placeholder in the script, for the SME to fill in on one screen."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        body = c.get("content_script") or ""
        for m in _PLACEHOLDER.finditer(body):
            out.append({"card_index": c.get("card_index"), "title": c.get("title"), "need": m.group(1).strip() or "information from the SME",
                        "placeholder": m.group(0), "context": body[max(0, m.start() - 80): m.end() + 80].replace("\n", " ")})
    return out


def normalise_typography(text: str) -> str:
    """Models like non-breaking hyphens (B\u2011tree) and narrow spaces. They look like punctuation problems to a reader
    and break search and copy-paste, so turn them into plain characters."""
    return (text or "").replace("\u2011", "-").replace("\u2010", "-").replace("\u202f", " ").replace("\u00a0", " ")


_HEADER_LINE = re.compile(r"^[ \t>*#_-]*(?:card[ _]title|pedagogical[ _]phase|card[ _]type|card[ _]index)[ \t*_]*[:=][^\n]*\n?", re.I | re.M)


def strip_card_headers(text: Optional[str]) -> str:
    """Remove prompt scaffolding a model repeats back into a card ('CARD TITLE: ...', 'PEDAGOGICAL PHASE: ...'). Those are labels for the
    model, never part of what the instructor says."""
    return _HEADER_LINE.sub("", text or "").lstrip("\n")


def clean_spoken(text: Optional[str]) -> str:
    """A freshly written card's text, tidied before it is saved: model label lines and non-breaking characters removed, and no em dashes (R8)."""
    out = normalise_typography(strip_card_headers(text))
    return out.replace(" " + EM_DASH + " ", ", ").replace(EM_DASH, ", ")


_NO_SPLIT_START = re.compile(r"^\s*(?:#|>|[-*\u2022]\s|\d{1,2}[.)]\s|\||\[|\*\*[^*]+\*\*:?\s*$|```)")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z\"'\*\(])")
_ABBREV = re.compile(r"(?:\be\.g|\bi\.e|\bvs|\betc|\bMr|\bMs|\bDr|\bNo|\bFig)\.$", re.I)


def split_long_paragraphs(text: str, longest: int = 4) -> str:
    """A free fix for rule R1: a paragraph of more than `longest` sentences is cut into paragraphs of about equal length, at sentence ends. Headings, lists, quotes,
    callouts, code, tables, bracketed cues and anything with a placeholder are left as they are, and so is a paragraph whose sentence ends are not clear."""
    out, fenced = [], False
    for block in re.split(r"\n\s*\n", text or ""):
        if "```" in block:
            fenced = block.count("```") % 2 == 1 if not fenced else (block.count("```") % 2 == 0)
            out.append(block)
            continue
        if fenced or _NO_SPLIT_START.match(block) or "[[" in block or "\n" in block.strip():
            out.append(block)
            continue
        parts, start = [], 0
        for m in _SENTENCE_END.finditer(block):
            if not _ABBREV.search(block[start:m.start()].rstrip()[-8:]):
                parts.append(block[start:m.start()]); start = m.end()
        parts.append(block[start:])
        if len(parts) <= longest:
            out.append(block)
            continue
        k = -(-len(parts) // longest)                    # how many paragraphs, each of about the same length
        size = -(-len(parts) // k)
        out.extend(" ".join(parts[i:i + size]) for i in range(0, len(parts), size))
    return "\n\n".join(out)


def fix_automatically(cards: List[Dict[str, Any]]) -> int:
    """Apply the fixes code can make safely (em dashes). Changes the cards in place and returns how many changed."""
    changed = 0
    for c in cards:
        for key in ("content_script", "title", "stage_directions"):
            val = c.get(key)
            if isinstance(val, str):
                new = normalise_typography(val).replace(" " + EM_DASH + " ", ", ").replace(EM_DASH, ", ")
                if key == "content_script":
                    new = split_long_paragraphs(strip_card_headers(new))
                if new != val:
                    c[key] = new
                    changed += 1
    return changed


def as_review(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The findings in the shape of a reviewer's result, so the Moderator and the UI treat them like a lens."""
    blockers = sum(f["severity"] == "blocker" for f in findings)
    majors = sum(f["severity"] == "major" for f in findings)
    minors = sum(f["severity"] == "minor" for f in findings)
    penalty = 2.5 * blockers + 1.0 * majors + 0.25 * minors
    score = round(10.0 / (1.0 + penalty / 8.0), 1)       # falls with every finding but never sits at zero, so a bad script and a terrible one still differ
    passed = blockers == 0 and majors == 0
    return {
        "lens_name": "Rule Check (automatic)",
        "reviewer_persona": "Rule checker (code, no AI)",
        "verdict": "pass" if passed else "fail", "score": score, "passed": passed, "ran": True,
        "blockers": blockers, "majors": majors, "minors": minors,
        "summary": "All automatic rules pass." if passed else f"{blockers} blockers, {majors} majors, {minors} minors from the automatic rule checks.",
        "findings": findings,
        "detailed_findings": [f"[{f['severity'].upper()}] Card {f['card_id']} {f['rule']}: {f['problem']}" for f in findings],
    }


def check_outline(outline: Dict[str, Any], target_minutes: int, agendas: Optional[List[str]], outcomes: Optional[str],
                  class_title: str = "", required_topics: Optional[List[str]] = None, min_animations: int = 0,
                  beginner_pct: Optional[int] = None, code_ratio: Optional[int] = None, upcoming: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, str]]:
    """Problems in a proposed outline, found by code: agenda and outcomes not mapped, minutes that do not add up,
    a wrong card count, too few visuals, and visuals that do not serve the scope. Each is {level, problem}."""
    out: List[Dict[str, str]] = []
    cards = [c for s in outline.get("sections", []) for c in s.get("cards", [])]
    if not cards:
        return [{"level": "blocker", "problem": "The outline has no cards."}]

    total = round(sum(c.get("duration_sec", 0) for c in cards) / 60.0, 1)
    if abs(total - target_minutes) > 0.5:
        out.append({"level": "blocker", "problem": f"Card minutes add up to {total}, not {target_minutes}."})
    lo, hi = cards_for_duration(target_minutes)
    if not lo <= len(cards) <= hi:
        out.append({"level": "major", "problem": f"{len(cards)} cards planned; a {target_minutes} minute class needs {lo} to {hi}."})

    for c in cards:
        kind = _kind({"card_type": c.get("card_type")})
        rng = (LIMITS["teaching_card_minutes"] if kind in TEACHING_CARD_TYPES else
               LIMITS["quiz_card_minutes"] if kind == "quiz" else LIMITS["note_card_minutes"])
        mins = round((c.get("duration_sec") or 0) / 60.0, 1)
        if not rng[0] - 0.05 <= mins <= rng[1] + 0.05:
            out.append({"level": "major", "problem": f"Card \"{c.get('title')}\" ({kind}) is planned at {mins} minutes; the range is {rng[0]} to {rng[1]}."})

    # an agenda item or outcome counts as mapped only when a card names it (title, agenda_items or outcomes)
    mapped = [" ".join([c.get("title") or ""] + list(c.get("agenda_items") or []) + list(c.get("outcomes") or [])) for c in cards]
    for label, items in (("agenda item", agendas or []), ("learning outcome", split_items(_real_outcomes(outcomes)))):
        for item in items:
            if not covered(item, mapped, threshold=0.6):
                out.append({"level": "blocker", "problem": f"The {label} \"{item[:90]}\" is not mapped to any card."})

    # the other way round: every teaching card serves something the author asked for. The class title is a label, so a topic that is only in the title is outside the scope.
    scope_items = list(agendas or []) + split_items(outcomes)
    if scope_items:
        for c in cards:
            if c.get("card_type") in ("concept", "problem", "walkthrough"):
                served = " ".join(list(c.get("agenda_items") or []) + list(c.get("outcomes") or []))
                if not any(covered(item, [served], threshold=0.6) for item in scope_items):
                    out.append({"level": "major", "problem": f"Card \"{c.get('title')}\" serves nothing in this class's agenda or learning outcomes, so it is outside the scope."})

    # the opening kit lists what an opening class must cover; most of it has to be planned as cards
    if required_topics:
        text = [" ".join([c.get("title") or "", c.get("teaching_shape_note") or ""] + list(c.get("agenda_items") or [])) for c in cards]
        missing = [t for t in required_topics if not covered(t, text, threshold=0.5)]
        if len(missing) > 0.3 * len(required_topics):
            out.append({"level": "major", "problem": "The opening kit topics not planned as cards: " + "; ".join(missing)})

    visuals = outline.get("visual_cards") or []
    if len(visuals) < LIMITS["min_diagrams"]:
        out.append({"level": "major", "problem": f"{len(visuals)} visual cards planned; at least {LIMITS['min_diagrams']} are required."})
    # nothing planned here may belong to an upcoming class
    if upcoming:
        own = class_scope_words(agendas, outcomes, class_title, upcoming)
        for c in cards:
            if c.get("card_type") not in ("concept", "problem", "walkthrough"):
                continue
            hit = upcoming_overlap(" ".join([c.get("title") or "", _without_exclusions(c.get("teaching_shape_note"))] + list(c.get("agenda_items") or [])), upcoming, own, sentences_needed=0)
            if hit:
                out.append({"level": "major", "problem": f"Card \"{c.get('title')}\" plans \"{hit['item']}\", which belongs to upcoming class {hit['number']} \"{hit['class']}\". "
                                                    f"Take it out of this card: keep only what this class's agenda items ask for, and say in the plan note \"do not teach {hit['item']}\"."})
    # the SME's beginner/advanced split, in teaching minutes, and the code ratio, in cards: the plan must match what was asked for
    teaching = [c for c in cards if c.get("card_type") in ("concept", "problem", "walkthrough")]
    if beginner_pct is not None and teaching:
        if any(not c.get("level") for c in teaching):
            out.append({"level": "major", "problem": "The cards have no beginner/advanced level yet, so the plan cannot follow the requested split."})
        else:
            total = sum(c.get("duration_sec") or 0 for c in teaching) or 1
            share = 100.0 * sum((c.get("duration_sec") or 0) * {"beginner": 1.0, "bridge": 0.5}.get(c.get("level"), 0.0) for c in teaching) / total
            if abs(share - beginner_pct) > 12:
                out.append({"level": "major", "problem": f"{round(share)}% of the teaching minutes are planned at beginner level; the SME asked for {beginner_pct}%."})
            levels = [c.get("level") for c in teaching]
            order = {"beginner": 0, "bridge": 1, "advanced": 2}
            if [order.get(l, 0) for l in levels] != sorted(order.get(l, 0) for l in levels):
                out.append({"level": "major", "problem": "The levels do not climb in order: a beginner card comes after an advanced one."})
    if code_ratio is not None and cards:
        coded = sum(1 for c in cards if c.get("has_code"))
        eligible = len(teaching)
        want = round(code_ratio / 100.0 * len(cards))
        if coded < min(want, eligible) - 1 or (cards and abs(100.0 * coded / len(cards) - code_ratio) > 12 and eligible >= want):
            out.append({"level": "major", "problem": f"{coded} of {len(cards)} cards are planned with code ({round(100.0 * coded / len(cards))}%); the SME asked for about {code_ratio}%."})
    animations = sum(1 for v in visuals if v.get("kind") == "animation")
    if min_animations and animations < min_animations:
        out.append({"level": "major", "problem": f"{animations} animation(s) planned; a beginner track needs at least {min_animations}."})
    scope_words = words(" ".join(list(agendas or []) + split_items(outcomes) + [class_title]))
    for v in visuals:
        serves = " ".join(list(v.get("serves_agenda_items") or []) + list(v.get("serves_outcomes") or []) + [v.get("prompt") or "", v.get("title") or ""])
        if scope_words and not (words(serves) & scope_words):
            out.append({"level": "major", "problem": f"Visual {v.get('visual_id')} \"{v.get('title')}\" does not relate to the agenda, outcomes or class topic."})
    return out
