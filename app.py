"""
OpsPilot AI — Flask application (routes only).

Routes read data through database.py, change state through services/workflow.py,
run deterministic checks through services/rules_engine.py and call Claude only
through services/ai_service.py.

Run:  python app.py   ->  http://127.0.0.1:5000
"""
import json
import logging
import re
import sys
from datetime import date, datetime

from flask import (Flask, abort, flash, jsonify, redirect, render_template, request,
                   url_for)

import config
import database as db_layer
from database import get_db
from seed import init_db
from markupsafe import Markup, escape

from services import (ai_service, approvals, automation_finder, copilot, intelligence, rules_engine, workflow,
                      workflow_designer)
from services import dashboard as dashboard_view
from services import explain
from services import evaluation

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # keep request noise out of the demo terminal

app = Flask(__name__)
app.config["SECRET_KEY"] = config.SECRET_KEY
app.teardown_appcontext(db_layer.close_db)

NAV = [
    {"endpoint": "dashboard", "label": "Dashboard", "icon": "grid"},
    {"endpoint": "projects", "label": "Projects", "icon": "folder"},
    {"endpoint": "tasks", "label": "Tasks", "icon": "check-square"},
    {"endpoint": "copilot_page", "label": "Copilot", "icon": "message"},
    {"endpoint": "automation_page", "label": "Automation Finder", "icon": "zap"},
    {"endpoint": "workflows_page", "label": "Workflows", "icon": "workflow"},
    {"endpoint": "approvals_page", "label": "Approvals", "icon": "stamp"},
    {"endpoint": "evaluation_page", "label": "Evaluation Lab", "icon": "beaker"},
]

PLANNED_MODULES = {}


# --- Template helpers ------------------------------------------------------------------

@app.context_processor
def inject_shell():
    db = get_db()
    return {
        "nav_items": NAV,
        "demo_user": config.DEMO_USER,
        "ai_configured": ai_service.is_configured(),
        "ai_model": config.AI_MODEL,
        "config_threshold": config.HUMAN_REVIEW_CONFIDENCE_THRESHOLD,
        "nav_counts": {
            "tasks": db.execute("SELECT COUNT(*) FROM tasks WHERE status != 'Completed'").fetchone()[0],
            "projects": db.execute(
                "SELECT COUNT(*) FROM projects WHERE project_status IN "
                "('Needs Attention','Blocked','Human Review Required')").fetchone()[0],
            "approvals": approvals.pending_count(db),
        },
        "STAGES": workflow.STAGES, "STATUSES": workflow.STATUSES, "PRIORITIES": workflow.PRIORITIES,
        "TASK_STATUSES": workflow.TASK_STATUSES, "DEPARTMENTS": config.DEPARTMENTS,
        "SERVICE_TYPES": config.SERVICE_TYPES,
    }


def _parse_dt(value):
    if not value:
        return None
    if isinstance(value, (datetime, date)):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@app.template_filter("timeago")
def timeago(value):
    dt = _parse_dt(value)
    if not isinstance(dt, datetime):
        return "—"
    seconds = int((datetime.now() - dt).total_seconds())
    if seconds < 60:
        return "just now"
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            n = seconds // size
            return f"{n}{unit} ago" if unit != "d" or n < 14 else f"{dt:%b} {dt.day}"
    return "just now"


@app.template_filter("datetime")
def fmt_datetime(value):
    dt = _parse_dt(value)
    return (f"{dt:%b} {dt.day}, {dt.year}, {dt.hour % 12 or 12}:{dt:%M} {dt:%p}"
            if isinstance(dt, datetime) else "—")


@app.template_filter("clock")
def fmt_clock(value):
    dt = _parse_dt(value)
    return f"{dt.hour % 12 or 12}:{dt:%M} {dt:%p}" if isinstance(dt, datetime) else ""


@app.template_filter("dayname")
def fmt_dayname(value):
    try:
        d = date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return ""
    delta = (date.today() - d).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    return f"{d:%A}, {d:%b} {d.day}"


@app.template_filter("due")
def fmt_due(value):
    """Due date relative to today, e.g. 'Today', 'Tomorrow', '2d overdue', 'Oct 4'."""
    try:
        d = date.fromisoformat(value)
    except (TypeError, ValueError):
        return "No date"
    delta = (d - date.today()).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Tomorrow"
    if delta < 0:
        return f"{-delta}d overdue"
    return f"{d:%b} {d.day}"


@app.template_filter("money")
def money(value):
    return "—" if value in (None, "") else f"${value:,.0f}"


@app.template_filter("fromjson")
def from_json(value):
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


@app.template_filter("slug")
def slugify(value):
    return re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")


# --- Pages -----------------------------------------------------------------------------

@app.template_filter("emphasize_numbers")
def emphasize_numbers(text):
    """Escape the text, then bold each number so calculated figures stand out."""
    return Markup(re.sub(r"\d(?:[\d,.]*\d)?%?", lambda m: f"<strong>{m.group(0)}</strong>", str(escape(text or ""))))


@app.route("/")
def dashboard():
    db = get_db()
    today = date.today()
    return render_template("dashboard.html", page_title="Dashboard", d=dashboard_view.build(db),
                           today_label=f"{today:%A}, {today:%B} {today.day}")


@app.post("/api/dashboard/summary")
def api_dashboard_summary():
    """Optional: Claude rewrites the verified insight sentences as a short briefing (checked before it is shown)."""
    try:
        result = dashboard_view.summarize(get_db())
    except ai_service.AIServiceError as err:
        return _ai_error_response(err)
    return jsonify({"ok": True, **result})


@app.route("/projects")
def projects():
    db = get_db()
    filters = {k: request.args.get(k, "").strip() for k in ("q", "status", "stage", "priority")}
    rows = db_layer.list_projects(db, **filters)
    return render_template("projects.html", page_title="Projects", projects=rows, filters=filters,
                           status_counts=db_layer.project_status_counts(db),
                           total=db.execute("SELECT COUNT(*) FROM projects").fetchone()[0])


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def validate_project_form(form):
    """Server-side validation. Returns (clean_data, errors)."""
    data = {k: (form.get(k) or "").strip() for k in
            ("first_name", "last_name", "email", "phone", "address", "service_type",
             "monthly_electric_bill", "roof_age", "notes", "priority")}
    data["documents_received"] = form.getlist("documents_received")
    errors = {}
    if not data["first_name"]:
        errors["first_name"] = "First name is required."
    if not data["last_name"]:
        errors["last_name"] = "Last name is required."
    if data["email"] and not EMAIL_RE.match(data["email"]):
        errors["email"] = "Enter a valid email address, e.g. name@example.com."
    if data["phone"] and len(re.sub(r"\D", "", data["phone"])) < 10:
        errors["phone"] = "Enter a 10-digit phone number."
    if data["service_type"] not in config.SERVICE_TYPES:
        errors["service_type"] = "Choose a service type."
    if data["priority"] not in workflow.PRIORITIES:
        data["priority"] = "Medium"
    if data["monthly_electric_bill"]:
        try:
            data["monthly_electric_bill"] = round(float(data["monthly_electric_bill"].replace("$", "").replace(",", "")), 2)
            if not 0 <= data["monthly_electric_bill"] <= 10000:
                raise ValueError
        except ValueError:
            errors["monthly_electric_bill"] = "Enter an amount between $0 and $10,000."
    else:
        data["monthly_electric_bill"] = None
    if data["roof_age"]:
        try:
            data["roof_age"] = int(data["roof_age"])
            if not 0 <= data["roof_age"] <= 120:
                raise ValueError
        except ValueError:
            errors["roof_age"] = "Enter a whole number of years between 0 and 120."
    else:
        data["roof_age"] = None
    if len(data["notes"]) > 2000:
        errors["notes"] = "Keep notes under 2,000 characters."
    return data, errors


@app.route("/projects/new", methods=["GET", "POST"])
def project_new():
    if request.method == "GET":
        return render_template("project_new.html", page_title="New project", form={}, errors={},
                               required_docs=config.REQUIRED_DOCUMENTS,
                               roof_threshold=config.ROOF_AGE_REVIEW_THRESHOLD)
    data, errors = validate_project_form(request.form)
    if errors:
        flash("Please fix the highlighted fields.", "error")
        return render_template("project_new.html", page_title="New project", form=request.form, errors=errors,
                               required_docs=config.REQUIRED_DOCUMENTS,
                               roof_threshold=config.ROOF_AGE_REVIEW_THRESHOLD), 422
    db = get_db()
    project_id = workflow.create_project(db, data)
    workflow.log_activity(db, project_id, "Project created", "HUMAN",
                          {"service_type": data["service_type"], "created_by": config.DEMO_USER["name"]})
    result = rules_engine.apply_rules(db, project_id)
    db.commit()
    n = len(result["actions"])
    flash(f"Project created. Business rules ran and took {n} automated action{'s' if n != 1 else ''}.", "success")
    # The project page starts the AI analysis on load, so a slow or failed AI call never blocks saving.
    return redirect(url_for("project_detail", project_id=project_id, analyze=1))


def _load_project_or_404(db, project_id):
    project = db_layer.get_project(db, project_id)
    if project is None:
        abort(404)
    return project


def _detail_context(db, project_id):
    project = _load_project_or_404(db, project_id)
    documents = db_layer.project_documents(db, project_id)
    all_tasks = db_layer.project_tasks(db, project_id, include_completed=True)
    findings = rules_engine.evaluate(project, documents, all_tasks)
    analysis = db_layer.latest_analysis(db, project_id)
    stale = bool(analysis and analysis.get("context_hash")
                 and analysis["context_hash"] != intelligence.facts_hash(project, documents))
    return dict(
        project=project, documents=documents,
        tasks=[t for t in all_tasks if t["status"] != "Completed"],
        completed_task_count=sum(1 for t in all_tasks if t["status"] == "Completed"),
        findings=findings, analysis=analysis, analysis_stale=stale,
        nba=intelligence.next_best_action(findings, analysis, stale),
        activity=db_layer.recent_activity(db, limit=60, project_id=project_id),
        stage_index=workflow.STAGES.index(project["current_stage"]) if project["current_stage"] in workflow.STAGES else 0,
        roof_threshold=config.ROOF_AGE_REVIEW_THRESHOLD)


@app.route("/projects/<int:project_id>")
def project_detail(project_id):
    db = get_db()
    ctx = _detail_context(db, project_id)
    project = ctx["project"]
    return render_template(
        "project_detail.html", page_title=project["customer_name"], **ctx,
        auto_analyze=request.args.get("analyze") == "1" and ctx["analysis"] is None,
        draft_purposes=[("missing_information", "Request missing information",
                         "Ask for documents or details that are still missing from the file."),
                        ("roof_concern", "Acknowledge roof concern",
                         "Confirm the team will review roof information before next steps."),
                        ("status_update", "General status update",
                         "Summarize where the project stands and what happens next.")])


@app.post("/projects/<int:project_id>/workflow")
def project_workflow(project_id):
    db = get_db()
    _load_project_or_404(db, project_id)
    try:
        changes = workflow.update_workflow(db, project_id, request.form.get("stage"), request.form.get("status"),
                                           request.form.get("priority"), (request.form.get("note") or "").strip()[:500])
        db.commit()
        flash("Workflow updated and recorded in the activity log." if changes else "Note added to activity log.",
              "success")
    except workflow.WorkflowError as exc:
        flash(str(exc), "error")
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/projects/<int:project_id>/rules")
def project_rules(project_id):
    db = get_db()
    _load_project_or_404(db, project_id)
    result = rules_engine.apply_rules(db, project_id)
    db.commit()
    if result["actions"]:
        flash("Business rules ran: " + "; ".join(result["actions"]) + ".", "success")
    else:
        flash("Business rules ran. No new actions were needed.", "info")
    return redirect(url_for("project_detail", project_id=project_id))


@app.post("/documents/<int:document_id>/received")
def document_received(document_id):
    db = get_db()
    try:
        doc = workflow.mark_document_received(db, document_id)
        result = rules_engine.apply_rules(db, doc["project_id"])
        db.commit()
        extra = f" Automation: {'; '.join(result['actions'])}." if result["actions"] else ""
        flash(f"{doc['document_type']} marked as received.{extra}", "success")
        return redirect(url_for("project_detail", project_id=doc["project_id"]) + "#documents")
    except workflow.WorkflowError as exc:
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("projects"))


@app.post("/analyses/<int:analysis_id>/review")
def analysis_review(analysis_id):
    """Approve/override from the project page. Recorded in the Human Approval Center audit trail."""
    db = get_db()
    decision = {"accept": "approve", "dismiss": "reject"}.get(request.form.get("decision"))
    analysis = db.execute("SELECT project_id FROM ai_analyses WHERE id = ?", (analysis_id,)).fetchone()
    if analysis is None:
        abort(404)
    approvals.sync(db)
    item = db.execute("SELECT id FROM approval_items WHERE analysis_id = ? AND status = 'pending'", (analysis_id,)).fetchone()
    try:
        if item is None or decision is None:
            raise approvals.DecisionError("This recommendation is no longer waiting for a decision.")
        approvals.decide(db, item["id"], decision, note=(request.form.get("note") or ""), via="Project page")
        db.commit()
        flash("Recommendation approved and recorded." if decision == "approve"
              else "Recommendation overridden. Your decision was logged.", "success")
    except (approvals.DecisionError, LookupError) as exc:
        db.rollback()
        flash(str(exc), "error")
    return redirect(url_for("project_detail", project_id=analysis["project_id"]))


@app.post("/analyses/<int:analysis_id>/tasks/<int:index>/add")
def suggested_task_add(analysis_id, index):
    db = get_db()
    try:
        project_id, title = intelligence.add_suggested_task(db, analysis_id, index)
        db.commit()
        flash(f"Task added: {title}", "success")
        return redirect(url_for("project_detail", project_id=project_id) + "#ai-analysis-card")
    except workflow.WorkflowError as exc:
        db.commit()  # keeps an updated "duplicate" decision if one was recorded
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("projects"))


@app.route("/tasks")
def tasks():
    db = get_db()
    view = request.args.get("view", "all")
    if view not in ("all", "open", "in_progress", "overdue", "completed"):
        view = "all"
    q = request.args.get("q", "").strip()
    department = request.args.get("department", "").strip()
    return render_template("tasks.html", page_title="Tasks", tasks=db_layer.list_tasks(db, view, q, department),
                           view=view, q=q, department=department, counts=db_layer.task_view_counts(db))


@app.route("/copilot")
def copilot_page():
    return render_template("copilot.html", page_title="Copilot", suggestions=copilot.SUGGESTED_QUESTIONS,
                           tool_count=len(copilot.TOOLS))


@app.post("/api/copilot/ask")
def api_copilot_ask():
    payload = request.get_json(silent=True) or {}
    history = payload.get("history") if isinstance(payload.get("history"), list) else []
    try:
        answer = copilot.ask(get_db(), payload.get("question"), history)
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_question", "title": "Check your question",
                                               "message": str(exc)}}), 400
    except ai_service.AIServiceError as err:
        return _ai_error_response(err)
    for ref in answer["references"]:
        ref["url"] = url_for("project_detail", project_id=ref["id"])
    return jsonify({"ok": True, "answer": answer})


def _finder_context(db):
    return dict(examples=automation_finder.EXAMPLES, classes=automation_finder.CLASSES,
                saved=automation_finder.list_saved(db), min_chars=automation_finder.MIN_DESCRIPTION,
                max_chars=automation_finder.MAX_DESCRIPTION)


@app.route("/automation")
def automation_page():
    db = get_db()
    return render_template("automation.html", page_title="Automation Finder", result=None, **_finder_context(db))


@app.route("/automation/<int:analysis_id>")
def automation_view(analysis_id):
    db = get_db()
    result = automation_finder.load(db, analysis_id)
    if result is None:
        abort(404)
    return render_template("automation.html", page_title=result["process_name"], result=result,
                           **_finder_context(db))


@app.post("/api/automation/analyze")
def api_automation_analyze():
    payload = request.get_json(silent=True) or {}
    db = get_db()
    try:
        analysis_id, data, result = automation_finder.analyze(db, payload.get("description"), payload.get("process_name"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_input", "title": "Check the description",
                                               "message": str(exc)}}), 400
    except ai_service.AIServiceError as err:
        db.rollback()
        return _ai_error_response(err)
    if analysis_id is None:  # too vague / not a process: nothing stored
        return jsonify({"ok": True, "status": data["status"], "message": data["clarifying_question"]})
    db.commit()
    loaded = automation_finder.load(db, analysis_id)
    return jsonify({"ok": True, "status": "valid_process", "analysis_id": analysis_id,
                    "url": url_for("automation_view", analysis_id=analysis_id),
                    "html": render_template("partials/automation_result.html", result=loaded,
                                            classes=automation_finder.CLASSES),
                    "meta": {"model": result.model, "response_id": result.response_id, "latency_ms": result.latency_ms}})


@app.post("/api/automation/<int:analysis_id>/save")
def api_automation_save(analysis_id):
    payload = request.get_json(silent=True) or {}
    db = get_db()
    try:
        name, already = automation_finder.save(db, analysis_id, payload.get("process_name"))
    except LookupError as exc:
        return jsonify({"ok": False, "error": {"code": "not_found", "title": "Analysis not found",
                                               "message": str(exc)}}), 404
    db.commit()
    return jsonify({"ok": True, "name": name, "already_saved": already,
                    "url": url_for("automation_view", analysis_id=analysis_id),
                    "saved_html": render_template("partials/automation_saved.html",
                                                  saved=automation_finder.list_saved(db), current_id=analysis_id)})


def _builder_page(design):
    return render_template("workflow_builder.html", page_title=(design or {}).get("name") or "New workflow",
                           design=design, examples=workflow_designer.EXAMPLES, node_types=workflow_designer.NODE_TYPES,
                           ai_levels=workflow_designer.AI_LEVELS, min_chars=workflow_designer.MIN_DESCRIPTION,
                           max_chars=workflow_designer.MAX_DESCRIPTION)


@app.route("/workflows")
def workflows_page():
    designs = workflow_designer.list_designs(get_db())
    status = request.args.get("status", "")
    if status not in workflow_designer.STATUSES:
        status = ""
    counts = {s_: sum(1 for d in designs if d["status"] == s_) for s_ in workflow_designer.STATUSES}
    return render_template("workflows.html", page_title="Workflows", status=status, counts=counts, total=len(designs),
                           designs=[d for d in designs if not status or d["status"] == status],
                           node_types=workflow_designer.NODE_TYPES, examples=workflow_designer.EXAMPLES)


@app.route("/workflows/new")
def workflow_new():
    return _builder_page(None)


@app.route("/workflows/<int:design_id>")
def workflow_view(design_id):
    design = workflow_designer.load(get_db(), design_id)
    if design is None:
        abort(404)
    return _builder_page(design)


@app.post("/api/workflows/generate")
def api_workflow_generate():
    payload = request.get_json(silent=True) or {}
    db = get_db()
    design_id = payload.get("design_id")
    if design_id is not None and not isinstance(design_id, int):
        design_id = None
    try:
        new_id, data, result = workflow_designer.generate(db, payload.get("description"), payload.get("name"), design_id)
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_input", "title": "Check the description", "message": str(exc)}}), 400
    except (LookupError, PermissionError) as exc:
        return jsonify({"ok": False, "error": {"code": "not_allowed", "title": "Can't regenerate", "message": str(exc)}}), 409
    except ai_service.AIServiceError as err:
        db.rollback()
        return _ai_error_response(err)
    if new_id is None:
        return jsonify({"ok": True, "status": data["status"], "message": data["message"]})
    db.commit()
    return jsonify({"ok": True, "status": "valid_workflow", "design": workflow_designer.load(db, new_id),
                    "url": url_for("workflow_view", design_id=new_id),
                    "meta": {"model": result.model, "response_id": result.response_id, "latency_ms": result.latency_ms}})


@app.post("/api/workflows/<int:design_id>/status")
def api_workflow_status(design_id):
    payload = request.get_json(silent=True) or {}
    db = get_db()
    action = payload.get("action")
    try:
        if action in ("approve", "return_to_draft"):
            # Approving or rejecting a proposal is an approval decision: it goes through the audit trail.
            approvals.sync(db)
            item = db.execute("SELECT id FROM approval_items WHERE kind = 'workflow' AND workflow_design_id = ? "
                              "AND status = 'pending'", (design_id,)).fetchone()
            if item is None:
                raise PermissionError("This workflow is not waiting for approval.")
            approvals.decide(db, item["id"], "approve" if action == "approve" else "reject",
                             edits={"name": payload["name"]} if action == "approve" and isinstance(payload.get("name"), str)
                             and payload["name"].strip() else None,
                             note=payload.get("note") or "", via="Workflow Builder")
        else:
            workflow_designer.transition(db, design_id, action, payload.get("name"))
            approvals.sync(db)
    except LookupError as exc:
        db.rollback()
        return jsonify({"ok": False, "error": {"code": "not_found", "title": "Workflow not found", "message": str(exc)}}), 404
    except (PermissionError, approvals.DecisionError) as exc:
        db.rollback()
        return jsonify({"ok": False, "error": {"code": "not_allowed", "title": "Action not allowed", "message": str(exc)}}), 409
    db.commit()
    return jsonify({"ok": True, "design": workflow_designer.load(db, design_id)})


def _approvals_payload(db, view="pending"):
    items = approvals.list_items(db, view)
    for it in items:
        if it["project_id"]:
            it["project_url"] = url_for("project_detail", project_id=it["project_id"])
        if it["workflow_design_id"]:
            it["workflow_url"] = url_for("workflow_view", design_id=it["workflow_design_id"])
    return {"items": items, "counts": approvals.counts(db), "view": view}


@app.route("/approvals")
def approvals_page():
    db = get_db()
    approvals.sync(db)
    db.commit()
    return render_template("approvals.html", page_title="Approvals", data=_approvals_payload(db),
                           filters=approvals.FILTERS, kinds=approvals.KINDS, purposes=approvals.PURPOSES,
                           project_options=approvals.project_options(db), departments=config.DEPARTMENTS)


@app.get("/api/explain/<kind>/<int:obj_id>")
def api_explain(kind, obj_id):
    """Read-only "Why?" explanation, assembled from stored analysis data. Never calls Claude."""
    db = get_db()
    if kind == "analysis":
        data = explain.analysis(db, obj_id)
    elif kind == "approval":
        data = explain.approval_item(db, obj_id)
    elif kind == "automation":
        step = request.args.get("step", type=int)
        data = explain.automation_step(db, obj_id, step) if step else None
    elif kind == "workflow":
        node = (request.args.get("node") or "").strip()[:60] or None
        data = explain.workflow(db, obj_id, node)
    else:
        data = None
    if data is None:
        return jsonify({"ok": False, "error": {"code": "not_found", "title": "Nothing to explain",
                                               "message": "That record no longer exists or has no explanation."}}), 404
    return jsonify({"ok": True, "explanation": data})


@app.get("/api/approvals")
def api_approvals():
    db = get_db()
    approvals.sync(db)
    db.commit()
    view = "history" if request.args.get("view") == "history" else "pending"
    return jsonify({"ok": True, **_approvals_payload(db, view)})


@app.post("/api/approvals/draft")
def api_approvals_draft():
    payload = request.get_json(silent=True) or {}
    db = get_db()
    project_id = payload.get("project_id")
    if not isinstance(project_id, int) or db_layer.get_project(db, project_id) is None:
        return jsonify({"ok": False, "error": {"code": "invalid_input", "title": "Choose a project",
                                               "message": "Choose the project the message is for."}}), 400
    try:
        item_id, result = approvals.draft_communication(db, project_id, payload.get("purpose"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_input", "title": "Check the request", "message": str(exc)}}), 400
    except ai_service.AIServiceError as err:
        db.rollback()
        return _ai_error_response(err)
    db.commit()
    item = approvals.get(db, item_id)
    item["project_url"] = url_for("project_detail", project_id=project_id)
    return jsonify({"ok": True, "item": item, "counts": approvals.counts(db),
                    "meta": {"model": result.model, "response_id": result.response_id, "latency_ms": result.latency_ms}})


@app.post("/api/approvals/<int:item_id>/decide")
def api_approvals_decide(item_id):
    payload = request.get_json(silent=True) or {}
    db = get_db()
    try:
        kind, outcome = approvals.decide(db, item_id, payload.get("decision"), payload.get("edits"),
                                         payload.get("note") or "", via=payload.get("via") if payload.get("via") in
                                         ("Approval Center", "Project page") else "Approval Center")
    except LookupError as exc:
        db.rollback()
        return jsonify({"ok": False, "error": {"code": "not_found", "title": "Item not found", "message": str(exc)}}), 404
    except (approvals.DecisionError, PermissionError) as exc:
        db.rollback()
        return jsonify({"ok": False, "error": {"code": "not_allowed", "title": "Decision not recorded", "message": str(exc)}}), 409
    db.commit()
    return jsonify({"ok": True, "decision": kind, "outcome": outcome, "item": approvals.get(db, item_id),
                    "counts": approvals.counts(db)})


# --- AI Evaluation Lab -------------------------------------------------------------------------

def _eval_error(err):
    return jsonify({"ok": False, "error": {"code": err.code, "title": "Evaluation not started" if err.status != 404 else "Not found",
                                           "message": err.message}}), err.status


def _eval_view(db, run_id=None, compare_id=None):
    evaluation.ensure_cases(db)
    db.commit()
    cases = evaluation.list_cases(db)
    history = evaluation.runs(db)
    run = evaluation.get_run(db, run_id) if run_id else (evaluation.get_run(db, history[0]["id"]) if history else None)
    compare = evaluation.get_run(db, compare_id) if compare_id and run and compare_id != run["id"] else None
    return cases, history, run, compare


def _eval_rows(cases, run, compare):
    by_key = {r["case_key"]: r for r in (run or {}).get("results", [])}
    plan = set(json.loads(run["case_plan"])) if run else set()
    changes = evaluation.compare_runs(run, compare) if run and compare else {}
    return [{"case": c, "n": i, "result": by_key.get(c["key"]), "in_plan": (not run) or c["id"] in plan,
             "change": changes.get(c["key"])} for i, c in enumerate(cases, 1)]


@app.route("/evaluation")
def evaluation_page():
    db = get_db()
    run_id = request.args.get("run", type=int)
    compare_id = request.args.get("compare", type=int)
    cases, history, run, compare = _eval_view(db, run_id, compare_id)
    if run_id and run is None:
        abort(404)
    return render_template("evaluation.html", page_title="Evaluation Lab", cases=cases, history=history, run=run,
                           compare=compare, rows=_eval_rows(cases, run, compare), shared=evaluation.shared_metrics(run, compare),
                           limits={"max": evaluation.MAX_CASES, "quick": evaluation.QUICK_CASES,
                                   "cooldown": evaluation.COOLDOWN_SECONDS, "per_day": evaluation.MAX_RUNS_PER_DAY},
                           estimate_full=evaluation.estimate(cases[:evaluation.MAX_CASES]),
                           estimate_quick=evaluation.estimate(cases[:evaluation.QUICK_CASES]),
                           prompt_version=evaluation.prompt_version(), verdicts=evaluation.VERDICTS,
                           result_labels=evaluation.RESULTS)


@app.post("/api/eval/runs")
def api_eval_start():
    payload = request.get_json(silent=True) or {}
    db = get_db()
    try:
        run_id = evaluation.start_run(db, payload.get("limit"))
    except evaluation.EvalError as err:
        db.rollback()
        return _eval_error(err)
    db.commit()
    run = evaluation.get_run(db, run_id)
    keys = {c["id"]: (c["key"], c["title"]) for c in evaluation.list_cases(db)}
    plan = [{"key": keys[i][0], "title": keys[i][1]} for i in json.loads(run["case_plan"]) if i in keys]
    return jsonify({"ok": True, "run_id": run_id, "planned": run["planned_cases"], "plan": plan,
                    "url": url_for("evaluation_page", run=run_id)})


def _eval_fragments(db, run_id, result_id=None):
    cases, history, run, compare = _eval_view(db, run_id, None)
    rows = _eval_rows(cases, run, None)
    out = {"metrics_html": render_template("partials/eval_metrics.html", run=run, compare=None, shared=None)}
    if result_id:
        row = next((r for r in rows if r["result"] and r["result"]["id"] == result_id), None)
        if row:
            out.update(case_key=row["case"]["key"], row_html=render_template(
                "partials/eval_row.html", row=row, run=run, verdicts=evaluation.VERDICTS, result_labels=evaluation.RESULTS))
    return out


@app.post("/api/eval/runs/<int:run_id>/step")
def api_eval_step(run_id):
    """Runs the next case of a run: at most one Claude request per call. Calling it again after
    the run finishes does nothing."""
    db = get_db()
    try:
        progress = evaluation.step(db, run_id)
    except evaluation.EvalError as err:
        db.rollback()
        return _eval_error(err)
    db.commit()
    return jsonify({"ok": True, **progress, **_eval_fragments(db, run_id, progress.get("result_id"))})


@app.post("/api/eval/runs/<int:run_id>/stop")
def api_eval_stop(run_id):
    db = get_db()
    evaluation.stop_run(db, run_id)
    db.commit()
    return jsonify({"ok": True})


@app.get("/api/eval/results/<int:result_id>")
def api_eval_result(result_id):
    db = get_db()
    r = evaluation.get_result(db, result_id)
    if r is None:
        return jsonify({"ok": False, "error": {"code": "not_found", "title": "Not found", "message": "That result no longer exists."}}), 404
    case = evaluation.CASE_BY_KEY.get(r["case_key"])
    run = db.execute("SELECT id, model, started_at, prompt_version FROM eval_runs WHERE id = ?", (r["run_id"],)).fetchone()
    return jsonify({"ok": True, "result": {
        **{k: r[k] for k in ("id", "case_key", "case_title", "expected", "actual", "comparison", "auto_result", "response_id",
                              "model", "latency_ms", "input_tokens", "output_tokens", "error_code", "error_message",
                              "created_at", "reviews", "verdict", "rule_findings")},
        "input": json.loads(r["input_context"]), "summary": case["summary"] if case else None,
        "run": dict(run) if run else None}})


@app.post("/api/eval/results/<int:result_id>/review")
def api_eval_review(result_id):
    payload = request.get_json(silent=True) or {}
    db = get_db()
    try:
        evaluation.review(db, result_id, payload.get("verdict"), payload.get("note"))
    except evaluation.EvalError as err:
        db.rollback()
        return _eval_error(err)
    db.commit()
    run_id = db.execute("SELECT run_id FROM eval_results WHERE id = ?", (result_id,)).fetchone()[0]
    return jsonify({"ok": True, "result": evaluation.get_result(db, result_id)["reviews"][-1],
                    **_eval_fragments(db, run_id, result_id)})


@app.route("/modules/<slug>")
def planned_module(slug):
    if slug not in PLANNED_MODULES:
        abort(404)
    title, icon, description = PLANNED_MODULES[slug]
    return render_template("module_planned.html", page_title=title, module_icon=icon,
                           module_description=description, active_slug=slug)


# --- JSON endpoints (used by static/js/app.js) ------------------------------------------

def _ai_context(db, project_id):
    project = db_layer.get_project(db, project_id)
    if project is None:
        abort(404)
    documents = db_layer.project_documents(db, project_id)
    tasks_ = db_layer.project_tasks(db, project_id, include_completed=True)
    findings = rules_engine.evaluate(project, documents, tasks_)
    return project, ai_service.build_project_context(project, documents, tasks_, findings)


def _ai_error_response(err):
    return jsonify({"ok": False, "error": err.to_dict()}), err.http_status


PROJECT_PARTIALS = {"project-header": "partials/project_header.html",
                    "next-best-action": "partials/next_best_action.html",
                    "ai-analysis-card": "partials/ai_analysis.html",
                    "tasks": "partials/project_tasks.html",
                    "activity": "partials/activity_timeline.html"}


@app.post("/api/projects/<int:project_id>/analyze")
def api_analyze(project_id):
    db = get_db()
    _load_project_or_404(db, project_id)
    try:
        _, result = intelligence.run_project_analysis(db, project_id)
    except ai_service.AIServiceError as err:
        db.rollback()
        return _ai_error_response(err)
    approvals.sync(db)
    db.commit()
    ctx = _detail_context(db, project_id)
    return jsonify({"ok": True,
                    "sections": {el_id: render_template(tpl, fresh=True, **ctx) for el_id, tpl in PROJECT_PARTIALS.items()},
                    "meta": {"model": result.model, "response_id": result.response_id,
                             "latency_ms": result.latency_ms, "usage": result.usage}})


@app.post("/api/projects/<int:project_id>/draft")
def api_draft(project_id):
    """Draft from the project page. The draft is queued in the Human Approval Center; nothing is sent."""
    db = get_db()
    _load_project_or_404(db, project_id)
    purpose = (request.get_json(silent=True) or {}).get("purpose", "")
    try:
        item_id, result = approvals.draft_communication(db, project_id, purpose)
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "bad_request", "title": "Unknown message type", "message": str(exc)}}), 400
    except ai_service.AIServiceError as err:
        db.rollback()
        return _ai_error_response(err)
    db.commit()
    item = approvals.get(db, item_id)
    return jsonify({"ok": True, "item_id": item_id, "draft": item["ai_output"], "checks": item["checks"],
                    "confidence": item["confidence"], "decide_url": url_for("api_approvals_decide", item_id=item_id),
                    "meta": {"model": result.model, "response_id": result.response_id, "latency_ms": result.latency_ms}})


@app.post("/api/tasks/<int:task_id>/status")
def api_task_status(task_id):
    db = get_db()
    new_status = (request.get_json(silent=True) or {}).get("status", "")
    try:
        task = workflow.set_task_status(db, task_id, new_status)
        result = rules_engine.apply_rules(db, task["project_id"])  # e.g. a completed review clears the flag
        db.commit()
    except workflow.WorkflowError as exc:
        return jsonify({"ok": False, "error": {"title": "Could not update task", "message": str(exc)}}), 400
    msg = f"Task marked {new_status}."
    if result["actions"]:
        msg += " Automation: " + "; ".join(result["actions"]) + "."
    return jsonify({"ok": True, "status": new_status, "message": msg, "project_changed": bool(result["actions"])})


@app.get("/api/health")
def api_health():
    """Reports whether AI is configured. Never includes the key."""
    return jsonify({"status": "ok", "ai_configured": ai_service.is_configured(), "model": config.AI_MODEL})


# --- Errors ------------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": {"title": "Not found", "message": "That record does not exist."}}), 404
    return render_template("error.html", page_title="Not found", code=404,
                           message="The page or record you were looking for doesn't exist."), 404


@app.errorhandler(500)
def server_error(_e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": {"title": "Server error",
                                               "message": "Something went wrong. Please try again."}}), 500
    return render_template("error.html", page_title="Something went wrong", code=500,
                           message="An unexpected error occurred. Your data was not changed."), 500


with app.app_context():
    init_db()  # create tables and seed synthetic data on first run


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    print("\n  OpsPilot AI running at http://127.0.0.1:5000")
    print(f"  AI: {'configured (' + config.AI_MODEL + ')' if ai_service.is_configured() else 'NOT configured — add ANTHROPIC_API_KEY to .env'}\n")
    app.run(host="127.0.0.1", port=5000, debug=False)
