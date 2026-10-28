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


# the verbs and filler every outcome starts with: a card teaches the topic, it does not have to say "identify" or "evaluate"
_GENERIC_OUTCOME_WORDS = {"learner", "learners", "identify", "evaluate", "accurately", "derive", "explain", "apply", "understand", "design", "solution", "problem",
                          "entry-level", "optimal", "common", "basic", "able", "can", "will", "using", "use"}


# outcome words a card usually says another way
_SAME_MEANING = {"performance": {"cost", "speed", "slow", "fast", "time"}, "production": {"real", "real-world", "checkout", "booking"},
                 "inefficienci": {"slow", "bottleneck", "cost", "cheaper"}, "snippet": {"code"}, "efficient": {"fast", "cheaper", "cost"}}


def covered_across(item: str, texts: List[str], threshold: float = 0.6) -> bool:
    """An outcome is often taught by several cards together (the cause in one, the fix in the next). It is covered when the whole script's words hold
    at least `threshold` of the item's significant words."""
    need = words((item or "").replace(".", " ")) - _GENERIC_OUTCOME_WORDS      # "snippets." ending a sentence is the word "snippets"
    if not need:
        return True
    have = set()
    for t in texts:
        have |= words((t or "").replace(".", " "))
    got = sum(w in have or bool(_SAME_MEANING.get(w, set()) & have) for w in need)
    return got / len(need) >= threshold


_LINE_CUES = (("loop", r"\bfor\b|\bwhile\b"), ("for", r"\bfor\b"), ("def", r"\bdef\b"), ("function", r"\bdef\b"),
              ("print", r"\bprint\b"), ("return", r"\breturn\b"), ("condition", r"\bif\b|\belif\b|\bwhile\b|\band\b|\bor\b|[=<>!]=?"))


def check_cited_lines(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """L2: when the text says "the outer loop on line 14", line 14 of the code must be a line like that. Free and exact: it counts the real lines.
    The code is the card's own, or the nearest earlier card's (a follow-up card often points back to the code it just showed)."""
    out: List[Dict[str, Any]] = []
    earlier = ""
    for c in cards:
        own = c.get("code_snippet") or ""
        pools = [p.split("\n") for p in (own, earlier) if p.strip()]
        if own.strip():
            earlier = own
        if not pools:
            continue
        for m in re.finditer(r"([^.\n]*?)\blines? (\d+)(?:\s*(?:and|to|-|,)\s*(\d+))?([^.\n]*)", spoken_text(c.get("content_script"))):
            before = m.group(1).lower()                    # the words just before "line N" say what that line is
            hits = [(mm.start(), rx) for word, rx in _LINE_CUES for mm in re.finditer(rf"\b{word}(?:s|es)?\b", before)]
            cue = max(hits)[1] if hits else None
            for g in (m.group(2), m.group(3)):
                if not g:
                    continue
                n = int(g)
                if not any(n <= len(lines) for lines in pools):
                    out.append(_finding(c.get("card_index"), "L2", "major", f"The text points to line {n}, but the code has only {len(pools[0])} lines.",
                                        "Use the real line number, or describe the line in words.", m.group(0).strip()))
                elif cue and not any(n <= len(lines) and re.search(cue, lines[n - 1]) for lines in pools):
                    shown = next(lines[n - 1] for lines in pools if n <= len(lines))
                    out.append(_finding(c.get("card_index"), "L2", "major",
                                        f"The text says line {n} is something the code's line {n} is not: it reads \"{shown.strip()[:60]}\".",
                                        "Use the real line number from the code, or describe the line in words.", m.group(0).strip()))
    return out


_PRINT_CLAIM = re.compile(r"\b(?:prints?|printed|outputs?|returns?|shows?|gives?|displays?)\b[^.\n]{0,40}?(?<![\w.])(\d[\d,]*(?:\.\d+)?)\b", re.I)


def check_numbers_match_output(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """L3: when the text says what the code prints ("it prints 16"), that number must be in what the code really printed. The run is the truth, so this is exact and free."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        run = c.get("code_run") or {}
        shown = run.get("stdout") if isinstance(run, dict) else None
        if not shown or run.get("status") not in (None, "passed"):
            continue
        printed = set(re.findall(r"\d+(?:\.\d+)?", shown.replace(",", "")))
        for m in _PRINT_CLAIM.finditer(spoken_text(c.get("content_script"))):
            around = spoken_text(c.get("content_script"))[max(0, m.start() - 160):m.end() + 40]
            if re.search(r"\b(instead of|would|if you|if the|forgetting|mistake|wrong|skips?|only)\b", around, re.I):
                continue                                      # a sentence about what a mistake would print, not what this code prints
            num = m.group(1).replace(",", "")
            if num not in printed and num not in re.findall(r"\d+(?:\.\d+)?", c.get("code_snippet") or ""):
                out.append(_finding(c.get("card_index"), "L3", "major", f"The text says the code gives {num}, but the code printed: {shown.strip()[:80]!r}.",
                                    "Use the number the code really prints.", m.group(0)))
    return out


_BOLD_TERM = re.compile(r"\*\*([A-Za-z][A-Za-z0-9 \-]{2,30})\*\*")


def check_term_order(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """T6: a term a teaching card defines in bold should not be leaned on by an earlier teaching card. Quiz, welcome, agenda and note cards are left out
    (a quiz may ask before it teaches). Minor, because an earlier mention is sometimes a deliberate preview."""
    out: List[Dict[str, Any]] = []
    first_bold: Dict[str, int] = {}
    for c in cards:
        if _kind(c) in ("concept", "problem", "walkthrough"):
            for t in _BOLD_TERM.findall(c.get("content_script") or ""):
                first_bold.setdefault(t.strip().lower(), c.get("card_index") or 0)
    for term, home in first_bold.items():
        if len(term) < 4 or term in ("note", "key", "remember", "mistake", "predict"):
            continue
        for c in cards:
            idx = c.get("card_index") or 0
            if idx >= home:
                break
            if _kind(c) in ("concept", "problem", "walkthrough") and re.search(rf"\b{re.escape(term)}\b", spoken_text(c.get("content_script")).lower()):
                out.append(_finding(idx, "T6", "minor", f'"{term}" is used here, but it is first explained in card {home}.',
                                    "Explain it here in one short sentence, or leave it for card " + str(home) + "."))
                break
    return out


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
    mermaid = re.sub(r'"[^"\n]*"|\|[^|\n]*\|', '""', mermaid or "")      # words inside labels, like T(n), are not nodes
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
        if planned_code is True and not has_code_now and kind in ("walkthrough", "problem", "concept", "quiz"):
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
                least = int(round(target * LIMITS["length_tolerance"][0] / 5.0) * 5)
                out.append(_finding(cid, "S8", "major" if words_in_card < target * 0.6 else "minor",
                                    f"The card has only {words_in_card} spoken words for {dur:g} minutes (about {target} fit, at least {least}), so part of its time would be empty.",
                                    f"Add about {max(5, target - words_in_card)} words of teaching: a worked example using the class's own data, a check question for the learners, or the reason "
                                    f"behind a step. Check every agenda item and outcome the card serves is explained. Do not repeat points or pad."))

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
        if re.search(r"Predict|\[\[\s*QUIZ\s*\]\]", body) and re.search(r"^\W*(?:Reveal\b|The answer (?:is|was)\b)", body, re.M | re.I):      # the answer line may start "Reveal:" or "The answer is"
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
            if not covered(item, texts) and not covered_across(item, texts):
                out.append(_finding("all", "S5", "blocker", f"The {label} \"{item[:90]}\" is not covered by any card.",
                                    "Write or extend a card so it covers this."))
    # ---- S5 each teaching card says what it was planned to teach: the agenda items the plan gave it appear in its own text
    real_agenda = {str(a).strip().lower(): str(a).strip() for a in (agendas or []) if str(a).strip()}
    for c in cards:
        if _kind(c) in ("concept", "problem", "walkthrough") and c.get("covers"):
            own = [f"{c.get('title') or ''} {c.get('content_script') or ''} {c.get('code_snippet') or ''}"]
            for item in c["covers"]:
                if str(item).strip().lower() in real_agenda and not covered(real_agenda[str(item).strip().lower()], own, threshold=0.34):
                    out.append(_finding(c.get("card_index"), "S5", "minor", f'This card is planned to teach "{real_agenda[str(item).strip().lower()][:80]}" but its text never gets to it.',
                                        "Teach that agenda item in this card, with an example, or move it to a card that does."))
    out.extend(check_script_craft(cards, running_example))
    out.extend(check_cited_lines(cards))
    out.extend(check_company_as_place(cards))
    out.extend(check_repetition(cards))
    out.extend(check_answer_given_away(cards))
    out.extend(check_quiz_teaches_first(cards))
    out.extend(check_spoken_polish(cards))
    out.extend(check_numbers_match_output(cards))
    out.extend(check_term_order(cards))
    # ---- K6 the last card tells the learners what the next class covers
    if upcoming:
        spoken = [c for c in cards if _kind(c) != "instructor_note" and (c.get("content_script") or "").strip()]
        nxt = upcoming[0]
        if spoken and nxt.get("title"):
            tail = " ".join(_sentences(spoken_text(spoken[-1].get("content_script")))[-4:])
            words = _topic_words(re.sub(r"^\s*[A-Za-z ]{1,25}:\s*", "", nxt["title"]))
            if words and _ratio(words, _topic_words(tail)) < 0.5:
                out.append(_finding(spoken[-1].get("card_index"), "K6", "minor", f"The last card does not say what the next class (\"{nxt['title']}\") covers.",
                                    "End the class with one or two sentences about the next class" + (f": {'; '.join(nxt['items'][:4])}." if nxt.get("items") else ", by name only: its agenda is not written yet.")))
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


def check_repetition(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Q5, C3 and Q6: the same quiz question twice, two code cards printing the same output, and a spoken answer that explains what the quiz widget already explains."""
    out: List[Dict[str, Any]] = []
    quizzes = [c for c in cards if isinstance(c.get("quiz"), dict) and (c["quiz"].get("question") or "").strip()]
    for k, a in enumerate(quizzes):
        for b in quizzes[k + 1:]:
            wa, wb = words(a["quiz"]["question"]), words(b["quiz"]["question"])
            if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.7:
                out.append(_finding(b.get("card_index"), "Q5", "major", f"This quiz asks almost the same question as card {a.get('card_index')}.",
                                    "Ask about a different idea, or use a fresh case.", (b["quiz"]["question"] or "")[:100]))
    seen: Dict[str, Any] = {}
    for c in cards:
        run = c.get("code_run")
        shown = ((run or {}).get("stdout") or "").strip() if isinstance(run, dict) else ""
        if shown and len(shown) > 12:
            if shown in seen:
                out.append(_finding(c.get("card_index"), "C3", "minor", f"This code prints exactly what the code in card {seen[shown]} printed.",
                                    "Use a fresh case for this idea (a different target, filter or count).", shown[:80]))
            else:
                seen[shown] = c.get("card_index")
    for c in cards:
        if isinstance(c.get("quiz"), dict) and c["quiz"].get("question"):
            spoken = spoken_text(c.get("content_script"))
            m = re.search(r"\bThe answer is\b.*", spoken, re.S)
            if m and len(m.group(0).split()) > 70:
                out.append(_finding(c.get("card_index"), "Q6", "minor", f"The spoken answer is {len(m.group(0).split())} words; the quiz widget already explains it.",
                                    "Keep it to the answer, one reason and a lead into the next card (about 45 words).", m.group(0)[:100]))
    return out


_QUIZ_MARKER = re.compile(r"\[\[\s*QUIZ\s*\]\]", re.I)


def normalize_quiz_text(body: Optional[str], quiz: Any) -> str:
    """A quiz card's text in the one format: a lead-in, "Let's take a quiz.", the marker [[QUIZ]] where the quiz is shown, the pause, the answer. A Predict callout that prints
    the question and the options is taken out (the quiz on screen carries them); a card with no marker gets one before the pause or the answer. A "Reveal:" label is dropped.
    Text of a card with no quiz is returned unchanged."""
    text = body or ""
    if isinstance(quiz, str):
        try:
            import json as _json
            quiz = _json.loads(quiz)
        except ValueError:
            return text
    if not (isinstance(quiz, dict) and str(quiz.get("question") or "").strip()):
        return text
    text = re.sub(r"^(\W*)Reveal:\s*", r"\1", text, flags=re.M | re.I)
    if _QUIZ_MARKER.search(text):
        return text
    lines = text.split("\n")
    start = next((i for i, l in enumerate(lines) if re.match(r"^\s*>.*Predict", l, re.I)), None)
    if start is not None:
        end = start
        while end + 1 < len(lines) and lines[end + 1].lstrip().startswith(">"):
            end += 1
        lines = lines[:start] + ["Let's take a quiz.", "", "[[QUIZ]]"] + lines[end + 1:]
    else:
        at = next((i for i, l in enumerate(lines) if "[pause for replies]" in l), None)
        if at is None:
            at = next((i for i, l in enumerate(lines) if re.match(r"^\W*The answer (?:is|was)\b", l, re.I)), len(lines))
        lines = lines[:at] + ["Let's take a quiz.", "", "[[QUIZ]]", ""] + lines[at:]
    out = "\n".join(lines)
    out = re.sub(r"\[\[QUIZ\]\]\s*\n\s*\[pause for replies\]", "[[QUIZ]]\n\n[pause for replies]", out)
    return re.sub(r"\n{3,}", "\n\n", out).strip("\n") + "\n"


_SELF_CORRECTION = re.compile(r"\b(but wait|actually,|let me rethink|scratch that|on second thought|hmm,)\b", re.I)


def check_spoken_polish(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Q10 and R16: a Predict callout must contain a question, and a teaching card must not correct itself aloud ("But wait", "Actually,")."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        body = c.get("content_script") or ""
        for m in re.finditer(r"^[ \t]*>[ \t]*\*\*Predict:?\*\*:?(.*(?:\n[ \t]*>.*)*)", body, re.M | re.I):
            stem = "\n".join(l for l in re.sub(r"^[ \t]*>[ \t]?", "", m.group(1), flags=re.M).split("\n") if not re.match(r"^\s*[A-D][.)]\s", l))
            if "?" not in stem:
                out.append(_finding(c.get("card_index"), "Q10", "major", "A Predict callout lists options but asks no question.", "State the question the learners are answering.", m.group(0)[:100]))
        if _kind(c) in ("concept", "problem", "walkthrough"):
            m = _SELF_CORRECTION.search(spoken_text(body))
            if m:
                out.append(_finding(c.get("card_index"), "R16", "minor", f'The card corrects itself aloud ("{m.group(0)}").', "Decide the explanation first, then write it once, without backing out of it.", m.group(0)))
    return out


def check_quiz_teaches_first(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Q8: a quiz card asks first. Headings or Key, Remember and Mistake callouts before its question mean it is teaching, and that belongs in a concept card."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        if not (isinstance(c.get("quiz"), dict) and c["quiz"].get("question")):
            continue
        body = c.get("content_script") or ""
        before = re.split(r"\[pause for replies\]|>\s*\*\*Predict|\bPredict\s*:|\[\[\s*QUIZ\s*\]\]", body, maxsplit=1)[0]
        m = re.search(r"^#{2,4}\s+.+$|^>\s*\*\*(Key|Remember|Mistake|Real-world)\b", before, re.M | re.I)
        if m:
            out.append(_finding(c.get("card_index"), "Q8", "major", "This quiz card teaches before it asks.",
                                "Move the explanation into a concept card before it, and keep this card to the question.", m.group(0)[:100]))
    return out


def check_answer_given_away(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Q7: a quiz must not be explained before it is asked. When the right option is a short number and that number is not in the question itself, it must not appear in
    the spoken text before the question."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        quiz = c.get("quiz")
        if not isinstance(quiz, dict) or not isinstance(quiz.get("options"), list):
            continue
        try:
            right = str(quiz["options"][int(quiz.get("correct_option_index"))]).strip()
        except (TypeError, ValueError, IndexError):
            continue
        right = re.sub(r"^[A-D][.)]\s*", "", right)
        if not (0 < len(right) <= 14 and re.search(r"\d", right)):
            continue
        before = re.split(r"\[pause for replies\]|\bPredict\s*:|The answer is", spoken_text(c.get("content_script")), maxsplit=1)[0]
        token = re.escape(right)
        if re.search(rf"(?<![\w.]){token}(?![\w.])", before) and not re.search(rf"(?<![\w.]){token}(?![\w.])", str(quiz.get("question") or "")):
            out.append(_finding(c.get("card_index"), "Q7", "major", f'The right answer "{right}" is already stated before the question is asked.',
                                "Move the explanation after the question, or take the number out of the text before it.", right))
    return out


_COMPANY_AS_PLACE = re.compile(r"\b(BookMyShow|Swiggy|Zomato|Uber|Ola|Paytm|PhonePe|IRCTC|Flipkart|Amazon)(?:'s)?\s+(hall|venue|theatre|theater|cinema|restaurant|kitchen|warehouse|station)\b", re.I)


def check_company_as_place(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """N3: "the BookMyShow hall" treats an app as a building. The example is a cinema hall booked through an app like it."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        for field in (c.get("title") or "", spoken_text(c.get("content_script")), c.get("code_snippet") or ""):
            m = _COMPANY_AS_PLACE.search(field)
            if m:
                out.append(_finding(c.get("card_index"), "N3", "minor", f'"{m.group(0)}" treats an app or company as a place.',
                                    'Say "a cinema hall booked through a BookMyShow-style app", or "our booking app".', m.group(0)))
                break
    return out


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
        if kind in ("welcome", "agenda", "concept", "problem", "walkthrough", "quiz", "faq", "recap"):
            # R14 simple English: no sentence over 30 words, and short sentences on the whole
            sentences = [s_ for s_ in _sentences(re.sub(r"\[[^\]]*\]", " ", prose)) if len(s_.split()) >= 3]
            lengths = [len(s_.split()) for s_ in sentences]
            if lengths:
                longest = max(lengths)
                if longest > 30:
                    out.append(_finding(cid, "R14", "minor", f"A sentence has {longest} words; keep sentences to about 15 words and none over 30.",
                                        "Split it into two or three short sentences, or cut what the learner does not need.", quote=sentences[lengths.index(longest)][:100]))
                elif len(lengths) >= 4 and sum(lengths) / len(lengths) > 22:
                    out.append(_finding(cid, "R14", "minor", f"The sentences average {sum(lengths) / len(lengths):.0f} words; aim for about 15.",
                                        "Use shorter sentences and everyday words."))
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
        m = re.search(r"(?:option|answer|quiz)[^.\n]{0,40}zero-based|zero-based[^.\n]{0,40}(?:option|answer|quiz)|option index|quiz json|\[Visual V\d+|not asked for, because", body, re.I)   # "index 2 (zero-based)" in a lesson on lists is teaching, not plumbing
        if m:
            out.append(_finding(cid, "N2", "minor", "Production wording from the generator is in the spoken text.", "Delete it; the instructor does not need it.", quote=body[max(0, m.start() - 30):m.end() + 60]))
    out.extend(check_made_up_statistics(cards))
    out.extend(check_repeated_lead_in(cards))
    out.extend(check_visuals_and_quizzes(cards))
    return out


# a claim about how learners usually answer or behave, which nobody measured
_STATISTIC = re.compile(r"\b(?:most|more) common(?:ly)?\b|\bcommon(?:ly)? (?:wrong|mistake|slip|pick|answer)s?\b|\b(?:both|these|those|all) (?:are|is) common\b|\balmost always\b|"
                        r"\b(?:most|many) (?:learners|students) (?:pick|choose|say|answer|get)\b|\blearners often (?:pick|choose|say|answer)\b", re.I)


def check_made_up_statistics(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """R15. The staging cue and the reasons under a quiz's wrong options must not say how learners usually answer ("32 and 64 are the most common picks"): nobody
    measured it, and the instructor will repeat it as a fact."""
    out: List[Dict[str, Any]] = []
    for c in cards:
        quiz = c.get("quiz") if isinstance(c.get("quiz"), dict) else {}
        places = [("staging cue", c.get("stage_directions") or "")] + [("reason for a wrong option", str(v)) for v in (quiz.get("distractor_explanations") or {}).values()]
        for where, text in places:
            m = _STATISTIC.search(text)
            if m:
                out.append(_finding(c.get("card_index"), "R15", "minor", f"The {where} says how learners usually answer (\"{m.group(0)}\"), but nobody measured that.",
                                    "Say \"a tempting wrong answer\" instead, or cut the claim.", quote=text[:100]))
                break
    return out


def check_repeated_lead_in(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """C2. A card that opens with a sentence the card before it just ended on. A card ends by leading into the next one; the next must not say it again."""
    out: List[Dict[str, Any]] = []
    norm = lambda s: re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()
    spoken = [c for c in sorted(cards, key=lambda x: x.get("card_index") or 0) if _kind(c) != "instructor_note" and (c.get("content_script") or "").strip()]
    for before, c in zip(spoken, spoken[1:]):
        last = {norm(s_) for s_ in _sentences(spoken_text(before.get("content_script")))[-3:] if len(s_.split()) >= 8}
        first = [s_ for s_ in _sentences(spoken_text(c.get("content_script")))[:3] if len(s_.split()) >= 8]
        hit = next((s_ for s_ in first if norm(s_) in last), None)
        if hit:
            out.append(_finding(c.get("card_index"), "C2", "minor", f"The card opens with a sentence card {before.get('card_index')} just ended on: \"{hit[:80]}\".",
                                "Take the sentence out of one of the two cards, so the class does not hear it twice.", quote=hit[:100]))
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
            if len({re.sub(r"\s+", " ", o).strip().lower() for o in opts}) < len(opts):          # symbols count: "O(N + Q)" and "O(N · Q)" are different answers
                out.append(_finding(cid, "Q2", "major", "Two quiz options say the same thing.", "Make every option different."))
            if (q.get("question") or "").strip() and not _QUIZ_MARKER.search(body):
                out.append(_finding(cid, "Q9", "major", "The card does not say where its quiz is shown: it has no [[QUIZ]] marker.",
                                    "Write a lead-in, \"Let's take a quiz.\", the marker, the pause and the answer; do not print the question and options in the text.", auto_fix=True))
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
                if mix < 0.6 and not re.search(r"\[\[\s*QUIZ\s*\]\]", body):          # a card that points at the quiz on screen does not print its question
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


def strip_duplicate_code(body: Optional[str], snippet: Optional[str]) -> str:
    """A card shows its code in its own Live Code box. When the writer also pasted the same code into the spoken text, the learner sees it twice (three times with
    the step-by-step player). Take that copy out of the text; any other code block (a trace table, an output sample) stays."""
    body = body or ""
    code = re.sub(r"\s+", " ", snippet or "").strip()
    if not code:
        return body

    placed = "[[CODE]]" in body                      # the writer already said where the code box goes

    def keep(match):
        nonlocal placed
        block = re.sub(r"\s+", " ", re.sub(r"^```[^\n]*\n|```\s*$", "", match.group(0))).strip()
        if not (block and (block == code or code in block)):
            return match.group(0)
        if placed:
            return ""
        placed = True
        return "\n\n[[CODE]]\n\n"                 # the code box is shown right here instead of a second copy of the code
    cleaned = re.sub(r"```[^\n]*\n.*?```[ \t]*\n?", keep, body, flags=re.S)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip("\n") if cleaned != body else body


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

    # an outcome alone is not enough: a teaching card must serve at least one agenda item the author wrote (a hook or a bridge nobody asked for serves only an outcome)
    real_agendas = [a for a in (agendas or []) if str(a).strip()]
    if real_agendas:
        outcome_lines = {x.strip().lower() for x in split_items(_real_outcomes(outcomes))}
        for c in cards:
            if c.get("card_type") in ("concept", "problem", "walkthrough"):
                served = " ".join(a for a in (c.get("agenda_items") or []) if str(a).strip().lower() not in outcome_lines)
                if not any(covered(item, [served], threshold=0.6) for item in real_agendas):
                    out.append({"level": "major", "problem": f"Card \"{c.get('title')}\" serves only a learning outcome and none of the agenda items, so it teaches something nobody asked for. "
                                                        f"Attach it to an agenda item or take it out."})

    # every agenda item is taught by a concept, problem or walkthrough card: a quiz, the agenda card or the recap that names it is not teaching
    for item in [a for a in (agendas or []) if str(a).strip()]:
        if not any(c.get("card_type") in ("concept", "problem", "walkthrough") and covered(item, [" ".join(c.get("agenda_items") or [])], threshold=0.6) for c in cards):
            out.append({"level": "major", "problem": f'The agenda item "{item[:80]}" has no teaching card of its own: only a quiz or the recap names it. Plan a concept, problem or walkthrough card for it.'})

    # the opening kit lists what an opening class must cover; most of it has to be planned as cards
    if required_topics:
        text = [" ".join([c.get("title") or "", c.get("teaching_shape_note") or ""] + list(c.get("agenda_items") or [])) for c in cards]
        missing = [t for t in required_topics if not covered(t, text, threshold=0.5)]
        if len(missing) > 0.3 * len(required_topics):
            out.append({"level": "major", "problem": "The opening kit topics not planned as cards: " + "; ".join(missing)})

    # a quiz is a question, the demo and Q&A the agenda names get a card, program logistics stay short, quizzes sit next to their topic
    real_items = [a for a in (agendas or []) if str(a).strip()]
    quizzes = [c for c in cards if c.get("card_type") == "quiz"]
    for q in quizzes:
        asked = re.sub(r"^\s*(predict|quiz)\s*:\s*", "", q.get("title") or "", flags=re.I)
        if any(words(asked) and len(words(asked) & words(it)) / max(1, len(words(asked))) >= 0.8 for it in real_items):
            out.append({"level": "major", "problem": f"Quiz card \"{q.get('title')}\" is named after an agenda item, not a question. Make the title the question learners are asked."})
    for it in real_items:
        if re.search(r"q\s*&\s*a|questions", it, re.I) and not any(re.search(r"q\s*&\s*a|questions", f"{c.get('title') or ''} {c.get('teaching_shape_note') or ''}", re.I)
                                                                      for c in cards if c.get("card_type") != "quiz"):
            out.append({"level": "major", "problem": f"The agenda item \"{it[:70]}\" asks for Q&A, but no card plans it. Add a card, or time inside the demo card, for open questions."})
    logistics = [c for c in cards if re.search(r"\b(psp|attendance|contests?|faqs?|dashboard|chat norms|session flow)\b", c.get("title") or "", re.I) or c.get("card_type") == "faq"]
    spent = sum((c.get("duration_sec") or 0) for c in logistics) / 60.0
    if logistics and target_minutes and spent > 0.15 * target_minutes + 0.05:
        out.append({"level": "major", "problem": f"Program logistics cards take {round(spent, 1)} of {target_minutes} minutes ({round(100 * spent / target_minutes)}%); keep them under 15% so the class itself has the time."})
    for c in logistics:
        if c.get("has_code"):
            out.append({"level": "major", "problem": f"Card \"{c.get('title')}\" is about how the program runs and is planned with code. Logistics cards carry no code."})
    qmin = sum((c.get("duration_sec") or 0) for c in quizzes) / 60.0
    if target_minutes and qmin > 0.22 * target_minutes:
        out.append({"level": "major", "problem": f"Quiz cards take {round(qmin, 1)} of {target_minutes} minutes ({round(100 * qmin / target_minutes)}%); keep quizzes to about a fifth of the class."})
    if real_items:
        lowered = {str(a).strip().lower() for a in real_items}
        for k, q in enumerate(cards):
            if q.get("card_type") != "quiz":
                continue
            mine = {str(a).strip().lower() for a in (q.get("agenda_items") or [])} & lowered
            if not mine:
                continue
            target = next((j for j in range(k + 1, len(cards)) if cards[j].get("card_type") in ("concept", "problem", "walkthrough")
                           and mine & {str(a).strip().lower() for a in (cards[j].get("agenda_items") or [])}), None)
            if target is not None and sum(1 for j in range(k + 1, target) if cards[j].get("card_type") != "quiz") > 2:
                out.append({"level": "major", "problem": f"Quiz card \"{q.get('title')}\" comes {target - k - 1} cards before the card that teaches its topic. Put it right before that topic."})

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
    if code_ratio and cards:
        can_carry = [c for c in cards if c.get("card_type") in ("concept", "problem", "walkthrough", "quiz") and not re.search(r"\b(psp|attendance|contests?|faqs?|dashboard)\b", c.get("title") or "", re.I)]
        want_cards = round(code_ratio / 100.0 * len(cards))
        if want_cards > len(can_carry):
            out.append({"level": "minor", "problem": f"The code share you asked for (about {code_ratio}%) needs {want_cards} cards with code, but only {len(can_carry)} of {len(cards)} cards can carry code "
                                                     f"(at most {round(100.0 * len(can_carry) / len(cards))}%). Quizzes, the welcome, the agenda and the recap carry none. Plan more walkthrough or problem cards, or lower the share."})
    animations = sum(1 for v in visuals if v.get("kind") == "animation")
    if min_animations and animations < min_animations:
        out.append({"level": "major", "problem": f"{animations} animation(s) planned; a beginner track needs at least {min_animations}."})
    # the scope is what the author asked for plus what the cards themselves name (their agenda items and outcomes, already checked against the author's list above)
    card_items = [str(x) for c in cards for x in list(c.get("agenda_items") or []) + list(c.get("outcomes") or [])]
    scope_words = words(" ".join(list(agendas or []) + split_items(outcomes) + [class_title] + card_items))
    for v in visuals:
        serves = " ".join(list(v.get("serves_agenda_items") or []) + list(v.get("serves_outcomes") or []) + [v.get("prompt") or "", v.get("title") or ""])
        if scope_words and not (words(serves) & scope_words):
            out.append({"level": "major", "problem": f"Visual {v.get('visual_id')} \"{v.get('title')}\" does not relate to the agenda, outcomes or class topic."})
    return out
