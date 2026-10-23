"""
Fit checker: does each diagram, animation and quiz say the right thing, not only use the right words?

The code rules in rule_checker.check_visuals_and_quizzes judge by shared words, which is free but blind to meaning. This is the second
look, by the model, in one small request for every few cards:
  * a visual must show what its card teaches, with the card's names, and nothing from outside the script
  * a quiz must test what the script has taught by that card, its keyed answer must really be right, and no other option may also be right
  * the on-screen quiz must be the question the instructor reads out

It only reports (findings in the rule checker's shape, rules V5 and Q4). Repairs are done by the review loop. If the model cannot answer, it returns
nothing and says so; it never invents a finding.
"""

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from agents.llm import LLMUnavailable, call_llm_json, llm_configured
from agents.rule_checker import spoken_text, visual_words

CARDS_PER_REQUEST = 6

JUDGE_TASK = """\
You check whether the diagrams, animations and quizzes of a lecture script say the right thing. Judge by meaning, not by shared words.

SCOPE
{scope}

For each card below you get what the instructor says, then its quiz and its visual if it has them.
Flag a VISUAL (rule V5) only when it shows something the card does not teach, uses names the script does not use, gets the order or the cause
and effect wrong, or would mislead a learner.
Flag a QUIZ (rule Q4) only when: it tests something the script has not taught by that card, the keyed answer is wrong according to the script, more than
one option is also right, or the question cannot be answered from what was taught.
Do not flag style. If a card is fine, leave it out. Quote the exact words that are wrong.

CARDS
{cards}

Return ONLY JSON: {{"findings": [{{"card": int, "item": "visual" or "quiz", "problem": string, "fix": string, "quote": string}}]}}
"""

ALIGN_TASK = """\
The instructor reads this quiz question and its options aloud from the card text. The quiz shown on screen is different. Rewrite the on-screen quiz so it is
exactly the question and options the card text gives, with the right answer the card's own reveal gives.

CARD TEXT
{body}

CURRENT ON-SCREEN QUIZ
{quiz}

Rules: options are exactly 4, each one short line with no letter in front, in the same order as the card text; correct_option_index is 0 to 3;
explanation is one or two sentences without naming a letter; distractor_explanations has one short sentence for each wrong option, keyed by its letter A to D.
No em dashes.
Return ONLY JSON: {{"question": string, "options": [string], "correct_option_index": number, "explanation": string, "distractor_explanations": {{"A": string}}}}
"""


def _visual_view(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind = c.get("visual_type")
    if kind not in ("mermaid", "animation", "image"):
        return None
    shown = " ".join(sorted(visual_words(c)))[:600]
    return {"type": kind, "caption": (c.get("visual_caption") or "")[:200], "text_it_shows": shown,
            **({"diagram": (c.get("visual_content") or "")[:700]} if kind == "mermaid" else {})}


def _card_view(c: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    visual, quiz = _visual_view(c), c.get("quiz") if isinstance(c.get("quiz"), dict) and c["quiz"].get("question") else None
    if not visual and not quiz:
        return None
    out: Dict[str, Any] = {"card": c.get("card_index"), "title": c.get("title"), "instructor_says": spoken_text(c.get("content_script"))[:1100]}
    if quiz:
        out["quiz"] = {"question": quiz.get("question"), "options": quiz.get("options"), "keyed_answer_index": quiz.get("correct_option_index")}
    if visual:
        out["visual"] = visual
    return out


def judge_fit(cards: List[Dict[str, Any]], scope_digest: str = "", script_id: Optional[int] = None,
              only: Optional[set] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """(findings, problem). `problem` is None when the model answered for every request, else why it could not (the review then says the meaning
    check did not run). `only` limits the check to those card numbers (the cards repaired since the last look)."""
    views = [v for c in cards if only is None or c.get("card_index") in only for v in [_card_view(c)] if v]
    if not views:
        return [], None
    if not llm_configured():
        return [], "the AI is not configured"
    by_card = {c.get("card_index"): c for c in cards}
    out: List[Dict[str, Any]] = []
    problem = None
    for start in range(0, len(views), CARDS_PER_REQUEST):
        chunk = views[start:start + CARDS_PER_REQUEST]
        prompt = JUDGE_TASK.format(scope=scope_digest or "(not given)", cards=json.dumps(chunk, separators=(",", ":"), ensure_ascii=False))
        try:
            got = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=1800, temperature=0.0, agent="fit_checker", script_id=script_id)
        except LLMUnavailable as e:
            problem = str(e)[:120]
            continue
        except Exception as e:
            problem = f"the AI could not answer ({str(e)[:80]})"
            continue
        for f in (got.get("findings") if isinstance(got, dict) else None) or []:
            if not isinstance(f, dict):
                continue
            try:
                card = int(f.get("card"))
            except (TypeError, ValueError):
                continue
            item = "quiz" if str(f.get("item")).lower().startswith("q") else "visual"
            if card not in by_card or not (f.get("problem") or "").strip():
                continue
            if item == "visual" and not _visual_view(by_card[card]) or item == "quiz" and not isinstance(by_card[card].get("quiz"), dict):
                continue
            out.append({"card_id": card, "rule": "Q4" if item == "quiz" else "V5", "severity": "major", "quote": str(f.get("quote") or "")[:120],
                        "problem": ("The quiz: " if item == "quiz" else "The " + str(by_card[card].get("visual_type")) + ": ") + str(f["problem"]).strip()[:260],
                        "suggested_fix": str(f.get("fix") or ("Rewrite the quiz so it tests what the script taught." if item == "quiz"
                                                              else "Regenerate it from this card's own steps and names."))[:260],
                        "auto_fix": False})
    return out, problem


def align_quiz(card: Dict[str, Any], script_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The on-screen quiz rewritten to be the question the card text reads out, or None when the model could not (the quiz is then left alone)."""
    from agents.quiz_format import clean_quiz, quiz_problems
    if not llm_configured():
        return None
    prompt = ALIGN_TASK.format(body=(card.get("content_script") or "")[:3500], quiz=json.dumps(card.get("quiz") or {}, ensure_ascii=False)[:1500])
    try:
        got = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=1200, temperature=0.0, agent="fit_checker", script_id=script_id)
    except Exception:
        return None
    quiz = clean_quiz(got) if isinstance(got, dict) else None
    if not quiz or quiz_problems(quiz):
        return None
    return quiz
