/* OpsPilot Copilot — conversation UI. All AI and database access happens on the server. */
(function () {
  "use strict";
  const root = document.getElementById("copilot");
  if (!root) return;

  const $ = (sel, el = document) => el.querySelector(sel);
  const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
  const thread = $("#cp-thread"), form = $("#cp-form"), input = $("#cp-input"), send = $("#cp-send");
  const welcome = $("#cp-welcome"), chips = $("#cp-chips"), clearBtn = $("[data-cp-clear]");
  const STORE = "opspilot.copilot.v1";
  const INSUFFICIENT = "I don't have enough information in OpsPilot to answer that.";
  let turns = [];   // {question, time, answer?, error?}
  let busy = false;

  const iconTpl = document.getElementById("cp-tpl-icons");
  const icon = (name) => { const el = iconTpl && iconTpl.content.querySelector(`[data-i="${name}"]`); return el ? el.innerHTML : ""; };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const clock = (iso) => new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });

  /* Safe rendering: escape everything, then allow **bold**, "- " bullets and links to referenced projects. */
  function renderText(text, refs) {
    const blocks = String(text || "").trim().split(/\n{2,}/);
    let html = blocks.map((block) => {
      const lines = block.split("\n");
      if (lines.every((l) => /^\s*[-*•]\s+/.test(l))) {
        return "<ul>" + lines.map((l) => `<li>${inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>`).join("") + "</ul>";
      }
      const out = []; let list = [];
      lines.forEach((l) => {
        if (/^\s*[-*•]\s+/.test(l)) list.push(`<li>${inline(l.replace(/^\s*[-*•]\s+/, ""))}</li>`);
        else { if (list.length) { out.push(`<ul>${list.join("")}</ul>`); list = []; } out.push(`<p>${inline(l)}</p>`); }
      });
      if (list.length) out.push(`<ul>${list.join("")}</ul>`);
      return out.join("");
    }).join("");
    (refs || []).forEach((r) => {  // link the first mention of each referenced customer
      const name = esc(r.name);
      // Only generated tags (<p>, <ul>, <li>, <strong>, <a href="/projects/N">) exist here, so a name can't be inside an attribute.
      const re = new RegExp(`(^|[^\\w])(${name.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")})(?![^<]*</a>)`, "i");
      html = html.replace(re, `$1<a href="${esc(r.url)}">$2</a>`);
    });
    return html;
  }
  function inline(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>"); }
  const initials = (name) => (name || "?").split(/\s+/).map((p) => p[0]).slice(0, 2).join("").toUpperCase();

  /* ---------------- Rendering turns ---------------- */
  function questionEl(t) {
    const el = $("#cp-tpl-question").content.cloneNode(true);
    $(".cp-q-text", el).textContent = t.question;
    $(".cp-time", el).textContent = clock(t.time);
    return el;
  }

  function answerEl(t, index) {
    const wrap = document.createElement("div");
    wrap.className = "cp-a";
    const a = t.answer;
    if (t.error) {
      wrap.innerHTML = `<span class="cp-a-mark">${icon("sparkle")}</span>
        <div class="cp-a-body"><div class="alert alert-error" role="alert">${icon("alert")}
          <div class="grow"><div class="alert-title">Copilot is temporarily unavailable.</div><div>${esc(t.error.message)}</div>
          ${t.error.code ? `<div class="xsmall muted" style="margin-top:4px">${esc(t.error.title || "")}. Error code <code>${esc(t.error.code)}</code></div>` : ""}</div>
          <button type="button" class="btn btn-sm" data-cp-retry="${index}">${icon("refresh")}Retry</button></div></div>`;
      return wrap;
    }
    const insufficient = a.answer_type === "insufficient_data" || a.answer_type === "read_only_request";
    const clarify = a.answer_type === "clarification_needed";
    const ds = a.data_sources || {};
    const counts = [["projects", "project"], ["tasks", "task"], ["documents", "document"], ["activity", "activity entry"], ["analyses", "AI analysis"]]
      .filter(([k]) => ds[k]).map(([k, label]) => `<span class="cp-count"><strong>${ds[k]}</strong>${ds[k] === 1 ? label : (k === "activity" ? "activity entries" : k === "analyses" ? "AI analyses" : k)}</span>`);
    let badge;
    if (a.grounded) badge = `<span class="badge actor-automation" data-tip="Built only from records OpsPilot retrieved for this question.">${icon("database")}Grounded in OpsPilot data</span>`;
    else if (clarify) badge = `<span class="badge status-needs-attention">${icon("info")}Needs clarification</span>`;
    else if (a.answer_type === "read_only_request") badge = `<span class="badge actor-automation">${icon("shield")}Read-only</span>`;
    else badge = `<span class="badge status-completed" data-tip="No OpsPilot records support an answer to this question.">${icon("info")}Not answerable from OpsPilot data</span>`;

    const refs = (a.references || []).map((r) => `<a class="cp-ref" href="${esc(r.url)}"><span class="avatar">${esc(initials(r.name))}</span>${esc(r.name)}</a>`).join("");
    const follow = (a.follow_ups || []).map((q) => `<button type="button" class="cp-chip" data-cp-ask="${esc(q)}">${esc(q)}</button>`).join("");
    const tools = (a.tools || []).map((tl) => `<div class="cp-tool${tl.ok ? "" : " is-error"}">${icon("tool")}<code>${esc(tl.name)}</code><span>${esc(tl.ok ? tl.arguments : "rejected: " + (tl.error || ""))}</span></div>`).join("");
    const m = a.meta || {};

    wrap.innerHTML = `<span class="cp-a-mark">${icon("sparkle")}</span>
      <div class="cp-a-body">
        <div class="cp-text${insufficient ? " is-insufficient" : ""}">${renderText(a.text, a.references)}</div>
        ${a.guard ? `<div class="alert alert-warning" style="font-size:var(--text-sm)">${icon("shield")}<div>${esc(a.guard)}</div></div>` : ""}
        <div class="cp-meta">${badge}
          ${a.grounded ? `<span class="cp-meta-label" style="margin-left:6px">Data source</span>${counts.join("") || '<span class="cp-count"><strong>0</strong>matching records</span>'}` : ""}
        </div>
        ${refs ? `<div class="cp-refs"><span class="cp-meta-label">Referenced projects</span>${refs}</div>` : ""}
        ${follow ? `<div class="cp-followups">${follow}</div>` : ""}
        <details class="cp-how"><summary>${icon("chevron")}How this was answered</summary>
          <div class="cp-how-body">
            ${tools || '<div class="cp-tool">No OpsPilot queries were run.</div>'}
            <div class="provenance"><span>Model <code>${esc(m.model || "")}</code></span>
              <span>${((m.latency_ms || 0) / 1000).toFixed(1)}s</span>
              ${(m.response_ids || []).map((id) => `<span>Response <code>${esc(id)}</code></span>`).join("")}</div>
          </div></details>
      </div>`;
    return wrap;
  }

  function turnEl(t, index) {
    const el = document.createElement("div");
    el.className = "cp-turn";
    el.appendChild(questionEl(t));
    if (t.pending) el.appendChild($("#cp-tpl-loading").content.cloneNode(true));
    else el.appendChild(answerEl(t, index));
    return el;
  }

  function render() {
    $$(".cp-turn", thread).forEach((n) => n.remove());
    welcome.hidden = turns.length > 0;
    chips.hidden = turns.length === 0;
    clearBtn.hidden = turns.length === 0;
    turns.forEach((t, i) => {
      const el = turnEl(t, i);
      if (i === turns.length - 1 && t.pending) el.classList.add("is-new");  // animate only a newly asked question
      thread.appendChild(el);
    });
    const last = thread.lastElementChild;  // show the start of the newest turn, not the bottom of a long answer
    if (last && last.classList.contains("cp-turn")) thread.scrollTop = Math.max(0, last.offsetTop - thread.offsetTop - 12);
  }

  function save() {
    try { sessionStorage.setItem(STORE, JSON.stringify(turns.filter((t) => !t.pending).slice(-20))); } catch (e) { /* storage unavailable */ }
  }
  function load() {
    try { turns = JSON.parse(sessionStorage.getItem(STORE) || "[]").filter((t) => t && t.question); } catch (e) { turns = []; }
  }

  /* ---------------- Asking ---------------- */
  function setBusy(on) {
    busy = on;
    send.classList.toggle("is-loading", on);
    send.disabled = on;
    $$("[data-cp-ask]").forEach((b) => { b.disabled = on; });
  }

  async function ask(question, retryIndex) {
    question = (question || "").trim();
    if (!question || busy) return;
    const history = turns.filter((t) => t.answer && !t.error).slice(-6)
      .map((t) => ({ question: t.question, answer: t.answer.text }));
    let t;
    if (retryIndex !== undefined) { t = turns[retryIndex]; delete t.error; t.pending = true; }
    else { t = { question, time: new Date().toISOString(), pending: true }; turns.push(t); }
    input.value = ""; autosize(); updateCount();
    setBusy(true); render();

    let data;
    try {
      const res = await fetch(root.dataset.askUrl, { method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" },
                                                     body: JSON.stringify({ question, history }) });
      try { data = await res.json(); } catch (e) { data = { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${res.status}.` } }; }
    } catch (e) {
      data = { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server. Check that it is still running." } };
    }
    delete t.pending;
    if (data.ok) t.answer = data.answer; else t.error = data.error || { message: "Something went wrong." };
    setBusy(false); render(); save();
    if (!data.ok && window.opsToast) window.opsToast("Copilot is temporarily unavailable.", "error");
    input.focus();
  }

  form.addEventListener("submit", (e) => { e.preventDefault(); ask(input.value); });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); ask(input.value); }
  });
  document.addEventListener("click", (e) => {
    const s = e.target.closest("[data-cp-ask]");
    if (s && root.contains(s)) { ask(s.dataset.cpAsk); return; }
    const r = e.target.closest("[data-cp-retry]");
    if (r) { const i = Number(r.dataset.cpRetry); ask(turns[i].question, i); }
  });
  clearBtn.addEventListener("click", () => {
    turns = []; save(); render(); input.focus();
    if (window.opsToast) window.opsToast("Conversation cleared.", "info", 2500);
  });

  function autosize() { input.style.height = "auto"; input.style.height = Math.min(input.scrollHeight, 140) + "px"; }
  function updateCount() { const c = $("[data-cp-count]"); if (c) c.textContent = input.value.length; }
  input.addEventListener("input", () => { autosize(); updateCount(); });

  load(); render();
  if (root.dataset.aiConfigured !== "1") {
    const note = document.createElement("div");
    note.className = "alert alert-warning";
    note.style.margin = "0 0 var(--space-2)";
    note.innerHTML = `${icon("info")}<div>AI is not configured, so Copilot can't answer yet. Add ANTHROPIC_API_KEY to .env and restart the app.</div>`;
    form.prepend(note);
  }
  input.focus();
})();
