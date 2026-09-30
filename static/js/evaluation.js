/* OpsPilot AI Evaluation Lab.
   A run is started by a person, then advanced one case (one Claude request) per call, so it can be
   stopped between cases. Opening the page never calls Claude. All model text is set with textContent. */
(function () {
  "use strict";
  const root = document.getElementById("ev");
  if (!root) return;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const toast = (m, t, ms) => window.opsToast && window.opsToast(m, t, ms);
  const cfg = JSON.parse($("#ev-config").textContent);
  const SVG = "http://www.w3.org/2000/svg";
  const url = (tpl, id) => root.dataset[tpl].replace("/0/", `/${id}/`).replace(/\/0$/, `/${id}`);

  function icon(name, cls) { const s = document.createElementNS(SVG, "svg"); s.setAttribute("class", "icon" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true"); const u = document.createElementNS(SVG, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s; }
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v == null || v === false) return; if (k === "class") el.className = v; else if (k === "text") el.textContent = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? "" : v); });
    children.flat().forEach((c) => { if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return el;
  }
  const fromHTML = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content; };
  const when = (ts) => { const d = new Date(ts); return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); };
  async function request(u, opts) {
    try {
      const r = await fetch(u, opts);
      try { return await r.json(); } catch (e) { return { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${r.status}.` } }; }
    } catch (e) { return { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server." } }; }
  }
  const post = (u, body) => request(u, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body || {}) });
  function setLoading(btn, on, text) { if (!btn) return; btn.classList.toggle("is-loading", on); btn.disabled = on; const l = $(".btn-text", btn); if (l) { if (on) { l.dataset.orig = l.dataset.orig || l.textContent; if (text) l.textContent = text; } else if (l.dataset.orig) l.textContent = l.dataset.orig; } }

  /* ---------------- Compare ---------------- */
  const cmp = $("#ev-compare");
  if (cmp) cmp.addEventListener("change", () => {
    const u = new URL(root.dataset.pageUrl, location.origin); u.searchParams.set("run", cmp.dataset.run);
    if (cmp.value) u.searchParams.set("compare", cmp.value);
    location.href = u.toString();
  });

  /* ---------------- Running an evaluation ---------------- */
  const modal = $("#ev-run-modal"), go = $("#ev-run-go"), alertBox = $("#ev-run-alert");
  const prog = $("#ev-progress"), progTitle = $("#ev-progress-title"), progSub = $("#ev-progress-sub"), fill = $("#ev-progress-fill"), stopBtn = $("#ev-stop");
  const runBtn = $("#ev-run-open");
  let running = null, stopRequested = false;

  modal.addEventListener("close", () => { alertBox.replaceChildren(); setLoading(go, false); });
  function showError(box, err) {
    box.replaceChildren(h("div", { class: "alert alert-error", role: "alert" }, icon("alert"),
      h("div", {}, h("div", { class: "alert-title", text: err.title || "Evaluation not started" }), h("div", { text: `${err.message || ""}${err.code ? ` (${err.code})` : ""}` }))));
  }

  function markQueued(plan) {
    const keys = new Set(plan.map((p) => p.key));
    $$(".ev-row").forEach((row) => {
      const inPlan = keys.has(row.dataset.case);
      row.classList.remove("is-clickable"); row.removeAttribute("data-result"); row.removeAttribute("tabindex");
      row.classList.toggle("is-out", !inPlan);
      $(".ev-c-act", row).replaceChildren(h("span", { class: "muted small" + (inPlan ? " ev-waiting" : ""), text: inPlan ? "Queued" : "Not in this run" }));
      $(".ev-c-res", row).replaceChildren(h("span", { class: "badge ev-notrun" }, icon("clock"), inPlan ? "Queued" : "Not run"));
      $(".ev-c-human", row).replaceChildren(h("span", { class: "muted small", text: "—" }));
      $(".ev-c-go", row).replaceChildren();
    });
  }
  function markRunning(key) {
    $$(".ev-row.is-running").forEach((r) => r.classList.remove("is-running"));
    const row = $(`.ev-row[data-case="${CSS.escape(key)}"]`);
    if (!row) return;
    row.classList.add("is-running");
    $(".ev-c-act", row).replaceChildren(h("span", { class: "ev-running" }, h("span", { class: "spinner" }), "Waiting for Claude…"));
    $(".ev-c-res", row).replaceChildren(h("span", { class: "badge ev-st-running" }, icon("refresh"), "Running"));
  }
  function progress(done, total, next) {
    fill.style.width = `${Math.round(100 * done / total)}%`;
    progTitle.textContent = next ? `Running case ${done + 1} of ${total}: ${next.title}` : `Finishing… ${done} of ${total} cases`;
    progSub.textContent = stopRequested ? "Stopping after this case. No further requests will be sent." : "Each case is one request to Claude. Results are stored as they arrive.";
  }

  async function loop(runId, plan, startAt) {
    running = runId; stopRequested = false;
    prog.hidden = false; prog.classList.remove("is-error");
    runBtn.disabled = true;
    const total = plan.length;
    let done = startAt || 0;
    while (true) {
      const next = plan[done];
      progress(done, total, next);
      if (next) markRunning(next.key);
      const res = await post(url("stepUrlTemplate", runId));
      if (!res.ok) {
        prog.classList.add("is-error");
        progTitle.textContent = (res.error && res.error.title) || "The run was interrupted";
        progSub.textContent = ((res.error && res.error.message) || "") + " Nothing more will be sent unless you continue.";
        stopBtn.replaceChildren(icon("refresh"), h("span", { class: "btn-text", text: "Continue run" }));
        stopBtn.onclick = () => { stopBtn.onclick = null; resetStop(); loop(runId, plan, done); };
        return;
      }
      if (res.row_html) {
        const row = $(`.ev-row[data-case="${CSS.escape(res.case_key)}"]`);
        if (row) row.replaceWith(fromHTML(res.row_html));
      }
      if (res.metrics_html) $("#ev-metrics-wrap").replaceChildren(fromHTML(res.metrics_html));
      if (res.result_id) done += 1;
      if (res.error) { toast(`Run stopped: ${res.error.title}. ${res.error.message}`, "error", 9000); break; }
      if (res.done) break;
      if (stopRequested) { await post(url("stopUrlTemplate", runId)); toast("Run stopped. Completed cases are saved.", "info"); break; }
    }
    progress(done, total, null);
    setTimeout(() => { location.href = `${root.dataset.pageUrl}?run=${runId}`; }, 500);
  }
  function resetStop() {
    stopBtn.onclick = null;
    stopBtn.replaceChildren(icon("minus"), h("span", { class: "btn-text", text: "Stop after this case" }));
  }
  stopBtn.addEventListener("click", () => {
    if (!running || stopBtn.onclick) return;
    stopRequested = true; stopBtn.disabled = true;
    progSub.textContent = "Stopping after this case. No further requests will be sent.";
  });

  go.addEventListener("click", async () => {
    const limit = Number(($('input[name="ev-limit"]:checked') || {}).value || 3);
    alertBox.replaceChildren();
    setLoading(go, true, "Starting…");
    const res = await post(root.dataset.startUrl, { limit });
    setLoading(go, false);
    if (!res.ok) { showError(alertBox, res.error || {}); return; }
    modal.close();
    history.replaceState(null, "", res.url);
    const bar = $(".ev-runbar-main");
    if (bar) bar.replaceChildren(h("span", { class: "ev-run-id", text: `Run #${res.run_id}` }), h("span", { class: "badge ev-st-running" }, icon("refresh"), "Running"),
      h("span", { class: "ev-run-meta", text: `${res.planned} ${res.planned === 1 ? "case" : "cases"}` }));
    const c = $(".ev-compare"); if (c) c.hidden = true;
    markQueued(res.plan);
    loop(res.run_id, res.plan, 0);
  });

  // A run left in progress (another tab, or a reload): offer to continue. Never continue automatically.
  if (root.dataset.runningRun) {
    const runId = Number(root.dataset.runningRun);
    prog.hidden = false; prog.classList.add("is-paused");
    $(".spinner", prog).hidden = true;
    progTitle.textContent = `Run #${runId} is not finished`;
    progSub.textContent = "It paused when the page was closed. Continue to send the remaining cases, or stop it here.";
    runBtn.disabled = true;
    const cont = h("button", { type: "button", class: "btn btn-sm btn-ai" }, icon("play"), h("span", { class: "btn-text", text: "Continue run" }));
    stopBtn.before(cont);
    stopBtn.replaceChildren(icon("minus"), h("span", { class: "btn-text", text: "Stop run" }));
    stopBtn.onclick = async () => { await post(url("stopUrlTemplate", runId)); location.reload(); };
    cont.addEventListener("click", () => {
      cont.remove(); $(".spinner", prog).hidden = false; prog.classList.remove("is-paused"); resetStop();
      const plan = $$(".ev-row").filter((r) => !r.classList.contains("is-out")).map((r) => ({ key: r.dataset.case, title: $(".ev-case-title", r).textContent, done: !!r.dataset.result }));
      const pending = plan.filter((p) => !p.done);
      loop(runId, plan.filter((p) => p.done).concat(pending), plan.filter((p) => p.done).length);
    });
  }

  /* ---------------- Result detail ---------------- */
  const drawer = $("#ev-drawer"), dBody = $("#ev-d-body"), dHead = $("#ev-d-head");
  let current = null, opener = null;
  const RES = { passed: ["ev-passed", "check"], review: ["ev-review", "flag"], failed: ["ev-failed", "x"] };
  const VER = { correct: ["ev-v-correct", "check"], partially_correct: ["ev-v-partially_correct", "minus"], incorrect: ["ev-v-incorrect", "x"] };
  const STATE = { pass: ["is-passed", "check", "Pass"], partial: ["is-adjusted", "minus", "Partial"], fail: ["is-fail", "x", "Fail"] };
  const badge = (cls, ic, text) => h("span", { class: "badge " + cls }, icon(ic), text);
  function sec(id, src, title, label, intro, ...content) {
    return h("section", { class: "xp-sec " + src, id },
      h("div", { class: "xp-sec-head" }, h("span", { class: "xp-sec-icon" }, icon({ "src-fact": "database", "src-expected": "target", "src-ai": "sparkle", "src-rule": "cpu", "src-human": "user" }[src])),
        h("div", { class: "xp-sec-titles" }, h("h3", { class: "xp-sec-title", text: title }), intro ? h("p", { class: "xp-sec-intro", text: intro }) : null),
        h("span", { class: "xp-sec-src", text: label })),
      h("div", { class: "xp-sec-body" }, ...content));
  }
  const facts = (title, rows) => h("div", { class: "xp-block" }, title ? h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: title })) : null,
    h("dl", { class: "xp-facts" }, ...rows.filter(Boolean).map(([k, v, st]) => h("div", { class: "xp-fact is-" + (st || "info") }, h("dt", { text: k }),
      h("dd", {}, h("span", { class: "xp-fact-value" }, h("span", { class: "xp-dot" }), h("span", { text: v == null || v === "" ? "—" : String(v) })))))));
  const block = (title, ...c) => h("div", { class: "xp-block" }, h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: title })), ...c);
  const list = (items) => h("ul", { class: "xp-list" }, ...items.map((i) => h("li", { class: i.tone ? "tone-" + i.tone : null }, h("span", { text: i.text }), i.meta ? h("span", { class: "xp-list-meta", text: i.meta }) : null)));
  const chips = (arr, cls) => h("div", { class: "ev-chips" }, ...arr.map((x) => h("span", { class: "ev-chip " + (cls || ""), text: x })));
  const raw = (label, obj) => h("details", { class: "ev-raw" }, h("summary", {}, icon("file", "icon-xs"), label), h("pre", { text: JSON.stringify(obj, null, 2) }));
  function quote(text) {
    const q = h("blockquote", { class: "xp-quote is-clamped", text });
    const more = h("button", { type: "button", class: "link-btn xp-more", onclick: (e) => { q.classList.toggle("is-clamped"); e.currentTarget.textContent = q.classList.contains("is-clamped") ? "Show all" : "Show less"; } }, "Show all");
    requestAnimationFrame(() => { if (q.scrollHeight <= q.clientHeight + 2) { q.classList.remove("is-clamped"); more.remove(); } });
    return [q, more];
  }

  function render(r) {
    current = r;
    const inp = r.input || {}, ex = r.expected || {}, a = r.actual, cmpd = r.comparison || { checks: [] };
    $("#ev-d-kicker").textContent = `Test case · Run #${r.run ? r.run.id : ""}`;
    const [rc, ri] = RES[r.auto_result];
    dHead.replaceChildren(h("div", { class: "xp-rec" },
      h("h2", { class: "xp-rec-text", id: "ev-d-title", text: r.case_title }),
      r.summary ? h("div", { class: "xp-rec-meta" }, h("span", { text: r.summary })) : null),
      h("div", { class: "xp-prov" }, badge(rc, ri, cfg.results[r.auto_result]),
        r.verdict ? badge(VER[r.verdict][0], VER[r.verdict][1], cfg.verdicts[r.verdict]) : badge("ev-noteval", "user", "Not evaluated"),
        r.response_id ? h("span", { class: "badge source-live" }, icon("check"), "Live from Claude") : null,
        r.model ? h("code", { class: "xp-resp", text: r.model }) : null,
        r.latency_ms ? h("span", { class: "xp-prov-time" }, icon("clock", "icon-xs"), `${(r.latency_ms / 1000).toFixed(1)} s · ${(r.input_tokens || 0) + (r.output_tokens || 0)} tokens`) : null,
        r.response_id ? h("code", { class: "xp-resp", title: "Anthropic message ID", text: r.response_id }) : null));

    const contact = inp.contact_info_on_file || {};
    const input = sec("ev-sec-input", "src-fact", "Input", "Synthetic", "The fictional project record, built into the same context the project page sends.",
      facts("Project record", [["Service", inp.service_type], ["Workflow stage", inp.current_workflow_stage], ["Status", inp.project_status], ["Priority", inp.priority],
        ["Monthly electric bill", inp.monthly_electric_bill_usd != null ? `$${inp.monthly_electric_bill_usd}` : "Not provided", inp.monthly_electric_bill_usd != null ? "info" : "missing"],
        ["Roof age", inp.roof_age_years != null ? `${inp.roof_age_years} years` : "Not provided", inp.roof_age_years != null ? "info" : "missing"],
        ...Object.entries(contact).map(([k, v]) => [k[0].toUpperCase() + k.slice(1), v ? "On file" : "Missing", v ? "ok" : "missing"]),
        ...(inp.documents || []).map((d) => [d.type, d.status, ["Missing", "Requested"].includes(d.status) ? "missing" : "ok"])]),
      inp.customer_notes ? block("Customer notes", ...quote(inp.customer_notes)) : null,
      (r.rule_findings || []).length ? block("Business-rule findings included in the input", list(r.rule_findings.map((t) => ({ text: t })))) : null,
      raw("Exactly what was sent to Claude", inp));

    const expected = sec("ev-sec-expected", "src-expected", "Expected output", "Fixed per case", "Written with the test case, before any run.",
      block("Accepted classification", chips(ex.intent || [])),
      block("Important information the model should detect", (ex.information || []).length ? list(ex.information.map((i) => ({ text: i.label, meta: "Matches any of: " + i.any.join(", ") }))) : h("p", { class: "xp-block-note", text: "None beyond the rule findings." })),
      (ex.must_not_claim || []).length ? block("Must not claim as missing", list(ex.must_not_claim.map((i) => ({ text: i.label, meta: "Fails if reported missing: " + i.any.join(", ") })))) : null,
      block("Accepted action category (team)", chips(ex.departments || [])),
      ex.action_label ? h("p", { class: "xp-block-note", text: "Expected action: " + ex.action_label }) : null);

    const actual = a ? sec("ev-sec-actual", "src-ai", "Actual AI output", "Stored unchanged", "The validated structured output returned by Claude.",
      h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: "Classification (intent)" }), h("span", { class: "xp-ai-value", text: a.detected_intent + (a.intent_detail ? `: ${a.intent_detail}` : "") })),
      h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: "Suggested team" }), h("span", { class: "xp-ai-value", text: a.suggested_department })),
      h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: "Recommended action" }), h("span", { class: "xp-ai-value", text: a.recommended_action })),
      h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: "Information status" }), h("span", { class: "xp-ai-value", text: a.information_status })),
      h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: "Confidence" }), h("span", { class: "xp-ai-value", text: `${Math.round(a.confidence * 100)}%` })),
      h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: "Human review requested" }), h("span", { class: "xp-ai-value", text: a.requires_human_review ? `Yes${a.human_review_reason ? ": " + a.human_review_reason : ""}` : "No" })),
      block("Missing information", (a.missing_information || []).length ? list(a.missing_information.map((m) => ({ text: m.item, meta: m.why_it_matters }))) : h("p", { class: "xp-block-note", text: "None reported." })),
      block("Concerns", (a.concerns || []).length ? list(a.concerns.map((c) => ({ text: `${c.topic}: ${c.detail}`, meta: `${c.severity} · ${c.source.replace(/_/g, " ")}`, tone: { high: "danger", medium: "warning" }[c.severity] }))) : h("p", { class: "xp-block-note", text: "None reported." })),
      block("Summary", h("p", { class: "xp-ai-text", text: a.summary })),
      block("Written justification", h("p", { class: "xp-ai-text", text: a.reasoning }), h("p", { class: "xp-block-note", text: "A short justification from the structured output. The model's internal reasoning is not requested or stored." })),
      raw("Full structured output (JSON)", a))
      : sec("ev-sec-actual", "src-ai", "Actual AI output", "No output", null,
        h("div", { class: "alert alert-error" }, icon("alert"), h("div", {}, h("div", { class: "alert-title", text: `The API call failed (${r.error_code})` }), h("div", { text: r.error_message || "" }))),
        h("p", { class: "xp-block-note", text: "Counted as Failed. No output was invented to replace it." }));

    const compare = sec("ev-sec-compare", "src-rule", "Comparison", "Computed in Python", "Expected against actual. The model does not grade itself.",
      ...(cmpd.checks.length ? cmpd.checks.map((c) => {
        const [cls, ic, lab] = STATE[c.state];
        return h("div", { class: "xp-rule ev-check " + cls },
          h("div", { class: "xp-rule-head" }, h("span", { class: "xp-rule-name", text: c.label }), h("span", { class: "xp-state " + cls }, icon(ic, "icon-xs"), lab)),
          h("div", { class: "xp-rule-logic" }, h("div", {}, h("span", { class: "xp-kw", text: "Exp." }), h("span", { text: c.expected })), h("div", {}, h("span", { class: "xp-kw", text: "Got" }), h("span", { text: c.actual || "—" }))),
          c.items ? h("ul", { class: "xp-rule-sub" }, ...c.items.map((i) => h("li", { class: i.ok ? "is-passed" : "is-fail" },
            h("span", { class: "xp-sub-icon" }, icon(i.ok ? "check" : "x", "icon-xs")),
            h("span", { class: "xp-sub-text" }, h("strong", { text: (i.kind === "must_not" ? "Must not claim: " : "") + i.label }),
              h("span", { text: i.kind === "must_not" ? (i.ok ? "Not reported missing" : `Reported missing (“${i.matched}”)`) : (i.ok ? `Found “${i.matched}” in ${i.where}` : "Not found in missing information or concerns") }))))) : null);
      }) : [h("p", { class: "xp-block-note", text: "No comparison: the API call returned no output." })]),
      h("div", { class: "ev-overall" }, "Automatic result: ", badge(rc, ri, cfg.results[r.auto_result]),
        h("span", { class: "xsmall muted", text: " All checks pass → Passed · one does not → Review required · two or more → Failed" })));

    const human = sec("ev-sec-human", "src-human", "Human evaluation", "Reviewer", "Your judgment is stored beside the automatic result. It never changes the stored output.",
      h("div", { class: "ev-verdicts", role: "radiogroup", "aria-label": "Human evaluation" },
        ...["correct", "partially_correct", "incorrect"].map((k) => [k, cfg.verdicts[k]]).map(([k, lab]) => h("button", { type: "button", class: "ev-verdict " + VER[k][0] + (r.verdict === k ? " is-selected" : ""), role: "radio",
          "aria-checked": r.verdict === k ? "true" : "false", "data-verdict": k, onclick: (e) => pick(e.currentTarget) }, icon(VER[k][1]), lab))),
      h("div", { class: "field" }, h("label", { class: "label", for: "ev-note" }, "Note", h("span", { class: "opt", text: "Optional" })),
        h("textarea", { class: "textarea", id: "ev-note", rows: "2", maxlength: "500", placeholder: "What was right or wrong about this output?" })),
      h("div", { class: "row", style: "justify-content:flex-end" }, h("button", { type: "button", class: "btn btn-primary", id: "ev-save", disabled: true, onclick: save }, icon("check"), h("span", { class: "spinner" }), h("span", { class: "btn-text", text: "Save evaluation" }))),
      (r.reviews || []).length ? block("History", h("ul", { class: "ev-rhist" }, ...r.reviews.slice().reverse().map((v) => h("li", {},
        badge(VER[v.verdict][0], VER[v.verdict][1], cfg.verdicts[v.verdict]), h("span", { class: "xsmall muted", text: `${v.reviewer} · ${when(v.created_at)}` }),
        v.note ? h("span", { class: "ev-rhist-note", text: v.note }) : null)))) : null);

    dBody.replaceChildren(input, expected, actual, compare, human);
    dBody.scrollTop = 0;
    $$(".xp-nav-btn", drawer).forEach((b, i) => b.classList.toggle("is-current", i === 0));
  }
  function pick(btn) {
    $$(".ev-verdict", drawer).forEach((b) => { const on = b === btn; b.classList.toggle("is-selected", on); b.setAttribute("aria-checked", on ? "true" : "false"); });
    $("#ev-save").disabled = false;
  }
  async function save() {
    const sel = $(".ev-verdict.is-selected", drawer); if (!sel || !current) return;
    const btn = $("#ev-save"); setLoading(btn, true, "Saving…");
    const res = await post(url("reviewUrlTemplate", current.id), { verdict: sel.dataset.verdict, note: $("#ev-note").value });
    setLoading(btn, false);
    if (!res.ok) { toast((res.error && res.error.message) || "The evaluation was not saved.", "error"); return; }
    if (res.row_html) { const row = $(`.ev-row[data-case="${CSS.escape(res.case_key)}"]`); if (row) row.replaceWith(fromHTML(res.row_html)); }
    if (res.metrics_html) $("#ev-metrics-wrap").replaceChildren(fromHTML(res.metrics_html));
    toast(`Saved: ${cfg.verdicts[sel.dataset.verdict]}. The stored AI output is unchanged.`, "success");
    open(current.id, true);
  }

  async function open(resultId, keepScroll) {
    const y = dBody.scrollTop;
    if (!drawer.open) { opener = document.activeElement; drawer.showModal(); dHead.replaceChildren(); dBody.replaceChildren(h("div", { class: "xp-loading" }, ...[80, 60, 90, 70].map((w) => h("div", { class: "skeleton", style: `height:14px;width:${w}%` })))); }
    const res = await request(url("resultUrlTemplate", resultId), { headers: { Accept: "application/json" } });
    if (!res.ok) { dBody.replaceChildren(h("div", { class: "alert alert-error" }, icon("alert"), h("div", { text: (res.error && res.error.message) || "Could not load this result." }))); return; }
    render(res.result);
    if (keepScroll) dBody.scrollTop = y; else $("[data-ev-close]", drawer).focus();
  }
  function closeDrawer() { drawer.classList.add("is-closing"); setTimeout(() => { drawer.classList.remove("is-closing"); drawer.close(); }, 160); }
  drawer.addEventListener("close", () => { if (opener && document.contains(opener)) opener.focus(); });
  drawer.addEventListener("cancel", (e) => { e.preventDefault(); closeDrawer(); });
  const topOf = (el) => el.getBoundingClientRect().top - dBody.getBoundingClientRect().top + dBody.scrollTop;
  drawer.addEventListener("click", (e) => {
    if (e.target === drawer || e.target.closest("[data-ev-close]")) { closeDrawer(); return; }
    const j = e.target.closest("[data-ev-jump]");
    if (j) { const t = document.getElementById(j.dataset.evJump); if (t) dBody.scrollTo({ top: topOf(t) - 12, behavior: "smooth" }); }
  });
  dBody.addEventListener("scroll", () => {
    const secs = $$(".xp-sec", dBody); let cur = secs[0];
    secs.forEach((s) => { if (topOf(s) - dBody.scrollTop <= 60) cur = s; });
    if (dBody.scrollTop + dBody.clientHeight >= dBody.scrollHeight - 4) cur = secs[secs.length - 1];
    $$(".xp-nav-btn", drawer).forEach((b) => b.classList.toggle("is-current", cur && b.dataset.evJump === cur.id));
  }, { passive: true });
  $("#ev-tbody").addEventListener("click", (e) => {
    const row = e.target.closest(".ev-row[data-result]");
    if (row) open(Number(row.dataset.result));
  });
  $("#ev-tbody").addEventListener("keydown", (e) => {
    const row = e.target.closest(".ev-row[data-result]");
    if (row && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); open(Number(row.dataset.result)); }
  });
})();
