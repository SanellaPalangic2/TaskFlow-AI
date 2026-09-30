"""
AI Evaluation Lab.

Tests the REAL project-analysis call (ai_service.analyze_project, the same function the project page
uses) against synthetic test cases with known expected answers.

  1. Each synthetic case is a fictional project record. OpsPilot builds the context exactly as in
     production (the same rules engine findings, the same context builder) and sends it to Claude.
  2. The validated structured output is stored unchanged.
  3. Python compares three fields against the expected values:
       classification   detected_intent is one of the accepted intents
       information      each expected item appears in the model's missing_information or concerns,
                        and nothing on the "must not claim" list is reported missing
       action           suggested_department is one of the accepted teams
  4. Disagreements are flagged for a person, who can mark each result Correct, Partially correct
     or Incorrect.

The model never grades itself. Every metric is counted in Python from stored expected and actual values.

Cost controls: nothing runs on page load; a run is at most EVAL_MAX_CASES requests; one run at a time;
a cooldown between runs and a daily cap; each step processes one case and is idempotent.
"""
import hashlib
import json
import re
from datetime import datetime, timedelta

import config
from services import ai_service, rules_engine

MAX_CASES = min(10, max(1, int(getattr(config, "EVAL_MAX_CASES", 10))))
COOLDOWN_SECONDS = int(getattr(config, "EVAL_COOLDOWN_SECONDS", 60))
MAX_RUNS_PER_DAY = int(getattr(config, "EVAL_MAX_RUNS_PER_DAY", 10))
STALE_MINUTES = 10              # a "running" run with no progress for this long is treated as stopped
QUICK_CASES = 3
STOP_CODES = {"not_configured", "auth_failed", "quota_exceeded", "rate_limited", "unavailable"}  # stop, don't retry

VERDICTS = {"correct": "Correct", "partially_correct": "Partially correct", "incorrect": "Incorrect"}
RESULTS = {"passed": "Passed", "review": "Review required", "failed": "Failed", "pending": "Not run"}


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _loads(v, default=None):
    try:
        return json.loads(v) if v else default
    except (TypeError, ValueError):
        return default


# --- Synthetic test cases --------------------------------------------------------------------------
# All fictional. `expected.information` items pass when ANY of their phrases appears in the model's
# missing_information (item + why) or concerns (topic + detail). `must_not_claim` phrases must NOT
# appear as missing-information items (catches the model inventing gaps).

def _doc(t, status):
    return {"document_type": t, "status": status}


CASES = [
    {"key": "roof-worry", "title": "Roof worry before solar",
     "summary": "Customer wants solar but is worried the roof needs replacing. Two documents missing.",
     "project": {"first_name": "Avery", "last_name": "Test", "service_type": "Solar + Roofing", "monthly_electric_bill": 240,
                 "roof_age": 19, "current_stage": "Information Gathering", "project_status": "Human Review Required",
                 "priority": "High", "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "I want solar but I'm worried my roof may need to be replaced first. It's about 19 years old."},
     "documents": [_doc("Utility Bill", "Missing"), _doc("Roof Photos", "Missing")],
     "expected": {"intent": ["Concern or objection"],
                  "information": [{"label": "Utility bill", "any": ["utility bill", "electric bill"]},
                                  {"label": "Roof photos", "any": ["roof photo", "photos of the roof", "roof image"]},
                                  {"label": "Roof condition concern", "any": ["replac", "condition", "roof age", "old roof", "worried", "concern about the roof"]}],
                  "must_not_claim": [],
                  "departments": ["Project Review"],
                  "action_label": "Route the roof question to qualified staff"}},
    {"key": "ready-to-buy", "title": "Ready to move forward, file complete",
     "summary": "Everything is on file and the customer asks for next steps.",
     "project": {"first_name": "Blake", "last_name": "Test", "service_type": "Solar", "monthly_electric_bill": 180,
                 "roof_age": 6, "current_stage": "Assessment", "project_status": "Active", "priority": "Medium",
                 "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "We're ready to move forward. Everything you asked for is attached. What's the next step?"},
     "documents": [_doc("Utility Bill", "Verified")],
     "expected": {"intent": ["Purchase interest"], "information": [],
                  "must_not_claim": [{"label": "Utility bill (already verified)", "any": ["utility bill"]}],
                  "departments": ["Sales", "Operations", "Scheduling"],
                  "action_label": "Move the customer to the next step"}},
    {"key": "pricing", "title": "Battery pricing and rebates",
     "summary": "Customer asks for prices and rebates before scheduling.",
     "project": {"first_name": "Casey", "last_name": "Test", "service_type": "Battery Storage", "monthly_electric_bill": 150,
                 "roof_age": None, "current_stage": "New Lead", "project_status": "Needs Attention", "priority": "Medium",
                 "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "How much would a 13 kWh battery cost, and are there any rebates? Please send numbers before we schedule anything."},
     "documents": [_doc("Utility Bill", "Missing")],
     "expected": {"intent": ["Pricing question"],
                  "information": [{"label": "Utility bill", "any": ["utility bill", "electric bill"]}],
                  "must_not_claim": [{"label": "Roof information (not needed for batteries)", "any": ["roof age", "roof photo"]}],
                  "departments": ["Sales"], "action_label": "Sales answers the pricing question"}},
    {"key": "scheduling", "title": "Site visit scheduling request",
     "summary": "Customer proposes visit days; phone number missing.",
     "project": {"first_name": "Devon", "last_name": "Test", "service_type": "Solar + Battery", "monthly_electric_bill": 310,
                 "roof_age": 9, "current_stage": "Assessment", "project_status": "Needs Attention", "priority": "Medium",
                 "email": "on-file", "phone": "", "address": "on-file",
                 "notes": "Can someone come out for the site visit next Tuesday or Wednesday morning? Weekdays after 9 work best."},
     "documents": [_doc("Utility Bill", "Received")],
     "expected": {"intent": ["Scheduling request"],
                  "information": [{"label": "Phone number", "any": ["phone"]}],
                  "must_not_claim": [{"label": "Utility bill (already received)", "any": ["utility bill"]}],
                  "departments": ["Scheduling"], "action_label": "Schedule the site visit"}},
    {"key": "complaint", "title": "Frustrated customer, possible cancellation",
     "summary": "Third time sending the bill, no callback, mentions cancelling.",
     "project": {"first_name": "Emerson", "last_name": "Test", "service_type": "Solar", "monthly_electric_bill": 205,
                 "roof_age": 11, "current_stage": "Document Review", "project_status": "Needs Attention", "priority": "Medium",
                 "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "This is the third time I've sent my utility bill and nobody has called me back. I'm frustrated and thinking about cancelling."},
     "documents": [_doc("Utility Bill", "Requested")],
     "expected": {"intent": ["Complaint"],
                  "information": [{"label": "Utility bill", "any": ["utility bill", "electric bill"]},
                                  {"label": "Cancellation risk", "any": ["cancel", "frustrat", "callback", "call back", "follow-up", "follow up", "response"]}],
                  "must_not_claim": [], "departments": ["Customer Support"],
                  "action_label": "Customer Support calls the customer back"}},
    {"key": "vague", "title": "Very short, vague note",
     "summary": "Two-word note; roof information and photos missing.",
     "project": {"first_name": "Finley", "last_name": "Test", "service_type": "Roofing", "monthly_electric_bill": None,
                 "roof_age": None, "current_stage": "New Lead", "project_status": "Needs Attention", "priority": "Low",
                 "email": "on-file", "phone": "on-file", "address": "on-file", "notes": "Interested. Call me."},
     "documents": [_doc("Roof Photos", "Missing")],
     "expected": {"intent": ["Unclear", "Purchase interest", "Information request"],
                  "information": [{"label": "Roof age", "any": ["roof age", "age of the roof", "roof information"]},
                                  {"label": "Roof photos", "any": ["roof photo", "photos"]}],
                  "must_not_claim": [], "departments": ["Sales", "Customer Support", "Operations"],
                  "action_label": "Call the customer to understand the request"}},
    {"key": "backup-scope", "title": "Unclear battery backup scope",
     "summary": "Customer unsure whether they need whole-home or partial backup.",
     "project": {"first_name": "Gray", "last_name": "Test", "service_type": "Solar + Battery", "monthly_electric_bill": 275,
                 "roof_age": 8, "current_stage": "Information Gathering", "project_status": "Active", "priority": "Medium",
                 "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "We lose power a lot. Not sure if we need the whole house backed up or just the fridge and the well pump."},
     "documents": [_doc("Utility Bill", "Received")],
     "expected": {"intent": ["Information request", "Purchase interest", "Concern or objection"],
                  "information": [{"label": "Backup scope (whole home or essential loads)",
                                   "any": ["backup", "whole home", "whole-home", "whole house", "essential", "loads", "circuit"]}],
                  "must_not_claim": [{"label": "Utility bill (already received)", "any": ["utility bill"]}],
                  "departments": ["Sales", "Project Review", "Operations"],
                  "action_label": "Clarify what the battery needs to back up"}},
    {"key": "contact-missing", "title": "Referral with missing email and phone",
     "summary": "Referred lead; prefers mail; no email or phone on file.",
     "project": {"first_name": "Harper", "last_name": "Test", "service_type": "Solar", "monthly_electric_bill": 160,
                 "roof_age": 4, "current_stage": "New Lead", "project_status": "Needs Attention", "priority": "Medium",
                 "email": "", "phone": "", "address": "on-file",
                 "notes": "My neighbor referred me. Mail is the best way to reach me for now."},
     "documents": [_doc("Utility Bill", "Received")],
     "expected": {"intent": ["Purchase interest", "Information request"],
                  "information": [{"label": "Email", "any": ["email"]}, {"label": "Phone", "any": ["phone"]}],
                  "must_not_claim": [{"label": "Utility bill (already received)", "any": ["utility bill"]}],
                  "departments": ["Customer Support", "Sales"], "action_label": "Collect the missing contact details"}},
    {"key": "hoa", "title": "HOA approval paperwork",
     "summary": "Homeowners association must approve front-facing panels.",
     "project": {"first_name": "Indigo", "last_name": "Test", "service_type": "Solar", "monthly_electric_bill": 220,
                 "roof_age": 7, "current_stage": "Information Gathering", "project_status": "Active", "priority": "Medium",
                 "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "Our HOA needs to approve any panels on the front of the house. Can you help with the paperwork?"},
     "documents": [_doc("Utility Bill", "Received")],
     "expected": {"intent": ["Information request", "Concern or objection"],
                  "information": [{"label": "HOA approval", "any": ["hoa", "homeowners association", "homeowner association"]}],
                  "must_not_claim": [{"label": "Utility bill (already received)", "any": ["utility bill"]}],
                  "departments": ["Operations", "Project Review", "Customer Support"],
                  "action_label": "Help with the HOA approval"}},
    {"key": "safety-question", "title": "Roof safety question",
     "summary": "Soft spot and attic water stain; asks if panels are safe. Needs qualified staff.",
     "project": {"first_name": "Jordan", "last_name": "Test", "service_type": "Solar + Roofing", "monthly_electric_bill": 260,
                 "roof_age": 22, "current_stage": "Assessment", "project_status": "Human Review Required", "priority": "High",
                 "email": "on-file", "phone": "on-file", "address": "on-file",
                 "notes": "There's a soft spot near the chimney and I noticed a water stain in the attic. Is it safe to put panels up there?"},
     "documents": [_doc("Utility Bill", "Received"), _doc("Roof Photos", "Missing")],
     "expected": {"intent": ["Concern or objection", "Information request"],
                  "information": [{"label": "Roof damage (soft spot / water stain)", "any": ["soft spot", "water", "leak", "stain", "damage", "structural"]},
                                  {"label": "Roof photos", "any": ["roof photo", "photos"]}],
                  "must_not_claim": [], "departments": ["Project Review"],
                  "action_label": "Route to qualified staff; no safety judgment"}},
]
CASE_BY_KEY = {c["key"]: c for c in CASES}


def ensure_cases(db):
    """Keep the stored synthetic cases in step with the definitions above (insert or update by key)."""
    for i, c in enumerate(CASES, 1):
        definition = json.dumps({k: c[k] for k in ("project", "documents", "expected", "summary")}, sort_keys=True)
        row = db.execute("SELECT id, definition FROM eval_cases WHERE key = ?", (c["key"],)).fetchone()
        if row is None:
            db.execute("INSERT INTO eval_cases (key, title, position, definition, created_at) VALUES (?, ?, ?, ?, ?)",
                       (c["key"], c["title"], i, definition, _now()))
        elif row["definition"] != definition:
            db.execute("UPDATE eval_cases SET title = ?, position = ?, definition = ? WHERE id = ?",
                       (c["title"], i, definition, row["id"]))


def list_cases(db):
    out = []
    for r in db.execute("SELECT * FROM eval_cases ORDER BY position, id"):
        d = _loads(r["definition"], {})
        out.append({"id": r["id"], "key": r["key"], "title": r["title"], **d})
    return out


# --- Building the model input exactly as production does -------------------------------------------

def build_context(case):
    p = dict(case["project"])
    docs = [{"id": i + 1, **d} for i, d in enumerate(case["documents"])]
    findings = rules_engine.evaluate(p, docs, [])
    return ai_service.build_project_context(p, docs, [], findings), [f.message for f in findings if not f.resolved]


def prompt_version():
    """Fingerprint of the production prompt and schema, so run history shows when either changed."""
    schema = json.dumps(ai_service.ProjectAnalysis.model_json_schema(), sort_keys=True)
    return hashlib.sha256((ai_service.ANALYSIS_INSTRUCTIONS + schema).encode()).hexdigest()[:8]


# --- Deterministic comparison ------------------------------------------------------------------------

def _has(text, phrase):
    return re.search(r"(?<![a-z])" + re.escape(phrase.lower()), text.lower()) is not None


def compare(expected, actual):
    """Compare stored expected values with the stored model output. Pure Python, no AI."""
    missing_text = " | ".join(f"{m['item']} {m.get('why_it_matters', '')}" for m in actual.get("missing_information", []))
    missing_items = " | ".join(m["item"] for m in actual.get("missing_information", []))
    concern_text = " | ".join(f"{c['topic']} {c['detail']}" for c in actual.get("concerns", []))

    intent_ok = actual.get("detected_intent") in expected["intent"]
    items = []
    for it in expected["information"]:
        where = None
        hit = next((p for p in it["any"] if _has(missing_text, p)), None)
        if hit:
            where = "missing information"
        else:
            hit = next((p for p in it["any"] if _has(concern_text, p)), None)
            where = "concerns" if hit else None
        items.append({"label": it["label"], "kind": "expected", "ok": bool(hit), "matched": hit, "where": where})
    for it in expected.get("must_not_claim", []):
        hit = next((p for p in it["any"] if _has(missing_items, p)), None)
        items.append({"label": it["label"], "kind": "must_not", "ok": not hit, "matched": hit,
                      "where": "missing information" if hit else None})
    found = sum(1 for i in items if i["ok"])
    info_state = "pass" if found == len(items) else ("partial" if found else "fail")
    dept_ok = actual.get("suggested_department") in expected["departments"]

    checks = [
        {"key": "classification", "label": "Classification", "state": "pass" if intent_ok else "fail",
         "expected": " or ".join(expected["intent"]), "actual": actual.get("detected_intent")},
        {"key": "information", "label": "Important information", "state": info_state,
         "expected": f"{len(items)} item{'s' if len(items) != 1 else ''}", "actual": f"{found} of {len(items)} correct",
         "items": items},
        {"key": "action", "label": "Action category", "state": "pass" if dept_ok else "fail",
         "expected": " or ".join(expected["departments"]), "actual": actual.get("suggested_department")},
    ]
    not_passing = sum(1 for c in checks if c["state"] != "pass")
    result = "passed" if not_passing == 0 else ("review" if not_passing == 1 else "failed")
    return {"checks": checks, "result": result, "info_found": found, "info_total": len(items)}


# --- Runs -----------------------------------------------------------------------------------------

class EvalError(Exception):
    def __init__(self, code, message, status=409):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def _expire_stale(db):
    cutoff = (datetime.now() - timedelta(minutes=STALE_MINUTES)).isoformat(timespec="seconds")
    db.execute("UPDATE eval_runs SET status = 'stopped', finished_at = ?, stop_reason = 'No progress for "
               f"{STALE_MINUTES} minutes' WHERE status = 'running' AND last_activity_at < ?", (_now(), cutoff))


def guard(db):
    """Cost controls, checked on the server before any request is sent."""
    _expire_stale(db)
    if not ai_service.is_configured():
        raise EvalError("not_configured", "Claude is not configured. Add ANTHROPIC_API_KEY to .env and restart.", 503)
    if db.execute("SELECT 1 FROM eval_runs WHERE status = 'running'").fetchone():
        raise EvalError("run_in_progress", "An evaluation is already running. Wait for it to finish or stop it.")
    last = db.execute("SELECT MAX(started_at) FROM eval_runs").fetchone()[0]
    if last:
        wait = COOLDOWN_SECONDS - int((datetime.now() - datetime.fromisoformat(last)).total_seconds())
        if wait > 0:
            raise EvalError("cooldown", f"Runs are limited to one every {COOLDOWN_SECONDS} seconds. Try again in {wait} s.", 429)
    today = db.execute("SELECT COUNT(*) FROM eval_runs WHERE substr(started_at, 1, 10) = ?",
                       (datetime.now().date().isoformat(),)).fetchone()[0]
    if today >= MAX_RUNS_PER_DAY:
        raise EvalError("daily_limit", f"The daily limit of {MAX_RUNS_PER_DAY} evaluation runs has been reached "
                                       "(EVAL_MAX_RUNS_PER_DAY in .env).", 429)


def start_run(db, limit):
    ensure_cases(db)
    guard(db)
    cases = list_cases(db)
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = MAX_CASES
    limit = max(1, min(limit, MAX_CASES, len(cases)))
    plan = [c["id"] for c in cases[:limit]]
    now = _now()
    cur = db.execute("""INSERT INTO eval_runs (model, reasoning_effort, prompt_version, status, planned_cases, case_plan,
                                               started_at, last_activity_at, feature)
                        VALUES (?, ?, ?, 'running', ?, ?, ?, ?, 'project_analysis')""",
                     (config.AI_MODEL, None, prompt_version(), len(plan),
                      json.dumps(plan), now, now))
    return cur.lastrowid


def step(db, run_id):
    """Run the next case of a run: one real API request at most. Safe to call again (idempotent)."""
    run = db.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
    if run is None:
        raise EvalError("not_found", "That evaluation run no longer exists.", 404)
    if run["status"] != "running":
        return {"done": True, "result_id": None, "status": run["status"]}
    plan = _loads(run["case_plan"], [])
    done = {r[0] for r in db.execute("SELECT case_id FROM eval_results WHERE run_id = ?", (run_id,))}
    todo = [cid for cid in plan if cid not in done]
    if not todo:
        _finish(db, run_id, "completed")
        return {"done": True, "result_id": None, "status": "completed"}
    case_id = todo[0]
    row = db.execute("SELECT * FROM eval_cases WHERE id = ?", (case_id,)).fetchone()
    case = {"key": row["key"], "title": row["title"], **_loads(row["definition"], {})}
    context, findings = build_context(case)
    started = _now()
    try:
        res = ai_service.analyze_project(context)                       # the production call
        cmp = compare(case["expected"], res.data)
        rid = _store(db, run_id, row, case, context, findings, res.data, cmp, res, None)
    except ai_service.AIServiceError as err:
        cmp = {"checks": [], "result": "failed", "info_found": 0,
               "info_total": len(case["expected"]["information"]) + len(case["expected"].get("must_not_claim", []))}
        rid = _store(db, run_id, row, case, context, findings, None, cmp, None, err)
        if err.code in STOP_CODES:                                      # no point spending more requests
            _finish(db, run_id, "stopped", f"Stopped after an API error: {err.title}")
            return {"done": True, "result_id": rid, "status": "stopped", "error": err.to_dict()}
    db.execute("UPDATE eval_runs SET last_activity_at = ? WHERE id = ?", (_now(), run_id))
    remaining = len(todo) - 1
    if remaining == 0:
        _finish(db, run_id, "completed")
    return {"done": remaining == 0, "result_id": rid, "status": "completed" if remaining == 0 else "running",
            "completed": len(plan) - remaining, "planned": len(plan), "started": started}


def _store(db, run_id, row, case, context, findings, actual, cmp, res, err):
    usage = (res.usage if res else None) or {}
    cur = db.execute("""INSERT INTO eval_results (run_id, case_id, case_key, case_title, input_context, rule_findings, expected,
                                                  actual, comparison, auto_result, response_id, model, latency_ms,
                                                  input_tokens, output_tokens, error_code, error_message, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                     (run_id, row["id"], row["key"], row["title"], context, json.dumps(findings),
                      json.dumps(case["expected"]), json.dumps(actual) if actual is not None else None,
                      json.dumps(cmp), cmp["result"], res.response_id if res else None, res.model if res else None,
                      res.latency_ms if res else None, usage.get("input_tokens"), usage.get("output_tokens"),
                      err.code if err else None, err.message if err else None, _now()))
    return cur.lastrowid


def _finish(db, run_id, status, reason=None):
    db.execute("UPDATE eval_runs SET status = ?, finished_at = ?, stop_reason = COALESCE(?, stop_reason) WHERE id = ? "
               "AND status = 'running'", (status, _now(), reason, run_id))


def stop_run(db, run_id):
    _finish(db, run_id, "stopped", "Stopped by the reviewer")


# --- Human evaluation -------------------------------------------------------------------------------

def review(db, result_id, verdict, note=""):
    if verdict not in VERDICTS:
        raise EvalError("bad_request", "Choose Correct, Partially correct or Incorrect.", 400)
    if db.execute("SELECT 1 FROM eval_results WHERE id = ?", (result_id,)).fetchone() is None:
        raise EvalError("not_found", "That result no longer exists.", 404)
    note = (note or "").strip()[:500] if isinstance(note, str) else ""
    db.execute("INSERT INTO eval_reviews (result_id, verdict, note, reviewer, created_at) VALUES (?, ?, ?, ?, ?)",
               (result_id, verdict, note or None, config.DEMO_USER["name"], _now()))


# --- Reads and metrics ------------------------------------------------------------------------------

def _result(r, reviews):
    d = dict(r)
    for k in ("expected", "actual", "comparison", "rule_findings"):
        d[k] = _loads(d.get(k))
    d["reviews"] = reviews.get(r["id"], [])
    d["verdict"] = d["reviews"][-1]["verdict"] if d["reviews"] else None
    d["tokens"] = (d.get("input_tokens") or 0) + (d.get("output_tokens") or 0)
    return d


def results(db, run_id):
    rows = db.execute("SELECT * FROM eval_results WHERE run_id = ? ORDER BY id", (run_id,)).fetchall()
    ids = [r["id"] for r in rows]
    reviews = {}
    if ids:
        for rv in db.execute(f"SELECT * FROM eval_reviews WHERE result_id IN ({','.join('?' * len(ids))}) ORDER BY id", ids):
            reviews.setdefault(rv["result_id"], []).append(dict(rv))
    return [_result(r, reviews) for r in rows]


def get_result(db, result_id):
    r = db.execute("SELECT * FROM eval_results WHERE id = ?", (result_id,)).fetchone()
    if r is None:
        return None
    reviews = {result_id: [dict(x) for x in db.execute("SELECT * FROM eval_reviews WHERE result_id = ? ORDER BY id", (result_id,))]}
    return _result(r, reviews)


def _pct(n, d):
    return round(100 * n / d) if d else None


def metrics(res):
    """Every figure is counted from stored expected and actual values."""
    run = len(res)
    by = {k: 0 for k in ("passed", "review", "failed")}
    cls = act = info_found = info_total = errors = 0
    for r in res:
        by[r["auto_result"]] += 1
        info_found += r["comparison"]["info_found"]
        info_total += r["comparison"]["info_total"]
        if r["error_code"]:
            errors += 1
            continue
        checks = {c["key"]: c["state"] for c in r["comparison"]["checks"]}
        cls += checks.get("classification") == "pass"
        act += checks.get("action") == "pass"
    verdicts = {k: 0 for k in VERDICTS}
    for r in res:
        if r["verdict"]:
            verdicts[r["verdict"]] += 1
    needs_review = sum(1 for r in res if r["auto_result"] != "passed" and not r["verdict"])
    tokens = sum(r["tokens"] for r in res)
    latency = [r["latency_ms"] for r in res if r["latency_ms"]]
    agree = sum(1 for r in res if r["verdict"] and (r["verdict"] == "correct") == (r["auto_result"] == "passed"))
    reviewed = sum(verdicts.values())
    return {
        "cases": run, "errors": errors, "results": by,
        "classification": {"n": cls, "of": run, "pct": _pct(cls, run)},
        "information": {"n": info_found, "of": info_total, "pct": _pct(info_found, info_total)},
        "action": {"n": act, "of": run, "pct": _pct(act, run)},
        "pass_rate": {"n": by["passed"], "of": run, "pct": _pct(by["passed"], run)},
        "needs_review": needs_review, "failed": by["failed"],
        "verdicts": verdicts, "reviewed": reviewed, "not_evaluated": run - reviewed,
        "human_agreement": {"n": agree, "of": reviewed, "pct": _pct(agree, reviewed)},
        "tokens": tokens, "avg_latency_ms": round(sum(latency) / len(latency)) if latency else None,
    }


def runs(db, limit=20):
    _expire_stale(db)
    out = []
    for r in db.execute("SELECT * FROM eval_runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall():
        res = results(db, r["id"])
        out.append({**dict(r), "metrics": metrics(res)})
    return out


def get_run(db, run_id):
    r = db.execute("SELECT * FROM eval_runs WHERE id = ?", (run_id,)).fetchone()
    if r is None:
        return None
    res = results(db, run_id)
    return {**dict(r), "metrics": metrics(res), "results": res}


def compare_runs(current, previous):
    """Per-case change between two runs, matched by case key."""
    if not previous:
        return {}
    order = {"failed": 0, "review": 1, "passed": 2}
    prev = {r["case_key"]: r for r in previous["results"]}
    out = {}
    for r in current["results"]:
        p = prev.get(r["case_key"])
        if not p:
            continue
        delta = order[r["auto_result"]] - order[p["auto_result"]]
        out[r["case_key"]] = {"previous": p["auto_result"], "change": "improved" if delta > 0 else "regressed" if delta < 0 else "same",
                              "previous_intent": (p["actual"] or {}).get("detected_intent"),
                              "previous_department": (p["actual"] or {}).get("suggested_department")}
    return out


def shared_metrics(current, previous):
    """Metrics for both runs over the cases they have in common, so deltas compare like with like."""
    if not current or not previous:
        return None
    keys = {r["case_key"] for r in current["results"]} & {r["case_key"] for r in previous["results"]}
    if not keys:
        return {"shared": 0, "same_set": False, "current": None, "previous": None}
    same = len(keys) == len(current["results"]) == len(previous["results"])
    return {"shared": len(keys), "same_set": same,
            "current": metrics([r for r in current["results"] if r["case_key"] in keys]),
            "previous": metrics([r for r in previous["results"] if r["case_key"] in keys])}


def estimate(cases):
    """Rough size of a run for the confirmation dialog: characters / 4, labelled as an estimate."""
    chars = sum(len(ai_service.ANALYSIS_INSTRUCTIONS) + len(build_context(c)[0]) + 2500 for c in cases)  # + schema
    return {"requests": len(cases), "input_tokens": round(chars / 4, -2), "output_tokens": 700 * len(cases)}
