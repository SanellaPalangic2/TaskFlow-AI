"""
Run the five Intelligence Layer scenarios against the REAL Anthropic API.

    python run_scenarios.py            # each scenario analyzed twice
    python run_scenarios.py --runs 3   # more runs = more wording variation

Uses a separate, throwaway database (instance/scenarios.db), so your demo data
is not touched. For every scenario it:

  1. creates the project and runs the business rules (as the New Project page does)
  2. analyzes it with Claude through the normal Intelligence Layer
  3. checks the deterministic results: rule tasks, status and stage must be
     identical before and after every AI run, whatever the AI wrote
  4. prints the AI output so you can see the wording change between runs

Rule checks are pass/fail. AI checks are reported for review, because AI
judgement is allowed to vary; the rules are not.
"""
import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):  # safe output on Windows consoles and redirects
    sys.stdout.reconfigure(errors="replace")

import config

config.DATABASE_PATH = config.BASE_DIR / "instance" / "scenarios.db"  # never the demo database

import database  # noqa: E402
from services import ai_service, intelligence, rules_engine, workflow  # noqa: E402

BASE_CONTACT = {"email": "test.customer@example.com", "phone": "(555) 010-2000", "address": "1 Test Street, Riverton"}

SCENARIOS = [
    {
        "name": "1. Complete project, nothing obviously missing",
        "form": {**BASE_CONTACT, "first_name": "Casey", "last_name": "Complete", "service_type": "Solar",
                 "monthly_electric_bill": 240, "roof_age": 6, "documents_received": ["Utility Bill"],
                 "notes": "Ready to move forward with solar. Please email me the next steps."},
        "rules": {"findings": [], "status": "Active"},
        "ai": lambda a, n: [("information status is not Incomplete", a["information_status"] != "Incomplete")],
    },
    {
        "name": "2. Missing utility document",
        "form": {**BASE_CONTACT, "first_name": "Morgan", "last_name": "Nobill", "service_type": "Solar",
                 "monthly_electric_bill": 310, "roof_age": 9, "documents_received": [],
                 "notes": "Interested in solar to cut costs. I'll look for my utility bill this weekend."},
        "rules": {"findings": ["document:Utility Bill"], "status": "Needs Attention",
                  "tasks": [("Request utility bill", "Customer Support", "Medium")]},
        "ai": lambda a, n: [("information status is Incomplete", a["information_status"] == "Incomplete"),
                            ("Utility Bill listed as missing",
                             any("utility" in m["item"].lower() for m in a["missing_information"]))],
    },
    {
        "name": "3. Solar + Roofing with an old roof",
        "form": {**BASE_CONTACT, "first_name": "John", "last_name": "Smith", "service_type": "Solar + Roofing",
                 "monthly_electric_bill": 420, "roof_age": 19, "documents_received": [],
                 "notes": "I want solar but I'm worried my roof may need to be replaced first."},
        "rules": {"findings": ["document:Utility Bill", "document:Roof Photos", "roof_review"],
                  "status": "Human Review Required", "priority": "High",
                  "tasks": [("Request utility bill", "Customer Support", "Medium"),
                            ("Request roof photos", "Customer Support", "Medium"),
                            ("Review roof information with qualified staff", "Project Review", "High")]},
        "ai": lambda a, n: [("human review required", bool(a["requires_human_review"])),
                            ("next best action source is Combined", n["source"] == "Combined")],
    },
    {
        "name": "4. Missing customer contact information",
        "form": {"first_name": "Riley", "last_name": "Nocontact", "email": "", "phone": "",
                 "address": "22 Harbor Road, Lakeside", "service_type": "Battery Storage",
                 "monthly_electric_bill": 200, "roof_age": None, "documents_received": ["Utility Bill"],
                 "notes": "Want backup power for the outages we keep getting."},
        "rules": {"findings": ["contact_info"], "status": "Needs Attention",
                  "tasks": [("Collect missing contact information", "Customer Support", "High")]},
        "ai": lambda a, n: [("information status is Incomplete", a["information_status"] == "Incomplete")],
    },
    {
        "name": "5. Ambiguous customer notes",
        "form": {**BASE_CONTACT, "first_name": "Avery", "last_name": "Unsure", "service_type": "Solar + Battery",
                 "monthly_electric_bill": 280, "roof_age": 11, "documents_received": ["Utility Bill"],
                 "notes": "Not sure what I need yet. My neighbor mentioned something about credits? "
                          "Maybe later this year, depends on a few things."},
        "rules": {"findings": [], "status": "Active"},
        "ai": lambda a, n: [("flagged as ambiguous (clarification, unclear intent, low confidence or review)",
                             a["information_status"] == "Needs clarification" or a["detected_intent"] == "Unclear"
                             or a["confidence"] < config.HUMAN_REVIEW_CONFIDENCE_THRESHOLD
                             or bool(a["requires_human_review"]))],
    },
]


def rule_snapshot(db, pid):
    project, documents, tasks = rules_engine.load_state(db, pid)
    findings = rules_engine.evaluate(project, documents, tasks)
    rule_tasks = sorted((t["title"], t["department"], t["priority"], t["status"])
                        for t in tasks if t["source"] == "AUTOMATION")
    return {"findings": sorted(f.rule_key for f in findings if not f.resolved), "tasks": rule_tasks,
            "status": project["project_status"], "stage": project["current_stage"], "priority": project["priority"],
            "ai_task_count": sum(1 for t in tasks if t["source"] == "AI")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=2)
    args = parser.parse_args()

    print(f"OpsPilot Intelligence Layer scenarios  |  model {config.AI_MODEL}  |  {args.runs} AI runs each")
    if not ai_service.is_configured():
        print("ANTHROPIC_API_KEY is not set. Add it to .env and run again.")
        return 1

    if config.DATABASE_PATH.exists():
        config.DATABASE_PATH.unlink()
    db = database.connect()
    database.init_schema(db)
    failures, ai_notes, api_errors = 0, 0, 0

    for sc in SCENARIOS:
        print("\n" + "=" * 88 + f"\n{sc['name']}\n" + "=" * 88)
        form = {**sc["form"], "priority": "Medium"}
        pid = workflow.create_project(db, form)
        workflow.log_activity(db, pid, "Project created", "HUMAN")
        rules_engine.apply_rules(db, pid)
        db.commit()
        baseline = rule_snapshot(db, pid)
        exp = sc["rules"]

        checks = [("rule findings", baseline["findings"] == sorted(exp["findings"])),
                  ("status after rules", baseline["status"] == exp["status"])]
        if "priority" in exp:
            checks.append(("priority escalated", baseline["priority"] == exp["priority"]))
        for title, dept, prio in exp.get("tasks", []):
            checks.append((f"task '{title}' ({dept}, {prio})",
                           any(t[:3] == (title, dept, prio) for t in baseline["tasks"])))
        if not exp.get("tasks"):
            checks.append(("no rule tasks created", not baseline["tasks"]))

        print("Business rules:", ", ".join(baseline["findings"]) or "no findings")
        for t in baseline["tasks"]:
            print(f"  task: {t[0]}  [{t[1]}, {t[2]}]")

        for run in range(1, args.runs + 1):
            try:
                analysis_id, result = intelligence.run_project_analysis(db, pid)
                db.commit()
            except ai_service.AIServiceError as err:
                db.rollback()
                api_errors += 1
                print(f"\n  Run {run}: AI analysis temporarily unavailable [{err.code}] {err.message}")
                continue
            a = database.analysis_to_dict(db.execute("SELECT * FROM ai_analyses WHERE id = ?", (analysis_id,)).fetchone())
            project, documents, tasks = rules_engine.load_state(db, pid)
            nba = intelligence.next_best_action(rules_engine.evaluate(project, documents, tasks), a)
            after = rule_snapshot(db, pid)

            print(f"\n  Run {run}  response {result.response_id}  ({result.latency_ms} ms)")
            print(f"    Summary:     {a['summary']}")
            print(f"    Intent:      {a['detected_intent']} ({a['intent_detail']})  confidence {a['confidence']:.0%}")
            print(f"    Info status: {a['information_status']}{'  (set by rules)' if a['info_status_adjusted'] else ''}")
            for m in a["missing_information"]:
                print(f"    Missing:     {m['item']}  [{'rule' if m['source'] == 'rule' else 'AI'}]")
            for c in a["concerns"]:
                print(f"    Concern:     {c['topic']} ({c['severity']})")
            print(f"    Next action: {nba['headline']}  [source: {nba['source']}]")
            for w in nba["why"]:
                print(f"      why:       {w['text']}  [{'rule' if w['source'] == 'rule' else 'AI'}]")
            for t in a["suggested_tasks"]:
                print(f"    AI task:     {t['title']}  -> {t['decision']}: {t['note']}")
            print(f"    Review:      {'required' if a['requires_human_review'] else 'not required'}"
                  + "".join(f"; {r['text']}" for r in a["human_review_reasons"]))

            same = {k: after[k] for k in ("findings", "tasks", "status", "stage", "priority")} == \
                   {k: baseline[k] for k in ("findings", "tasks", "status", "stage", "priority")}
            checks.append((f"run {run}: rule tasks, status, stage and priority unchanged by AI", same))
            checks.append((f"run {run}: AI created no tasks on its own", after["ai_task_count"] == 0))
            for label, ok in sc["ai"](a, nba):
                ai_notes += 0 if ok else 1
                print(f"    AI check:    {'ok   ' if ok else 'REVIEW'} {label}")

        print("\n  Rule checks:")
        for label, ok in checks:
            failures += 0 if ok else 1
            print(f"    {'PASS' if ok else 'FAIL'}  {label}")

    db.close()
    print("\n" + "=" * 88)
    print(f"Rule check failures: {failures}   AI checks to review: {ai_notes}   API errors: {api_errors}")
    print("Rule checks must always pass. AI checks may vary between runs; review any flagged ones.")
    return 1 if failures or api_errors else 0


if __name__ == "__main__":
    sys.exit(main())
