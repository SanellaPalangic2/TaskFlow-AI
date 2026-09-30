/* OpsPilot Automation Opportunity Finder. Claude is called by the server; the browser never sees the key. */
(function () {
  "use strict";
  const root = document.getElementById("af");
  if (!root) return;
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const toast = (m, t, ms) => window.opsToast && window.opsToast(m, t, ms);
  const form = $("#af-form"), desc = $("#af-desc"), name = $("#af-name"), submit = $("#af-submit");
  const resultBox = $("#af-result"), errorBox = $("#af-error");
  let examples = [];
  try { examples = JSON.parse($("#af-examples").textContent); } catch (e) { examples = []; }

  function setLoading(btn, on, text) {
    if (!btn) return;
    btn.classList.toggle("is-loading", on); btn.disabled = on;
    const label = $(".btn-text", btn);
    if (label) { if (on) { label.dataset.orig = label.dataset.orig || label.textContent; if (text) label.textContent = text; }
                 else if (label.dataset.orig) label.textContent = label.dataset.orig; }
  }
  async function post(url, body) {
    try {
      const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify(body || {}) });
      try { return await res.json(); } catch (e) { return { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${res.status}.` } }; }
    } catch (e) {
      return { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server. Check that it is still running." } };
    }
  }
  function alertBox(kind, title, message, extra) {
    const el = document.createElement("div");
    el.className = `alert alert-${kind}`; el.setAttribute("role", kind === "error" ? "alert" : "status");
    el.innerHTML = `<svg class="icon"><use href="#i-${kind === "error" ? "alert" : "info"}"></use></svg><div class="grow"><div class="alert-title"></div><div class="msg"></div>${extra || ""}</div>`;
    $(".alert-title", el).textContent = title; $(".msg", el).textContent = message || "";
    return el;
  }

  /* ---------- Input ---------- */
  if (form) {
    const count = $("[data-af-count]");
    const upd = () => { count.textContent = desc.value.length.toLocaleString(); };
    desc.addEventListener("input", () => { upd(); clearFieldError(); $$(".af-example").forEach((b) => b.classList.remove("is-selected")); });
    upd();

    $$("[data-af-example]").forEach((btn) => btn.addEventListener("click", () => {
      const ex = examples[Number(btn.dataset.afExample)];
      if (!ex) return;
      desc.value = ex.text; name.value = ex.name; upd(); clearFieldError();
      $$(".af-example").forEach((b) => b.classList.toggle("is-selected", b === btn));
      desc.focus(); desc.setSelectionRange(0, 0); desc.scrollTop = 0;
    }));

    function clearFieldError() {
      const f = $("#af-desc-field"); f.classList.remove("is-invalid");
      const e = $(".error-text", f); if (e) e.remove();
      $("#af-desc-help").hidden = false;
    }
    function fieldError(msg) {
      const f = $("#af-desc-field"); f.classList.add("is-invalid");
      let e = $(".error-text", f);
      if (!e) { e = document.createElement("span"); e.className = "error-text"; $(".row-between", f).prepend(e); }
      e.innerHTML = `<svg class="icon icon-sm"><use href="#i-alert"></use></svg><span></span>`; $("span", e).textContent = msg;
      $("#af-desc-help").hidden = true; desc.focus();
    }

    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const text = desc.value.trim();
      if (text.length < 40) { fieldError("Describe the process in a bit more detail (at least 40 characters)."); return; }
      errorBox.innerHTML = "";
      setLoading(submit, true, "Analyzing…");
      resultBox.innerHTML = "";
      resultBox.appendChild($("#af-loading").content.cloneNode(true));
      runStages();
      resultBox.scrollIntoView({ behavior: "smooth", block: "start" });

      const data = await post(root.dataset.analyzeUrl, { description: text, process_name: name.value.trim() });
      stopStages(); setLoading(submit, false);

      if (data.ok && data.status === "valid_process") {
        resultBox.innerHTML = data.html;
        const inner = $("#af-result-inner"); if (inner) inner.classList.add("is-new");
        history.replaceState(null, "", data.url);   // the analysis has its own URL (unsaved until you save it)
        toast(`Analysis received from Claude in ${(data.meta.latency_ms / 1000).toFixed(1)}s. Metrics calculated by OpsPilot.`, "success");
        resultBox.scrollIntoView({ behavior: "smooth", block: "start" });
      } else if (data.ok) {
        resultBox.innerHTML = "";
        errorBox.appendChild(alertBox("warning", data.status === "not_a_process" ? "That doesn't look like a business process" : "Add a little more detail",
                                      data.message));
        desc.focus();
      } else {
        resultBox.innerHTML = "";
        const err = data.error || {};
        if (err.code === "invalid_input") { fieldError(err.message); return; }
        const box = alertBox("error", "Process analysis temporarily unavailable.", err.message,
          `${err.code ? `<div class="xsmall muted" style="margin-top:4px">${err.title ? err.title + ". " : ""}Error code <code>${err.code}</code></div>` : ""}`);
        const retry = document.createElement("button");
        retry.type = "button"; retry.className = "btn btn-sm"; retry.innerHTML = `<svg class="icon"><use href="#i-refresh"></use></svg>Retry`;
        retry.addEventListener("click", () => form.requestSubmit());
        box.appendChild(retry);
        errorBox.appendChild(box);
        toast("Process analysis temporarily unavailable.", "error");
      }
    });
  }

  let stageTimer = null;
  function runStages() {
    const labels = ["Breaking the process into steps…", "Classifying each step with Claude…",
                    "Applying OpsPilot's human-judgment policy…", "Calculating metrics from the classifications…"];
    let i = 0;
    const tick = () => {
      $$("[data-af-step]").forEach((li) => { const n = Number(li.dataset.afStep); li.classList.toggle("is-active", n === i); li.classList.toggle("is-done", n < i); });
      const st = $("[data-af-stage]"); if (st) st.textContent = labels[i];
    };
    tick();
    // The server does all four steps in one request; the labels advance gently and stop at the last one.
    stageTimer = setInterval(() => { if (i < 1) { i++; tick(); } }, 1200);
  }
  function stopStages() { clearInterval(stageTimer); }

  /* ---------- Save ---------- */
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-af-save]");
    if (!btn) return;
    const inner = $("#af-result-inner");
    const input = $("#af-save-name");
    const nameVal = (input.value || "").trim();
    if (!nameVal) { input.classList.add("is-invalid"); input.focus(); toast("Give the analysis a name before saving.", "error"); return; }
    setLoading(btn, true, "Saving…");
    const data = await post(inner.dataset.saveUrl, { process_name: nameVal });
    if (!data.ok) { setLoading(btn, false); toast((data.error && data.error.message) || "Could not save the analysis.", "error"); return; }
    $("[data-af-save-area]", inner).innerHTML = `<span class="badge status-active">${'<svg class="icon"><use href="#i-check"></use></svg>'}Saved just now</span>`;
    const t = $("[data-af-title]", inner); if (t) t.textContent = data.name;
    const list = document.getElementById("af-saved");
    if (list && data.saved_html) list.outerHTML = data.saved_html;
    toast(`Saved “${data.name}”. It now appears in Saved analyses.`, "success");
  });
})();
