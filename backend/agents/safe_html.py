"""
Safe rendering of everything a model writes.

Model output is untrusted. Card text, titles, code, quiz options, source names and the HTML of animations all pass through
here before they reach a page, in the app and on the public share page.

  esc()               plain text -> HTML-safe text
  format_spoken()     spoken script text -> safe HTML (bold, italics, inline code, line breaks, [[VISUAL]] markers)
  format_card_text()  a whole card (headings, lists, callouts, cues, paragraphs) -> safe HTML, so no raw '#', '-', '>' or '**' shows
  safe_url()          only http(s) links and our own /generated/ files; anything else becomes "#"
  animation_document() the animation as a complete page that runs only inside a sandboxed iframe, with a Content Security
                      Policy that blocks every network request, so it cannot read the app or send anything out
  visual_html()       the embed for one visual (diagram, animation or picture), safe by construction
"""

import html
import re
from typing import Optional
from urllib.parse import quote

# What an animation may do: run its own inline script and style, and show data: images and fonts. Nothing else, and in
# particular no network (connect-src, img-src and every other source are closed), no forms, no <base> tricks.
ANIMATION_CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data: blob:; "
                 "font-src data:; media-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'")

# Tells the page that holds the iframe how tall the animation is, so it is shown in full and not cut off.
HEIGHT_REPORTER = """<script>(function(){function r(){try{var h=Math.ceil(document.body.getBoundingClientRect().height);
parent.postMessage({type:'scaler-animation-height',height:h},'*');}catch(e){}}
window.addEventListener('load',r);window.addEventListener('resize',r);
if(window.ResizeObserver){new ResizeObserver(r).observe(document.body);}
setInterval(r,1000);r();})();</script>"""

# Defaults only: anything the animation styles itself wins. Light by default, dark when the viewer's system is dark, so
# text is always readable even in an animation that sets no colours.
BASE_STYLE = ("<style>:root{color-scheme:light dark;}html,body{margin:0;padding:0;background:#ffffff;color:#0f172a;"
              "font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;}*{box-sizing:border-box;}body{padding:12px;}"
              "@media (prefers-color-scheme: dark){html,body{background:#0f172a;color:#f8fafc;}}"
              # an animation that sizes itself to the window (min-height:100vh) would make its frame grow without end, so the height comes from its content
              "html,body{min-height:0!important;height:auto!important;}body{zoom:1.3;}</style>")        # shown larger: the model writes animations small


def esc(value) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def safe_url(url: Optional[str]) -> str:
    """http(s) links and paths under /generated/ only. javascript:, data:, protocol-relative and the rest become '#'."""
    u = (url or "").strip()
    if re.match(r"^https?://", u, re.I):
        return u
    if u.startswith("/generated/") and ".." not in u:
        return u
    return "#"


def format_spoken(text: Optional[str]) -> str:
    """Spoken script text as safe HTML. Everything is escaped first; only the markup we choose to allow is added back."""
    out = esc(text)
    out = re.sub(r"\[\[VISUAL:\s*([^\]|]+?)\s*(?:\|[^\]]*)?\]\]", "", out)         # the visual is shown in its place; the marker is only for the writer
    out = re.sub(r"\*\*([^*\n]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<em>\1</em>", out)
    out = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", out)
    return out.replace("\n", "<br>")


def animation_document(content: Optional[str]) -> str:
    """The animation as one complete HTML page for an <iframe sandbox="allow-scripts" srcdoc=...>.
    A Content Security Policy goes first in the document, any CSP or external script the model wrote is removed, and a
    small script reports the page height to the parent."""
    body = content or ""
    body = re.sub(r"<meta[^>]+http-equiv=[\"']?content-security-policy[^>]*>", "", body, flags=re.I)
    body = re.sub(r"<script\b[^>]*\bsrc\s*=[^>]*>\s*</script>", "<!-- external script removed -->", body, flags=re.I)
    body = re.sub(r"<link\b[^>]*\bhref\s*=\s*[\"']?https?:[^>]*>", "<!-- external stylesheet removed -->", body, flags=re.I)
    csp = f'<meta http-equiv="Content-Security-Policy" content="{ANIMATION_CSP}">'
    meta = '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
    if re.search(r"<head[^>]*>", body, re.I):
        body = re.sub(r"(<head[^>]*>)", r"\1" + (csp + meta + BASE_STYLE).replace("\\", "\\\\"), body, count=1, flags=re.I)
        if re.search(r"</body>", body, re.I):
            return re.sub(r"</body>", HEIGHT_REPORTER.replace("\\", "\\\\") + "</body>", body, count=1, flags=re.I)
        return body + HEIGHT_REPORTER
    # a fragment, or a document with no <head>: strip an outer <html>/<body> wrapper and build the page ourselves
    inner = re.sub(r"</?(?:!doctype[^>]*|html[^>]*|body[^>]*)>", "", body, flags=re.I)
    return f"<!doctype html><html><head>{csp}{meta}{BASE_STYLE}</head><body>{inner}{HEIGHT_REPORTER}</body></html>"


def animation_is_complete(content: Optional[str]) -> bool:
    """False for an animation the model did not finish writing (its answer hit the length limit): a page with no body, an open <style> or
    <script>, no closing tag. Such a page shows nothing, so it is never shown as if it were an animation."""
    c = content or ""
    low = c.lower()
    if len(c.strip()) < 40:
        return False
    for tag in ("style", "script"):
        if low.count(f"<{tag}") != low.count(f"</{tag}>"):
            return False
    if "<html" in low or "<!doctype" in low:
        if "<body" not in low or ("</body>" not in low and "</html>" not in low):
            return False
        body = low.split("<body", 1)[1]
    else:
        body = low
    shown = re.sub(r"<(style|script)\b.*?</\1>", "", body, flags=re.S)
    return len(re.findall(r"<(?:div|svg|section|p|span|button|canvas|ul|ol|table|h[1-6]|g|rect|circle)\b", shown)) >= 3


def step_player_html(title: str, steps) -> str:
    """A walkthrough that always works: the whole flow is drawn as numbered steps joined by arrows, the current step is lit and named ("Step 2 of 5: ..."),
    the steps already done are ticked, and Back, Play or Pause, Next and Replay move through it, in light or dark mode. Used when the model's own animation
    could not be completed. Every value is escaped, and the script reads the steps from the page, never from a string."""
    labels = [str(x)[:70] for x in (steps or []) if str(x).strip()][:7] or ["Step one", "Step two", "Step three"]
    n = len(labels)
    nodes = "".join(f'<li class="s" data-i="{i}"><span class="num">{i + 1}</span><span class="lab">{esc(label)}</span></li>' for i, label in enumerate(labels))
    return ('<!doctype html><html><head><meta charset="utf-8"><style>'
            ':root{--bg:#fff;--fg:#0f172a;--mut:#64748b;--box:#f1f5f9;--line:#cbd5e1;--on:#2563eb;--onfg:#fff;--done:#16a34a}'
            '@media(prefers-color-scheme:dark){:root{--bg:#0f172a;--fg:#f8fafc;--mut:#94a3b8;--box:#1e293b;--line:#334155;--on:#38bdf8;--onfg:#0f172a;--done:#4ade80}}'
            'body{margin:0;padding:18px;background:var(--bg);color:var(--fg);font-family:system-ui,sans-serif}h4{margin:0 0 14px;font-size:1.05rem}'
            'ol{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px 0;align-items:stretch}'
            '.s{display:flex;align-items:center;gap:8px;flex:1 1 130px;min-width:120px;padding:10px 12px;margin-right:22px;border-radius:10px;background:var(--box);opacity:.5;'
            'position:relative;transition:all .35s;border:2px solid transparent}'
            '.s:not(:last-child):after{content:"\\2192";position:absolute;right:-20px;top:50%;transform:translateY(-50%);color:var(--mut);font-size:1.1rem}'
            '.num{flex:none;width:26px;height:26px;border-radius:50%;background:var(--line);display:flex;align-items:center;justify-content:center;font-weight:700;font-size:.85rem}'
            '.lab{font-size:.92rem;line-height:1.3}.s.done{opacity:.9}.s.done .num{background:var(--done);color:#fff}'
            '.s.on{opacity:1;border-color:var(--on);background:var(--box);transform:translateY(-2px)}.s.on .num{background:var(--on);color:var(--onfg)}'
            '#now{margin:18px 0 6px;padding:14px 16px;border-radius:12px;background:var(--box);border-left:5px solid var(--on)}'
            '#now small{display:block;color:var(--mut);font-weight:600;letter-spacing:.4px;text-transform:uppercase;font-size:.72rem;margin-bottom:4px}'
            '#now strong{font-size:1.15rem}.bar{height:5px;border-radius:3px;background:var(--line);margin-top:12px;overflow:hidden}.bar i{display:block;height:100%;width:0;background:var(--on);transition:width .35s}'
            'button{margin:12px 8px 0 0;padding:7px 14px;border:0;border-radius:8px;background:var(--on);color:var(--onfg);font-weight:600;cursor:pointer}button.alt{background:var(--box);color:var(--fg);border:1px solid var(--line)}'
            'button:disabled{opacity:.4;cursor:default}'
            f'</style></head><body><h4>{esc(title)}</h4><ol id="flow">{nodes}</ol>'
            '<div id="now"><small id="count"></small><strong id="name"></strong><div class="bar"><i id="fill"></i></div></div>'
            '<div><button class="alt" id="back">Back</button><button id="play">Pause</button><button class="alt" id="next">Next</button><button class="alt" id="again">Replay</button></div>'
            '<script>var items=document.querySelectorAll(".s"),n=items.length,k=0,t=null;'
            'function show(j){k=Math.max(0,Math.min(n-1,j));for(var q=0;q<n;q++)items[q].className="s"+(q<k?" done":q==k?" on":"");'
            'document.getElementById("count").textContent="Step "+(k+1)+" of "+n;document.getElementById("name").textContent=items[k].querySelector(".lab").textContent;'
            'document.getElementById("fill").style.width=((k+1)/n*100)+"%";document.getElementById("back").disabled=k==0;document.getElementById("next").disabled=k==n-1;}'
            'function stop(){clearInterval(t);t=null;document.getElementById("play").textContent="Play";}'
            'function play(){stop();document.getElementById("play").textContent="Pause";t=setInterval(function(){if(k>=n-1){stop();return;}show(k+1);},2600);}'
            'document.getElementById("play").onclick=function(){t?stop():(k>=n-1?(show(0),play()):play());};'
            'document.getElementById("next").onclick=function(){stop();show(k+1);};document.getElementById("back").onclick=function(){stop();show(k-1);};'
            'document.getElementById("again").onclick=function(){show(0);play();};show(0);play();</script></body></html>')


def animation_shows_steps(content: Optional[str]) -> bool:
    """True when an animation is a walkthrough: it names its steps (the word "step" with numbers) and lets the viewer move between them. A page that only
    moves shapes around does not tell a learner which step they are on or what it is."""
    low = (content or "").lower()
    return "step" in low and bool(re.search(r"\b(next|back|previous)\b", low)) and bool(re.search(r"\b(step\s*\d|step[ \"'+]*\(?\w*\s*\+?\s*1|of\s*\d)", low) or low.count("step") >= 4)


def svg_data_uri(svg: str) -> str:
    """An SVG as an <img> source. Inside <img> an SVG cannot run scripts or load anything."""
    return "data:image/svg+xml;charset=utf-8," + quote(svg or "")


def visual_html(visual_type: Optional[str], content: Optional[str], caption: Optional[str] = None, frame_id: str = "v") -> str:
    """The embed for one visual, for the public page. The app builds the same thing in its own JavaScript."""
    if not content:
        return ""
    cap = f'<div class="visual-caption">{esc(caption or "")}</div>' if caption else ""
    if visual_type == "mermaid":
        return f'<div class="visual-wrap"><div class="mermaid">{esc(content)}</div>{cap}</div>'
    if visual_type == "animation" and not animation_is_complete(content):
        return ('<div class="visual-wrap" style="padding:14px;border:1px dashed #94a3b8;border-radius:10px;color:#94a3b8;">'
                'This animation was cut off while it was being written, so it cannot play. It needs to be regenerated.</div>' + cap)
    if visual_type == "animation":
        return (f'<div class="visual-wrap animation-wrap"><iframe class="animation-frame" id="anim-{esc(frame_id)}" sandbox="allow-scripts" '
                f'referrerpolicy="no-referrer" loading="lazy" title="{esc(caption or "Animation")}" '
                f'style="width:100%;height:360px;border:0;border-radius:10px;" srcdoc="{esc(animation_document(content))}"></iframe>{cap}</div>')
    if visual_type == "image":
        src = svg_data_uri(content) if content.strip().startswith("<") else safe_url(content)
        return f'<div class="visual-wrap"><img src="{esc(src)}" alt="{esc(caption or "")}" style="max-width:100%;border-radius:8px;">{cap}</div>'
    return ""


# ---- a whole card as readable HTML ------------------------------------------------------------------------------------------------

_DIVIDER = re.compile(r"^\s*([-*_])\1{2,}\s*$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$")
_QUOTE = re.compile(r"^\s*>\s?(.*)$")
_BULLET = re.compile(r"^\s*[-*\u2022]\s+(.+)$")
_NUMBER = re.compile(r"^\s*\d{1,2}[.)]\s+(.+)$")
_CUE = re.compile(r"^\s*\[(?!\[)[^\[\]]{1,160}\]\s*$")
_CALLOUT = re.compile(r"^\*\*\s*([A-Za-z][A-Za-z -]{1,18}?)\s*:?\s*\*\*\s*:?\s*(.*)$", re.S)
_CALLOUT_KINDS = {"key": "key", "mistake": "mistake", "remember": "remember", "real-world": "realworld", "real world": "realworld", "predict": "predict"}


def _link_index(sources) -> dict:
    from agents.sources import norm_url
    return {norm_url(x.get("url") or ""): x for x in (sources or []) if isinstance(x, dict) and x.get("url")}


def _link_html(label: str, url: str, index: dict) -> str:
    """A clickable link. Only http(s) links reach here, and every part is escaped. Where the link comes from and whether it was checked is shown in the
    card's References list, not in the middle of what the instructor reads."""
    shown = label or (re.sub(r"^https?://(www\.)?", "", url).rstrip("/")[:55] + ("..." if len(re.sub(r"^https?://(www\.)?", "", url)) > 56 else ""))
    return f'<a class="card-link" href="{esc(url)}" target="_blank" rel="noopener noreferrer">{esc(shown)}</a>'


def _inline(text: str, index: Optional[dict] = None) -> str:
    """One line or block of text as safe HTML: links beside their source, bold, italics, inline code, and our own markers.
    No line breaks added."""
    from agents.sources import BODY_LINK
    holders: list = []

    def take(m):
        label, url = (m.group(1) or ""), (m.group(2) or m.group(3) or "")
        tail = ""
        if m.group(3):
            clean = url.rstrip(".,;:!?")
            tail, url = url[len(clean):], clean
        holders.append(_link_html(label, url, index or {}))
        return f"\x00{len(holders) - 1}\x00{tail}"
    out = format_spoken(BODY_LINK.sub(take, text or "")).replace("<br>", " ")
    out = re.sub("\x00(\\d+)\x00", lambda m: holders[int(m.group(1))], out)
    return re.sub(r"\[\[FROM_SME:?\s*([^\]]*)\]\]", lambda m: f'<mark class="sme-gap">needed from the SME: {m.group(1).strip() or "a fact"}</mark>', out)


def format_card_text(text: Optional[str], sources=None) -> str:
    """A card's text as safe HTML. The writer uses light markdown (sub-headings, bold, bullet and numbered lists, '> **Key:** ...'
    callouts, '---' dividers, [bracketed cues]); this turns it into real headings, lists and callout boxes so none of the symbols are
    shown. Links are clickable, each with its source beside it. Everything is escaped before any markup is added, and dividers are dropped."""
    index = _link_index(sources)
    inl = lambda t: _inline(t, index)           # every link in the text shows its source, taken from the card's sources
    html_out: list = []
    para: list = []
    quote: list = []
    items: list = []
    list_tag = [None]

    def close_para():
        if para:
            html_out.append('<p class="card-para">' + "<br>".join(inl(x) for x in para) + "</p>")
            para.clear()

    def close_list():
        if items:
            html_out.append(f"<{list_tag[0]} class=\"card-list\">" + "".join(f"<li>{inl(i)}</li>" for i in items) + f"</{list_tag[0]}>")
            items.clear()
        list_tag[0] = None

    def close_quote():
        if quote:
            body = " ".join(quote).strip()
            m = _CALLOUT.match(body)
            kind = _CALLOUT_KINDS.get(m.group(1).strip().lower()) if m else None
            if kind:
                html_out.append(f'<div class="card-callout callout-{kind}"><span class="callout-label">{esc(m.group(1).strip())}</span> {inl(m.group(2))}</div>')
            else:
                html_out.append(f'<div class="card-callout callout-note">{inl(body)}</div>')
            quote.clear()

    def close_all():
        close_para(); close_list(); close_quote()

    fence: list = []
    in_fence = False
    for line in (text or "").replace("\r\n", "\n").split("\n"):
        if re.match(r"^\s*```", line):                       # a fenced code block: shown as code, line breaks and indents kept
            if in_fence:
                html_out.append('<pre class="card-code"><code>' + esc("\n".join(fence)) + "</code></pre>")
                fence.clear(); in_fence = False
            else:
                close_all(); in_fence = True
            continue
        if in_fence:
            fence.append(line); continue
        if not line.strip() or _DIVIDER.match(line):
            close_all(); continue
        m = _HEADING.match(line)
        if m:
            close_all(); html_out.append(f'<h4 class="card-heading">{inl(m.group(1))}</h4>'); continue
        m = _QUOTE.match(line)
        if m:
            close_para(); close_list(); quote.append(m.group(1)); continue
        close_quote()
        m = _BULLET.match(line)
        if m:
            close_para()
            if list_tag[0] != "ul":
                close_list(); list_tag[0] = "ul"
            items.append(m.group(1)); continue
        m = _NUMBER.match(line)
        if m:
            close_para()
            if list_tag[0] != "ol":
                close_list(); list_tag[0] = "ol"
            items.append(m.group(1)); continue
        close_list()
        if _CUE.match(line):
            close_para(); html_out.append(f'<p class="stage-cue">{esc(line.strip())}</p>'); continue
        if re.match(r"^\s*\**reveal\**\s*:", line, re.I):
            close_para(); html_out.append(f'<p class="card-para reveal">{inl(line.strip())}</p>'); continue
        para.append(line.strip())
    if in_fence and fence:                                      # a fence that was never closed still shows its code
        html_out.append('<pre class="card-code"><code>' + esc("\n".join(fence)) + "</code></pre>")
    close_all()
    return "".join(html_out)
