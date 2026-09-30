"""
Human Approval Center.

One queue for every AI output that needs a person before anything customer-facing or
consequential happens:

  communication   AI-drafted customer messages (drafted here with the real Anthropic API)
  recommendation  pending AI next-step recommendations from project analysis
  decision        recommendations OpsPilot flagged as needing human judgment
                  (low confidence, unclear intent, roof review, AI asked for review)
  workflow        workflow proposals waiting for approval

Audit guarantees (enforced in the database, not just in code):
  * approval_items.ai_output is written once. A trigger rejects any UPDATE of it.
  * approval_decisions is append-only. Triggers reject UPDATE and DELETE.
  * every decision stores the original AI output, the human's final version (if edited),
    which fields changed, who decided, where, and when.

Nothing here sends anything externally. Approving a message marks it approved.
"""
import json
import re
from datetime import date

from pydantic import BaseModel

import config
import database as db_layer
from database import now_iso
from services import ai_service, rules_engine, workflow, workflow_designer

KINDS = {
    "communication": {"label": "Customer communication", "filter": "communications", "icon": "mail"},
    "recommendation": {"label": "AI recommendation", "filter": "recommendations", "icon": "sparkle"},
    "workflow": {"label": "Workflow proposal", "filter": "workflows", "icon": "workflow"},
    "decision": {"label": "Project decision", "filter": "other", "icon": "split"},
}
FILTERS = [("all", "All"), ("communications", "Communications"), ("recommendations", "Recommendations"),
           ("workflows", "Workflows"), ("other", "Other")]
DECIDED_BY = config.DEMO_USER["name"]

PURPOSES = {
    "missing_information": ("Request missing information",
                            "Politely tell the customer exactly which items are still needed and why they help."),
    "roof_concern": ("Acknowledge roof concern",
                     "Acknowledge the customer's concern about their roof and explain that qualified staff will "
                     "review their roof information before next steps. Give no technical judgment."),
    "status_update": ("General status update",
                      "Give a short, friendly update on where the project stands and what happens next."),
}
PURPOSE_ALIASES = {"missing_documents": "missing_information"}

STAGE_PLAIN = {"New Lead": "just getting started", "Information Gathering": "collecting information",
               "Document Review": "reviewing documents", "Assessment": "being assessed",
               "Ready for Next Step": "ready for the next step", "Completed": "complete"}


# --- Schema and SQL helpers ---------------------------------------------------------------------

class CommunicationDraft(BaseModel):
    subject: str
    body: str
    reviewer_note: str
    confidence: float


DRAFT_INSTRUCTIONS = """
You draft short, warm, professional customer messages for a residential energy services company.
You receive only the minimum business context. Use nothing else.

- Address the customer by first name. Sign off exactly as "The Project Team".
- 70-170 words, plain language, short paragraphs. If items are needed, list them clearly (a short list is fine).
- Never promise prices, savings, dates, approvals or outcomes. Never give technical, structural or safety opinions.
- Do not invent details, links, phone numbers or names. No placeholders like [Name].
- reviewer_note: one sentence telling the employee what to double-check before approving.
- confidence: 0.0-1.0, your estimate that the draft covers what the context asks for and can be approved
  without edits. Lower it if the context is thin or ambiguous.
This is a draft for a person to review. It will not be sent automatically.
""".strip()


def _j(value):
    return json.dumps(value, default=str)


def _load(row):
    if row is None:
        return None
    d = dict(row)
    for k in ("ai_output", "justification", "evidence", "checks", "ai_input"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except (TypeError, ValueError):
            d[k] = None
    d["kind_label"] = KINDS[d["kind"]]["label"]
    d["filter"] = KINDS[d["kind"]]["filter"]
    d["icon"] = KINDS[d["kind"]]["icon"]
    return d


def _insert(db, **f):
    f.setdefault("status", "pending")
    f.setdefault("created_at", now_iso())
    for k in ("ai_output", "justification", "evidence", "checks", "ai_input"):
        if k in f and not isinstance(f[k], str) and f[k] is not None:
            f[k] = _j(f[k])
    cols = ", ".join(f)
    cur = db.execute(f"INSERT INTO approval_items ({cols}) VALUES ({', '.join('?' * len(f))})", list(f.values()))
    return cur.lastrowid


# --- Evidence builders (business data a reviewer needs) ------------------------------------------

def _project_evidence(db, project_id):
    p = db_layer.get_project(db, project_id)
    if p is None:
        return None, None
    docs = db_layer.project_documents(db, project_id)
    tasks = db_layer.project_tasks(db, project_id, include_completed=True)
    findings = [f for f in rules_engine.evaluate(p, docs, tasks) if not f.resolved]
    return p, {
        "customer": p["customer_name"], "project_id": p["id"], "service": p["service_type"],
        "stage": p["current_stage"], "status": p["project_status"], "priority": p["priority"],
        "rule_findings": [f.message for f in findings],
        "documents": [{"type": d["document_type"], "status": d["status"]} for d in docs],
        "open_tasks": [{"title": t["title"], "department": t["department"], "due": t["due_date"],
                        "overdue": bool(t["is_overdue"])} for t in tasks if t["status"] != "Completed"][:6],
        "contact_missing": [f for f in config.REQUIRED_CONTACT_FIELDS if not (p[f] or "").strip()],
    }


# --- Sync: keep the queue in step with AI analyses and workflow proposals ----------------------------

def sync(db):
    """Create queue items for new pending AI outputs; supersede items whose source moved on."""
    latest = db.execute("""SELECT a.* FROM ai_analyses a
                           WHERE a.id = (SELECT MAX(id) FROM ai_analyses x WHERE x.project_id = a.project_id)""").fetchall()
    latest_pending = {r["id"]: r for r in latest if r["review_status"] == "Pending"}
    have = {r["analysis_id"] for r in db.execute("SELECT analysis_id FROM approval_items WHERE analysis_id IS NOT NULL")}
    for aid, a in latest_pending.items():
        if aid not in have:
            _item_from_analysis(db, db_layer.analysis_to_dict(a))
    for r in db.execute("SELECT id, analysis_id FROM approval_items WHERE status = 'pending' AND analysis_id IS NOT NULL").fetchall():
        if r["analysis_id"] not in latest_pending:
            db.execute("UPDATE approval_items SET status = 'superseded', decided_at = ? WHERE id = ?", (now_iso(), r["id"]))

    pending_designs = {r["id"]: r for r in db.execute("SELECT * FROM workflow_designs WHERE status = 'Pending Approval'")}
    open_wf = {r["workflow_design_id"]: r["id"] for r in db.execute(
        "SELECT id, workflow_design_id FROM approval_items WHERE status = 'pending' AND kind = 'workflow'")}
    for did in pending_designs:
        if did not in open_wf:
            _item_from_workflow(db, workflow_designer.load(db, did))
    for did, iid in open_wf.items():
        if did not in pending_designs:
            db.execute("UPDATE approval_items SET status = 'superseded', decided_at = ? WHERE id = ?", (now_iso(), iid))


def _item_from_analysis(db, a):
    p, ev = _project_evidence(db, a["project_id"])
    if p is None:
        return None
    kind = "decision" if a.get("requires_human_review") else "recommendation"
    why = [w for w in (a.get("recommendation_why") or [])]
    reasons = [r["text"] for r in (a.get("human_review_reasons") or [])]
    ev.update(missing_information=[m["item"] for m in (a.get("missing_information") or [])],
              detected_intent=a.get("detected_intent"), information_status=a.get("information_status"),
              human_review_reasons=reasons)
    return _insert(db, kind=kind, project_id=a["project_id"], analysis_id=a["id"], title=a["recommended_action"],
                   ai_output={"recommended_action": a["recommended_action"], "suggested_department": a["suggested_department"],
                              "summary": a["summary"]},
                   justification={"points": why, "rationale": a.get("reasoning") or "",
                                  "needs_judgment": reasons if kind == "decision" else []},
                   evidence=ev, confidence=a["confidence"],
                   confidence_basis="The model's own estimate when it analyzed the project.",
                   potential_action=(f"Create a {a['suggested_department']} task for this action, unless an open task "
                                     "already covers it. The project's stage and status do not change."),
                   reject_effect="Recorded as overridden. No task is created and business rules keep running.",
                   source=a["source"], model=a.get("model"), response_id=a.get("response_id"), created_at=a["created_at"])


def _item_from_workflow(db, d):
    if d is None:
        return None
    df = d["definition"]
    policy = [n["text"] for n in df["notes"] if n["kind"] == "policy"]
    fixes = [n["text"] for n in df["notes"] if n["kind"] in ("fix", "warn")]
    requested = next((e["at"] for e in d["events"] if e["action"] == "Approval requested"), now_iso())
    return _insert(db, kind="workflow", workflow_design_id=d["id"], title=d["name"],
                   ai_output={"name": d["name"], "summary": df["summary"],
                              "steps": [{"type": n["type"], "title": n["title"], "added_by_policy": n["added_by_policy"]}
                                        for n in df["nodes"]]},
                   justification={"points": [df["summary"]] if df["summary"] else [], "rationale": "",
                                  "needs_judgment": policy},
                   evidence={"steps": len(df["nodes"]), "ai_steps": df["ai_nodes"], "approval_points": df["approval_points"],
                             "conditions": df["counts"].get("condition", 0), "policy_additions": policy,
                             "validation_fixes": fixes, "description": d["description"][:400]},
                   confidence=None,
                   confidence_basis=f"Not scored. OpsPilot validated the structure ({len(df['notes'])} check notes).",
                   potential_action="Make this workflow design Active. (Prototype: Active designs are approved, not executed.)",
                   reject_effect="Return the workflow to Draft so it can be changed or regenerated.",
                   source="live", model=d["model"], response_id=d["response_id"], created_at=requested)


# --- Drafting customer communications (real Claude API) -----------------------------------------------

def minimal_context(db, project_id, purpose):
    """Only what the message needs. No last name, email, phone, address, bill amount, roof age or raw notes."""
    p, ev = _project_evidence(db, project_id)
    if p is None:
        raise LookupError("That project no longer exists.")
    missing_docs = [d["type"] for d in ev["documents"] if d["status"] in ("Missing", "Requested")]
    ctx = {
        "customer_first_name": p["first_name"],
        "service": p["service_type"],
        "project_stage": STAGE_PLAIN.get(p["current_stage"], "in progress"),
        "message_goal": PURPOSES[purpose][1],
        "missing_documents": missing_docs,
        "missing_contact_details": ev["contact_missing"],
        "roof_review_pending": any("Roof information requires review" in f for f in ev["rule_findings"]),
        "customer_mentioned_roof_concern": bool(re.search(r"\broof\b", p["notes"] or "", re.I)),
        "sign_off": "The Project Team",
    }
    return p, ev, ctx


FIELD_WORDS = {"email": ["email"], "phone": ["phone", "number"], "address": ["address"]}
RISKY = re.compile(r"\$\s?\d|\d+\s?%|\bguarantee\w*|\bpromise\w*|\bwill be approved\b|\bwithin \d+\s+(business )?(days|hours)\b|"
                   r"\bby (monday|tuesday|wednesday|thursday|friday|saturday|sunday|tomorrow|next week)\b|\bfree\b", re.I)
PLACEHOLDER = re.compile(r"\[[^\]]+\]|\{[^}]+\}|<[^>]+>|\bTBD\b|\bXX+\b")


def draft_checks(draft, ctx):
    body, words = draft["body"], len(draft["body"].split())
    lower = body.lower()
    missing = ctx["missing_documents"] + ctx["missing_contact_details"]
    not_mentioned = []
    for item in missing:
        keys = FIELD_WORDS.get(item, [w for w in re.findall(r"[a-z]+", item.lower()) if len(w) > 3])
        if not any(k in lower for k in keys):
            not_mentioned.append(item)
    checks = [
        ("Greets the customer by first name", ctx["customer_first_name"].lower() in lower[:120]),
        ("Mentions every missing item" if missing else "No missing items to request",
         not not_mentioned),
        ("No prices, dates, guarantees or promises", not RISKY.search(body + " " + draft["subject"])),
        ("No placeholders or unfinished text", not PLACEHOLDER.search(body + " " + draft["subject"])),
        ("Signed “The Project Team”", "the project team" in lower[-80:]),
        ("Reasonable length (50–220 words)", 50 <= words <= 220),
    ]
    out = [{"label": l, "ok": bool(ok)} for l, ok in checks]
    if not_mentioned:
        out[1]["detail"] = "Not mentioned: " + ", ".join(not_mentioned)
    return out


def draft_communication(db, project_id, purpose):
    """Draft with the real Anthropic API and queue it for approval. Never sends anything."""
    purpose = PURPOSE_ALIASES.get(purpose, purpose)
    if purpose not in PURPOSES:
        raise ValueError("Choose what the message is for.")
    p, ev, ctx = minimal_context(db, project_id, purpose)
    parsed, result = ai_service._structured_request(
        "customer_communication_draft", DRAFT_INSTRUCTIONS, "Context (JSON):\n" + json.dumps(ctx, indent=2),
        CommunicationDraft)
    subject = re.sub(r"\s+", " ", parsed.subject or "").strip()[:150]
    body = (parsed.body or "").strip()[:2500]
    if not subject or not body:
        raise ai_service.AIServiceError("invalid_output", "AI returned an incomplete draft",
                                        "The draft was missing a subject or message, so it was not used.", 502)
    if not 0 <= parsed.confidence <= 1:
        raise ai_service.AIServiceError("invalid_output", "AI returned an invalid confidence score",
                                        "Confidence must be between 0 and 1, so the draft was not used.", 502)
    draft = {"subject": subject, "body": body, "reviewer_note": (parsed.reviewer_note or "").strip()[:300],
             "purpose": PURPOSES[purpose][0]}
    checks = draft_checks(draft, ctx)
    ev["missing_information"] = ctx["missing_documents"] + ctx["missing_contact_details"]
    item_id = _insert(
        db, kind="communication", project_id=project_id, title=subject, ai_output=draft,
        justification={"points": [f"Purpose: {PURPOSES[purpose][0]}."] + (
            [f"Still needed: {', '.join(ev['missing_information'])}."] if ev["missing_information"] else []),
            "rationale": draft["reviewer_note"], "needs_judgment": []},
        evidence=ev, confidence=round(parsed.confidence, 2),
        confidence_basis="The model's estimate that this draft covers what's needed without edits.",
        potential_action="Mark the message as approved and log it on the project. Nothing is sent (prototype).",
        reject_effect="The draft is discarded and the decision is logged. Nothing is sent.",
        checks=checks, ai_input=ctx, source="live", model=result.model, response_id=result.response_id)
    workflow.log_activity(db, project_id, f"AI drafted a customer message ({PURPOSES[purpose][0].lower()})", "AI",
                          {"model": result.model, "response_id": result.response_id, "approval_item": item_id})
    return item_id, result


# --- Decisions ---------------------------------------------------------------------------------------

class DecisionError(ValueError):
    pass


EDITABLE = {"communication": ("subject", "body"), "recommendation": ("recommended_action", "suggested_department"),
            "decision": ("recommended_action", "suggested_department"), "workflow": ("name",)}


def _validate_edits(kind, original, edits):
    if not isinstance(edits, dict):
        return None, []
    final = dict(original)
    for key in EDITABLE[kind]:
        if key in edits:
            v = edits[key]
            if not isinstance(v, str):
                raise DecisionError("Edits must be text.")
            v = v.strip()
            limit = {"subject": 150, "body": 2500, "recommended_action": 200, "name": 80}.get(key, 60)
            if not v:
                raise DecisionError(f"{key.replace('_', ' ').capitalize()} can't be empty.")
            if key == "suggested_department" and v not in config.DEPARTMENTS:
                raise DecisionError("Choose a valid department.")
            final[key] = v[:limit]
    changed = [k for k in EDITABLE[kind] if final.get(k) != original.get(k)]
    return final if changed else None, changed


def decide(db, item_id, decision, edits=None, note="", via="Approval Center"):
    """Approve (optionally with edits) or reject. Records an immutable decision and applies the effect."""
    if decision not in ("approve", "reject"):
        raise DecisionError("Unknown decision.")
    row = db.execute("SELECT * FROM approval_items WHERE id = ?", (item_id,)).fetchone()
    if row is None:
        raise LookupError("That approval item no longer exists.")
    item = _load(row)
    if item["status"] != "pending":
        raise DecisionError(f"This item was already {item['status']}.")
    note = (note.strip()[:500] if isinstance(note, str) else "")
    original = item["ai_output"]
    final, changed = (None, [])
    if decision == "approve" and edits:
        final, changed = _validate_edits(item["kind"], original, edits)
    kind = "approved_with_edits" if final else ("approved" if decision == "approve" else "rejected")
    result_text = _apply(db, item, decision, final or original, bool(final), note)
    ts = now_iso()
    db.execute("""INSERT INTO approval_decisions (item_id, decision, original_output, final_output, changed_fields, note,
                  decided_by, decided_via, outcome, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
               (item_id, kind, _j(original), _j(final) if final else None, _j(changed), note or None, DECIDED_BY, via,
                result_text, ts))
    db.execute("UPDATE approval_items SET status = ?, decided_at = ? WHERE id = ?",
               ("approved" if decision == "approve" else "rejected", ts, item_id))
    return kind, result_text


def _apply(db, item, decision, final, edited, note):
    """The effect of a decision. Every branch is a human action and is logged as HUMAN."""
    k, pid = item["kind"], item["project_id"]
    suffix = " (edited by reviewer)" if edited else ""
    if k == "communication":
        if decision == "approve":
            workflow.log_activity(db, pid, f"Customer message approved{suffix}: “{final['subject']}” (not sent in prototype)",
                                  "HUMAN", {"approval_item": item["id"], "subject": final["subject"], "note": note or None})
            workflow.touch_project(db, pid)
            return ("Approved with your edits. Nothing was sent." if edited else "Message approved. Nothing was sent.")
        workflow.log_activity(db, pid, f"AI-drafted customer message rejected: “{item['ai_output']['subject']}”", "HUMAN",
                              {"approval_item": item["id"], "note": note or None})
        return "Draft rejected. Nothing was sent."

    if k in ("recommendation", "decision"):
        a = db.execute("SELECT * FROM ai_analyses WHERE id = ?", (item["analysis_id"],)).fetchone()
        if a is None or a["review_status"] != "Pending":
            raise DecisionError("This recommendation is no longer pending.")
        label = "AI recommendation" if k == "recommendation" else "Project decision"
        if decision == "reject":
            db.execute("UPDATE ai_analyses SET review_status = 'Dismissed' WHERE id = ?", (a["id"],))
            workflow.log_activity(db, pid, f"{label} rejected by team member", "HUMAN",
                                  {"approval_item": item["id"], "analysis_id": a["id"], "note": note or None})
            workflow.touch_project(db, pid)
            return "Recommendation rejected. No task was created."
        from services.intelligence import similar_task  # local import avoids a cycle
        db.execute("UPDATE ai_analyses SET review_status = 'Accepted' WHERE id = ?", (a["id"],))
        tasks = db.execute("SELECT * FROM tasks WHERE project_id = ?", (pid,)).fetchall()
        existing = similar_task(final["recommended_action"], tasks)
        if existing:
            workflow.log_activity(db, pid, f"{label} approved{suffix} (already covered by: {existing['title']})", "HUMAN",
                                  {"approval_item": item["id"], "analysis_id": a["id"], "task_id": existing["id"], "note": note or None})
            outcome = f"Approved. No new task needed: “{existing['title']}” already covers it."
        else:
            tid = workflow.create_task(db, pid, final["recommended_action"][:140], final["suggested_department"],
                                       priority="Medium", due_in_days=3, source="AI",
                                       description="From an AI recommendation approved in the Human Approval Center."
                                                   + (" Edited by the reviewer." if edited else ""))
            workflow.log_activity(db, pid, f"{label} approved{suffix} and task created", "HUMAN",
                                  {"approval_item": item["id"], "analysis_id": a["id"], "task_id": tid, "note": note or None})
            outcome = f"Approved{' with your edits' if edited else ''}. Task created for {final['suggested_department']}."
        workflow.touch_project(db, pid)
        return outcome

    if k == "workflow":
        did = item["workflow_design_id"]
        if decision == "approve":
            if edited:
                db.execute("UPDATE workflow_designs SET name = ? WHERE id = ?", (final["name"], did))
                workflow_designer.log_event(db, did, f"Renamed during approval to “{final['name']}”", "HUMAN")
            workflow_designer.transition(db, did, "approve")
            workflow.log_activity(db, None, f"Workflow approved and activated: {final['name']}", "HUMAN",
                                  {"approval_item": item["id"], "workflow_design_id": did})
            return "Workflow approved and now Active."
        workflow_designer.transition(db, did, "return_to_draft")
        workflow.log_activity(db, None, f"Workflow proposal rejected and returned to draft: {item['ai_output']['name']}",
                              "HUMAN", {"approval_item": item["id"], "workflow_design_id": did, "note": note or None})
        return "Workflow returned to Draft."
    raise DecisionError("Unknown item type.")


# --- Reads -----------------------------------------------------------------------------------------

def get(db, item_id):
    item = _load(db.execute("SELECT * FROM approval_items WHERE id = ?", (item_id,)).fetchone())
    if item:
        item["decisions"] = [_decision(r) for r in db.execute(
            "SELECT * FROM approval_decisions WHERE item_id = ? ORDER BY id", (item_id,))]
        item["customer"] = (item["evidence"] or {}).get("customer")
    return item


def _decision(r):
    d = dict(r)
    for k in ("original_output", "final_output", "changed_fields"):
        d[k] = json.loads(d[k]) if d.get(k) else None
    return d


def list_items(db, view="pending"):
    if view == "history":
        rows = db.execute("SELECT * FROM approval_items WHERE status IN ('approved', 'rejected') ORDER BY decided_at DESC LIMIT 60")
    else:
        rows = db.execute("SELECT * FROM approval_items WHERE status = 'pending' ORDER BY created_at ASC")
    out = []
    for r in rows.fetchall():
        item = get(db, r["id"])
        out.append(item)
    return out


def counts(db):
    rows = db.execute("SELECT kind, COUNT(*) FROM approval_items WHERE status = 'pending' GROUP BY kind").fetchall()
    by_kind = dict(rows)
    by_filter = {f: 0 for f, _ in FILTERS}
    for kind, n in by_kind.items():
        by_filter[KINDS[kind]["filter"]] += n
    by_filter["all"] = sum(by_kind.values())
    today = date.today().isoformat()
    decided_today = db.execute("SELECT COUNT(*) FROM approval_decisions WHERE substr(created_at, 1, 10) = ?", (today,)).fetchone()[0]
    return {"pending": by_filter["all"], "by_filter": by_filter, "decided_today": decided_today}


def pending_count(db):
    return db.execute("SELECT COUNT(*) FROM approval_items WHERE status = 'pending'").fetchone()[0]


def project_options(db):
    rows = db_layer.list_projects(db)
    out = []
    for p in rows:
        if p["project_status"] == "Completed":
            continue
        _, ev = _project_evidence(db, p["id"])
        missing = [d["type"] for d in ev["documents"] if d["status"] in ("Missing", "Requested")] + \
                  [f"customer {f}" for f in ev["contact_missing"]]
        out.append({"id": p["id"], "name": p["customer_name"], "service": p["service_type"], "missing": missing,
                    "roof_review": any("Roof information requires review" in f for f in ev["rule_findings"])})
    out.sort(key=lambda x: (not x["missing"], x["name"]))
    return out
