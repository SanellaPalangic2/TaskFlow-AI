"""
One-command check that OpsPilot can reach the real Anthropic API.

    python verify_ai.py

It uses the same code path as the app (services/ai_service.py) and the key in
.env (or the environment). The key itself is never printed. On success it
prints Anthropic's message ID for the response, proving the result came from
the API.
"""
import json
import sys

if hasattr(sys.stdout, "reconfigure"):  # safe output on Windows consoles and redirects
    sys.stdout.reconfigure(errors="replace")

import config
from services import ai_service

SAMPLE_PROJECT = {
    "customer_first_name": "John",
    "service_type": "Solar + Roofing",
    "monthly_electric_bill_usd": 420,
    "roof_age_years": 19,
    "roof_information_provided": True,
    "current_workflow_stage": "Information Gathering",
    "project_status": "Human Review Required",
    "priority": "High",
    "customer_notes": "I want solar but I'm worried my roof may need to be replaced first.",
    "contact_info_on_file": {"email": True, "phone": True, "address": True},
    "documents": [{"type": "Utility Bill", "status": "Missing"}, {"type": "Roof Photos", "status": "Missing"}],
    "existing_tasks": [],
    "business_rule_findings": ["Required document not on file: Utility Bill",
                               "Roof information requires review (19 yrs, demo threshold 15 yrs)"],
    "rule_detected_missing_information": ["Utility Bill", "Roof Photos"],
}


def main():
    print("OpsPilot AI: Claude connection check")
    print(f"  Model:   {config.AI_MODEL}")
    print(f"  API key: {'found in environment (not shown)' if ai_service.is_configured() else 'NOT FOUND'}")
    if not ai_service.is_configured():
        print("\n  Copy .env.example to .env and set ANTHROPIC_API_KEY, then run this again.")
        return 1

    print("  Sending one structured-output request...")
    try:
        result = ai_service.analyze_project(json.dumps(SAMPLE_PROJECT, indent=2))
    except ai_service.AIServiceError as err:
        print(f"\n  FAILED [{err.code}] {err.title}: {err.message}")
        return 1

    d = result.data
    print("\n  SUCCESS: response received from the Anthropic API and validated")
    print(f"  Response ID:  {result.response_id}")
    print(f"  Model used:   {result.model}")
    print(f"  Latency:      {result.latency_ms} ms")
    print(f"  Tokens:       {result.usage.get('input_tokens')} in / {result.usage.get('output_tokens')} out")
    print(f"\n  Intent:       {d['detected_intent']}  (confidence {d['confidence']:.0%})")
    print(f"  Summary:      {d['summary']}")
    print(f"  Info status:  {d['information_status']}")
    print(f"  Next action:  {d['recommended_action']}  [{d['suggested_department']}]")
    for w in d["recommendation_why"]:
        print(f"    why: {w}")
    print("\n  To verify: the Usage page in the Claude Console (console.anthropic.com)")
    print("  shows this request a few minutes later.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
