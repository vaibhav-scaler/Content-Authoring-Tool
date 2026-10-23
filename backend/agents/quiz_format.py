"""
One way to show a quiz, everywhere: the script page, the shared page, and both Markdown exports.

The Writer returns a quiz as {question, options, correct_option_index, explanation, distractor_explanations}. Models write the options with a
letter in front ("A. ...") and key the reasons by letter ("A", "A.", "(A)") or by the option's own text. quiz_view() sorts that out once, so
every display shows the same thing:

  * the question, with any code in it shown as code
  * the options as A, B, C, D, each one short line, the letter shown once
  * the right answer marked clearly
  * for each wrong option, why it is wrong, right under it
  * why the right answer is right

On a page the answers can be hidden (for asking the class first) and shown again.
"""

import re
from typing import Any, Dict, List, Optional

from agents.safe_html import esc, format_card_text, _inline

_LETTER_PREFIX = re.compile(r"^\s*\(?([A-Ha-h])[.):\-]\s+")
_EM_DASH = "—"


def _clean(text: Any) -> str:
    return str(text or "").replace(" " + _EM_DASH + " ", ", ").replace(_EM_DASH, ", ").strip()


def _strip_letter(option: str) -> str:
    return _LETTER_PREFIX.sub("", option or "", count=1).strip()


def clean_quiz(quiz: Any) -> Any:
    """A quiz as saved: no em dashes, and no letter typed into the option text (the letter is shown by the display). Anything else is untouched."""
    if not isinstance(quiz, dict):
        return quiz
    out = dict(quiz)
    out["question"] = _clean(quiz.get("question"))
    out["options"] = [_strip_letter(_clean(o)) for o in (quiz.get("options") or [])]
    out["explanation"] = _clean(quiz.get("explanation"))
    if isinstance(quiz.get("distractor_explanations"), dict):
        out["distractor_explanations"] = {k: _clean(v) for k, v in quiz["distractor_explanations"].items()}
    return out


def quiz_view(quiz: Any) -> Optional[Dict[str, Any]]:
    """{question, options:[{letter, text, correct, why}], answer_letter, explanation}, or None when there is nothing to show."""
    if not isinstance(quiz, dict) or not (quiz.get("question") or quiz.get("options")):
        return None
    raw = [str(o) for o in (quiz.get("options") or [])]
    try:
        right = int(quiz.get("correct_option_index"))
    except (TypeError, ValueError):
        right = -1
    reasons: Dict[str, str] = {}
    for key, value in (quiz.get("distractor_explanations") or {}).items():
        letter = re.sub(r"[^A-Za-z]", "", str(key))[:1].upper()
        if len(re.sub(r"[^A-Za-z]", "", str(key))) <= 1 and letter:
            reasons[letter] = _clean(value)
        else:                                                  # keyed by the option's own text
            for i, option in enumerate(raw):
                if _strip_letter(option).lower() == _strip_letter(str(key)).lower():
                    reasons[chr(65 + i)] = _clean(value)
    options = []
    for i, option in enumerate(raw):
        letter = chr(65 + i)
        options.append({"letter": letter, "text": _strip_letter(_clean(option)), "correct": i == right, "why": "" if i == right else reasons.get(letter, "")})
    return {"question": _clean(quiz.get("question")), "options": options,
            "answer_letter": chr(65 + right) if 0 <= right < len(raw) else "", "explanation": _clean(quiz.get("explanation"))}


def quiz_html(quiz: Any) -> str:
    """The quiz as safe HTML. Everything is escaped; the question may carry code (a fenced block) and `inline code`."""
    v = quiz_view(quiz)
    if not v:
        return ""
    opts = []
    for o in v["options"]:
        answer_part = ('<div class="quiz2-ans"><span class="quiz2-tag">&#10003; Correct answer</span></div>' if o["correct"]
                       else (f'<div class="quiz2-ans quiz2-why"><b>Why not:</b> {_inline(o["why"])}</div>' if o["why"] else ""))
        opts.append(f'<div class="quiz2-opt{" right" if o["correct"] else ""}"><span class="quiz2-letter">{esc(o["letter"])}</span>'
                    f'<div class="quiz2-body"><div class="quiz2-text">{_inline(o["text"])}</div>{answer_part}</div></div>')
    expl = (f'<div class="quiz2-ans quiz2-expl"><b>Why {esc(v["answer_letter"]) if v["answer_letter"] else "this"} is right:</b> {_inline(v["explanation"])}</div>'
            if v["explanation"] else "")
    return (f'<div class="quiz2"><div class="quiz2-head"><span class="quiz2-label">Quiz</span>'
            f'<span class="quiz2-actions"><button type="button" class="quiz2-toggle" onclick="toggleQuizAnswers(this)">Hide answers</button></span></div>'
            f'<div class="quiz2-q">{format_card_text(v["question"])}</div><div class="quiz2-opts">{"".join(opts)}</div>{expl}</div>')


def quiz_markdown(quiz: Any) -> str:
    """The quiz as Markdown for an export: the question, the options with the right one marked, and the reasons."""
    v = quiz_view(quiz)
    if not v:
        return ""
    lines = [f"**Quiz:** {v['question']}", ""]
    for o in v["options"]:
        lines.append(f"- **{o['letter']}.** {o['text']}" + ("  ✅ *correct*" if o["correct"] else ""))
    lines.append("")
    if v["answer_letter"]:
        lines.append(f"**Answer: {v['answer_letter']}.** {v['explanation']}".rstrip())
    elif v["explanation"]:
        lines.append(v["explanation"])
    wrong = [f"{o['letter']}: {o['why']}" for o in v["options"] if o["why"]]
    if wrong:
        lines.append("")
        lines.append("*Why the others are wrong:* " + " ".join(wrong))
    return "\n".join(lines) + "\n"


# ---- checking, editing and regenerating a quiz ----------------------------------------------------------------------------------------

MIN_OPTIONS, MAX_OPTIONS = 2, 6


def quiz_problems(quiz: Any) -> List[str]:
    """What is wrong with a quiz, in words for the SME. Empty when it is complete: a question, 2 to 6 distinct options, a valid right answer,
    an explanation, and a reason for EVERY wrong option (rule E2)."""
    if not isinstance(quiz, dict):
        return ["There is no quiz."]
    v = quiz_view(quiz)
    if not v or not v["question"].strip():
        return ["The quiz has no question."]
    problems = []
    options = v["options"]
    if not MIN_OPTIONS <= len(options) <= MAX_OPTIONS:
        problems.append(f"A quiz needs {MIN_OPTIONS} to {MAX_OPTIONS} options; this one has {len(options)}.")
    if any(not o["text"].strip() for o in options):
        problems.append("An option is empty.")
    texts = [o["text"].strip().lower() for o in options]
    if len(set(texts)) != len(texts):
        problems.append("Two options are the same.")
    if not v["answer_letter"]:
        problems.append("No option is marked as the right answer.")
    if not v["explanation"].strip():
        problems.append("Say why the right answer is right.")
    missing = [o["letter"] for o in options if not o["correct"] and not o["why"].strip()]
    if missing:
        problems.append("Say why each wrong option is wrong (missing: " + ", ".join(missing) + ").")
    return problems


def quiz_from_edit(question: str, options: List[str], correct_index: int, explanation: str, reasons: List[str]) -> Dict[str, Any]:
    """A quiz built from the edit form: reasons[i] is the reason for option i (ignored for the right one). Cleaned like any saved quiz."""
    opts = [str(o) for o in options]
    return clean_quiz({
        "question": question, "options": opts, "correct_option_index": correct_index, "explanation": explanation,
        "distractor_explanations": {chr(65 + i): str(reasons[i]) for i in range(len(opts)) if i != correct_index and i < len(reasons) and str(reasons[i]).strip()},
    })


def move_correct_option(quiz: Dict[str, Any], position: int) -> Dict[str, Any]:
    """The same quiz with the right answer moved to `position` (the other options keep their order, and each reason follows its option).
    Models put the right answer in the same place again and again; this spreads it out."""
    v = quiz_view(quiz)
    if not v or not v["answer_letter"] or not 0 <= position < len(v["options"]):
        return quiz
    items = v["options"][:]
    right = next(i for i, o in enumerate(items) if o["correct"])
    item = items.pop(right)
    items.insert(position, item)
    return {**quiz, "options": [o["text"] for o in items], "correct_option_index": position,
            "distractor_explanations": {chr(65 + i): o["why"] for i, o in enumerate(items) if not o["correct"] and o["why"]}}
