"""
Intelligence Layer — where AI output meets deterministic rules and human decisions.

Responsibilities are split on purpose:

  AI (services/ai_service.py)      interprets notes, summarizes, suggests actions and tasks
  Rules (services/rules_engine.py) required fields, documents, roof checks, task creation, status flags
  This module (plain Python)       validates AI suggestions against the rules and the database,
                                   decides what is shown as the Next Best Action and why
  People (routes + workflow.py)    approve, override, add suggested tasks, change stages

Nothing here lets AI change a project's stage or status, or create a task
that the rules and the database don't support.
"""
import hashlib
import json
import re

import config
from services import ai_service, rules_engine, workflow

RULE_BACKED = {  # AI task category -> the rule finding category that must exist for it to be valid
    "request_document": "document",
    "collect_contact_info": "contact",
    "collect_roof_info": "roof_info",
    "review_roof_info": "roof_review",
}
NO_GAP_REASON = {
    "request_document": "required documents are already on file",
    "collect_contact_info": "contact details are complete",
    "collect_roof_info": "roof information is already on file",
    "review_roof_info": "no open roof review is required",
}
RULE_CATEGORY_TO_MISSING = {"contact": "contact", "document": "document", "roof_info": "roof"}
STOPWORDS = {"the", "a", "an", "and", "or", "to", "for", "of", "with", "from", "on", "in", "customer", "customers"}


# --- Helpers ---------------------------------------------------------------------------

def facts_hash(project, documents):
    """Fingerprint of the facts an analysis depends on. Changes when the record changes."""
    facts = {
        "service": project["service_type"], "bill": project["monthly_electric_bill"],
        "roof": project["roof_age"], "notes": project["notes"] or "", "stage": project["current_stage"],
        "contact": [bool((project[f] or "").strip()) for f in config.REQUIRED_CONTACT_FIELDS],
        "docs": sorted((d["document_type"], d["status"] in ("Received", "Verified")) for d in documents),
    }
    return hashlib.sha256(json.dumps(facts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _tokens(text):
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if w not in STOPWORDS}


def _overlap(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


def similar_task(title, tasks):
    """Return an open task whose title is close enough to count as a duplicate."""
    new = _tokens(title)
    if not new:
        return None
    for t in tasks:
        if t["status"] == "Completed":
            continue
        old = _tokens(t["title"])
        shared = new & old
        # Duplicate when most of the shorter title's key words appear in the other one.
        if old and len(shared) >= min(2, len(new), len(old)) and len(shared) / min(len(new), len(old)) >= 0.6:
            return t
    return None


# --- Deterministic checks on AI output ------------------------------------------------

def _matching_finding(task, candidates):
    """Which open rule finding (if any) an AI-suggested task corresponds to."""
    if not candidates:
        return None
    if candidates[0].category != "document":
        return candidates[0]
    title = _tokens(task["title"])
    for f in candidates:  # documents: match on the document name, e.g. "utility bill"
        if _tokens(f.missing_item) <= title or len(_tokens(f.missing_item) & title) >= 1 and len(candidates) == 1:
            return f
    return None


def evaluate_task_suggestions(ai_tasks, findings, tasks):
    """AI may suggest tasks; this decides, deterministically, what happens to each one."""
    open_findings = [f for f in findings if not f.resolved]
    decided = []
    for t in ai_tasks:
        entry = dict(t)
        rule_category = RULE_BACKED.get(t["category"])
        if rule_category:
            finding = _matching_finding(t, [f for f in open_findings if f.category == rule_category])
            if finding:
                entry.update(decision="covered", note=f"Handled by business rule task: {finding.task_title}")
            elif rule_category == "document" and any(f.category == "document" for f in open_findings):
                entry.update(decision="rejected",
                             note="Not created: not a required document for this service, or already on file")
            else:
                entry.update(decision="rejected", note=f"Not created: {NO_GAP_REASON[t['category']]}")
        else:
            dup = similar_task(t["title"], tasks)
            if dup:
                entry.update(decision="duplicate", note=f"Not created: similar open task exists ({dup['title']})")
            else:
                entry.update(decision="needs_approval", note="Not covered by a business rule. A person decides.")
        decided.append(entry)
    return decided


def merge_missing_information(findings, ai_items):
    """Rules are the source of truth for contact, document and roof gaps; AI adds clarifications."""
    merged = [{"item": f.missing_item, "category": RULE_CATEGORY_TO_MISSING.get(f.category, "project_detail"),
               "why_it_matters": f.message, "source": "rule"}
              for f in findings if f.missing_item and not f.resolved]
    dropped = 0
    for m in ai_items:
        if m["category"] in ("contact", "document", "roof"):
            dropped += 1  # already covered (or contradicted) by the rules above
            continue
        merged.append({**m, "source": "ai"})
    return merged, dropped


def final_information_status(merged, ai_status):
    rule_gaps = any(m["source"] == "rule" for m in merged)
    ai_gaps = [m for m in merged if m["source"] == "ai"]
    if rule_gaps or (ai_status == "Incomplete" and any(m["category"] == "project_detail" for m in ai_gaps)):
        return "Incomplete"
    if ai_gaps or ai_status != "Complete":
        return "Needs clarification"
    return "Complete"


def human_review_policy(data, findings):
    reasons = []
    for f in findings:
        if f.severity == "review" and not f.resolved:
            reasons.append({"text": "Roof information requires review by qualified staff", "source": "rule"})
    if data["requires_human_review"]:
        reasons.append({"text": data["human_review_reason"] or "AI flagged this project for review", "source": "ai"})
    threshold = config.HUMAN_REVIEW_CONFIDENCE_THRESHOLD
    if data["confidence"] < threshold:
        reasons.append({"text": f"AI confidence {data['confidence']:.0%} is below the {threshold:.0%} threshold",
                        "source": "rule"})
    if data["detected_intent"] == "Unclear":
        reasons.append({"text": "Customer intent is unclear", "source": "rule"})
    return reasons


# --- Next best action (what the employee sees first) -------------------------------------

def _rule_headline(open_findings):
    titles = [f.task_title for f in open_findings]
    if not titles:
        return None
    first = titles[0]
    if len(titles) == 1:
        return first
    second = titles[1][0].lower() + titles[1][1:]
    extra = f" (+{len(titles) - 2} more)" if len(titles) > 2 else ""
    return f"{first} and {second}{extra}"


def next_best_action(findings, analysis, is_stale=False):
    open_findings = sorted([f for f in findings if not f.resolved],
                           key=lambda f: rules_engine.SEVERITY_ORDER.get(f.severity, 9))
    ai = analysis if analysis and analysis["review_status"] != "Dismissed" else None
    rule_why = [{"text": f.message, "source": "rule"} for f in open_findings][:3]

    if ai:
        rule_tokens = [_tokens(w["text"]) for w in rule_why]
        ai_why = [{"text": w, "source": "ai"} for w in (ai.get("recommendation_why") or [])
                  if not any(_overlap(_tokens(w), rt) >= 0.6 for rt in rule_tokens)][:3]  # skip facts rules already state
        if is_stale:
            ai_why = []  # the AI's facts may no longer be true; show only what the rules confirm today
        why = rule_why + ai_why or [{"text": "No business rules are currently flagged", "source": "rule"}]
        return {"headline": ai["recommended_action"], "owner": ai["suggested_department"],
                "source": "Combined" if open_findings else "AI recommendation",
                "why": why, "analysis": ai, "stale": is_stale}
    if open_findings:
        owner = open_findings[0].department
        return {"headline": _rule_headline(open_findings), "owner": owner, "source": "Business rule",
                "why": rule_why, "analysis": None, "stale": False, "dismissed": analysis is not None}
    return {"headline": "Move the project to the next workflow stage when ready", "owner": None,
            "source": "Business rule",
            "why": [{"text": "All required contact details and documents are on file", "source": "rule"},
                    {"text": "No business rules are flagged", "source": "rule"}],
            "analysis": None, "stale": False, "dismissed": analysis is not None}


# --- Orchestration ------------------------------------------------------------------------

def run_project_analysis(db, project_id):
    """Analyze a project with Claude, then let rules and Python checks decide what happens.

    Raises ai_service.AIServiceError before writing anything if the AI call fails,
    so a failed call never leaves a half-saved or invented analysis.
    """
    project, documents, tasks = rules_engine.load_state(db, project_id)
    findings = rules_engine.evaluate(project, documents, tasks)
    context = ai_service.build_project_context(project, documents, tasks, findings)
    from services import explain                          # snapshot of the facts the AI is about to see
    snapshot = explain.capture_evidence(project, documents, tasks, findings, context)
    result = ai_service.analyze_project(context)          # AI: may raise, nothing written yet
    data = result.data
    provenance = {"model": result.model, "response_id": result.response_id}

    workflow.log_activity(db, project_id, "Customer notes analyzed and project summarized", "AI",
                          {**provenance, "intent": data["detected_intent"], "confidence": data["confidence"]})

    rule_run = rules_engine.apply_rules(db, project_id)   # RULES: same outcome whatever the AI said
    snapshot["rule_actions"] = rule_run["actions"]

    project, documents, tasks = rules_engine.load_state(db, project_id)
    findings = rules_engine.evaluate(project, documents, tasks)
    missing, dropped = merge_missing_information(findings, data["missing_information"])
    status = final_information_status(missing, data["information_status"])
    decided = evaluate_task_suggestions(data["suggested_tasks"], findings, tasks)
    reasons = human_review_policy(data, findings)

    analysis_id = workflow.insert_analysis(db, project_id, result, {
        "information_status": status,
        "info_status_adjusted": status != data["information_status"],
        "missing_information": missing,
        "suggested_tasks": decided,
        "requires_human_review": bool(reasons),
        "human_review_reasons": reasons,
        "context_hash": facts_hash(project, documents),
        "evidence_snapshot": snapshot,
    })

    if status != data["information_status"]:
        workflow.log_activity(db, project_id,
                              f"Information status set by business rules: {status} (AI said {data['information_status']})",
                              "AUTOMATION", {"analysis_id": analysis_id})
    if decided:
        counts = {}
        for d in decided:
            counts[d["decision"]] = counts.get(d["decision"], 0) + 1
        labels = {"covered": "covered by rules", "needs_approval": "need approval",
                  "rejected": "rejected", "duplicate": "duplicates"}
        summary = ", ".join(f"{n} {labels[k]}" for k, n in counts.items())
        workflow.log_activity(db, project_id, f"AI task suggestions validated: {summary}", "AUTOMATION",
                              {"analysis_id": analysis_id, "decisions": [
                                  {"title": d["title"], "decision": d["decision"]} for d in decided]})
    workflow.log_activity(db, project_id, f"Next action recommended: {data['recommended_action']}", "AI",
                          {**provenance, "analysis_id": analysis_id, "department": data["suggested_department"]})
    return analysis_id, result


def add_suggested_task(db, analysis_id, index):
    """A person approves one AI-suggested task. Python re-checks it before creating anything."""
    analysis = db.execute("SELECT * FROM ai_analyses WHERE id = ?", (analysis_id,)).fetchone()
    if analysis is None:
        raise workflow.WorkflowError("Analysis not found.")
    suggestions = json.loads(analysis["suggested_tasks"] or "[]")
    if not 0 <= index < len(suggestions):
        raise workflow.WorkflowError("Suggested task not found.")
    s = suggestions[index]
    if s.get("decision") != "needs_approval":
        raise workflow.WorkflowError("This suggestion can't be added.")
    tasks = db.execute("SELECT * FROM tasks WHERE project_id = ?", (analysis["project_id"],)).fetchall()
    dup = similar_task(s["title"], tasks)
    if dup:
        s.update(decision="duplicate", note=f"Not created: similar open task exists ({dup['title']})")
        workflow.update_suggested_tasks(db, analysis_id, suggestions)
        raise workflow.WorkflowError(f"A similar open task already exists: {dup['title']}")
    department = s["department"] if s["department"] in config.DEPARTMENTS else "Operations"
    priority = s["priority"] if s["priority"] in workflow.PRIORITIES else "Medium"
    task_id = workflow.create_task(db, analysis["project_id"], s["title"], department, priority=priority,
                                   description=f"AI suggestion approved by a team member. {s.get('reason', '')}".strip(),
                                   due_in_days=3, source="AI")
    s.update(decision="added", note="Added by a team member")
    workflow.update_suggested_tasks(db, analysis_id, suggestions)
    workflow.log_activity(db, analysis["project_id"], f"AI-suggested task approved: {s['title']}", "HUMAN",
                          {"analysis_id": analysis_id, "task_id": task_id})
    workflow.touch_project(db, analysis["project_id"])
    return analysis["project_id"], s["title"]
