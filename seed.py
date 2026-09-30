"""
Synthetic demo data for OpsPilot AI.

    python seed.py            # create tables and seed if the database is empty
    python seed.py --reset    # delete the database and reseed from scratch

All people, addresses, emails (@example.com) and phone numbers (555-01xx) are
fictional. Every seeded customer/project is flagged is_synthetic = 1, and the
few sample AI analyses are stored with source = 'synthetic' so the UI can
label them clearly and never present them as live Claude output.
"""
import json
import sys
from datetime import date, datetime, timedelta

import config
import database
from services import explain, intelligence, rules_engine, workflow

NOW = datetime.now().replace(microsecond=0)


def ts(days_ago=0, hours_ago=0):
    return (NOW - timedelta(days=days_ago, hours=hours_ago)).isoformat(timespec="seconds")


def due(days_from_today):
    return (date.today() + timedelta(days=days_from_today)).isoformat()


PROJECTS = [
    dict(key="smith", first_name="John", last_name="Smith", email="john.smith@example.com", phone="(555) 010-4821",
         address="1428 Maple Avenue, Riverton", service_type="Solar + Roofing", monthly_electric_bill=420,
         roof_age=19, docs=[], created=2.2, updated_h=3,
         notes="I want solar but I'm worried my roof may need to be replaced first.",
         stage="Information Gathering"),
    dict(key="gonzalez", first_name="Maria", last_name="Gonzalez", email="maria.gonzalez@example.com",
         phone="(555) 010-3377", address="77 Cedar Lane, Riverton", service_type="Solar",
         monthly_electric_bill=285, roof_age=8, docs=["Utility Bill"], created=11, updated_h=5,
         notes="Saw my neighbor's panels. Mostly want to lower my summer bills. Weekday afternoons are best for calls.",
         stage="Assessment"),
    dict(key="chen", first_name="David", last_name="Chen", email="d.chen@example.com", phone="(555) 010-9012",
         address="310 Harbor View Road, Lakeside", service_type="Battery Storage", monthly_electric_bill=190,
         roof_age=5, docs=["Utility Bill"], created=16, updated_h=20, priority="Low",
         notes="We lose power a few times every winter. Interested in backup for the fridge and home office.",
         stage="Ready for Next Step"),
    dict(key="patel", first_name="Priya", last_name="Patel", email="", phone="(555) 010-6645",
         address="12 Willow Court, Brookfield", service_type="Solar + Battery", monthly_electric_bill=365,
         roof_age=12, docs=["Utility Bill"], created=4, updated_h=7,
         notes="Please text rather than call. Want to know if a battery makes sense with time-of-use rates.",
         stage="Information Gathering"),
    dict(key="johnson", first_name="Robert", last_name="Johnson", email="rjohnson@example.com",
         phone="(555) 010-2290", address="905 Oak Ridge Drive, Brookfield", service_type="Roofing",
         monthly_electric_bill=None, roof_age=24, docs=[], created=6, updated_h=1,
         notes="Had a leak in the upstairs bedroom after the last storm. Need someone to look at it soon.",
         stage="Document Review"),
    dict(key="carter", first_name="Emily", last_name="Carter", email="emily.carter@example.com",
         phone="(555) 010-7718", address="48 Birch Street, Lakeside", service_type="Solar",
         monthly_electric_bill=510, roof_age=3, docs=[], created=0.3, updated_h=2,
         notes="Just bought the house. Not sure what the previous owners' bills looked like.", stage="New Lead"),
    dict(key="wilson", first_name="James", last_name="Wilson", email="jwilson@example.com", phone="(555) 010-5503",
         address="2201 Summit Avenue, Riverton", service_type="Solar + Roofing", monthly_electric_bill=330,
         roof_age=17, docs=["Utility Bill", "Roof Photos"], created=21, updated_h=30,
         notes="Interested, but traveling a lot this month. Email is the best way to reach me.",
         stage="Assessment", blocked="Customer asked to pause until they return from travel."),
    dict(key="mohammed", first_name="Aisha", last_name="Mohammed", email="aisha.m@example.com",
         phone="(555) 010-8834", address="66 Meadow Lane, Brookfield", service_type="Solar",
         monthly_electric_bill=275, roof_age=10, docs=["Utility Bill"], created=34, updated_h=52,
         notes="Ready to move forward. Would like everything handled over email.", stage="Completed"),
    dict(key="nguyen", first_name="Thomas", last_name="Nguyen", email="t.nguyen@example.com",
         phone="(555) 010-4410", address="19 Lakeshore Drive, Lakeside", service_type="Battery Storage",
         monthly_electric_bill=220, roof_age=7, docs=["Utility Bill"], created=8, updated_h=9,
         notes="Work from home and need reliable power. Asked whether a battery can run the whole house or just part.",
         stage="Document Review"),
    dict(key="thompson", first_name="Sarah", last_name="Thompson", email="sarah.t@example.com",
         phone="(555) 010-3156", address="540 Chestnut Street, Riverton", service_type="Solar + Roofing",
         monthly_electric_bill=450, roof_age=21, docs=["Utility Bill", "Roof Photos"], created=14, updated_h=26,
         notes="Roof has some older shingles. Happy to do both projects if the timing works.",
         stage="Assessment", resolve_roof=True),
    dict(key="obrien", first_name="Kevin", last_name="O'Brien", email="kobrien@example.com", phone="",
         address="8 Juniper Way, Brookfield", service_type="Solar", monthly_electric_bill=160, roof_age=6,
         docs=["Utility Bill"], created=5, updated_h=28, priority="Low",
         notes="Just exploring options for now, not in a rush. Mainly curious how the process works.",
         stage="Information Gathering"),
    dict(key="martinez", first_name="Linda", last_name="Martinez", email="linda.martinez@example.com",
         phone="(555) 010-6207", address="1190 Elm Street, Lakeside", service_type="Roofing",
         monthly_electric_bill=None, roof_age=14, docs=["Roof Photos"], created=12, updated_h=15,
         notes="Want a quote to replace the roof before selling the house next spring.",
         stage="Ready for Next Step"),
    dict(key="kim", first_name="Daniel", last_name="Kim", email="daniel.kim@example.com", phone="(555) 010-7390",
         address="302 Pinecrest Road, Riverton", service_type="Solar + Battery", monthly_electric_bill=395,
         roof_age=9, docs=[], requested=["Utility Bill"], created=7, updated_h=11,
         notes="Uploaded a partial bill last week, will send the full one. Curious about going mostly off-grid.",
         stage="Document Review"),
    dict(key="lee", first_name="Grace", last_name="Lee", email="grace.lee@example.com", phone="(555) 010-1184",
         address="75 Orchard Lane, Lakeside", service_type="Solar", monthly_electric_bill=240, roof_age=4,
         docs=["Utility Bill"], created=40, updated_h=70, priority="Low",
         notes="Referred by a coworker. Straightforward, wants minimal back-and-forth.", stage="Completed"),
]

# Extra human-created tasks: (project key, title, department, priority, status, due offset)
EXTRA_TASKS = [
    ("smith", "Schedule initial consultation call", "Scheduling", "Medium", "In Progress", 1),
    ("gonzalez", "Prepare assessment summary", "Project Review", "Medium", "In Progress", 2),
    ("gonzalez", "Confirm weekday afternoon call window", "Customer Support", "Low", "Completed", -3),
    ("chen", "Share next-step options with customer", "Sales", "Medium", "Open", 4),
    ("patel", "Confirm text-message contact preference", "Customer Support", "Low", "Open", -1),
    ("johnson", "Coordinate roof photo collection", "Operations", "High", "In Progress", -2),
    ("carter", "Welcome call and intake questions", "Sales", "Medium", "Open", 1),
    ("wilson", "Check in after customer's travel", "Customer Support", "Low", "Open", 14),
    ("nguyen", "Clarify whole-home vs. partial backup question", "Sales", "Medium", "Open", -1),
    ("thompson", "Compile assessment packet", "Project Review", "Medium", "In Progress", 3),
    ("martinez", "Send quote request to estimating team", "Operations", "Medium", "Open", 2),
    ("kim", "Follow up on full utility bill", "Operations", "Medium", "Open", -4),
    ("mohammed", "Send project completion summary", "Customer Support", "Low", "Completed", -6),
    ("lee", "Close out project file", "Operations", "Low", "Completed", -20),
]

SYNTHETIC_ANALYSES = {
    "gonzalez": dict(
        summary="Customer is interested in solar mainly to reduce summer electric bills. Utility bill is on file "
                "and the project is in Assessment with no open rule findings.",
        detected_intent="Purchase interest", intent_detail="Lower summer electric bills with solar",
        information_status="Complete", missing_information=[],
        recommendation_why=["Utility bill is on file", "Customer prefers weekday afternoon calls"],
        suggested_tasks=[],
        concerns=[{"topic": "Seasonal usage", "detail": "Notes focus on summer bills; usage pattern may vary by season.",
                   "severity": "low", "source": "customer_notes"}],
        recommended_action="Share assessment summary with the customer during a weekday afternoon call",
        suggested_department="Customer Support",
        reasoning="All required documents are on file and no rules are flagged. The customer stated a preferred "
                  "call window, so aligning outreach with it should help the next step move quickly.",
        confidence=0.86, review_status="Accepted", hours_ago=30),
    "nguyen": dict(
        summary="Customer works from home and wants reliable backup power. They asked whether a battery can power "
                "the whole home or only part of it.",
        detected_intent="Information request", intent_detail="Understand how much of the home a battery can back up",
        information_status="Needs clarification",
        missing_information=[{"item": "Backup scope the customer expects", "category": "customer_clarification",
                              "why_it_matters": "Whole-home and partial backup are different conversations."}],
        recommendation_why=["Customer asked about whole-home vs. partial backup", "Required documents are on file"],
        suggested_tasks=[{"title": "Clarify backup scope with customer", "category": "clarify_request",
                          "department": "Sales", "priority": "Medium", "reason": "Open question in the notes."}],
        concerns=[{"topic": "Scope question", "detail": "Customer is unsure about whole-home vs. partial backup.",
                   "severity": "medium", "source": "customer_notes"},
                  {"topic": "Expectations", "detail": "Backup scope should be clarified by qualified staff before "
                                                      "any commitment.", "severity": "medium", "source": "project_data"}],
        recommended_action="Route the backup-scope question to Sales for a clarification call",
        suggested_department="Sales",
        reasoning="The open question is about what a battery system can cover, which should be answered by "
                  "qualified staff. Documents are on file, so clarifying scope is the main blocker.",
        confidence=0.78, review_status="Pending", hours_ago=9),
    "kim": dict(
        summary="Customer is interested in solar plus battery and mentioned going mostly off-grid. Only a partial "
                "utility bill has been provided so far.",
        detected_intent="Purchase interest", intent_detail="Solar plus battery, aiming to be mostly off-grid",
        information_status="Incomplete",
        missing_information=[{"item": "Complete utility bill", "category": "document",
                              "why_it_matters": "Only a partial bill was provided."}],
        recommendation_why=["Utility bill is still requested", "Customer mentioned going mostly off-grid"],
        suggested_tasks=[{"title": "Request complete utility bill", "category": "request_document",
                          "department": "Customer Support", "priority": "Medium", "reason": "Partial bill only."},
                         {"title": "Note off-grid question for review team", "category": "other",
                          "department": "Project Review", "priority": "Low", "reason": "Outside operations scope."}],
        concerns=[{"topic": "Incomplete document", "detail": "Utility bill is still requested; a partial bill was "
                                                             "mentioned.", "severity": "medium", "source": "business_rules"},
                  {"topic": "Expectations", "detail": "An off-grid goal may need expectation-setting by qualified "
                                                      "staff.", "severity": "medium", "source": "customer_notes"}],
        recommended_action="Request the complete utility bill and note the off-grid question for the review team",
        suggested_department="Operations",
        reasoning="Document Review cannot finish without the full bill. The off-grid goal is outside operations "
                  "scope and should be discussed by the appropriate team.",
        confidence=0.72, review_status="Pending", hours_ago=11),
}


def seed(conn):
    ids = {}
    for spec in PROJECTS:
        created = ts(days_ago=spec["created"])
        form = {k: spec.get(k) for k in ("first_name", "last_name", "email", "phone", "address", "service_type",
                                         "monthly_electric_bill", "roof_age", "notes")}
        form["priority"] = spec.get("priority", "Medium")
        form["documents_received"] = spec["docs"]
        pid = workflow.create_project(conn, form, synthetic=True, created_at=created)
        ids[spec["key"]] = pid
        workflow.log_activity(conn, pid, "Project created", "HUMAN", {"service_type": spec["service_type"]})
        for doc_type in spec.get("requested", []):
            conn.execute("UPDATE documents SET status = 'Requested' WHERE project_id = ? AND document_type = ?",
                         (pid, doc_type))
        rules_engine.apply_rules(conn, pid)

        if spec.get("resolve_roof"):
            task = conn.execute("SELECT id FROM tasks WHERE project_id = ? AND rule_key = 'roof_review'",
                                (pid,)).fetchone()
            workflow.set_task_status(conn, task["id"], "Completed")
            rules_engine.apply_rules(conn, pid)

        project = conn.execute("SELECT * FROM projects WHERE id = ?", (pid,)).fetchone()
        status = project["project_status"]
        if spec.get("blocked"):
            status = "Blocked"
        if spec["stage"] == "Completed":
            status = "Completed"
            conn.execute("UPDATE tasks SET status = 'Completed' WHERE project_id = ?", (pid,))
        if spec["stage"] != "New Lead" or status != project["project_status"]:
            workflow.update_workflow(conn, pid, spec["stage"], status, project["priority"],
                                     note=spec.get("blocked", ""))

    for key, title, dept, prio, status, offset in EXTRA_TASKS:
        tid = workflow.create_task(conn, ids[key], title, dept, priority=prio, due_in_days=None, source="HUMAN")
        conn.execute("UPDATE tasks SET status = ?, due_date = ? WHERE id = ?", (status, due(offset), tid))

    # A few rule tasks already in progress or past due, so the Tasks page shows every state.
    for key, rule_key, status, offset in [("johnson", "document:Roof Photos", "Open", -1),
                                          ("patel", "contact_info", "In Progress", 1),
                                          ("carter", "document:Utility Bill", "Open", -2)]:
        conn.execute("UPDATE tasks SET status = ?, due_date = ? WHERE project_id = ? AND rule_key = ?",
                     (status, due(offset), ids[key], rule_key))

    for key, a in SYNTHETIC_ANALYSES.items():
        # Clearly labelled samples (source = 'synthetic'), passed through the same deterministic checks as live output.
        pid = ids[key]
        project, documents, tasks = rules_engine.load_state(conn, pid)
        findings = rules_engine.evaluate(project, documents, tasks)
        missing, _ = intelligence.merge_missing_information(findings, a["missing_information"])
        status = intelligence.final_information_status(missing, a["information_status"])
        data = {**a, "requires_human_review": False, "human_review_reason": ""}
        reasons = intelligence.human_review_policy(data, findings)
        conn.execute(
            """INSERT INTO ai_analyses (project_id, summary, detected_intent, intent_detail, information_status,
                   info_status_adjusted, missing_information, recommended_action, recommendation_why, reasoning,
                   confidence, concerns, suggested_department, suggested_tasks, requires_human_review,
                   human_review_reasons, context_hash, evidence_snapshot, review_status, source, model, response_id,
                   latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'synthetic', NULL, NULL, NULL, ?)""",
            (pid, a["summary"], a["detected_intent"], a["intent_detail"], status, int(status != a["information_status"]),
             json.dumps(missing), a["recommended_action"], json.dumps(a["recommendation_why"]), a["reasoning"],
             a["confidence"], json.dumps(a["concerns"]), a["suggested_department"],
             json.dumps(intelligence.evaluate_task_suggestions(a["suggested_tasks"], findings, tasks)),
             int(bool(reasons)), json.dumps(reasons), intelligence.facts_hash(project, documents),
             json.dumps(explain.capture_evidence(project, documents, tasks, findings, None,
                                                 captured_at=ts(hours_ago=a["hours_ago"])), default=str),
             a["review_status"], ts(hours_ago=a["hours_ago"])))
        workflow.log_activity(conn, pid, "Sample AI analysis loaded (synthetic demo data, not from Claude)", "AI",
                              {"synthetic": True})

    _spread_timestamps(conn, ids)
    from services import evaluation  # synthetic Evaluation Lab cases (no results: those come from real runs only)
    evaluation.ensure_cases(conn)
    from services import approvals  # queue the seeded (clearly synthetic) recommendations for review
    approvals.sync(conn)
    conn.commit()
    return ids


def _spread_timestamps(conn, ids):
    """Seed actions all ran 'now'; spread them between each project's creation and last update."""
    for spec in PROJECTS:
        pid = ids[spec["key"]]
        start = NOW - timedelta(days=spec["created"])
        end = NOW - timedelta(hours=spec["updated_h"])
        if end <= start:
            end = start + timedelta(minutes=30)
        rows = conn.execute("SELECT id, action_type FROM activity_log WHERE project_id = ? ORDER BY id",
                            (pid,)).fetchall()
        # "Project created" and the rule actions it triggered happen within the same minute;
        # later human and AI activity is spread across the project's life.
        first_later = next((i for i, r in enumerate(rows) if i > 0 and r["action_type"] != "AUTOMATION"), len(rows))
        initial, later = rows[:first_later], rows[first_later:]
        for i, row in enumerate(initial):
            conn.execute("UPDATE activity_log SET created_at = ? WHERE id = ?",
                         ((start + timedelta(seconds=4 * i)).isoformat(timespec="seconds"), row["id"]))
        span_start = start + timedelta(hours=1) if end - start > timedelta(hours=2) else start + timedelta(minutes=2)
        n = max(len(later) - 1, 1)
        for i, row in enumerate(later):
            t = span_start + (end - span_start) * (i / n) if len(later) > 1 else end
            conn.execute("UPDATE activity_log SET created_at = ? WHERE id = ?",
                         (t.isoformat(timespec="seconds"), row["id"]))
        conn.execute("UPDATE tasks SET created_at = ? WHERE project_id = ?",
                     ((start + timedelta(minutes=5)).isoformat(timespec="seconds"), pid))
        conn.execute("UPDATE projects SET updated_at = ? WHERE id = ?", (end.isoformat(timespec="seconds"), pid))


def init_db(reset=False):
    if reset and config.DATABASE_PATH.exists():
        config.DATABASE_PATH.unlink()
    conn = database.connect()
    try:
        database.init_schema(conn)
        if database.is_empty(conn):
            seed(conn)
            return True
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    seeded = init_db(reset="--reset" in sys.argv)
    print(f"Database ready at {config.DATABASE_PATH} ({'seeded with synthetic demo data' if seeded else 'already had data'})")
