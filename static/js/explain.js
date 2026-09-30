/* OpsPilot explainability panel ("Why?").
   Any element with data-explain="/api/explain/..." opens the side panel. The server assembles the
   explanation from stored data; this file only renders it. All text is set with textContent. */
(function () {
  "use strict";
  const dlg = document.getElementById("explain-drawer");
  if (!dlg) return;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const SVG = "http://www.w3.org/2000/svg";
  const body = $("#xp-body"), recBox = $("#xp-rec");
  let lastUrl = null, opener = null;

  function icon(name, cls) { const s = document.createElementNS(SVG, "svg"); s.setAttribute("class", "icon" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true"); const u = document.createElementNS(SVG, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s; }
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => {
      if (v == null || v === false) return;
      if (k === "class") el.className = v; else if (k === "text") el.textContent = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? "" : v);
    });
    children.flat().forEach((c) => { if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return el;
  }
  const when = (ts) => { const d = new Date(ts); return isNaN(d) ? "" : d.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" }); };

  const SRC = {
    fact: { icon: "database", label: "Fact from database", cls: "src-fact" },
    ai: { icon: "sparkle", label: "AI interpretation", cls: "src-ai" },
    rule: { icon: "cpu", label: "Business rule", cls: "src-rule" },
    policy: { icon: "cpu", label: "OpsPilot policy", cls: "src-rule" },
    human: { icon: "user", label: "Human decision", cls: "src-human" },
  };
  const srcTag = (kind, text) => { const s = SRC[kind] || SRC.fact; return h("span", { class: "xp-tag " + s.cls }, icon(s.icon, "icon-xs"), text || s.label); };
  const STATE = {
    triggered: ["Triggered", "is-triggered", "flag"], passed: ["Passed", "is-passed", "check"],
    adjusted: ["Changed output", "is-adjusted", "edit"], checked: ["Checked", "is-checked", "cpu"], info: ["Ran", "is-checked", "activity"],
  };

  /* ---------------- Header ---------------- */
  function renderHeader(x) {
    $("#xp-kicker").textContent = `Why? · ${x.kicker}`;
    const r = x.recommendation || {}, p = x.provenance || {};
    const live = p.source === "live";
    const conf = x.confidence && x.confidence.value != null ? Math.round(x.confidence.value * 100) : null;
    recBox.replaceChildren(
      h("div", { class: "xp-rec" },
        h("div", { class: "xp-rec-label" }, "Recommendation"),
        h("h2", { class: "xp-rec-text", id: "xp-title", text: r.text, title: (r.text || "").length > 140 ? r.text : null }),
        h("div", { class: "xp-rec-meta" },
          r.context ? (r.link ? h("a", { href: r.link, class: "xp-rec-context", text: r.context }) : h("span", { class: "xp-rec-context", text: r.context })) : null,
          r.owner ? h("span", {}, icon("user", "icon-xs"), "Owner: ", h("strong", { text: r.owner })) : null)),
      h("div", { class: "xp-prov" },
        live ? h("span", { class: "badge source-live", title: p.model ? "Model " + p.model : null }, icon("check"), "Live from Claude")
          : h("span", { class: "badge source-synthetic" }, icon("info"), "Synthetic sample"),
        r.source ? h("span", { class: "badge xp-src-badge" }, r.source) : null,
        conf !== null ? h("span", { class: "xp-conf", title: x.confidence.basis },
          h("span", { class: "confidence-bar" }, h("span", { style: `width:${conf}%` })), h("span", { class: "num strong", text: conf + "%" }), " confidence") : null,
        p.created_at ? h("span", { class: "xp-prov-time" }, icon("clock", "icon-xs"), when(p.created_at)) : null,
        live && p.response_id ? h("code", { class: "xp-resp", title: "Anthropic message ID", text: p.response_id }) : null));
  }

  /* ---------------- Section scaffolding ---------------- */
  function section(id, kind, title, intro, ...content) {
    const s = SRC[kind];
    return h("section", { class: "xp-sec " + s.cls, id, "aria-labelledby": id + "-t" },
      h("div", { class: "xp-sec-head" },
        h("span", { class: "xp-sec-icon" }, icon(s.icon)),
        h("div", { class: "xp-sec-titles" },
          h("h3", { class: "xp-sec-title", id: id + "-t", text: title }),
          intro ? h("p", { class: "xp-sec-intro", text: intro }) : null),
        h("span", { class: "xp-sec-src", text: s.label })),
      h("div", { class: "xp-sec-body" }, ...content));
  }

  function tags(list) { return (list || []).map((t) => srcTag(t.kind === "rule" ? "rule" : t.kind === "ai" ? "ai" : "fact", t.text)); }

  function quote(title, text, tagList) {
    const long = (text || "").length > 360;
    const q = h("blockquote", { class: "xp-quote" + (long ? " is-clamped" : ""), text: text });
    const more = long ? h("button", { type: "button", class: "link-btn xp-more", onclick: (e) => { q.classList.toggle("is-clamped"); e.currentTarget.textContent = q.classList.contains("is-clamped") ? "Show all" : "Show less"; } }, "Show all") : null;
    if (more) requestAnimationFrame(() => { if (q.scrollHeight <= q.clientHeight + 2) { q.classList.remove("is-clamped"); more.remove(); } });
    return h("div", { class: "xp-block" }, h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: title }), h("span", { class: "xp-tags" }, ...tags(tagList))), q, more);
  }

  /* ---------------- Evidence ---------------- */
  function factsBlock(b) {
    return h("div", { class: "xp-block" },
      h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: b.title })),
      b.note ? h("p", { class: "xp-block-note", text: b.note }) : null,
      h("dl", { class: "xp-facts" }, ...b.items.map((f) => h("div", { class: "xp-fact is-" + f.state },
        h("dt", { text: f.label }),
        h("dd", {},
          h("span", { class: "xp-fact-value" }, h("span", { class: "xp-dot", "aria-hidden": "true" }), h("span", { text: f.value })),
          f.now ? h("span", { class: "xp-now", title: "Different in today's record" }, "Now: ", f.now) : null,
          f.note ? h("span", { class: "xp-fact-note", text: f.note }) : null,
          f.tags && f.tags.length ? h("span", { class: "xp-tags" }, ...tags(f.tags)) : null)))));
  }
  function evidenceBlock(b) {
    if (b.type === "quote") return quote(b.title, b.text, b.tags);
    if (b.type === "note") return h("p", { class: "xp-block-note", text: b.text });
    return factsBlock(b);
  }

  /* ---------------- AI interpretation ---------------- */
  const CITE = {
    match: ["database", "is-match", "Matches the record"], conflict: ["alert", "is-conflict", "Record shows"],
    unmatched: ["sparkle", "is-unmatched", "AI's reading. No single database field to check."],
  };
  function aiBlock(b) {
    if (b.type === "field") return h("div", { class: "xp-ai-field" }, h("span", { class: "xp-ai-label", text: b.label }), h("span", { class: "xp-ai-value", text: b.value || "—" }));
    if (b.type === "quote") return quote(b.label, b.text, []);
    if (b.type === "text") return h("div", { class: "xp-block" }, h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: b.label })),
      h("p", { class: "xp-ai-text", text: b.text }), b.note ? h("p", { class: "xp-block-note", text: b.note }) : null);
    if (b.type === "list") return h("div", { class: "xp-block" }, h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: b.label })),
      h("ul", { class: "xp-list" }, ...b.items.map((i) => h("li", { class: i.tone ? "tone-" + i.tone : null }, h("span", { text: i.text }), i.meta ? h("span", { class: "xp-list-meta", text: i.meta }) : null))));
    if (b.type === "cited") return h("div", { class: "xp-block" }, h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: b.label })),
      h("ul", { class: "xp-cited" }, ...b.items.map((c) => {
        const [ic, cls, lead] = CITE[c.state] || CITE.unmatched;
        return h("li", { class: cls },
          h("span", { class: "xp-cited-text" }, h("span", { class: "xp-cited-q", text: "“" + c.text + "”" })),
          h("span", { class: "xp-cited-check" }, icon(ic, "icon-xs"), c.match ? `${lead}: ${c.match}` : lead));
      })));
    return null;
  }

  /* ---------------- Rules ---------------- */
  function ruleItem(r) {
    const [label, cls, ic] = STATE[r.state] || STATE.checked;
    return h("li", { class: "xp-rule " + cls },
      h("div", { class: "xp-rule-head" },
        h("span", { class: "xp-rule-name", text: r.name }),
        h("span", { class: "xp-state " + cls }, icon(ic, "icon-xs"), label)),
      r.condition || r.action ? h("div", { class: "xp-rule-logic" },
        r.condition ? h("div", {}, h("span", { class: "xp-kw", text: "If" }), h("span", { text: r.condition })) : null,
        r.action ? h("div", {}, h("span", { class: "xp-kw", text: "Then" }), h("span", { text: r.action })) : null) : null,
      r.result ? h("div", { class: "xp-rule-result" }, icon("chevron-right", "icon-xs"), h("span", { text: r.result })) : null,
      r.current ? h("div", { class: "xp-rule-current" + (r.current === "Resolved since" ? " is-resolved" : "") }, "Today: ", r.current) : null,
      r.items && r.items.length ? h("ul", { class: "xp-rule-sub" }, ...r.items.map((i) => {
        const [l2, c2, i2] = STATE[i.state] || STATE.checked;
        return h("li", { class: c2 }, h("span", { class: "xp-sub-icon" }, icon(i2, "icon-xs")), h("span", { class: "xp-sub-text" }, h("strong", { text: i.text }), i.result ? h("span", { text: i.result }) : null), h("span", { class: "sr-only", text: l2 }));
      })) : null);
  }

  /* ---------------- Human decision ---------------- */
  const DEC = { approved: "is-approved", approved_with_edits: "is-approved", accepted: "is-approved", active: "is-approved",
    rejected: "is-rejected", dismissed: "is-rejected", pending: "is-pending", pending_approval: "is-pending" };
  function humanBlock(hm) {
    const d = hm.decision;
    return [
      h("div", { class: "xp-human-req " + (hm.required ? "is-required" : "is-not-required") },
        h("span", { class: "xp-human-icon" }, icon(hm.required ? "shield" : "info")),
        h("div", {}, h("div", { class: "xp-human-title", text: hm.required ? "Human approval required" : "No approval at this point" }),
          h("p", { class: "xp-human-text", text: hm.headline }))),
      hm.reasons && hm.reasons.length ? h("div", { class: "xp-block" }, h("div", { class: "xp-block-head" }, h("span", { class: "xp-block-title", text: hm.reasons_title || "Reasons" })),
        h("ul", { class: "xp-list" }, ...hm.reasons.map((r) => h("li", {}, h("span", { text: r.text }), h("span", { class: "xp-tags" }, srcTag(r.source === "ai" ? "ai" : "rule", r.source === "ai" ? "Flagged by AI" : r.source === "policy" ? "Added by policy" : "Business rule")))))) : null,
      hm.effects && hm.effects.length ? h("div", { class: "xp-effects" }, ...hm.effects.map((e) => h("div", { class: "xp-effect is-" + e.tone }, h("span", { class: "xp-effect-label" }, icon(e.tone === "approve" ? "check" : "x", "icon-xs"), e.label), h("span", { text: e.text })))) : null,
      d ? h("div", { class: "xp-decision " + (DEC[d.status] || "is-other") },
        h("div", { class: "xp-decision-head" }, h("span", { class: "xp-decision-label" }, "Decision"), h("span", { class: "xp-decision-status", text: d.label })),
        d.by || d.at || d.via ? h("div", { class: "xp-decision-meta" },
          d.by ? h("span", {}, icon("user", "icon-xs"), d.by) : null, d.via ? h("span", { text: "via " + d.via }) : null, d.at ? h("span", { text: when(d.at) }) : null) : null,
        d.changed && d.changed.length ? h("div", { class: "xp-decision-note" }, "Edited before approval: ", d.changed.join(", ")) : null,
        d.note ? h("blockquote", { class: "xp-decision-quote", text: d.note }) : null,
        d.outcome ? h("div", { class: "xp-decision-note", text: d.outcome }) : null,
        d.link ? h("a", { class: "btn btn-sm xp-decision-link", href: d.link }, icon("stamp"), "Review in Approval Center") : null) : null,
      hm.guardrail ? h("p", { class: "xp-guard" }, icon("shield", "icon-xs"), hm.guardrail) : null,
    ];
  }

  /* ---------------- Render ---------------- */
  function render(x) {
    renderHeader(x);
    const ev = x.evidence, ai = x.ai, ru = x.rules, hm = x.human;
    const passed = ru.passed || [];
    body.replaceChildren(
      ...(x.notices || []).map((n) => h("div", { class: "alert alert-" + (n.tone === "warning" ? "warning" : "info") + " xp-notice" }, icon(n.tone === "warning" ? "refresh" : "info"), h("div", { text: n.text }))),
      section("xp-sec-evidence", "fact", "Evidence", ev.intro, ...ev.blocks.map(evidenceBlock)),
      section("xp-sec-ai", "ai", "AI interpretation", ai.intro, ...ai.blocks.map(aiBlock)),
      section("xp-sec-rules", "rule", "Business rules", ru.intro,
        h("ul", { class: "xp-rules" }, ...ru.items.map(ruleItem)),
        passed.length ? h("details", { class: "xp-passed" }, h("summary", {}, icon("check", "icon-xs"), `Checked, not triggered (${passed.length})`),
          h("ul", { class: "xp-rules is-compact" }, ...passed.map(ruleItem))) : null),
      section("xp-sec-human", "human", "Human decision", null, ...humanBlock(hm)),
      h("footer", { class: "xp-foot" }, icon("shield", "icon-xs"),
        h("span", {}, x.footer, " OpsPilot never asks the model for its internal reasoning; the AI section shows only its structured output and short written justification.")));
    body.scrollTop = 0;
    $$(".xp-nav-btn", dlg).forEach((b, i) => b.classList.toggle("is-current", i === 0));
  }

  function loading() {
    recBox.replaceChildren(h("div", { class: "xp-rec" }, h("div", { class: "xp-rec-label" }, "Recommendation"), h("div", { class: "skeleton", style: "height:22px;width:80%" }), h("div", { class: "skeleton", style: "height:12px;width:40%;margin-top:8px" })));
    body.replaceChildren(h("div", { class: "xp-loading" }, ...[90, 70, 85, 60, 75].map((w) => h("div", { class: "skeleton", style: `height:14px;width:${w}%` }))));
  }
  function failed(msg) {
    recBox.replaceChildren();
    body.replaceChildren(h("div", { class: "alert alert-error", role: "alert" }, icon("alert"),
      h("div", { class: "grow" }, h("div", { class: "alert-title", text: "Explanation unavailable" }), h("div", { class: "small", text: msg })),
      h("button", { type: "button", class: "btn btn-sm", onclick: () => load(lastUrl) }, icon("refresh"), "Retry")));
  }

  async function load(url) {
    lastUrl = url; loading();
    let res;
    try {
      const r = await fetch(url, { headers: { Accept: "application/json" } });
      try { res = await r.json(); } catch (e) { res = { ok: false, error: { message: `The server returned HTTP ${r.status}.` } }; }
    } catch (e) { res = { ok: false, error: { message: "Could not reach the OpsPilot server." } }; }
    if (url !== lastUrl) return;
    if (!res.ok) { failed((res.error && res.error.message) || "Something went wrong."); return; }
    render(res.explanation);
  }

  function open(url, trigger) {
    opener = trigger || document.activeElement;
    if (!dlg.open) dlg.showModal();
    $("[data-xp-close]", dlg).focus();
    load(url);
  }
  function close() { dlg.classList.add("is-closing"); setTimeout(() => { dlg.classList.remove("is-closing"); dlg.close(); }, 160); }
  dlg.addEventListener("close", () => { if (opener && opener.focus && document.contains(opener)) opener.focus(); });
  dlg.addEventListener("cancel", (e) => { e.preventDefault(); close(); });
  dlg.addEventListener("click", (e) => {
    if (e.target === dlg) { close(); return; }                       // click on the backdrop
    if (e.target.closest("[data-xp-close]")) { close(); return; }
    const jump = e.target.closest("[data-xp-jump]");
    if (jump) { const t = document.getElementById(jump.dataset.xpJump); if (t) body.scrollTo({ top: topOf(t) - 12, behavior: "smooth" }); }
  });
  const topOf = (el) => el.getBoundingClientRect().top - body.getBoundingClientRect().top + body.scrollTop;
  body.addEventListener("scroll", () => {                            // highlight the section in view
    const secs = $$(".xp-sec", body); let cur = secs[0];
    secs.forEach((s) => { if (topOf(s) - body.scrollTop <= 60) cur = s; });
    if (body.scrollTop + body.clientHeight >= body.scrollHeight - 4) cur = secs[secs.length - 1];
    $$(".xp-nav-btn", dlg).forEach((b) => b.classList.toggle("is-current", cur && b.dataset.xpJump === cur.id));
  }, { passive: true });
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-explain]");
    if (!t) return;
    e.preventDefault(); e.stopPropagation();
    open(t.dataset.explain, t);
  }, true);

  window.opsExplain = { open };
})();
