"""
One example dataset for the whole class.

Each card is written in its own request, and a card only sees a short gist of the ones before it. Left alone, every card invents its own people, rows and
numbers (Sneha lives in Pune in one card and Bangalore in the next, the trace table does not match the code, the diagram shows a result the text
contradicts). So before the first card of a class with code is written, one small request defines the example data once. It is stored with the outline and
handed to every card, code block and diagram, which must use exactly those names, rows and values.
"""

from typing import Any, Dict, List, Optional

from agents.llm import call_llm_json, llm_configured

MAX_CHARS = 1800

SETUP_TASK = """\
You set up ONE small example dataset for a lecture, so that every card, every code block, every trace table and every diagram of the class uses the same data.

CLASS: {title}
LEVEL: {level}
AGENDA: {agenda}
RUNNING EXAMPLE: {running_example}
CARDS THAT CARRY CODE: {coded}

Rules
- Use the language the class teaches (SQL: CREATE TABLE and INSERT statements with a one-line platform comment; Python or another language: the variables, lists or dicts the
  code will start from). If the class teaches no particular language, write the data as plain named values.
- Plain, familiar names (3 to 6 people or items) taken from the running example, and at most 12 rows or values in total.
- Name places and apps in plain words: "a small cinema hall booked through a BookMyShow-style app", never "a BookMyShow hall" (a company is not a place).
- Build the data so that it shows every situation the agenda teaches, on purpose: for example a row with no match for a join, a missing value for NULL handling, a duplicate
  for a grouping. Say which row shows which situation.
- Nothing random, nothing that later cards would have to change. ASCII only, no em dashes, at most 25 lines of code.
- Check yourself before you answer: the story and every line of "cases" must describe exactly the rows, values and constants in the code (for example, a target the cases call S3 must be
  the target the code sets). A mismatch between your own text and your own code would be copied into every card.

Return ONLY JSON: {{"story": "two or three plain sentences saying who or what the rows are", "cases": ["one line per situation the data is built to show, naming the row"], "setup": "the code, no code fences"}}
"""


def needs_shared_data(cards: List[Dict[str, Any]]) -> bool:
    return any(c.get("has_code") for c in cards or [])


def render(story: str, cases: List[str], setup: str) -> str:
    lines = [str(story).strip()]
    if cases:
        lines.append("Built to show:\n" + "\n".join(f"- {str(c).strip()}" for c in cases if str(c).strip()))
    lines.append("```\n" + str(setup).strip("\n") + "\n```")
    return "\n".join(lines)[:MAX_CHARS]


def make_shared_data(title: str, agendas: List[str], running_example: str, cards: List[Dict[str, Any]], level: Optional[str] = None,
                     script_id: Optional[int] = None) -> Optional[str]:
    """The class's example data as text, or None when the model could not give it (the cards are then written as before, without a shared dataset)."""
    if not llm_configured():
        return None
    coded = [str(c.get("title") or "") for c in cards if c.get("has_code")]
    prompt = SETUP_TASK.format(title=title, level=level or "as the track says", agenda="; ".join(agendas) or "from the class title",
                               running_example=running_example or "none: choose one everyday system", coded="; ".join(coded)[:600] or "none")
    try:
        got = call_llm_json(messages=[{"role": "user", "content": prompt}], max_tokens=1400, temperature=0.3, agent="class_data", script_id=script_id)
    except Exception as e:                      # the writer carries on without it
        print(f"Shared data skipped: {e}")
        return None
    if not isinstance(got, dict) or not str(got.get("setup") or "").strip() or not str(got.get("story") or "").strip():
        return None
    return render(got["story"], [c for c in (got.get("cases") or []) if isinstance(c, str)][:6], got["setup"])


def block(text: Optional[str]) -> str:
    """What goes into the Writer's and the visual generators' prompts."""
    if not text:
        return ""
    return ("SHARED DATA FOR THIS WHOLE CLASS. Every example, code block, trace table, question and diagram uses exactly these names, rows and values. "
            "Do not invent other people, tables, rows or numbers, and do not change any of them. If a card truly needs more data, add it and say plainly that it is new. "
            "If a diagram, animation or card plan names other seats, rows, items or sizes than these, use these instead.\n" + text)


def of_outline(outline: Any) -> str:
    return str(outline.get("shared_data") or "") if isinstance(outline, dict) else ""
