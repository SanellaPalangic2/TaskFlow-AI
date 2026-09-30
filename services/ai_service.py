"""
AI service — the only module that talks to the AI provider (Anthropic's Claude).

Design:
  * The API key is read from the ANTHROPIC_API_KEY environment variable (loaded
    from .env by config.py, or set in Render's Environment settings). It is never
    logged, stored, or returned.
  * Every call uses the Messages API with structured output: the answer must be
    returned through a "submit" tool whose input schema is generated from a Pydantic
    model. Pydantic then validates it, and a second, explicit validation pass runs
    before anything is used.
  * Every failure is converted into AIServiceError with a user-safe message.
    There is no fallback to canned/fake AI output.
  * This module returns data only. It never writes to the database; the
    workflow layer stores results as *pending suggestions* for a person to review.
"""
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import List, Literal

from pydantic import BaseModel, ValidationError

import config

log = logging.getLogger("opspilot.ai")

PLACEHOLDER_KEYS = {"", "your_api_key_here"}

INTENTS = ["Purchase interest", "Information request", "Concern or objection", "Scheduling request",
           "Pricing question", "Complaint", "Unclear"]


# --- Structured output schemas ---------------------------------------------------
# These are sent to Claude as the JSON schema of the answer tool, so the model
# must return exactly these fields (Pydantic rejects anything else). Every field is then re-checked in
# validate_analysis() before anything is stored or shown.

Department = Literal["Sales", "Customer Support", "Operations", "Project Review", "Scheduling"]


class MissingItem(BaseModel):
    item: str
    category: Literal["contact", "document", "roof", "project_detail", "customer_clarification"]
    why_it_matters: str


class Concern(BaseModel):
    topic: str
    detail: str
    severity: Literal["low", "medium", "high"]
    source: Literal["customer_notes", "project_data", "business_rules"]


class SuggestedTask(BaseModel):
    title: str
    category: Literal["request_document", "collect_contact_info", "collect_roof_info", "review_roof_info",
                      "clarify_request", "schedule_follow_up", "other"]
    department: Department
    priority: Literal["High", "Medium", "Low"]
    reason: str


class ProjectAnalysis(BaseModel):
    summary: str
    detected_intent: Literal["Purchase interest", "Information request", "Concern or objection",
                             "Scheduling request", "Pricing question", "Complaint", "Unclear"]
    intent_detail: str
    information_status: Literal["Complete", "Needs clarification", "Incomplete"]
    missing_information: List[MissingItem]
    concerns: List[Concern]
    recommended_action: str
    recommendation_why: List[str]
    suggested_department: Department
    suggested_tasks: List[SuggestedTask]
    reasoning: str
    confidence: float
    requires_human_review: bool
    human_review_reason: str


# --- Errors and results --------------------------------------------------------------

class AIServiceError(Exception):
    """A failure the UI can show as-is. `code` is stable for tests and logs."""

    def __init__(self, code, title, message, http_status=503):
        super().__init__(f"{code}: {message}")
        self.code, self.title, self.message, self.http_status = code, title, message, http_status

    def to_dict(self):
        return {"code": self.code, "title": self.title, "message": self.message}


@dataclass
class AIResult:
    data: dict
    model: str
    response_id: str
    latency_ms: int
    usage: dict


# --- Client --------------------------------------------------------------------------

_client = None
_client_key_fingerprint = None


def _api_key():
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip()


def is_configured():
    """True when a non-placeholder key is present. Never exposes the key itself."""
    return _api_key() not in PLACEHOLDER_KEYS


def _get_client():
    global _client, _client_key_fingerprint
    key = _api_key()
    if key in PLACEHOLDER_KEYS:
        raise AIServiceError(
            "not_configured", "AI is not configured",
            "No Anthropic API key was found. Set ANTHROPIC_API_KEY (in .env, or in Render's Environment "
            "settings) and restart the app.", 503)
    try:
        import anthropic  # noqa: F401  (imported lazily so the app still runs without the SDK)
    except ImportError:
        raise AIServiceError(
            "sdk_missing", "AI library not installed",
            "The Anthropic Python SDK is not installed. Run: pip install -r requirements.txt", 503)
    fingerprint = hash(key)  # detect a changed key without keeping a second copy of it
    if _client is None or fingerprint != _client_key_fingerprint:
        from anthropic import Anthropic
        _client = Anthropic(api_key=key, timeout=config.AI_TIMEOUT_SECONDS, max_retries=config.AI_MAX_RETRIES)
        _client_key_fingerprint = fingerprint
    return _client


def _error_text(exc):
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        return str(err.get("message", "")).lower()
    return str(getattr(exc, "message", "") or "").lower()


def _translate_error(exc):
    """Map SDK exceptions to user-safe AIServiceError instances."""
    import anthropic

    if isinstance(exc, anthropic.AuthenticationError):
        return AIServiceError("auth_failed", "AI credentials were rejected",
                              "Anthropic did not accept the configured API key. Check ANTHROPIC_API_KEY "
                              "(in .env, or in Render's Environment settings).", 502)
    if isinstance(exc, anthropic.PermissionDeniedError):
        return AIServiceError("permission_denied", "AI access denied",
                              "This API key does not have access to the requested model.", 502)
    if isinstance(exc, anthropic.RateLimitError):
        return AIServiceError("rate_limited", "AI is busy",
                              "Anthropic is rate-limiting requests right now. Wait a few seconds and try again.", 503)
    if isinstance(exc, anthropic.APITimeoutError):
        return AIServiceError("timeout", "AI request timed out",
                              f"Claude did not respond within {int(config.AI_TIMEOUT_SECONDS)} seconds. Try again.",
                              504)
    if isinstance(exc, anthropic.APIConnectionError):
        return AIServiceError("unavailable", "AI service unreachable",
                              "Could not connect to Anthropic. Check the network connection and try again.", 503)
    if isinstance(exc, anthropic.BadRequestError) and "credit balance" in _error_text(exc):
        return AIServiceError("quota_exceeded", "AI usage limit reached",
                              "The Anthropic account has no remaining credit. Add credit under Billing in the "
                              "Claude Console.", 503)
    if isinstance(exc, (anthropic.NotFoundError, anthropic.BadRequestError)):
        return AIServiceError("bad_request", "AI request was rejected",
                              f"Anthropic rejected the request (model '{config.AI_MODEL}'). "
                              "Check ANTHROPIC_MODEL.", 502)
    if isinstance(exc, anthropic.InternalServerError) or getattr(exc, "status_code", None) == 529:
        return AIServiceError("unavailable", "AI service temporarily unavailable",
                              "Anthropic returned a server error or is overloaded. Try again in a moment.", 503)
    if isinstance(exc, anthropic.APIStatusError):
        return AIServiceError("unavailable", "AI service error",
                              f"Anthropic returned HTTP {exc.status_code}. Try again in a moment.", 503)
    if isinstance(exc, (ValidationError, json.JSONDecodeError)):
        return AIServiceError("invalid_output", "AI returned an unexpected format",
                              "The AI response did not match the required structure, so it was not used.", 502)
    return AIServiceError("unexpected", "AI request failed",
                          "Something went wrong while contacting the AI service.", 500)


def _call(client, feature, **kwargs):
    """Make one Messages API call, converting every failure to AIServiceError."""
    try:
        return client.messages.create(model=config.AI_MODEL, **kwargs)
    except AIServiceError:
        raise
    except Exception as exc:  # SDK and network errors
        err = _translate_error(exc)
        log.warning("AI request failed [%s] feature=%s type=%s", err.code, feature, type(exc).__name__)
        raise err from None


# --- Structured output through an "answer" tool ------------------------------------------
# The model is given one tool whose input schema IS the answer schema, and is required to
# call it. Its arguments are then validated by Pydantic, so free text is never used.

def _inline_refs(node, defs, field_map=False):
    """Replace $ref pointers with their definitions and drop Pydantic's "title" labels.
    `field_map` marks a "properties" object, whose keys are field names (a field may be called "title")."""
    if isinstance(node, dict):
        if not field_map and "$ref" in node:
            return _inline_refs(defs[node["$ref"].split("/")[-1]], defs)
        return {k: _inline_refs(v, defs, field_map=(k == "properties" and not field_map))
                for k, v in node.items() if field_map or k not in ("$defs", "title")}
    if isinstance(node, list):
        return [_inline_refs(v, defs) for v in node]
    return node


def _answer_tool(schema):
    json_schema = schema.model_json_schema()
    return {"name": f"submit_{schema.__name__}",
            "description": "Return your final answer by calling this tool exactly once. Fill in every field.",
            "input_schema": _inline_refs(json_schema, json_schema.get("$defs", {}))}


def _parse_answer(data, tool):
    """Validate the answer tool's arguments. Nested lists/objects sent as JSON text are decoded first."""
    if not isinstance(data, dict):
        raise AIServiceError("invalid_output", "AI returned an unexpected format",
                             "The AI response did not match the required structure, so it was not used.", 502)
    props = tool["input_schema"].get("properties", {})
    fixed = dict(data)
    for key, value in data.items():
        if isinstance(value, str) and props.get(key, {}).get("type") in ("array", "object"):
            try:
                fixed[key] = json.loads(value)
            except ValueError:
                pass
    return fixed


def _check_stop(response):
    """Checks shared by every call: not refused, not cut off."""
    reason = getattr(response, "stop_reason", None)
    if reason == "refusal":
        raise AIServiceError("refused", "AI declined this request", "The model declined to answer this request.", 422)
    if reason in ("max_tokens", "model_context_window_exceeded"):
        raise AIServiceError("incomplete", "AI response was incomplete",
                             f"The AI response stopped early ({reason}). Try again.", 502)


def _final_output(response, schema, tool):
    _check_stop(response)
    block = next((b for b in (response.content or [])
                  if getattr(b, "type", None) == "tool_use" and b.name == tool["name"]), None)
    if block is None:
        raise AIServiceError("invalid_output", "AI returned an unexpected format",
                             "The AI response was empty or unstructured, so it was not used.", 502)
    try:
        return schema.model_validate(_parse_answer(block.input, tool))
    except ValidationError:
        raise AIServiceError("invalid_output", "AI returned an unexpected format",
                             "The AI response did not match the required structure, so it was not used.", 502) from None


def _usage(responses):
    total = {"input_tokens": 0, "output_tokens": 0}
    for r in responses:
        u = getattr(r, "usage", None)
        if u:
            total["input_tokens"] += getattr(u, "input_tokens", 0) or 0
            total["output_tokens"] += getattr(u, "output_tokens", 0) or 0
    return total


def _structured_request(feature, instructions, user_input, schema, max_tokens=6000):
    """One reusable call: send a prompt, get back a validated Pydantic object + provenance."""
    client = _get_client()
    tool = _answer_tool(schema)
    started = time.perf_counter()
    response = _call(client, feature, max_tokens=max_tokens, system=instructions,
                     messages=[{"role": "user", "content": user_input}],
                     tools=[tool], tool_choice={"type": "tool", "name": tool["name"]})
    latency_ms = int((time.perf_counter() - started) * 1000)
    parsed = _final_output(response, schema, tool)
    log.info("AI request ok feature=%s model=%s response_id=%s latency_ms=%s",
             feature, response.model, response.id, latency_ms)
    return parsed, AIResult(data={}, model=response.model, response_id=response.id,
                            latency_ms=latency_ms, usage=_usage([response]))


def _as_anthropic_tool(tool):
    """Tools are declared once, in a provider-neutral function format: name, description, parameters."""
    return {"name": tool["name"], "description": tool["description"], "input_schema": tool["parameters"]}


def _assistant_blocks(response):
    blocks = []
    for b in response.content or []:
        if b.type == "text" and b.text:
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


def run_tool_loop(feature, instructions, input_items, tools, execute_tool, schema, max_rounds=4):
    """Let the model call ONLY the given function tools, run them through `execute_tool`,
    and return its final structured answer.

    `execute_tool(name, arguments_json)` is supplied by the caller. It validates the
    arguments and runs a predefined, read-only query; the model never sees or sends SQL.
    Returns (parsed_answer, AIResult, tool_calls).
    """
    client = _get_client()
    answer = _answer_tool(schema)
    all_tools = [_as_anthropic_tool(t) for t in tools] + [answer]
    system = (f"{instructions}\n\nHow to reply: the data tools only look things up. Always give your final reply "
              f"by calling `{answer['name']}` exactly once, after any data tools you need.")
    messages = [{"role": i["role"], "content": i["content"]} for i in input_items]
    started = time.perf_counter()
    responses, tool_calls = [], []

    for round_no in range(max_rounds + 1):
        # After the last allowed round of lookups, the model must answer with what it has.
        choice = {"type": "tool", "name": answer["name"]} if round_no == max_rounds else {"type": "any"}
        response = _call(client, feature, max_tokens=3000, system=system, messages=messages,
                         tools=all_tools, tool_choice=choice)
        responses.append(response)
        _check_stop(response)
        uses = [b for b in (response.content or []) if getattr(b, "type", None) == "tool_use"]
        lookups = [b for b in uses if b.name != answer["name"]]
        if not lookups:
            break
        results = []
        for b in uses:
            if b.name == answer["name"]:  # an answer written before the lookups returned is discarded
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": "Not recorded. Answer again once the data tool results are in."})
                continue
            arguments = json.dumps(b.input if isinstance(b.input, dict) else {})
            result = execute_tool(b.name, arguments)
            tool_calls.append({"name": b.name, "arguments": arguments, "result": result})
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(result, default=str)})
        messages += [{"role": "assistant", "content": _assistant_blocks(response)},
                     {"role": "user", "content": results}]

    parsed = _final_output(response, schema, answer)
    latency_ms = int((time.perf_counter() - started) * 1000)
    log.info("AI tool loop ok feature=%s model=%s rounds=%s tools=%s latency_ms=%s", feature, response.model,
             len(responses), [c["name"] for c in tool_calls], latency_ms)
    result = AIResult(data={}, model=response.model, response_id=response.id, latency_ms=latency_ms,
                      usage=_usage(responses))
    result.response_ids = [r.id for r in responses]
    return parsed, result, tool_calls


# --- Output validation (second pass, after the schema) -------------------------------

def shorten(text, limit):
    """Trim to `limit` characters at a word boundary, with an ellipsis, instead of cutting mid-word."""
    if len(text) <= limit:
        return text
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space >= limit - 25:
        cut = cut[:space]
    return cut.rstrip(" ,;:.-") + "…"


def _clean(text, field, max_len, required=True):
    text = (text or "").strip()
    if required and not text:
        raise AIServiceError("invalid_output", "AI returned an incomplete answer",
                             f"The AI response was missing '{field}', so it was not used.", 502)
    return shorten(text, max_len)


def validate_analysis(parsed: ProjectAnalysis) -> dict:
    """Second validation pass: ranges, required text, sensible list sizes, trimmed lengths."""
    if not 0.0 <= parsed.confidence <= 1.0:
        raise AIServiceError("invalid_output", "AI returned an invalid confidence score",
                             "Confidence must be between 0 and 1, so the response was not used.", 502)
    why = [w.strip()[:220] for w in parsed.recommendation_why if w and w.strip()][:4]
    if not why:
        raise AIServiceError("invalid_output", "AI returned an incomplete answer",
                             "The AI response did not explain its recommendation, so it was not used.", 502)
    seen, missing = set(), []
    for m in parsed.missing_information[:8]:
        item = _clean(m.item, "missing item", 90)
        if item.lower() in seen:
            continue
        seen.add(item.lower())
        missing.append({"item": item, "category": m.category,
                        "why_it_matters": _clean(m.why_it_matters, "why_it_matters", 220, required=False)})
    concerns = [{"topic": _clean(c.topic, "concern topic", 80),
                 "detail": _clean(c.detail, "concern detail", 300),
                 "severity": c.severity, "source": c.source} for c in parsed.concerns[:6]]
    tasks = [{"title": _clean(t.title, "task title", 120), "category": t.category,
              "department": t.department, "priority": t.priority,
              "reason": _clean(t.reason, "task reason", 220, required=False)} for t in parsed.suggested_tasks[:5]]
    return {
        "summary": _clean(parsed.summary, "summary", 700),
        "detected_intent": parsed.detected_intent,
        "intent_detail": _clean(parsed.intent_detail, "intent_detail", 160, required=False),
        "information_status": parsed.information_status,
        "missing_information": missing,
        "concerns": concerns,
        "recommended_action": _clean(parsed.recommended_action, "recommended_action", 200),
        "recommendation_why": why,
        "suggested_department": parsed.suggested_department,
        "suggested_tasks": tasks,
        "reasoning": _clean(parsed.reasoning, "reasoning", 700),
        "confidence": round(float(parsed.confidence), 2),
        "requires_human_review": bool(parsed.requires_human_review),
        "human_review_reason": _clean(parsed.human_review_reason, "human_review_reason", 300, required=False),
    }


# --- Public functions ---------------------------------------------------------------

GUARDRAILS = """
Boundaries (always follow):
- You support an operations team. You do NOT make engineering, structural, safety, legal, financial,
  lending, regulatory, permitting or installation decisions. When those topics come up, recommend
  routing to appropriate qualified staff instead of giving a judgment.
- Only use facts present in the project data. Do not invent numbers, dates, prices or commitments.
- Your output is a suggestion for a human reviewer. It will not change the project automatically.
""".strip()

ANALYSIS_INSTRUCTIONS = f"""
You are OpsPilot, an assistant inside a residential energy services company's internal operations tool.
Read the project record (service, electric bill, roof information, documents, existing tasks, customer notes,
workflow stage and business-rule findings) and help an employee decide what should happen next.

The record already lists what deterministic business rules found. Treat those as facts; do not contradict them.
Your job is the part rules can't do: interpret the customer's own words, summarize, spot contextual concerns,
notice anything that needs clarifying, and suggest a sensible next step.

Return:
- summary: 2-3 plain sentences an employee can read in 10 seconds.
- detected_intent: the customer's main intent, inferred from their notes. Use "Unclear" if the notes are vague.
- intent_detail: one short phrase describing what the customer wants (under 15 words).
- information_status: Complete (nothing needed), Needs clarification (records complete but the customer's
  request is ambiguous) or Incomplete (required information is missing).
- missing_information: items still needed. Include rule-detected gaps, plus any clarification the notes call for.
- concerns: contextual concerns (0-5), each with severity and source.
- recommended_action: one concrete operational next step, starting with a verb, under 20 words.
- recommendation_why: 2-4 short bullet facts from the record that justify the action (e.g. "Utility bill not on file").
  State facts only; no step-by-step thinking.
- suggested_department: the team best placed to own the next step.
- suggested_tasks: 0-4 follow-up tasks. Pick the closest category. Deterministic code decides whether they are created.
- reasoning: a concise, user-facing justification (2-3 sentences) grounded in the record. Not internal deliberation.
- confidence: 0.0-1.0, lower when information is missing or the notes are ambiguous.
- requires_human_review: true if anything is ambiguous, customer-facing, or outside operations scope.
- human_review_reason: short reason, or an empty string.

{GUARDRAILS}
""".strip()

def build_project_context(project, documents, tasks, findings):
    """Serialize only what the model needs. Contact details are reduced to present/missing."""
    return json.dumps({
        "customer_first_name": project["first_name"],
        "service_type": project["service_type"],
        "monthly_electric_bill_usd": project["monthly_electric_bill"],
        "roof_age_years": project["roof_age"],
        "roof_information_provided": project["roof_age"] is not None,
        "current_workflow_stage": project["current_stage"],
        "project_status": project["project_status"],
        "priority": project["priority"],
        "customer_notes": project["notes"] or "",
        "contact_info_on_file": {f: bool((project[f] or "").strip()) for f in config.REQUIRED_CONTACT_FIELDS},
        "documents": [{"type": d["document_type"], "status": d["status"]} for d in documents],
        "existing_tasks": [{"title": t["title"], "department": t["department"], "status": t["status"]}
                           for t in tasks],
        "business_rule_findings": [f.message for f in findings if not f.resolved],
        "rule_detected_missing_information": [f.missing_item for f in findings if f.missing_item and not f.resolved],
    }, indent=2)


def analyze_project(context_json) -> AIResult:
    parsed, result = _structured_request(
        "project_analysis", ANALYSIS_INSTRUCTIONS,
        f"Project record (JSON):\n{context_json}", ProjectAnalysis)
    result.data = validate_analysis(parsed)
    return result
