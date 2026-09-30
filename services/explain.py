"""
Explainability: the "Why?" panel.

Read-only. Every explanation is assembled from data OpsPilot already stored:

  Evidence          database values (captured when the analysis ran, or the stored input)
  AI interpretation the model's structured output fields and its short written justification
  Business rules    the deterministic rules and policy checks that ran, with their results
  Human decision    whether a person must approve, and what they decided (from the audit trail)

Nothing here calls Claude, and OpsPilot never asks the model for its internal reasoning.
"""
import json
import re
from datetime import datetime

import config
import database as db_layer
from services import rules_engine

PLAIN_STAGE = {"New Lead": "New Lead", "Information Gathering": "Information Gathering"}
CONTACT_LABELS = {"email": "Email", "phone": "Phone", "address": "Address"}
STOP = {"the", "a", "an", "and", "or", "to", "for", "of", "with", "from", "on", "in", "is", "are", "has", "have",
        "customer", "customers", "project", "yet", "still", "been", "not", "no"}
NEGATIVE = re.compile(r"\b(not|no|missing|without|lacks?|lacking|outstanding|needed|needs|requested|pending|absent)\b", re.I)
NOT_SENT = ["last name", "email", "phone", "address", "electric bill amount", "roof age", "customer notes"]

RULE_CATALOG = {
    "contact": ("Required contact information",
                "Email, phone and address must all be on file",
                "Create “Collect missing contact information” for Customer Support and flag the project Needs Attention"),
    "document": ("Required documents",
                 "Each document the service needs must be Received or Verified",
                 "Create a document request task for Customer Support and flag the project Needs Attention"),
    "roof_info": ("Roof information",
                  "Services that depend on the roof need the roof age",
                  "Create “Collect roof information” for Operations and flag the project Needs Attention"),
    "roof_review": ("Roof age review threshold",
                    "Roof age above the demo threshold goes to qualified staff",
                    "Create a Project Review task, flag Human Review Required and raise priority to High"),
}
TASK_DECISIONS = {
    "covered": ("Covered by a rule task", "passed"), "rejected": ("Rejected by rules", "adjusted"),
    "duplicate": ("Not created: duplicate", "adjusted"), "needs_approval": ("Waiting for a person to add", "checked"),
    "added": ("Added by a team member", "passed"),
}


# --- Small helpers ---------------------------------------------------------------------------------

def _now():
    return datetime.now().isoformat(timespec="seconds")


def _loads(value, default=None):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOP}


def fact(label, value, state="info", note=None, tags=None, now=None):
    return {"label": label, "value": "—" if value in (None, "") else str(value), "state": state,
            "note": note, "tags": tags or [], "now": now}


def _money(v):
    return f"${v:,.0f} per month" if isinstance(v, (int, float)) else None


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


# --- Capture (called when an analysis runs) --------------------------------------------------------

def capture_evidence(project, documents, tasks, findings, context_json=None, captured_at=None):
    """Snapshot of the database facts an analysis used. Stored with the analysis so the "Why?"
    panel shows what the AI and the rules actually saw, even if the record changes later."""
    sent = sorted(_loads(context_json, {}).keys()) if context_json else []
    return {
        "captured_at": captured_at or _now(),
        "project": {
            "customer": f"{project['first_name']} {project['last_name']}",
            "service_type": project["service_type"], "current_stage": project["current_stage"],
            "project_status": project["project_status"], "priority": project["priority"],
            "monthly_electric_bill": project["monthly_electric_bill"], "roof_age": project["roof_age"],
            "notes": project["notes"] or "",
        },
        "contact_on_file": {f: bool((project[f] or "").strip()) for f in config.REQUIRED_CONTACT_FIELDS},
        "documents": [{"type": d["document_type"], "status": d["status"]} for d in documents],
        "required_documents": config.REQUIRED_DOCUMENTS.get(project["service_type"], []),
        "open_tasks": [{"title": t["title"], "department": t["department"], "status": t["status"],
                        "due_date": t["due_date"], "rule_key": t["rule_key"]}
                       for t in tasks if t["status"] != "Completed"],
        "findings": [{"rule_key": f.rule_key, "category": f.category, "severity": f.severity, "message": f.message,
                      "task_title": f.task_title, "department": f.department, "missing_item": f.missing_item,
                      "resolved": f.resolved} for f in findings],
        "roof_threshold": config.ROOF_AGE_REVIEW_THRESHOLD,
        "roof_services": sorted(config.ROOF_INFO_SERVICES),
        "sent_to_ai": sent,
        "rule_actions": [],
    }


def _current_snapshot(db, project_id):
    project, documents, tasks = rules_engine.load_state(db, project_id)
    if project is None:
        return None, None, None
    findings = rules_engine.evaluate(project, documents, tasks)
    return capture_evidence(project, documents, tasks, findings), tasks, project


# --- Evidence from a project snapshot --------------------------------------------------------------

def _match_keys(snap):
    """Keywords that tie a sentence (an AI fact, a concern) to a specific database field."""
    keys = {}
    for d in snap["documents"]:
        keys["doc:" + d["type"]] = _tokens(d["type"])
    for f in config.REQUIRED_CONTACT_FIELDS:
        keys["contact:" + f] = {f}
    keys["roof_age"] = {"roof"}
    keys["bill"] = {"electric"}
    return keys


def _cites(text, key, keys):
    t = _tokens(text)
    k = keys.get(key) or set()
    if not k or not k <= t:
        return False
    if key == "roof_age":
        return bool(t & {"age", "old", "older", "years", "yrs", "year"}) or bool(re.search(r"\d", text))
    return True


def project_evidence(snap, current=None, cited_texts=(), concern_texts=()):
    """Evidence blocks for a project snapshot. `current` (a later snapshot) marks values that changed since."""
    p = snap["project"]
    keys = _match_keys(snap)
    threshold = snap.get("roof_threshold", config.ROOF_AGE_REVIEW_THRESHOLD)
    by_cat = {}
    for f in snap["findings"]:
        by_cat.setdefault(f["category"], []).append(f)
    cp = current["project"] if current else None

    def ai_tag(key):
        return [{"kind": "ai", "text": "Cited by AI"}] if any(_cites(t, key, keys) for t in cited_texts) else []

    def changed(now_value, then_value):
        return str(now_value) if current is not None and str(now_value) != str(then_value) else None

    record = [fact("Customer", p["customer"])]
    record.append(fact("Service", p["service_type"], now=changed(cp["service_type"], p["service_type"]) if cp else None))
    record.append(fact("Workflow stage", p["current_stage"], now=changed(cp["current_stage"], p["current_stage"]) if cp else None))
    record.append(fact("Project status", p["project_status"],
                       "flag" if p["project_status"] in ("Needs Attention", "Human Review Required", "Blocked") else "info",
                       now=changed(cp["project_status"], p["project_status"]) if cp else None))
    record.append(fact("Priority", p["priority"], "flag" if p["priority"] == "High" else "info",
                       now=changed(cp["priority"], p["priority"]) if cp else None))
    bill = _money(p["monthly_electric_bill"])
    record.append(fact("Monthly electric bill", bill or "Not provided", "info" if bill else "missing", tags=ai_tag("bill"),
                       now=changed(_money(cp["monthly_electric_bill"]) or "Not provided", bill or "Not provided") if cp else None))

    roof = p["roof_age"]
    roof_tags = ai_tag("roof_age")
    if roof is None:
        needs = p["service_type"] in snap.get("roof_services", config.ROOF_INFO_SERVICES)
        roof_fact = fact("Roof age", "Not provided", "missing" if needs else "info",
                         f"Needed for {p['service_type']}" if needs else f"Not needed for {p['service_type']}",
                         roof_tags + ([{"kind": "rule", "text": "Roof information rule"}] if needs else []))
    elif roof > threshold:
        roof_fact = fact("Roof age", f"{roof} years", "flag", f"Above the demo review threshold of {threshold} years",
                         roof_tags + [{"kind": "rule", "text": "Roof age review rule"}])
    else:
        roof_fact = fact("Roof age", f"{roof} years", "ok", f"Within the demo threshold of {threshold} years", roof_tags)
    if cp:
        now_roof = f"{cp['roof_age']} years" if cp["roof_age"] is not None else "Not provided"
        roof_fact["now"] = changed(now_roof, roof_fact["value"])
    record.append(roof_fact)

    docs = []
    now_docs = {d["type"]: d["status"] for d in (current or {}).get("documents", [])}
    for d in snap["documents"]:
        missing = d["status"] in ("Missing", "Requested")
        tags = ([{"kind": "rule", "text": "Required documents rule"}] if missing else []) + ai_tag("doc:" + d["type"])
        docs.append(fact(d["type"], d["status"], "missing" if missing else "ok",
                         "Required for " + p["service_type"] if d["type"] in snap.get("required_documents", []) else None,
                         tags, now=changed(now_docs.get(d["type"], d["status"]), d["status"]) if current else None))

    contact = []
    now_contact = (current or {}).get("contact_on_file", {})
    for f, on in snap["contact_on_file"].items():
        tags = ([] if on else [{"kind": "rule", "text": "Contact information rule"}]) + ai_tag("contact:" + f)
        contact.append(fact(CONTACT_LABELS.get(f, f.title()), "On file" if on else "Missing", "ok" if on else "missing",
                            tags=tags, now=changed("On file" if now_contact.get(f) else "Missing",
                                                   "On file" if on else "Missing") if current else None))

    blocks = [{"type": "facts", "title": "Project record", "items": record}]
    if docs:
        blocks.append({"type": "facts", "title": f"Documents for {p['service_type']}", "items": docs})
    blocks.append({"type": "facts", "title": "Contact details", "note": "Only whether each is on file. Values are not shown here.",
                   "items": contact})
    if p["notes"]:
        blocks.append({"type": "quote", "title": "Customer notes (stored on the project)", "text": p["notes"],
                       "tags": [{"kind": "ai", "text": "Interpreted by AI"}] if concern_texts else []})
    tasks = snap.get("open_tasks") or []
    if tasks:
        blocks.append({"type": "facts", "title": f"Open tasks at the time ({len(tasks)})",
                       "items": [fact(t["title"], t["status"], "info", f"{t['department']}" + (f" · due {t['due_date']}" if t.get("due_date") else ""),
                                      [{"kind": "rule", "text": "Created by a rule"}] if t.get("rule_key") else [])
                                 for t in tasks[:6]]})
    return blocks


def _cited_items(texts, snap):
    """Each fact the AI cited, checked against the stored record."""
    keys = _match_keys(snap)
    docs = {d["type"]: d["status"] for d in snap["documents"]}
    out = []
    for text in texts:
        match, state = None, "unmatched"
        for d_type, status in docs.items():
            if _cites(text, "doc:" + d_type, keys):
                match, state = f"{d_type}: {status}", "match"
                if status in ("Received", "Verified") and NEGATIVE.search(text):
                    state = "conflict"
                break
        if not match:
            for f, on in snap["contact_on_file"].items():
                if _cites(text, "contact:" + f, keys):
                    match, state = f"{CONTACT_LABELS[f]}: {'On file' if on else 'Missing'}", "match"
                    if on and NEGATIVE.search(text):
                        state = "conflict"
                    break
        if not match and _cites(text, "roof_age", keys):
            roof = snap["project"]["roof_age"]
            match, state = f"Roof age: {roof} years" if roof is not None else "Roof age: Not provided", "match"
            nums = [int(n) for n in re.findall(r"\b(\d{1,3})\b", text)]
            if roof is not None and nums and roof not in nums and snap.get("roof_threshold") not in nums:
                state = "conflict"
        if not match and _cites(text, "bill", keys):
            match, state = "Monthly electric bill: " + (_money(snap["project"]["monthly_electric_bill"]) or "Not provided"), "match"
        out.append({"text": text, "match": match, "state": state})
    return out


# --- Rules for a project snapshot ------------------------------------------------------------------

def _task_result(rule_key, current_tasks):
    t = next((t for t in current_tasks or [] if t["rule_key"] == rule_key), None)
    if t is None:
        return None
    due = f" · due {t['due_date']}" if t["due_date"] and t["status"] != "Completed" else ""
    return f"Task “{t['title']}”: {t['status']}{due}"


def project_rules(snap, current, current_tasks):
    p = snap["project"]
    threshold = snap.get("roof_threshold", config.ROOF_AGE_REVIEW_THRESHOLD)
    now_open = {f["rule_key"] for f in (current or {}).get("findings", []) if not f["resolved"]}
    items, fired = [], set()
    for f in snap["findings"]:
        name, _cond, action = RULE_CATALOG.get(f["category"], (f["category"], "", ""))
        fired.add(f["category"])
        if f["category"] == "document":
            condition = f"{f['missing_item']} is required for {p['service_type']} and is not on file"
        elif f["category"] == "contact":
            condition = f["message"]
        elif f["category"] == "roof_review":
            condition = f"Roof age {p['roof_age']} years is above the {threshold}-year demo threshold"
        else:
            condition = f"{p['service_type']} depends on the roof and the roof age is unknown"
        still = f["rule_key"] in now_open if current is not None else True
        items.append({"name": name, "condition": condition, "action": action,
                      "result": _task_result(f["rule_key"], current_tasks) or (f"Task “{f['task_title']}”" if not f["resolved"] else "Review task completed"),
                      "state": "triggered" if not f["resolved"] else "passed",
                      "current": None if current is None else ("Still open" if still else "Resolved since")})
    passed = []
    if "contact" not in fired:
        passed.append({"name": RULE_CATALOG["contact"][0], "result": "Email, phone and address are on file", "state": "passed"})
    if "document" not in fired:
        req = snap.get("required_documents") or []
        passed.append({"name": RULE_CATALOG["document"][0],
                       "result": ("All required documents are on file" if req else f"No documents required for {p['service_type']}"),
                       "state": "passed"})
    if "roof_info" not in fired:
        needs = p["service_type"] in snap.get("roof_services", config.ROOF_INFO_SERVICES)
        passed.append({"name": RULE_CATALOG["roof_info"][0],
                       "result": "Roof age is on file" if needs else f"Not needed for {p['service_type']}", "state": "passed"})
    if "roof_review" not in fired:
        roof = p["roof_age"]
        passed.append({"name": RULE_CATALOG["roof_review"][0],
                       "result": (f"{roof} years is within the {threshold}-year demo threshold" if roof is not None
                                  else "No roof age to compare"), "state": "passed"})
    return items, passed


# --- Explanation: project analysis (Next best action) ----------------------------------------------

def _approval_decision(db, where, value):
    item = db.execute(f"SELECT * FROM approval_items WHERE {where} = ? ORDER BY id DESC LIMIT 1", (value,)).fetchone()
    if item is None:
        return None, None
    dec = db.execute("SELECT * FROM approval_decisions WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],)).fetchone()
    return item, dec


def _decision_block(item, dec, fallback_status=None):
    if dec is not None:
        label = {"approved": "Approved", "approved_with_edits": "Approved with edits", "rejected": "Rejected"}[dec["decision"]]
        changed = _loads(dec["changed_fields"], []) or []
        return {"status": dec["decision"], "label": label, "by": dec["decided_by"], "at": dec["created_at"],
                "via": dec["decided_via"], "note": dec["note"], "outcome": dec["outcome"],
                "changed": [c.replace("_", " ") for c in changed]}
    if item is not None and item["status"] == "pending":
        return {"status": "pending", "label": "Waiting for a person", "at": item["created_at"],
                "via": "Approval Center", "link": f"/approvals?item={item['id']}"}
    if item is not None and item["status"] == "superseded":
        return {"status": "superseded", "label": "Superseded by a newer analysis", "at": item["decided_at"]}
    if fallback_status and fallback_status != "Pending":
        return {"status": fallback_status.lower(), "label": {"Accepted": "Approved", "Dismissed": "Overridden"}.get(fallback_status, fallback_status)}
    return {"status": "pending", "label": "Waiting for a person"}


def _provenance(source, model, response_id, created_at, latency_ms=None):
    return {"source": source, "model": model, "response_id": response_id, "created_at": created_at,
            "latency_ms": latency_ms}


def analysis(db, analysis_id):
    a = db_layer.get_analysis(db, analysis_id)
    if a is None:
        return None
    from services import intelligence
    current, current_tasks, project = _current_snapshot(db, a["project_id"])
    stored = _loads(a.get("evidence_snapshot"))
    snap = stored or current
    notices = []
    if a["source"] != "live":
        notices.append({"tone": "info", "text": "Seeded sample analysis for the demo. It was not produced by the Anthropic API."})
    if not stored:
        notices.append({"tone": "info", "text": "This analysis predates stored evidence, so the evidence shows the current record."})
    stale = bool(project and a.get("context_hash") and a["context_hash"] != intelligence.facts_hash(
        project, db_layer.project_documents(db, a["project_id"])))
    if stale and stored:
        notices.append({"tone": "warning", "text": "The project changed after this analysis. Evidence shows the record as it "
                                                   "was then. “Now” marks values that are different today."})

    why = [w for w in (a.get("recommendation_why") or []) if isinstance(w, str)]
    concerns = []
    for c in a.get("concerns") or []:  # older analyses stored concerns as plain strings
        if isinstance(c, str):
            c = {"topic": "Concern", "detail": c}
        if isinstance(c, dict):
            concerns.append({"topic": c.get("topic") or "Concern", "detail": c.get("detail") or "",
                             "severity": c.get("severity") or "low", "source": c.get("source")})
    concern_texts = [f"{c['topic']} {c['detail']}" for c in concerns if c.get("source") == "customer_notes"]
    evidence = project_evidence(snap, current if (stored and stale) else None,
                                cited_texts=why + [f"{c['topic']} {c['detail']}" for c in concerns],
                                concern_texts=concern_texts)

    # AI interpretation: only the model's structured output fields.
    ai_blocks = [
        {"type": "field", "label": "Recommended action", "value": a["recommended_action"]},
        {"type": "field", "label": "Suggested owner", "value": a["suggested_department"]},
        {"type": "field", "label": "Customer intent",
         "value": a["detected_intent"] + (f": {a['intent_detail']}" if a.get("intent_detail") else "")},
        {"type": "text", "label": "Summary", "text": a["summary"]},
    ]
    if concerns:
        src = {"customer_notes": "From the customer notes", "project_data": "From the project data",
               "business_rules": "From the rule findings"}
        ai_blocks.append({"type": "list", "label": "Concerns the AI identified", "items": [
            {"text": f"{c['topic']}: {c['detail']}", "meta": f"{c['severity'].title()} · {src.get(c.get('source'), 'From the record')}",
             "tone": {"high": "danger", "medium": "warning"}.get(c["severity"], "neutral")} for c in concerns]})
    if why:
        ai_blocks.append({"type": "cited", "label": "Facts the AI cited, checked against the record",
                          "items": _cited_items(why, snap)})
    clarifications = [m for m in (a.get("missing_information") or []) if m.get("source") == "ai"]
    if clarifications:
        ai_blocks.append({"type": "list", "label": "Clarifications the AI suggested",
                          "items": [{"text": m["item"], "meta": m.get("why_it_matters") or None} for m in clarifications]})
    ai_blocks.append({"type": "text", "label": "Written justification", "text": a["reasoning"],
                      "note": "A short justification the model writes for reviewers. OpsPilot does not ask for, store or show the model's internal reasoning."})

    # Business rules.
    triggered, passed = project_rules(snap, current if stored else None, current_tasks)
    checks = []
    for t in a.get("suggested_tasks") or []:
        label, state = TASK_DECISIONS.get(t.get("decision"), (t.get("decision", ""), "checked"))
        checks.append({"text": t["title"], "result": t.get("note") or label, "state": state})
    policy = []
    if checks:
        policy.append({"name": "AI task suggestions checked against rules", "condition": "Every task the AI suggests",
                       "action": "Rule-backed tasks must match an open finding; others are checked for duplicates and wait for a person",
                       "state": "checked", "items": checks})
    if a.get("info_status_adjusted"):
        policy.append({"name": "Information status set by rules", "condition": "Rules found missing information",
                       "action": "Rules decide the information status, not the AI",
                       "result": f"Final status: {a['information_status']} (the AI's value was replaced)", "state": "adjusted"})
    else:
        policy.append({"name": "Information status", "result": f"{a['information_status']}: consistent with the rule findings",
                       "state": "passed"})
    threshold = config.HUMAN_REVIEW_CONFIDENCE_THRESHOLD
    low = a["confidence"] < threshold
    policy.append({"name": "Confidence threshold", "condition": f"AI confidence below {threshold:.0%} goes to a person",
                   "result": f"{a['confidence']:.0%} is {'below' if low else 'at or above'} the threshold",
                   "state": "triggered" if low else "passed"})
    if a["detected_intent"] == "Unclear":
        policy.append({"name": "Unclear intent", "condition": "The AI could not tell what the customer wants",
                       "result": "Routed to a person", "state": "triggered"})
    actions = snap.get("rule_actions") or []
    if actions:
        policy.append({"name": "Actions taken by rules when the analysis ran", "state": "info",
                       "items": [{"text": x, "state": "passed"} for x in actions]})

    # Human decision.
    item, dec = _approval_decision(db, "analysis_id", analysis_id)
    reasons = a.get("human_review_reasons") or []
    guard = any(f["category"] == "roof_review" and not f["resolved"] for f in snap["findings"])
    human = {
        "required": True,
        "headline": "Yes. A person approves or overrides this recommendation before any task is created.",
        "reasons": [{"text": r["text"], "source": r.get("source", "rule")} for r in reasons],
        "reasons_title": "Why it needs human judgment" if reasons else None,
        "effects": [{"label": "If approved", "tone": "approve",
                     "text": f"A {a['suggested_department']} task is created, unless an open task already covers it. Stage and status do not change."},
                    {"label": "If overridden", "tone": "reject",
                     "text": "The decision is logged, no task is created, and business rules keep running."}],
        "decision": _decision_block(item, dec, a.get("review_status")),
        "guardrail": ("OpsPilot flags roof questions for qualified staff. It makes no engineering, safety or installation decisions."
                      if guard else None),
    }
    open_findings = [f for f in snap["findings"] if not f["resolved"]]
    return {
        "kind": "analysis", "kicker": "Next best action",
        "recommendation": {"text": a["recommended_action"], "owner": a["suggested_department"],
                           "source": "Combined" if open_findings else "AI recommendation",
                           "context": f"{snap['project']['customer']} · {snap['project']['service_type']}",
                           "link": f"/projects/{a['project_id']}"},
        "provenance": _provenance(a["source"], a.get("model"), a.get("response_id"), a["created_at"], a.get("latency_ms")),
        "confidence": {"value": a["confidence"], "basis": "The model's own estimate when it analyzed the project."},
        "notices": notices,
        "evidence": {"intro": "Values from the OpsPilot database" + (" when the analysis ran." if stored else "."),
                     "blocks": evidence},
        "ai": {"intro": "What the model returned in its structured output.", "blocks": ai_blocks},
        "rules": {"intro": "Deterministic checks. The same record always gives the same result, whatever the AI said.",
                  "items": triggered + policy, "passed": passed},
        "human": human,
        "footer": f"Built from stored analysis #{a['id']}"
                  + (f" · evidence captured {snap['captured_at'][:16].replace('T', ' ')}" if stored else "")
                  + ". Nothing in this panel is generated on the fly.",
    }


# --- Explanation: customer communication draft -----------------------------------------------------

SENT_LABELS = {"customer_first_name": "Customer first name", "service": "Service", "project_stage": "Project stage (plain words)",
               "message_goal": "Message goal", "missing_documents": "Missing documents", "missing_contact_details": "Missing contact details",
               "roof_review_pending": "Roof review pending", "customer_mentioned_roof_concern": "Customer mentioned the roof",
               "sign_off": "Sign-off"}


def _v(value):
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(value) or "None"
    return value


def communication(db, item):
    ev = item.get("evidence") or {}
    sent = item.get("ai_input") or {}
    out = item.get("ai_output") or {}
    checks = item.get("checks") or []
    record = [fact("Customer", ev.get("customer")), fact("Service", ev.get("service")), fact("Workflow stage", ev.get("stage")),
              fact("Project status", ev.get("status"))]
    docs = [fact(d["type"], d["status"], "missing" if d["status"] in ("Missing", "Requested") else "ok",
                 tags=[{"kind": "rule", "text": "Required documents rule"}] if d["status"] in ("Missing", "Requested") else [])
            for d in ev.get("documents", [])]
    contact = [fact(CONTACT_LABELS.get(f, f), "Missing", "missing", tags=[{"kind": "rule", "text": "Contact information rule"}])
               for f in ev.get("contact_missing", [])]
    blocks = [{"type": "facts", "title": "Project record when drafted", "items": record}]
    if docs:
        blocks.append({"type": "facts", "title": "Documents", "items": docs})
    if contact:
        blocks.append({"type": "facts", "title": "Contact details", "items": contact})
    blocks.append({"type": "facts", "title": "Exactly what was sent to Claude",
                   "note": "Not sent: " + ", ".join(NOT_SENT) + ".",
                   "items": [fact(SENT_LABELS.get(k, k), _v(v)) for k, v in sent.items()]})

    ai_blocks = [{"type": "field", "label": "Subject", "value": out.get("subject")},
                 {"type": "quote", "label": "Message drafted by the AI", "text": out.get("body", "")}]
    if out.get("reviewer_note"):
        ai_blocks.append({"type": "text", "label": "Note to the reviewer", "text": out["reviewer_note"],
                          "note": "A short note the model writes for the reviewer. OpsPilot does not request its internal reasoning."})

    findings = ev.get("rule_findings") or []
    rules = [{"name": "Missing items come from business rules",
              "condition": "Which documents and contact details to ask for",
              "action": "Taken from the rule findings, not from the AI",
              "result": "; ".join(findings) if findings else "No open findings, so nothing to request",
              "state": "triggered" if findings else "passed"},
             {"name": "Minimal context", "condition": "Only what the message needs leaves OpsPilot",
              "result": f"{len(sent)} fields sent; no contact details, amounts or notes", "state": "passed"}]
    if "customer_mentioned_roof_concern" in sent:
        rules.append({"name": "Roof mention check", "condition": "The word “roof” appears in the customer notes",
                      "result": "Found, so the AI may acknowledge it" if sent["customer_mentioned_roof_concern"] else "Not found",
                      "state": "passed"})
    rules.append({"name": "Draft checks", "condition": "Run by OpsPilot on the AI's draft", "state": "checked",
                  "items": [{"text": c["label"], "result": c.get("detail") or ("Passed" if c["ok"] else "Flagged for the reviewer"),
                             "state": "passed" if c["ok"] else "adjusted"} for c in checks]})
    rules.append({"name": "Never sent automatically", "result": "Approving marks the message approved. This prototype sends nothing.",
                  "state": "passed"})
    item_row = db.execute("SELECT * FROM approval_items WHERE id = ?", (item["id"],)).fetchone()
    dec = db.execute("SELECT * FROM approval_decisions WHERE item_id = ? ORDER BY id DESC LIMIT 1", (item["id"],)).fetchone()
    flagged = [c["label"] for c in checks if not c["ok"]]
    return {
        "kind": "communication", "kicker": "Customer message draft",
        "recommendation": {"text": out.get("subject") or item["title"], "owner": None, "source": "AI draft",
                           "context": f"{ev.get('customer', '')} · {sent.get('message_goal', '')}".strip(" ·"),
                           "link": f"/projects/{item['project_id']}" if item.get("project_id") else None},
        "provenance": _provenance(item["source"], item.get("model"), item.get("response_id"), item["created_at"]),
        "confidence": {"value": item["confidence"], "basis": item.get("confidence_basis") or "The model's own estimate."}
        if item.get("confidence") is not None else None,
        "notices": [],
        "evidence": {"intro": "Values from the OpsPilot database when the draft was written, and the exact fields sent.",
                     "blocks": blocks},
        "ai": {"intro": "What the model wrote.", "blocks": ai_blocks},
        "rules": {"intro": "Deterministic checks on what was sent and on what came back.", "items": rules, "passed": []},
        "human": {"required": True,
                  "headline": "Yes. Customer-facing messages always need a person's approval.",
                  "reasons": [{"text": f"OpsPilot check flagged: {x}", "source": "rule"} for x in flagged],
                  "reasons_title": "Check before approving" if flagged else None,
                  "effects": [{"label": "If approved", "tone": "approve", "text": "Marked approved and logged. Nothing is sent."},
                              {"label": "If rejected", "tone": "reject", "text": "The draft is discarded and the decision is logged."}],
                  "decision": _decision_block(item_row, dec)},
        "footer": f"Built from approval item #{item['id']}: the stored draft, its checks and the exact input sent.",
    }


# --- Explanation: Automation Finder step ------------------------------------------------------------

def automation_step(db, analysis_id, step_number):
    from services import automation_finder as af
    d = af.load(db, analysis_id)
    if d is None or d.get("status", "valid_process") != "valid_process":
        return None
    step = next((s for s in d["steps"] if s["number"] == step_number), None)
    if step is None:
        return None
    cls = af.CLASSES[step["classification"]]
    ai_cls = af.CLASSES[step.get("ai_classification", step["classification"])]
    evidence = [{"type": "quote", "title": "Process description (as the employee entered it)", "text": d["description"]},
                {"type": "facts", "title": "Stored analysis", "items": [
                    fact("Process", d["process_name"]), fact("Steps found", len(d["steps"])),
                    fact("Saved", "Yes" if d["is_saved"] else "Not yet")]}]
    ai_just = step.get("ai_justification") or (None if step["adjusted"] else step["justification"])
    ai_blocks = [{"type": "field", "label": "Step, as the AI read it", "value": step["name"]},
                 {"type": "text", "label": "What happens", "text": step["description"] or "—"},
                 {"type": "field", "label": "AI classification", "value": ai_cls["label"]},
                 {"type": "field", "label": "AI marked it consequential", "value": "Yes" if step.get("consequential") else "No"},
                 {"type": "text", "label": "AI justification",
                  "text": ai_just or "Not stored for this analysis (it predates stored AI justifications).",
                  "note": "A short justification from the structured output. OpsPilot does not request internal reasoning."}]
    rules = []
    if step["adjusted"]:
        rules.append({"name": "Human-judgment policy", "condition": "Approvals, decisions, money, eligibility, legal or safety calls stay with people",
                      "action": f"Changed from {ai_cls['label']} to {cls['label']}", "result": step["adjust_reason"], "state": "adjusted"})
    elif step["classification"] in ("human_decision", "keep_manual"):
        rules.append({"name": "Human-judgment policy", "result": f"The AI already kept this with people ({cls['label']})", "state": "passed"})
    else:
        rules.append({"name": "Human-judgment policy", "condition": "Approvals, decisions, money, eligibility, legal or safety calls stay with people",
                      "result": "No consequential action found in this step, so the AI's classification stands", "state": "passed"})
    if af.CUSTOMER_FACING.search(f"{step['name']} {step['description']}") and step["classification"] in ("traditional_automation", "ai_candidate"):
        rules.append({"name": "Customer-facing check", "condition": "A step that reaches the customer",
                      "result": "A review point was added: approve customer messages before they are sent", "state": "triggered"})
    if d.get("claims_removed"):
        rules.append({"name": "No invented savings", "condition": "Sentences claiming time, cost or percentage savings",
                      "result": f"Removed {d['claims_removed']} such {_plural(d['claims_removed'], 'claim')} from this analysis", "state": "adjusted"})
    metric = {"traditional_automation": "Automatable", "business_rule": "Automatable", "ai_candidate": "AI candidates",
              "human_decision": "Human decision points", "keep_manual": "Total steps only"}[step["classification"]]
    rules.append({"name": "Metrics counted in Python", "result": f"This step counts toward: {metric}", "state": "passed"})
    points = [p for p in d.get("review_points", []) if step["name"].lower() in p["point"].lower()]
    human_needed = step["classification"] == "human_decision"
    return {
        "kind": "automation_step", "kicker": f"Automation Finder · Step {step['number']}",
        "recommendation": {"text": f"{step['name']}: {cls['label']}", "owner": None,
                           "source": "Adjusted by policy" if step["adjusted"] else "AI recommendation",
                           "context": cls["meaning"], "link": f"/automation/{analysis_id}"},
        "provenance": _provenance(d.get("source", "live"), d.get("model"), d.get("response_id"), d["created_at"], d.get("latency_ms")),
        "confidence": None,
        "notices": [],
        "evidence": {"intro": "The stored input. Process analyses do not use project records.", "blocks": evidence},
        "ai": {"intro": "What the model returned for this step.", "blocks": ai_blocks},
        "rules": {"intro": "Deterministic checks applied after the AI responded.", "items": rules, "passed": []},
        "human": {"required": human_needed,
                  "headline": ("Yes. This step is a decision a person makes in the process." if human_needed else
                               "No approval needed for this step. The analysis is advisory: nothing is automated."),
                  "reasons": [{"text": f"{p['point']}: {p['reason']}", "source": "policy" if p.get("source") == "policy" else "ai"} for p in points],
                  "reasons_title": "Human review points" if points else None,
                  "effects": [], "decision": None},
        "footer": f"Built from stored process analysis #{analysis_id}.",
    }


# --- Explanation: Workflow Builder proposal (or one step) ------------------------------------------

NODE_TYPES = {"trigger": "Trigger", "ai": "AI step", "condition": "Condition", "automation": "Automation",
              "human_approval": "Human approval", "action": "Action"}
INVOLVEMENT = {"none": "None", "assists": "Assists a person", "performs": "Performs the step"}


def workflow(db, design_id, node_id=None):
    from services import workflow_designer as wd
    d = wd.load(db, design_id)
    if d is None:
        return None
    df = d["definition"]
    node = next((n for n in df["nodes"] if n["id"] == node_id), None) if node_id else None
    if node_id and node is None:
        return None
    events = [fact(e["action"], e["at"][:16].replace("T", " "), "info",
                   {"AI": "AI", "AUTOMATION": "OpsPilot checks", "HUMAN": "Person"}.get(e["actor"], e["actor"]))
              for e in d["events"][:6]]
    evidence = [{"type": "quote", "title": "Workflow description (as the employee entered it)", "text": d["description"]},
                {"type": "facts", "title": "Stored design", "items": [fact("Status", d["status"] or "Unsaved proposal",
                                                                             "ok" if d["status"] == "Active" else "info"),
                                                                        fact("Steps", len(df["nodes"])),
                                                                        fact("Approval steps", df.get("approval_points", 0))]}]
    if events:
        evidence.append({"type": "facts", "title": "Recent history", "items": events})
    notes = df.get("notes", [])
    if node:
        ai_blocks = [{"type": "field", "label": "Step type", "value": NODE_TYPES.get(node["type"], node["type"])},
                     {"type": "text", "label": "What it does", "text": node["description"] or "—"},
                     {"type": "text", "label": "Why it exists", "text": node["purpose"] or "—"},
                     {"type": "field", "label": "AI involvement", "value": INVOLVEMENT.get(node["ai_involvement"], node["ai_involvement"])
                      + (f": {node['ai_role']}" if node.get("ai_role") else "")}]
        if node.get("data_used"):
            ai_blocks.append({"type": "list", "label": "Data this step uses (named by the AI)",
                              "items": [{"text": x} for x in node["data_used"]]})
        rules = [{"name": "Added by OpsPilot", "result": "The human-judgment policy inserted this approval step", "state": "adjusted"}] \
            if node.get("added_by_policy") else []
        rules += [{"name": "Policy note", "result": p, "state": "adjusted"} for p in node.get("policy", [])
                  if not (node.get("added_by_policy") and p.startswith("Added by"))]
        if not rules:
            rules.append({"name": "Structure and policy checks", "result": "Passed without changes", "state": "passed"})
        needs = node["type"] == "human_approval" or node.get("requires_human_approval")
        human = {"required": bool(needs),
                 "headline": (f"Yes. {node.get('approver_role') or 'A team member'} approves at this step." if needs
                              else "No approval at this step. The whole workflow still needs a person's approval before it is Active."),
                 "reasons": [], "effects": [], "decision": None}
        rec = {"text": node["title"], "owner": None, "source": "Added by policy" if node.get("added_by_policy") else "AI proposal",
               "context": f"{NODE_TYPES.get(node['type'], node['type'])} in “{d['name']}”", "link": f"/workflows/{design_id}"}
        kicker = "Workflow step"
    else:
        ai_blocks = [{"type": "field", "label": "Workflow name", "value": d["name"]},
                     {"type": "text", "label": "Summary", "text": df.get("summary") or "—"},
                     {"type": "list", "label": "Steps proposed", "items": [
                         {"text": n["title"], "meta": NODE_TYPES.get(n["type"], n["type"]) + (" · added by OpsPilot" if n.get("added_by_policy") else "")}
                         for n in df["nodes"]]}]
        if df.get("assumptions"):
            ai_blocks.append({"type": "list", "label": "Assumptions the AI made", "items": [{"text": x} for x in df["assumptions"]]})
        state = {"fix": "adjusted", "policy": "adjusted", "warn": "triggered", "info": "checked"}
        name = {"fix": "Structure fixed", "policy": "Human-approval policy", "warn": "Warning", "info": "Note"}
        rules = [{"name": name.get(n["kind"], "Check"), "result": n["text"], "state": state.get(n["kind"], "checked")} for n in notes]
        if not any(n["kind"] in ("fix", "warn") for n in notes):
            rules.append({"name": "Graph validation", "result": "One trigger, real connections, labelled branches: passed without fixes",
                          "state": "passed"})
        if not any(n["kind"] == "policy" for n in notes):
            rules.append({"name": "Human-approval policy", "result": "Consequential and customer-facing steps already sit behind an approval",
                          "state": "passed"})
        approvals = [n for n in df["nodes"] if n["type"] == "human_approval"]
        item, dec = _approval_decision(db, "workflow_design_id", design_id)
        decision = _decision_block(item, dec) if item else None
        if decision is None:
            decision = {"status": (d["status"] or "unsaved").lower().replace(" ", "_"),
                        "label": {"Active": "Approved and active", "Pending Approval": "Waiting for a person", "Draft": "Draft: not yet submitted",
                                  "Disabled": "Disabled"}.get(d["status"], "Unsaved proposal"),
                        "at": d.get("approved_at")}
        human = {"required": True, "headline": "Yes. A person must approve the workflow before it becomes Active.",
                 "reasons": [{"text": f"{n['title']} ({n.get('approver_role') or 'Team member'})",
                              "source": "policy" if n.get("added_by_policy") else "ai"} for n in approvals],
                 "reasons_title": "Approval steps inside the workflow" if approvals else None,
                 "effects": [{"label": "If approved", "tone": "approve", "text": "Status becomes Active. This prototype does not run workflows."},
                             {"label": "If returned", "tone": "reject", "text": "Status goes back to Draft for changes."}],
                 "decision": decision}
        rec = {"text": d["name"], "owner": None, "source": "AI proposal", "context": df.get("summary") or None,
               "link": f"/workflows/{design_id}"}
        kicker = "Workflow proposal"
    return {
        "kind": "workflow", "kicker": kicker, "recommendation": rec,
        "provenance": _provenance("live" if d.get("response_id") else "synthetic", d.get("model"), d.get("response_id"), d["created_at"],
                                  d.get("latency_ms")),
        "confidence": None, "notices": [],
        "evidence": {"intro": "The stored input and history. Workflow proposals do not read project records.", "blocks": evidence},
        "ai": {"intro": "What the model proposed.", "blocks": ai_blocks},
        "rules": {"intro": "OpsPilot's validation and human-approval policy.", "items": rules, "passed": []},
        "human": human,
        "footer": f"Built from stored workflow design #{design_id}" + (f", step “{node['title']}”" if node else "") + ".",
    }


# --- Dispatch ---------------------------------------------------------------------------------------

def approval_item(db, item_id):
    from services import approvals
    item = approvals.get(db, item_id)
    if item is None:
        return None
    if item["kind"] in ("recommendation", "decision") and item.get("analysis_id"):
        return analysis(db, item["analysis_id"])
    if item["kind"] == "communication":
        return communication(db, item)
    if item["kind"] == "workflow" and item.get("workflow_design_id"):
        return workflow(db, item["workflow_design_id"])
    return None
