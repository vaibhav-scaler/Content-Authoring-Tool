import contextvars
import re
import os
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Any, Optional
from dotenv import load_dotenv

load_dotenv()

from agents.llm import call_llm_json, llm_configured
from agents.shared_context import SEVERITY_GUIDE

EXECUTION_MODE = os.getenv("EXECUTION_MODE", "hybrid")

# ── 3 Independent Review Lenses Configuration ────────────────────────────────
LENS_SPECS = {
    "technical_accuracy": {
        "lens_name": "Technical Accuracy",
        "reviewer_persona": "Principal Staff Engineer & Technical Auditor",
        "criteria": [
            "A1: Qualify any / every / all / no / never claims by naming the exact boundary. Only a claim the card actually makes counts; do not add a boundary the learners cannot use at their level (hash collisions, hardware, library internals).",
            "A2: State platforms, versions, and supported runtimes upfront.",
            "A3: Code snippets must be fully runnable without syntax errors or hallucinated APIs.",
            "A4: Point out silent pitfalls and performance/concurrency gotchas before they occur.",
            "A5: State time and space complexity with both average and worst cases for code.",
            "A6: Walkthroughs must state what to run, success output, and quantified failure mode.",
            "A7: A command, API or version-specific fact must be backed by a source, or labelled UNVERIFIED.",
            "A5: No file, code, dataset or output from an earlier class is invented; a placeholder is used where the script depends on one."
        ],
        "max_majors": 1
    },
    "learner_clarity": {
        "lens_name": "Learner Clarity",
        "reviewer_persona": "Beginner Learner & Student Empathy Lens",
        "criteria": [
            "L1: Read as a BEGINNER and report every place you got lost: a term used before it is taught, a jump between ideas, a step with no reason, code that is not explained. Report where a beginner of THIS track would really be stuck, not every line of syntax; a line the card or its code comment already explains is not a problem.",
            "T1: Why -> What -> How -> Where -> What breaks (Never open with a definition).",
            "T2: Show obvious naive approach, break it with quantified failure, then name the fix.",
            "T3: Every claim must be followed by its 'so what' consequence in the next sentence. Check the next few sentences before reporting; a consequence given a sentence or two later counts.",
            "T4: One continuous running example; do not switch analogies midway.",
            "E4: Analogies strictly from everyday daily life in India (UPI, WhatsApp, food delivery, train booking, cricket and the IPL, banks, e-commerce). Cricket is everyday, not esoteric. STRICTLY BAN esoteric analogies (like fighter jets, rockets, particle physics).",
            "T5: An idea comes back in a new form each time (words, then a diagram, then a quiz, then applied) and is never just repeated.",
            "R8: Address the learner as 'you' (em dashes are checked by code, so never report them)."
        ],
        "max_majors": 1
    },
    "intermediate_clarity": {
        "lens_name": "Intermediate Reviewer",
        "reviewer_persona": "Working Developer with some experience, reading as an Intermediate Learner",
        "criteria": [
            "I1: Read as an INTERMEDIATE learner and report every gap between the basics they know and the depth the card jumps to.",
            "I2: Report pacing problems: the basics explained again at length, or a new idea that arrives with no bridge from what they know.",
            "I3: Examples are realistic enough to feel like real work, not toy cases only.",
            "I4: Each new idea says when to use it and what to do when it fails.",
            "R9: Depth comes from examples, traces and consequences, never from more paragraphs."
        ],
        "max_majors": 1
    },
    "advanced_depth": {
        "lens_name": "Advanced Reviewer",
        "reviewer_persona": "Senior Engineer reading as an Advanced Learner",
        "criteria": [
            "D1: Read as an ADVANCED learner and report every technical error: a wrong statement, a wrong number, a claim without its boundary.",
            "D2: Report shallow depth: a topic explained only at the surface, an example too trivial to teach the idea, an edge case or failure mode that is missing.",
            "D3: The class climbs to its target level; flag cards that stay too basic for the level the class aims at.",
            "D4: Every claim about speed, cost or scale has a number or a consequence, not an adjective."
        ],
        "max_majors": 1
    },
    "syllabus_and_rules": {
        "lens_name": "Syllabus Coverage & SOP Rules",
        "reviewer_persona": "Curriculum Quality & Standards Auditor",
        "criteria": [
            "S5: Every card serves an agenda item or learning outcome in the scope. Flag content that is OUTSIDE the scope, not content that is merely extra explanation of an agenda item. Welcome, agenda, recap, FAQ and note cards serve the whole class.",
            "S9: Every agenda item is TAUGHT by a concept, problem or walkthrough card. An item that only the agenda card, a quiz or the recap mentions is not taught. For each agenda item name the teaching card (the script map shows each card's type); report any item with none, and a card that promises to teach something in 'the next card' that the next card does not teach. Report these with card_id null.",
            "S6: Nothing from the upcoming classes is taught (a one-line forward pointer is allowed).",
            "S3: The recap card is one unified visual mental model of the class. The recap is the LAST spoken card only; never apply this to an earlier card, even one that recaps a previous class.",
            "T6: Every term is defined before it is used, including in quizzes and code comments. Applies to a term new to THIS class; a term the learners met in an earlier class or module, or in the scope above, needs no definition. A quiz may ask before the cards that teach it.",
            "T7: The difficulty climbs step by step and each step up is signposted.",
            "E3: Interactions work online: learners type answers in chat, and thumbs up or down is used only for yes or no questions.",
            "Additional resources from the SME, where given in the scope, are used and not contradicted."
        ],
        "max_majors": 2
    }
}

DEFAULT_LENSES = ("technical_accuracy", "learner_clarity", "advanced_depth", "syllabus_and_rules")

TRACK_LEVEL_LENS = {"beginner": "learner_clarity", "intermediate": "intermediate_clarity", "advanced": "advanced_depth"}
TRACK_LEVEL_NOTE = {
    "beginner": "BEGINNER TRACK. The learners are new to this program. Judge every card as a beginner would read it: plain words, every term defined before use, small steps. "
                "Do not ask for expert depth, internals or edge cases; a card that goes deeper than a beginner can follow is a problem.",
    "intermediate": "INTERMEDIATE TRACK. The learners know the basics of this program. Do not ask for the basics to be explained again at length; judge whether each new idea is "
                    "bridged from what they know, is realistic work and says when to use it. Flag a card that is either too basic or too advanced for them.",
    "advanced": "ADVANCED TRACK. The learners are experienced. Judge for depth: edge cases, how it works inside, trade-offs, failure modes and numbers. Do not ask for basic terms to be "
                "defined; a card that stays at the surface is a problem.",
}


def _focus_entries() -> List[Dict[str, Any]]:
    program_focus("x", "technical_accuracy")          # loads data/lens_focus.json into the cache the first time
    return _FOCUS_CACHE.get("data", [])


def program_default_level(program: Optional[str]) -> Optional[str]:
    """The level of a program whose tracks do not say one in their name (data/lens_focus.json, 'default_level'), else None."""
    low = (program or "").lower()
    if not low:
        return None
    for entry in _focus_entries():
        if any(word in low for word in entry.get("match", [])):
            return entry.get("default_level")
    return None


def track_level(track_name: Optional[str], program: Optional[str] = None) -> Optional[str]:
    """beginner, intermediate or advanced from the track's name ("Beginner Track"); a track with no level in its name takes its program's default level
    (DevOps and Cloud and the FDE program are beginner-level), else None."""
    low = (track_name or "").lower()
    for level in ("beginner", "intermediate", "advanced"):
        if level in low:
            return level
    return program_default_level(program)


def lenses_for_track(track_name: Optional[str], program: Optional[str] = None) -> tuple:
    """The reviewers a track gets when the author picked none: technical accuracy and the syllabus check always, plus the reviewer who reads like the track's learners.
    A track with no level at all gets the usual four (beginner and advanced readers both)."""
    level = track_level(track_name, program)
    if level is None:
        return DEFAULT_LENSES
    return tuple(k for k in ALL_LENSES if k in ("technical_accuracy", "syllabus_and_rules", TRACK_LEVEL_LENS[level]))
ALL_LENSES = ("technical_accuracy", "learner_clarity", "intermediate_clarity", "advanced_depth", "syllabus_and_rules")


_FOCUS_CACHE: Dict[str, Any] = {}


def program_focus(program: Optional[str], lens_key: str) -> List[str]:
    """Extra criteria one reviewer applies for one program (data/lens_focus.json), so each lens is tuned for the program it reviews."""
    if not program:
        return []
    if "data" not in _FOCUS_CACHE:
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "lens_focus.json"), encoding="utf-8") as fh:
                _FOCUS_CACHE["data"] = json.load(fh).get("programs", [])
        except (OSError, ValueError):
            _FOCUS_CACHE["data"] = []
    low = program.lower()
    for entry in _FOCUS_CACHE["data"]:
        if any(word in low for word in entry.get("match", [])):
            return list((entry.get("lenses") or {}).get(lens_key, []))
    return []


def short_reason(reason: str) -> str:
    """Why a reviewer could not run, in a sentence a person can read instead of a network error trace."""
    low = (reason or "").lower()
    if any(k in low for k in ("nameresolution", "max retries exceeded", "connectionerror", "connection refused", "timed out", "network is unreachable")):
        return "the AI service could not be reached; check the internet connection"
    if "rate limit" in low or "busy" in low or "429" in low:
        return "the AI service is at its rate limit; wait a minute"
    if "budget" in low:
        return "the AI budget is used up"
    return (reason or "unknown reason")[:140]


def _review_did_not_run(spec: Dict[str, Any], reason: str) -> Dict[str, Any]:
    """A lens that could not run is a failure, never a pass: the script must not look reviewed when it was not."""
    reason = short_reason(reason)
    return {
        "lens_name": spec.get("lens_name", "Review"),
        "reviewer_persona": spec.get("reviewer_persona", "Reviewer"),
        "score": 0.0,
        "passed": False,
        "verdict": "fail",
        "ran": False,
        "summary": f"{spec.get('lens_name', 'Review')}: the review did not run ({reason}).",
        "findings": [],
        "detailed_findings": [f"{spec.get('lens_name', 'Review')}: the review did not run ({reason}). Run it again."]
    }


CHUNK_TOKENS = 14000         # card text per reviewer request; most scripts fit in one or two, so each reviewer sees the whole class
CARD_BODY_CHARS = 6000       # a card body is sent whole; this only guards against a runaway card


def _numbered(code: Optional[str]) -> Optional[str]:
    """The code with its real line numbers in front of every line, so a reviewer checks "line 14" against the code and never counts by eye."""
    lines = (code or "")[:2000].split("\n")
    return "\n".join(f"{n}: {l}" for n, l in enumerate(lines, 1)) if (code or "").strip() else None


def _card_view(c: Dict[str, Any]) -> Dict[str, Any]:
    dur = c.get("duration_mins") or (c.get("duration_sec", 300) / 60.0)
    quiz = c.get("quiz") or {}
    return {
        "card": c.get("card_index") or c.get("card_id"),
        "title": c.get("title"),
        "type": c.get("pedagogical_phase") or c.get("card_type"),
        "minutes": round(dur, 1),
        "text": (c.get("content_script") or c.get("body_markdown") or "")[:CARD_BODY_CHARS],
        "code": _numbered(c.get("code_snippet")),
        "printed_output": ((c.get("code_run") or {}).get("stdout") or "")[:500] or None,       # what the code really printed when it was run
        "quiz": ({"q": quiz.get("question"), "options": quiz.get("options"), "answer": quiz.get("correct_option_index"),
                  "why_wrong": quiz.get("distractor_explanations")} if quiz else None),
        "sources": [{"claim": s.get("claim"), "checked_page_text": (s.get("evidence") or "")[:300], "verified": s.get("verified")} for s in (c.get("sources") or [])] or None,
    }


_RETRACTED = re.compile(r"no action needed|remove this finding|no (?:real )?blocker|not a (?:real )?(?:bug|problem|issue)|this is actually (?:consistent|correct|fine)|on closer inspection|re-?checking|downgrad\w+ (?:this|the) finding|"
                        r"no issue (?:here|found)|nothing to fix", re.I)


def is_retracted(finding: Dict[str, Any]) -> bool:
    """A finding the reviewer talked itself out of while writing it ("on closer inspection this is consistent ... remove this finding"), or one so long it is
    the reviewer thinking aloud. It is not a problem in the script, so it is dropped instead of counting as one."""
    text = f"{finding.get('problem') or ''} {finding.get('suggested_fix') or ''}"
    return bool(_RETRACTED.search(text)) or len(str(finding.get("problem") or "")) > 1200


_PERSONA_CACHE: Dict[str, Any] = {}


def class_persona(class_title: Optional[str], program: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The specialist who audits this kind of class (data/class_personas.json): an algorithms auditor for a DSA class, a SQL auditor for a SQL class, and so on.
    Chosen from the class title first, then the program; None when nothing fits, and the general auditor is used."""
    if "data" not in _PERSONA_CACHE:
        try:
            with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "class_personas.json"), encoding="utf-8") as fh:
                _PERSONA_CACHE["data"] = json.load(fh).get("classes", [])
        except (OSError, ValueError):
            _PERSONA_CACHE["data"] = []
    for text in ((class_title or "").lower(), (program or "").lower()):
        if not text:
            continue
        for entry in _PERSONA_CACHE["data"]:
            if any(re.search(rf"\b{re.escape(w)}", text) for w in entry.get("match", [])):
                return entry
    return None


VERIFY_BATCH = 14


def _verify_prompt(items: List[Dict[str, Any]], cards_by_id: Dict[Any, Dict[str, Any]]) -> str:
    shown = {}
    for it in items:
        cid = it["card"]
        if cid not in cards_by_id:
            continue
        named = [int(n) for m in re.finditer(r"\bcards? ((?:\d+(?:,? (?:and )?)?)+)", f"{it.get('problem') or ''}") for n in re.findall(r"\d+", m.group(1))][:4]
        for near in list((cid - 1, cid, cid + 1) if isinstance(cid, int) else (cid,)) + [n for n in named if n != cid]:     # the card before and after, so a claim about "the earlier card" can be checked
            if near in cards_by_id and near not in shown:
                shown[near] = _card_view(cards_by_id[near])
                if near != cid:
                    shown[near]["text"] = shown[near]["text"][:900]
    return f"""You are the final checker of a script review. Reviewers listed the findings below. Some are wrong. For each finding, look at the card it names and decide.

verdict "keep": the card really has this problem, and you can point to the words.
verdict "drop": the card does not have this problem. Use it when the claim is false once you recompute it (add the numbers, trace the code, count the lines shown with the code),
  when it is about a card that is not shown, when it says an answer is given away but that answer comes only after "[pause for replies]" in a quiz card (that is the design),
  or when the card already says what the finding asks to add.
verdict "lower": the problem is real but only a wording or style preference: make it minor.
A finding that says a card contradicts a linked source is "drop" unless the source's checked_page_text shown with the card says the opposite.
Be strict: a finding you cannot confirm from the card shown is "drop". Do not add findings.

FINDINGS
{json.dumps(items, ensure_ascii=False, separators=(",", ":"))}

CARDS NAMED, with the card before and after each (code lines are numbered with their real line numbers; printed_output is what the code really printed; sources are the checked reference claims)
{json.dumps(list(shown.values()), ensure_ascii=False, separators=(",", ":"))}

Return ONLY JSON, first character {{: {{"verdicts": [{{"id": number, "verdict": "keep" | "drop" | "lower", "reason": string under 20 words}}]}}"""


def verify_review_findings(results: List[Dict[str, Any]], cards: List[Dict[str, Any]], script_id: Optional[int] = None,
                           dropped_out: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """A second, cheap look at what the AI reviewers found. Each finding is checked against the card it names; findings that do not hold are dropped and
    wording-only ones are made minor. If the checker cannot answer, the findings are kept as they were (a missed drop is better than a lost real problem)."""
    if EXECUTION_MODE == "mock" or not llm_configured():
        return results
    cards_by_id = {c.get("card_index") or c.get("card_id"): c for c in cards}
    pool = []
    for r in results:
        if r.get("ran") is False:
            continue
        for f in r.get("findings", []):
            if f.get("card_id") not in cards_by_id:
                continue                                   # a whole-script finding names no card to check it against; it stays
            pool.append({"id": len(pool), "lens": r.get("lens_name"), "card": f.get("card_id"), "rule": f.get("rule"), "severity": f.get("severity"),
                         "quote": f.get("quote"), "problem": str(f.get("problem"))[:500], "fix": str(f.get("suggested_fix"))[:300], "_f": f})
    if not pool:
        return results
    from agents.progress import report
    report(script_id, "Checking each AI finding against the card it names")
    verdicts: Dict[int, str] = {}
    for i in range(0, len(pool), VERIFY_BATCH):
        batch = pool[i:i + VERIFY_BATCH]
        try:
            parsed = call_llm_json(messages=[{"role": "user", "content": _verify_prompt([{k: v for k, v in b.items() if k != "_f"} for b in batch], cards_by_id)}],
                                   max_tokens=2000, temperature=0.0, agent="reviewer_verify", script_id=script_id)
        except Exception as e:
            print(f"Warning: the finding check could not run ({str(e)[:120]}); the findings are kept as they were.")
            continue
        for v in (parsed.get("verdicts") if isinstance(parsed, dict) else None) or []:
            if isinstance(v, dict) and isinstance(v.get("id"), int) and v.get("verdict") in ("keep", "drop", "lower"):
                verdicts[v["id"]] = v["verdict"]
    drop = {id(b["_f"]) for b in pool if verdicts.get(b["id"]) == "drop"}
    if dropped_out is not None:
        dropped_out.extend({"card": b["card"], "rule": b["rule"], "problem": b["problem"][:240]} for b in pool if id(b["_f"]) in drop)
    lower = {id(b["_f"]) for b in pool if verdicts.get(b["id"]) == "lower"}
    specs = {sp["lens_name"]: sp for sp in LENS_SPECS.values()}
    out = []
    for r in results:
        if r.get("ran") is False or not r.get("findings"):
            out.append(r)
            continue
        kept = []
        for f in r["findings"]:
            if id(f) in drop:
                continue
            if id(f) in lower:
                f = {**f, "severity": "minor"}
            kept.append(f)
        blockers = sum(f.get("severity") == "blocker" for f in kept)
        majors = sum(f.get("severity") == "major" for f in kept)
        minors = sum(f.get("severity") == "minor" for f in kept)
        passed = blockers == 0 and majors <= specs.get(r.get("lens_name"), {}).get("max_majors", 0)
        out.append({**r, **({"summary": f"{len(r['findings']) - len(kept)} of this reviewer's findings did not hold when checked against the cards, and were dropped."} if len(kept) < len(r["findings"]) else {}), "findings": kept, "blockers": blockers, "majors": majors, "minors": minors, "passed": passed, "verdict": "pass" if passed else "fail",
                    "score": max(r.get("score", 0.0), round(10 - 2.5 * blockers - 1.0 * majors - 0.25 * minors, 1)) if len(kept) < len(r["findings"]) else r.get("score"),
                    "dropped_by_check": len(r["findings"]) - len(kept),
                    "detailed_findings": [f"[{str(f.get('severity', 'info')).upper()}] Card {f.get('card_id')}: {f.get('problem')}" for f in kept]})
    return out


def verify_finding_list(findings: List[Dict[str, Any]], cards: List[Dict[str, Any]], name: str, script_id: Optional[int] = None,
                        dropped_out: Optional[List[Dict[str, Any]]] = None, dismissed: Optional[List[Dict[str, Any]]] = None) -> List[Dict[str, Any]]:
    """The same final check for a list of findings made by something other than a lens (the cross-card consistency judge, the meaning check)."""
    findings = filter_dismissed(findings, dismissed, cards)
    if not findings:
        return findings
    got = verify_review_findings([{"lens_name": name, "ran": True, "score": 0.0, "findings": list(findings)}], cards, script_id, dropped_out)
    return got[0].get("findings", findings)


def card_fingerprint(card: Dict[str, Any]) -> str:
    """A short fingerprint of what a card says (text, code, quiz), so a dismissed finding is forgotten when the card is edited."""
    import hashlib
    raw = "|".join(str(card.get(k) or "") for k in ("content_script", "code_snippet")) + json.dumps(card.get("quiz") or {}, sort_keys=True, default=str)
    return hashlib.md5(raw.encode("utf-8")).hexdigest()[:12]


def _overlap(a: str, b: str) -> float:
    x, y = set(_plain_words(a)), set(_plain_words(b))
    return len(x & y) / max(1, min(len(x), len(y)))


def filter_dismissed(findings: List[Dict[str, Any]], dismissed: Optional[List[Dict[str, Any]]], cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Leave out a finding that was already checked and found not to be a problem, on a card that has not changed since. Reviewers word the same
    false alarm differently each time, so a finding matches when it is on the same card and its words overlap those of the dismissed one by half or more."""
    if not dismissed:
        return findings
    fp = {c.get("card_index") or c.get("card_id"): card_fingerprint(c) for c in cards}
    kept = []
    for f in findings:
        same = any(d.get("card") == f.get("card_id") and d.get("fp") == fp.get(f.get("card_id")) and _overlap(d.get("problem", ""), str(f.get("problem"))) >= 0.5
                   for d in dismissed)
        if not same:
            kept.append(f)
    return kept


def _plain_words(text: str) -> List[str]:
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def quote_is_real(finding: Dict[str, Any], group: List[Dict[str, Any]]) -> bool:
    """A finding must quote the script. The quote is looked for in the cards the reviewer saw (text, code, quiz); a quote with fewer than
    60% of its words anywhere in them was made up or badly remembered, so the finding is dropped. No quote at all is allowed (whole-script points)."""
    quote = _plain_words(str(finding.get("quote") or ""))
    if len(quote) < 4:
        return True
    pool = set()
    for v in group:
        pool.update(_plain_words(json.dumps(v, ensure_ascii=False)))
    return sum(w in pool for w in quote) / len(quote) >= 0.6


def _chunks(cards: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Group the cards so each group's text fits one request. Every card is sent in full, none sampled."""
    from agents.llm import estimate_tokens
    groups, cur, used = [], [], 0
    for c in cards:
        view = _card_view(c)
        cost = estimate_tokens(json.dumps(view, separators=(",", ":")))
        if cur and used + cost > CHUNK_TOKENS:
            groups.append(cur)
            cur, used = [], 0
        cur.append(view)
        used += cost
    if cur:
        groups.append(cur)
    return groups


def run_single_lens_review(
    lens_key: str,
    class_title: str,
    cards: List[Dict[str, Any]],
    target_duration_mins: int,
    block_a_context: Optional[str] = None,
    scope_digest: Optional[str] = None,
    script_id: Optional[int] = None,
    program: Optional[str] = None,
    already_reported: Optional[List[str]] = None,
    context_cards: Optional[List[Dict[str, Any]]] = None,
    dismissed: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """One review lens over the whole script. The cards are sent in full, in groups that fit the token budget, and the
    findings are combined. If any group cannot be reviewed the lens did not run (never a pass)."""
    spec = LENS_SPECS.get(lens_key, LENS_SPECS["technical_accuracy"])
    lens_name = spec["lens_name"]
    max_majors = spec["max_majors"]
    persona = class_persona(class_title, program) if lens_key == "technical_accuracy" else None
    if persona:                                            # the accuracy reviewer for this kind of class: its own person and its own checks
        spec = {**spec, "reviewer_persona": persona["persona"], "criteria": list(spec["criteria"]) + list(persona["criteria"])}

    if EXECUTION_MODE == "mock":
        return {
            "lens_name": lens_name, "reviewer_persona": spec["reviewer_persona"], "score": 9.2, "passed": True,
            "verdict": "pass", "ran": False, "summary": f"{lens_name}: MOCK MODE, nothing was reviewed.",
            "findings": [], "detailed_findings": [f"{lens_name}: mock mode, not reviewed."]
        }
    if not llm_configured():
        return _review_did_not_run(spec, "no AI key is configured")

    scope = scope_digest or f"CLASS TITLE: {class_title}\nTARGET DURATION: {target_duration_mins} minutes"
    criteria_text = "\n".join(f"- {c}" for c in list(spec["criteria"]) + program_focus(program, lens_key))
    earlier_text = ""
    if already_reported:
        earlier_text = ("ALREADY REPORTED. An earlier review of this script reported the problems below and the author has since worked on it. Report one of them again ONLY if the "
                        "text you see still has that exact problem. Never report a problem a second time in other words.\n" + "\n".join(f"- {t[:200]}" for t in already_reported[:40]) + "\n\n")
    if dismissed:
        earlier_text += ("CHECKED AND NOT PROBLEMS. These were reported before and found, on checking, not to be problems. Never report them again, in any wording:\n"
                         + "\n".join(f"- Card {d.get('card')}: {str(d.get('problem'))[:160]}" for d in dismissed[-30:]) + "\n\n")
    groups = _chunks(cards)
    everything = context_cards or cards
    script_map = "; ".join(f"{c.get('card_index') or c.get('card_id')} [{str(c.get('pedagogical_phase') or c.get('card_type') or '').lower()[:9]}] {str(c.get('title') or '')[:45]}" for c in everything)
    subset_note = ("THESE ARE ONLY SOME CARDS: those changed since the last read. The cards between them exist and were already read; never report that a card, a number or an idea "
                   "is missing because it is not shown here.\n") if len(everything) > len(cards) else ""
    findings: List[Dict[str, Any]] = []
    scores: List[float] = []
    summaries: List[str] = []

    from agents.progress import report
    for n, group in enumerate(groups, start=1):
        report(script_id, f"{lens_name}: reading cards {group[0]['card']} to {group[-1]['card']} (part {n} of {len(groups)})")
        prompt = f"""{scope}

You are the {spec['reviewer_persona']}. Audit the cards below EXCLUSIVELY under the '{lens_name}' lens.
These are cards {group[0]['card']} to {group[-1]['card']} of a {target_duration_mins} minute script (group {n} of {len(groups)}).
Judge the cards against the TRACK LEVEL and PROGRAM named in the scope above: a class is good when it is right for its own learners. Judge only the cards shown, and quote the text you rely on. Every card is shown in full, so never report text as cut off.
instructor_note and agenda cards are housekeeping: they do not need to serve an agenda item. Counts across the whole script (predict moments, quiz count,
diagram count, total minutes, em dashes) are checked by code, so do not report them. A [[FROM_SME: ...]] marker is a deliberate gap the SME fills in:
do not report it. Report only real problems: a script with no real problem gets an empty findings list and a score of 9 or 10, so never invent a problem to have something to say.
Report at most 6 findings for these cards, the most serious first. Report a finding only when you can prove it from words quoted in the cards shown; a doubt you cannot prove is not a finding.

VERIFY BEFORE YOU REPORT. A wrong finding costs the author more time than a missed one.
- Every finding must quote words that really appear in the card it names (or, for a disagreement between cards, in one of the cards shown). If you cannot quote it, do not report it.
- Code is shown with its real line numbers ("14: for i in ..."). Check any "line N" in the text against those numbers. Never count lines yourself and never guess the code of a card you cannot see.
- Cards are shown in order, but only the cards in this group. Do not claim what an earlier or later card contains unless it is shown here.
- A quiz card asks first: the Predict question, then [pause for replies], then "The answer is ...". The answer read after the pause is the design, so never report it as giving the answer away.
- A welcome card may mention the learners' earlier module or class; that comes from the program. Report it only if the scope above contradicts it.
- Before you say two facts disagree, recompute both from the cards (add the numbers, trace the code). Say what the real value is.
- A source's "checked_page_text" is the text really found on the linked page. Say what a linked page says ONLY from that text; never from memory, and never report that a card contradicts a source unless the checked_page_text says so.
- Severity: a term explained by the code's own comment or obvious from its use is minor, not a blocker. A style or wording preference is minor.
Do all of this checking silently. Never write your analysis, card-by-card notes or reasoning: your reply is the JSON object and nothing else, and its first character is {{.

{earlier_text}CRITERIA
{criteria_text}

{subset_note}ALL CARDS OF THIS SCRIPT, titles only (the cards outside this group are read in other groups, so you cannot judge what they say): {script_map}

CARDS (full text; "printed_output" is what the code really printed when it was run, so trust it over your own tracing)
{json.dumps(group, separators=(",", ":"))}

{SEVERITY_GUIDE}

Return ONLY JSON:
{{"score": number 0 to 10, "summary": string,
  "findings": [{{"card_id": number or null for the whole script, "rule": string, "severity": "blocker" | "major" | "minor",
                 "quote": string under 20 words, "problem": string, "suggested_fix": string}}]}}"""
        try:
            parsed = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=3500,
                temperature=0.0,
                agent=f"reviewer_{lens_name}",
                script_id=script_id
            )
        except Exception as e:
            print(f"Warning: Lens '{lens_name}' review failed on group {n} ({e}).")
            return _review_did_not_run(spec, str(e)[:200])
        if not isinstance(parsed, dict):
            return _review_did_not_run(spec, "the reviewer returned an unreadable answer")
        order_ = {"blocker": 0, "major": 1, "minor": 2}
        got = sorted((f for f in parsed.get("findings", []) if isinstance(f, dict) and not is_retracted(f) and quote_is_real(f, group)), key=lambda f: order_.get(f.get("severity"), 3))
        findings.extend(filter_dismissed(got, dismissed, context_cards or cards)[:8])                               # a reviewer's list is capped, most serious first
        if isinstance(parsed.get("score"), (int, float)):
            scores.append(float(parsed["score"]))
        if parsed.get("summary"):
            summaries.append(str(parsed["summary"]))

    blockers = sum(f.get("severity") == "blocker" for f in findings)
    majors = sum(f.get("severity") == "major" for f in findings)
    minors = sum(f.get("severity") == "minor" for f in findings)
    passed = blockers == 0 and majors <= max_majors
    return {
        "lens_name": lens_name, "reviewer_persona": spec["reviewer_persona"], "ran": True,
        "verdict": "pass" if passed else "fail", "passed": passed,
        "score": round(sum(scores) / len(scores), 1) if scores else (9.0 if passed else 5.0),
        "blockers": blockers, "majors": majors, "minors": minors,
        "summary": " ".join(summaries)[:600], "findings": findings,
        "detailed_findings": [f"[{str(f.get('severity', 'info')).upper()}] Card {f.get('card_id')}: {f.get('problem')}" for f in findings],
    }


def merge_lens_reviews(previous: List[Dict[str, Any]], fresh: List[Dict[str, Any]], changed_cards) -> List[Dict[str, Any]]:
    """Combine an earlier full review with a fresh review of only the cards that were repaired. For each lens: the findings on the
    repaired cards come from the fresh review, the findings on every other card and on the script as a whole are kept from the
    earlier one. A fresh review that could not run leaves that lens as 'did not run'."""
    changed = {int(c) for c in changed_cards}
    specs = {s["lens_name"]: s for s in LENS_SPECS.values()}
    new_by_name = {r.get("lens_name"): r for r in fresh}
    out = []
    for old in previous:
        new = new_by_name.get(old.get("lens_name"))
        if new is None or new.get("ran") is False or old.get("ran") is False:
            out.append(new or old)
            continue

        def on_changed(f):
            try:
                return int(f.get("card_id")) in changed
            except (TypeError, ValueError):
                return False                                  # a whole-script finding: only the full review judges those
        findings = [f for f in old.get("findings", []) if not on_changed(f)] + [f for f in new.get("findings", []) if on_changed(f)]
        blockers = sum(f.get("severity") == "blocker" for f in findings)
        majors = sum(f.get("severity") == "major" for f in findings)
        minors = sum(f.get("severity") == "minor" for f in findings)
        passed = blockers == 0 and majors <= specs.get(old.get("lens_name"), {}).get("max_majors", 0)
        out.append({
            **old, "verdict": "pass" if passed else "fail", "passed": passed, "ran": True, "findings": findings,
            "blockers": blockers, "majors": majors, "minors": minors,
            "score": max(0.0, round(10 - 2.5 * blockers - 1.0 * majors - 0.25 * minors, 1)),
            "detailed_findings": [f"[{str(f.get('severity', 'info')).upper()}] Card {f.get('card_id')}: {f.get('problem')}" for f in findings],
        })
    return out


COMBINED_LENS_KEYS = {"technical_accuracy": "technical_accuracy", "learner_clarity": "learner_clarity", "syllabus_and_rules": "syllabus_and_rules"}


def run_multi_lens_reviews(
    class_title: str,
    cards: List[Dict[str, Any]],
    target_duration_mins: int,
    content_mix_beginner: int = 70,
    block_a_context: Optional[str] = None,
    scope_digest: Optional[str] = None,
    script_id: Optional[int] = None,
    lens_keys: Optional[List[str]] = None,
    program: Optional[str] = None,
    already_reported: Optional[List[str]] = None,
    context_cards: Optional[List[Dict[str, Any]]] = None,
    dismissed: Optional[List[Dict[str, Any]]] = None,
    dropped_out: Optional[List[Dict[str, Any]]] = None
) -> List[Dict[str, Any]]:
    """
    Runs the review lenses the SME chose (four by default, five to pick from: Technical Accuracy, Learner Clarity as a beginner, the Advanced Reviewer,
    Syllabus Coverage & SOP Rules) independently, in PARALLEL using a thread pool.
    """
    lens_keys = [k for k in (lens_keys or DEFAULT_LENSES) if k in LENS_SPECS] or list(DEFAULT_LENSES)
    results = []
    from agents.progress import add_partial, clear_partial
    clear_partial(script_id)

    with ThreadPoolExecutor(max_workers=len(lens_keys)) as executor:
        future_to_lens = {
            executor.submit(
                contextvars.copy_context().run,        # a worker thread starts with an empty context; this hands it the request's
                run_single_lens_review,
                lens_key=lk,
                class_title=class_title,
                cards=cards,
                target_duration_mins=target_duration_mins,
                block_a_context=block_a_context,
                scope_digest=scope_digest,
                script_id=script_id,
                program=program,
                already_reported=already_reported,
                context_cards=context_cards,
                dismissed=dismissed
            ): lk
            for lk in lens_keys
        }
        for future in as_completed(future_to_lens):
            try:
                res = future.result()
                results.append(res)
            except Exception as e:
                lk = future_to_lens[future]
                spec = LENS_SPECS.get(lk, {})
                res = _review_did_not_run(spec or {"lens_name": lk, "reviewer_persona": "Reviewer"}, str(e)[:200])
                results.append(res)
            add_partial(script_id, res)                    # shown to the author and the Module Owner while the other reviewers are still reading

    results = verify_review_findings(results, context_cards or cards, script_id, dropped_out)
    # Sort results deterministically
    order = {"Technical Accuracy": 0, "Learner Clarity": 1, "Intermediate Reviewer": 2, "Advanced Reviewer": 3, "Syllabus Coverage & SOP Rules": 4}
    results.sort(key=lambda x: order.get(x.get("lens_name", ""), 3))
    return results
