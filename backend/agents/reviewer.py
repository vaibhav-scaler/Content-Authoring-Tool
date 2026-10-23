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
            "A1: Qualify any / every / all / no / never claims by naming the exact boundary.",
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
            "L1: Read as a BEGINNER and report every place you got lost: a term used before it is taught, a jump between ideas, a step with no reason, code that is not explained.",
            "T1: Why -> What -> How -> Where -> What breaks (Never open with a definition).",
            "T2: Show obvious naive approach, break it with quantified failure, then name the fix.",
            "T3: Every claim must be followed by its 'so what' consequence in the next sentence.",
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
            "S5: Every card serves an agenda item or learning outcome in the scope. Flag any content outside them.",
            "S6: Nothing from the upcoming classes is taught (a one-line forward pointer is allowed).",
            "S3: The recap card is one unified visual mental model of the class.",
            "T6: Every term is defined before it is used, including in quizzes and code comments.",
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


def _card_view(c: Dict[str, Any]) -> Dict[str, Any]:
    dur = c.get("duration_mins") or (c.get("duration_sec", 300) / 60.0)
    quiz = c.get("quiz") or {}
    return {
        "card": c.get("card_index") or c.get("card_id"),
        "title": c.get("title"),
        "type": c.get("pedagogical_phase") or c.get("card_type"),
        "minutes": round(dur, 1),
        "text": (c.get("content_script") or c.get("body_markdown") or "")[:CARD_BODY_CHARS],
        "code": (c.get("code_snippet") or "")[:2000] or None,
        "quiz": ({"q": quiz.get("question"), "options": quiz.get("options"), "answer": quiz.get("correct_option_index"),
                  "why_wrong": quiz.get("distractor_explanations")} if quiz else None),
        "sources": [s.get("source") for s in (c.get("sources") or [])] or None,
    }


_RETRACTED = re.compile(r"no action needed|remove this finding|no (?:real )?blocker|not a (?:real )?(?:bug|problem|issue)|this is actually (?:consistent|correct|fine)|on closer inspection|re-?checking|downgrad\w+ (?:this|the) finding|"
                        r"no issue (?:here|found)|nothing to fix", re.I)


def is_retracted(finding: Dict[str, Any]) -> bool:
    """A finding the reviewer talked itself out of while writing it ("on closer inspection this is consistent ... remove this finding"), or one so long it is
    the reviewer thinking aloud. It is not a problem in the script, so it is dropped instead of counting as one."""
    text = f"{finding.get('problem') or ''} {finding.get('suggested_fix') or ''}"
    return bool(_RETRACTED.search(text)) or len(str(finding.get("problem") or "")) > 1200


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
    already_reported: Optional[List[str]] = None
) -> Dict[str, Any]:
    """One review lens over the whole script. The cards are sent in full, in groups that fit the token budget, and the
    findings are combined. If any group cannot be reviewed the lens did not run (never a pass)."""
    spec = LENS_SPECS.get(lens_key, LENS_SPECS["technical_accuracy"])
    lens_name = spec["lens_name"]
    max_majors = spec["max_majors"]

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
    groups = _chunks(cards)
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
Report at most 8 findings for these cards, the most serious first.

{earlier_text}CRITERIA
{criteria_text}

CARDS (full text)
{json.dumps(group, separators=(",", ":"))}

{SEVERITY_GUIDE}

Return ONLY JSON:
{{"score": number 0 to 10, "summary": string,
  "findings": [{{"card_id": number, "rule": string, "severity": "blocker" | "major" | "minor",
                 "quote": string under 20 words, "problem": string, "suggested_fix": string}}]}}"""
        try:
            parsed = call_llm_json(
                messages=[{"role": "user", "content": prompt}],
                max_tokens=2500,
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
        got = sorted((f for f in parsed.get("findings", []) if isinstance(f, dict) and not is_retracted(f)), key=lambda f: order_.get(f.get("severity"), 3))
        findings.extend(got[:8])                               # a reviewer's list is capped, most serious first
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
    already_reported: Optional[List[str]] = None
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
                already_reported=already_reported
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

    # Sort results deterministically
    order = {"Technical Accuracy": 0, "Learner Clarity": 1, "Intermediate Reviewer": 2, "Advanced Reviewer": 3, "Syllabus Coverage & SOP Rules": 4}
    results.sort(key=lambda x: order.get(x.get("lens_name", ""), 3))
    return results
