"""
Run the AI Evaluation Lab from the terminal (same code, same cost controls as the page).

    python run_evaluation.py --dry-run        # show what would be sent; makes NO API calls
    python run_evaluation.py --limit 3        # quick check: 3 requests
    python run_evaluation.py                  # full evaluation: 10 requests

Results are saved to your OpsPilot database and appear in Evaluation Lab > Run history.
The cooldown and daily cap in .env (EVAL_COOLDOWN_SECONDS, EVAL_MAX_RUNS_PER_DAY) apply here too.
"""
import argparse
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import config
import database
import seed
from services import ai_service, evaluation


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=evaluation.MAX_CASES, help=f"cases to run (1-{evaluation.MAX_CASES})")
    ap.add_argument("--dry-run", action="store_true", help="list the cases and request count without calling Claude")
    args = ap.parse_args()

    db = database.connect()
    database.init_schema(db)
    if database.is_empty(db):
        seed.init_db()
    evaluation.ensure_cases(db)
    db.commit()
    cases = evaluation.list_cases(db)[:max(1, min(args.limit, evaluation.MAX_CASES))]
    est = evaluation.estimate(cases)
    print(f"AI Evaluation Lab  |  feature: project analysis  |  model {config.AI_MODEL}  |  prompt v{evaluation.prompt_version()}")
    print(f"{len(cases)} synthetic cases = {est['requests']} requests, roughly {est['input_tokens']:,.0f} input and "
          f"{est['output_tokens']:,} output tokens (estimate)\n")
    for i, c in enumerate(cases, 1):
        print(f"  {i:>2}. {c['title']}  ->  expects {' / '.join(c['expected']['intent'])}, team {' / '.join(c['expected']['departments'])}")
    if args.dry_run:
        print("\nDry run: nothing was sent to Claude.")
        return 0
    if not ai_service.is_configured():
        print("\nANTHROPIC_API_KEY is not set. Add it to .env and run again.")
        return 1
    try:
        run_id = evaluation.start_run(db, len(cases))
        db.commit()
    except evaluation.EvalError as err:
        print(f"\nNot started: {err.message}")
        return 1
    print(f"\nRun #{run_id} started. Press Ctrl+C to stop after the current case.\n")
    try:
        while True:
            step = evaluation.step(db, run_id)
            db.commit()
            if step.get("result_id"):
                r = evaluation.get_result(db, step["result_id"])
                label = evaluation.RESULTS[r["auto_result"]]
                detail = r["error_message"] if r["error_code"] else ", ".join(f"{c['label']}: {c['state']}" for c in r["comparison"]["checks"])
                print(f"  {label:<16} {r['case_title']}  ({detail})")
            if step.get("error"):
                print(f"\nStopped: {step['error']['title']}. {step['error']['message']}")
            if step["done"]:
                break
    except KeyboardInterrupt:
        evaluation.stop_run(db, run_id)
        db.commit()
        print("\nStopped by you. Completed cases are saved.")
    m = evaluation.get_run(db, run_id)["metrics"]
    fmt = lambda k: f"{m[k]['pct']}% ({m[k]['n']}/{m[k]['of']})" if m[k]["pct"] is not None else "n/a"
    print("\n" + "=" * 80)
    print(f"Classification accuracy        {fmt('classification')}")
    print(f"Information detection accuracy {fmt('information')}")
    print(f"Recommendation agreement       {fmt('action')}")
    print(f"Passed / review / failed       {m['results']['passed']} / {m['results']['review']} / {m['results']['failed']}")
    print(f"Tokens reported by Claude      {m['tokens']:,}")
    print("Review disagreements in the app: Evaluation Lab.")
    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
