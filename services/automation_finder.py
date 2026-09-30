"""
Automation Opportunity Finder.

An employee describes a repetitive process in plain English. Claude breaks it
into steps and suggests where traditional automation, business rules, AI and
human judgment fit. Python then:

  * validates every field of the structured response,
  * applies a human-judgment policy: consequential decisions (approvals,
    money, eligibility, legal/safety calls, exceptions) are never left to
    automation or AI, whatever the model suggested,
  * removes invented efficiency claims (percentages, time or money savings),
  * builds the proposed workflow in a fixed order
    (Trigger → Automation → AI → Business rule → Human approval → Action),
  * calculates every metric by counting the validated step classifications.

Analyses are stored as drafts and only listed once a person saves them.
"""
import json
import re
from typing import List, Literal

from pydantic import BaseModel

from database import now_iso
from services import ai_service

MIN_DESCRIPTION, MAX_DESCRIPTION = 40, 4000
MAX_NAME = 80

CLASSES = {
    "traditional_automation": {"label": "Traditional automation", "short": "Automation", "icon": "zap", "css": "cls-automation",
                               "meaning": "A fixed, repeatable step software can do the same way every time."},
    "business_rule": {"label": "Business rule", "short": "Business rule", "icon": "cpu", "css": "cls-rule",
                      "meaning": "A yes/no check against clear criteria, written once as a rule."},
    "ai_candidate": {"label": "AI candidate", "short": "AI", "icon": "sparkle", "css": "cls-ai",
                     "meaning": "Reading or writing unstructured content, where AI can help and a person can check."},
    "human_decision": {"label": "Human decision", "short": "Human", "icon": "shield", "css": "cls-human",
                       "meaning": "A judgment call, approval or anything with real consequences. A person decides."},
    "keep_manual": {"label": "Keep manual", "short": "Manual", "icon": "user", "css": "cls-manual",
                    "meaning": "Relationship, physical or rare work where automating adds little."},
}
STAGES = [("trigger", "Trigger"), ("automation", "Automation"), ("ai", "AI"), ("business_rule", "Business rule"),
          ("human_approval", "Human approval"), ("action", "Action")]
STAGE_ORDER = {k: i for i, (k, _) in enumerate(STAGES)}
STAGE_LABEL = dict(STAGES)
CLASS_TO_STAGE = {"traditional_automation": "automation", "ai_candidate": "ai", "business_rule": "business_rule",
                  "human_decision": "human_approval", "keep_manual": "action"}

EXAMPLES = [
    {"key": "intake", "name": "Document intake", "icon": "file",
     "text": ("Customers email us documents like utility bills and roof photos. I download the attachments, figure out "
              "what each one is, rename the files using our naming format, enter the customer information into our "
              "system, check whether anything required is missing, and email the customer if we still need something. "
              "If a document looks unusual or hard to read, I ask my manager what to do.")},
    {"key": "followup", "name": "Customer follow-up", "icon": "mail",
     "text": ("Every morning I go through the list of customers who haven't replied in five days. I read our last "
              "conversation to remember where things stand, decide whether to follow up or give them more time, write a "
              "personal email or text, and log the follow-up in the project notes. If a customer seems upset or asks "
              "for a discount, I pass it to a manager.")},
    {"key": "handoff", "name": "Internal project handoff", "icon": "workflow",
     "text": ("When Sales closes a deal, they send Operations an email with the customer details. Operations copies the "
              "details into our project tracker, checks that the contract, utility bill and roof photos are attached, "
              "writes a summary of anything special the customer asked for, assigns a project coordinator based on "
              "region and workload, and schedules a kickoff call. The coordinator's manager signs off before the "
              "customer is contacted.")},
]

# Deterministic human-judgment policy. Applied after the AI responds, regardless of what it said.
CONSEQUENTIAL = [
    (r"\bapprov\w*|\bsign[- ]?off\b|\bsigns off\b|\bauthori[sz]\w*", "It is an approval or sign-off"),
    (r"\bdecid\w*|\bdecision\b|\bjudg(e?ment|e)\b", "It is a decision that needs judgment"),
    (r"\b(issu|grant|giv|offer|releas|process|waiv|apply|pay|send)\w*\b[^.]{0,30}\b(refund|discount|credit|payment|money|fee)s?\b"
     r"|\bpay(s|ing)? (the )?customer|\bprice (exception|change)s?", "It moves money or changes what a customer pays"),
    (r"\bdeny\b|\bdenial\b|\breject\w*|\beligib\w*|\bqualif(y|ies|ication)\b", "It decides eligibility or rejects a customer"),
    (r"\bhir(e|ing)\b|\bfir(e|ing)\b|\bterminat\w*|\bdisciplin\w*|\bperformance review", "It affects someone's job"),
    (r"\blegal\b|\bcontract (change|terms|exception)s?|\bcompliance\b|\bsafety\b", "It has legal, safety or compliance impact"),
    (r"\brefund\w*|\bdiscount\w*", "It moves money or changes what a customer pays"),
    (r"\b(escalat\w*|upset|angry|unhappy|complain\w*|unusual|exceptions?|ambiguous|dispute\w*)\b"
     r"|\b(pass|send|hand|refer|ask|forward)\w*\b[^.]{0,30}\b(manager|supervisor|lead)\b",
     "It handles an exception, an escalation or an unhappy customer"),
]
# Steps that only move or record information are not decisions, even if they mention a sensitive topic.
INTAKE_VERBS = re.compile(r"^(receive|collect|download|upload|log|record|save|store|file|rename|copy|enter|type|"
                          r"track|move|open|read|forward|attach|scan|sort)\b", re.I)
CUSTOMER_FACING = re.compile(r"\b(email|text|message|call|contact|notify|send|reply)\w*\b.*\b(customer|client)s?\b|"
                             r"\b(customer|client)s?\b.*\b(email|text|message|notify|send|reply)\w*", re.I)
CLAIM = re.compile(r"[^.!?\n]*(\d+\s?%|\bpercent\b|\d+\s?(x|times) faster|\bsav(e|es|ing|ings)\b[^.!?\n]*"
                   r"\b(\d+\s?(hours?|minutes?|days?|dollars?)|\$\s?\d+))[^.!?\n]*[.!?]?", re.I)


# --- Structured output schema ---------------------------------------------------------------

class Step(BaseModel):
    name: str
    description: str
    classification: Literal["traditional_automation", "business_rule", "ai_candidate", "human_decision", "keep_manual"]
    justification: str
    consequential: bool


class WorkflowStage(BaseModel):
    stage: Literal["trigger", "automation", "ai", "business_rule", "human_approval", "action"]
    title: str
    description: str
    step_numbers: List[int]


class Risk(BaseModel):
    title: str
    detail: str
    severity: Literal["low", "medium", "high"]


class ReviewPoint(BaseModel):
    point: str
    reason: str


class ProcessAnalysis(BaseModel):
    input_assessment: Literal["valid_process", "too_vague", "not_a_process"]
    clarifying_question: str
    process_name: str
    process_summary: str
    trigger: str
    steps: List[Step]
    proposed_workflow: List[WorkflowStage]
    risks: List[Risk]
    human_review_points: List[ReviewPoint]


INSTRUCTIONS = """
You help a business owner see where a repetitive process could use traditional automation, AI, or people.
The reader is not technical. Write in plain, short sentences. Avoid jargon (no "OCR", "RPA", "NLP", "API",
"LLM", "pipeline"); describe what happens instead.

First decide input_assessment:
- valid_process: a repeatable process with at least two steps.
- too_vague: it is a process but too short or unclear to break into steps. Ask one clarifying_question.
- not_a_process: not a business process at all. Explain briefly in clarifying_question.
For anything other than valid_process, return empty lists and short placeholder text for the other fields.

For a valid process:
- process_name: 2-5 words. process_summary: 2-3 sentences describing what the process achieves today.
- trigger: what starts the process, in a few words.
- steps: the process as it runs today, in order, 3-12 steps. Split combined actions into separate steps.
  Keep each step's name under 8 words and its description to one sentence.
- classification for each step:
    traditional_automation: fixed, repeatable, same every time (moving files, copying data, scheduling, reminders)
    business_rule: a clear yes/no check against known criteria (required fields present, deadline passed)
    ai_candidate: reading, sorting or writing unstructured content (identifying a document type, pulling details
      out of free text, summarizing, drafting a message) where a person can check the result
    human_decision: approvals, anything involving money, eligibility, legal, safety, exceptions, unhappy customers,
      sending customer-facing messages, or any judgment with real consequences
    keep_manual: relationship-building, physical work, or rare tasks where automation adds little
- consequential: true if a wrong result could affect a customer, money, a legal/safety obligation or an employee.
  Consequential decisions must be human_decision even if AI could technically do them. AI may prepare or draft;
  a person decides and approves.
- justification: one plain sentence explaining the classification.
- proposed_workflow: the improved process as stages, each with stage, title, one-sentence description and the
  step_numbers (1-based) it covers. Start with a trigger and end with an action. Include a human_approval stage
  before anything consequential or customer-facing happens.
- risks: 2-5 practical considerations (data quality, errors, customer trust, exceptions), each with severity.
- human_review_points: where a person must stay involved and why.

Never give percentages, time savings, cost savings or any other numbers about efficiency. Do not invent tools,
vendors or systems the description does not mention.
""".strip()


# --- Validation ---------------------------------------------------------------------------------

def _clean(text, limit, strip_claims=False):
    text = re.sub(r"\s+", " ", (text or "")).strip()
    removed = False
    if strip_claims:
        new = CLAIM.sub("", text).strip()
        removed = new != text
        text = re.sub(r"\s{2,}", " ", new)
    return ai_service.shorten(text, limit), removed


def _policy_reason(step_name, step_text):
    if INTAKE_VERBS.match(step_name.strip()):
        return None
    for pattern, reason in CONSEQUENTIAL:
        if re.search(pattern, step_text, re.I):
            return reason
    return None


def validate_and_enrich(parsed: ProcessAnalysis):
    """Turn the model's answer into the result OpsPilot shows. Raises AIServiceError if unusable."""
    if parsed.input_assessment != "valid_process":
        q, _ = _clean(parsed.clarifying_question, 300)
        return {"status": parsed.input_assessment, "clarifying_question": q or
                "Describe the steps someone takes, from what starts the work to what finishes it."}

    claims_removed = 0
    steps, adjustments = [], []
    for i, s in enumerate(parsed.steps[:15], 1):
        name, _ = _clean(s.name, 80)
        desc, r1 = _clean(s.description, 280, strip_claims=True)
        just, r2 = _clean(s.justification, 280, strip_claims=True)
        claims_removed += r1 + r2
        if not name:
            continue
        cls = s.classification
        if not just:  # e.g. the justification was only an efficiency claim and was removed
            just = f"{CLASSES[cls]['meaning']}"
        entry = {"number": len(steps) + 1, "name": name, "description": desc, "classification": cls,
                 "ai_classification": cls, "justification": just, "ai_justification": just,
                 "consequential": bool(s.consequential),
                 "adjusted": False, "adjust_reason": None}
        if cls not in ("human_decision", "keep_manual"):
            reason = "The AI marked it as consequential" if s.consequential else _policy_reason(name, f"{name}. {desc}")
            if reason:
                entry.update(classification="human_decision", adjusted=True,
                             adjust_reason=f"{reason}, so a person should decide.",
                             justification=f"{reason}, so a person decides.")
                adjustments.append({"step": entry["number"], "name": name, "from": cls, "to": "human_decision",
                                    "reason": entry["adjust_reason"]})
        steps.append(entry)

    if len(steps) < 2:  # not enough detail to analyze; ask for more rather than showing a thin result
        return {"status": "too_vague", "clarifying_question":
                "Describe the individual steps: what starts the work, what you do, what you check, who decides, "
                "and how it ends."}

    summary, r = _clean(parsed.process_summary, 600, strip_claims=True)
    claims_removed += r
    risks = []
    for x in parsed.risks[:6]:
        title, _ = _clean(x.title, 80)
        detail, r = _clean(x.detail, 280, strip_claims=True)
        claims_removed += r
        if title:
            risks.append({"title": title, "detail": detail, "severity": x.severity})
    review = []
    for x in parsed.human_review_points[:8]:
        point, _ = _clean(x.point, 120)
        reason, _ = _clean(x.reason, 240)
        if point:
            review.append({"point": point, "reason": reason, "source": "ai"})

    # Policy: every human-decision step is a review point, and customer-facing sends need approval.
    covered = " ".join(p["point"].lower() for p in review)
    for s in steps:
        if s["classification"] == "human_decision" and s["name"].lower() not in covered:
            review.append({"point": s["name"], "reason": s["adjust_reason"] or s["justification"],
                           "source": "policy" if s["adjusted"] else "ai"})
    message_approved = any(s["classification"] == "human_decision" and
                           re.search(r"message|email|reply|communicat|text|letter", f"{s['name']} {s['description']}", re.I)
                           for s in steps)
    for s in steps:
        if CUSTOMER_FACING.search(f"{s['name']} {s['description']}") and s["classification"] in (
                "traditional_automation", "ai_candidate") and not message_approved:
            review.append({"point": "Approve customer messages before they are sent",
                           "reason": f"Step {s['number']} ({s['name']}) reaches the customer.", "source": "policy"})
            break

    return {"status": "valid_process",
            "process_name": _clean(parsed.process_name, MAX_NAME)[0] or "Untitled process",
            "summary": summary, "trigger": _clean(parsed.trigger, 120)[0],
            "steps": steps, "adjustments": adjustments, "claims_removed": claims_removed,
            "proposed": build_proposed_workflow(parsed.proposed_workflow, steps, _clean(parsed.trigger, 120)[0]),
            "risks": risks, "review_points": review, "metrics": compute_metrics(steps)}


def build_proposed_workflow(stages, steps, trigger):
    """Validated, ordered workflow. Step references must exist; a human approval stage is required
    whenever a human decision exists. Stages are ordered Trigger → … → Action."""
    by_num = {s["number"]: s for s in steps}
    out, moved = [], []
    for i, st in enumerate(stages[:12]):
        title, _ = _clean(st.title, 80)
        desc, _ = _clean(st.description, 240, strip_claims=True)
        refs = sorted({n for n in st.step_numbers if n in by_num})
        if not title:
            continue
        stage, note = st.stage, None
        human_refs = [n for n in refs if by_num[n]["classification"] == "human_decision"]
        if stage != "human_approval" and human_refs:
            if len(human_refs) == len(refs):   # the whole stage is a human decision
                stage, title = "human_approval", f"Person decides: {title[0].lower() + title[1:]}"
                note = "Changed to human approval by OpsPilot's policy."
            else:                              # split the decision out of an AI/automation stage
                refs = [n for n in refs if n not in human_refs]
                moved += human_refs
                note = "Decision steps moved to human approval by OpsPilot's policy."
        out.append({"stage": stage, "label": STAGE_LABEL[stage], "title": title, "description": desc,
                    "steps": refs, "order": i, "added_by_policy": False, "note": note})

    human_steps = [s["number"] for s in steps if s["classification"] == "human_decision"]
    if moved:
        approval = next((x for x in out if x["stage"] == "human_approval"), None)
        if approval:
            approval["steps"] = sorted(set(approval["steps"]) | set(moved))
        else:
            first = max(x["order"] for x in out if x["note"] and x["note"].startswith("Decision steps"))
            out.append({"stage": "human_approval", "label": "Human approval", "title": "A person reviews and decides",
                        "description": "Covers " + ", ".join(by_num[n]["name"].lower() for n in sorted(set(moved))) + ".",
                        "steps": sorted(set(moved)), "order": first + 0.5, "added_by_policy": True, "note": None})
    have = {x["stage"] for x in out}
    if not any(x["stage"] == "trigger" for x in out):
        out.append({"stage": "trigger", "label": "Trigger", "title": trigger or "Work arrives", "description": "",
                    "steps": [], "order": -1, "added_by_policy": True, "note": None})
    if human_steps and "human_approval" not in have:
        out.append({"stage": "human_approval", "label": "Human approval", "title": "A person reviews and decides",
                    "description": "Covers " + ", ".join(by_num[n]["name"].lower() for n in human_steps) + ".",
                    "steps": human_steps, "order": 99, "added_by_policy": True, "note": None})
    if not any(x["stage"] == "action" for x in out):
        out.append({"stage": "action", "label": "Action", "title": "Complete the work",
                    "description": "The approved result is carried out.", "steps": [], "order": 100,
                    "added_by_policy": True, "note": None})
    # Always shown in the same order a business owner can follow:
    # Trigger → Automation → AI → Business rule → Human approval → Action (model order breaks ties).
    def key(x):
        return (STAGE_ORDER[x["stage"]], x["order"])
    return sorted(out, key=key)


def compute_metrics(steps):
    """Every number shown on the page comes from here: counts of validated step classifications."""
    count = {k: sum(1 for s in steps if s["classification"] == k) for k in CLASSES}
    auto = count["traditional_automation"] + count["business_rule"]
    return {
        "total": len(steps),
        "automatable": auto,
        "ai": count["ai_candidate"],
        "human": count["human_decision"],
        "manual": count["keep_manual"],
        "by_class": count,
        "adjusted": sum(1 for s in steps if s["adjusted"]),
        "explain": {
            "total": f"Number of steps in the process as described ({len(steps)}).",
            "automatable": (f"Steps classified Traditional automation ({count['traditional_automation']}) "
                            f"+ Business rule ({count['business_rule']})."),
            "ai": f"Steps classified AI candidate ({count['ai_candidate']}). A person can check each result.",
            "human": (f"Steps classified Human decision ({count['human_decision']})"
                      + (f", including {sum(1 for s in steps if s['adjusted'])} moved there by OpsPilot's policy."
                         if any(s["adjusted"] for s in steps) else ".")),
        },
    }


# --- Orchestration ---------------------------------------------------------------------------------

def validate_input(description, process_name=""):
    description = description.strip() if isinstance(description, str) else ""
    process_name = process_name.strip()[:MAX_NAME] if isinstance(process_name, str) else ""
    if len(description) < MIN_DESCRIPTION:
        raise ValueError(f"Describe the process in a bit more detail (at least {MIN_DESCRIPTION} characters).")
    if len(description) > MAX_DESCRIPTION:
        raise ValueError(f"Keep the description under {MAX_DESCRIPTION:,} characters.")
    return description, process_name


def analyze(db, description, process_name=""):
    """Call Claude, validate, store as a draft. Raises ValueError or AIServiceError."""
    description, process_name = validate_input(description, process_name)
    user = f"Process name given by the employee: {process_name or '(none)'}\n\nProcess description:\n{description}"
    parsed, result = ai_service._structured_request("automation_finder", INSTRUCTIONS, user, ProcessAnalysis)
    data = validate_and_enrich(parsed)
    if data["status"] != "valid_process":
        return None, data, result
    if process_name:
        data["process_name"] = process_name
    m = data["metrics"]
    cur = db.execute(
        """INSERT INTO automation_analyses (process_name, original_description, steps, automation_opportunities,
               ai_opportunities, human_decisions, total_steps, analysis, is_saved, source, model, response_id,
               latency_ms, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 'live', ?, ?, ?, ?)""",
        (data["process_name"], description, json.dumps(data["steps"]), m["automatable"], m["ai"], m["human"],
         m["total"], json.dumps({k: v for k, v in data.items() if k != "steps"}), result.model, result.response_id,
         result.latency_ms, now_iso()))
    db.execute("DELETE FROM automation_analyses WHERE is_saved = 0 AND created_at < DATETIME('now', 'localtime', '-2 days')")
    return cur.lastrowid, data, result


def save(db, analysis_id, process_name=None):
    row = db.execute("SELECT * FROM automation_analyses WHERE id = ?", (analysis_id,)).fetchone()
    if row is None:
        raise LookupError("That analysis no longer exists. Analyze the process again.")
    name = (process_name.strip()[:MAX_NAME] if isinstance(process_name, str) else "") or row["process_name"]
    db.execute("UPDATE automation_analyses SET is_saved = 1, process_name = ?, saved_at = ? WHERE id = ?",
               (name, now_iso(), analysis_id))
    return name, bool(row["is_saved"])


def load(db, analysis_id):
    row = db.execute("SELECT * FROM automation_analyses WHERE id = ?", (analysis_id,)).fetchone()
    if row is None:
        return None
    data = json.loads(row["analysis"])
    steps = json.loads(row["steps"])
    data.update(steps=steps, process_name=row["process_name"])
    data["metrics"] = compute_metrics(steps)  # always recomputed from the stored steps
    return {"id": row["id"], "is_saved": bool(row["is_saved"]), "created_at": row["created_at"],
            "saved_at": row["saved_at"], "description": row["original_description"], "model": row["model"],
            "response_id": row["response_id"], "latency_ms": row["latency_ms"], "source": row["source"], **data}


def list_saved(db, limit=20):
    return db.execute("""SELECT id, process_name, total_steps, automation_opportunities, ai_opportunities,
                                human_decisions, saved_at, created_at FROM automation_analyses
                         WHERE is_saved = 1 ORDER BY saved_at DESC LIMIT ?""", (limit,)).fetchall()
