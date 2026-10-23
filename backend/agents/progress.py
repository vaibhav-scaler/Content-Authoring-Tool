"""Progress messages for long jobs, so the UI and the server log can say what a slow review is doing."""
import logging
import threading
import time
from typing import Dict, Optional

log = logging.getLogger("progress")
_lock = threading.Lock()
_state: Dict[int, Dict[str, object]] = {}


def report(script_id: Optional[int], message: str, done: bool = False) -> None:
    log.warning("script %s: %s", script_id, message)         # warning level so it shows under uvicorn's default logging
    if script_id is None:
        return
    with _lock:
        previous = _state.get(script_id) or {}
        steps = list(previous.get("steps") or []) if not previous.get("done") else []        # a new run starts a fresh list
        steps.append({"message": message, "at": time.time()})
        _state[script_id] = {"message": message, "done": done, "at": time.time(), "steps": steps[-40:], "partial": [] if not previous or previous.get("done") else list(previous.get("partial") or []),
                             "checklist": previous.get("checklist")}


def current(script_id: int) -> Dict[str, object]:
    with _lock:
        return dict(_state.get(script_id) or {"message": "", "done": True, "at": 0, "steps": []})


def clear_partial(script_id: Optional[int]) -> None:
    """A new round of reviewing starts: the reviewers' results shown so far belong to the round before."""
    if script_id is None:
        return
    with _lock:
        if script_id in _state:
            _state[script_id]["partial"] = []


def add_partial(script_id: Optional[int], review: Dict[str, object]) -> None:
    """One reviewer has finished: its findings can be shown while the others are still reading."""
    if script_id is None:
        return
    with _lock:
        state = _state.setdefault(script_id, {"message": "", "done": False, "at": time.time(), "steps": [], "partial": []})
        partial = [r for r in (state.get("partial") or []) if r.get("lens_name") != review.get("lens_name")]
        partial.append(review)
        state["partial"] = partial


def set_checklist(script_id: Optional[int], items) -> None:
    """The fix checklist of the run in progress (None clears it). The page polls it, so each fix can be ticked as it is made."""
    if script_id is None:
        return
    with _lock:
        state = _state.setdefault(script_id, {"message": "", "done": False, "at": time.time(), "steps": [], "partial": []})
        state["checklist"] = [dict(i) for i in items] if items else None


def mark_card(script_id: Optional[int], card: int, status: str) -> None:
    """Every item still waiting on this card gets the status (for example "applied" when its repair has been made)."""
    if script_id is None:
        return
    with _lock:
        for i in (_state.get(script_id) or {}).get("checklist") or []:
            if str(i.get("card_id")) == str(card) and i.get("status") == "todo":
                i["status"] = status
