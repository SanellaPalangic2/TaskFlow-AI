"""
Data layer: SQLite schema, connection handling, and read queries.

Uses Python's built-in sqlite3 module (no ORM) to keep the prototype small
and dependency-light. Writes that change workflow state live in
services/workflow.py so every change is paired with an activity-log entry.
"""
import json
import sqlite3
from datetime import date, datetime

from flask import g

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS customers (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    first_name   TEXT NOT NULL,
    last_name    TEXT NOT NULL,
    email        TEXT,
    phone        TEXT,
    address      TEXT,
    is_synthetic INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id           INTEGER NOT NULL REFERENCES customers(id),
    service_type          TEXT NOT NULL,
    monthly_electric_bill REAL,
    roof_age              INTEGER,
    project_status        TEXT NOT NULL DEFAULT 'Active',
    current_stage         TEXT NOT NULL DEFAULT 'New Lead',
    priority              TEXT NOT NULL DEFAULT 'Medium',
    notes                 TEXT,
    is_synthetic          INTEGER NOT NULL DEFAULT 0,
    created_at            TEXT NOT NULL,
    updated_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id),
    document_type TEXT NOT NULL,
    filename      TEXT,
    status        TEXT NOT NULL DEFAULT 'Missing',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id),
    title       TEXT NOT NULL,
    description TEXT,
    department  TEXT NOT NULL,
    priority    TEXT NOT NULL DEFAULT 'Medium',
    status      TEXT NOT NULL DEFAULT 'Open',
    due_date    TEXT,
    source      TEXT NOT NULL DEFAULT 'HUMAN',
    rule_key    TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ai_analyses (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id           INTEGER NOT NULL REFERENCES projects(id),
    summary              TEXT NOT NULL,
    detected_intent      TEXT NOT NULL,
    recommended_action   TEXT NOT NULL,
    reasoning            TEXT NOT NULL,
    confidence           REAL NOT NULL,
    concerns             TEXT NOT NULL DEFAULT '[]',
    suggested_department TEXT,
    review_status        TEXT NOT NULL DEFAULT 'Pending',
    source               TEXT NOT NULL DEFAULT 'live',
    model                TEXT,
    response_id          TEXT,
    latency_ms           INTEGER,
    created_at           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER REFERENCES projects(id),
    action      TEXT NOT NULL,
    action_type TEXT NOT NULL CHECK (action_type IN ('AI', 'AUTOMATION', 'HUMAN')),
    details     TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS automation_analyses (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    process_name             TEXT NOT NULL,
    original_description     TEXT NOT NULL,
    steps                    TEXT NOT NULL,
    automation_opportunities INTEGER NOT NULL,
    ai_opportunities         INTEGER NOT NULL,
    human_decisions          INTEGER NOT NULL,
    total_steps              INTEGER NOT NULL,
    analysis                 TEXT NOT NULL,
    is_saved                 INTEGER NOT NULL DEFAULT 0,
    source                   TEXT NOT NULL DEFAULT 'live',
    model                    TEXT,
    response_id              TEXT,
    latency_ms               INTEGER,
    created_at               TEXT NOT NULL,
    saved_at                 TEXT
);

CREATE TABLE IF NOT EXISTS workflow_designs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    status      TEXT CHECK (status IS NULL OR status IN ('Draft', 'Pending Approval', 'Active', 'Disabled')),
    definition  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'live',
    model       TEXT,
    response_id TEXT,
    latency_ms  INTEGER,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    approved_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_design_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    design_id  INTEGER NOT NULL REFERENCES workflow_designs(id) ON DELETE CASCADE,
    action     TEXT NOT NULL,
    actor_type TEXT NOT NULL CHECK (actor_type IN ('AI', 'AUTOMATION', 'HUMAN')),
    details    TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approval_items (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    kind               TEXT NOT NULL CHECK (kind IN ('communication', 'recommendation', 'workflow', 'decision')),
    status             TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected', 'superseded')),
    project_id         INTEGER,
    analysis_id        INTEGER,
    workflow_design_id INTEGER,
    title              TEXT NOT NULL,
    ai_output          TEXT NOT NULL,            -- original AI output, never modified
    justification      TEXT,
    evidence           TEXT,
    confidence         REAL,
    confidence_basis   TEXT,
    potential_action   TEXT,
    reject_effect      TEXT,
    checks             TEXT,
    ai_input           TEXT,                     -- exactly what was sent to Claude, when OpsPilot drafted it
    source             TEXT,
    model              TEXT,
    response_id        TEXT,
    created_at         TEXT NOT NULL,
    decided_at         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_analysis ON approval_items(analysis_id) WHERE analysis_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_open_workflow ON approval_items(workflow_design_id)
    WHERE workflow_design_id IS NOT NULL AND status = 'pending';

CREATE TABLE IF NOT EXISTS approval_decisions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    item_id         INTEGER NOT NULL REFERENCES approval_items(id),
    decision        TEXT NOT NULL CHECK (decision IN ('approved', 'approved_with_edits', 'rejected')),
    original_output TEXT NOT NULL,
    final_output    TEXT,
    changed_fields  TEXT,
    note            TEXT,
    decided_by      TEXT NOT NULL,
    decided_via     TEXT NOT NULL,
    outcome         TEXT,
    created_at      TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS approval_ai_output_is_immutable
BEFORE UPDATE OF ai_output, kind, created_at ON approval_items
BEGIN SELECT RAISE(ABORT, 'The original AI output of an approval item cannot be changed'); END;

CREATE TRIGGER IF NOT EXISTS approval_decisions_no_update
BEFORE UPDATE ON approval_decisions
BEGIN SELECT RAISE(ABORT, 'Approval decisions are append-only'); END;

CREATE TRIGGER IF NOT EXISTS approval_decisions_no_delete
BEFORE DELETE ON approval_decisions
BEGIN SELECT RAISE(ABORT, 'Approval decisions are append-only'); END;

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS idx_docs_project ON documents(project_id);
CREATE INDEX IF NOT EXISTS idx_activity_project ON activity_log(project_id);
CREATE INDEX IF NOT EXISTS idx_analyses_project ON ai_analyses(project_id);

-- AI Evaluation Lab: synthetic test cases, runs against the real API, stored outputs, human reviews.
CREATE TABLE IF NOT EXISTS eval_cases (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    position    INTEGER NOT NULL DEFAULT 0,
    definition  TEXT NOT NULL,          -- synthetic project record + expected values (JSON)
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    feature           TEXT NOT NULL,
    model             TEXT NOT NULL,       -- model name only; the API key is never stored
    reasoning_effort  TEXT,
    prompt_version    TEXT,
    status            TEXT NOT NULL CHECK (status IN ('running', 'completed', 'stopped')),
    planned_cases     INTEGER NOT NULL,
    case_plan         TEXT NOT NULL,
    stop_reason       TEXT,
    started_at        TEXT NOT NULL,
    last_activity_at  TEXT NOT NULL,
    finished_at       TEXT
);

CREATE TABLE IF NOT EXISTS eval_results (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES eval_runs(id),
    case_id        INTEGER NOT NULL REFERENCES eval_cases(id),
    case_key       TEXT NOT NULL,
    case_title     TEXT NOT NULL,
    input_context  TEXT NOT NULL,          -- exactly what was sent to the model
    rule_findings  TEXT,
    expected       TEXT NOT NULL,          -- expected values at run time
    actual         TEXT,                   -- validated structured model output (NULL if the call failed)
    comparison     TEXT NOT NULL,          -- computed in Python
    auto_result    TEXT NOT NULL CHECK (auto_result IN ('passed', 'review', 'failed')),
    response_id    TEXT,
    model          TEXT,
    latency_ms     INTEGER,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    error_code     TEXT,
    error_message  TEXT,
    created_at     TEXT NOT NULL,
    UNIQUE (run_id, case_id)
);

CREATE TABLE IF NOT EXISTS eval_reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    result_id  INTEGER NOT NULL REFERENCES eval_results(id),
    verdict    TEXT NOT NULL CHECK (verdict IN ('correct', 'partially_correct', 'incorrect')),
    note       TEXT,
    reviewer   TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TRIGGER IF NOT EXISTS eval_results_are_immutable
BEFORE UPDATE ON eval_results
BEGIN SELECT RAISE(ABORT, 'Stored evaluation results cannot be changed'); END;

CREATE TRIGGER IF NOT EXISTS eval_reviews_no_update
BEFORE UPDATE ON eval_reviews
BEGIN SELECT RAISE(ABORT, 'Human evaluations are append-only'); END;

CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);
"""


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


# --- Connection handling ---------------------------------------------------

def connect(path=None):
    path = path or config.DATABASE_PATH
    config.DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_db():
    """One connection per request, closed automatically at teardown."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


# Columns added by the Intelligence Layer. Older databases are upgraded in place.
ANALYSIS_COLUMNS = {
    "intent_detail": "TEXT",
    "information_status": "TEXT",
    "info_status_adjusted": "INTEGER NOT NULL DEFAULT 0",
    "missing_information": "TEXT NOT NULL DEFAULT '[]'",
    "recommendation_why": "TEXT NOT NULL DEFAULT '[]'",
    "suggested_tasks": "TEXT NOT NULL DEFAULT '[]'",
    "requires_human_review": "INTEGER NOT NULL DEFAULT 0",
    "human_review_reasons": "TEXT NOT NULL DEFAULT '[]'",
    "context_hash": "TEXT",
    "evidence_snapshot": "TEXT",   # Explainability: database facts and rule findings the analysis used
}
JSON_ANALYSIS_FIELDS = ("concerns", "missing_information", "recommendation_why", "suggested_tasks",
                        "human_review_reasons")


def init_schema(conn):
    conn.executescript(SCHEMA)
    existing = {r[1] for r in conn.execute("PRAGMA table_info(ai_analyses)")}
    for column, ddl in ANALYSIS_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE ai_analyses ADD COLUMN {column} {ddl}")
    conn.execute("UPDATE tasks SET department = 'Customer Support' WHERE department = 'Customer Success'")
    # Live AI results were marked 'openai' before the switch to Anthropic; the marker is now provider-neutral.
    for table in ("ai_analyses", "automation_analyses", "workflow_designs", "approval_items"):
        conn.execute(f"UPDATE {table} SET source = 'live' WHERE source = 'openai'")
    conn.commit()


def is_empty(conn):
    return conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0


# --- Read queries ----------------------------------------------------------

PROJECT_SELECT = """
SELECT p.*, c.first_name, c.last_name, c.email, c.phone, c.address,
       c.first_name || ' ' || c.last_name AS customer_name
FROM projects p JOIN customers c ON c.id = p.customer_id
"""

OVERDUE_SQL = "(t.status != 'Completed' AND t.due_date IS NOT NULL AND t.due_date < DATE('now', 'localtime'))"


def dashboard_metrics(db):
    one = lambda sql, *a: db.execute(sql, a).fetchone()[0]
    return {
        "active_projects": one("SELECT COUNT(*) FROM projects WHERE project_status != 'Completed'"),
        "needs_attention": one(
            "SELECT COUNT(*) FROM projects WHERE project_status IN ('Needs Attention', 'Blocked')"),
        "open_tasks": one("SELECT COUNT(*) FROM tasks WHERE status != 'Completed'"),
        "overdue_tasks": one(f"SELECT COUNT(*) FROM tasks t WHERE {OVERDUE_SQL}"),
        "human_reviews": one(
            "SELECT COUNT(*) FROM projects WHERE project_status = 'Human Review Required'")
        + one("SELECT COUNT(*) FROM ai_analyses WHERE review_status = 'Pending'"),
        "automated_actions": one(
            "SELECT COUNT(*) FROM activity_log WHERE action_type = 'AUTOMATION' "
            "AND created_at >= DATETIME('now', 'localtime', '-7 days')"),
        "ai_actions": one(
            "SELECT COUNT(*) FROM activity_log WHERE action_type = 'AI' "
            "AND created_at >= DATETIME('now', 'localtime', '-7 days')"),
        "human_actions": one(
            "SELECT COUNT(*) FROM activity_log WHERE action_type = 'HUMAN' "
            "AND created_at >= DATETIME('now', 'localtime', '-7 days')"),
    }


def stage_distribution(db, stages):
    counts = dict(db.execute(
        "SELECT current_stage, COUNT(*) FROM projects GROUP BY current_stage").fetchall())
    total = sum(counts.values()) or 1
    return [{"stage": s, "count": counts.get(s, 0), "pct": round(100 * counts.get(s, 0) / total)}
            for s in stages]


def attention_queue(db, limit=6):
    """Projects that need a person to look at them. The route adds the reason from the rules engine."""
    return db.execute(PROJECT_SELECT + """
        WHERE p.project_status IN ('Needs Attention', 'Blocked', 'Human Review Required')
        ORDER BY CASE p.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
                 p.updated_at DESC
        LIMIT ?""", (limit,)).fetchall()


def latest_workflow_note(db, project_id):
    row = db.execute(
        "SELECT details FROM activity_log WHERE project_id = ? AND action_type = 'HUMAN' "
        "AND action LIKE 'Workflow updated%' ORDER BY created_at DESC, id DESC LIMIT 1", (project_id,)).fetchone()
    try:
        return (json.loads(row["details"]) or {}).get("note") if row else None
    except (TypeError, ValueError):
        return None


def recent_activity(db, limit=8, project_id=None):
    sql = """SELECT a.*, c.first_name || ' ' || c.last_name AS customer_name
             FROM activity_log a
             LEFT JOIN projects p ON p.id = a.project_id
             LEFT JOIN customers c ON c.id = p.customer_id"""
    args = []
    if project_id:
        sql += " WHERE a.project_id = ?"
        args.append(project_id)
    sql += " ORDER BY a.created_at DESC, a.id DESC LIMIT ?"
    args.append(limit)
    return db.execute(sql, args).fetchall()


def list_projects(db, q="", status="", stage="", priority=""):
    sql = PROJECT_SELECT + " WHERE 1=1"
    args = []
    if q:
        sql += (" AND (c.first_name || ' ' || c.last_name LIKE ? OR p.service_type LIKE ?"
                " OR c.email LIKE ? OR c.address LIKE ?)")
        args += [f"%{q}%"] * 4
    if status:
        sql += " AND p.project_status = ?"
        args.append(status)
    if stage:
        sql += " AND p.current_stage = ?"
        args.append(stage)
    if priority:
        sql += " AND p.priority = ?"
        args.append(priority)
    sql += " ORDER BY p.updated_at DESC"
    return db.execute(sql, args).fetchall()


def project_status_counts(db):
    rows = db.execute("SELECT project_status, COUNT(*) FROM projects GROUP BY project_status").fetchall()
    return dict(rows)


def get_project(db, project_id):
    return db.execute(PROJECT_SELECT + " WHERE p.id = ?", (project_id,)).fetchone()


def project_documents(db, project_id):
    return db.execute("SELECT * FROM documents WHERE project_id = ? ORDER BY id", (project_id,)).fetchall()


def project_tasks(db, project_id, include_completed=False):
    sql = f"SELECT t.*, {OVERDUE_SQL} AS is_overdue FROM tasks t WHERE t.project_id = ?"
    if not include_completed:
        sql += " AND t.status != 'Completed'"
    sql += " ORDER BY CASE t.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END, t.due_date"
    return db.execute(sql, (project_id,)).fetchall()


def latest_analysis(db, project_id):
    row = db.execute("SELECT * FROM ai_analyses WHERE project_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
                     (project_id,)).fetchone()
    return analysis_to_dict(row)


def get_analysis(db, analysis_id):
    return analysis_to_dict(db.execute("SELECT * FROM ai_analyses WHERE id = ?", (analysis_id,)).fetchone())


def analysis_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for field in JSON_ANALYSIS_FIELDS:
        try:
            d[field] = json.loads(d.get(field) or "[]")
        except (TypeError, ValueError):
            d[field] = []
    return d


def last_activity_at(db, project_id):
    row = db.execute("SELECT MAX(created_at) FROM activity_log WHERE project_id = ?", (project_id,)).fetchone()
    return row[0]


def list_tasks(db, view="all", q="", department=""):
    sql = f"""SELECT t.*, {OVERDUE_SQL} AS is_overdue,
                     c.first_name || ' ' || c.last_name AS customer_name, p.service_type
              FROM tasks t JOIN projects p ON p.id = t.project_id
              JOIN customers c ON c.id = p.customer_id WHERE 1=1"""
    args = []
    if view == "open":
        sql += f" AND t.status = 'Open' AND NOT {OVERDUE_SQL}"
    elif view == "in_progress":
        sql += f" AND t.status = 'In Progress' AND NOT {OVERDUE_SQL}"
    elif view == "overdue":
        sql += f" AND {OVERDUE_SQL}"
    elif view == "completed":
        sql += " AND t.status = 'Completed'"
    if q:
        sql += " AND (t.title LIKE ? OR c.first_name || ' ' || c.last_name LIKE ?)"
        args += [f"%{q}%"] * 2
    if department:
        sql += " AND t.department = ?"
        args.append(department)
    sql += """ ORDER BY CASE WHEN t.status = 'Completed' THEN 1 ELSE 0 END,
                        t.due_date IS NULL, t.due_date,
                        CASE t.priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END"""
    return db.execute(sql, args).fetchall()


def task_view_counts(db):
    one = lambda sql: db.execute(sql).fetchone()[0]
    return {
        "all": one("SELECT COUNT(*) FROM tasks"),
        "open": one(f"SELECT COUNT(*) FROM tasks t WHERE t.status = 'Open' AND NOT {OVERDUE_SQL}"),
        "in_progress": one(f"SELECT COUNT(*) FROM tasks t WHERE t.status = 'In Progress' AND NOT {OVERDUE_SQL}"),
        "overdue": one(f"SELECT COUNT(*) FROM tasks t WHERE {OVERDUE_SQL}"),
        "completed": one("SELECT COUNT(*) FROM tasks WHERE status = 'Completed'"),
    }


def tasks_due_soon(db, limit=5):
    return db.execute(f"""SELECT t.*, {OVERDUE_SQL} AS is_overdue,
                                 c.first_name || ' ' || c.last_name AS customer_name
                          FROM tasks t JOIN projects p ON p.id = t.project_id
                          JOIN customers c ON c.id = p.customer_id
                          WHERE t.status != 'Completed' AND t.due_date IS NOT NULL
                          ORDER BY t.due_date LIMIT ?""", (limit,)).fetchall()


def today_iso():
    return date.today().isoformat()
