"""
Run the Automation Opportunity Finder against the REAL Anthropic API.

    python run_automation_examples.py            # runs the 3 demo processes + edge cases, saves the 3 demos
    python run_automation_examples.py --no-save  # same, but saves nothing

The three demo processes (document intake, customer follow-up, internal
project handoff) are saved to your OpsPilot database so they appear under
"Saved analyses" for the interview. Running it again replaces them.

Hard checks (must pass) verify the parts OpsPilot controls: metrics are counted
from the step classifications, no efficiency claims are shown, the proposed
workflow starts with a trigger and ends with an action, human decisions have a
human approval stage, and approval-type steps are never left to AI/automation.
AI checks (review) cover judgement that may vary between runs.
"""
import argparse
import json
import re
import sys

if hasattr(sys.stdout, "reconfigure"):  # safe output on Windows consoles and redirects
    sys.stdout.reconfigure(errors="replace")

import config
import database
from services import ai_service, automation_finder as af

EDGE_CASES = [
    {"name": "Too vague", "text": "We handle invoices every week and it takes a long time.", "expect": {"too_vague", "not_a_process"}},
    {"name": "Not a process", "text": "My favorite pasta recipe uses garlic, olive oil, chili flakes and lots of parmesan cheese.",
     "expect": {"not_a_process", "too_vague"}},
    {"name": "Refund approvals (consequential)", "expect": {"valid_process"}, "needs_human": True,
     "text": ("Customers email asking for a refund. I find their order, check whether it is within 30 days, read their "
              "reason, decide whether to approve the refund, issue the payment in our billing system, and email the "
              "customer with the outcome.")},
]
JARGON = re.compile(r"\b(OCR|RPA|NLP|API|LLM|pipeline|webhook|ETL|ML model|machine learning)\b", re.I)
CLAIM = re.compile(r"\d+\s?%|\bpercent\b|\bhours? (a|per) (week|day)\b|\$\s?\d", re.I)
APPROVAL = re.compile(r"\bapprov|\bsign[- ]?off|\bsigns off|\bdecide whether|\brefund\b.*\b(issue|approve)|\bdiscount", re.I)


def texts(result):
    yield result["summary"]
    for s in result["steps"]:
        yield s["description"]; yield s["justification"]
    for r in result["risks"]:
        yield r["detail"]
    for st in result["proposed"]:
        yield st["description"]


def check(db, analysis_id, needs_human):
    r = af.load(db, analysis_id)
    row = db.execute("SELECT * FROM automation_analyses WHERE id = ?", (analysis_id,)).fetchone()
    steps = r["steps"]
    recount = {"total": len(steps),
               "automatable": sum(s["classification"] in ("traditional_automation", "business_rule") for s in steps),
               "ai": sum(s["classification"] == "ai_candidate" for s in steps),
               "human": sum(s["classification"] == "human_decision" for s in steps)}
    stages = [st["stage"] for st in r["proposed"]]
    hard = [
        ("metrics equal a recount of the step classifications", all(r["metrics"][k] == v for k, v in recount.items())),
        ("stored metric columns match", (row["total_steps"], row["automation_opportunities"], row["ai_opportunities"],
                                         row["human_decisions"]) == (recount["total"], recount["automatable"], recount["ai"], recount["human"])),
        ("no efficiency claims in displayed text", not any(CLAIM.search(t or "") for t in texts(r))),
        ("proposed workflow starts with a trigger", stages[:1] == ["trigger"]),
        ("proposed workflow ends with an action", stages[-1:] == ["action"]),
        ("human approval stage exists when there are human decisions",
         not recount["human"] or "human_approval" in stages),
        ("approval-type steps are human decisions",
         all(s["classification"] == "human_decision" for s in steps
             if APPROVAL.search(s["name"] + " " + s["description"]))),
        ("every step reference in the workflow exists", all(n <= len(steps) for st in r["proposed"] for n in st["steps"])),
    ]
    if needs_human:
        hard.append(("at least one human decision", recount["human"] >= 1))
    ai = [("3-12 steps", 3 <= len(steps) <= 12),
          ("no technical jargon", not any(JARGON.search(t or "") for t in texts(r))),
          ("at least one risk listed", bool(r["risks"]))]
    return r, hard, ai


def show(r):
    print(f"  Name:    {r['process_name']}")
    print(f"  Summary: {r['summary']}")
    m = r["metrics"]
    print(f"  Metrics (Python): {m['total']} steps | {m['automatable']} automatable | {m['ai']} AI | {m['human']} human decisions"
          f" | {m['manual']} manual")
    for s in r["steps"]:
        adj = f"   <- policy: AI said {af.CLASSES[s['ai_classification']]['label']}" if s["adjusted"] else ""
        print(f"    {s['number']:2}. {s['name']:<44} {af.CLASSES[s['classification']]['label']}{adj}")
    print("  Proposed: " + " -> ".join(f"{st['label']} ({st['title']})" for st in r["proposed"]))
    for p in r["review_points"]:
        print(f"  Review:  {p['point']}  [{p['source']}]")
    for x in r["risks"]:
        print(f"  Risk:    {x['title']} ({x['severity']})")
    if r["claims_removed"]:
        print(f"  Removed {r['claims_removed']} efficiency claim(s) from the AI text")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    print(f"Automation Opportunity Finder  |  model {config.AI_MODEL}")
    if not ai_service.is_configured():
        print("ANTHROPIC_API_KEY is not set. Add it to .env and run again.")
        return 1
    db = database.connect()
    database.init_schema(db)
    hard_fail = ai_review = api_errors = 0

    cases = [{"name": e["name"], "text": e["text"], "expect": {"valid_process"}, "needs_human": True, "save": True}
             for e in af.EXAMPLES] + EDGE_CASES
    for case in cases:
        print("\n" + "=" * 88 + f"\n{case['name']}\n" + "=" * 88)
        try:
            analysis_id, data, result = af.analyze(db, case["text"], case["name"] if case.get("save") else "")
        except ai_service.AIServiceError as err:
            api_errors += 1
            print(f"  Process analysis temporarily unavailable [{err.code}] {err.message}")
            continue
        status = data["status"]
        ok = status in case["expect"]
        hard_fail += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  input assessment: {status}  (response {result.response_id}, {result.latency_ms} ms)")
        if analysis_id is None:
            print(f"  Message: {data['clarifying_question']}")
            continue
        db.commit()
        r, hard, ai = check(db, analysis_id, case.get("needs_human"))
        show(r)
        for label, ok in hard:
            hard_fail += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        for label, ok in ai:
            ai_review += 0 if ok else 1
            print(f"  AI check: {'ok    ' if ok else 'REVIEW'} {label}")
        if case.get("save") and not args.no_save:
            db.execute("DELETE FROM automation_analyses WHERE is_saved = 1 AND process_name = ? AND id != ?",
                       (case["name"], analysis_id))
            af.save(db, analysis_id, case["name"])
            db.commit()
            print(f"  Saved as “{case['name']}” (analysis #{analysis_id})")

    print("\n" + "=" * 88)
    print(f"Hard check failures: {hard_fail}   AI checks to review: {ai_review}   API errors: {api_errors}")
    db.close()
    return 1 if hard_fail or api_errors else 0


if __name__ == "__main__":
    sys.exit(main())
