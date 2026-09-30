"""
Check the "Why?" explanations against REAL Claude responses.

    python run_explainability_check.py

Runs on a throwaway copy of the demo data (instance/explain_test.db). It makes these real API calls:
  * 4 project analyses (John Smith, Emily Carter, Priya Patel, Robert Johnson)
  * 1 customer message draft
  * 1 Automation Finder analysis (Document intake example)
  * 1 Workflow Builder proposal (Customer message triage example)

For each result it builds the explanation exactly as the app does, prints it, and checks that:
  * evidence values equal the database values captured when the AI ran
  * the AI section contains only the model's stored structured output
  * triggered rules match the deterministic rules engine
  * the human decision matches the Approval Center record
  * no key or internal reasoning field appears in any explanation
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import config

config.DATABASE_PATH = config.BASE_DIR / "instance" / "explain_test.db"

import database  # noqa: E402
import seed  # noqa: E402
from services import (ai_service, approvals, automation_finder, explain, intelligence, rules_engine,  # noqa: E402
                      workflow_designer)

PROJECTS = ["John Smith", "Emily Carter", "Priya Patel", "Robert Johnson"]
hard = 0


def check(label, ok, detail=""):
    global hard
    hard += 0 if ok else 1
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail and not ok else ""))


def pid(db, name):
    return db.execute("SELECT p.id FROM projects p JOIN customers c ON c.id = p.customer_id "
                      "WHERE c.first_name || ' ' || c.last_name = ?", (name,)).fetchone()[0]


def show(x):
    print(f"  Recommendation: {x['recommendation']['text']}")
    for b in x["evidence"]["blocks"]:
        if b["type"] == "facts":
            print(f"  Evidence · {b['title']}: " + "; ".join(f"{f['label']}={f['value']}" for f in b["items"][:6]))
    for b in x["ai"]["blocks"]:
        if b["type"] == "cited":
            for c in b["items"]:
                print(f"  AI cited: “{c['text']}” -> {c['state']}{(': ' + c['match']) if c['match'] else ''}")
    for r in x["rules"]["items"]:
        print(f"  Rule [{r['state']}] {r['name']}: {r.get('result') or r.get('condition') or ''}")
    print(f"  Human: {x['human']['headline']}  Decision: {(x['human'].get('decision') or {}).get('label')}")


def safe(x):
    blob = json.dumps(x).lower()
    key = (ai_service._api_key() or "").lower() if hasattr(ai_service, "_api_key") else ""
    return (not key or key not in blob) and "chain of thought" not in blob and "chain-of-thought" not in blob


def main():
    print(f"Explainability check with the real Anthropic API  |  model {config.AI_MODEL}")
    if not ai_service.is_configured():
        print("ANTHROPIC_API_KEY is not set. Add it to .env and run again.")
        return 1
    seed.init_db(reset=True)
    db = database.connect()
    errors = 0

    for name in PROJECTS:
        print("\n" + "-" * 88 + f"\nProject analysis: {name}")
        p = pid(db, name)
        project, documents, tasks = rules_engine.load_state(db, p)
        before = {d["document_type"]: d["status"] for d in documents}
        try:
            aid, result = intelligence.run_project_analysis(db, p)
            approvals.sync(db)
            db.commit()
        except ai_service.AIServiceError as err:
            errors += 1
            print(f"  AI temporarily unavailable [{err.code}] {err.message}")
            continue
        print(f"  Response {result.response_id} ({result.latency_ms} ms)")
        x = explain.analysis(db, aid)
        show(x)
        a = database.get_analysis(db, aid)
        facts = {f["label"]: f for b in x["evidence"]["blocks"] if b["type"] == "facts" for f in b["items"]}
        check("evidence: document statuses equal the database at analysis time",
              all(facts[k]["value"] == v for k, v in before.items()))
        roof = project["roof_age"]
        check("evidence: roof age equals the database", facts["Roof age"]["value"] == (f"{roof} years" if roof is not None else "Not provided"))
        ai = {b.get("label"): b for b in x["ai"]["blocks"]}
        check("AI section: only stored structured output", ai["Recommended action"]["value"] == a["recommended_action"]
              and ai["Written justification"]["text"] == a["reasoning"] and ai["Summary"]["text"] == a["summary"])
        cited = next((b for b in x["ai"]["blocks"] if b["type"] == "cited"), {"items": []})["items"]
        check("AI section: every cited fact is checked against the record", len(cited) == len(a["recommendation_why"]))
        conflicts = [c["text"] for c in cited if c["state"] == "conflict"]
        if conflicts:
            print(f"  NOTE  the AI stated something the record contradicts (shown to the reviewer): {conflicts}")
        findings = [f for f in rules_engine.evaluate(project, documents, tasks) if not f.resolved]
        triggered = [r for r in x["rules"]["items"] if r["state"] == "triggered" and r["name"] in
                     {v[0] for v in explain.RULE_CATALOG.values()}]
        check("rules: triggered rules equal the rules engine findings", len(triggered) == len(findings),
              f"{len(triggered)} vs {len(findings)}")
        item = db.execute("SELECT status FROM approval_items WHERE analysis_id = ?", (aid,)).fetchone()
        check("human: approval required and matches the Approval Center",
              x["human"]["required"] and item is not None and x["human"]["decision"]["status"] == item["status"])
        check("no key or internal reasoning in the explanation", safe(x))

    print("\n" + "-" * 88 + "\nCustomer message draft: Emily Carter")
    try:
        item_id, result = approvals.draft_communication(db, pid(db, "Emily Carter"), "missing_information")
        db.commit()
        x = explain.approval_item(db, item_id)
        show(x)
        sent = json.loads(db.execute("SELECT ai_input FROM approval_items WHERE id = ?", (item_id,)).fetchone()[0])
        block = next(b for b in x["evidence"]["blocks"] if b["title"].startswith("Exactly what was sent"))
        check("evidence: exactly the fields sent to Claude", len(block["items"]) == len(sent))
        check("rules: all six draft checks listed", any(r["name"] == "Draft checks" and len(r["items"]) == 6 for r in x["rules"]["items"]))
        check("human: customer-facing message needs approval", x["human"]["required"])
        check("no key or internal reasoning in the explanation", safe(x))
    except ai_service.AIServiceError as err:
        errors += 1
        print(f"  AI temporarily unavailable [{err.code}] {err.message}")

    print("\n" + "-" * 88 + "\nAutomation Finder: Document intake")
    try:
        fid, data, result = automation_finder.analyze(db, automation_finder.EXAMPLES[0]["text"], None)
        db.commit()
        if fid is None:
            print(f"  The model asked for more detail: {data.get('clarifying_question')}")
        else:
            for s in data["steps"]:
                x = explain.automation_step(db, fid, s["number"])
                first = x["rules"]["items"][0]
                print(f"  Step {s['number']} {s['name']}: {first['state']} · {first.get('result', '')[:90]}")
                ok = (first["state"] == "adjusted") == bool(s["adjusted"])
                check(f"step {s['number']}: policy result matches the stored step", ok)
                check(f"step {s['number']}: no key or internal reasoning", safe(x))
    except ai_service.AIServiceError as err:
        errors += 1
        print(f"  AI temporarily unavailable [{err.code}] {err.message}")

    print("\n" + "-" * 88 + "\nWorkflow Builder: Customer message triage")
    try:
        wid, data, result = workflow_designer.generate(db, workflow_designer.EXAMPLES[1]["text"], None, None)
        db.commit()
        if wid is None:
            print(f"  The model asked for more detail: {data.get('message')}")
        else:
            x = explain.workflow(db, wid)
            show(x)
            nodes = workflow_designer.load(db, wid)["definition"]["nodes"]
            check("human: a person must approve before Active", x["human"]["required"])
            check("rules: every validation note is listed", len([r for r in x["rules"]["items"] if r["state"] != "passed"])
                  >= len([n for n in workflow_designer.load(db, wid)["definition"]["notes"] if n["kind"] in ("fix", "policy", "warn")]))
            check("every step has its own explanation", all(explain.workflow(db, wid, n["id"]) for n in nodes))
            check("no key or internal reasoning in the explanation", safe(x))
    except ai_service.AIServiceError as err:
        errors += 1
        print(f"  AI temporarily unavailable [{err.code}] {err.message}")

    db.close()
    print("\n" + "=" * 88)
    print(f"Hard check failures: {hard}   API errors: {errors}")
    return 1 if hard or errors else 0


if __name__ == "__main__":
    sys.exit(main())
