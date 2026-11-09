"""Quizzes that belong to the script, checked at the moment they are written.

A quiz must (1) be about what this class teaches, (2) not repeat a quiz already in the script, and (3) not be sample text. These are judged by code from the words
the quiz shares with the class's scope and the cards around it, so the check is free. A quiz that fails is made again by the quiz maker (one request), and only a
quiz that is better is kept. No model call here."""
import re
from typing import Any, Dict, List, Optional

from agents.rule_checker import _ratio, _topic_words

RELATED_AT = 0.35          # the share of the quiz's own words that must appear in the class scope or the nearby cards
REPEAT_AT = 0.8            # words in common (against the shorter quiz) that make two quizzes the same idea

# phrases of the built-in sample content (agents/writer.py, mock mode): never meant for a real script
SAMPLE = re.compile(r"unbuffered synchronous|thread pool exhaustion|cascading HTTP 504|Look at the poll on your screen|"
                    r"Now let us examine .{3,120}\. When we look at ", re.I)


def looks_like_sample(*texts: Optional[str]) -> bool:
    return any(SAMPLE.search(t or "") for t in texts)


def quiz_words(q: Dict[str, Any]) -> set:
    """The words that say what the quiz is about: its question, its right answer and the reason."""
    opts, i = q.get("options") or [], q.get("correct_option_index")
    right = opts[i] if isinstance(i, int) and 0 <= i < len(opts) else ""
    return _topic_words(" ".join([re.sub(r"```.*?```", " ", str(q.get("question") or ""), flags=re.S), str(right), str(q.get("explanation") or "")]))


def problem_with(quiz: Optional[Dict[str, Any]], context_texts: List[str], other_quizzes: List[Dict[str, Any]]) -> Optional[str]:
    """Why this quiz should be made again, or None when it is fine. `context_texts`: the class scope, this card's plan, the next teaching card's plan and the cards around it."""
    if not isinstance(quiz, dict) or not str(quiz.get("question") or "").strip():
        return None
    if looks_like_sample(quiz.get("question"), " ".join(map(str, quiz.get("options") or [])), quiz.get("explanation")):
        return "it is built-in sample text"
    mine = quiz_words(quiz)
    if not mine:
        return None
    have: set = set()
    for t in context_texts:
        have |= _topic_words(t or "")
    if _ratio(mine, have) < RELATED_AT:
        return "it is not about what this class teaches"
    for other in other_quizzes:
        theirs = quiz_words(other)
        if theirs and len(mine & theirs) / min(len(mine), len(theirs)) >= REPEAT_AT:
            return "it asks the same thing as another quiz in the script"
    return None
