"""
Send the three sample workflow descriptions through the REAL Anthropic API and validate the results.

    python run_workflow_examples.py            # generates, checks and saves the 3 samples as Drafts
    python run_workflow_examples.py --no-save

The samples are saved as *Drafts*. Nothing is approved automatically: open
Workflows in the app to request approval and approve them yourself.
Re-running replaces earlier drafts with the same names.

Hard checks (must pass) cover what OpsPilot guarantees: graph structure,
labelled branches, the human-approval policy, a clean layout, and the status
rules. AI checks (review) cover things that depend on the model's judgement.
"""
import argparse
import sys
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import config
import database
from services import ai_service
from services import workflow_designer as wd

EDGE_CASES = [
    {"name": "Too vague", "text": "We should automate approvals somehow for new projects.", "expect": {"too_vague", "not_a_workflow"}},
    {"name": "Not a workflow", "text": "Plan a surprise birthday dinner for my sister with a cake and her favorite music.",
     "expect": {"not_a_workflow", "too_vague"}},
]


def structure_checks(d):
    nodes, edges, pos = d["nodes"], d["edges"], d["layout"]["positions"]
    ids = {n["id"] for n in nodes}
    kinds = {n["id"]: n["type"] for n in nodes}
    out, inc = defaultdict(list), defaultdict(list)
    for e in edges:
        out[e["from"]].append(e)
        inc[e["to"]].append(e)
    order = wd._topo(nodes, edges, d["trigger"])
    checks = [
        ("exactly one trigger, with nothing leading into it",
         sum(n["type"] == "trigger" for n in nodes) == 1 and not inc[d["trigger"]]),
        ("every connection joins two real steps", all(e["from"] in ids and e["to"] in ids and e["from"] != e["to"] for e in edges)),
        ("every step is reachable from the trigger and the drawn graph has no cycles", len(order) == len(nodes)),
        ("every condition has 2+ labelled outcomes",
         all(len(out[n["id"]]) + len([r for r in n["returns_to"]]) >= 2 and all(e["label"] for e in out[n["id"]])
             for n in nodes if n["type"] == "condition")),
        ("consequential steps are behind a human approval",
         all(wd._covered(nodes, edges, d["trigger"], n["id"]) for n in nodes
             if n["type"] not in ("trigger", "human_approval") and wd._consequential_reason(n))),
        ("AI-written customer messages are approved by a person before they are sent",
         all(inc[n["id"]] and all(kinds[e["from"]] == "human_approval" for e in inc[n["id"]])
             for n in nodes if n["type"] not in ("trigger", "human_approval") and wd._ai_customer_message(n)
             and not wd._consequential_reason(n))
         and not any(wd._unapproved_path_to_send(nodes, edges, n["id"]) for n in nodes
                     if wd._ai_writes_customer_message(n) and not wd._ai_customer_message(n))),
        ("human approval steps are marked as requiring approval",
         all(n["requires_human_approval"] for n in nodes if n["type"] == "human_approval")),
        ("no overlapping steps on the canvas",
         not any(abs(pos[a]["x"] - pos[b]["x"]) < wd.NODE_W and abs(pos[a]["y"] - pos[b]["y"]) < wd.NODE_H
                 for a in pos for b in pos if a < b)),
        ("no connection line runs through another step", not crossings(d)),
        ("everything fits inside the canvas",
         all(0 <= p["x"] and p["x"] + wd.NODE_W <= d["layout"]["width"] and p["y"] + wd.NODE_H <= d["layout"]["height"]
             for p in pos.values())),
    ]
    ai = [("3-15 steps", 3 <= len(nodes) <= 15),
          ("uses at least one condition", any(n["type"] == "condition" for n in nodes)),
          ("every step lists the data it uses", all(n["data_used"] for n in nodes)),
          ("every step explains why it exists", all(n["purpose"] for n in nodes))]
    return checks, ai


def crossings(d):
    pos = d["layout"]["positions"]
    bad = []
    for e in d["layout"]["edges"]:
        pts = e["points"]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            for nid, p in pos.items():
                if nid in (e["from"], e["to"]):
                    continue
                left, right, top, bottom = p["x"], p["x"] + wd.NODE_W, p["y"], p["y"] + wd.NODE_H
                if x1 == x2 and left < x1 < right and max(min(y1, y2), top) < min(max(y1, y2), bottom):
                    bad.append((e["from"], e["to"], nid))
                if y1 == y2 and top < y1 < bottom and max(min(x1, x2), left) < min(max(x1, x2), right):
                    bad.append((e["from"], e["to"], nid))
    return bad


def show(design):
    d = design["definition"]
    print(f"  Name:    {design['name']}\n  Summary: {d['summary']}")
    for n in d["nodes"]:
        flags = []
        if n["ai_involvement"] != "none":
            flags.append(f"AI {n['ai_involvement']}")
        if n["requires_human_approval"]:
            flags.append("needs approval")
        if n["added_by_policy"]:
            flags.append("ADDED BY POLICY")
        print(f"    [{wd.NODE_TYPES[n['type']]['label']:<14}] {n['title']:<40} {', '.join(flags)}")
    for e in d["edges"]:
        print(f"      {e['from']} -> {e['to']}" + (f"  ({e['label']})" if e["label"] else ""))
    for l in d["loops"]:
        print(f"      {l['from']} ~> returns to {l['to']}" + (f"  ({l['label']})" if l["label"] else ""))
    for note in d["notes"]:
        print(f"  OpsPilot {note['kind']}: {note['text']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()
    print(f"AI Workflow Builder samples  |  model {config.AI_MODEL}")
    if not ai_service.is_configured():
        print("ANTHROPIC_API_KEY is not set. Add it to .env and run again.")
        return 1
    db = database.connect()
    database.init_schema(db)
    hard = review = api_errors = 0

    for case in [{"name": e["name"], "text": e["text"], "sample": True} for e in wd.EXAMPLES] + EDGE_CASES:
        print("\n" + "=" * 88 + f"\n{case['name']}\n" + "=" * 88)
        try:
            design_id, data, result = wd.generate(db, case["text"], case["name"] if case.get("sample") else "")
        except ai_service.AIServiceError as err:
            api_errors += 1
            print(f"  Workflow generation temporarily unavailable [{err.code}] {err.message}")
            continue
        if not case.get("sample"):
            ok = design_id is None and data["status"] in case["expect"]
            hard += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  assessed as {data.get('status', 'valid_workflow')}: {data.get('message', '')}")
            db.rollback()
            continue
        if design_id is None:
            hard += 1
            print(f"  FAIL  expected a workflow, got {data['status']}: {data['message']}")
            continue
        db.commit()
        design = wd.load(db, design_id)
        print(f"  Response {result.response_id}  ({result.latency_ms} ms)")
        show(design)
        checks, ai = structure_checks(design["definition"])
        checks.append(("new proposal is unsaved (AI cannot set a status)", design["status"] is None))
        try:
            wd.transition(db, design_id, "approve")
            checks.append(("an unsaved proposal cannot be approved", False))
        except PermissionError:
            checks.append(("an unsaved proposal cannot be approved", True))
        for label, ok in checks:
            hard += 0 if ok else 1
            print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        for label, ok in ai:
            review += 0 if ok else 1
            print(f"  AI check: {'ok    ' if ok else 'REVIEW'} {label}")
        if not args.no_save:
            old = db.execute("SELECT id FROM workflow_designs WHERE name = ? AND status = 'Draft' AND id != ?",
                             (case["name"], design_id)).fetchall()
            for r in old:
                db.execute("DELETE FROM workflow_design_events WHERE design_id = ?", (r["id"],))
                db.execute("DELETE FROM workflow_designs WHERE id = ?", (r["id"],))
            status, _ = wd.transition(db, design_id, "save_draft", case["name"])
            db.commit()
            print(f"  Saved as {status} (workflow #{design_id}). Approve it yourself in the app.")

    print("\n" + "=" * 88)
    print(f"Hard check failures: {hard}   AI checks to review: {review}   API errors: {api_errors}")
    db.close()
    return 1 if hard or api_errors else 0


if __name__ == "__main__":
    sys.exit(main())
