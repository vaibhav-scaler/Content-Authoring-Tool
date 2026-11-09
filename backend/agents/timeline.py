"""
The class clock: when each card starts and ends, and how long each part of the agenda takes.

An instructor needs to know, at any moment, where the class should be on the clock. This module works that out from the cards' planned minutes:
  * build_timeline()   each card's start and end, counted from the first word of the class
  * clock()            a time as 12:30 or 1:05:00
  * agenda_timing()    each agenda item with the minutes the cards that teach it take
  * ensure_agenda_block()  the agenda card always lists every item with its time, after the welcome
"""

import re
from typing import Any, Dict, List, Optional

from agents.rule_checker import covered, spoken_text

NOT_TEACHING = ("instructor_note", "welcome", "agenda", "faq", "recap")


def clock(seconds: float) -> str:
    """12:30 for 12 minutes 30 seconds, 1:05:00 for an hour and five minutes."""
    s = max(0, int(round(seconds)))
    h, rest = divmod(s, 3600)
    m, sec = divmod(rest, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def build_timeline(cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """[{card_index, start_sec, end_sec, minutes}] in card order, each card starting where the one before ended."""
    out, t = [], 0.0
    for c in sorted(cards, key=lambda c: c.get("card_index") or 0):
        dur = float(c.get("duration_mins") or 0) * 60.0
        out.append({"card_index": c.get("card_index"), "start_sec": round(t, 1), "end_sec": round(t + dur, 1), "minutes": round(dur / 60.0, 1)})
        t += dur
    return out


def agenda_timing(outline_cards: List[Dict[str, Any]], agendas: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """[{item, minutes, cards}]: each agenda item with the minutes of the teaching and quiz cards that serve it. A card that serves several items shares
    its time between them. With no agenda given, the items the cards name are used, in the order they first appear."""
    teaching = [c for c in outline_cards if c.get("card_type") not in NOT_TEACHING]
    items = [a for a in (agendas or []) if isinstance(a, str) and a.strip()]
    if not items:
        for c in teaching:
            for a in c.get("agenda_items") or []:
                if a and a not in items:
                    items.append(a)
    serving: Dict[int, List[int]] = {i: [] for i in range(len(items))}
    for n, c in enumerate(teaching):
        named = [str(a) for a in (c.get("agenda_items") or [])]
        for i, item in enumerate(items):
            if any(a.strip().lower() == item.strip().lower() for a in named) or (named and covered(item, [" ".join(named)], threshold=0.7)):
                serving[i].append(n)
    shares = {n: sum(1 for lst in serving.values() if n in lst) for n in range(len(teaching))}
    intro = sum(float(c.get("duration_sec") or 0) / 60.0 for c in outline_cards if c.get("card_type") in ("instructor_note", "welcome", "agenda"))
    out = []
    for i, item in enumerate(items):
        minutes = sum((float(teaching[n].get("duration_sec") or 0) / 60.0) / max(1, shares[n]) for n in serving[i])
        if minutes <= 0 and not serving[i] and re.search(r"\b(warm|welcome|intro|getting to know|icebreak)", item, re.I):
            minutes = intro                                  # the opening is the note, welcome and agenda cards
        out.append({"item": item.strip(), "minutes": round(minutes, 1), "cards": [teaching[n].get("card_id") or n for n in serving[i]]})
    return out


def _short_title(title: str) -> str:
    """The part of a card title before a colon or a dash: "How This Program Works: PSP, Attendance" gives "How This Program Works"."""
    return (re.split(r"\s+[\u2014\u2013-]\s+|:\s+|\s*\(", title or "", maxsplit=1)[0].strip() or (title or "").strip())


def with_class_segments(outline_cards: List[Dict[str, Any]], timing: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The agenda items with the rest of the class around them, so the minutes read aloud add up to the class length: the opening (note, welcome and agenda cards) first, then each
    program FAQ card under its own name, then the items, then the recap, and whatever time no row claims goes to the last row. Whole minutes that add up exactly (largest remainder)."""
    if not timing:
        return timing
    secs = lambda c: float(c.get("duration_sec") or 0) / 60.0
    total = sum(secs(c) for c in outline_cards)
    if total <= 0:
        return timing
    opening = sum(secs(c) for c in outline_cards if c.get("card_type") in ("instructor_note", "welcome", "agenda"))
    faqs = [(_short_title(c.get("title")), secs(c)) for c in outline_cards if c.get("card_type") == "faq"]
    recap = sum(secs(c) for c in outline_cards if c.get("card_type") == "recap")
    rows: List[List[Any]] = []                                   # [label, minutes, cards]
    if opening > 0:
        rows.append(["Welcome, introductions and agenda", opening, []])
    rows += [[name, mins, []] for name, mins in faqs]
    rows += [[t["item"], float(t["minutes"]), t.get("cards", [])] for t in timing]
    rows.append(["Recap and what's next", recap, []])
    leftover = total - sum(r[1] for r in rows)
    if abs(leftover) > 0.05:
        rows[-1][1] += leftover                                  # time no row claims (a card without an agenda item) closes the class
    goal = int(round(total))
    floors = [max(1, int(r[1])) if r[1] > 0 else 0 for r in rows]
    drift = goal - sum(floors)
    order = sorted(range(len(rows)), key=lambda k: -(rows[k][1] - int(rows[k][1])))
    k = 0
    while drift != 0 and order and k < 1000:
        idx = order[k % len(order)]
        if drift > 0 and rows[idx][1] > 0:
            floors[idx] += 1; drift -= 1
        elif drift < 0 and floors[idx] > 1:
            floors[idx] -= 1; drift += 1
        k += 1
    out = []
    for r, m in zip(rows, floors):
        if r[0] == "Recap and what's next" and m <= 0:
            continue
        out.append({"item": r[0], "minutes": m, "cards": r[2]})
    return out


def _minutes_text(m: float) -> str:
    if m <= 0:
        return "time to be decided"
    whole = max(1, int(round(m)))
    return f"{whole} minute" + ("" if whole == 1 else "s")


def agenda_block(timing: List[Dict[str, Any]]) -> str:
    """The agenda as the instructor reads it aloud: every item, in order, with its time."""
    untimed = bool(timing) and all(t.get("untimed") for t in timing)
    lines = ["Here is what we will do today." if untimed else "Here is what we will do today, and how long each part takes.", ""]
    for n, t in enumerate(timing, start=1):
        lines.append(f"{n}. {t['item']}" + ("" if untimed else f" ({_minutes_text(t['minutes'])})"))
    return "\n".join(lines)


def agenda_is_complete(body: str, timing: List[Dict[str, Any]]) -> bool:
    """True when the card names every agenda item and gives most of them a time."""
    if not timing:
        return True
    text = spoken_text(body)
    named = sum(1 for t in timing if covered(t["item"], [text], threshold=0.6))
    timed = len(re.findall(r"\b\d+\s*(?:minutes?|mins?|min)\b", text, re.I))
    if all(t.get("untimed") for t in timing):
        return named == len(timing)
    return named == len(timing) and timed >= max(1, len(timing) // 2)


def ensure_agenda_block(body: str, timing: List[Dict[str, Any]]) -> str:
    """The agenda card's text, with the agenda and its times added after what is there when the card does not already give them. The welcome stays first."""
    if timing and all(t.get("untimed") for t in timing):
        body = re.sub(r"(?m)^(\s*(?:\d+[.)]|[-*])\s+.*?)\s*\((?:about\s+)?\d+\s*(?:minutes?|mins?)\)\s*$", r"\1", body or "")      # an untimed agenda carries no minutes
    if not timing or agenda_is_complete(body, timing):
        return body
    lines = (body or "").split("\n")
    item_re = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")
    n = 0
    while n < len(lines):                                    # an existing list of the agenda is replaced by the timed one, not repeated
        if item_re.match(lines[n]):
            end = n
            while end < len(lines) and item_re.match(lines[end]):
                end += 1
            text = " ".join(lines[n:end])
            if sum(1 for t in timing if covered(t["item"], [text], threshold=0.6)) >= max(2, len(timing) // 2):
                new = [f"{k}. {t['item']}" + ("" if t.get("untimed") else f" ({_minutes_text(t['minutes'])})") for k, t in enumerate(timing, start=1)]
                return "\n".join(lines[:n] + new + lines[end:])
            n = end
        else:
            n += 1
    return (body or "").rstrip() + "\n\n" + agenda_block(timing)
