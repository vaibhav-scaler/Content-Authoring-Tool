"""
Free pictures from Wikimedia Commons for image visuals.

What this does, in order:
  1. asks Commons for about 10 candidates for the card's image description
  2. drops the ones that cannot be used: licences that forbid commercial use or changes, no licence at all, logos, stock-photo
     sites, files that are too small, and any address the SME already rejected
  3. picks the best of what is left: the model chooses from titles and descriptions (or "none fits"); with no model available a
     plain word-match is used and it must clear a minimum
  4. downloads the file (Wikimedia hosts only, image types only, size capped) and saves it under backend/generated/, so a moved or
     deleted Commons file cannot break a published script
  5. returns the asset with the licence, the author and the Commons page, written into the caption

What it cannot do: look at the pixels. A logo or watermark burned into a file is not detected, so every web image is marked
needs_review and the SME confirms it. "Different image" in the visual panel rejects this one and finds the next.
"""

import html
import logging
import re
import time
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests

from agents.image_gen import save_image
from agents.llm import LLMUnavailable, call_llm_chat, llm_configured, parse_json_reply

log = logging.getLogger("web_images")

API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "ScalerScriptGenerator/1.0 (https://www.scaler.com; educational lecture-script tool) python-requests"
ALLOWED_HOSTS = ("upload.wikimedia.org", "thumb.wikimedia.org")
ALLOWED_MIME = ("image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml")
MAX_BYTES = 6 * 1024 * 1024
MIN_WIDTH = 400
CANDIDATES = 10
THUMB_WIDTH = 1000
GENERIC_WORDS = {"diagram", "flowchart", "image", "visual", "picture", "illustration", "the", "a", "an", "of", "for", "and", "to", "in", "how", "what"}
JUNK = re.compile(r"\b(logo|watermark|watermarked|sample|stock|shutterstock|alamy|getty|istock|dreamstime|screenshot of|advert|poster)\b", re.I)
MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF8", b"RIFF", b"<svg", b"<?xml")


def _plain(text: Any) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", str(text or ""))).replace("\n", " ").strip()


def licence_usable(name: str) -> bool:
    """Public domain, CC0, CC BY and CC BY-SA can be used with credit. Anything forbidding commercial use or changes, or unnamed, cannot."""
    low = (name or "").lower().strip()
    if not low:
        return False
    if re.search(r"\b(nc|nd)\b|non-?commercial|no-?deriv", low):
        return False
    return bool(re.search(r"public domain|\bpd\b|cc0|cc[- ]by|attribution", low))


def _meta(info: Dict[str, Any], key: str) -> str:
    return _plain((info.get("extmetadata") or {}).get(key, {}).get("value"))


def candidates(query: str, exclude_urls: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Usable Commons files for the query, best-ranked by Commons first. Empty on any problem."""
    words = [w for w in re.findall(r"[A-Za-z0-9+#.-]+", query or "") if w.lower() not in GENERIC_WORDS][:5]
    if not words:
        return []
    params = {
        "action": "query", "generator": "search", "gsrsearch": f"filetype:bitmap|drawing {' '.join(words)}",
        "gsrnamespace": "6", "gsrlimit": str(CANDIDATES), "prop": "imageinfo",
        "iiprop": "url|mime|size|extmetadata", "iiurlwidth": str(THUMB_WIDTH), "format": "json",
    }
    try:
        r = requests.get(API, params=params, headers={"User-Agent": USER_AGENT}, timeout=8)
        r.raise_for_status()
        pages = (r.json().get("query") or {}).get("pages") or {}
    except (requests.RequestException, ValueError) as e:
        log.warning("Commons search failed: %s", e)
        return []
    skip = set(exclude_urls or [])
    out = []
    for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
        info = (page.get("imageinfo") or [{}])[0]
        page_url = info.get("descriptionurl") or ""
        file_url = info.get("thumburl") or info.get("url") or ""
        title = _plain(page.get("title", "")).replace("File:", "")
        description = _meta(info, "ImageDescription")
        licence = _meta(info, "LicenseShortName")
        if not file_url or page_url in skip or file_url in skip:
            continue
        if info.get("mime") not in ALLOWED_MIME or int(info.get("width") or 0) < MIN_WIDTH:
            continue
        if not licence_usable(licence) or JUNK.search(f"{title} {description}"):
            continue
        out.append({"title": title, "description": description[:300], "licence": licence, "author": _meta(info, "Artist")[:120] or "unknown author",
                    "file_url": file_url, "page_url": page_url, "mime": info.get("mime")})
    return out


def _overlap(query: str, c: Dict[str, Any]) -> int:
    q = {w.lower() for w in re.findall(r"[A-Za-z0-9+#]+", query) if w.lower() not in GENERIC_WORDS and len(w) > 2}
    text = f"{c['title']} {c['description']}".lower()
    return sum(1 for w in q if w in text)


def choose(query: str, card_text: str, cands: List[Dict[str, Any]], script_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The candidate that fits the lesson, or None when none does."""
    if not cands:
        return None
    if llm_configured("anthropic"):
        listing = "\n".join(f"{i}. {c['title']} | {c['description'][:160]}" for i, c in enumerate(cands))
        prompt = (f"A lecture card needs ONE picture.\nWHAT THE PICTURE MUST SHOW: {query}\nTHE CARD SAYS: {(card_text or '')[:500]}\n\n"
                  f"CANDIDATE FILES (title | description):\n{listing}\n\n"
                  'Pick the one that really shows what is asked, for a learner, with no unrelated subject. If none does, pick none. '
                  'Reply ONLY with JSON: {"pick": <number or null>}')
        try:
            raw = call_llm_chat(messages=[{"role": "user", "content": prompt}], max_tokens=60, temperature=0.0, agent="image_pick", script_id=script_id)
            pick = parse_json_reply(raw).get("pick")
            if pick is None:
                return None
            return cands[int(pick)] if 0 <= int(pick) < len(cands) else None
        except LLMUnavailable as e:
            log.info("Image pick by the model not available (%s); using word match.", e)
        except (ValueError, TypeError, AttributeError):
            return None
    best = max(cands, key=lambda c: _overlap(query, c))
    return best if _overlap(query, best) >= 2 else None


def download(url: str) -> bytes:
    """The file bytes, or ValueError. Wikimedia hosts only, image types only, size capped, and the bytes must look like an image."""
    if urlparse(url).scheme != "https" or urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ValueError("not a Wikimedia upload address")
    data = b""
    for attempt in (1, 2):
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=15, stream=True, allow_redirects=False) as r:
            if r.status_code == 429 and attempt == 1:             # Wikimedia asks clients to slow down: wait what it says (at most 5s), once
                try:
                    wait = min(5.0, float(r.headers.get("Retry-After", 2)))
                except (TypeError, ValueError):
                    wait = 2.0
                time.sleep(wait)
                continue
            if r.status_code != 200:
                raise ValueError(f"download answered {r.status_code}")
            if not (r.headers.get("Content-Type") or "").split(";")[0].strip().lower() in ALLOWED_MIME:
                raise ValueError("the file is not an image")
            data = b""
            for chunk in r.iter_content(65536):
                data += chunk
                if len(data) > MAX_BYTES:
                    raise ValueError("the image is too large")
            break
    if not data.lstrip()[:8].startswith(MAGIC):
        raise ValueError("the bytes do not look like an image")
    return data


def find_image(spec: Dict[str, Any], card_text: str = "", script_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """The asset for an image visual from the web, or None (the caller then tries Gemini, then the placeholder)."""
    query = spec.get("intent") or spec.get("title") or ""
    cands = candidates(query, spec.get("rejected_images"))
    pick = choose(query, card_text, cands, script_id=script_id)
    if pick is None:
        return None
    try:
        data = download(pick["file_url"])
    except (ValueError, requests.RequestException) as e:
        log.warning("Could not download %s: %s", pick["file_url"], e)
        return None
    url = save_image(data, pick["mime"], str(spec.get("id") or "image"))
    credit = f"{pick['author']}, {pick['licence']}, via Wikimedia Commons"
    return {
        "id": spec.get("id", "img_01"), "type": "image", "content": url, "alt_text": spec.get("intent") or pick["title"],
        "caption": f"Figure: {spec.get('title') or spec.get('intent') or pick['title']} ({credit})",
        "licence": pick["licence"], "author": pick["author"], "source": "web_search", "source_url": pick["page_url"], "generated": False,
        "needs_review": True,
        "note": "Web picture. Check it fits the lesson and carries no watermark or logo before approving.",
    }
