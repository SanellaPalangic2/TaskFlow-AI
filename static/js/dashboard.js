/* OpsPilot dashboard: activity filter and the optional AI summary of verified insights.
   All AI text is set with textContent. The server rejects summaries with unverified numbers. */
(function () {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const SVG = "http://www.w3.org/2000/svg";
  function icon(name, cls) { const s = document.createElementNS(SVG, "svg"); s.setAttribute("class", "icon" + (cls ? " " + cls : "")); s.setAttribute("aria-hidden", "true"); const u = document.createElementNS(SVG, "use"); u.setAttribute("href", "#i-" + name); s.append(u); return s; }
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    Object.entries(attrs || {}).forEach(([k, v]) => { if (v == null || v === false) return; if (k === "class") el.className = v; else if (k === "text") el.textContent = v; else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v); });
    children.flat().forEach((c) => { if (c != null && c !== false) el.append(c instanceof Node ? c : document.createTextNode(String(c))); });
    return el;
  }

  /* ---------------- Activity filter ---------------- */
  const feed = $("#da-feed");
  if (feed) {
    const PER_VIEW = 8;
    const apply = (actor) => {
      let shown = 0;
      $$(".da-item", feed).forEach((li) => {
        const match = actor === "all" || li.dataset.actor === actor;
        li.hidden = !(match && shown < PER_VIEW);
        if (match && shown < PER_VIEW) shown++;
      });
      feed.hidden = shown === 0;
      const empty = $("#da-empty"); if (empty) empty.hidden = shown !== 0;
    };
    $$("[data-da-filter]").forEach((b) => b.addEventListener("click", () => {
      $$("[data-da-filter]").forEach((x) => { x.classList.toggle("is-active", x === b); x.setAttribute("aria-pressed", x === b ? "true" : "false"); });
      apply(b.dataset.daFilter);
    }));
  }

  /* ---------------- AI summary of verified insights ---------------- */
  const card = $("#dash-insights"), btn = $("#di-summarize"), box = $("#di-summary");
  if (!card || !btn || !box) return;
  function setLoading(on) {
    btn.classList.toggle("is-loading", on); btn.disabled = on;
    $(".btn-text", btn).textContent = on ? "Summarizing…" : "Summarize";
  }
  async function summarize() {
    setLoading(true);
    let res;
    try {
      const r = await fetch(card.dataset.summaryUrl, { method: "POST", headers: { Accept: "application/json" } });
      try { res = await r.json(); } catch (e) { res = { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${r.status}.` } }; }
    } catch (e) { res = { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server." } }; }
    setLoading(false);
    $$(".di-item.is-cited", card).forEach((li) => li.classList.remove("is-cited"));
    box.hidden = false;
    if (!res.ok) {
      const err = res.error || {};
      box.className = "di-summary is-error";
      box.replaceChildren(h("div", { class: "alert alert-error", role: "alert" }, icon("alert"),
        h("div", { class: "grow" }, h("div", { class: "alert-title", text: err.code === "invalid_output" ? "AI summary not shown." : "AI summary temporarily unavailable." }),
          h("div", { class: "small", text: `${err.message || err.title || ""}${err.code ? ` (${err.code})` : ""}` }),
          h("div", { class: "xsmall", style: "margin-top:4px", text: "The calculated insights below are unaffected." })),
        h("button", { type: "button", class: "btn btn-sm", onclick: summarize }, icon("refresh"), "Retry")));
      return;
    }
    box.className = "di-summary";
    box.dataset.done = "1";
    res.insight_ids_used.forEach((id) => { const li = $(`.di-item[data-insight="${CSS.escape(id)}"]`, card); if (li) li.classList.add("is-cited"); });
    box.replaceChildren(
      h("div", { class: "di-summary-head" }, icon("sparkle", "icon-xs"), "AI briefing"),
      h("p", { class: "di-summary-text", text: res.summary }),
      h("div", { class: "di-summary-meta" },
        h("span", { class: "ok", "data-tip": "OpsPilot compared every number in this summary with the calculated insights before showing it." }, icon("check", "icon-xs"), "Every figure verified"),
        h("span", { text: `Based on ${res.insight_ids_used.length || res.facts_sent.length} of ${res.facts_sent.length} insights` }),
        h("span", { "data-tip": `Claude response ${res.response_id}` }, "Live from Claude · ", h("code", { text: res.model || "" })),
        h("span", { text: `${(res.latency_ms / 1000).toFixed(1)}s` })));
    window.opsToast && window.opsToast("Summary ready. Every figure matches the calculated insights.", "success");
  }
  btn.addEventListener("click", summarize);
})();
