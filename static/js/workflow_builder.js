/* OpsPilot Workflow Builder.
   Renders ONLY validated data from the server. Every piece of model-written text is set with
   textContent; no model output is ever inserted as HTML. Layout coordinates come from Python. */
(function () {
  "use strict";
  const root = document.getElementById("wb");
  if (!root) return;

  const $ = (s, el = document) => el.querySelector(s);
  const toast = (m, t, ms) => window.opsToast && window.opsToast(m, t, ms);
  const cfg = JSON.parse($("#wb-config").textContent);
  let design = JSON.parse($("#wb-data").textContent);   // null on /workflows/new
  let selected = null, scale = 1, userZoomed = false;

  const els = {
    name: $("#wb-name"), meta: $("#wb-meta"), actions: $("#wb-actions"), describe: $("#wb-describe"),
    desc: $("#wb-desc"), preview: $("#wb-desc-preview"), gen: $("#wb-generate"), genNote: $("#wb-generate-note"),
    alert: $("#wb-describe-alert"), canvas: $("#wb-canvas"), wrap: $("#wb-stage-wrap"), stage: $("#wb-stage"),
    empty: $("#wb-empty"), loading: $("#wb-loading"), panel: $("#wb-panel"), zoom: $("#wb-zoom-level"),
    foot: $("#wb-canvas-foot"), main: $(".wb-main"),
  };

  /* ---------------- tiny DOM helpers (text only) ---------------- */
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    children.flat().forEach((c) => { if (c !== null && c !== undefined && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return el;
  }
  const SVG = "http://www.w3.org/2000/svg";
  function icon(name, cls) {
    const s = document.createElementNS(SVG, "svg"); s.setAttribute("class", "icon" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true");
    const u = document.createElementNS(SVG, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s;
  }
  function badge(cls, iconName, text, tip) {
    return h("span", { class: "badge " + cls, "data-tip": tip || null }, iconName ? icon(iconName) : null, text);
  }
  function parseTs(ts) { return ts ? new Date(ts.length === 19 ? ts : ts) : null; }
  function ago(ts) {
    const d = parseTs(ts); if (!d || isNaN(d)) return "—";
    const s = Math.floor((Date.now() - d.getTime()) / 1000);
    if (s < 60) return "just now";
    if (s < 3600) return Math.floor(s / 60) + "m ago";
    if (s < 86400) return Math.floor(s / 3600) + "h ago";
    if (s < 14 * 86400) return Math.floor(s / 86400) + "d ago";
    return d.toLocaleDateString([], { month: "short", day: "numeric" });
  }
  function exact(ts) { const d = parseTs(ts); return d && !isNaN(d) ? d.toLocaleString([], { month: "short", day: "numeric", year: "numeric", hour: "numeric", minute: "2-digit" }) : ""; }
  const T = (type) => cfg.node_types[type] || { label: type, icon: "info", css: "" };
  const ORDERED = cfg.type_order.map((k) => [k, cfg.node_types[k]]);  // Trigger → AI → Condition → Automation → Human → Action

  const STATUS = {
    null: { cls: "wf-status-unsaved", icon: "info", text: "Unsaved proposal" },
    "Draft": { cls: "wf-status-draft", icon: "edit", text: "Draft" },
    "Pending Approval": { cls: "wf-status-pending", icon: "clock", text: "Pending Approval" },
    "Active": { cls: "wf-status-active", icon: "check", text: "Active" },
    "Disabled": { cls: "wf-status-disabled", icon: "power", text: "Disabled" },
  };
  const editable = () => !design || design.status === null || design.status === "Draft";

  /* ---------------- Header ---------------- */
  function renderHeader() {
    els.name.readOnly = !editable() || !design;
    els.name.value = design ? design.name : "";
    els.name.placeholder = design ? "Untitled workflow" : "New workflow";
    els.name.classList.toggle("is-locked", !!design && !editable());
    els.meta.replaceChildren();
    els.actions.replaceChildren();
    if (!design) {
      els.meta.append(h("span", { class: "muted small", text: "Describe a workflow below to generate a proposal." }));
      return;
    }
    const st = STATUS[design.status] || STATUS[null];
    els.meta.append(
      badge(st.cls, st.icon, st.text, design.status === null ? "AI's proposal. Save it as a draft to keep it." : null),
      h("span", { class: "wb-meta-item", "data-tip": exact(design.created_at) }, "Created ", ago(design.created_at)),
      h("span", { class: "wb-meta-item", "data-tip": exact(design.updated_at) }, "Updated ", ago(design.updated_at)),
      design.model ? badge("source-live", "check", "Proposed by Claude", "Model " + design.model) : null,
      whyBtn(`/api/explain/workflow/${design.id}`, "Why? Explain this workflow proposal"),
    );
    const btn = (label, iconName, cls, action, opts = {}) =>
      h("button", { type: "button", class: "btn " + (cls || ""), "data-action": action, onclick: () => doAction(action, opts) },
        icon(iconName), h("span", { class: "spinner" }), h("span", { class: "btn-text", text: label }));
    switch (design.status) {
      case null:
        els.actions.append(h("span", { class: "xsmall muted wb-action-hint", text: "Not saved yet" }),
                           btn("Save draft", "bookmark", "btn-primary", "save_draft"));
        break;
      case "Draft":
        els.actions.append(btn("Save draft", "bookmark", "", "save_draft"),
                           btn("Request approval", "send", "btn-primary", "request_approval", {
                             confirm: ["Request approval", "Send this workflow for approval? It will be locked for editing while it waits.", "Request approval"] }));
        break;
      case "Pending Approval":
        els.actions.append(btn("Return to draft", "rotate", "", "return_to_draft"),
                           btn("Approve and activate", "shield", "btn-primary", "approve", {
                             confirm: ["Approve and activate", "You are approving this AI-proposed workflow as a person. Review every step, especially the human approval points, before continuing. (Prototype: Active workflows are approved designs and are not executed.)", "Approve and activate"] }));
        break;
      case "Active":
        els.actions.append(btn("Disable", "power", "btn-danger", "disable", {
          confirm: ["Disable workflow", "Disable this workflow? It will stop being Active. Turning it back on requires a new approval.", "Disable"] }));
        break;
      case "Disabled":
        els.actions.append(btn("Request approval", "send", "btn-primary", "request_approval", {
          confirm: ["Request approval", "Re-enabling needs a fresh approval. Send it for approval now?", "Request approval"] }));
        break;
    }
  }

  function confirmDialog(title, message, actionLabel) {
    return new Promise((resolve) => {
      const yes = h("button", { type: "button", class: "btn btn-primary", text: actionLabel });
      const no = h("button", { type: "button", class: "btn", text: "Cancel" });
      const d = h("dialog", { class: "modal" },
        h("div", { class: "modal-header" }, h("div", {}, h("div", { class: "modal-title", text: title }))),
        h("div", { class: "modal-body" }, h("p", { class: "secondary", text: message })),
        h("div", { class: "modal-footer" }, no, yes));
      let result = false;
      yes.addEventListener("click", () => { result = true; d.close(); });
      no.addEventListener("click", () => d.close());
      d.addEventListener("close", () => { d.remove(); resolve(result); });
      document.body.append(d); d.showModal(); yes.focus();
    });
  }

  async function doAction(action, opts) {
    if (!design) return;
    if (opts.confirm && !(await confirmDialog(...opts.confirm))) return;
    const button = $(`[data-action="${action}"]`, els.actions);
    setLoading(button, true);
    const data = await post(root.dataset.statusUrlTemplate.replace("/0/", `/${design.id}/`), { action, name: els.name.value.trim() });
    setLoading(button, false);
    if (!data.ok) { toast((data.error && data.error.message) || "That action couldn't be completed.", "error"); return; }
    const before = design.status;
    design = data.design;
    renderHeader(); renderTitles(); renderPanel(); syncDescribe();
    const msg = { save_draft: before === null ? "Saved as a draft. It now appears in Workflows." : "Draft saved.",
                  request_approval: "Approval requested. The workflow is locked while it waits.",
                  approve: "Approved by you and now Active.", return_to_draft: "Returned to draft for changes.",
                  disable: "Workflow disabled." }[action];
    toast(msg, "success");
  }

  /* ---------------- Network ---------------- */
  async function post(url, body) {
    try {
      const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify(body || {}) });
      try { return await res.json(); } catch (e) { return { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${res.status}.` } }; }
    } catch (e) { return { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server. Check that it is still running." } }; }
  }
  function setLoading(btn, on, text) {
    if (!btn) return;
    btn.classList.toggle("is-loading", on); btn.disabled = on;
    const l = $(".btn-text", btn);
    if (l) { if (on) { l.dataset.orig = l.dataset.orig || l.textContent; if (text) l.textContent = text; } else if (l.dataset.orig) l.textContent = l.dataset.orig; }
  }

  /* ---------------- Description + generation ---------------- */
  function syncDescribe() {
    const canGen = editable();
    els.desc.readOnly = !canGen;
    els.gen.hidden = !canGen;
    $$("[data-wb-example]").forEach((b) => { b.hidden = !canGen; });
    els.genNote.textContent = canGen
      ? (design ? "Regenerating replaces this proposal. It stays a draft until someone requests approval."
                : "AI proposes the workflow. OpsPilot validates it, and a person approves it before it can be Active.")
      : `This workflow is ${design.status}. Return it to draft to change the description.`;
    $(".btn-text", els.gen).textContent = design ? "Regenerate proposal" : "Generate workflow";
    if (design) els.preview.textContent = design.description.length > 140 ? design.description.slice(0, 140) + "…" : design.description;
    updateCount();
  }
  function $$(s, el = document) { return Array.from(el.querySelectorAll(s)); }
  function updateCount() { const c = $("[data-wb-count]"); if (c) c.textContent = els.desc.value.length.toLocaleString(); }
  els.desc.addEventListener("input", () => { updateCount(); clearFieldError(); });

  $$("[data-wb-example]").forEach((b) => b.addEventListener("click", () => {
    const ex = cfg.examples[Number(b.dataset.wbExample)];
    els.desc.value = ex.text;
    if (!design || !els.name.value.trim()) els.name.value = ex.name;
    if (!design) els.name.readOnly = false;
    updateCount(); clearFieldError(); els.desc.focus();
  }));

  function clearFieldError() { const f = $("#wb-desc-field"); f.classList.remove("is-invalid"); const e = $(".error-text", f); if (e) e.remove(); $("#wb-desc-help").hidden = false; }
  function fieldError(msg) {
    const f = $("#wb-desc-field"); f.classList.add("is-invalid");
    const e = h("span", { class: "error-text" }, icon("alert", "icon-sm"), h("span", { text: msg }));
    const old = $(".error-text", f); if (old) old.remove();
    $(".row-between", f).prepend(e); $("#wb-desc-help").hidden = true; els.desc.focus();
  }
  function describeAlert(kind, title, message, retry) {
    els.alert.replaceChildren();
    if (!title) return;
    const box = h("div", { class: `alert alert-${kind} wb-alert`, role: kind === "error" ? "alert" : "status" },
      icon(kind === "error" ? "alert" : "info"),
      h("div", { class: "grow" }, h("div", { class: "alert-title", text: title }), h("div", { text: message || "" })),
      retry ? h("button", { type: "button", class: "btn btn-sm", onclick: generate }, icon("refresh"), "Retry") : null);
    els.alert.append(box);
  }

  async function generate() {
    const text = els.desc.value.trim();
    if (text.length < cfg.min_chars) { els.describe.open = true; fieldError(`Describe the workflow in a bit more detail (at least ${cfg.min_chars} characters).`); return; }
    describeAlert();
    setLoading(els.gen, true, "Generating…");
    els.loading.hidden = false; els.empty.hidden = true;
    const steps = $$("li", els.loading); steps.forEach((li, i) => { li.className = i === 0 ? "is-active" : ""; });
    const timer = setTimeout(() => { steps[0].className = "is-done"; steps[1].className = "is-active"; }, 1200);
    const data = await post(root.dataset.generateUrl, { description: text, name: els.name.value.trim(), design_id: design && design.status !== undefined ? design.id : null });
    clearTimeout(timer);
    setLoading(els.gen, false);
    els.loading.hidden = true;
    if (data.ok && data.status === "valid_workflow") {
      design = data.design; selected = null; userZoomed = false;
      history.replaceState(null, "", data.url);
      document.title = `${design.name} | OpsPilot AI`;
      els.describe.open = false;
      renderAll();
      toast(`Workflow proposed by Claude in ${(data.meta.latency_ms / 1000).toFixed(1)}s and validated by OpsPilot.`, "success");
      return;
    }
    els.empty.hidden = !!design;
    if (data.ok) {
      describeAlert("warning", data.status === "not_a_workflow" ? "That doesn't look like a workflow" : "Add a little more detail", data.message);
    } else if (data.error && data.error.code === "invalid_input") {
      fieldError(data.error.message);
    } else {
      const err = data.error || {};
      describeAlert("error", "Workflow generation temporarily unavailable.", `${err.message || ""}${err.code ? ` (${err.code})` : ""}`, true);
      toast("Workflow generation temporarily unavailable.", "error");
    }
  }
  els.gen.addEventListener("click", generate);

  /* ---------------- Canvas ---------------- */
  function roundedPath(pts, r = 10) {
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 1; i < pts.length - 1; i++) {
      const [px, py] = pts[i - 1], [cx, cy] = pts[i], [nx, ny] = pts[i + 1];
      const l1 = Math.hypot(cx - px, cy - py), l2 = Math.hypot(nx - cx, ny - cy);
      const rr = Math.min(r, l1 / 2, l2 / 2);
      if (rr < 0.5) { d += ` L${cx},${cy}`; continue; }
      const ax = cx - ((cx - px) / l1) * rr, ay = cy - ((cy - py) / l1) * rr;
      const bx = cx + ((nx - cx) / l2) * rr, by = cy + ((ny - cy) / l2) * rr;
      d += ` L${ax},${ay} Q${cx},${cy} ${bx},${by}`;
    }
    // stop 3px short of the node along the last segment, so the arrowhead meets the border cleanly
    const [lx, ly] = pts[pts.length - 1], [qx, qy] = pts[pts.length - 2];
    const len = Math.hypot(lx - qx, ly - qy) || 1;
    return d + ` L${lx - ((lx - qx) / len) * 3},${ly - ((ly - qy) / len) * 3}`;
  }

  function renderCanvas() {
    els.stage.replaceChildren();
    if (!design) { els.empty.hidden = false; els.foot.replaceChildren(); return; }
    els.empty.hidden = true;
    const def = design.definition, L = def.layout;
    els.stage.style.width = L.width + "px"; els.stage.style.height = L.height + "px";

    const svg = document.createElementNS(SVG, "svg");
    svg.setAttribute("class", "wb-edges"); svg.setAttribute("width", L.width); svg.setAttribute("height", L.height);
    svg.setAttribute("viewBox", `0 0 ${L.width} ${L.height}`);
    const defs = document.createElementNS(SVG, "defs");
    [["wb-arrow", "wb-arrow-head"], ["wb-arrow-active", "wb-arrow-head is-active"]].forEach(([id, cls]) => {
      const m = document.createElementNS(SVG, "marker");
      m.setAttribute("id", id); m.setAttribute("viewBox", "0 0 10 10"); m.setAttribute("refX", "8"); m.setAttribute("refY", "5");
      m.setAttribute("markerWidth", "7"); m.setAttribute("markerHeight", "7"); m.setAttribute("orient", "auto-start-reverse");
      const p = document.createElementNS(SVG, "path"); p.setAttribute("d", "M0,1 L9,5 L0,9 z"); p.setAttribute("class", cls);
      m.append(p); defs.append(m);
    });
    svg.append(defs);
    (L.loops || []).forEach((e) => {
      const p = document.createElementNS(SVG, "path");
      p.setAttribute("d", roundedPath(e.points, 12));
      p.setAttribute("class", "wb-edge wb-loop"); p.setAttribute("marker-end", "url(#wb-arrow)");
      p.dataset.from = e.from; p.dataset.to = e.to;
      svg.append(p);
    });
    L.edges.forEach((e) => {
      const p = document.createElementNS(SVG, "path");
      p.setAttribute("d", roundedPath(e.points)); p.setAttribute("class", "wb-edge");
      p.setAttribute("marker-end", "url(#wb-arrow)");
      p.dataset.from = e.from; p.dataset.to = e.to;
      svg.append(p);
    });
    els.stage.append(svg);

    (L.loops || []).filter((e) => e.label && e.label_at).forEach((e) => {
      els.stage.append(h("span", { class: "wb-edge-label is-loop", style: `left:${e.label_at.x}px;top:${e.label_at.y}px`,
        "data-from": e.from, "data-to": e.to, text: e.label }));
    });
    L.edges.filter((e) => e.label && e.label_at).forEach((e) => {
      els.stage.append(h("span", { class: "wb-edge-label" + (e.branch === 0 ? " is-negative" : e.branch === 2 ? " is-positive" : ""),
        style: `left:${e.label_at.x}px;top:${e.label_at.y}px`, "data-from": e.from, "data-to": e.to, text: e.label }));
    });

    def.nodes.forEach((n) => {
      const pos = L.positions[n.id]; if (!pos) return;
      const t = T(n.type);
      const chips = h("span", { class: "wb-node-chips" });
      if (n.ai_involvement !== "none" && n.type !== "ai") chips.append(h("span", { class: "wb-chip wf-ai", title: "AI " + n.ai_involvement }, icon("sparkle", "icon-xs"), "AI"));
      if (n.requires_human_approval && n.type !== "human_approval") chips.append(h("span", { class: "wb-chip wf-human", title: "Needs human approval" }, icon("shield", "icon-xs"), "Approval"));
      if (n.returns_to && n.returns_to.length) chips.append(h("span", { class: "wb-chip", title: "Returns to " + n.returns_to.map((r) => r.title).join(", ") }, icon("rotate", "icon-xs"), "Loops back"));
      if (n.added_by_policy) chips.append(h("span", { class: "wb-chip wf-policy", title: "Added by OpsPilot's approval policy" }, icon("cpu", "icon-xs"), "Policy"));
      const node = h("button", { type: "button", class: `wb-node ${t.css}`, style: `left:${pos.x}px;top:${pos.y}px;width:${L.node_w}px;height:${L.node_h}px`,
        "data-id": n.id, "aria-label": `${t.label}: ${n.title}`, onclick: (ev) => { ev.stopPropagation(); select(n.id); } },
        h("span", { class: "wb-node-icon" }, icon(t.icon)),
        h("span", { class: "wb-node-body" },
          h("span", { class: "wb-node-type", text: t.label }),
          h("span", { class: "wb-node-title", text: n.title }),
          chips));
      els.stage.append(node);
    });

    const c = def.counts || {};
    els.foot.replaceChildren(
      h("span", {}, `${def.nodes.length} steps`),
      h("span", {}, icon("sparkle", "icon-sm"), `${def.ai_nodes} involve AI`),
      h("span", {}, icon("shield", "icon-sm"), `${def.approval_points} human approval${def.approval_points === 1 ? "" : "s"}`),
      h("span", {}, icon("split", "icon-sm"), `${c.condition || 0} condition${c.condition === 1 ? "" : "s"}`),
      h("span", { class: "wb-foot-hint" }, "Click a step for details. Arrow keys move between steps."));
    applySelection();
    if (!userZoomed) fit(); else applyScale();
  }

  function applyScale() {
    if (!design) return;
    const L = design.definition.layout;
    els.stage.style.transform = `scale(${scale})`;
    els.wrap.style.width = L.width * scale + "px"; els.wrap.style.height = L.height * scale + "px";
    els.zoom.textContent = Math.round(scale * 100) + "%";
  }
  function fit() {
    if (!design) return;
    const L = design.definition.layout;
    const w = els.canvas.clientWidth - 16, hgt = els.canvas.clientHeight - 16;
    // Fit the width; for tall workflows keep text readable (>= 80%) and let the canvas scroll vertically.
    const byWidth = Math.min(1, w / L.width);
    scale = Math.max(0.4, Math.min(byWidth, Math.max(hgt / L.height, 0.8)));
    applyScale();
  }
  $$("[data-wb-zoom]").forEach((b) => b.addEventListener("click", () => {
    const k = b.dataset.wbZoom;
    if (k === "fit") { userZoomed = false; fit(); return; }
    userZoomed = true;
    scale = Math.max(0.4, Math.min(1.6, +(scale + (k === "in" ? 0.1 : -0.1)).toFixed(2)));
    applyScale();
  }));

  /* ---------------- Selection ---------------- */
  function select(id) { selected = id; applySelection(); renderPanel(); const el = $(`.wb-node[data-id="${CSS.escape(id)}"]`, els.stage); if (el) { el.focus({ preventScroll: true }); el.scrollIntoView({ block: "nearest", inline: "nearest" }); } }
  function applySelection() {
    const related = new Set();
    $$(".wb-edge", els.stage).forEach((p) => {
      const on = selected && (p.dataset.from === selected || p.dataset.to === selected);
      p.classList.toggle("is-active", !!on);
      p.setAttribute("marker-end", on ? "url(#wb-arrow-active)" : "url(#wb-arrow)");
      if (on) { related.add(p.dataset.from); related.add(p.dataset.to); }
    });
    $$(".wb-edge-label", els.stage).forEach((l) => l.classList.toggle("is-active", !!selected && (l.dataset.from === selected || l.dataset.to === selected)));
    $$(".wb-node", els.stage).forEach((n) => {
      n.classList.toggle("is-selected", n.dataset.id === selected);
      n.classList.toggle("is-related", !!selected && related.has(n.dataset.id) && n.dataset.id !== selected);
      n.setAttribute("aria-pressed", n.dataset.id === selected ? "true" : "false");
    });
    els.stage.classList.toggle("has-selection", !!selected);
  }
  els.canvas.addEventListener("click", (e) => { if (!e.target.closest(".wb-node") && selected) { selected = null; applySelection(); renderPanel(); } });
  els.canvas.addEventListener("keydown", (e) => {
    if (!design) return;
    if (e.key === "Escape" && selected) { selected = null; applySelection(); renderPanel(); return; }
    if (!selected || !["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
    e.preventDefault();
    const L = design.definition.layout, edges = design.definition.edges;
    let next = null;
    if (e.key === "ArrowDown") next = (edges.find((x) => x.from === selected) || {}).to;
    if (e.key === "ArrowUp") next = (edges.find((x) => x.to === selected) || {}).from;
    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      const me = L.positions[selected];
      const row = Object.entries(L.positions).filter(([, p]) => p.layer === me.layer).sort((a, b) => a[1].x - b[1].x).map(([id]) => id);
      const i = row.indexOf(selected);
      next = row[i + (e.key === "ArrowRight" ? 1 : -1)];
    }
    if (next) select(next);
  });

  /* ---------------- Details panel ---------------- */
  function section(title, ...content) { return h("section", { class: "wb-section" }, h("h3", { class: "wb-section-title", text: title }), ...content); }
  function nodeLink(id, label) {
    const n = design.definition.nodes.find((x) => x.id === id); if (!n) return null;
    return h("button", { type: "button", class: "wb-link " + T(n.type).css, onclick: () => select(id) },
      h("span", { class: "wb-link-icon" }, icon(T(n.type).icon, "icon-xs")), h("span", { text: n.title }),
      label ? h("span", { class: "wb-link-label", text: label }) : null);
  }

  function whyBtn(url, label) {
    return h("button", { type: "button", class: "why-btn", "data-explain": url, "aria-label": label }, icon("info"), "Why?");
  }

  function renderPanel() {
    els.panel.replaceChildren();
    if (!design) {
      els.panel.append(h("div", { class: "wb-panel-head" }, h("div", { class: "card-title" }, icon("info"), "How this works")),
        h("div", { class: "wb-panel-body" },
          h("ol", { class: "cp-steps" },
            ...[["user", "why-human", "You describe the workflow in plain English."],
                ["sparkle", "why-ai", "Claude proposes steps, conditions and connections."],
                ["cpu", "why-rule", "OpsPilot validates the structure and adds human approvals where they're required."],
                ["user", "why-human", "A person saves it, requests approval and approves it before it's Active."]]
              .map(([ic, cls, text]) => h("li", {}, h("span", { class: "why-icon " + cls }, icon(ic, "icon-sm")), h("span", { text })))),
          h("div", { class: "wb-types" }, ...ORDERED.map(([, t]) => h("span", { class: "wb-legend-item " + t.css }, h("span", { class: "wb-legend-icon" }, icon(t.icon, "icon-sm")), t.label)))));
      return;
    }
    const def = design.definition;
    const n = selected && def.nodes.find((x) => x.id === selected);
    if (!n) return renderOverview(def);

    const t = T(n.type);
    const ins = def.edges.filter((e) => e.to === n.id), outs = def.edges.filter((e) => e.from === n.id);
    const aiText = cfg.ai_levels[n.ai_involvement] || n.ai_involvement;
    els.panel.append(
      h("div", { class: `wb-panel-head wb-node-head ${t.css}` },
        h("span", { class: "wb-node-icon" }, icon(t.icon)),
        h("div", { class: "grow" }, h("div", { class: "wb-node-type", text: t.label }), h("div", { class: "wb-panel-title", text: n.title })),
        whyBtn(`/api/explain/workflow/${design.id}?node=${encodeURIComponent(n.id)}`, "Why? Explain this step"),
        h("button", { type: "button", class: "btn btn-ghost btn-icon btn-sm", "aria-label": "Close details", title: "Back to overview",
          onclick: () => { selected = null; applySelection(); renderPanel(); } }, icon("x"))),
      h("div", { class: "wb-panel-body" },
        n.added_by_policy ? h("div", { class: "alert alert-review wb-policy-alert" }, icon("shield"), h("div", {}, h("div", { class: "alert-title", text: "Added by OpsPilot" }), h("div", { text: "The AI didn't include this approval. OpsPilot's human-judgment policy requires it." }))) : null,
        section("What it does", h("p", { text: n.description || "—" })),
        section("Why it exists", h("p", { text: n.purpose || "—" })),
        h("div", { class: "wb-facts" },
          h("div", { class: "wb-fact" }, h("div", { class: "wb-fact-label", text: "AI involvement" }),
            n.ai_involvement === "none" ? badge("wb-plain", "x", "Not involved") : badge("actor-ai", "sparkle", aiText),
            n.ai_role ? h("p", { class: "wb-fact-note", text: n.ai_role }) : null),
          h("div", { class: "wb-fact" }, h("div", { class: "wb-fact-label", text: "Human approval" }),
            n.requires_human_approval ? badge("cls-human", "shield", "Required") : badge("wb-plain", "check", "Not required"),
            n.requires_human_approval && n.approver_role ? h("p", { class: "wb-fact-note", text: "By: " + n.approver_role }) : null)),
        section("Data it uses", n.data_used.length ? h("div", { class: "wb-chips" }, ...n.data_used.map((d) => h("span", { class: "wb-data-chip" }, icon("database", "icon-xs"), d))) : h("p", { class: "muted", text: "None listed." })),
        section("Connections",
          h("div", { class: "wb-conn" },
            h("div", { class: "wb-conn-label", text: "Comes from" }),
            ins.length ? h("div", { class: "wb-links" }, ...ins.map((e) => nodeLink(e.from, e.label))) : h("span", { class: "muted small", text: n.type === "trigger" ? "Starts the workflow" : "—" })),
          h("div", { class: "wb-conn" },
            h("div", { class: "wb-conn-label", text: "Goes to" }),
            outs.length ? h("div", { class: "wb-links" }, ...outs.map((e) => nodeLink(e.to, e.label))) : h("span", { class: "muted small", text: "Ends this path" })),
          n.returns_to.length ? h("div", { class: "wb-conn" }, h("div", { class: "wb-conn-label", text: "Loops back to" }),
            h("div", { class: "wb-links" }, ...n.returns_to.map((r) => nodeLink(r.id, r.label)))) : null),
        n.policy.length ? section("OpsPilot checks", h("ul", { class: "wb-notes" }, ...n.policy.map((p) => h("li", { class: "is-policy" }, icon("cpu", "icon-sm"), h("span", { text: p }))))) : null));
  }

  function renderOverview(def) {
    const c = def.counts || {};
    const noteIcon = { fix: "cpu", policy: "shield", warn: "alert", info: "info" };
    els.panel.append(
      h("div", { class: "wb-panel-head" }, h("div", { class: "card-title" }, icon("layers"), "Workflow overview"),
        whyBtn(`/api/explain/workflow/${design.id}`, "Why? Explain this workflow proposal")),
      h("div", { class: "wb-panel-body" },
        def.summary ? section("Summary", h("p", { text: def.summary })) : null,
        h("div", { class: "wb-type-grid" }, ...ORDERED.map(([k, t]) =>
          h("div", { class: "wb-type-cell " + t.css }, h("span", { class: "wb-legend-icon" }, icon(t.icon, "icon-sm")), h("span", { class: "wb-type-n num", text: String(c[k] || 0) }), h("span", { class: "wb-type-l", text: t.label })))),
        section("OpsPilot checks",
          def.notes.length ? h("ul", { class: "wb-notes" }, ...def.notes.map((x) => h("li", { class: "is-" + x.kind }, icon(noteIcon[x.kind] || "info", "icon-sm"), h("span", { text: x.text }))))
                           : h("p", { class: "wb-ok" }, icon("check", "icon-sm"), "Structure is valid and every approval rule is met. No changes were needed.")),
        def.assumptions.length ? section("Assumptions the AI made", h("ul", { class: "wb-bullets" }, ...def.assumptions.map((a) => h("li", { text: a })))) : null,
        section("History", h("ul", { class: "timeline wb-history" }, ...design.events.map((ev) =>
          h("li", { class: "timeline-item" },
            h("span", { class: "timeline-marker actor-" + ev.actor.toLowerCase() }, icon({ AI: "sparkle", AUTOMATION: "cpu", HUMAN: "user" }[ev.actor] || "user")),
            h("div", { class: "timeline-content" }, h("div", { class: "timeline-action", text: ev.action }),
              h("div", { class: "timeline-meta" }, h("span", { text: { AI: "AI", AUTOMATION: "Business rule", HUMAN: "Human" }[ev.actor] }), h("span", { title: exact(ev.at), text: ago(ev.at) }))))))),
        design.response_id ? h("div", { class: "provenance" }, h("span", {}, "Model ", h("code", { text: design.model })), h("span", {}, "Response ", h("code", { text: design.response_id }))) : null,
        h("p", { class: "xsmall muted", text: "Prototype: OpsPilot stores and approves workflow designs. It does not run them." })));
  }

  /* ---------------- Sizing ---------------- */
  function size() {
    const top = els.main.getBoundingClientRect().top + window.scrollY;
    const bottomPad = parseFloat(getComputedStyle(document.getElementById("content")).paddingBottom) || 24;
    els.main.style.height = Math.max(440, window.innerHeight - top - bottomPad) + "px";
    if (!userZoomed) fit();
  }
  window.addEventListener("resize", size);
  els.describe.addEventListener("toggle", size);

  function renderTitles() {
    if (!design) return;
    const t = $(".page-title"); if (t) t.textContent = design.name;
    const crumb = $(".crumbs span:last-child"); if (crumb) crumb.textContent = design.name;
    document.title = `${design.name} | OpsPilot AI`;
  }
  function renderAll() { renderHeader(); renderTitles(); syncDescribe(); renderCanvas(); renderPanel(); size(); }
  renderAll();
})();
