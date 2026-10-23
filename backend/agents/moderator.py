"""
Moderator Agent: repairs only the cards the reviewers and the rule checker flagged.

How it works inside the free-tier budget:
  * code fixes what code can fix (em dashes, durations), with no model call
  * each flagged card is repaired by its own small request: the card, its findings, the text of only the rules
    those findings cite, and a short scope digest (agenda and outcomes). It never sends the whole script.
  * a finding about the script as a whole (card_id "all", for example "too few predict moments") has no single card
    to repair, so it is left for the SME and listed as unresolved
  * if the model cannot answer, the card is left as it was and listed as unresolved. The Moderator never reports a
    script as good-to-go unless there is nothing left to fix.

Conflict priority when fixes pull apart: correctness, then difficulty arc, then room mix, then time.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.llm import LLMUnavailable, call_llm_json, llm_configured
from agents.rule_checker import fix_automatically
from agents.shared_context import LIMITS, RULES, RULES_BY_ID, TEACHING_CARD_TYPES

MAX_CARDS_PER_PASS = 8
MAX_CARDS_WHEN_FIXING_ALL = 40       # when the author asks for every fix to be made, no flagged card is left for the next pass
ACTIONABLE = ("blocker", "major")

REPAIR_TASK = """\
You repair ONE card of a lecture script. Fix every finding below and change nothing else.
Keep the card's meaning, running example and names. Keep its length too, unless a finding is about length: then meet the word count it gives, keeping every topic. Do not add anything outside the scope. No em dashes.
Use the language of a teacher speaking to "you". The card text is the exact words the instructor says aloud: no directions to the
instructor ("Greet everyone", "Ask the learners to...") and no talk about "the learners"; only short bracketed cues such as [pause for replies].

SCOPE
{scope}

RULES THESE FINDINGS CITE
{rules}

FINDINGS FOR THIS CARD
{findings}

THE CARD (card_type {card_type}, {duration} minutes)
title: {title}
body:
{body}
code ({language}): {code}
quiz: {quiz}
sources: {sources}

Return ONLY JSON with the fields you changed or kept:
{{"title": string, "content_script": string, "code_snippet": string or null, "code_language": string or null,
  "quiz": {{"question": string, "options": [string], "correct_option_index": int, "explanation": string,
            "distractor_explanations": {{"A": string}}}} or null,
  "sources": [{{"claim": string, "source": string, "search_terms": string}}]}}
"""


def _kind(card: Dict[str, Any]) -> str:
    return str(card.get("pedagogical_phase") or card.get("card_type") or "").lower().replace(" ", "_")


def _rules_text(rule_ids: List[str]) -> str:
    wanted = [r.upper() for r in rule_ids]
    lines = [f"{RULES_BY_ID[i].id}: {RULES_BY_ID[i].text}" for i in dict.fromkeys(wanted) if i in RULES_BY_ID]
    return "\n".join(lines) or "(none cited; use your judgement against the finding)"


def moderate_and_repair(
    cards: List[Dict[str, Any]],
    reviews: List[Dict[str, Any]],
    target_duration_mins: int,
    iteration: int = 1,
    scope_digest: Optional[str] = None,
    block_a_context: Optional[str] = None,
    script_id: Optional[int] = None,
    max_passes: int = 5,
    severities: Tuple[str, ...] = ACTIONABLE,
    on_card=None,
    max_cards: int = MAX_CARDS_PER_PASS,
) -> Tuple[List[Dict[str, Any]], bool, Dict[str, Any]]:
    """Returns (cards, is_gtg, info). `info` has changed (card indexes the Moderator edited), fixed, unresolved, summary.
    The cards come back as new dicts; the caller saves the ones listed in info["changed"]."""
    findings = [{**f, "lens": r.get("lens_name")} for r in reviews for f in r.get("findings", [])]
    actionable = [f for f in findings if f.get("severity") in severities]
    work = [dict(c) for c in cards]
    changed: List[int] = []
    fixed: List[str] = []

    def done(is_gtg: bool, unresolved: List[Dict[str, Any]], summary: str):
        return work, is_gtg, {
            "iteration": iteration, "is_gtg": is_gtg, "escalate_to_sme": not is_gtg and bool(unresolved),
            "changed": sorted(set(changed)), "fixed": fixed, "unresolved": unresolved, "summary": summary,
        }

    # 1. code fixes (no model call): em dashes, then durations
    by_index = {c.get("card_index"): c for c in work}
    before = {i: (c.get("content_script"), c.get("title"), c.get("stage_directions")) for i, c in by_index.items()}
    if fix_automatically(work):
        for i, c in by_index.items():
            if before[i] != (c.get("content_script"), c.get("title"), c.get("stage_directions")):
                changed.append(i)
        fixed.append("Removed em dashes")
    if any(f.get("rule") in ("S1", "S4") for f in actionable):
        old = [c.get("duration_mins") for c in work]
        rebalance_durations(work, target_duration_mins)
        for c, o in zip(work, old):
            if c.get("duration_mins") != o:
                changed.append(c.get("card_index"))
        fixed.append(f"Rebalanced durations to {target_duration_mins} minutes")

    if not actionable:
        return done(True, [], "Nothing needs repair.")

    # 2. the model repairs each flagged card (findings with a card number)
    per_card: Dict[int, List[Dict[str, Any]]] = {}
    unresolved: List[Dict[str, Any]] = []
    for f in actionable:
        if f.get("rule") == "R8" or f.get("auto_fix"):
            continue                              # already fixed by code
        if f.get("rule") in ("S1", "S4"):
            continue                              # timing handled above
        try:
            per_card.setdefault(int(f.get("card_id")), []).append(f)
        except (TypeError, ValueError):
            unresolved.append(f)                  # a whole-script finding: no single card to repair

    if iteration > max_passes:
        for lst in per_card.values():
            unresolved.extend(lst)
        per_card = {}

    if per_card and not llm_configured():
        for lst in per_card.values():
            unresolved.extend(lst)
        per_card = {}

    for n, (idx, flist) in enumerate(sorted(per_card.items(), key=lambda kv: -len(kv[1]))):
        card = by_index.get(idx)
        if card is None:
            unresolved.extend(flist)
            continue
        if n >= max_cards:
            unresolved.extend(flist)
            continue
        from agents.progress import report
        report(script_id, f"Moderator: repairing card {idx} ({n + 1} of {min(len(per_card), max_cards)})")
        prompt = REPAIR_TASK.format(
            scope=scope_digest or (block_a_context or "")[:1500] or "(not given)",
            rules=_rules_text([f.get("rule", "") for f in flist]),
            findings=json.dumps([{k: f.get(k) for k in ("rule", "severity", "quote", "problem", "suggested_fix")} for f in flist], separators=(",", ":")),
            card_type=_kind(card) or "concept", duration=card.get("duration_mins"), title=card.get("title"),
            body=card.get("content_script") or "", language=card.get("code_language") or "-",
            code=(card.get("code_snippet") or "none"),
            quiz=json.dumps(card.get("quiz")) if card.get("quiz") else "none",
            sources=json.dumps(card.get("sources") or []),
        )
        try:
            out = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=2500, temperature=0.2, agent="moderator", script_id=script_id)
        except LLMUnavailable as e:
            unresolved.extend(flist)
            fixed.append(f"Card {idx}: not repaired ({str(e)[:90]})")
            if on_card:
                on_card(idx, False)
            continue
        if not isinstance(out, dict) or not (out.get("content_script") or out.get("quiz")):
            unresolved.extend(flist)
            if on_card:
                on_card(idx, False)
            continue
        for key in ("title", "content_script", "code_language", "quiz", "sources"):
            if out.get(key) not in (None, "", []):
                card[key] = out[key]
        if "code_snippet" in out:
            card["code_snippet"] = out["code_snippet"]
        fix_automatically([card])
        changed.append(idx)
        fixed.append(f"Card {idx}: repaired ({', '.join(sorted({f.get('rule', '?') for f in flist}))})")
        if on_card:
            on_card(idx, True)

    # 3. the result
    unresolved_cards = sorted({f.get("card_id") for f in unresolved}, key=str)
    summary = (f"Moderator pass {iteration}: edited {len(set(changed))} cards. "
               + (f"{len(unresolved)} findings left for the SME ({', '.join(map(str, unresolved_cards))})." if unresolved else "Nothing left unresolved."))
    # Repaired cards still need a fresh review: is_gtg stays False until the next review passes.
    return done(False if (unresolved or changed) else True, unresolved, summary)


def rebalance_durations(cards: List[Dict[str, Any]], target_mins: float) -> None:
    """Scale card durations to sum to target_mins, keeping each card inside the range for its type (rule S1)."""
    if not cards:
        return

    def limits(c):
        k = _kind(c)
        return (LIMITS["teaching_card_minutes"] if k in TEACHING_CARD_TYPES else
                LIMITS["quiz_card_minutes"] if k == "quiz" else LIMITS["note_card_minutes"])

    cur = sum(float(c.get("duration_mins") or 0) for c in cards) or 1.0
    for c in cards:
        lo, hi = limits(c)
        c["duration_mins"] = round(min(hi, max(lo, float(c.get("duration_mins") or lo) * target_mins / cur)), 1)
    # hand the remainder out in 0.1 minute steps, always to the card with the most room, until the total is exact
    step = 0.1
    for _ in range(5000):
        diff = round(target_mins - sum(c["duration_mins"] for c in cards), 1)
        if abs(diff) < 0.05:
            break
        grow = diff > 0
        room = [(limits(c)[1] - c["duration_mins"]) if grow else (c["duration_mins"] - limits(c)[0]) for c in cards]
        best = max(range(len(cards)), key=lambda i: room[i])
        if room[best] < step - 1e-9:
            break
        cards[best]["duration_mins"] = round(cards[best]["duration_mins"] + (step if grow else -step), 1)


def reconcile_and_repair(
    script_id: int,
    cards: List[Dict[str, Any]],
    reviews: List[Dict[str, Any]],
    retry_count: int,
    content_mix_beginner: int = 70,
    block_a_context: Optional[str] = None,
) -> Tuple[List[Dict[str, Any]], bool, str]:
    """Legacy adapter for workflow_graph.py."""
    target = int(round(sum(float(c.get("duration_mins", 0)) for c in cards))) or 90
    repaired, is_gtg, info = moderate_and_repair(cards, reviews, target, iteration=retry_count + 1, block_a_context=block_a_context)
    return repaired, is_gtg, info.get("summary", "Moderation complete.")
