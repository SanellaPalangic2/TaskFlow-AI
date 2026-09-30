"""
Operations overview (dashboard).

Read-only. Everything on the dashboard is calculated here from SQLite with plain Python and SQL.
Nothing in this module changes project state.

The one optional AI feature, summarize(), sends the already-calculated insight sentences to
Claude and asks for a short plain-language summary. OpsPilot then checks that every number in
the summary appears in those verified sentences, and rejects the summary if it does not.
"""
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta

from pydantic import BaseModel

import database as db_layer
from services import ai_service, approvals, intelligence, rules_engine, workflow

ATTENTION_STATUSES = ("Needs Attention", "Blocked", "Human Review Required")
QUEUE_LIMIT = 8
FEED_LIMIT = 40          # items sent to the page; the feed shows 8 at a time per filter

STATUS_KEYS = [  # pipeline segment order
    ("Blocked", "blocked"), ("Human Review Required", "review"), ("Needs Attention", "attention"),
    ("Active", "active"), ("Completed", "completed")]

REASON_LABELS = {  # rules-engine finding category -> (sentence subject with verb, plural noun)
    "document": ("Missing documents are", "missing documents"),
    "contact": ("Missing contact details are", "missing contact details"),
    "roof_info": ("Missing roof information is", "missing roof information"),
    "roof_review": ("Pending roof reviews are", "pending roof reviews"),
    "blocked": ("Team blocks are", "team blocks"),
}
ACTOR_LABELS = {"AI": "AI", "AUTOMATION": "Business rule", "HUMAN": "Human"}


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts)[:19])
    except (TypeError, ValueError):
        return None


def _json(value):
    try:
        return json.loads(value) if value else {}
    except (TypeError, ValueError):
        return {}


def _plural(n, one, many=None):
    return one if n == 1 else (many or one + "s")


def _age(ts, now):
    """Short age for sentences, e.g. '11 hours', '2 days'."""
    dt = _parse(ts)
    if not dt:
        return None
    s = max(0, (now - dt).total_seconds())
    if s < 3600:
        m = max(1, int(s // 60))
        return f"{m} {_plural(m, 'minute')}"
    if s < 86400:
        h = int(s // 3600)
        return f"{h} {_plural(h, 'hour')}"
    d = int(s // 86400)
    return f"{d} {_plural(d, 'day')}"


# --- Attention queue ------------------------------------------------------------------------------

def _project_state(db, p):
    """Findings and Next Best Action, computed exactly as on the project page."""
    documents = db_layer.project_documents(db, p["id"])
    tasks = db_layer.project_tasks(db, p["id"], True)
    findings = rules_engine.evaluate(p, documents, tasks)
    analysis = db_layer.latest_analysis(db, p["id"])
    stale = bool(analysis and analysis.get("context_hash")
                 and analysis["context_hash"] != intelligence.facts_hash(p, documents))
    open_findings = sorted([f for f in findings if not f.resolved],
                           key=lambda f: rules_engine.SEVERITY_ORDER.get(f.severity, 9))
    return open_findings, intelligence.next_best_action(findings, analysis, stale), tasks


def attention_projects(db):
    return db.execute(db_layer.PROJECT_SELECT + f"""
        WHERE p.project_status IN ({','.join('?' * len(ATTENTION_STATUSES))})
        ORDER BY CASE p.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, p.updated_at DESC""",
                      ATTENTION_STATUSES).fetchall()


def attention_queue(db, rows):
    today = date.today().isoformat()
    out = []
    for p in rows:
        findings, nba, tasks = _project_state(db, p)
        overdue = sum(1 for t in tasks if t["status"] != "Completed" and t["due_date"] and t["due_date"] < today)
        if p["project_status"] == "Blocked":
            note = db_layer.latest_workflow_note(db, p["id"])
            issue = f"Blocked by team: {note}" if note else "Blocked by team"
            extra = len(findings)
            categories = ["blocked"]
        else:
            issue = findings[0].message if findings else "Flagged for review"
            extra = max(0, len(findings) - 1)
            categories = sorted({f.category for f in findings})
        stage_i = workflow.STAGES.index(p["current_stage"]) if p["current_stage"] in workflow.STAGES else 0
        out.append({
            "id": p["id"], "customer": p["customer_name"], "first_name": p["first_name"], "last_name": p["last_name"],
            "service": p["service_type"], "status": p["project_status"], "priority": p["priority"],
            "stage": p["current_stage"], "stage_step": stage_i + 1, "stage_total": len(workflow.STAGES),
            "issue": issue, "more_issues": extra, "categories": categories, "overdue_tasks": overdue,
            "action": nba["headline"], "action_owner": nba.get("owner"), "action_source": nba["source"],
            "action_stale": nba.get("stale", False),
            "action_synthetic": bool(nba.get("analysis") and nba["analysis"].get("source") != "live"),
            "action_pending": bool(nba.get("analysis") and nba["analysis"].get("review_status") == "Pending"),
            "action_analysis_id": nba["analysis"]["id"] if nba.get("analysis") else None,
            "updated_at": p["updated_at"],
        })
    return out


# --- Pipeline -------------------------------------------------------------------------------------

def pipeline(db):
    rows = db.execute("SELECT current_stage, project_status, COUNT(*) FROM projects GROUP BY 1, 2").fetchall()
    by_stage = defaultdict(Counter)
    for stage, status, n in rows:
        by_stage[stage][status] += n
    counts = [sum(by_stage[s].values()) for s in workflow.STAGES]
    peak = max(counts) or 1
    stages = []
    for i, s in enumerate(workflow.STAGES):
        total = counts[i]
        segs = [{"key": key, "status": status, "count": by_stage[s][status],
                 "pct": round(100 * by_stage[s][status] / peak, 2)} for status, key in STATUS_KEYS if by_stage[s][status]]
        flagged = sum(by_stage[s][st] for st in ATTENTION_STATUSES)
        stages.append({"stage": s, "step": i + 1, "count": total, "flagged": flagged, "segments": segs})
    totals = Counter()
    for c in by_stage.values():
        totals.update(c)
    return {"stages": stages, "total": sum(counts), "completed": totals["Completed"],
            "open": sum(counts) - totals["Completed"], "flagged": sum(totals[st] for st in ATTENTION_STATUSES),
            "legend": [{"key": key, "status": status, "count": totals[status]} for status, key in STATUS_KEYS]}


# --- Activity feed --------------------------------------------------------------------------------

def _provenance(details, actor):
    if actor != "AI":
        return None
    if details.get("response_id"):
        return {"kind": "live", "model": details.get("model"), "response_id": details["response_id"]}
    if details.get("synthetic"):
        return {"kind": "synthetic"}
    return None


def activity(db, limit=FEED_LIMIT):
    """One feed across the project activity log, Workflow Builder events and Automation Finder runs."""
    items = []
    for r in db.execute("""SELECT a.*, c.first_name || ' ' || c.last_name AS customer_name
                           FROM activity_log a LEFT JOIN projects p ON p.id = a.project_id
                           LEFT JOIN customers c ON c.id = p.customer_id
                           ORDER BY a.created_at DESC, a.id DESC LIMIT ?""", (limit,)):
        d = _json(r["details"])
        items.append({"actor": r["action_type"], "action": r["action"], "created_at": r["created_at"],
                      "subject": r["customer_name"], "url": f"/projects/{r['project_id']}" if r["project_id"] else None,
                      "area": "Project" if r["project_id"] else "Operations", "provenance": _provenance(d, r["action_type"])})
    # Workflow approvals and returns are already in the activity log (recorded by the Approval Center).
    for r in db.execute("""SELECT e.*, w.name FROM workflow_design_events e JOIN workflow_designs w ON w.id = e.design_id
                           WHERE e.action NOT IN ('Approved and activated', 'Returned to draft')
                           ORDER BY e.created_at DESC, e.id DESC LIMIT ?""", (limit,)):
        d = _json(r["details"])
        items.append({"actor": r["actor_type"], "action": r["action"], "created_at": r["created_at"],
                      "subject": r["name"], "url": f"/workflows/{r['design_id']}", "area": "Workflow Builder",
                      "provenance": _provenance(d, r["actor_type"])})
    for r in db.execute("""SELECT id, process_name, source, model, response_id, created_at FROM automation_analyses
                           ORDER BY created_at DESC LIMIT ?""", (limit,)):
        items.append({"actor": "AI", "action": "Process analyzed for automation opportunities", "created_at": r["created_at"],
                      "subject": r["process_name"], "url": f"/automation/{r['id']}", "area": "Automation Finder",
                      "provenance": _provenance({"response_id": r["response_id"], "model": r["model"],
                                                 "synthetic": r["source"] != "live"}, "AI")})
    items.sort(key=lambda i: i["created_at"] or "", reverse=True)
    return items[:limit]


def actor_counts(db, since):
    """Actions per actor since a timestamp, across the same three sources as the feed.
    AI counts only real Claude operations: seeded sample analyses are left out."""
    c = Counter(dict(db.execute("SELECT action_type, COUNT(*) FROM activity_log WHERE created_at >= ? AND action_type != 'AI' "
                                "GROUP BY 1", (since,)).fetchall()))
    c["AI"] += sum(1 for (details,) in db.execute("SELECT details FROM activity_log WHERE action_type = 'AI' AND created_at >= ?", (since,))
                   if not _json(details).get("synthetic"))
    c.update(dict(db.execute("""SELECT actor_type, COUNT(*) FROM workflow_design_events
                                WHERE created_at >= ? AND action NOT IN ('Approved and activated', 'Returned to draft')
                                GROUP BY 1""", (since,)).fetchall()))
    c["AI"] += db.execute("SELECT COUNT(*) FROM automation_analyses WHERE created_at >= ? AND source = 'live'", (since,)).fetchone()[0]
    return {k: c.get(k, 0) for k in ("AI", "AUTOMATION", "HUMAN")}


def automation_by_day(db, days=7):
    start = date.today() - timedelta(days=days - 1)
    rows = dict(db.execute("""SELECT substr(created_at, 1, 10), COUNT(*) FROM activity_log
                              WHERE action_type = 'AUTOMATION' AND created_at >= ? GROUP BY 1""", (start.isoformat(),)).fetchall())
    wf = dict(db.execute("""SELECT substr(created_at, 1, 10), COUNT(*) FROM workflow_design_events
                            WHERE actor_type = 'AUTOMATION' AND created_at >= ? GROUP BY 1""", (start.isoformat(),)).fetchall())
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        k = d.isoformat()
        out.append({"date": k, "label": f"{d:%a} {d:%b} {d.day}", "count": rows.get(k, 0) + wf.get(k, 0)})
    peak = max(x["count"] for x in out) or 1
    for x in out:
        x["pct"] = round(100 * x["count"] / peak)
    return out


# --- Insights -------------------------------------------------------------------------------------

def _join(words):
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def insights(db, queue, now):
    """Plain-language facts. Every number is counted here from SQLite; nothing comes from a model."""
    out = []
    today = date.today().isoformat()

    # 1. Most common reason projects need attention (a project counts once per reason).
    if queue:
        reasons = Counter(c for q in queue for c in q["categories"])
        if reasons:
            top = max(reasons.values())
            leaders = [k for k, v in reasons.items() if v == top]
            if len(leaders) == 1:
                text = (f"{REASON_LABELS[leaders[0]][0]} the most common reason projects need attention: "
                        f"{top} of {len(queue)} flagged {_plural(len(queue), 'project')}.")
            else:
                names = [REASON_LABELS[k][1] for k in leaders]
                names[0] = names[0][0].upper() + names[0][1:]
                text = (f"{_join(names)} are tied as the most common reasons projects need attention "
                        f"({top} of {len(queue)} flagged projects each).")
            out.append({"id": "top_reason", "tone": "warning", "icon": "flag", "text": text,
                        "link": "/projects?status=Needs+Attention", "link_label": "Flagged projects",
                        "basis": "Unresolved business-rule findings on projects that are flagged, blocked or awaiting review. "
                                 "Each project counts once per reason."})

    # 2. Overdue tasks.
    r = db.execute(f"""SELECT COUNT(*) AS tasks, COUNT(DISTINCT t.project_id) AS projects, MIN(t.due_date) AS oldest
                       FROM tasks t WHERE {db_layer.OVERDUE_SQL}""").fetchone()
    if r["tasks"]:
        late = (date.today() - date.fromisoformat(r["oldest"])).days
        out.append({"id": "overdue", "tone": "danger", "icon": "clock",
                    "text": f"{r['projects']} {_plural(r['projects'], 'project has', 'projects have')} overdue tasks "
                            f"({r['tasks']} {_plural(r['tasks'], 'task')} in total; the oldest is {late} "
                            f"{_plural(late, 'day')} late).",
                    "link": "/tasks?view=overdue", "link_label": "Overdue tasks",
                    "basis": "Open tasks with a due date before today."})
    else:
        out.append({"id": "overdue", "tone": "success", "icon": "check", "text": "No tasks are overdue.",
                    "link": "/tasks", "link_label": "Tasks", "basis": "Open tasks with a due date before today."})

    # 3. AI output waiting for a person.
    kinds = dict(db.execute("SELECT kind, COUNT(*) FROM approval_items WHERE status = 'pending' GROUP BY kind").fetchall())
    recs = kinds.get("recommendation", 0) + kinds.get("decision", 0)
    parts = []
    if recs:
        parts.append(f"{recs} AI {_plural(recs, 'recommendation')}")
    if kinds.get("communication"):
        n = kinds["communication"]
        parts.append(f"{n} customer message {_plural(n, 'draft')}")
    if kinds.get("workflow"):
        n = kinds["workflow"]
        parts.append(f"{n} workflow {_plural(n, 'proposal')}")
    if parts:
        total = sum(kinds.values())
        oldest = db.execute("SELECT MIN(created_at) FROM approval_items WHERE status = 'pending'").fetchone()[0]
        age = _age(oldest, now)
        out.append({"id": "approvals", "tone": "review", "icon": "stamp",
                    "text": f"{_join(parts)} {'is' if total == 1 else 'are'} waiting for human review"
                            + (f"; the oldest has waited {age}." if age else "."),
                    "link": "/approvals", "link_label": "Approval Center",
                    "basis": "Pending items in the Human Approval Center."})

    # 4. Most frequently missing document on open projects.
    docs = db.execute("""SELECT d.document_type, COUNT(DISTINCT d.project_id) FROM documents d
                         JOIN projects p ON p.id = d.project_id
                         WHERE d.status IN ('Missing', 'Requested') AND p.project_status != 'Completed'
                         GROUP BY 1 ORDER BY 2 DESC, 1""").fetchall()
    if docs:
        top = docs[0][1]
        leaders = [d[0] for d in docs if d[1] == top]
        total_docs = sum(d[1] for d in docs)
        text = (f"{_join(leaders)} {'is' if len(leaders) == 1 else 'are'} the most frequently missing "
                f"{_plural(len(leaders), 'document')}, outstanding on {top} open {_plural(top, 'project')}"
                + (f" ({total_docs} missing documents in total)." if total_docs != top else "."))
        out.append({"id": "missing_docs", "tone": "info", "icon": "file", "text": text,
                    "link": "/tasks", "link_label": "Tasks",
                    "basis": "Documents marked Missing or Requested on projects that are not completed."})

    # 5. Workload by department.
    dept = db.execute("SELECT department, COUNT(*) FROM tasks WHERE status != 'Completed' GROUP BY 1 ORDER BY 2 DESC, 1").fetchall()
    if dept:
        total = sum(d[1] for d in dept)
        out.append({"id": "workload", "tone": "neutral", "icon": "inbox",
                    "text": f"{dept[0][0]} holds the most open tasks: {dept[0][1]} of {total}.",
                    "link": "/tasks", "link_label": "Tasks", "basis": "Open and in-progress tasks grouped by department."})

    # 6. Who did the work in the last 7 days.
    week = actor_counts(db, (date.today() - timedelta(days=6)).isoformat())
    total = sum(week.values())
    if total:
        auto = week["AUTOMATION"]
        pct = round(100 * auto / total)
        out.append({"id": "automation_share", "tone": "automation", "icon": "cpu",
                    "text": f"Business rules performed {auto} of {total} logged actions in the last 7 days ({pct}%).",
                    "link": None, "link_label": None,
                    "basis": "Activity-log entries, Workflow Builder events and Automation Finder runs from the last 7 days. AI counts only live Claude calls, not seeded samples."})
    return out


# --- Assemble -------------------------------------------------------------------------------------

def build(db):
    now = datetime.now()
    today = date.today().isoformat()
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    rows = attention_projects(db)
    queue = attention_queue(db, rows)
    pending = approvals.list_items(db, "pending")
    counts = approvals.counts(db)
    week_start = (date.today() - timedelta(days=6)).isoformat()   # today and the 6 days before it
    today_counts = actor_counts(db, today)
    week_counts = actor_counts(db, week_start)
    oldest = min((i["created_at"] for i in pending), default=None)

    metrics = {
        "active_projects": one("SELECT COUNT(*) FROM projects WHERE project_status != 'Completed'"),
        "total_projects": one("SELECT COUNT(*) FROM projects"),
        "new_this_week": one("SELECT COUNT(*) FROM projects WHERE created_at >= ?", week_start),
        "needs_attention": len(rows),
        "high_priority_attention": sum(1 for q in queue if q["priority"] == "High"),
        "blocked": sum(1 for q in queue if q["status"] == "Blocked"),
        "review": sum(1 for q in queue if q["status"] == "Human Review Required"),
        "open_tasks": one("SELECT COUNT(*) FROM tasks WHERE status != 'Completed'"),
        "overdue_tasks": one(f"SELECT COUNT(*) FROM tasks t WHERE {db_layer.OVERDUE_SQL}"),
        "due_today": one("SELECT COUNT(*) FROM tasks WHERE status != 'Completed' AND due_date = ?", today),
        "approvals": counts["pending"],
        "approvals_oldest": oldest,
        "automated_today": today_counts["AUTOMATION"],
        "ai_today": today_counts["AI"],
        "automation_days": automation_by_day(db),
        "week": week_counts,
    }
    metrics["automated_week"] = sum(d["count"] for d in metrics["automation_days"])
    tasks = db.execute(f"""SELECT t.*, {db_layer.OVERDUE_SQL} AS is_overdue, c.first_name || ' ' || c.last_name AS customer_name
                           FROM tasks t JOIN projects p ON p.id = t.project_id JOIN customers c ON c.id = p.customer_id
                           WHERE t.status != 'Completed' AND t.due_date IS NOT NULL
                           ORDER BY t.due_date, CASE t.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END LIMIT 6""").fetchall()
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "metrics": metrics,
        "queue": queue[:QUEUE_LIMIT],
        "queue_total": len(queue),
        "pipeline": pipeline(db),
        "activity": activity(db),
        "insights": insights(db, queue, now),
        "approvals": pending[:4],
        "approval_counts": counts,
        "tasks": tasks,
    }


# --- Optional AI summary of verified insights -----------------------------------------------------

class DashboardSummary(BaseModel):
    summary: str
    insight_ids_used: list[str]


SUMMARY_INSTRUCTIONS = """
You write a short briefing for an operations manager at a solar and roofing company.

You receive a list of VERIFIED STATEMENTS. OpsPilot calculated each one from its database.

Rules:
- Write 2 or 3 short sentences, under 70 words in total, in plain business English.
- Use ONLY the verified statements. Do not add any number, percentage, date, name, cause, trend,
  comparison, forecast or recommendation that is not stated in them.
- Every number you write must appear exactly as written in a verified statement. You may leave numbers out.
- Do not say "increase", "decrease", "trend", "improving", "worsening" or compare to past periods.
- Lead with what needs attention first.
- No greeting, no headings, no bullet points, no markdown.
- insight_ids_used lists the ids of the statements you used.
""".strip()

NUMBER_WORDS = {w: i for i, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
    "seventeen eighteen nineteen twenty".split())}
NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")
WORD_RE = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")\b", re.I)
TREND_RE = re.compile(r"\b(increas\w*|decreas\w*|trend\w*|improv\w*|worsen\w*|compared|last (week|month)|up from|down from)\b", re.I)


def numbers_in(text):
    vals = {float(n.replace(",", "")) for n in NUM_RE.findall(text or "")}
    vals |= {float(NUMBER_WORDS[w.lower()]) for w in WORD_RE.findall(text or "")}
    return vals


def check_summary(summary, facts):
    """Return a list of problems. An empty list means the summary only uses verified figures."""
    allowed = set()
    for f in facts:
        allowed |= numbers_in(f["text"])
    problems = []
    extra = sorted(n for n in numbers_in(summary) if n not in allowed)
    if extra:
        problems.append("figures not in the verified statistics: " + ", ".join(f"{n:g}" for n in extra))
    trend = TREND_RE.search(summary)
    if trend:
        problems.append(f"a trend claim (“{trend.group(0)}”) the statistics do not support")
    if re.search(r"[#*`<>]", summary):
        problems.append("formatting characters")
    return problems


def summarize(db):
    """Ask Claude to turn the verified insight sentences into a 2-3 sentence summary, then verify it."""
    data = build(db)
    facts = [{"id": i["id"], "text": i["text"]} for i in data["insights"]]
    if not facts:
        raise ai_service.AIServiceError("bad_request", "Nothing to summarize",
                                        "There are no calculated insights yet.", 400)
    user = "VERIFIED STATEMENTS (JSON):\n" + json.dumps(facts, indent=2)
    parsed, result = ai_service._structured_request("dashboard_summary", SUMMARY_INSTRUCTIONS, user, DashboardSummary)
    summary = re.sub(r"\s+", " ", parsed.summary or "").strip()
    if not summary:
        raise ai_service.AIServiceError("invalid_output", "AI returned an empty summary",
                                        "The summary was empty, so it was not shown.", 502)
    if len(summary) > 700:
        raise ai_service.AIServiceError("invalid_output", "AI summary was too long",
                                        "The summary was longer than allowed, so it was not shown.", 502)
    problems = check_summary(summary, facts)
    if problems:
        raise ai_service.AIServiceError(
            "invalid_output", "AI summary failed verification",
            "The summary included " + "; ".join(problems) + ", so OpsPilot did not show it.", 502)
    known = {f["id"] for f in facts}
    used = [i for i in dict.fromkeys(parsed.insight_ids_used or []) if i in known]
    return {"summary": summary, "insight_ids_used": used, "facts_sent": facts,
            "model": result.model, "response_id": result.response_id, "latency_ms": result.latency_ms}
