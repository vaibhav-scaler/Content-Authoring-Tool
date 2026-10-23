"""
What a class's approved script taught, kept for the classes after it.

When the Module Owner approves a script, build_class_memory() reads that script and records, from the script itself and nothing else:
  * the cards in order, each with its level and its one key point (its Key callout, else its first sentence)
  * the terms it defined (the bold ones), so the next class uses them without teaching them again
  * what it built: the tables, functions, classes and files in its code, so the next class refers only to things that exist
  * the running example it really used
  * its last words and the sentence that points to the next class
  * the level it reached, and what its quizzes tested

The next class's prompt shows this record (render_previous_class) so the Writer knows what learners already know and where to start. Nothing here is
guessed: a field with no evidence is left empty, never filled with a stand-in.
"""

import re
from typing import Any, Dict, List, Optional

from agents.rule_checker import spoken_tail, spoken_text

MAX_POINTS = 16
MAX_TERMS = 40
LABELS = {"key", "mistake", "remember", "real-world", "real world", "predict", "reveal", "note", "tip", "warning", "example", "output", "why not", "likely failure",
          "the likely failure", "common mistake", "what breaks", "success looks like", "what success looks like", "run it", "answer", "hint", "recap", "term", "definition"}
TEACHING = ("concept", "problem", "walkthrough", "recap")
_NEXT_CUE = re.compile(r"\b(next class|next session|next time|next up|coming next|up next|coming class|upcoming class|in the next class|in class \d+|see you (in|at) the next)\b", re.I)


def _first_sentence(text: str, words: int = 22) -> str:
    sentence = re.split(r"(?<=[.!?])\s+", spoken_text(text).strip(), maxsplit=1)[0] if text else ""
    parts = sentence.split(" ")
    return " ".join(parts[:words]) + ("..." if len(parts) > words else "")


def _key_point(card: Dict[str, Any]) -> str:
    body = card.get("content_script") or ""
    m = re.search(r"^\s*>\s*\*\*\s*Key\s*:?\s*\*\*\s*:?\s*(.+)$", body, re.I | re.M)
    if m:
        return _first_sentence(m.group(1))
    return _first_sentence(re.sub(r"^\s*#{1,6}\s+.*$", "", body, flags=re.M).strip())


_DEFINES = re.compile(r"\b(is|are|means|stands for|refers to|called|known as|describes|denotes)\b", re.I)


def taught_terms(cards: List[Dict[str, Any]]) -> List[str]:
    """The terms the script DEFINED. A bold phrase counts only when it is short and the sentence around it defines it ("A **table** is rows and columns",
    "this is called **indexing**"). Bold used for emphasis, list labels, headings and whole phrases is not a term and is left out."""
    seen: Dict[str, str] = {}
    for c in cards:
        body = re.sub(r"```.*?```", " ", c.get("content_script") or "", flags=re.S)
        for m in re.finditer(r"\*\*(?:Term|Definition)\s*:?\s*\*\*\s*:?\s*([A-Za-z][^()\n:;.]{0,38}?(?:\s*\([^)\n]{2,50}\))?)\s+(?:is|are|means)\b", body):
            seen.setdefault(m.group(1).strip().lower(), m.group(1).strip())          # a "Term:" callout names the term it defines
        for sentence in re.split(r"(?<=[.!?])\s+|\n+", body):
            for m in re.finditer(r"\*\*([^*\n]{2,40})\*\*", sentence):
                term = m.group(1).strip().rstrip(":").strip()
                words = term.split()
                if (term.lower() in LABELS or len(words) > 4 or re.search(r"[.!?:;,\d]", term) or sentence.strip().startswith("**" + m.group(1))
                        and not _DEFINES.search(sentence[m.end():m.end() + 40])):
                    continue
                after, before = sentence[m.end():m.end() + 45], sentence[max(0, m.start() - 30):m.start()]
                if _DEFINES.search(after) or re.search(r"\b(called|known as|term|named|define[sd]?)\s*$", before, re.I):
                    seen.setdefault(term.lower(), term)
    return list(seen.values())[:MAX_TERMS]


def built_artefacts(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """What the script's code created: tables, functions, classes and files, with the card each came from."""
    out: List[Dict[str, Any]] = []
    seen = set()

    def add(kind: str, name: str, card: int):
        if (kind, name.lower()) not in seen:
            seen.add((kind, name.lower()))
            out.append({"kind": kind, "name": name, "card": card})
    for c in cards:
        code = c.get("code_snippet") or ""
        n = c.get("card_index")
        if not code.strip():
            continue
        for m in re.finditer(r"\bcreate\s+(?:(?:global\s+|local\s+)?(?:temp|temporary)\s+)?(?:table|view)\s+(?:if\s+not\s+exists\s+)?([\w.`\"]+)\s*(\([^)]*\))?", code, re.I):
            cols = re.findall(r"([A-Za-z_]\w*)\s+(?:INT|INTEGER|TEXT|VARCHAR|REAL|FLOAT|DATE|BOOLEAN|NUMERIC|STRING|TIMESTAMP)", m.group(2) or "", re.I)
            add("table", m.group(1).strip('`"') + (f"({', '.join(cols[:8])})" if cols else ""), n)
        for m in re.finditer(r"^\s*(?:def|function)\s+([A-Za-z_]\w*)", code, re.M):
            add("function", m.group(1), n)
        for m in re.finditer(r"^\s*(?:public\s+|final\s+|abstract\s+)*class\s+([A-Za-z_]\w*)", code, re.M):
            add("class", m.group(1), n)
        for m in re.finditer(r"\b([\w-]+\.(?:py|csv|json|txt|sql|ipynb|js|java|md))\b", code):
            add("file", m.group(1), n)
    return out[:20]


def build_class_memory(title: str, cards: List[Dict[str, Any]], outline: Optional[Dict[str, Any]], next_class_title: Optional[str] = None) -> Dict[str, Any]:
    """The record of an approved script, built only from the script (its cards and its outline)."""
    cards = sorted(cards, key=lambda c: c.get("card_index") or 0)
    points = []
    for c in cards:
        kind = str(c.get("pedagogical_phase") or "").lower().replace(" ", "_")
        if kind in TEACHING and (c.get("content_script") or "").strip():
            points.append({"n": c.get("card_index"), "title": c.get("title"), "type": kind, "level": c.get("level"), "point": _key_point(c),
                           "covers": [str(x)[:80] for x in (c.get("covers") or [])][:3]})
    quizzes = [spoken_text((c.get("quiz") or {}).get("question"))[:140] for c in cards if isinstance(c.get("quiz"), dict) and (c.get("quiz") or {}).get("question")]
    # the closing words: the recap if there is one, else the last teaching card; and the sentence that points to the next class
    closing_card = next((c for c in reversed(cards) if str(c.get("pedagogical_phase") or "").lower() == "recap" and c.get("content_script")), None) \
        or next((c for c in reversed(cards) if str(c.get("pedagogical_phase") or "").lower() in TEACHING and c.get("content_script")), None)
    pointer = ""
    for c in reversed(cards[-4:]):
        for sentence in re.split(r"(?<=[.!?])\s+", spoken_text(c.get("content_script"))):
            words_ = sentence.split()
            title_case = len(words_) >= 3 and sum(1 for w in words_ if w[:1].isupper()) >= 0.7 * len(words_)      # "One Line on What Is Coming Next." is a heading
            if _NEXT_CUE.search(sentence) and len(words_) >= 6 and not title_case:
                pointer = sentence.strip()
                break
        if pointer:
            break
    levels = [c.get("level") for c in cards if c.get("level")]
    running = (outline or {}).get("running_example") if isinstance(outline, dict) else None
    return {
        "title": title, "cards": len(cards), "points": points[:MAX_POINTS], "terms": taught_terms(cards), "artefacts": built_artefacts(cards),
        "running_example": running or None, "ending": spoken_tail(closing_card.get("content_script"), 55) if closing_card else "", "next_pointer": pointer,
        "next_class": next_class_title, "level_reached": levels[-1] if levels else None, "quiz_topics": quizzes[:8],
    }


def latest_script_memory(db, class_id: int, next_class_title: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The record of what a class's NEWEST script teaches, for when the Module Owner has not approved one yet. The next class can still follow it, with the
    text saying it is a draft that may change. None when the class has no script with enough written cards (8 or more) to say anything."""
    import json
    from models import Script, ScriptCard
    rank = {"MO Approval": 0, "SME Review": 1, "Script WIP": 2}
    best = None
    for sc in db.query(Script).filter(Script.class_id == class_id, Script.status.in_(list(rank))).order_by(Script.id.desc()).all():
        rows = db.query(ScriptCard).filter(ScriptCard.script_id == sc.id).order_by(ScriptCard.card_index).all()
        if sum(1 for r in rows if (r.content_script or "").strip()) < 8:
            continue
        if best is None or rank[sc.status] < rank[best[0].status]:
            best = (sc, rows)
    if best is None:
        return None
    sc, rows = best
    cards = []
    for r in rows:
        try:
            quiz = json.loads(r.quiz_json) if r.quiz_json else None
        except ValueError:
            quiz = None
        cards.append({"card_index": r.card_index, "title": r.title, "pedagogical_phase": r.pedagogical_phase, "content_script": r.content_script,
                      "code_snippet": r.code_snippet, "quiz": quiz if isinstance(quiz, dict) else None})
    try:
        outline = json.loads(sc.outline_json) if sc.outline_json else None
    except ValueError:
        outline = None
    memory = build_class_memory(sc.custom_class_title or "", cards, outline if isinstance(outline, dict) else None, next_class_title)
    memory["draft_status"] = sc.status
    return memory if memory.get("points") else None


def render_previous_class(number: Any, title: str, memory: Optional[Dict[str, Any]], approved_by: Optional[str] = None) -> str:
    """The previous class as the next class's Writer reads it. With no approved script the text says so plainly and asks for no guessing."""
    if not memory or not memory.get("points"):
        return ("<previous_class>\n"
                f"Class {number} \"{title}\" has NO approved script yet, so there is no record of what it actually taught. Assume only that its agenda was covered. "
                "Do not rely on any detail from it: if you need an example, a term or a file from it, write [[FROM_CLASS: <class title> | <what is needed>]].\n"
                "</previous_class>")
    if memory.get("draft_status"):
        lines = ["<previous_class>",
                 f"Class {number} \"{title}\" has no approved script yet; its newest script is still a draft ({memory['draft_status']}). Below is what that draft teaches. "
                 "Follow it as the best record there is, and assume learners know it if the class is delivered as drafted, but it may still change, so do not depend on a small detail.",
                 "Taught in the draft, in order:"]
    else:
        lines = ["<previous_class>",
                 f"Class {number} \"{title}\" was approved{(' by ' + approved_by) if approved_by else ''}. Below is what its approved script taught. Learners know ALL of it.",
                 "Taught, in order:"]
    for i, p in enumerate(memory["points"], start=1):
        lines.append(f"  {i}. {p['title']}" + (f" [{p['level']}]" if p.get("level") else "")
                     + (f" - covers: {'; '.join(p['covers'])}" if p.get("covers") else "") + (f". Key point: {p['point']}" if p.get("point") else ""))
    if memory.get("terms"):
        lines.append("Terms they now know (use them freely, never define them again): " + ", ".join(memory["terms"]))
    if memory.get("artefacts"):
        lines.append("What the class built (these exist, nothing else does): " + "; ".join(f"{a['kind']} {a['name']}" for a in memory["artefacts"]))
    if memory.get("quiz_topics"):
        lines.append("Its quizzes tested: " + " | ".join(memory["quiz_topics"][:4]))
    if memory.get("running_example"):
        lines.append(f"Its running example: {memory['running_example']}")
    if memory.get("level_reached"):
        lines.append(f"The level it reached: {memory['level_reached']}")
    if memory.get("ending"):
        lines.append(f"Its last words were: \"{memory['ending']}\"")
    if memory.get("next_pointer"):
        lines.append(f"It pointed to this class with: \"{memory['next_pointer']}\"")
    lines.append("START HERE: this class begins where that one stopped. Its first spoken card opens with two or three spoken sentences on where we left off (use its last words), "
                 "then moves to today's agenda. Reviewing something already taught takes one sentence at most, and never re-explains it.")
    lines.append("</previous_class>")
    return "\n".join(lines)
