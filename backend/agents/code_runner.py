"""
Runs the code that a card shows, so a script never teaches code that does not work (rule A3).

Each snippet is run in a separate process with a time limit, a stripped environment, a temporary working folder, limits on
CPU, memory and file size, and (for Python) the network and the ability to start other programs blocked. The result is saved
with the card and shown under the code: whether it ran, what it printed, and how long it took.

IMPORTANT: this is a safeguard against mistakes, not a security boundary. Code written by a model is untrusted. For a
production deployment run this module's work in a container with no network (set CODE_RUN=off to switch it off here).

Languages: Python (always), JavaScript (when Node is installed), Java (when a JDK is installed), SQL (with SQLite, only
for snippets that create their own tables). Anything else, or a language that is not installed, comes back as "skipped"
with the reason. It is never reported as passed.

Some code is meant to fail, to show learners an error. The writer says so with expected_error (for example "NameError");
the run then passes only if the code fails with exactly that error.

Settings (backend/.env):
  CODE_RUN            on | off      (default on)
  CODE_RUN_TIMEOUT    seconds one snippet may run (default 8; Java gets three times as long)
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from typing import Any, Dict, Optional

OUTPUT_LIMIT = 6000
MEMORY_LIMIT_BYTES = 768 * 1024 * 1024

PYTHON_GUARD = r'''
import builtins, io, json, os, socket, subprocess, sys, types
def _blocked(*a, **k):
    raise PermissionError("blocked in the script sandbox")
socket.socket = _blocked; socket.create_connection = _blocked; socket.getaddrinfo = _blocked
subprocess.Popen = _blocked; subprocess.run = _blocked; subprocess.call = _blocked
os.system = _blocked; os.popen = _blocked
for _n in ("execv", "execve", "execvp", "execl", "execle", "execlp", "spawnl", "spawnv", "fork", "forkpty", "kill", "killpg"):
    if hasattr(os, _n): setattr(os, _n, _blocked)
sys.argv = ["main.py"]
_src = open("main.py", encoding="utf-8").read()

# a record of the run, line by line: which line, the simple variables in scope, and how much had been printed by then
_steps, _real_out, _printed = [], sys.stdout, [0]
class _Tee:
    def write(self, s):
        _real_out.write(s); _printed[0] += len(s); return len(s)
    def flush(self): _real_out.flush()
    def __getattr__(self, name): return getattr(_real_out, name)
sys.stdout = _Tee()
_SKIP = (types.FunctionType, types.ModuleType, type, types.BuiltinFunctionType, types.MethodType)
def _snap(frame):
    out = {}
    for k, v in list(frame.f_locals.items()):
        if k.startswith("_") or isinstance(v, _SKIP) or len(out) >= 8: continue
        try: out[k] = repr(v)[:40]
        except Exception: out[k] = "?"
    return out
def _tracer(frame, event, arg):
    if frame.f_code.co_filename != "main.py": return None
    if event == "line":
        if len(_steps) >= 150:
            sys.settrace(None); _steps.append({"cut": True}); return None
        _steps.append({"l": frame.f_lineno, "vars": _snap(frame), "out": _printed[0], "fn": frame.f_code.co_name})
    return _tracer
def _dump():
    try:
        with open("trace.json", "w", encoding="utf-8") as f: json.dump(_steps, f)
    except Exception: pass
sys.settrace(_tracer)
try:
    exec(compile(_src, "main.py", "exec"), {"__name__": "__main__", "__file__": "main.py"})
finally:
    sys.settrace(None); _dump()
'''

LANGUAGE_ALIASES = {
    "py": "python", "python3": "python", "python": "python",
    "js": "javascript", "node": "javascript", "javascript": "javascript",
    "java": "java", "sql": "sql", "mysql": "sql", "postgresql": "sql", "sqlite": "sql", "bigquery": "sql",
}


MAX_TRACE_LINES = 80


def _trace(kind: str, code: str, steps: list, truncated: bool = False) -> Optional[Dict[str, Any]]:
    """The step-by-step record shown as an animation under the code: {kind, lines, steps:[{l:[first,last], note, vars, out}], truncated}.
    out is how many characters of the program's output had been printed by that step (None: show it only at the end)."""
    lines = code.split("\n")
    if not steps or len(lines) > MAX_TRACE_LINES:
        return None
    return {"kind": kind, "lines": lines, "steps": steps, "truncated": truncated}


def _python_steps(wd: str, code: str) -> Optional[Dict[str, Any]]:
    import json
    try:
        with open(os.path.join(wd, "trace.json"), encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    truncated = any(isinstance(r, dict) and r.get("cut") for r in raw)
    steps = [{"l": [r["l"], r["l"]], "note": "", "vars": r.get("vars") or {}, "out": r.get("out", 0), "fn": r.get("fn")}
             for r in raw if isinstance(r, dict) and "l" in r]
    return _trace("python", code, steps, truncated)


def _line_steps(code: str) -> Optional[Dict[str, Any]]:
    """For a language we cannot trace: each real line of code in turn, then the output. Honest, if less detailed."""
    steps = [{"l": [n, n], "note": "", "vars": {}, "out": None} for n, line in enumerate(code.split("\n"), start=1)
             if line.strip() and not re.match(r"^\s*(//|#|--|/\*|\*)", line) and line.strip() not in ("{", "}", "};")]
    return _trace("lines", code, steps)


_VERSION_CACHE: Dict[str, str] = {}


def runtime_version(language: str) -> str:
    """The version of the tool that really runs this language on this server, for example 'Python 3.11.4' or 'Node.js v22.14.0'. Empty if unknown."""
    if language in _VERSION_CACHE:
        return _VERSION_CACHE[language]
    out = ""
    try:
        if language == "python":
            out = "Python " + ".".join(map(str, sys.version_info[:3]))
        elif language == "sql":
            import sqlite3
            out = "SQLite " + sqlite3.sqlite_version
        elif language == "javascript" and shutil.which("node"):
            out = "Node.js " + subprocess.run(["node", "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        elif language == "java" and shutil.which("javac"):
            r = subprocess.run(["javac", "-version"], capture_output=True, text=True, timeout=15)
            out = "JDK " + ((r.stdout or r.stderr).strip().replace("javac", "").strip())
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    _VERSION_CACHE[language] = out
    return out


def enabled() -> bool:
    return os.getenv("CODE_RUN", "on").lower() != "off"


def _timeout() -> float:
    try:
        return float(os.getenv("CODE_RUN_TIMEOUT", "8"))
    except ValueError:
        return 8.0


def normalise_language(language: Optional[str], code: str = "") -> str:
    lang = LANGUAGE_ALIASES.get((language or "").strip().lower(), (language or "").strip().lower())
    if lang:
        return lang
    if re.search(r"\b(public\s+static\s+void\s+main|System\.out\.print)", code):
        return "java"
    if re.search(r"^\s*(SELECT|INSERT|CREATE|UPDATE|WITH)\b", code, re.I | re.M):
        return "sql"
    if re.search(r"\b(def |import |print\()", code):
        return "python"
    return ""


def _limits():
    """Applied in the child before it starts: CPU time, memory, file size, number of open files."""
    import resource
    cpu = int(_timeout() * 3 + 2)
    for name, value in (("RLIMIT_CPU", (cpu, cpu)), ("RLIMIT_FSIZE", (2_000_000, 2_000_000)), ("RLIMIT_NOFILE", (64, 64)),
                        ("RLIMIT_AS", (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))):
        try:
            resource.setrlimit(getattr(resource, name), value)
        except (ValueError, OSError):
            pass                       # not every system enforces every limit


def _result(language: str, status: str, **kw) -> Dict[str, Any]:
    base = {"language": language, "status": status, "stdout": "", "stderr": "", "exit_code": None, "seconds": 0.0,
            "reason": None, "expected_error": None, "ran_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    base.update(kw)
    return base


def _clean_env(workdir: str) -> Dict[str, str]:
    return {"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": workdir, "TMPDIR": workdir, "LANG": "C.UTF-8",
            "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}


def _exec(cmd, workdir: str, timeout: float) -> Dict[str, Any]:
    """Run one command in its own process group, kill the whole group on timeout, and cap the output."""
    started = time.time()
    try:
        proc = subprocess.Popen(cmd, cwd=workdir, env=_clean_env(workdir), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True, preexec_fn=_limits)
    except OSError as e:
        return {"started": False, "error": str(e)}
    try:
        out, err = proc.communicate(timeout=timeout)
        timed_out = False
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()
        timed_out = True
    return {"started": True, "code": proc.returncode, "stdout": out.decode("utf-8", "replace")[:OUTPUT_LIMIT],
            "stderr": err.decode("utf-8", "replace")[-OUTPUT_LIMIT:], "timed_out": timed_out, "seconds": round(time.time() - started, 2)}


def _judge(language: str, run: Dict[str, Any], expected_error: Optional[str], runner: str) -> Dict[str, Any]:
    """Turn a finished process into passed, failed or skipped."""
    if not run["started"]:
        return _result(language, "skipped", reason=f"Could not start the runner ({run['error']}).", runner=runner)
    common = dict(stdout=run["stdout"], stderr=run["stderr"], exit_code=run["code"], seconds=run["seconds"], runner=runner,
                  expected_error=expected_error)
    if run["timed_out"]:
        return _result(language, "timeout", reason=f"It was still running after the time limit.", **common)
    stderr = run["stderr"]
    if run["code"] == 0:
        if expected_error:
            return _result(language, "failed", reason=f"It was meant to fail with {expected_error}, but it ran without an error.", **common)
        return _result(language, "passed", **common)
    # a library this server does not have is not the script's fault
    missing = re.search(r"(?:ModuleNotFoundError|ImportError): No module named '([\w.]+)'", stderr)
    if missing:
        return _result(language, "skipped", reason=f"It needs the library '{missing.group(1).split('.')[0]}', which is not installed on this server.", **common)
    if "EOFError" in stderr and "input" in stderr.lower():
        return _result(language, "skipped", reason="It waits for keyboard input, which a check cannot give.", **common)
    if expected_error:
        if expected_error.lower() in stderr.lower():
            return _result(language, "passed", reason=f"It failed with {expected_error}, as the card intends.", **common)
        return _result(language, "failed", reason=f"It was meant to fail with {expected_error}, but the error was different.", **common)
    return _result(language, "failed", reason=(stderr.strip().splitlines() or ["The program exited with an error."])[-1][:240], **common)


# ---- one runner per language -------------------------------------------------------------------------------------

def _run_python(code: str, expected_error: Optional[str]) -> Dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="run_") as wd:
        with open(os.path.join(wd, "main.py"), "w", encoding="utf-8") as f:
            f.write(code)
        run = _exec([sys.executable, "-I", "-B", "-c", PYTHON_GUARD], wd, _timeout())
        trace = _python_steps(wd, code)
    result = _judge("python", run, expected_error, "python " + ".".join(map(str, sys.version_info[:3])))
    if trace:
        result["trace"] = trace
    return result


def _run_javascript(code: str, expected_error: Optional[str]) -> Dict[str, Any]:
    node = shutil.which("node")
    if not node:
        return _result("javascript", "skipped", reason="Node.js is not installed on this server, so JavaScript was not run.")
    with tempfile.TemporaryDirectory(prefix="run_") as wd:
        with open(os.path.join(wd, "main.js"), "w", encoding="utf-8") as f:
            f.write(code)
        run = _exec([node, "main.js"], wd, _timeout())
    return _judge("javascript", run, expected_error, runtime_version("javascript") or "node")


def _java_available() -> bool:
    if not (shutil.which("java") and shutil.which("javac")):
        return False
    try:
        return subprocess.run(["java", "-version"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _run_java(code: str, expected_error: Optional[str]) -> Dict[str, Any]:
    if not _java_available():
        return _result("java", "skipped", reason="Java (JDK) is not installed on this server, so Java was not run. Install a JDK to check Java code.")
    source = code
    if not re.search(r"\bclass\s+\w+", code):                       # a few statements: wrap them in a class
        imports = "\n".join(re.findall(r"^\s*import\s+[\w.*]+;\s*$", code, re.M))
        body = "\n".join(l for l in code.split("\n") if not re.match(r"^\s*import\s", l))
        source = f"{imports}\npublic class Main {{\n  public static void main(String[] args) throws Exception {{\n{body}\n  }}\n}}\n"
    match = re.search(r"public\s+class\s+(\w+)", source) or re.search(r"\bclass\s+(\w+)", source)
    name = match.group(1) if match else "Main"
    with tempfile.TemporaryDirectory(prefix="run_") as wd:
        with open(os.path.join(wd, f"{name}.java"), "w", encoding="utf-8") as f:
            f.write(source)
        compiled = _exec(["javac", "-encoding", "UTF-8", f"{name}.java"], wd, _timeout() * 3)
        if compiled.get("started") and compiled.get("code") not in (0, None) and not compiled.get("timed_out"):
            first = (compiled["stderr"].strip().splitlines() or ["compile error"])[0]
            if expected_error and expected_error.lower() in compiled["stderr"].lower():
                return _result("java", "passed", reason=f"It failed to compile with {expected_error}, as the card intends.",
                               stderr=compiled["stderr"], exit_code=compiled["code"], seconds=compiled["seconds"], runner="javac", expected_error=expected_error)
            return _result("java", "failed", reason=first[:240], stderr=compiled["stderr"], exit_code=compiled["code"],
                           seconds=compiled["seconds"], runner="javac", expected_error=expected_error)
        run = _exec(["java", "-Xmx256m", "-cp", wd, name], wd, _timeout() * 3)
    return _judge("java", run, expected_error, runtime_version("java") or "java")


_CLAUSES = (
    ("FROM", r"\bfrom\b", "Start with the table named here. Every row in it is a candidate."),
    ("JOIN", r"\bjoin\b", "Match each row with rows of the other table, using the ON condition."),
    ("WHERE", r"\bwhere\b", "Keep only the rows that meet this condition. The rest are dropped."),
    ("GROUP BY", r"\bgroup\s+by\b", "Collapse the rows that share the same value into one group."),
    ("HAVING", r"\bhaving\b", "Keep only the groups that meet this condition."),
    ("SELECT", r"\bselect\b", "Now pick the columns to show, and work out any calculation in them."),
    ("ORDER BY", r"\border\s+by\b", "Sort what is left."),
    ("LIMIT", r"\blimit\b", "Keep only this many rows from the top."),
)


def _split_sql(code: str):
    """[(statement, first line, last line)]. Split at each real semicolon: one inside a quoted string or a comment does not count, and two statements
    on one line are two statements. Comments are dropped from the statement text; line breaks are kept so line numbers still match the code."""
    out, buf, line, start, i, n, quote = [], [], 1, None, 0, len(code), None
    while i < n:
        ch = code[i]
        if quote:
            buf.append(ch)
            if ch == quote:
                if i + 1 < n and code[i + 1] == quote:
                    buf.append(code[i + 1]); i += 1                      # a doubled quote is a quote inside the string
                else:
                    quote = None
        elif code.startswith("--", i):
            while i < n and code[i] != "\n":
                i += 1
            continue
        elif code.startswith("/*", i):
            end = code.find("*/", i + 2)
            end = n if end < 0 else end + 2
            newlines = code[i:end].count("\n")
            if start is not None:
                buf.append("\n" * newlines)
            line += newlines
            i = end
            continue
        elif ch in "'\"`":
            quote = ch
            if start is None:
                start = line
            buf.append(ch)
        elif ch == ";":
            text = "".join(buf).strip()
            if text and start is not None:
                out.append((text, start, line))
            buf, start = [], None
        else:
            if not ch.isspace() and start is None:
                start = line
            if start is not None:
                buf.append(ch)
        if ch == "\n":
            line += 1
        i += 1
    text = "".join(buf).strip()
    if text and start is not None:
        out.append((text, start, line))
    return out


def _sql_steps(code: str, rows: Optional[int], output_len: Optional[int]) -> Optional[Dict[str, Any]]:
    """What a database does, in the order it does it, one highlighted piece at a time. Explains the query; it does not need a database."""
    steps = []
    for stmt, first, last in _split_sql(code):
        raw_lines = stmt.split("\n")
        skip = 0
        while skip < len(raw_lines) and (not raw_lines[skip].strip() or raw_lines[skip].strip().startswith("--")):
            skip += 1                                    # comment lines in front of a statement are not part of it
        stmt = "\n".join(raw_lines[skip:])
        first = first                                    # the first line of the statement itself
        low = stmt.lower()
        text_lines = stmt.split("\n")

        def at(pattern):
            for i, ln in enumerate(text_lines):
                if re.search(pattern, ln, re.I):
                    return first + i + (0 if not stmt.startswith("--") else 0)
            return first
        if low.startswith("create"):
            name = (re.search(r"create\s+(?:temp\w*\s+)?table\s+(?:if\s+not\s+exists\s+)?([\w.`\"]+)", stmt, re.I) or [None, "a table"])[1]
            steps.append({"l": [first, last], "note": f"Create the table {name}, with the columns listed here.", "vars": {}, "out": None})
        elif low.startswith("insert"):
            name = (re.search(r"insert\s+(?:or\s+\w+\s+)?into\s+([\w.`\"]+)", stmt, re.I) or [None, "the table"])[1]
            count = len(re.findall(r"\)\s*,\s*\(", stmt)) + 1 if re.search(r"\bvalues\b", stmt, re.I) else None
            steps.append({"l": [first, last], "note": f"Add {str(count) + ' row' + ('s' if count != 1 else '') if count else 'rows'} to {name}.", "vars": {}, "out": None})
        elif low.startswith(("select", "with")):
            for label, pattern, note in _CLAUSES:
                if re.search(pattern, stmt, re.I):
                    line = at(pattern)
                    steps.append({"l": [line, line], "note": f"{label}: {note}", "vars": {}, "out": None})
            steps.append({"l": [first, last], "note": "Done. This is the result the query gives back." + (f" It has {rows} row{'s' if rows != 1 else ''}." if rows is not None else ""),
                          "vars": {}, "out": output_len})
        else:
            steps.append({"l": [first, last], "note": "Run this statement.", "vars": {}, "out": None})
    return _trace("sql", code, steps)


def _run_sql(code: str, expected_error: Optional[str]) -> Dict[str, Any]:
    """SQL that builds its own tables is run for real (SQLite). A query against a database we do not have is explained step by step, not run."""
    if not re.search(r"\bcreate\s+(?:(?:global\s+|local\s+)?(?:temp|temporary)\s+)?(?:table|view)\b", code, re.I):
        res = _result("sql", "skipped", reason="This query needs a database that is not available here (it creates no tables of its own), so it was not run. "
                                              "The walkthrough below explains the query without running it.")
        trace = _sql_steps(code, None, None)
        if trace:
            res["trace"] = trace
        return res
    import sqlite3
    started = time.time()
    conn = sqlite3.connect(":memory:")
    out_lines, last_rows = [], None
    try:
        for statement, _first, _last in _split_sql(code):
            cur = conn.execute(statement)
            if cur.description:
                rows = cur.fetchmany(20)
                last_rows = len(rows)
                out_lines.append(" | ".join(d[0] for d in cur.description))
                out_lines.extend(" | ".join(str(v) for v in row) for row in rows)
        status, reason = "passed", "Checked with SQLite. Other databases can differ in syntax."
    except sqlite3.Error as e:
        # a failure in SQLite may just be a dialect difference, so it is not reported as a failure of the card
        res = _result("sql", "skipped", reason=f"SQLite could not run it ({e}); other databases use different syntax, so this was not counted as an error.",
                      stderr=str(e), seconds=round(time.time() - started, 2), runner=runtime_version("sql") or "sqlite")
        trace = _sql_steps(code, None, None)
        if trace:
            res["trace"] = trace
        return res
    finally:
        conn.close()
    output = "\n".join(out_lines)[:OUTPUT_LIMIT]
    res = _result("sql", status, reason=reason, stdout=output, seconds=round(time.time() - started, 2), runner=runtime_version("sql") or "sqlite")
    trace = _sql_steps(code, last_rows, len(output))
    if trace:
        res["trace"] = trace
    return res


RUNNERS = {"python": _run_python, "javascript": _run_javascript, "java": _run_java, "sql": _run_sql}


def run_code(code: Optional[str], language: Optional[str] = None, expected_error: Optional[str] = None) -> Dict[str, Any]:
    """Run one snippet and say what happened. Never raises."""
    code = (code or "").strip("\n")
    if not code.strip():
        return _result(language or "", "skipped", reason="There is no code to run.")
    lang = normalise_language(language, code)
    if not enabled():
        return _result(lang, "skipped", reason="Running code is switched off on this server (CODE_RUN=off).")
    runner = RUNNERS.get(lang)
    if runner is None:
        return _result(lang or "", "skipped", reason=f"{lang or 'This'} code cannot be run here (pseudocode or a language with no runner).")
    try:
        res = runner(code, (expected_error or "").strip() or None)
        if lang in ("javascript", "java") and "trace" not in res and res.get("status") in ("passed", "failed"):
            trace = _line_steps(code)
            if trace:
                res["trace"] = trace
        return res
    except Exception as e:                      # a bug in the runner must not break script generation
        return _result(lang, "skipped", reason=f"The runner failed ({type(e).__name__}: {str(e)[:120]}).")
