"""
Real sources for the claims a card makes.

For every source a card cites, this module finds the page on the internet, fetches it, and checks that the page really
says what the claim says. Only then is the source marked verified.

How a source is resolved (agents.source_fetcher.resolve_sources):
  1. candidate pages: a URL the writer named, URLs the SME supplied, then official-documentation search results
     (only pages on a trusted documentation domain, or on a domain the SME supplied)
  2. each candidate is fetched (public addresses only, size and time limited, cached for a week)
  3. the page text is searched for the claim's key terms; enough of them together means "verified", with the sentence
     that supports the claim kept as evidence
  4. if nothing verifies, the source stays a search link, clearly marked as unverified, and says why

The same fetcher grounds the Writer: grounding_excerpts() returns the passages of the approved pages that match a card,
so the claims are written from the documentation and not from memory.

Settings (backend/.env):
  SOURCE_FETCH           on | off. Off skips every network call and leaves search links (default on)
  SOURCE_CACHE_DAYS      how long a fetched page is reused (default 7)
  SOURCE_FETCH_TIMEOUT   seconds for one page (default 12)
"""

import hashlib
import ipaddress
import json
import os
import re
import socket
import time
from concurrent.futures import ThreadPoolExecutor
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, quote_plus, urljoin, urlparse

import requests

from agents.rule_checker import words
from agents.sources import build_sources, provided_urls

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cache", "sources")
MAX_BYTES = 1_500_000
MAX_REDIRECTS = 5
# Some documentation sites refuse unfamiliar crawler user agents (dev.mysql.com answers 403 to them) but accept the plain
# requests default, so pages are fetched without a custom agent unless SOURCE_FETCH_USER_AGENT says otherwise.
SEARCH_USER_AGENT = "ScalerScriptStudio/1.0 (documentation checker)"
ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
SEARCH_URL = "https://html.duckduckgo.com/html/"

# Documentation sites we trust as "official" when a search result points at them.
TRUSTED_DOMAINS = (
    "docs.python.org", "python.org", "peps.python.org", "pypi.org", "numpy.org", "pandas.pydata.org", "matplotlib.org",
    "scikit-learn.org", "scipy.org", "seaborn.pydata.org", "pytorch.org", "tensorflow.org", "keras.io", "huggingface.co",
    "dev.mysql.com", "mysql.com", "postgresql.org", "sqlite.org", "mongodb.com", "redis.io", "duckdb.org",
    "docs.oracle.com", "oracle.com", "openjdk.org", "developer.mozilla.org", "nodejs.org", "typescriptlang.org",
    "learn.microsoft.com", "docs.microsoft.com", "cloud.google.com", "developers.google.com", "ai.google.dev",
    "docs.aws.amazon.com", "aws.amazon.com", "kubernetes.io", "docs.docker.com", "docker.com", "terraform.io",
    "developer.hashicorp.com", "docs.github.com", "git-scm.com", "prometheus.io", "grafana.com", "nginx.org",
    "apache.org", "spark.apache.org", "docs.anthropic.com", "anthropic.com", "platform.openai.com", "w3.org",
    "rfc-editor.org", "ietf.org", "gnu.org", "linux.org", "kernel.org", "docs.snowflake.com", "cloud.google.com",
)


# ---- safety: public pages only -----------------------------------------------------------------------------------

def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_public_url(url: str) -> bool:
    """http(s) only, and the host must resolve to public addresses. Blocks localhost, private networks, link-local
    (cloud metadata) and anything else internal, so a source link cannot be used to reach our own network."""
    parsed = urlparse(url or "")
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname
    try:
        infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


def host_trusted(host: str, extra_hosts: Tuple[str, ...] = ()) -> bool:
    host = host.lower()
    return any(host == d or host.endswith("." + d) for d in TRUSTED_DOMAINS + tuple(extra_hosts))


# ---- fetching and reading a page ---------------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "nav", "footer", "aside", "form", "button"}

    def __init__(self):
        super().__init__()
        self.title, self._in_title, self._skip, self.parts = "", False, 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag in self.SKIP:
            self._skip += 1
        elif tag in ("p", "li", "br", "tr", "h1", "h2", "h3", "h4", "pre", "div", "dt", "dd"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data):
        if self._in_title:
            self.title += data
        elif not self._skip:
            self.parts.append(re.sub(r"\s+", " ", data))      # line breaks inside a paragraph are not paragraph breaks


def html_to_text(page: str) -> Tuple[str, str]:
    """(title, readable text) of an HTML page."""
    parser = _TextExtractor()
    try:
        parser.feed(page)
    except Exception:
        pass
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return " ".join(parser.title.split()), text


def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")


def _cache_days() -> float:
    try:
        return float(os.getenv("SOURCE_CACHE_DAYS", "7"))
    except ValueError:
        return 7.0


def fetch_page(url: str, timeout: Optional[float] = None) -> Dict[str, Any]:
    """Fetch one public page. Returns {ok, url, final_url, status, title, text, fetched_at, from_cache, error}.
    Never raises: a failure comes back as ok=False with a reason."""
    result: Dict[str, Any] = {"ok": False, "url": url, "final_url": url, "status": None, "title": "", "text": "",
                              "fetched_at": time.time(), "from_cache": False, "error": None}
    path = _cache_path(url)
    try:
        with open(path, encoding="utf-8") as f:
            cached = json.load(f)
        if time.time() - cached.get("fetched_at", 0) < _cache_days() * 86400 and cached.get("ok"):
            cached["from_cache"] = True
            return cached
    except (OSError, ValueError):
        pass
    if os.getenv("SOURCE_FETCH", "on").lower() == "off":
        result["error"] = "Fetching is switched off (SOURCE_FETCH=off)."
        return result
    try:
        limit = timeout or float(os.getenv("SOURCE_FETCH_TIMEOUT", "12"))
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if not is_public_url(current):
                result["error"] = "The address is not a public web page."
                return result
            headers = {"Accept": "text/html,text/plain;q=0.9"}
            if os.getenv("SOURCE_FETCH_USER_AGENT"):
                headers["User-Agent"] = os.getenv("SOURCE_FETCH_USER_AGENT")
            resp = requests.get(current, headers=headers, timeout=limit, stream=True, allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                current = urljoin(current, resp.headers["Location"])
                resp.close()
                continue
            break
        else:
            result["error"] = "Too many redirects."
            return result
        result["status"], result["final_url"] = resp.status_code, current
        if resp.status_code != 200:
            result["error"] = f"The page answered HTTP {resp.status_code}."
            resp.close()
            return result
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype and ctype not in ALLOWED_TYPES:
            result["error"] = f"Not a readable page ({ctype})."
            resp.close()
            return result
        body = b""
        for chunk in resp.iter_content(65536):
            body += chunk
            if len(body) > MAX_BYTES:
                break
        resp.close()
        charset = re.search(r"charset=([\w-]+)", resp.headers.get("Content-Type") or "", re.I)
        raw = body.decode(charset.group(1) if charset else "utf-8", errors="replace")      # pages without a charset are UTF-8 in practice
        title, text = (html_to_text(raw) if ctype != "text/plain" else ("", raw))
        result.update(ok=True, title=title, text=text[:400_000])
        try:
            os.makedirs(CACHE_DIR, exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(result, f)
        except OSError:
            pass
    except requests.RequestException as e:
        result["error"] = f"Could not reach the page ({type(e).__name__})."
    return result


# ---- does the page say it? ----------------------------------------------------------------------------------------

def evidence_for(text: str, claim_terms: str) -> Tuple[float, str]:
    """(score 0..1, the supporting passage). The page counts as supporting the claim only when the claim's key terms appear
    TOGETHER in one short passage (a line or sentence and the one or two after it), not scattered over the whole page.
    The score is the share of terms found in the best passage, and the sentence that holds most of them is returned."""
    need = words(claim_terms)
    if not need or not text:
        return 0.0, ""
    units = [u.strip() for u in re.split(r"\n+", text) if u.strip()]
    pieces: List[str] = []
    for u in units:                                   # long lines are cut into sentences so a window stays short
        pieces.extend(x for x in re.split(r"(?<=[.!?])\s+", u) if x.strip()) if len(u) > 300 else pieces.append(u)
    best, best_sentence = (0.0, 0.0), ""
    for i in range(len(pieces)):
        for size in (1, 2):                          # one piece, or a piece with the one after it
            window = pieces[i:i + size]
            joined = " ".join(window)
            if len(joined) > 520:
                continue
            found = need & words(joined)
            score = len(found) / len(need)
            density = len(found) / max(1, len(words(joined)))   # on equal scores, the tighter passage wins
            if (score, density) > best:
                best = (score, density)
                candidates = [(len(need & words(p)), p) for p in window if 30 <= len(p) <= 600 and len(p.split()) >= 5]
                best_sentence = max(candidates, key=lambda t: t[0])[1] if candidates else max(window, key=len)
    best_score = best[0]
    return best_score, best_sentence.strip()[:260]


def verify_source(url: str, claim: str, search_terms: str) -> Dict[str, Any]:
    """Fetch the page and check it supports the claim."""
    page = fetch_page(url)
    if not page["ok"]:
        return {"verified": False, "fetched": False, "note": page["error"], "final_url": url}
    terms = f"{search_terms} {claim}"
    score, snippet = evidence_for(page["text"], terms)
    n_terms = len(words(terms))
    enough = score >= 0.6 and (n_terms < 2 or round(score * n_terms) >= 2)
    return {"verified": bool(enough), "fetched": True, "page_title": page["title"], "final_url": page["final_url"],
            "evidence": snippet if enough else "", "score": round(score, 2), "checked_at": time.strftime("%Y-%m-%d"),
            "note": None if enough else "The page was found but it does not clearly contain the claim."}


# ---- finding candidate pages --------------------------------------------------------------------------------------

def search_official(query: str, extra_hosts: Tuple[str, ...] = (), limit: int = 4) -> List[str]:
    """Official-documentation pages for a query, from a web search, keeping only trusted domains."""
    try:
        resp = requests.post(SEARCH_URL, data={"q": query}, headers={"User-Agent": SEARCH_USER_AGENT}, timeout=12)
        if resp.status_code != 200:
            return []
    except requests.RequestException:
        return []
    found: List[str] = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', resp.text):
        target = parse_qs(urlparse(href).query).get("uddg", [href])[0]
        if target.startswith("//"):
            target = "https:" + target
        if target.startswith("http") and host_trusted(_host_of(target), extra_hosts) and target not in found:
            found.append(target)
        if len(found) >= limit:
            break
    return found


# ---- resolving a card's sources -----------------------------------------------------------------------------------

def _resolve_one(item: Dict[str, Any], sme_urls: List[str], extra_hosts: Tuple[str, ...], search: bool) -> Dict[str, Any]:
    claim = str(item.get("claim") or "")
    name = str(item.get("source") or item.get("title") or "")
    terms = str(item.get("search_terms") or claim)
    candidates: List[Tuple[str, str]] = []
    named = str(item.get("url") or "").strip().rstrip(".,;")
    if named.startswith("http"):
        candidates.append((named, "provided" if named in sme_urls else "fetched"))
    for u in sme_urls:
        if u not in [c[0] for c in candidates]:
            candidates.append((u, "provided"))
    unconfirmed: Optional[Dict[str, Any]] = None
    tried = 0
    pending = list(candidates)
    searched = False
    while pending or (search and not searched):
        if not pending:
            searched = True
            pending = [(u, "fetched") for u in search_official(f"{name} {terms} official documentation", extra_hosts)
                       if u not in [c[0] for c in candidates]]
            candidates += pending
            if not pending:
                break
        url, kind = pending.pop(0)
        tried += 1
        if tried > 5:
            break
        check = verify_source(url, claim, terms)
        if check["verified"]:
            return {"claim": claim[:200], "source": name[:120], "url": check["final_url"], "url_kind": kind, "verified": True,
                    "page_title": check.get("page_title"), "evidence": check.get("evidence"), "checked_at": check.get("checked_at")}
        if check.get("fetched") and unconfirmed is None:
            unconfirmed = {"claim": claim[:200], "source": name[:120], "url": check["final_url"], "url_kind": kind, "verified": False,
                           "page_title": check.get("page_title"), "note": check.get("note")}
    if unconfirmed:
        return unconfirmed
    fallback = build_sources([item], [])[0] if build_sources([item], []) else {"claim": claim[:200], "source": name[:120], "url": "#", "url_kind": "search", "verified": False}
    fallback["note"] = "No official page could be fetched and checked for this claim."
    return fallback


def resolve_sources(raw: Any, approved_texts: List[Optional[str]], search: bool = True, deadline_seconds: float = 40.0) -> List[Dict[str, Any]]:
    """The card's sources, each resolved to a real page and checked against the claim where possible.
    Runs the sources in parallel and gives up on slow ones, falling back to a labelled search link."""
    items = [i for i in (raw if isinstance(raw, list) else []) if isinstance(i, dict) and (i.get("source") or i.get("title") or i.get("search_terms") or i.get("claim"))][:6]
    if not items:
        return []
    from agents.sources import is_kit_source, kit_entry
    kit_items = [kit_entry(i) for i in items if is_kit_source(i)]        # the SME's own facts: never fetched, never searched
    items = [i for i in items if not is_kit_source(i)]
    if not items:
        return list({e["claim"]: e for e in kit_items}.values())
    sme_urls = provided_urls(*approved_texts)
    extra_hosts = tuple({_host_of(u) for u in sme_urls if _host_of(u)})
    if os.getenv("SOURCE_FETCH", "on").lower() == "off":
        return build_sources(items, approved_texts)
    results: List[Optional[Dict[str, Any]]] = [None] * len(items)
    with ThreadPoolExecutor(max_workers=min(5, len(items))) as pool:
        futures = [pool.submit(_resolve_one, it, sme_urls, extra_hosts, search) for it in items]
        end = time.time() + deadline_seconds
        for i, fut in enumerate(futures):
            try:
                results[i] = fut.result(timeout=max(0.1, end - time.time()))
            except Exception:
                results[i] = None
    out, seen = [], set()
    for item, res in zip(items, results):
        res = res or (build_sources([item], approved_texts) or [None])[0]
        key = (res["url"], res.get("claim")) if res else None       # two claims on one page are two sources
        if res and key not in seen:
            seen.add(key)
            out.append(res)
    out.extend(list({e["claim"]: e for e in kit_items}.values()))
    return out


# ---- grounding the Writer -----------------------------------------------------------------------------------------

def grounding_excerpts(urls: List[str], focus: str, max_chars: int = 2600, per_page: int = 3) -> str:
    """Sentences of the approved pages that match the card being written, so the card can be written from them.
    Empty when there are no approved URLs or nothing matches."""
    if not urls or os.getenv("SOURCE_FETCH", "on").lower() == "off":
        return ""
    need = words(focus)
    if not need:
        return ""
    chunks: List[str] = []
    for url in urls[:5]:
        page = fetch_page(url)
        if not page["ok"]:
            continue
        sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", page["text"])) if 50 <= len(x.strip()) <= 500]
        scored = [(len(need & words(x)), i, x) for i, x in enumerate(sentences)]
        best = sorted([t for t in scored if t[0] >= 2], key=lambda t: -t[0])[:per_page]
        for _, _, sentence in sorted(best, key=lambda t: t[1]):
            chunks.append(f"[{page['title'] or url}] {sentence}")
    return "\n".join(chunks)[:max_chars]
