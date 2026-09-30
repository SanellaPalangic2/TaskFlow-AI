"""
Restore the clean synthetic demo dataset before a demonstration.

    python reset_demo_data.py          # asks for confirmation
    python reset_demo_data.py --yes    # no prompt

What it does:
  * Touches ONE file: the OpsPilot demo database (instance/opspilot.db by default).
    It refuses to run if the database path points outside this project's instance/ folder.
  * Saves a backup copy first (instance/backups/, the 5 most recent are kept).
  * Rebuilds the database with the synthetic demo data: 14 fictional customers and projects,
    tasks, documents, activity, 3 clearly labelled sample AI analyses, and the 10 Evaluation Lab cases.

It makes no Anthropic API calls. Evaluation Lab runs, workflow designs and Automation Finder analyses start
empty: those only ever come from real API calls (see INTERVIEW_DEMO.md for optional commands).
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

import config

INSTANCE = (config.BASE_DIR / "instance").resolve()
BACKUPS = INSTANCE / "backups"
KEEP_BACKUPS = 5


def main():
    ap = argparse.ArgumentParser(description="Restore the synthetic OpsPilot demo dataset.")
    ap.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    args = ap.parse_args()

    db_path = config.DATABASE_PATH.resolve()
    if INSTANCE not in db_path.parents or db_path.suffix != ".db":
        print(f"Refusing to reset: the database path is outside this project's instance folder.\n  {db_path}\n"
              "Unset OPSPILOT_DB or point it inside the instance/ folder.")
        return 1

    print(f"OpsPilot demo reset\n  Database: {db_path}")
    if not args.yes:
        answer = input("This replaces all demo data (projects, tasks, approvals, evaluation runs). A backup is saved first.\n"
                       "Continue? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Cancelled. Nothing was changed.")
            return 0

    if db_path.exists():
        BACKUPS.mkdir(parents=True, exist_ok=True)
        backup = BACKUPS / f"opspilot-{datetime.now():%Y%m%d-%H%M%S}.db"
        src = sqlite3.connect(db_path)
        dst = sqlite3.connect(backup)
        src.backup(dst)                      # consistent copy even if the app is running
        dst.close(); src.close()
        print(f"  Backup:   {backup}")
        for old in sorted(BACKUPS.glob("opspilot-*.db"))[:-KEEP_BACKUPS]:
            old.unlink()

    import seed
    try:
        seed.init_db(reset=True)
    except PermissionError:
        print("The database file is in use. Stop the app (Ctrl+C), run this again, then restart the app.")
        return 1

    conn = sqlite3.connect(db_path)
    count = lambda sql: conn.execute(sql).fetchone()[0]
    projects = count("SELECT COUNT(*) FROM projects")
    attention = count("SELECT COUNT(*) FROM projects WHERE project_status IN ('Needs Attention', 'Blocked', 'Human Review Required')")
    open_tasks = count("SELECT COUNT(*) FROM tasks WHERE status != 'Completed'")
    approvals = count("SELECT COUNT(*) FROM approval_items WHERE status = 'pending'")
    cases = count("SELECT COUNT(*) FROM eval_cases")
    print("\nDemo data restored (all synthetic):")
    print(f"  Projects            {projects}  ({attention} need attention)")
    print(f"  Open tasks          {open_tasks}")
    print(f"  Pending approvals   {approvals}")
    print(f"  Evaluation cases    {cases} (no runs yet)")
    conn.close()
    print("\nNext: python app.py   then open http://127.0.0.1:5000")
    return 0


if __name__ == "__main__":
    sys.exit(main())
