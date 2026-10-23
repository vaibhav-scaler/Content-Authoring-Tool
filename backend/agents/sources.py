"""
Source links for the claims a card makes.

The model never writes a URL we trust. It names the source and the search terms, and this module builds the link:
  * a URL the SME supplied (reference URLs, API docs, additional resources) is kept only if it appears there verbatim
  * anything else becomes a live search link built from the source name and terms, which always resolves

Search links are leads for the SME to verify, not verified citations, and are labelled that way (rule A7).
"""

import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

MAX_SOURCES_PER_CARD = 6
_URL = re.compile(r"https?://[^\s)\]>\"']+")

# A "source" that is really the program's own approved facts (the opening kit, the SME's additional resources) is not an outside
# citation. It is kept, labelled as the SME's facts, with no search link and no claim of having been checked.
_KIT_SOURCE = re.compile(r"^\s*(the\s+)?(opening[ -]kit|program[ -]kit|approved\s+(program\s+)?facts|sme[- ]supplied|sme\s+additional|additional\s+resources|scope\b)", re.I)
KIT_LABEL = "Approved program facts (supplied by the SME)"


def is_kit_source(item: Any) -> bool:
    return isinstance(item, dict) and bool(_KIT_SOURCE.match(str(item.get("source") or item.get("title") or "")))


def kit_entry(item: Dict[str, Any]) -> Dict[str, Any]:
    return {"claim": str(item.get("claim") or "")[:200], "source": KIT_LABEL, "url": "", "url_kind": "kit", "verified": None}


def provided_urls(*texts: Optional[str]) -> List[str]:
    found: List[str] = []
    for t in texts:
        found.extend(u.rstrip(".,;") for u in _URL.findall(t or ""))
    return list(dict.fromkeys(found))


def build_sources(raw: Any, approved_texts: List[Optional[str]]) -> List[Dict[str, Any]]:
    """raw: the card's sources as the model wrote them. Returns [{claim, source, url, url_kind, verified}]."""
    approved = provided_urls(*approved_texts)
    out: List[Dict[str, Any]] = []
    seen = set()
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        if is_kit_source(item):
            entry = kit_entry(item)
            if ("kit", entry["claim"]) not in seen:
                seen.add(("kit", entry["claim"]))
                out.append(entry)
            continue
        name = str(item.get("source") or item.get("title") or "").strip()
        terms = str(item.get("search_terms") or item.get("claim") or "").strip()
        if not name and not terms:
            continue
        url = str(item.get("url") or "").strip().rstrip(".,;")
        if url and url in approved:
            kind = "provided"
        else:
            url = "https://www.google.com/search?q=" + quote_plus(f"{name} {terms}".strip())
            kind = "search"
        if url in seen:
            continue
        seen.add(url)
        out.append({"claim": str(item.get("claim") or "")[:200], "source": name[:120], "url": url,
                    "url_kind": kind, "verified": kind == "provided"})
    return out[:MAX_SOURCES_PER_CARD]


# ---- links written in a card's text ------------------------------------------------------------------------------------------------

BODY_LINK = re.compile(r"\[([^\]\n]{1,120})\]\((https?://[^)\s`]+)\)|(https?://[^\s<>)\]\"'`]+)")


def norm_url(url: str) -> str:
    """A URL in a form that two spellings of the same page share: no fragment, no trailing slash, lower-case scheme and host."""
    u = (url or "").strip().split("#", 1)[0]
    m = re.match(r"^(https?)://([^/?]+)(.*)$", u, re.I)
    return f"{m.group(1).lower()}://{m.group(2).lower()}{m.group(3).rstrip('/')}" if m else u


def host_of(url: str) -> str:
    m = re.match(r"^https?://([^/?#:]+)", url or "", re.I)
    return re.sub(r"^www\.", "", m.group(1).lower()) if m else ""


def body_links(text: Optional[str]) -> List[Tuple[str, str]]:
    """[(link text or '', url)] for every http(s) link in a card's text, in order, without repeats. Both [text](url) and bare URLs."""
    out: List[Tuple[str, str]] = []
    seen = set()
    for m in BODY_LINK.finditer(text or ""):
        label, url = (m.group(1) or "").strip(), (m.group(2) or m.group(3) or "").rstrip(".,;:!?")
        if url and norm_url(url) not in seen:
            seen.add(norm_url(url))
            out.append((label, url))
    return out


def add_link_sources(body: Optional[str], sources: Optional[List[Dict[str, Any]]], approved_urls: List[str]) -> Optional[List[Dict[str, Any]]]:
    """The card's sources plus one entry for every link its text gives that the sources do not already hold, so each link shows its source.
    A link the SME supplied is labelled as theirs; any other link is the AI's and is marked 'not checked'."""
    have = {norm_url(s.get("url") or "") for s in (sources or []) if isinstance(s, dict)}
    approved = {norm_url(u) for u in approved_urls}
    out = list(sources or [])
    for label, url in body_links(body):
        if norm_url(url) in have:
            continue
        mine = norm_url(url) in approved
        out.append({"claim": "Link given in the card", "source": label or host_of(url) or "Link", "url": url,
                    "url_kind": "provided" if mine else "link", "verified": False,
                    "note": "Provided by the SME." if mine else "Written by the AI. Open it and check it before you rely on it."})
    return out or (sources if sources is not None else None)
