/* OpsPilot Human Approval Center.
   All AI-written text is rendered with textContent. Decisions are recorded on the server,
   which keeps the original AI output unchanged and stores the human's version beside it. */
(function () {
  "use strict";
  const root = document.getElementById("ac");
  if (!root) return;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const toast = (m, t, ms) => window.opsToast && window.opsToast(m, t, ms);
  const cfg = JSON.parse($("#ac-config").textContent);
  let data = JSON.parse($("#ac-data").textContent);
  let filter = "all", view = "pending", selectedId = null;

  /* ---------------- DOM helpers (text only) ---------------- */
  const SVG = "http://www.w3.org/2000/svg";
  function icon(name, cls) { const s = document.createElementNS(SVG, "svg"); s.setAttribute("class", "icon" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true"); const u = document.createElementNS(SVG, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s; }
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v; else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? "" : v);
    }
    children.flat().forEach((c) => { if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return el;
  }
  const badge = (cls, ic, text, tip) => h("span", { class: "badge " + cls, "data-tip": tip || null }, ic ? icon(ic) : null, text);
  function ago(ts) {
    const d = new Date(ts); if (isNaN(d)) return "—";
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now"; if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago"; if (s < 14 * 86400) return Math.floor(s / 86400) + "d ago";
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  const exact = (ts) => { const d = new Date(ts); return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }); };
  const KIND_CSS = { communication: "ac-k-comm", recommendation: "ac-k-rec", workflow: "ac-k-wf", decision: "ac-k-dec" };
  const DECISION = { approved: ["status-active", "check", "Approved"], approved_with_edits: ["ac-edited", "edit", "Approved with edits"], rejected: ["status-blocked", "x", "Rejected"] };

  async function request(url, opts) {
    try {
      const res = await fetch(url, opts);
      try { return await res.json(); } catch (e) { return { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${res.status}.` } }; }
    } catch (e) { return { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server. Check that it is still running." } }; }
  }
  const post = (url, body) => request(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body) });
  function setLoading(btn, on, text) { if (!btn) return; btn.classList.toggle("is-loading", on); btn.disabled = on; const l = $(".btn-text", btn); if (l) { if (on) { l.dataset.orig = l.dataset.orig || l.textContent; if (text) l.textContent = text; } else if (l.dataset.orig) l.textContent = l.dataset.orig; } }

  /* ---------------- Counts ---------------- */
  function renderCounts() {
    const c = data.counts;
    $("[data-ac-pending]").textContent = c.pending;
    $("[data-ac-decided]").textContent = c.decided_today;
    Object.entries(c.by_filter).forEach(([k, n]) => {
      const a = $(`[data-ac-count="${k}"]`); if (a) a.textContent = n;
      const t = $(`[data-ac-tab-count="${k}"]`); if (t) { t.textContent = n; t.hidden = view !== "pending"; }
    });
    const nav = $("[data-nav-approvals]");
    if (nav) { nav.textContent = c.pending; nav.closest(".nav-meta").hidden = c.pending === 0; }
  }

  /* ---------------- List ---------------- */
  const visible = () => data.items.filter((i) => filter === "all" || i.filter === filter);
  function renderList() {
    const list = $("#ac-list"); list.replaceChildren();
    const items = visible();
    if (!items.length) { list.append(emptyList()); renderDetail(null); return; }
    if (!items.some((i) => i.id === selectedId)) selectedId = items[0].id;
    items.forEach((it) => {
      const conf = it.confidence == null ? null : Math.round(it.confidence * 100);
      const dec = it.decisions && it.decisions[it.decisions.length - 1];
      const row = h("button", { type: "button", class: "ac-row" + (it.id === selectedId ? " is-selected" : ""), role: "option",
        "aria-selected": it.id === selectedId ? "true" : "false", "data-id": it.id, onclick: () => select(it.id) },
        h("span", { class: "ac-row-icon " + KIND_CSS[it.kind] }, icon(it.icon)),
        h("span", { class: "ac-row-main" },
          h("span", { class: "ac-row-top" }, h("span", { class: "ac-row-kind", text: it.kind_label }),
            h("span", { class: "ac-row-time", title: exact(view === "history" ? it.decided_at : it.created_at), text: ago(view === "history" ? it.decided_at : it.created_at) })),
          h("span", { class: "ac-row-title", text: finalTitle(it, dec) }),
          h("span", { class: "ac-row-meta" },
            it.customer ? h("span", { class: "ac-row-customer" }, icon("user", "icon-xs"), it.customer) : null,
            view === "history" && dec ? badge(DECISION[dec.decision][0], DECISION[dec.decision][1], DECISION[dec.decision][2])
              : conf !== null ? h("span", { class: "ac-conf-pill" + (conf < 60 ? " is-low" : ""), title: "Confidence" }, `${conf}%`) : null,
            it.source === "synthetic" ? h("span", { class: "ac-row-synth", title: "Seeded sample, not from Claude", text: "Sample" }) : null)));
      list.append(row);
    });
    renderDetail(data.items.find((i) => i.id === selectedId));
  }
  function finalTitle(it, dec) {
    if (view !== "history" || !dec || !dec.final_output) return it.title;
    const f = dec.final_output;
    return f.subject || f.recommended_action || f.name || it.title;
  }
  function emptyList() {
    const pending = view === "pending";
    const label = { all: "", communications: "communications", recommendations: "recommendations", workflows: "workflow proposals", other: "project decisions" }[filter];
    return h("div", { class: "empty ac-empty" },
      h("div", { class: "empty-icon" }, icon(pending ? "check" : "clock", "icon-lg")),
      h("div", { class: "empty-title", text: pending ? (filter === "all" ? "Nothing waiting for approval" : `No pending ${label}`) : "No decisions yet" }),
      h("p", { class: "empty-text", text: pending ? "When AI drafts a customer message, recommends a next step or proposes a workflow, it lands here for a person to review." : "Approved and rejected items appear here with their full audit record." }),
      pending ? h("button", { type: "button", class: "btn btn-ai", style: "margin-top:8px", onclick: () => $("#ac-draft-modal").showModal() }, icon("sparkle"), "Draft communication") : null);
  }
  function select(id) {
    selectedId = id;
    $$(".ac-row").forEach((r) => { const on = Number(r.dataset.id) === id; r.classList.toggle("is-selected", on); r.setAttribute("aria-selected", on ? "true" : "false"); });
    renderDetail(data.items.find((i) => i.id === id));
  }
  $("#ac-list").addEventListener("keydown", (e) => {
    if (!["ArrowDown", "ArrowUp"].includes(e.key)) return;
    e.preventDefault();
    const items = visible(); const i = items.findIndex((x) => x.id === selectedId);
    const next = items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
    if (next) { select(next.id); const el = $(`.ac-row[data-id="${next.id}"]`); if (el) { el.focus(); el.scrollIntoView({ block: "nearest" }); } }
  });

  /* ---------------- Detail ---------------- */
  const sec = (title, iconName, ...content) => h("section", { class: "ac-sec" }, h("h3", { class: "ac-sec-title" }, iconName ? icon(iconName, "icon-sm") : null, title), ...content);
  function kv(rows) { return h("dl", { class: "ac-kv" }, ...rows.filter(Boolean).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", {}, v)])); }

  function outputBlock(it, out) {
    if (it.kind === "communication") {
      return h("div", { class: "ac-letter" },
        h("div", { class: "ac-letter-head" }, h("span", { class: "ac-letter-label", text: "Subject" }), h("span", { class: "ac-letter-subject", text: out.subject })),
        h("div", { class: "ac-letter-body", text: out.body }));
    }
    if (it.kind === "workflow") {
      return h("div", { class: "ac-rec" },
        h("div", { class: "ac-rec-action", text: out.name }),
        out.summary ? h("p", { class: "secondary", text: out.summary }) : null,
        h("ol", { class: "ac-wf-steps" }, ...out.steps.map((s) => h("li", { class: "wf-" + (s.type === "human_approval" ? "human" : s.type) },
          h("span", { class: "ac-wf-dot" }), h("span", { text: s.title }), s.added_by_policy ? h("span", { class: "ac-wf-policy", text: "Policy" }) : null))),
        it.workflow_url ? h("a", { class: "btn btn-sm", href: it.workflow_url, style: "align-self:flex-start" }, icon("workflow"), "Open in Workflow Builder") : null);
    }
    return h("div", { class: "ac-rec" },
      h("div", { class: "ac-rec-action", text: out.recommended_action }),
      h("div", { class: "small secondary" }, "Suggested owner: ", h("strong", { text: out.suggested_department })),
      out.summary ? h("p", { class: "secondary small", text: out.summary }) : null);
  }

  function evidenceBlock(it) {
    const ev = it.evidence || {};
    if (it.kind === "workflow") {
      return h("div", {}, kv([["Steps", String(ev.steps)], ["Steps using AI", String(ev.ai_steps)], ["Human approvals", String(ev.approval_points)], ["Conditions", String(ev.conditions)]]),
        ev.policy_additions && ev.policy_additions.length ? h("ul", { class: "ac-bullets" }, ...ev.policy_additions.map((p) => h("li", { text: p }))) : null,
        ev.description ? h("blockquote", { class: "ac-quote", text: ev.description }) : null);
    }
    const customer = it.project_url ? h("a", { href: it.project_url, text: ev.customer }) : (ev.customer || "—");
    const missing = ev.missing_information || [];
    return h("div", { class: "stack", style: "gap:10px" },
      kv([["Customer", customer], ["Service", ev.service], ["Stage", ev.stage],
          ["Status", h("span", { class: "badge status-" + String(ev.status || "").toLowerCase().replace(/ /g, "-") }, h("span", { class: "dot" }), ev.status)],
          ["Priority", ev.priority]]),
      missing.length ? h("div", {}, h("div", { class: "ac-mini-label", text: "Still needed" }),
        h("div", { class: "ac-chips" }, ...missing.map((m) => h("span", { class: "ac-chip is-missing" }, icon("alert", "icon-xs"), m)))) : null,
      ev.documents && ev.documents.length ? h("div", {}, h("div", { class: "ac-mini-label", text: "Documents" }),
        h("div", { class: "ac-chips" }, ...ev.documents.map((d) => h("span", { class: "ac-chip" + (["Missing", "Requested"].includes(d.status) ? " is-missing" : " is-ok") },
          icon(["Missing", "Requested"].includes(d.status) ? "x" : "check", "icon-xs"), `${d.type}: ${d.status}`)))) : null,
      ev.rule_findings && ev.rule_findings.length ? h("div", {}, h("div", { class: "ac-mini-label", text: "Business rule findings" }),
        h("ul", { class: "ac-bullets" }, ...ev.rule_findings.map((f) => h("li", { text: f })))) : null,
      ev.open_tasks && ev.open_tasks.length ? h("div", {}, h("div", { class: "ac-mini-label", text: `Open tasks (${ev.open_tasks.length})` }),
        h("ul", { class: "ac-tasks" }, ...ev.open_tasks.map((t) => h("li", {}, h("span", { text: t.title }), h("span", { class: "muted xsmall", text: t.department + (t.overdue ? ", overdue" : "") }))))) : null);
  }

  function confidenceBlock(it) {
    if (it.confidence == null) return h("div", { class: "ac-conf" }, badge("wb-plain", "info", "Not scored"), h("p", { class: "xsmall muted", text: it.confidence_basis }));
    const pct = Math.round(it.confidence * 100);
    return h("div", { class: "ac-conf" },
      h("div", { class: "confidence" }, h("span", { class: "confidence-bar" }, h("span", { style: `width:${pct}%` })), h("span", { class: "num strong", text: pct + "%" }),
        h("span", { class: "small secondary", text: pct >= 80 ? "High" : pct >= 60 ? "Medium" : "Low" })),
      h("p", { class: "xsmall muted", text: it.confidence_basis + " A signal, not a guarantee." }));
  }

  function renderDetail(it) {
    const box = $("#ac-detail"); box.replaceChildren();
    if (!it) {
      box.append(h("div", { class: "empty ac-detail-empty" }, h("div", { class: "empty-icon" }, icon("stamp", "icon-lg")),
        h("div", { class: "empty-title", text: view === "pending" ? "Select an item to review" : "Select a decision to see its audit record" })));
      return;
    }
    const just = it.justification || { points: [], rationale: "", needs_judgment: [] };
    const dec = it.decisions && it.decisions[it.decisions.length - 1];
    box.append(...[
      h("header", { class: "ac-detail-head" },
        h("div", { class: "row wrap", style: "gap:6px" },
          h("span", { class: "badge ac-kind " + KIND_CSS[it.kind] }, icon(it.icon), it.kind_label),
          it.source === "live" ? badge("source-live", "check", "Live from Claude", it.model ? "Model " + it.model : null)
            : badge("source-synthetic", "info", "Synthetic sample", "Seeded for the demo. Not produced by the Anthropic API."),
          h("button", { type: "button", class: "why-btn is-lg ac-why", "data-explain": `/api/explain/approval/${it.id}`,
            "aria-label": "Why? Explain this item: evidence, AI interpretation, business rules and the human decision" }, icon("info"), "Why?")),
        h("h2", { class: "ac-detail-title", text: finalTitle(it, dec) }),
        h("div", { class: "ac-detail-meta" },
          it.customer ? (it.project_url ? h("a", { href: it.project_url }, icon("user", "icon-sm"), it.customer) : h("span", {}, it.customer)) : null,
          h("span", { title: exact(it.created_at) }, icon("clock", "icon-sm"), "Created " + ago(it.created_at)))),
      h("div", { class: "ac-detail-body" },
        view === "history" && dec ? decisionRecord(it, dec) : null,
        sec(view === "history" && dec && dec.final_output ? "Original AI recommendation" : "AI recommendation", "sparkle", outputBlock(it, it.ai_output)),
        sec("Justification", "info",
          just.points && just.points.length ? h("ul", { class: "ac-bullets" }, ...just.points.map((p) => h("li", { text: p }))) : null,
          just.rationale ? h("p", { class: "secondary", text: just.rationale }) : null,
          just.needs_judgment && just.needs_judgment.length ? h("div", { class: "ac-judgment" }, h("div", { class: "ac-mini-label" }, icon("shield", "icon-xs"), "Why a person must decide"),
            h("ul", { class: "ac-bullets" }, ...just.needs_judgment.map((p) => h("li", { text: p })))) : null,
          h("p", { class: "xsmall muted", text: "A short justification written for reviewers. The model's internal reasoning is not stored or shown." })),
        sec("Relevant business data", "database", evidenceBlock(it)),
        h("div", { class: "ac-two" },
          sec("Confidence", "target", confidenceBlock(it)),
          it.checks ? sec("OpsPilot checks", "cpu", h("ul", { class: "ac-checks" }, ...it.checks.map((c) =>
            h("li", { class: c.ok ? "is-ok" : "is-warn" }, icon(c.ok ? "check" : "alert", "icon-sm"), h("span", {}, c.label, c.detail ? h("span", { class: "ac-check-detail", text: c.detail }) : null))))) : null),
        view === "pending" ? sec("Potential action", "play", h("div", { class: "ac-effects" },
          h("div", { class: "ac-effect-row is-approve" }, h("span", { class: "ac-effect-label" }, icon("check", "icon-sm"), "If you approve"), h("span", { text: it.potential_action })),
          h("div", { class: "ac-effect-row is-reject" }, h("span", { class: "ac-effect-label" }, icon("x", "icon-sm"), "If you reject"), h("span", { text: it.reject_effect })))) : null,
        it.ai_input ? h("details", { class: "ac-sent" }, h("summary", {}, icon("shield", "icon-sm"), "Data sent to Claude for this draft"),
          h("dl", { class: "ac-kv ac-kv-code" }, ...Object.entries(it.ai_input).flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: Array.isArray(v) ? (v.join(", ") || "none") : String(v) })])),
          h("p", { class: "xsmall muted", text: "No last name, email, phone, address, bill amount, roof age or notes are sent." })) : null,
        it.response_id ? h("div", { class: "provenance" }, h("span", {}, "Model ", h("code", { text: it.model || "" })), h("span", {}, "Response ", h("code", { text: it.response_id }))) : null),
      view === "pending" ? h("footer", { class: "ac-actions" },
        h("span", { class: "ac-actions-hint" }, icon("shield", "icon-sm"), "Your decision is recorded with the original AI output."),
        h("button", { type: "button", class: "btn btn-danger", onclick: () => confirmDecision(it, "reject") }, icon("x"), "Reject"),
        h("button", { type: "button", class: "btn", onclick: () => openEdit(it) }, icon("edit"), "Edit"),
        h("button", { type: "button", class: "btn btn-primary", onclick: () => confirmDecision(it, "approve") }, icon("check"), "Approve")) : null].filter(Boolean));
    box.scrollTop = 0;
  }

  function decisionRecord(it, dec) {
    const [cls, ic, label] = DECISION[dec.decision];
    return h("section", { class: "ac-record" },
      h("div", { class: "row wrap", style: "gap:8px" }, badge(cls, ic, label),
        h("span", { class: "small secondary" }, `by ${dec.decided_by} via ${dec.decided_via}, `, h("span", { title: exact(dec.created_at), text: ago(dec.created_at) }))),
      dec.outcome ? h("p", { class: "small", text: dec.outcome }) : null,
      dec.note ? h("blockquote", { class: "ac-quote", text: dec.note }) : null,
      dec.final_output ? h("div", {}, h("div", { class: "ac-mini-label" }, icon("edit", "icon-xs"), `Human correction (${(dec.changed_fields || []).join(", ").replace(/_/g, " ")})`), outputBlock(it, dec.final_output)) : null);
  }

  /* ---------------- Approve / reject ---------------- */
  const confirmModal = $("#ac-confirm-modal");
  let pending = null;
  function confirmDecision(it, decision) {
    pending = { it, decision, edits: null };
    const noun = { communication: "message", recommendation: "recommendation", workflow: "workflow", decision: "decision" }[it.kind];
    $("#ac-confirm-title").textContent = decision === "approve" ? `Approve this ${noun}?` : `Reject this ${noun}?`;
    $("#ac-confirm-desc").textContent = it.title;
    const eff = $("#ac-confirm-effect"); eff.replaceChildren(
      h("div", { class: "ac-effect-row " + (decision === "approve" ? "is-approve" : "is-reject") },
        h("span", { class: "ac-effect-label" }, icon(decision === "approve" ? "check" : "x", "icon-sm"), "What happens"),
        h("span", { text: decision === "approve" ? it.potential_action : it.reject_effect })));
    $("#ac-confirm-note").value = "";
    $("#ac-confirm-note").placeholder = decision === "approve" ? "Anything worth recording" : "Why you rejected it (recommended)";
    $("#ac-confirm-note-opt").textContent = decision === "approve" ? "Optional" : "Recommended";
    const go = $("#ac-confirm-go");
    go.className = "btn " + (decision === "approve" ? "btn-primary" : "btn-danger");
    go.replaceChildren(icon(decision === "approve" ? "check" : "x"), h("span", { class: "spinner" }), h("span", { class: "btn-text", text: decision === "approve" ? "Approve" : "Reject" }));
    confirmModal.showModal(); go.focus();
  }
  $("#ac-confirm-go").addEventListener("click", () => submitDecision($("#ac-confirm-go"), $("#ac-confirm-note").value, confirmModal));

  async function submitDecision(btn, note, modal) {
    if (!pending) return;
    setLoading(btn, true, "Recording…");
    const res = await post(root.dataset.decideUrlTemplate.replace("/0/", `/${pending.it.id}/`),
      { decision: pending.decision, edits: pending.edits, note });
    setLoading(btn, false);
    if (!res.ok) { toast((res.error && res.error.message) || "The decision was not recorded.", "error"); return; }
    modal.close();
    const idx = visible().findIndex((x) => x.id === pending.it.id);
    data.items = data.items.filter((x) => x.id !== pending.it.id);
    data.counts = res.counts;
    const rest = visible(); selectedId = rest.length ? rest[Math.min(idx, rest.length - 1)].id : null;
    renderCounts(); renderList();
    toast(res.decision === "approved_with_edits" ? `${res.outcome} Your edits are saved beside the original AI output.` : res.outcome, "success");
    pending = null;
  }

  /* ---------------- Edit ---------------- */
  const editModal = $("#ac-edit-modal");
  const FIELDS = {
    communication: [["subject", "Subject", "input"], ["body", "Message", "textarea"]],
    recommendation: [["recommended_action", "Recommended action", "input"], ["suggested_department", "Owner", "select"]],
    decision: [["recommended_action", "Recommended action", "input"], ["suggested_department", "Owner", "select"]],
    workflow: [["name", "Workflow name", "input"]],
  };
  function openEdit(it) {
    pending = { it, decision: "approve", edits: null };
    $("#ac-edit-original").replaceChildren(outputBlock(it, it.ai_output));
    const fields = $("#ac-edit-fields"); fields.replaceChildren();
    FIELDS[it.kind].forEach(([key, label, type]) => {
      const id = "ac-edit-" + key;
      let control;
      if (type === "select") {
        control = h("select", { class: "select", id }, ...cfg.departments.map((d) => h("option", { value: d, text: d })));
        control.value = it.ai_output[key];
      } else if (type === "textarea") {
        control = h("textarea", { class: "textarea ac-edit-body", id, rows: "12", maxlength: "2500" }); control.value = it.ai_output[key];
      } else {
        control = h("input", { class: "input", id, maxlength: key === "subject" ? "150" : "200" }); control.value = it.ai_output[key];
      }
      control.dataset.key = key;
      control.addEventListener("input", updateChanges); control.addEventListener("change", updateChanges);
      fields.append(h("div", { class: "field" }, h("label", { class: "label", for: id, text: label }), control));
    });
    if (it.kind === "workflow") fields.append(h("p", { class: "xsmall muted", text: "Workflow steps can't be edited here. Reject it to return it to Draft, then change it in the Workflow Builder." }));
    $("#ac-edit-note").value = "";
    updateChanges(); editModal.showModal();
    const first = $("input, textarea, select", fields); if (first) first.focus();
  }
  function currentEdits() {
    const out = {}; $$("#ac-edit-fields [data-key]").forEach((c) => { out[c.dataset.key] = c.value; }); return out;
  }
  function updateChanges() {
    if (!pending) return;
    const e = currentEdits(), orig = pending.it.ai_output;
    const changed = Object.keys(e).filter((k) => e[k].trim() !== String(orig[k]).trim());
    $$("#ac-edit-fields [data-key]").forEach((c) => c.classList.toggle("is-changed", changed.includes(c.dataset.key)));
    $("#ac-edit-changes").textContent = changed.length ? `${changed.length} field${changed.length === 1 ? "" : "s"} changed` : "No changes yet";
    $("#ac-edit-changes").classList.toggle("is-changed", changed.length > 0);
    $("#ac-edit-approve").disabled = changed.length === 0 || Object.values(e).some((v) => !v.trim());
  }
  $("#ac-edit-reset").addEventListener("click", () => {
    $$("#ac-edit-fields [data-key]").forEach((c) => { c.value = pending.it.ai_output[c.dataset.key]; }); updateChanges();
  });
  $("#ac-edit-approve").addEventListener("click", () => { pending.edits = currentEdits(); submitDecision($("#ac-edit-approve"), $("#ac-edit-note").value, editModal); });

  /* ---------------- Filters and views ---------------- */
  $$("[data-ac-filter]").forEach((b) => b.addEventListener("click", () => {
    filter = b.dataset.acFilter; $$("[data-ac-filter]").forEach((x) => x.classList.toggle("is-active", x === b)); selectedId = null; renderList();
  }));
  $$("[data-ac-view]").forEach((b) => b.addEventListener("click", async () => {
    if (b.dataset.acView === view) return;
    view = b.dataset.acView; $$("[data-ac-view]").forEach((x) => x.classList.toggle("is-active", x === b));
    $("#ac-list").replaceChildren(h("div", { class: "ac-list-loading" }, h("span", { class: "spinner" }), "Loading…"));
    const res = await request(root.dataset.listUrl + "?view=" + view, { headers: { Accept: "application/json" } });
    if (!res.ok) { toast("Could not load the queue.", "error"); return; }
    data = res; selectedId = null; renderCounts(); renderList();
  }));

  /* ---------------- Draft a communication ---------------- */
  const draftModal = $("#ac-draft-modal"), draftGo = $("#ac-draft-go"), draftAlert = $("#ac-draft-alert");
  draftModal.addEventListener("close", () => { draftAlert.replaceChildren(); setLoading(draftGo, false); });
  $("#ac-draft-project").addEventListener("change", (e) => {
    const opt = e.target.selectedOptions[0];
    const purpose = opt && opt.dataset.missing ? "missing_information" : (opt && opt.dataset.roof ? "roof_concern" : "status_update");
    const r = $(`input[name="ac-purpose"][value="${purpose}"]`); if (r) r.checked = true;
  });
  async function draft() {
    const projectId = Number($("#ac-draft-project").value);
    const purpose = ($('input[name="ac-purpose"]:checked') || {}).value;
    draftAlert.replaceChildren();
    setLoading(draftGo, true, "Drafting with Claude…");
    const res = await post(root.dataset.draftUrl, { project_id: projectId, purpose });
    setLoading(draftGo, false);
    if (!res.ok) {
      const err = res.error || {};
      draftAlert.append(h("div", { class: "alert alert-error", role: "alert" }, icon("alert"),
        h("div", { class: "grow" }, h("div", { class: "alert-title", text: "AI drafting temporarily unavailable." }), h("div", { text: `${err.message || ""}${err.code ? ` (${err.code})` : ""}` })),
        h("button", { type: "button", class: "btn btn-sm", onclick: draft }, icon("refresh"), "Retry")));
      return;
    }
    draftModal.close();
    if (view !== "pending") { view = "pending"; $$("[data-ac-view]").forEach((x) => x.classList.toggle("is-active", x.dataset.acView === "pending")); const r = await request(root.dataset.listUrl, { headers: { Accept: "application/json" } }); if (r.ok) data = r; }
    else { data.items.push(res.item); data.counts = res.counts; }
    filter = "all"; $$("[data-ac-filter]").forEach((x) => x.classList.toggle("is-active", x.dataset.acFilter === "all"));
    selectedId = res.item.id; renderCounts(); renderList();
    const row = $(`.ac-row[data-id="${res.item.id}"]`); if (row) row.scrollIntoView({ block: "nearest" });
    toast(`Draft ready for review. Drafted by Claude in ${(res.meta.latency_ms / 1000).toFixed(1)}s. Nothing was sent.`, "success");
  }
  draftGo.addEventListener("click", draft);
  $("#ac-draft-project").dispatchEvent(new Event("change"));

  /* ---------------- Sizing ---------------- */
  const split = $(".ac-split");
  function size() {
    const top = split.getBoundingClientRect().top + window.scrollY;
    const pad = parseFloat(getComputedStyle(document.getElementById("content")).paddingBottom) || 24;
    split.style.height = Math.max(440, window.innerHeight - top - pad) + "px";
  }
  window.addEventListener("resize", size);
  const wanted = Number(new URLSearchParams(window.location.search).get("item"));  // e.g. linked from the dashboard
  if (wanted && data.items.some((i) => i.id === wanted)) selectedId = wanted;
  renderCounts(); renderList(); size();
  if (selectedId) { const row = $(`.ac-row[data-id="${selectedId}"]`); if (row) row.scrollIntoView({ block: "nearest" }); }
})();
