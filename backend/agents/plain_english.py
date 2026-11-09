"""Simple English for every card, at the moment it is written.

The writer is told to use short sentences and everyday words, but a model drifts. So each card goes through two steps before it is saved:
  1. fix_fancy_words: a fancy word is swapped for the everyday one ("utilise" becomes "use"). By code, no model call.
  2. simplify_long_sentences: a sentence longer than MAX_WORDS is rewritten as short sentences in one small request. The rewrite is used only when it keeps every number,
     every `code` span and the length of the meaning; otherwise the original sentence stays. Best effort: a failed request leaves the card as it was.
Code blocks, headings, callouts, tables and lists are never touched."""
import re
from typing import Any, Callable, Dict, List, Optional

from agents.rule_checker import _FANCY_WORDS, _FANCY_WORDS_SIMPLE, _sentences

MAX_WORDS = 25
MAX_REWRITES = 8

_SKIP_LINE = re.compile(r"^\s*(?:#|>|\||[-*•]\s|\d{1,2}[.)]\s|\[\[|\[pause)")


def _match_case(found: str, simple: str) -> str:
    return simple.capitalize() if found[:1].isupper() else simple


def fix_fancy_words(text: str) -> str:
    """Swap fancy words for everyday ones outside code blocks and `inline code`."""
    if not text:
        return text
    out, in_fence = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if in_fence:
            out.append(line)
            continue
        parts = re.split(r"(`[^`]*`)", line)
        out.append("".join(p if p.startswith("`") else _FANCY_WORDS.sub(lambda m: _match_case(m.group(0), _FANCY_WORDS_SIMPLE[m.group(0).lower()]), p) for p in parts))
    return "\n".join(out)


def long_sentences(text: str) -> List[str]:
    """The plain sentences over MAX_WORDS words, in order, from ordinary paragraphs only."""
    found, in_fence = [], False
    for line in (text or "").split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence or not line.strip() or _SKIP_LINE.match(line):
            continue
        for s in _sentences(re.sub(r"\[\[[^\]]*\]\]", " ", line)):
            if len(s.split()) > MAX_WORDS:
                found.append(s.strip())
    return found


def _numbers(s: str) -> List[str]:
    return sorted(re.findall(r"\d+(?:[.,]\d+)?", s))


def _spans(s: str) -> List[str]:
    return sorted(re.findall(r"`[^`]*`", s))


def acceptable(original: str, rewrite: str) -> bool:
    """The rewrite keeps every number and `code` span, is made of short sentences and is not a summary of the original."""
    if not rewrite or not isinstance(rewrite, str):
        return False
    if _numbers(original) != _numbers(rewrite) or _spans(original) != _spans(rewrite):
        return False
    if any(len(x.split()) > MAX_WORDS for x in _sentences(rewrite)):
        return False
    return len(rewrite.split()) >= 0.5 * len(original.split())


PROMPT = """Rewrite each sentence below in simple English for a beginner who is listening to an instructor.
Rules: split it into short sentences of about 12 to 15 words (none over 25); use everyday words; keep every number, name, formula and `code` span exactly as written; add no new facts and leave nothing important out.
Return ONLY JSON: {{"rewrites": [string, ...]}} with one string per sentence, in the same order. Each string may hold two or three short sentences.

SENTENCES:
{sentences}
"""


def simplify_long_sentences(text: str, ask: Callable[[str], Any]) -> str:
    """Rewrite the long sentences of a card. `ask(prompt)` returns the parsed JSON reply. Never raises: on any failure the text comes back as it was."""
    todo = long_sentences(text)[:MAX_REWRITES]
    if not todo:
        return text
    import json
    try:
        got = ask(PROMPT.format(sentences=json.dumps(todo, ensure_ascii=False, indent=0)))
        rewrites = got.get("rewrites") if isinstance(got, dict) else None
    except Exception as e:
        print(f"Plain English rewrite skipped: {str(e)[:100]}")
        return text
    if not isinstance(rewrites, list) or len(rewrites) != len(todo):
        return text
    for original, new in zip(todo, rewrites):
        if acceptable(original, new):
            text = text.replace(original, new.strip(), 1)
    return text


def polish(text: str, ask: Optional[Callable[[str], Any]] = None) -> str:
    """The whole step for one card: the free word fix, then the rewrite of long sentences when a model is available."""
    text = fix_fancy_words(text)
    return simplify_long_sentences(text, ask) if ask is not None else text
