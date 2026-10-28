"""
Do the cards agree with each other? One request reads the whole script and reports where one card contradicts another: a name, a number, a table or a result
that is different from card to card, a trace that does not match its own code, a diagram that shows another result than the text, a term used before it is
defined. Reviewers read the script too, but each part on its own; this is the one place the cards are compared against each other.

It only reports (findings in the rule checker's shape, rule C1). If the model cannot answer it returns nothing and says why; it never invents a finding.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.llm import LLMUnavailable, call_llm_json, llm_configured
from agents.rule_checker import spoken_text

TEXT_CHARS = 1500
CODE_CHARS = 800

TASK = """\
You compare the cards of one lecture script against each other. You are looking ONLY for places where cards disagree.

{scope}
{shared}

Report a problem only when two parts of the script cannot both be true, or a learner who follows along would be misled:
- a person, table, row, column, value or result that differs from one card to another (another city, another number, another name for the same thing)
- a trace, table or result written in the text that does not match the code or the data in the same card or an earlier card
- a diagram's labels or result that contradict the text of its card
- the same term given two different meanings
- a term used before any card has defined it, or a card that depends on something no earlier card sets up
Do not report style, length or anything inside one card that is merely unclear. If the cards agree, return an empty list. Quote the exact words that disagree.

CARDS (in order; "says" is what the instructor says, "code" is its code, "diagram" is the words in its diagram)
{cards}

Do not write out your comparison: your whole reply is the JSON object and its first character is an opening brace. Return ONLY JSON: {{"findings": [{{"card": int (the card that must change), "also": [int] (the other cards involved), "severity": "blocker" or "major",
"problem": string, "fix": string (say which value or name to use, taken from the shared data when there is one), "quote": string}}]}}
Use "blocker" when a learner following the code or the trace would get a different result from the one stated.
"""


def _view(c: Dict[str, Any]) -> Dict[str, Any]:
    v: Dict[str, Any] = {"card": c.get("card_index"), "title": c.get("title"), "says": spoken_text(c.get("content_script"))[:TEXT_CHARS]}
    if (c.get("code_snippet") or "").strip():
        v["code"] = c["code_snippet"][:CODE_CHARS]
    if c.get("visual_type") == "mermaid" and c.get("visual_content"):
        v["diagram"] = str(c["visual_content"])[:500]
    return v


def judge_consistency(cards: List[Dict[str, Any]], scope_digest: str = "", shared_data: str = "", script_id: Optional[int] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """(findings, problem). `problem` is None when the model answered, else why it could not (the review then says this check did not run)."""
    cards = [c for c in cards if (c.get("content_script") or "").strip()]
    if len(cards) < 2:
        return [], None
    if not llm_configured():
        return [], "the AI is not configured"
    prompt = TASK.format(scope=scope_digest or "", shared=("SHARED DATA (the cards must all use it):\n" + shared_data) if shared_data else "",
                         cards=json.dumps([_view(c) for c in cards], separators=(",", ":"), ensure_ascii=False))
    try:
        got = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=4000, temperature=0.0, agent="consistency", script_id=script_id)
    except LLMUnavailable as e:
        return [], str(e)[:120]
    except Exception as e:
        return [], f"the AI could not answer ({str(e)[:80]})"
    known = {c.get("card_index") for c in cards}
    out: List[Dict[str, Any]] = []
    for f in (got.get("findings") if isinstance(got, dict) else None) or []:
        if not isinstance(f, dict) or not str(f.get("problem") or "").strip():
            continue
        if re.match(r"\s*(no change|nothing to (change|fix)|none\b|n/a)", str(f.get("fix") or ""), re.I):
            continue                                         # the model found on checking that the cards agree
        try:
            card = int(f.get("card"))
        except (TypeError, ValueError):
            continue
        if card not in known:
            continue
        also = [int(x) for x in (f.get("also") or []) if str(x).lstrip("-").isdigit() and int(x) in known and int(x) != card][:3]
        out.append({"card_id": card, "rule": "C1", "severity": "blocker" if str(f.get("severity")).lower() == "blocker" else "major",
                    "quote": str(f.get("quote") or "")[:120],
                    "problem": (f"Disagrees with card{'s' if len(also) > 1 else ''} {', '.join(map(str, also))}: " if also else "Disagrees with other cards: ") + str(f["problem"]).strip()[:260],
                    "suggested_fix": str(f.get("fix") or "Make this card use the same names, rows and values as the others.")[:260], "auto_fix": False})
    return out, None
