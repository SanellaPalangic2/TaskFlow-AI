"""
Test the Human Approval Center with REAL Claude-drafted customer communications.

    python run_approval_drafts.py            # tests on a throwaway copy, then queues 3 live drafts in your demo data
    python run_approval_drafts.py --no-save  # tests only

Part 1 runs on a fresh copy of the demo data (instance/approvals_test.db):
  * drafts 5 messages for different projects and purposes with the real API
  * checks that only minimal context was sent (no last name, email, phone, address, bill, roof age or notes)
  * approves one, edits-and-approves one, rejects one
  * verifies the audit trail: original AI output unchanged, human correction stored beside it,
    decisions append-only, AI drafting logged as AI and decisions logged as HUMAN, nothing sent

Part 2 (unless --no-save) drafts 3 live messages into your OpsPilot database so the
Approval Center has real items waiting for you. They stay pending; nothing is sent.
"""
import argparse
import json
import sqlite3
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import config

DEMO_DB = config.DATABASE_PATH
TEST_DB = config.BASE_DIR / "instance" / "approvals_test.db"

CASES = [  # (customer name, purpose)
    ("John Smith", "missing_information"),
    ("Emily Carter", "missing_information"),
    ("Priya Patel", "missing_information"),
    ("John Smith", "roof_concern"),
    ("Maria Gonzalez", "status_update"),
]
LIVE = [("Emily Carter", "missing_information"), ("Robert Johnson", "missing_information"), ("Kevin O'Brien", "missing_information")]
ALLOWED_KEYS = {"customer_first_name", "service", "project_stage", "message_goal", "missing_documents",
                "missing_contact_details", "roof_review_pending", "customer_mentioned_roof_concern", "sign_off"}


def use_db(path):
    config.DATABASE_PATH = path


def project_id(db, name):
    return db.execute("SELECT p.id FROM projects p JOIN customers c ON c.id = p.customer_id "
                      "WHERE c.first_name || ' ' || c.last_name = ?", (name,)).fetchone()[0]


def sensitive_values(db, pid):
    r = db.execute("""SELECT c.last_name, c.email, c.phone, c.address, p.notes, p.monthly_electric_bill, p.roof_age
                      FROM projects p JOIN customers c ON c.id = p.customer_id WHERE p.id = ?""", (pid,)).fetchone()
    vals = [r["last_name"], r["email"], r["phone"], r["address"], (r["notes"] or "")[:40]]
    nums = [str(int(r["monthly_electric_bill"])) if r["monthly_electric_bill"] else None]
    return [v for v in vals if v], [n for n in nums if n]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-save", action="store_true")
    args = ap.parse_args()

    use_db(TEST_DB)
    import database
    import seed
    from services import ai_service, approvals
    print(f"Human Approval Center: real AI drafting test  |  model {config.AI_MODEL}")
    if not ai_service.is_configured():
        print("ANTHROPIC_API_KEY is not set. Add it to .env and run again.")
        return 1
    seed.init_db(reset=True)
    db = database.connect()
    hard = review = api_errors = 0

    def check(label, ok):
        nonlocal hard
        hard += 0 if ok else 1
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")

    items = []
    for name, purpose in CASES:
        pid = project_id(db, name)
        print("\n" + "-" * 88 + f"\n{name}  |  {approvals.PURPOSES[purpose][0]}")
        try:
            item_id, result = approvals.draft_communication(db, pid, purpose)
            db.commit()
        except ai_service.AIServiceError as err:
            api_errors += 1
            print(f"  AI drafting temporarily unavailable [{err.code}] {err.message}")
            continue
        it = approvals.get(db, item_id)
        items.append(it)
        print(f"  Response {result.response_id} ({result.latency_ms} ms)  confidence {it['confidence']:.0%}")
        print(f"  Subject: {it['ai_output']['subject']}")
        print("  " + it["ai_output"]["body"].replace("\n", "\n  "))
        print(f"  Reviewer note: {it['ai_output']['reviewer_note']}")
        for c in it["checks"]:
            review += 0 if c["ok"] else 1
            print(f"  AI check: {'ok    ' if c['ok'] else 'REVIEW'} {c['label']}{(' - ' + c['detail']) if c.get('detail') else ''}")
        sent = it["ai_input"]
        blob = json.dumps(sent).lower()
        words, nums = sensitive_values(db, pid)
        check("queued as a pending communication", it["status"] == "pending" and it["kind"] == "communication")
        check("only minimal context sent to Claude (allowed fields)", set(sent) <= ALLOWED_KEYS)
        check("no last name, email, phone, address or notes sent", not any(w.lower() in blob for w in words))
        check("no bill amount sent", not any(n in blob for n in nums))
        check("drafting logged as AI", db.execute("SELECT COUNT(*) FROM activity_log WHERE project_id = ? AND action_type = 'AI' "
                                                   "AND action LIKE 'AI drafted a customer message%'", (pid,)).fetchone()[0] >= 1)

    if len(items) >= 3:
        print("\n" + "=" * 88 + "\nDecisions and audit trail\n" + "=" * 88)
        a, e, r = items[0], items[1], items[2]
        kind, outcome = approvals.decide(db, a["id"], "approve", note="Looks right.")
        db.commit()
        check(f"approve: recorded as {kind} ({outcome})", kind == "approved")
        edits = {"subject": "Two quick items for your project", "body": e["ai_output"]["body"] + "\n\nP.S. Reply to this email with any questions."}
        kind, _ = approvals.decide(db, e["id"], "approve", edits=edits, note="Added a P.S.")
        db.commit()
        check("edit: recorded as approved_with_edits", kind == "approved_with_edits")
        stored = approvals.get(db, e["id"])
        dec = stored["decisions"][-1]
        check("edit: original AI output unchanged", stored["ai_output"] == e["ai_output"] and dec["original_output"] == e["ai_output"])
        check("edit: human correction stored beside it", dec["final_output"]["subject"] == edits["subject"]
              and dec["changed_fields"] == ["subject", "body"])
        kind, _ = approvals.decide(db, r["id"], "reject", note="Tone is off.")
        db.commit()
        check("reject: recorded as rejected", kind == "rejected")
        try:
            approvals.decide(db, a["id"], "reject")
            check("a decided item can't be decided again", False)
        except approvals.DecisionError:
            check("a decided item can't be decided again", True)
        for sql, label in [("UPDATE approval_items SET ai_output = '{}' WHERE id = ?", "database blocks changing the original AI output"),
                           ("UPDATE approval_decisions SET decision = 'rejected' WHERE item_id = ?", "database blocks editing a decision"),
                           ("DELETE FROM approval_decisions WHERE item_id = ?", "database blocks deleting a decision")]:
            try:
                db.execute(sql, (e["id"],))
                check(label, False)
            except sqlite3.DatabaseError:
                check(label, True)
        db.rollback()
        human = db.execute("SELECT action FROM activity_log WHERE action_type = 'HUMAN' AND (action LIKE 'Customer message approved%' "
                           "OR action LIKE 'AI-drafted customer message rejected%')").fetchall()
        check("decisions logged as HUMAN (3 entries)", len(human) == 3)
        check("approvals say nothing was sent", all("not sent" in h["action"] or "rejected" in h["action"] for h in human))
        timestamps = db.execute("SELECT created_at, decided_by FROM approval_decisions").fetchall()
        check("each decision has a timestamp and a decider", all(t["created_at"] and t["decided_by"] for t in timestamps))
    db.close()

    if not args.no_save and api_errors == 0:
        print("\n" + "=" * 88 + "\nQueueing live drafts in your OpsPilot database\n" + "=" * 88)
        use_db(DEMO_DB)
        demo = database.connect()
        database.init_schema(demo)
        if database.is_empty(demo):
            seed.init_db()
        for name, purpose in LIVE:
            try:
                pid = project_id(demo, name)
            except TypeError:
                print(f"  Skipped {name}: not in your demo data")
                continue
            try:
                item_id, result = approvals.draft_communication(demo, pid, purpose)
                demo.commit()
                print(f"  Queued draft for {name} (item #{item_id}, response {result.response_id})")
            except ai_service.AIServiceError as err:
                api_errors += 1
                print(f"  Could not draft for {name}: {err.message}")
        demo.close()

    print("\n" + "=" * 88)
    print(f"Hard check failures: {hard}   AI checks to review: {review}   API errors: {api_errors}")
    return 1 if hard or api_errors else 0


if __name__ == "__main__":
    sys.exit(main())
