"""
The setup checklist for a programming class: what the instructor and the learners must have working BEFORE the class starts.

Where each part comes from, so it can be trusted:
  * which tools the class needs      the code in its cards (languages, imports, "Platform:" lines), plus the SME's own notes
  * the version that was TESTED      the real version of the tool that ran the card's code on this server (not a guess)
  * extras (libraries, accounts...)  the AI, told exactly what the code uses and told to name nothing the class does not use
  * the source link of each tool     the official page; every link is fetched and the page is checked to be about that tool, and the result
                                     is shown ("checked" or "not checked"), never assumed

Shown at the top of the script (with the instructor's own pre-class checks), on the shared page, and in both Markdown exports.
"""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from agents.safe_html import esc, safe_url

# The official page for each language's tool, how to check it is installed, and how to put it on a machine. The AI may add to these, never remove them.
BASELINE: Dict[str, Dict[str, Any]] = {
    "python": {"tool": "Python", "purpose": "Runs the class code.", "who": "both",
               "install": ["Open the link below and download the latest Python 3 installer for your system (Windows, macOS or Linux).", "Run the installer. On Windows, tick \"Add Python to PATH\" on the first screen, then choose Install Now.", "On macOS, open the downloaded .pkg and click Continue until it finishes.", "Close every terminal window, then open a new one (Terminal on macOS, Command Prompt on Windows).", "Type the check command below and press Enter. If it prints a version, you are done."],
               "verify_command": "python3 --version", "expected_output": "Python 3.x.y (the version you installed)",
               "source_name": "Python downloads (python.org)", "source_url": "https://www.python.org/downloads/"},
    "javascript": {"tool": "Node.js", "purpose": "Runs the class JavaScript.", "who": "both",
                   "install": ["Open the link below and download the LTS installer for your system.", "Run the installer and keep the default options on every screen.", "Close every terminal window, then open a new one.", "Type the check command below and press Enter. If it prints a version, you are done."],
                   "verify_command": "node --version", "expected_output": "v followed by the version, for example v20.11.0",
                   "source_name": "Node.js downloads (nodejs.org)", "source_url": "https://nodejs.org/en/download"},
    "java": {"tool": "JDK (Java Development Kit)", "purpose": "Compiles and runs the class Java code.", "who": "both",
             "install": ["Open the link below and download the JDK installer for your system (pick version 17 or newer).", "Run the installer and keep the default options on every screen.", "Close every terminal window, then open a new one.", "Type the check command below and press Enter. If it prints a version, you are done."],
             "verify_command": "javac -version", "expected_output": "javac followed by the version, for example javac 17.0.9",
             "source_name": "Eclipse Temurin JDK (adoptium.net)", "source_url": "https://adoptium.net/temurin/releases/"},
    "sql": {"tool": "SQLite", "purpose": "Runs the class SQL on a small practice database that needs no server.", "who": "both",
            "install": ["Open the link below and download the command-line tools for your system, or use the SQLite that comes with Python.", "Unzip the download and put the sqlite3 program in a folder on your PATH.", "Close every terminal window, then open a new one.", "Type the check command below and press Enter. If it prints a version, you are done."],
            "verify_command": "sqlite3 --version", "expected_output": "A version number, for example 3.43.2",
            "source_name": "SQLite downloads (sqlite.org)", "source_url": "https://www.sqlite.org/download.html"},
}

# Python import name -> the name to pip install, for the libraries a class commonly imports that are not part of Python itself
PIP_NAMES = {"numpy": "numpy", "pandas": "pandas", "matplotlib": "matplotlib", "seaborn": "seaborn", "scipy": "scipy", "sklearn": "scikit-learn",
             "requests": "requests", "flask": "flask", "fastapi": "fastapi", "django": "django", "torch": "torch", "tensorflow": "tensorflow",
             "streamlit": "streamlit", "bs4": "beautifulsoup4", "PIL": "pillow", "cv2": "opencv-python", "sqlalchemy": "sqlalchemy", "pytest": "pytest",
             "plotly": "plotly", "openai": "openai", "anthropic": "anthropic", "langchain": "langchain", "dotenv": "python-dotenv", "yaml": "pyyaml"}

LANG_ORDER = ("python", "javascript", "java", "sql")

# Sites that really are the maker of a tool. A page on one of these that opens is labelled "official site" even when its text cannot be matched
# (download pages and sign-in pages are often built with scripts). It is never labelled "checked": only matching text earns that.
OFFICIAL_HOSTS = ("python.org", "pypi.org", "nodejs.org", "npmjs.com", "adoptium.net", "oracle.com", "openjdk.org", "sqlite.org", "postgresql.org", "mysql.com",
                  "mariadb.org", "git-scm.com", "github.com", "code.visualstudio.com", "jetbrains.com", "docker.com", "google.com", "cloud.google.com",
                  "colab.research.google.com", "mozilla.org", "microsoft.com", "anaconda.com", "jupyter.org", "pandas.pydata.org", "numpy.org", "scikit-learn.org",
                  "pytorch.org", "tensorflow.org", "kaggle.com", "apache.org", "gnu.org", "rust-lang.org", "go.dev", "ruby-lang.org", "php.net", "dotnet.microsoft.com")
SQL_ENGINES = re.compile(r"big\s*query|mysql|postgres|sql\s*server|oracle|snowflake|redshift|mariadb|duckdb|databricks|athena|clickhouse", re.I)


def _lang(card: Dict[str, Any]) -> str:
    from agents.code_runner import normalise_language
    return normalise_language(card.get("code_language"), card.get("code_snippet") or "")


def is_programming(cards: List[Dict[str, Any]]) -> bool:
    """A class is a programming class when any of its cards carries code."""
    return any((c.get("code_snippet") or "").strip() for c in cards)


def detect_stack(cards: List[Dict[str, Any]]) -> Dict[str, Any]:
    """What the class's own code says it uses: languages, platform notes, Python libraries, and the tool versions its code was really run with."""
    from agents.code_runner import runtime_version
    langs, platforms, imports = [], [], []
    for c in cards:
        code = c.get("code_snippet") or ""
        if not code.strip():
            continue
        lang = _lang(c)
        if lang and lang not in langs:
            langs.append(lang)
        for m in re.finditer(r"(?:--|#|//)\s*Platform:\s*([^\n]+)", code, re.I):
            note = m.group(1).strip()
            if note and note not in platforms:
                platforms.append(note[:200])
        if lang == "python":
            for m in re.finditer(r"^\s*(?:import|from)\s+([A-Za-z_][\w]*)", code, re.M):
                if m.group(1) in PIP_NAMES and PIP_NAMES[m.group(1)] not in imports:
                    imports.append(PIP_NAMES[m.group(1)])
    ordered = [l for l in LANG_ORDER if l in langs] + [l for l in langs if l not in LANG_ORDER]
    tested = {l: runtime_version(l) for l in ordered if runtime_version(l)}
    for c in cards:                                                # a card's own run says what it was run with, which may differ from today's server
        run = c.get("code_run") if isinstance(c.get("code_run"), dict) else None
        lang = _lang(c) if (c.get("code_snippet") or "").strip() else ""
        if run and lang and run.get("status") == "passed" and run.get("runner") and lang not in tested:
            tested[lang] = str(run["runner"])
    return {"languages": ordered, "platforms": platforms, "packages": imports, "tested_on": tested}


def setup_prompt(ctx: Dict[str, Any], stack: Dict[str, Any]) -> str:
    return (
        "You prepare the MANDATORY SETUP CHECKLIST for a live programming class. The instructor and the learners must have all of this working BEFORE the class starts.\n\n"
        f"CLASS: {ctx.get('title')} ({ctx.get('program') or ''} {ctx.get('track') or ''}, {ctx.get('minutes') or '?'} minutes)\n"
        f"LANGUAGES THE CLASS CODE USES: {', '.join(stack['languages']) or 'none found'}\n"
        f"PLATFORM NOTES WRITTEN IN THE CODE: {'; '.join(stack['platforms']) or 'none'}\n"
        f"PYTHON LIBRARIES THE CODE IMPORTS: {', '.join(stack['packages']) or 'none'}\n"
        f"VERSIONS THE CODE WAS ACTUALLY RUN WITH (tested): {'; '.join(f'{k}: {v}' for k, v in stack['tested_on'].items()) or 'unknown'}\n"
        f"THE SME'S OWN NOTES AND LINKS (use them, never contradict them): {(ctx.get('notes') or 'none')[:1200]}\n\n"
        "Rules:\n"
        "- List the SOFTWARE, accounts, libraries and data the class CODE needs, and nothing else. Include the languages above. Do NOT list generic things such as an\n"
        "  internet connection, a video call tool, a laptop or a headset.\n"
        "- If the platform notes name a database or service (for example BigQuery), that is what the learners use. SQLite below is only what this server used to test the code.\n"
        "  Do not list SQLite for learners unless the platform notes say SQLite.\n"
        "- Never give a sign-in, login or account-selection page as a source: give the product's public download or documentation page.\n"
        "- version: major.minor, for example '3.11 or newer' or 'Node 20 LTS' (never a patch number, never 'latest'). If the code's platform notes name a version, use that one. If a tested version is given, the version you state must be that one or newer.\n"
        "- who: 'instructor', 'learners' or 'both'.\n"
        "- why: one short sentence. install: the COMPLETE step-by-step setup, 4 to 8 short numbered actions, in the order a beginner does them, including account sign-up, opening the console, creating a project or workspace, "
        "and the first thing to click, for tools that need it. Say where to click or what to type. Give the Windows and macOS difference when there is one. verify_command: the command or action that shows it works. expected_output: what they should see.\n"
        "- source_name and source_url: the OFFICIAL download or documentation page for that tool. Prefer a link from the SME's notes if one fits. If you are not sure of an exact page, give the tool's official home page.\n"
        "- For a Python library, install is 'pip install <name>' and source_url is its page on pypi.org.\n"
        "- instructor_checklist: 4 to 7 short checks the instructor does before going live (open each tool, run the first snippet, test screen sharing, have a backup).\n"
        'Return ONLY JSON: {"items": [{"tool": string, "version": string, "why": string, "who": string, "install": [string], "verify_command": string, '
        '"expected_output": string, "source_name": string, "source_url": string}], "instructor_checklist": [string]}'
    )


def _str(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def _norm_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    tool = _str(raw.get("tool"), 80)
    if not tool:
        return None
    who = _str(raw.get("who"), 20).lower()
    return {
        "tool": tool, "version": _str(raw.get("version"), 80), "why": _str(raw.get("why"), 220),
        "who": who if who in ("instructor", "learners", "both") else "both",
        "install": [_str(s, 260) for s in (raw.get("install") or []) if _str(s, 260)][:8],
        "verify_command": _str(raw.get("verify_command"), 160), "expected_output": _str(raw.get("expected_output"), 160),
        "source": {"name": _str(raw.get("source_name"), 100) or tool, "url": safe_url(raw.get("source_url")) if safe_url(raw.get("source_url")) != "#" else "",
                   "verified": False, "note": None},
        "tested_on": "",
    }


def _from_baseline(lang: str, tested: str) -> Dict[str, Any]:
    b = BASELINE[lang]
    major = re.search(r"(\d+(?:\.\d+)?)", tested or "")
    return {"tool": b["tool"], "version": (f"{major.group(1)} or newer" if major else ""), "why": b["purpose"], "who": b["who"], "install": list(b["install"]),
            "verify_command": b["verify_command"], "expected_output": b["expected_output"],
            "source": {"name": b["source_name"], "url": b["source_url"], "verified": False, "note": None}, "tested_on": ""}


def _same_tool(a: str, b: str) -> bool:
    return bool({w for w in re.findall(r"[a-z]+", a.lower()) if len(w) > 2} & {w for w in re.findall(r"[a-z]+", b.lower()) if len(w) > 2})


def _host(url: str) -> str:
    m = re.match(r"^https?://([^/?#:]+)", url or "", re.I)
    return re.sub(r"^www\.", "", m.group(1).lower()) if m else ""


def _is_official(url: str) -> bool:
    host = _host(url)
    return any(host == h or host.endswith("." + h) for h in OFFICIAL_HOSTS)


def _verify(item: Dict[str, Any], fallback_url: str = "") -> None:
    """Fetch the source page. 'checked': its text is about this tool. 'official': it opens and is on the maker's own site, but its text could not be
    matched. 'unverified': neither. The link shown is always the one given, never an address the page redirected to (that can be a sign-in page)."""
    from agents.source_fetcher import verify_source
    claim = f"{item['tool']} download"                       # the tool's name only: a version number is not on every page, and a missing one proves nothing
    reachable_official = None
    for url in [u for u in dict.fromkeys([item["source"]["url"], fallback_url]) if u]:
        try:
            check = verify_source(url, claim, f"{item['tool']} download install")
        except Exception as e:                                 # a broken page must never break the checklist
            check = {"verified": False, "fetched": False, "note": str(e)[:100]}
        if check.get("verified"):
            item["source"].update({"url": url, "verified": True, "status": "checked", "note": None, "evidence": check.get("evidence"),
                                   "page_title": check.get("page_title"), "checked_at": check.get("checked_at")})
            return
        if check.get("fetched") and _is_official(url) and reachable_official is None:
            reachable_official = (url, check)
        item["source"].update({"note": check.get("note")})
    if reachable_official:
        url, check = reachable_official
        item["source"].update({"url": url, "verified": False, "status": "official", "note": "On the maker's own site and the page opens, but its text could not be matched.",
                               "page_title": check.get("page_title"), "checked_at": check.get("checked_at")})
        return
    item["source"].update({"verified": False, "status": "unverified"})
    if not item["source"]["url"]:
        item["source"]["note"] = "No link was given."


def build_setup(ctx: Dict[str, Any], cards: List[Dict[str, Any]], ask: Callable[[str], Any], verify: bool = True) -> Dict[str, Any]:
    """The checklist for this class. `ask(prompt)` returns the AI's JSON. Raises ValueError when the AI's answer has no usable items."""
    stack = detect_stack(cards)
    answer = ask(setup_prompt(ctx, stack))
    raw_items = answer.get("items") if isinstance(answer, dict) else None
    items = [i for i in (_norm_item(r) for r in (raw_items or []) if isinstance(r, dict)) if i][:10]
    if not items:
        raise ValueError("the AI gave no setup items")
    for lang in stack["languages"]:                            # a language the code uses is never left out, whatever the AI said
        if lang == "sql" and SQL_ENGINES.search(" ".join(stack["platforms"])) and not re.search(r"sqlite", " ".join(stack["platforms"]), re.I):
            continue                                           # the class runs on another database; SQLite is only this server's test tool
        if lang in BASELINE and not any(_same_tool(i["tool"], BASELINE[lang]["tool"]) for i in items):
            items.insert(0, _from_baseline(lang, stack["tested_on"].get(lang, "")))
    other_engine = bool(SQL_ENGINES.search(" ".join(stack["platforms"]))) and not re.search(r"sqlite", " ".join(stack["platforms"]), re.I)
    for i in items:
        for lang, tested in stack["tested_on"].items():
            if lang == "sql" and other_engine:
                continue                                       # tested on SQLite, but learners use another database: do not claim it as theirs
            if lang in BASELINE and _same_tool(i["tool"], BASELINE[lang]["tool"]):
                i["tested_on"] = tested
    if verify:
        fallbacks = {id(i): next((BASELINE[l]["source_url"] for l in BASELINE if _same_tool(i["tool"], BASELINE[l]["tool"])), "") for i in items}
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda i: _verify(i, fallbacks[id(i)]), items))
    return {"items": items, "instructor_checklist": [_str(s, 200) for s in (answer.get("instructor_checklist") or []) if _str(s, 200)][:7],
            "languages": stack["languages"], "generated_at": time.strftime("%Y-%m-%d")}


_GENERIC = {"the", "and", "for", "with", "google", "free", "sandbox", "account", "console", "development", "kit", "sdk", "jdk", "tool", "tools", "online", "web", "cloud", "desktop",
            "software", "install", "version", "setup", "new", "newer", "latest", "learner", "learners"}


def _tokens(tool: str) -> List[str]:
    name = re.sub(r"\(.*?\)", " ", tool or "")
    return [t.lower() for t in re.findall(r"[A-Za-z][A-Za-z0-9+#.]{2,}", name) if t.lower() not in _GENERIC]


def find_usage(item: Dict[str, Any], cards: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Where in the class this tool is really used: [{card, title, evidence}], from the cards' own code and words, never claimed. Empty means no card uses it."""
    langs = [l for l in BASELINE if _same_tool(item.get("tool", ""), BASELINE[l]["tool"])]
    pip = re.search(r"pip\s+install\s+([\w.-]+)", " ".join(item.get("install") or []), re.I)
    words = _tokens(item.get("tool", "")) + ([pip.group(1).lower()] if pip else [])
    out: List[Dict[str, Any]] = []
    for c in sorted(cards, key=lambda c: c.get("card_index") or 0):
        code, text, evidence = c.get("code_snippet") or "", c.get("content_script") or "", ""
        if langs and code.strip() and _lang(c) in langs:
            first = next((l.strip() for l in code.splitlines() if l.strip()), "")
            evidence = f"runs this {_lang(c)} code: {first[:70]}"
        else:
            for w in words:
                m = re.search(r"\b" + re.escape(w) + r"\b", code, re.I)
                if m:
                    line = next((l.strip() for l in code.splitlines() if re.search(r"\b" + re.escape(w) + r"\b", l, re.I)), "")
                    evidence = f"the code uses it: {line[:70]}"
                    break
                if re.search(r"\b" + re.escape(w) + r"\b", text, re.I):
                    evidence = "the script uses it in the lesson"
                    break
        if evidence:
            out.append({"card": c.get("card_index"), "title": c.get("title"), "evidence": evidence})
    return out[:5]


def annotate_usage(setup: Any, cards: List[Dict[str, Any]]) -> Any:
    """The setup with each tool's real use in the class attached. A tool no card uses is moved to `left_out`: the class must not ask for what it never uses."""
    if not isinstance(setup, dict) or not setup.get("items"):
        return setup
    kept, left = [], []
    for i in list(setup["items"]) + [{"tool": t, "_was_left": True} for t in setup.get("left_out") or []]:
        if i.get("_was_left"):
            left.append(i["tool"])
            continue
        used = find_usage(i, cards)
        if used or not cards:
            kept.append({**i, "used_in": used})
        else:
            left.append(i["tool"])
    # a tool another kept tool's steps rely on (the Google account behind the Cloud console) is a prerequisite, not an extra
    for tool in list(left):
        names = {tool.lower(), *_tokens(tool)}
        if any(n and n in " ".join(k.get("install") or []).lower() for k in kept for n in names if len(n) > 5 or " " in n):
            left.remove(tool)
            original = next((i for i in setup["items"] if i.get("tool") == tool), None)
            if original:
                kept.insert(0, {**original, "used_in": [], "prerequisite": True})
    for k in kept:     # a year written into a version goes stale; say "latest" instead
        if isinstance(k.get("version"), str):
            k["version"] = re.sub(r"\s*\(?current as of \d{4}\)?", " (latest)", k["version"]).strip()
    return {**setup, "items": kept, "left_out": left} if kept else setup


_COMMANDS = {"python", "python3", "pip", "pip3", "node", "npm", "npx", "java", "javac", "sqlite3", "git", "docker", "code", "mvn", "gradle", "go", "rustc", "cargo",
             "ruby", "gem", "php", "dotnet", "conda", "jupyter", "psql", "mysql", "curl", "which", "echo", "ls", "cd", "pwd", "ping"}


def looks_like_command(text: str) -> bool:
    """True for something typed into a terminal (shown as code); false for an instruction in words, like 'Open the page and confirm...'."""
    t = (text or "").strip()
    first = t.split(" ", 1)[0].lower() if t else ""
    return bool(t) and len(t) <= 90 and first in _COMMANDS and not t.endswith((".", "!", "?"))


# ---- showing it ------------------------------------------------------------------------------------------------------------------------

_STATE = {"checked": "checked: the page is about this tool", "official": "official site, page opens", "unverified": "not checked"}
_WHO = {"instructor": "Instructor", "learners": "Learners", "both": "Instructor and learners"}


def setup_html(setup: Any) -> str:
    """The checklist as safe HTML (everything escaped, only http(s) links)."""
    if not isinstance(setup, dict) or not setup.get("items"):
        return ""
    cards = []
    for i in setup["items"]:
        src = i.get("source") or {}
        link = safe_url(src.get("url"))
        state = _STATE.get(src.get("status") or ("checked" if src.get("verified") else "unverified"), "not checked")
        source = (f'<a class="setup2-link" href="{esc(link)}" target="_blank" rel="noopener noreferrer">{esc(src.get("name") or i["tool"])}</a>'
                  f' <span class="setup2-chip {"ok" if src.get("verified") else ""}">{esc(state)}</span>' if link != "#" else esc(src.get("name") or "No link"))
        steps = "".join(f"<li>{esc(s)}</li>" for s in i.get("install") or [])
        how = f'<code>{esc(i["verify_command"])}</code>' if looks_like_command(i.get("verify_command")) else esc(i.get("verify_command"))
        verify = (f'<div class="setup2-row"><b>Check it works:</b> {how}' + (f' &rarr; {esc(i["expected_output"])}' if i.get("expected_output") else "") + "</div>"
                  if i.get("verify_command") else "")
        cards.append(
            f'<div class="setup2-item"><div class="setup2-top"><span class="setup2-tool">{esc(i["tool"])}</span>'
            + (f'<span class="setup2-chip ver">Version: {esc(i["version"])}</span>' if i.get("version") else "")
            + f'<span class="setup2-chip">{esc(_WHO.get(i.get("who"), "Instructor and learners"))}</span></div>'
            + (f'<div class="setup2-row">{esc(i["why"])}</div>' if i.get("why") else "")
            + (f'<div class="setup2-row"><b>Tested on:</b> {esc(i["tested_on"])}</div>' if i.get("tested_on") else "")
            + (f'<div class="setup2-row"><b>Set it up, step by step:</b></div><ol class="setup2-steps">{steps}</ol>' if steps else "") + verify
            + ('<div class="setup2-row"><b>Used in this class:</b></div><ul class="setup2-steps">' + "".join(
                f'<li>Card {esc(u["card"])}, {esc(u["title"])}: {esc(u["evidence"])}</li>' for u in i["used_in"]) + "</ul>" if i.get("used_in") else "")
            + f'<div class="setup2-row"><b>Source:</b> {source}</div></div>')
    checklist = "".join(f"<li>{esc(s)}</li>" for s in setup.get("instructor_checklist") or [])
    left = (f'<div class="setup2-foot">Not asked for, because no card uses it: {esc(", ".join(setup["left_out"]))}.</div>' if setup.get("left_out") else "")
    return ('<div class="setup2"><div class="setup2-head">Before the class: mandatory setup</div>'
            '<div class="setup2-lead">All of this must work before the class starts. Check it on the machine you will teach from, and ask learners to do the same.</div>'
            + "".join(cards) + (f'<div class="setup2-item"><div class="setup2-tool">Instructor: check before going live</div><ul class="setup2-steps">{checklist}</ul></div>' if checklist else "")
            + left + f'<div class="setup2-foot">Made on {esc(setup.get("generated_at") or "")}. "Checked" means the page text matched. "Official site" means it is the maker\'s own site and opens, but the text could not be matched. "Not checked" means neither: open it before you rely on it.</div></div>')


def setup_markdown(setup: Any) -> str:
    if not isinstance(setup, dict) or not setup.get("items"):
        return ""
    lines = ["## Before the class: mandatory setup", "", "All of this must work before the class starts. Check it on the machine you will teach from.", ""]
    for i in setup["items"]:
        src = i.get("source") or {}
        lines.append(f"### {i['tool']}" + (f" ({i['version']})" if i.get("version") else "") + f" - {_WHO.get(i.get('who'), 'Instructor and learners')}")
        if i.get("why"):
            lines.append(i["why"])
        if i.get("tested_on"):
            lines.append(f"- Tested on: {i['tested_on']}")
        for n, step in enumerate(i.get("install") or [], start=1):
            lines.append(f"{n}. {step}")
        for u in i.get("used_in") or []:
            lines.append(f"- Used in card {u['card']}, {u['title']}: {u['evidence']}")
        if i.get("verify_command"):
            how = f"`{i['verify_command']}`" if looks_like_command(i["verify_command"]) else i["verify_command"]
            lines.append(f"- Check it works: {how}" + (f" -> {i['expected_output']}" if i.get("expected_output") else ""))
        link = safe_url(src.get("url"))
        if link != "#":
            lines.append(f"- Source: [{src.get('name') or i['tool']}]({link}) ({_STATE.get(src.get('status') or ('checked' if src.get('verified') else 'unverified'), 'not checked')})")
        lines.append("")
    if setup.get("instructor_checklist"):
        lines.append("### Instructor: check before going live")
        lines.extend(f"- [ ] {s}" for s in setup["instructor_checklist"])
        lines.append("")
    return "\n".join(lines)
