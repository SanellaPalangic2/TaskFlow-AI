"""
Business rules engine — plain, deterministic Python.

Two steps, kept separate so the logic is easy to read and test:

  evaluate(project, documents, tasks) -> list of findings   (pure, no database)
  apply_rules(db, project_id)          -> performs actions   (creates tasks, flags status)

The same inputs always give the same findings and the same tasks, no matter
what the AI said. Rules only flag and route work. They never make
engineering, safety, legal, financial, permitting or installation decisions,
and they never move a project to a new stage — people do that.
"""
from dataclasses import dataclass, field

import config
from services import workflow

SEVERITY_ORDER = {"review": 0, "attention": 1}


@dataclass
class Finding:
    rule_key: str            # stable id, also used to de-duplicate tasks
    category: str            # contact | document | roof_info | roof_review
    severity: str            # "review" -> Human Review Required, "attention" -> Needs Attention
    message: str             # shown in the UI
    detected: str            # activity-log wording for the detection event
    task_title: str
    department: str
    priority: str = "Medium"
    due_in_days: int = 3
    missing_item: str = ""   # set when the finding is a piece of missing information
    resolved: bool = False
    details: dict = field(default_factory=dict)


def evaluate(project, documents, tasks):
    """Return every rule finding for a project. `project` must include customer contact fields."""
    findings = []
    completed_rule_keys = {t["rule_key"] for t in tasks if t["rule_key"] and t["status"] == "Completed"}

    # Rule 1 — required contact information
    missing = [f for f in config.REQUIRED_CONTACT_FIELDS if not (project[f] or "").strip()]
    if missing:
        findings.append(Finding(
            rule_key="contact_info", category="contact", severity="attention",
            message=f"Missing contact information: {', '.join(missing)}",
            detected=f"Missing contact information detected: {', '.join(missing)}",
            task_title="Collect missing contact information",
            department="Customer Support", priority="High", due_in_days=2,
            missing_item=f"Customer {', '.join(missing)}",
            details={"missing_fields": missing}))

    # Rule 2 — required documents for the service type
    for doc in documents:
        if doc["status"] in ("Missing", "Requested"):
            findings.append(Finding(
                rule_key=f"document:{doc['document_type']}", category="document", severity="attention",
                message=f"Required document not on file: {doc['document_type']}",
                detected=f"Missing document detected: {doc['document_type']}",
                task_title=f"Request {doc['document_type'].lower()}",
                department="Customer Support", priority="Medium", due_in_days=3,
                missing_item=doc["document_type"],
                details={"document_id": doc["id"], "document_type": doc["document_type"]}))

    # Rule 3 — roof information missing for services that depend on the roof
    if project["service_type"] in config.ROOF_INFO_SERVICES and project["roof_age"] is None:
        findings.append(Finding(
            rule_key="roof_info", category="roof_info", severity="attention",
            message="Roof information not provided (roof age unknown)",
            detected="Missing roof information detected: roof age",
            task_title="Collect roof information", department="Operations", priority="Medium",
            due_in_days=3, missing_item="Roof age"))

    # Rule 4 — roof age above the configurable demo threshold
    threshold = config.ROOF_AGE_REVIEW_THRESHOLD
    if project["roof_age"] is not None and project["roof_age"] > threshold:
        findings.append(Finding(
            rule_key="roof_review", category="roof_review", severity="review",
            message=f"Roof information requires review ({project['roof_age']} yrs, demo threshold {threshold} yrs)",
            detected=f"Roof age above review threshold: {project['roof_age']} yrs (threshold {threshold})",
            task_title="Review roof information with qualified staff",
            department="Project Review", priority="High", due_in_days=2,
            resolved="roof_review" in completed_rule_keys,
            details={"roof_age": project["roof_age"], "threshold": threshold}))

    return findings


def target_status(current_status, findings):
    """Status the rules would set. Blocked/Completed are owned by people and left alone."""
    if current_status not in workflow.AUTOMATION_STATUSES:
        return current_status
    open_findings = [f for f in findings if not f.resolved]
    if any(f.severity == "review" for f in open_findings):
        return "Human Review Required"
    if open_findings:
        return "Needs Attention"
    return "Active"


def load_state(db, project_id):
    project = db.execute("""SELECT p.*, c.email, c.phone, c.address, c.first_name, c.last_name
                            FROM projects p JOIN customers c ON c.id = p.customer_id WHERE p.id = ?""",
                         (project_id,)).fetchone()
    documents = db.execute("SELECT * FROM documents WHERE project_id = ?", (project_id,)).fetchall()
    tasks = db.execute("SELECT * FROM tasks WHERE project_id = ?", (project_id,)).fetchall()
    return project, documents, tasks


def apply_rules(db, project_id):
    """Run all rules for one project and act on the results. Returns a short summary."""
    project, documents, tasks = load_state(db, project_id)
    findings = evaluate(project, documents, tasks)
    actions = []

    # Create one task per finding (never duplicate an existing rule task).
    existing_keys = {t["rule_key"] for t in tasks if t["rule_key"]}
    for f in findings:
        if f.resolved or f.rule_key in existing_keys:
            continue
        workflow.log_activity(db, project_id, f.detected, "AUTOMATION", {"rule": f.rule_key, **f.details})
        workflow.create_task(db, project_id, f.task_title, f.department, priority=f.priority,
                             description=f.message, due_in_days=f.due_in_days,
                             source="AUTOMATION", rule_key=f.rule_key)
        workflow.log_activity(db, project_id, f"Follow-up task created: {f.task_title}", "AUTOMATION",
                              {"rule": f.rule_key, "task": f.task_title, "department": f.department,
                               "priority": f.priority})
        actions.append(f"Created task: {f.task_title}")

    # Close rule tasks whose underlying gap has been fixed (e.g. the document arrived).
    open_keys = {f.rule_key for f in findings}
    for t in tasks:
        key = t["rule_key"] or ""
        if (key.startswith("document:") or key in ("contact_info", "roof_info")) \
                and t["status"] != "Completed" and key not in open_keys:
            db.execute("UPDATE tasks SET status = 'Completed' WHERE id = ?", (t["id"],))
            workflow.log_activity(db, project_id, f"Task auto-closed: {t['title']} (information now on file)",
                                  "AUTOMATION", {"task_id": t["id"], "rule": key})
            actions.append(f"Closed task: {t['title']}")

    # Flag status (only between Active / Needs Attention / Human Review Required).
    new_status = target_status(project["project_status"], findings)
    if new_status != project["project_status"]:
        db.execute("UPDATE projects SET project_status = ? WHERE id = ?", (new_status, project_id))
        workflow.log_activity(db, project_id, f"Status flagged: {project['project_status']} → {new_status}",
                              "AUTOMATION", {"reason": [f.message for f in findings if not f.resolved]})
        actions.append(f"Status set to {new_status}")

    # Escalate priority when a human review is needed (rules only escalate, never downgrade).
    if any(f.severity == "review" and not f.resolved for f in findings) and project["priority"] != "High":
        db.execute("UPDATE projects SET priority = 'High' WHERE id = ?", (project_id,))
        workflow.log_activity(db, project_id, "Priority escalated to High (review required)", "AUTOMATION",
                              {"from": project["priority"], "to": "High"})
        actions.append("Priority escalated to High")

    if actions:
        workflow.touch_project(db, project_id)
    return {"findings": findings, "actions": actions}
