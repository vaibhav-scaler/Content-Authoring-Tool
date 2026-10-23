// Step-by-step code walkthrough: shows the code with the current line highlighted, the variables as they are at that moment,
// and the output printed so far. Used on the script page and on the shared page.
//
// Markup it looks for:  <div class="code-trace" data-trace='{"kind","lines","steps","truncated","stdout","status"}'></div>
// Everything from the data is put in with textContent, never as HTML, so nothing in it can run.
(function () {
  var STYLE_ID = "code-trace-style";
  var CSS = [
    ".ct{--ct-bg:#f8fafc;--ct-fg:#0f172a;--ct-muted:#64748b;--ct-line:#e2e8f0;--ct-on:#fde68a;--ct-onfg:#78350f;--ct-accent:#2563eb;--ct-chg:#bbf7d0;--ct-out:#0f172a;--ct-outfg:#e2e8f0;",
    "border:1px solid var(--ct-line);border-radius:10px;background:var(--ct-bg);color:var(--ct-fg);margin:12px 0;font-family:system-ui,sans-serif;overflow:hidden}",
    "@media (prefers-color-scheme: dark){.ct{--ct-bg:#0b1220;--ct-fg:#e2e8f0;--ct-muted:#94a3b8;--ct-line:#1e293b;--ct-on:#854d0e;--ct-onfg:#fef9c3;--ct-accent:#38bdf8;--ct-chg:#166534;--ct-out:#020617}}",
    ".ct-head{display:flex;flex-wrap:wrap;gap:8px;align-items:center;padding:8px 12px;border-bottom:1px solid var(--ct-line)}",
    ".ct-title{font-weight:700;font-size:.9rem;margin-right:auto}",
    ".ct-btn{border:1px solid var(--ct-line);background:transparent;color:var(--ct-fg);border-radius:6px;padding:3px 10px;font-size:.8rem;cursor:pointer}",
    ".ct-btn.primary{background:var(--ct-accent);border-color:var(--ct-accent);color:#fff;font-weight:600}",
    ".ct-count{font-size:.78rem;color:var(--ct-muted)}",
    ".ct-body{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:0}@media (max-width:720px){.ct-body{grid-template-columns:1fr}}",
    ".ct-code{margin:0;padding:8px 0;font:12.5px/1.6 ui-monospace,Menlo,Consolas,monospace;overflow:auto;max-height:340px;border-right:1px solid var(--ct-line)}",
    ".ct-row{display:flex;white-space:pre;padding:0 12px}.ct-row.on{background:var(--ct-on);color:var(--ct-onfg)}.ct-no{width:2.2em;color:var(--ct-muted);flex:none;user-select:none}",
    ".ct-side{padding:8px 12px;display:flex;flex-direction:column;gap:8px;min-width:0}",
    ".ct-note{font-size:.88rem;min-height:2.6em}.ct-h{font-size:.7rem;text-transform:uppercase;letter-spacing:.5px;color:var(--ct-muted);margin-bottom:3px}",
    ".ct-vars{font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;display:grid;grid-template-columns:auto 1fr;gap:2px 10px}",
    ".ct-vars .chg{background:var(--ct-chg);border-radius:3px;padding:0 4px}.ct-empty{color:var(--ct-muted);font-size:.8rem}",
    ".ct-out{margin:0;padding:6px 8px;border-radius:6px;background:var(--ct-out);color:var(--ct-outfg);font:12px/1.5 ui-monospace,Menlo,Consolas,monospace;min-height:2em;max-height:140px;overflow:auto;white-space:pre-wrap}",
    ".ct-foot{padding:6px 12px;border-top:1px solid var(--ct-line);font-size:.75rem;color:var(--ct-muted)}"
  ].join("\n");

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }

  function build(root) {
    var data;
    try { data = JSON.parse(root.getAttribute("data-trace") || "null"); } catch (e) { data = null; }
    root.setAttribute("data-ready", "1");
    if (!data || !Array.isArray(data.steps) || !data.steps.length || !Array.isArray(data.lines)) { root.style.display = "none"; return; }

    var steps = data.steps.slice();
    var stdout = String(data.stdout || "");
    var last = steps[steps.length - 1];
    var sqlDone = data.kind === "sql" && last.out !== null && last.out !== undefined;     // a SQL walkthrough already ends on its own result step
    if (!sqlDone) steps.push({ end: true, l: null, note: data.status === "skipped" ? "End of the walkthrough. The code was not run, so there is no output to show." : "The program finished.", vars: last.vars || {}, out: stdout.length });
    var idx = 0, timer = null, speed = 1100;

    root.className = (root.className + " ct").trim();
    root.textContent = "";
    var head = el("div", "ct-head");
    head.appendChild(el("span", "ct-title", "Watch the code run, step by step"));
    var count = el("span", "ct-count");
    var bReset = el("button", "ct-btn", "Reset"), bPrev = el("button", "ct-btn", "Back"), bPlay = el("button", "ct-btn primary", "Play"), bNext = el("button", "ct-btn", "Next");
    [bReset, bPrev, bPlay, bNext].forEach(function (b) { b.type = "button"; });
    var sel = el("select", "ct-btn");
    [["Slow", 1800], ["Normal", 1100], ["Fast", 500]].forEach(function (o) { var op = el("option", "", o[0]); op.value = o[1]; if (o[1] === 1100) op.selected = true; sel.appendChild(op); });
    [count, bReset, bPrev, bPlay, bNext, sel].forEach(function (n) { head.appendChild(n); });

    var body = el("div", "ct-body");
    var pre = el("div", "ct-code");
    var rows = data.lines.map(function (text, i) {
      var r = el("div", "ct-row"); r.appendChild(el("span", "ct-no", String(i + 1))); r.appendChild(el("span", "", text || " ")); pre.appendChild(r); return r;
    });
    var side = el("div", "ct-side");
    var note = el("div", "ct-note");
    var varsBox = el("div"); var varsHead = el("div", "ct-h", data.kind === "python" ? "Variables, before this line runs" : "Variables"); var vars = el("div", "ct-vars");
    varsBox.appendChild(varsHead); varsBox.appendChild(vars);
    if (data.kind !== "python") varsBox.style.display = "none";
    var outBox = el("div"); outBox.appendChild(el("div", "ct-h", "Output so far")); var out = el("pre", "ct-out"); outBox.appendChild(out);
    [note, varsBox, outBox].forEach(function (n) { side.appendChild(n); });
    body.appendChild(pre); body.appendChild(side);
    root.appendChild(head); root.appendChild(body);
    if (data.truncated) root.appendChild(el("div", "ct-foot", "This program runs for a long time, so only its first " + (steps.length - 1) + " steps are shown."));

    function show() {
      var s = steps[idx], prev = idx > 0 ? steps[idx - 1].vars || {} : {};
      rows.forEach(function (r, i) { var on = s.l && i + 1 >= s.l[0] && i + 1 <= s.l[1]; r.className = "ct-row" + (on ? " on" : ""); });
      var firstOn = s.l ? rows[s.l[0] - 1] : null;
      if (firstOn && pre.scrollHeight > pre.clientHeight) pre.scrollTop = Math.max(0, firstOn.offsetTop - pre.clientHeight / 3);
      var text = s.note || (s.end ? "" : (s.fn && s.fn !== "<module>" ? "Line " + s.l[0] + ", inside " + s.fn + "()" : "Line " + s.l[0]));
      note.textContent = text;
      vars.textContent = "";
      var names = Object.keys(s.vars || {});
      if (!names.length) vars.appendChild(el("span", "ct-empty", "nothing yet"));
      names.forEach(function (k) {
        var changed = prev[k] !== s.vars[k];
        vars.appendChild(el("span", "", k)); vars.appendChild(el("span", changed ? "chg" : "", s.vars[k]));
      });
      var shown = s.out === null || s.out === undefined ? (s.end ? stdout : "") : stdout.slice(0, s.out);
      out.textContent = shown || (s.end && !stdout ? "(no output)" : "");
      count.textContent = "Step " + (idx + 1) + " of " + steps.length;
      bPrev.disabled = idx === 0; bNext.disabled = idx === steps.length - 1;
    }
    function stop() { if (timer) { clearInterval(timer); timer = null; } bPlay.textContent = idx >= steps.length - 1 ? "Replay" : "Play"; }
    function tick() { if (idx >= steps.length - 1) { stop(); return; } idx += 1; show(); if (idx >= steps.length - 1) stop(); }
    bPlay.onclick = function () {
      if (timer) { stop(); return; }
      if (idx >= steps.length - 1) { idx = 0; show(); }
      bPlay.textContent = "Pause"; timer = setInterval(tick, speed);
    };
    bNext.onclick = function () { stop(); if (idx < steps.length - 1) { idx += 1; show(); } };
    bPrev.onclick = function () { stop(); if (idx > 0) { idx -= 1; show(); } };
    bReset.onclick = function () { stop(); idx = 0; show(); };
    sel.onchange = function () { speed = Number(sel.value); if (timer) { clearInterval(timer); timer = setInterval(tick, speed); } };
    show();
  }

  window.initCodeTrace = function (scope) {
    if (!document.getElementById(STYLE_ID)) { var st = document.createElement("style"); st.id = STYLE_ID; st.textContent = CSS; document.head.appendChild(st); }
    (scope || document).querySelectorAll(".code-trace:not([data-ready])").forEach(build);
  };
})();
