/* OpsPilot AI — small, dependency-free UI behaviors.
   All AI calls go to our own Flask endpoints; the browser never sees the API key. */
(function () {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const ICONS = { success: "check", error: "alert", info: "info" };

  /* ---------------- Toasts ---------------- */
  function toast(message, type = "info", timeout = 4200) {
    const stack = $("#toasts");
    if (!stack) return;
    const el = document.createElement("div");
    el.className = `toast toast-${type}`;
    el.setAttribute("role", type === "error" ? "alert" : "status");
    el.innerHTML = `<svg class="icon" aria-hidden="true"><use href="#i-${ICONS[type] || "info"}"></use></svg>
      <div class="toast-msg"></div>
      <button class="toast-close" aria-label="Dismiss"><svg class="icon icon-sm"><use href="#i-x"></use></svg></button>`;
    $(".toast-msg", el).textContent = message;
    // A success replaces any error toast still showing (e.g. after a successful Retry).
    if (type === "success") stack.querySelectorAll(".toast-error").forEach((t) => { t.classList.add("is-leaving"); setTimeout(() => t.remove(), 200); });
    const remove = () => { el.classList.add("is-leaving"); setTimeout(() => el.remove(), 200); };
    $(".toast-close", el).addEventListener("click", remove);
    stack.appendChild(el);
    if (timeout) setTimeout(remove, timeout);
  }
  window.opsToast = toast;

  // Keep a success message visible across the page refresh that follows some actions.
  function reloadWithToast(message, type = "success") {
    try { sessionStorage.setItem("ops-pending-toast", JSON.stringify([message, type])); } catch (e) { /* storage unavailable */ }
    window.location.reload();
  }
  try {
    const pending = sessionStorage.getItem("ops-pending-toast");
    if (pending) { sessionStorage.removeItem("ops-pending-toast"); const [m, t] = JSON.parse(pending); toast(m, t, 5000); }
  } catch (e) { /* storage unavailable */ }

  try {
    const flashes = JSON.parse(($("#flash-data") || {}).textContent || "[]");
    flashes.forEach(([cat, msg]) => toast(msg, cat === "message" ? "info" : cat, cat === "error" ? 7000 : 5000));
  } catch (e) { /* no flashes */ }

  /* ---------------- Shell ---------------- */
  const sidebar = $("#sidebar"), scrim = $("#scrim");
  const setMenu = (open) => { sidebar && sidebar.classList.toggle("is-open", open); scrim && scrim.classList.toggle("is-open", open); };
  $("#menu-toggle") && $("#menu-toggle").addEventListener("click", () => setMenu(!sidebar.classList.contains("is-open")));
  scrim && scrim.addEventListener("click", () => setMenu(false));

  document.addEventListener("keydown", (e) => {
    if (e.key === "/" && !/INPUT|TEXTAREA|SELECT/.test(document.activeElement.tagName)) {
      const s = $("#global-search");
      if (s && s.offsetParent !== null) { e.preventDefault(); s.focus(); }
    }
  });

  /* ---------------- Buttons & forms ---------------- */
  function setLoading(btn, on, text) {
    if (!btn) return;
    btn.classList.toggle("is-loading", on);
    btn.disabled = on;
    const label = $(".btn-text", btn);
    if (label) {
      if (on) { label.dataset.orig = label.dataset.orig || label.textContent; if (text) label.textContent = text; }
      else if (label.dataset.orig) label.textContent = label.dataset.orig;
    }
  }

  document.addEventListener("submit", (e) => {
    const form = e.target;
    if (form.matches("[data-confirm]") && !form.dataset.confirmed) {
      e.preventDefault();
      confirmDialog(form.dataset.confirmTitle || "Please confirm", form.dataset.confirm, form.dataset.confirmAction || "Confirm")
        .then((ok) => { if (ok) { form.dataset.confirmed = "1"; setLoading($("button", form), true); form.submit(); } });
      return;
    }
    if (form.matches("[data-validate]") && !validateForm(form)) { e.preventDefault(); return; }
    const btn = e.submitter || $("button[type=submit], button:not([type])", form);
    // Forms that manage their own loading state (drafts, Copilot, Automation Finder) opt out.
    if (btn && !form.matches("[data-draft-form], [data-own-loading]")) setLoading(btn, true, btn.dataset.loadingText);
  });

  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-autosubmit]")) e.target.form.submit();
  });

  /* Clickable table rows */
  document.addEventListener("click", (e) => {
    const row = e.target.closest("tr.row-link");
    if (row && !e.target.closest("a, button, select, input, label")) window.location = row.dataset.href;
  });

  /* Live filtering of the visible table */
  $$("[data-live-filter]").forEach((input) => {
    const table = $(input.dataset.liveFilter);
    if (!table) return;
    const card = input.closest(".card");
    const apply = () => {
      const q = input.value.trim().toLowerCase();
      let visible = 0;
      $$("tbody tr", table).forEach((tr) => {
        const hit = !q || (tr.dataset.search || "").includes(q);
        tr.hidden = !hit; if (hit) visible++;
      });
      const count = $("[data-visible-count]", card); if (count) count.textContent = visible;
      const empty = $("[data-no-results]", card); if (empty) empty.hidden = visible !== 0;
      table.closest(".table-wrap").hidden = visible === 0;
    };
    input.addEventListener("input", apply);
  });

  /* ---------------- Modals ---------------- */
  document.addEventListener("click", (e) => {
    const opener = e.target.closest("[data-open-modal]");
    if (opener) { const m = document.getElementById(opener.dataset.openModal); if (m) m.showModal(); }
    const closer = e.target.closest("[data-close-modal]");
    if (closer) closer.closest("dialog").close();
  });
  document.addEventListener("click", (e) => {  // click on backdrop closes
    if (e.target.tagName === "DIALOG") {
      const r = e.target.getBoundingClientRect();
      if (e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom) e.target.close();
    }
  });

  function confirmDialog(title, message, actionLabel) {
    return new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.className = "modal";
      d.innerHTML = `<div class="modal-header"><div><div class="modal-title"></div></div></div>
        <div class="modal-body"><p class="secondary"></p></div>
        <div class="modal-footer"><button type="button" class="btn" data-no>Cancel</button>
        <button type="button" class="btn btn-primary" data-yes></button></div>`;
      $(".modal-title", d).textContent = title;
      $(".modal-body p", d).textContent = message;
      $("[data-yes]", d).textContent = actionLabel;
      document.body.appendChild(d);
      let result = false;
      $("[data-yes]", d).addEventListener("click", () => { result = true; d.close(); });
      $("[data-no]", d).addEventListener("click", () => d.close());
      d.addEventListener("close", () => { d.remove(); resolve(result); });
      d.showModal();
      $("[data-yes]", d).focus();
    });
  }

  /* ---------------- API helper ---------------- */
  async function postJSON(url, body) {
    let res;
    try {
      res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "Accept": "application/json" },
                               body: JSON.stringify(body || {}) });
    } catch (err) {
      return { ok: false, error: { title: "Connection lost", message: "Could not reach the OpsPilot server. Check that it is still running." } };
    }
    try { return await res.json(); }
    catch (err) { return { ok: false, error: { title: "Unexpected response", message: `The server returned HTTP ${res.status}.` } }; }
  }

  function alertHTML(err, retryAttr, retryLabel = "Try again") {
    const wrap = document.createElement("div");
    wrap.className = "alert alert-error";
    wrap.setAttribute("role", "alert");
    wrap.style.marginBottom = "var(--space-4)";
    wrap.innerHTML = `<svg class="icon"><use href="#i-alert"></use></svg><div class="grow"><div class="alert-title"></div><div class="msg"></div>
      ${err.code ? `<div class="xsmall muted" style="margin-top:4px"><span class="detail"></span>Error code <code></code></div>` : ""}</div>
      ${retryAttr ? `<button type="button" class="btn btn-sm" ${retryAttr}><svg class="icon"><use href="#i-refresh"></use></svg><span class="spinner"></span><span class="btn-text"></span></button>` : ""}`;
    $(".alert-title", wrap).textContent = err.title || "Something went wrong";
    $(".msg", wrap).textContent = err.message || "";
    if (err.code) { $("code", wrap).textContent = err.code; $(".detail", wrap).textContent = err.detail ? `${err.detail}. ` : ""; }
    if (retryAttr) $(".btn-text", wrap).textContent = retryLabel;
    return wrap;
  }

  /* ---------------- AI analysis ---------------- */
  let analysisRunning = false;
  async function runAnalysis(url) {
    if (analysisRunning) return;
    analysisRunning = true;
    const card = $("#ai-analysis-card");
    const body = $("[data-ai-body]", card);
    const previous = body.innerHTML;
    $$("[data-run-analysis]").forEach((b) => setLoading(b, true, "Analyzing…"));
    body.innerHTML = "";
    body.appendChild($("template[data-ai-loading]", card).content.cloneNode(true));
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });

    const data = await postJSON(url);
    analysisRunning = false;
    if (data.ok) {
      // Swap every section the analysis can change: header, next best action, AI card, tasks, timeline.
      Object.entries(data.sections || {}).forEach(([id, html]) => {
        const el = document.getElementById(id);
        if (el) el.outerHTML = html;
      });
      toast(`AI analysis received from Claude in ${(data.meta.latency_ms / 1000).toFixed(1)}s and checked by business rules.`, "success");
      const nba = $("#next-best-action"); if (nba) nba.scrollIntoView({ behavior: "smooth", block: "start" });
    } else {
      body.innerHTML = previous;
      const err = data.error || {};
      const slot = $("[data-ai-error]", body);
      slot.innerHTML = "";
      slot.appendChild(alertHTML({ title: "AI analysis temporarily unavailable.", message: err.message,
                                   detail: err.title, code: err.code }, `data-run-analysis="${url}"`, "Retry"));
      $$("[data-run-analysis]").forEach((b) => setLoading(b, false));
      toast("AI analysis temporarily unavailable.", "error");
    }
  }
  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-run-analysis]");
    if (btn) runAnalysis(btn.dataset.runAnalysis);
  });
  if ($("[data-auto-analyze]")) {
    const trigger = $("#ai-analysis-card [data-run-analysis]");
    if (trigger) setTimeout(() => runAnalysis(trigger.dataset.runAnalysis), 400);
    history.replaceState(null, "", location.pathname);  // don't re-run on refresh
  }

  /* ---------------- Draft customer message ---------------- */
  // Drafts are queued in the Human Approval Center. Approving here records the same audited decision.
  const draftModal = $("#draft-modal");
  if (draftModal) {
    const steps = (name) => $$("[data-draft-step]", draftModal).forEach((s) => { s.hidden = s.dataset.draftStep !== name; });
    const genBtn = $("[data-draft-generate]", draftModal), approveBtn = $("[data-draft-approve]", draftModal),
          backBtn = $("[data-draft-back]", draftModal), errorSlot = $("[data-draft-error]", draftModal),
          queueLink = $("[data-draft-queue]", draftModal);
    let current = null;   // { decideUrl, original: {subject, body} }
    const reset = () => { steps("choose"); errorSlot.innerHTML = ""; genBtn.hidden = false; approveBtn.hidden = true; backBtn.hidden = true;
                          queueLink.hidden = true; setLoading(genBtn, false); setLoading(approveBtn, false); };
    draftModal.addEventListener("close", () => {
      if (current) toast("The draft is waiting in the Approval Center.", "info");
      current = null; reset();
    });
    backBtn.addEventListener("click", reset);

    const generate = async () => {
      const purpose = ($("input[name=purpose]:checked", draftModal) || {}).value;
      errorSlot.innerHTML = "";
      steps("loading"); setLoading(genBtn, true, "Drafting…");
      const data = await postJSON(draftModal.dataset.draftUrl, { purpose });
      setLoading(genBtn, false);
      if (data.ok) {
        current = { decideUrl: data.decide_url, original: { subject: data.draft.subject, body: data.draft.body } };
        $("#draft-subject").value = data.draft.subject;
        $("#draft-body").value = data.draft.body;
        $("[data-note-text]", draftModal).textContent = data.draft.reviewer_note || "Read the full message and confirm every detail is accurate.";
        const why = $("[data-draft-why]", draftModal);
        if (why && data.item_id) { why.dataset.explain = `/api/explain/approval/${data.item_id}`; why.hidden = false; }
        const meta = $("[data-draft-meta]", draftModal);
        meta.innerHTML = `<span>Model <code></code></span><span>Response <code></code></span><span></span>`;
        const codes = $$("code", meta);
        codes[0].textContent = data.meta.model; codes[1].textContent = data.meta.response_id;
        meta.lastElementChild.textContent = `${(data.meta.latency_ms / 1000).toFixed(1)}s`;
        steps("review"); genBtn.hidden = true; approveBtn.hidden = false; backBtn.hidden = true; queueLink.hidden = false;
        $("#draft-body").focus();
      } else {
        steps("choose");
        const err = data.error || {};
        errorSlot.appendChild(alertHTML({ title: "AI drafting temporarily unavailable.", message: err.message,
                                          detail: err.title, code: err.code }, "data-draft-retry", "Retry"));
      }
    };
    genBtn.addEventListener("click", generate);
    draftModal.addEventListener("click", (e) => { if (e.target.closest("[data-draft-retry]")) generate(); });
    $("[data-draft-form]", draftModal).addEventListener("submit", async (e) => {
      e.preventDefault();
      if (!current) return;
      const subject = $("#draft-subject").value.trim(), body = $("#draft-body").value.trim();
      if (!subject || !body) { toast("Add a subject and message before approving.", "error"); return; }
      const edited = subject !== current.original.subject.trim() || body !== current.original.body.trim();
      setLoading(approveBtn, true, "Approving…");
      const res = await postJSON(current.decideUrl, { decision: "approve", via: "Project page",
                                                      edits: edited ? { subject, body } : null });
      setLoading(approveBtn, false);
      if (!res.ok) { toast((res.error && res.error.message) || "The approval was not recorded.", "error"); return; }
      current = null; draftModal.close();
      const msg = edited ? "Approved with your edits. The original AI draft is kept in the audit trail. Nothing was sent."
                         : "Message approved. Nothing was sent.";
      toast(msg, "success");
      setTimeout(() => reloadWithToast(msg), 700);
    });
  }

  /* ---------------- Task status (inline) ---------------- */
  document.addEventListener("change", async (e) => {
    const sel = e.target.closest("[data-task-status]");
    if (!sel) return;
    const previous = sel.querySelector("option[selected]") ? sel.querySelector("option[selected]").value : sel.dataset.prev;
    sel.disabled = true;
    const data = await postJSON(sel.dataset.taskStatus, { status: sel.value });
    sel.disabled = false;
    if (!data.ok) {
      if (previous) sel.value = previous;
      toast((data.error && data.error.message) || "Could not update the task.", "error");
      return;
    }
    toast(data.message, "success");
    $$("option", sel).forEach((o) => o.toggleAttribute("selected", o.value === data.status));
    const row = sel.closest("tr");
    const cell = row && $("[data-status-cell]", row);
    const tpl = document.getElementById(`badge-${data.status}`);
    if (cell && tpl) { cell.innerHTML = ""; cell.appendChild(tpl.content.cloneNode(true)); }
    if (row) {
      row.classList.toggle("is-completed", data.status === "Completed");
      if (data.status === "Completed") row.classList.remove("is-overdue");
      row.classList.remove("flash-row"); void row.offsetWidth; row.classList.add("flash-row");
    }
    // On the project page, refresh so open tasks, flags and the timeline stay in sync.
    if (data.project_changed || $("#next-best-action")) setTimeout(() => reloadWithToast(data.message), 700);
  });

  /* ---------------- New project form ---------------- */
  const EMAIL = /^[^@\s]+@[^@\s]+\.[^@\s]+$/;
  const RULES = {
    first_name: (v) => v ? "" : "First name is required.",
    last_name: (v) => v ? "" : "Last name is required.",
    service_type: (v) => v ? "" : "Choose a service type.",
    email: (v) => !v || EMAIL.test(v) ? "" : "Enter a valid email address, e.g. name@example.com.",
    phone: (v) => !v || v.replace(/\D/g, "").length >= 10 ? "" : "Enter a 10-digit phone number.",
    monthly_electric_bill: (v) => { if (!v) return ""; const n = Number(v.replace(/[$,]/g, "")); return isFinite(n) && n >= 0 && n <= 10000 ? "" : "Enter an amount between $0 and $10,000."; },
    roof_age: (v) => { if (!v) return ""; const n = Number(v); return Number.isInteger(n) && n >= 0 && n <= 120 ? "" : "Enter a whole number of years between 0 and 120."; },
  };
  function showFieldError(input, msg) {
    const field = input.closest(".field");
    if (!field) return;
    field.classList.toggle("is-invalid", !!msg);
    input.setAttribute("aria-invalid", msg ? "true" : "false");
    let err = $(".error-text", field);
    const help = $(".help", field);
    if (msg) {
      if (!err) { err = document.createElement("span"); err.className = "error-text"; field.appendChild(err); }
      err.innerHTML = `<svg class="icon icon-sm"><use href="#i-alert"></use></svg><span></span>`;
      $("span", err).textContent = msg;
      if (help) help.hidden = true;
    } else {
      if (err) err.remove();
      if (help) help.hidden = false;
    }
  }
  function validateForm(form) {
    let first = null;
    Object.entries(RULES).forEach(([name, rule]) => {
      const input = form.elements[name];
      if (!input) return;
      const msg = rule(input.value.trim());
      showFieldError(input, msg);
      if (msg && !first) first = input;
    });
    if (first) { first.focus(); toast("Please fix the highlighted fields.", "error"); }
    return !first;
  }
  const npForm = $("#new-project-form");
  if (npForm) {
    Object.keys(RULES).forEach((name) => {
      const input = npForm.elements[name];
      if (!input) return;
      // Validate on submit, then re-check live while a field is marked invalid.
      // (No blur validation: inserting an error on blur shifts the layout under the pointer and can swallow the submit click.)
      input.addEventListener("input", () => { if (input.closest(".is-invalid")) showFieldError(input, RULES[name](input.value.trim())); });
    });
    const svc = npForm.elements.service_type;
    const syncDocs = () => {
      let shown = 0;
      $$("[data-doc-for]", npForm).forEach((el) => {
        const on = svc.value && el.dataset.docFor.split("|").includes(svc.value);
        el.hidden = !on; if (!on) $("input", el).checked = false; else shown++;
      });
      $("[data-doc-empty]", npForm).hidden = shown > 0;
    };
    svc.addEventListener("change", syncDocs); syncDocs();
  }
  $$("[data-char-count]").forEach((ta) => {
    const out = $("[data-count]", ta.closest(".field"));
    const upd = () => { if (out) out.textContent = ta.value.length.toLocaleString(); };
    ta.addEventListener("input", upd); upd();
  });
})();
