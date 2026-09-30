"""
Workflow layer: the only place that writes project state.

Every write here is paired with an ACTIVITY_LOG entry tagged with who made
the change: AI, AUTOMATION (business rules) or HUMAN. AI results are stored
as suggestions only; nothing in this module lets AI change a project's stage
or status. Those changes require a human action.
"""
import json
from datetime import date, timedelta

import config
from database import now_iso

STAGES = ["New Lead", "Information Gathering", "Document Review", "Assessment",
          "Ready for Next Step", "Completed"]
STATUSES = ["Active", "Needs Attention", "Human Review Required", "Blocked", "Completed"]
PRIORITIES = ["High", "Medium", "Low"]
TASK_STATUSES = ["Open", "In Progress", "Completed"]
DOCUMENT_STATUSES = ["Missing", "Requested", "Received", "Verified"]
ACTION_TYPES = ["AI", "AUTOMATION", "HUMAN"]

# Statuses the rules engine is allowed to set or clear. Blocked and Completed are human-only.
AUTOMATION_STATUSES = {"Active", "Needs Attention", "Human Review Required"}


class WorkflowError(ValueError):
    """Raised when a requested change is not allowed; message is user-safe."""


# --- Activity log ------------------------------------------------------------

def log_activity(db, project_id, action, action_type, details=None):
    if action_type not in ACTION_TYPES:
        raise ValueError(f"Unknown action type: {action_type}")
    if isinstance(details, (dict, list)):
        details = json.dumps(details)
    db.execute(
        "INSERT INTO activity_log (project_id, action, action_type, details, created_at) VALUES (?, ?, ?, ?, ?)",
        (project_id, action, action_type, details, now_iso()))


def touch_project(db, project_id):
    db.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (now_iso(), project_id))


# --- Tasks -------------------------------------------------------------------

def create_task(db, project_id, title, department, priority="Medium", description=None,
                due_in_days=3, source="HUMAN", rule_key=None):
    due = (date.today() + timedelta(days=due_in_days)).isoformat() if due_in_days is not None else None
    cur = db.execute(
        """INSERT INTO tasks (project_id, title, description, department, priority, status,
                              due_date, source, rule_key, created_at)
           VALUES (?, ?, ?, ?, ?, 'Open', ?, ?, ?, ?)""",
        (project_id, title, description, department, priority, due, source, rule_key, now_iso()))
    return cur.lastrowid


def set_task_status(db, task_id, new_status):
    if new_status not in TASK_STATUSES:
        raise WorkflowError("Unknown task status.")
    task = db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
    if task is None:
        raise WorkflowError("Task not found.")
    if task["status"] == new_status:
        return task
    db.execute("UPDATE tasks SET status = ? WHERE id = ?", (new_status, task_id))
    log_activity(db, task["project_id"], f"Task marked {new_status}: {task['title']}", "HUMAN",
                 {"task_id": task_id, "from": task["status"], "to": new_status})
    touch_project(db, task["project_id"])
    return task


# --- Projects ----------------------------------------------------------------

def create_project(db, form, synthetic=False, created_at=None):
    """Create customer + project + required-document checklist. Returns project id."""
    ts = created_at or now_iso()
    cur = db.execute(
        """INSERT INTO customers (first_name, last_name, email, phone, address, is_synthetic, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (form["first_name"], form["last_name"], form.get("email") or None, form.get("phone") or None,
         form.get("address") or None, int(synthetic), ts))
    customer_id = cur.lastrowid
    cur = db.execute(
        """INSERT INTO projects (customer_id, service_type, monthly_electric_bill, roof_age, project_status,
                                 current_stage, priority, notes, is_synthetic, created_at, updated_at)
           VALUES (?, ?, ?, ?, 'Active', 'New Lead', ?, ?, ?, ?, ?)""",
        (customer_id, form["service_type"], form.get("monthly_electric_bill"), form.get("roof_age"),
         form.get("priority") or "Medium", form.get("notes") or None, int(synthetic), ts, ts))
    project_id = cur.lastrowid

    received = set(form.get("documents_received") or [])
    for doc_type in config.REQUIRED_DOCUMENTS.get(form["service_type"], []):
        status = "Received" if doc_type in received else "Missing"
        filename = f"{doc_type.lower().replace(' ', '_')}_{project_id}.pdf" if status == "Received" else None
        db.execute("INSERT INTO documents (project_id, document_type, filename, status, created_at) "
                   "VALUES (?, ?, ?, ?, ?)", (project_id, doc_type, filename, status, ts))
    return project_id


def update_workflow(db, project_id, stage, status, priority, note=""):
    """Human-initiated stage/status/priority change."""
    if stage not in STAGES or status not in STATUSES or priority not in PRIORITIES:
        raise WorkflowError("Choose a valid stage, status and priority.")
    project = db.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone()
    if project is None:
        raise WorkflowError("Project not found.")
    if (stage == "Completed") != (status == "Completed"):
        raise WorkflowError("Stage and status must both be Completed to close a project.")

    changes = {}
    for field, new in (("current_stage", stage), ("project_status", status), ("priority", priority)):
        if project[field] != new:
            changes[field] = {"from": project[field], "to": new}
    if not changes and not note:
        raise WorkflowError("Nothing changed.")

    db.execute("UPDATE projects SET current_stage = ?, project_status = ?, priority = ?, updated_at = ? "
               "WHERE id = ?", (stage, status, priority, now_iso(), project_id))
    labels = {"current_stage": "Stage", "project_status": "Status", "priority": "Priority"}
    summary = "; ".join(f"{labels[k]} {v['from']} → {v['to']}" for k, v in changes.items()) or "note added"
    log_activity(db, project_id, f"Workflow updated: {summary}", "HUMAN",
                 {"changes": changes, "note": note or None})
    return changes


def mark_document_received(db, document_id):
    doc = db.execute("SELECT * FROM documents WHERE id = ?", (document_id,)).fetchone()
    if doc is None:
        raise WorkflowError("Document not found.")
    if doc["status"] in ("Received", "Verified"):
        raise WorkflowError("This document is already on file.")
    filename = f"{doc['document_type'].lower().replace(' ', '_')}_{doc['project_id']}.pdf"
    db.execute("UPDATE documents SET status = 'Received', filename = ? WHERE id = ?", (filename, document_id))
    log_activity(db, doc["project_id"], f"{doc['document_type']} marked as received", "HUMAN",
                 {"document_id": document_id, "filename": filename})
    touch_project(db, doc["project_id"])
    return doc


# --- AI suggestions (stored, never auto-applied) -------------------------------

def insert_analysis(db, project_id, result, derived):
    """Persist a validated AI analysis as a *pending* suggestion, with the rule-checked fields."""
    data = result.data
    cur = db.execute(
        """INSERT INTO ai_analyses (project_id, summary, detected_intent, intent_detail, information_status,
                                    info_status_adjusted, missing_information, recommended_action,
                                    recommendation_why, reasoning, confidence, concerns, suggested_department,
                                    suggested_tasks, requires_human_review, human_review_reasons, context_hash,
                                    evidence_snapshot, review_status, source, model, response_id, latency_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 'live', ?, ?, ?, ?)""",
        (project_id, data["summary"], data["detected_intent"], data["intent_detail"],
         derived["information_status"], int(derived["info_status_adjusted"]),
         json.dumps(derived["missing_information"]), data["recommended_action"],
         json.dumps(data["recommendation_why"]), data["reasoning"], data["confidence"],
         json.dumps(data["concerns"]), data["suggested_department"], json.dumps(derived["suggested_tasks"]),
         int(derived["requires_human_review"]), json.dumps(derived["human_review_reasons"]),
         derived["context_hash"], json.dumps(derived.get("evidence_snapshot"), default=str) if derived.get("evidence_snapshot") else None,
         result.model, result.response_id, result.latency_ms, now_iso()))
    return cur.lastrowid


def update_suggested_tasks(db, analysis_id, suggestions):
    db.execute("UPDATE ai_analyses SET suggested_tasks = ? WHERE id = ?", (json.dumps(suggestions), analysis_id))
