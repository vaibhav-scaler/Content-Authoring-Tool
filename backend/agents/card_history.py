"""Undo and Redo for a script card. By code, no model call.

Every change to a card's words (the author's Edit, the AI's Chat revision, a regenerated quiz, a fact filled in on the Needs from the SME screen) saves what the card said
just before the change. Undo puts the previous words back and keeps the ones it replaced for Redo; a new change clears what Redo held, as in any editor.

What a saved state holds: the title, the spoken text, the stage directions, the code and the quiz. Not the minutes (changing them moves the other cards' minutes too, so a
single card cannot take its minutes back alone) and not the approval (a card that was undone or redone is a draft again).

One hook watches the card's fields, so no route has to remember to save history.
"""
import json
from typing import Any, Dict, Optional

from sqlalchemy import event
from sqlalchemy.orm import Session

from models import CardHistory, ScriptCard

TRACKED = ("title", "stage_directions", "content_script", "code_snippet", "code_language", "quiz_json")
KEEP = 30                                  # the states kept per card, per stack
SKIP = "skip_card_history"                 # set on a session while Undo or Redo writes the card, so that write is not recorded as a new change


def snapshot(card: ScriptCard) -> Dict[str, Any]:
    return {f: getattr(card, f) for f in TRACKED}


def _remember(target, value, oldvalue, initiator):
    """Called when a tracked field is set: keep the first old value seen since the last flush."""
    from sqlalchemy.orm.attributes import NO_VALUE
    from sqlalchemy.orm.base import NEVER_SET
    if oldvalue in (NO_VALUE, NEVER_SET) or oldvalue == value:
        return
    target.__dict__.setdefault("_card_old", {}).setdefault(initiator.key, oldvalue)


for _field in TRACKED:
    event.listen(getattr(ScriptCard, _field), "set", _remember, active_history=True)


@event.listens_for(Session, "before_flush")
def _save_history(session, flush_context, instances):
    if session.info.get(SKIP):
        for obj in session.dirty:
            if isinstance(obj, ScriptCard):
                obj.__dict__.pop("_card_old", None)
        return
    changed = [o for o in session.dirty if isinstance(o, ScriptCard) and o.__dict__.get("_card_old")]
    for card in changed:
        old = dict(snapshot(card))
        old.update(card.__dict__.pop("_card_old"))
        if all(old.get(f) == getattr(card, f) for f in TRACKED) or card.id is None:
            continue
        with session.no_autoflush:
            session.query(CardHistory).filter(CardHistory.card_id == card.id, CardHistory.stack == "redo").delete(synchronize_session=False)
            session.add(CardHistory(card_id=card.id, script_id=card.script_id, stack="undo", snapshot=json.dumps(old)))
            _trim(session, card.id, "undo")


def _trim(session, card_id: int, stack: str) -> None:
    """Keep the newest KEEP states of a stack. Called right after a new state was added in the session but not yet saved, so the rows found here are one short of the total."""
    with session.no_autoflush:
        rows = session.query(CardHistory).filter(CardHistory.card_id == card_id, CardHistory.stack == stack).order_by(CardHistory.id.desc()).all()
    for extra in rows[KEEP - 1:]:
        session.delete(extra)


def counts(db, card_ids) -> Dict[int, Dict[str, int]]:
    """{card_id: {"undo": n, "redo": n}} for the cards that have any history."""
    from sqlalchemy import func
    out: Dict[int, Dict[str, int]] = {}
    if not card_ids:
        return out
    for cid, stack, n in db.query(CardHistory.card_id, CardHistory.stack, func.count(CardHistory.id)).filter(CardHistory.card_id.in_(list(card_ids))).group_by(CardHistory.card_id, CardHistory.stack).all():
        out.setdefault(cid, {"undo": 0, "redo": 0})[stack] = n
    return out


def _step(db, card: ScriptCard, take: str, give: str) -> Optional[Dict[str, Any]]:
    """Move the card one state: take the newest row of the `take` stack, save the card's present words on the `give` stack, write the taken words to the card."""
    row = db.query(CardHistory).filter(CardHistory.card_id == card.id, CardHistory.stack == take).order_by(CardHistory.id.desc()).first()
    if row is None:
        return None
    wanted = json.loads(row.snapshot)
    db.add(CardHistory(card_id=card.id, script_id=card.script_id, stack=give, snapshot=json.dumps(snapshot(card))))
    db.delete(row)
    db.info[SKIP] = True
    try:
        for f in TRACKED:
            setattr(card, f, wanted.get(f))
        card.is_approved = False
        db.flush()
    finally:
        db.info.pop(SKIP, None)
    _trim(db, card.id, give)
    return wanted


def undo(db, card: ScriptCard) -> bool:
    return _step(db, card, "undo", "redo") is not None


def redo(db, card: ScriptCard) -> bool:
    return _step(db, card, "redo", "undo") is not None
