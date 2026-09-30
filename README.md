# OpsPilot AI

An internal operations command center prototype. It shows how **business rules**, **AI** and **human decisions** work together on customer projects:

- **Automation** (plain Python rules) checks the predictable things: contact details, required documents, roof age against a demo threshold. It creates tasks and flags projects.
- **AI** (the real Anthropic API) reads unstructured customer notes, summarizes the project, detects intent and concerns, suggests a next action and drafts customer messages. It never changes project state.
- **People** approve AI suggestions, move workflow stages and approve anything customer-facing, in one place: the **Human Approval Center**.

Every action is written to an activity log tagged `AI`, `AUTOMATION` or `HUMAN`.

All customers and projects are **synthetic demo data**. OpsPilot makes no engineering, safety, legal, financial, permitting or installation decisions.

---

## Run it

Requires Python 3.10+.

**macOS / Linux**
```bash
cd opspilot-ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then open .env and paste your key
python app.py
```

**Windows (PowerShell)**
```powershell
cd opspilot-ai
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env        # then open .env and paste your key
python app.py
```

Open **http://127.0.0.1:5000**

The database is created and seeded automatically on first run (`instance/opspilot.db`).
To restore the clean synthetic demo data at any time: `python reset_demo_data.py` (backs up first, touches only the demo database).
For the interview walkthrough, commands and likely questions, see **INTERVIEW_DEMO.md**.

### Run the Intelligence Layer scenarios against the real API
```bash
python run_scenarios.py            # 5 scenarios, each analyzed twice
python run_scenarios.py --runs 3
```
Uses a throwaway database (`instance/scenarios.db`). For each scenario it prints the AI output from every run and checks that the rule-driven results (findings, tasks, status, stage, priority) are identical no matter what the AI wrote. Rule checks must pass; AI checks are shown for review.

### Test the Copilot against the real API
```bash
python run_copilot_tests.py
```
Asks 16 questions (normal, false-premise, not-found, off-topic, ambiguous, follow-up, SQL injection, change requests) against a fresh copy of the demo data in `instance/copilot_test.db`. Hard checks cover grounding, the exact fallback sentence, real project links, and that the database is unchanged.

### Run the Automation Finder demo processes against the real API
```bash
python run_automation_examples.py            # analyzes and saves the 3 demo processes
python run_automation_examples.py --no-save
```
Runs Document intake, Customer follow-up and Internal project handoff (saved so they appear under **Saved analyses**), plus three edge cases: a vague description, something that isn't a process, and a refund-approval process that must keep people in charge. Re-running replaces the saved demos.

### Run the Workflow Builder samples against the real API
```bash
python run_workflow_examples.py            # generates, validates and saves the 3 samples as Drafts
python run_workflow_examples.py --no-save
```
Generates *Missing document follow-up*, *Customer message triage* and *New lead intake*, plus a vague and an off-topic description. The samples are saved as **Drafts** only. Approve them yourself in **Workflows**.

### Test the Human Approval Center with real AI drafts
```bash
python run_approval_drafts.py            # tests on a throwaway copy, then queues 3 live drafts for you to review
python run_approval_drafts.py --no-save  # tests only
```
Drafts 5 customer messages (missing information, roof concern, status update) on a fresh copy in `instance/approvals_test.db`. It checks that only minimal context was sent, then approves one, edits and approves one, and rejects one. Finally it verifies the audit trail: original output unchanged, correction stored beside it, decisions append-only, drafting logged as `AI` and decisions as `HUMAN`, nothing sent. Unless `--no-save`, it then drafts 3 more into your demo data. They wait in **Approvals**.

### Check the "Why?" explanations against real AI responses
```bash
python run_explainability_check.py
```
On a throwaway copy (`instance/explain_test.db`) it runs 4 real project analyses, 1 message draft, 1 Automation Finder analysis and 1 Workflow Builder proposal. For each one it prints the explanation and checks it: evidence matches the database, the AI section holds only stored model output, triggered rules match the rules engine, the human decision matches the Approval Center, and no key or internal reasoning appears.

### Run the AI Evaluation Lab from the terminal
```bash
python run_evaluation.py --dry-run   # lists the cases and request count; makes NO API calls
python run_evaluation.py --limit 3   # quick check: 3 requests
python run_evaluation.py             # full evaluation: 10 requests
```
Results are saved and appear under **Evaluation Lab → Run history**. The same cost controls apply as on the page.

### Check the real Anthropic (Claude) connection
```bash
python verify_ai.py
```
Sends one structured request through the same code the app uses and prints Anthropic's **message ID**, model, latency and token counts. The key is never printed.

### Optional settings (`.env`)
| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | none | Required for AI features |
| `ANTHROPIC_MODEL` | `claude-haiku-4-5-20251001` | Any Claude model that supports tool use, e.g. `claude-sonnet-5-5` for stronger answers |
| `ROOF_AGE_REVIEW_THRESHOLD` | `15` | Demo rule threshold in years |
| `EVAL_MAX_CASES` | `10` | Evaluation Lab: most requests one run can send (hard cap 10) |
| `EVAL_COOLDOWN_SECONDS` | `60` | Evaluation Lab: minimum time between runs |
| `EVAL_MAX_RUNS_PER_DAY` | `10` | Evaluation Lab: most runs per day |

---

## Architecture

```
Browser (HTML/CSS/JS)
   │  page requests + small JSON calls (never sees the API key)
   ▼
app.py ─────────── routes only
   ├── database.py              read queries (SQLite, built-in sqlite3)
   ├── services/workflow.py     every write + activity log entry
   ├── services/rules_engine.py deterministic business rules  → AUTOMATION
   └── services/ai_service.py   Anthropic Messages API        → AI (suggestions only)
```

| File | What it does |
|---|---|
| `app.py` | Flask routes for pages and JSON endpoints, form validation, template filters, error pages |
| `config.py` | Settings: model, timeouts, roof threshold, required documents per service. Loads `.env` |
| `database.py` | Schema, connection handling, read queries for every page |
| `seed.py` | Synthetic customers, projects, tasks and activity; `--reset` rebuilds the DB |
| `services/rules_engine.py` | `evaluate()` returns findings (pure function); `apply_rules()` creates tasks, closes tasks when documents arrive, flags status, escalates priority |
| `services/workflow.py` | The only place that changes state. Stage/status changes are human actions; AI results are stored as *pending* suggestions |
| `services/ai_service.py` | The only file that calls Claude. Pydantic schemas, structured output via a required answer tool, second validation pass, error mapping |
| `services/intelligence.py` | The Intelligence Layer: runs an analysis, re-runs rules, validates AI task suggestions, merges missing information, sets information status and human-review reasons, builds the Next Best Action |
| `services/copilot.py` | Copilot: 10 safe read-only query tools, argument validation, answer schema, grounding checks |
| `templates/copilot.html`, `static/js/copilot.js` | Copilot page: conversation, suggestions, data-source line, project links, loading and error states |
| `run_copilot_tests.py` | 16 Copilot questions against the real API with automatic grounding checks |
| `services/automation_finder.py` | Automation Finder: schema, prompt, validation, human-judgment policy, efficiency-claim removal, proposed-workflow builder, Python metrics, saving |
| `templates/automation.html`, `templates/partials/automation_*.html`, `static/js/automation.js` | Finder page, results workspace and saved list |
| `run_automation_examples.py` | Runs the 3 demo processes and edge cases against the real API with automatic checks |
| `services/workflow_designer.py` | Workflow Builder: proposal schema, graph validation, human-approval policy, deterministic layout and line routing, status lifecycle |
| `templates/workflows.html`, `templates/workflow_builder.html`, `static/js/workflow_builder.js` | Workflow library and the visual builder (header, description, canvas, details panel) |
| `run_workflow_examples.py` | Runs the 3 sample workflows through the real API with automatic structure, policy and layout checks |
| `services/dashboard.py` | Operations overview: every metric, the attention queue, pipeline, activity feed and insights, calculated read-only from SQLite; the optional verified AI summary |
| `templates/dashboard.html`, `static/js/dashboard.js` | Dashboard page, activity filter and the Summarize button |
| `services/explain.py` | Explainability: builds each "Why?" explanation (evidence, AI interpretation, business rules, human decision) from stored data; `capture_evidence()` snapshots the facts at analysis time |
| `templates/partials/explain_drawer.html`, `static/js/explain.js` | The shared "Why?" side panel. Any element with `data-explain="/api/explain/..."` opens it |
| `run_explainability_check.py` | Real API check of the explanations |
| `services/evaluation.py` | AI Evaluation Lab: 10 synthetic cases, run engine with cost controls, Python comparison and metrics, human evaluations, run comparison |
| `templates/evaluation.html`, `templates/partials/eval_*.html`, `static/js/evaluation.js` | Evaluation Lab page: metrics, results table, live run progress, detail panel, run history |
| `reset_demo_data.py` | Restores the synthetic demo dataset (backup first; only the demo database) |
| `INTERVIEW_DEMO.md` | Start commands, API setup, demo scripts, architecture, likely questions, production limitations |
| `run_evaluation.py` | Run the evaluation from the terminal (`--dry-run`, `--limit`) |
| `services/approvals.py` | Human Approval Center: builds the queue, minimal drafting context, draft checks, approve / edit / reject with the audit record, and the effect of each decision |
| `templates/approvals.html`, `static/js/approvals.js` | Approval Center page: counts, filters, list and detail split view, edit modal (original beside editable copy), confirm and draft modals, history |
| `run_approval_drafts.py` | Real API drafting test: 5 drafts, minimal-context checks, approve / edit / reject, audit trail checks |
| `run_scenarios.py` | Five end-to-end scenarios against the real API with automatic rule-consistency checks |
| `verify_ai.py` | One-command real API check |
| `templates/base.html` | App shell: sidebar, header, icon sprite, toasts |
| `templates/_macros.html` | Reusable UI pieces: icons, badges, empty states, activity items |
| `templates/partials/` | Project header, Next best action, AI analysis, open tasks and activity timeline; re-rendered in place after a live AI call |
| `static/css/design-system.css` | Design tokens and all reusable components |
| `static/css/layout.css` | Shell and page layouts, responsive breakpoints |
| `static/js/app.js` | Loading states, modals, confirmations, toasts, inline task updates, live filtering, form validation |

### How data travels (example: Run AI analysis)
1. Browser `POST /api/projects/<id>/analyze` (no key, no prompt, just the project id).
2. `app.py` loads the project, documents and tasks from SQLite and runs `rules_engine.evaluate()`.
3. `ai_service.build_project_context()` builds a minimal JSON record (contact details reduced to present/missing).
4. `ai_service.analyze_project()` calls `client.responses.parse(..., text_format=ProjectAnalysis)`. The SDK sends a strict JSON schema; the response is parsed into a Pydantic object, then `validate_analysis()` checks it again (confidence range, required text, lengths).
5. `workflow.save_analysis()` stores it with `review_status = Pending`, the model name and Anthropic's message ID, and logs an `AI` activity entry. Project stage and status are **not** touched.
6. The route returns rendered HTML for the AI card and Next best action card; the browser swaps them in.
7. `approvals.sync()` puts the recommendation in the **Approval Center** queue. A person approves, edits or rejects it there (or on the project page) → `approvals.decide()` → decision record + `HUMAN` activity entry.

On any failure (missing key, bad key, rate limit, quota, timeout, network, bad model, malformed or invalid output, refusal) the service raises `AIServiceError` with a plain-language message, and the UI shows it inside the card with a **Try again** button. There is no fallback to fake output.

### Where Claude is called
Only in `services/ai_service.py`: `_structured_request()` (`client.messages.create` with a required answer tool) and `run_tool_loop()` (Copilot). Project analysis, communication drafting (`approvals.draft_communication`), the Automation Finder and the Workflow Builder all go through them. No other file creates an Claude client.

### How to verify a response came from the API
- Each live analysis shows a **Live from Claude** badge with the model and **message ID** (`msg_…`). Seeded samples say **Synthetic sample** instead.
- The same ID appears in the activity timeline and in the terminal log line `AI request ok … response_id=…`.
- `python verify_ai.py` prints the response ID and token usage.
- The Usage page in the Claude Console shows the request.
- Remove the key from `.env` and restart: every AI button then shows "AI is not configured", proving nothing is faked.

### Security
The key is read from the environment only (`os.environ` in `ai_service._api_key`). It is never written to HTML, JavaScript, SQLite, logs or any API response. `.env` is in `.gitignore`.

---

## Intelligence Layer

| Who | Does |
|---|---|
| **AI** (`ai_service.py`) | Reads notes and the project record; returns summary, intent, information status, missing information, concerns, recommended action, "why" facts, suggested tasks, confidence, a short rationale |
| **Business rules** (`rules_engine.py`) | Contact details, required documents, roof information, roof age threshold; creates and closes tasks; flags status; escalates priority |
| **Python checks** (`intelligence.py`) | Decides what happens to each AI suggestion, which missing items are authoritative, the final information status, whether a human review is required, and the Next Best Action with its source |
| **People** | Approve or override recommendations, add AI-suggested tasks, change stages, approve customer messages |

**Analyze Project flow** (`intelligence.run_project_analysis`):
1. Load the record and evaluate rules. Build a minimal JSON context (service, bill, roof, documents, existing tasks, notes, stage, rule findings).
2. Call Claude with a strict JSON schema (the answer tool's input schema). If anything fails, stop: nothing is written and the UI shows **"AI analysis temporarily unavailable."** with **Retry**.
3. Log `AI: Customer notes analyzed`.
4. Run the business rules again. They create any missing tasks (never duplicates) and log each detection and task as `Business rule`. Their outcome does not depend on the AI output.
5. Check the AI output in Python:
   - Suggested tasks: document, contact and roof tasks are only valid if a rule finding backs them (then the rule task already covers it); otherwise they are rejected. Other tasks are checked for duplicates, then wait for a person to click **Add task**.
   - Missing information: rules are authoritative for contact, document and roof gaps; AI adds clarification items.
   - Information status: cannot be "Complete" while rules find gaps. Corrections are shown ("Set by rules") and logged.
   - Human review: required if a roof review rule is open, the AI asks for it, confidence is under 60%, or the intent is unclear.
6. Store the analysis as a *pending suggestion* with the model and response ID, and a fingerprint of the facts it used. If the record later changes, it is marked out of date.
7. Log `AI: Next action recommended`.

**Next Best Action** shows one headline, the owner, **Why?** (each fact tagged Business rule or AI) and **Source**: *AI recommendation*, *Business rule* or *Combined*. It shows only a short, user-facing justification; the model's internal reasoning is never requested, stored or displayed.

## Operations Copilot

```
Question ─▶ Claude (sees only 10 tool definitions) ─▶ tool call, e.g. get_overdue_tasks()
                                                       │
            OpsPilot validates name + arguments ◀──────┘   (unknown tool / bad args → rejected)
                         │
            fixed, parameterized, read-only SQL in services/copilot.py
                         │
            results ─▶ Claude writes a structured answer ─▶ Python checks it ─▶ UI
```

**Tools:** `get_projects_needing_attention`, `get_projects_by_status(status)`, `find_projects(customer_name)`, `get_project_details(project_id)`, `get_missing_documents(project_id?)`, `get_open_tasks(project_id?, priority?, department?)`, `get_overdue_tasks`, `get_projects_awaiting_human_review`, `get_pipeline_summary`, `get_recent_activity(project_id?, limit)`.

**Checks Python applies to every answer**
- An answer that used no successful tool call is withheld and replaced with *"I don't have enough information in OpsPilot to answer that."*
- Off-topic and unanswerable questions always get that exact sentence (written in Python, not by the model).
- Requests to change data get a fixed read-only reply.
- Project links are limited to projects the tools actually returned; invented IDs are dropped.
- The **Data source** counts (projects, tasks, documents, activity, analyses) come from what was retrieved, not from the model.
- Each answer's "How this was answered" panel lists the queries run and the message IDs.

The tool loop resends the conversation each round (the Messages API is stateless). `tool_choice` requires a tool call every round, and the last round forces the answer tool.

## Automation Opportunity Finder

1. The employee describes a process. The server sends it to Claude with a strict schema (steps with name, description, classification, justification and a `consequential` flag; proposed workflow; risks; human review points). The schema has **no numeric fields**.
2. Python validates and cleans every field. Sentences claiming percentages, time or cost savings are removed.
3. **Human-judgment policy** (`CONSEQUENTIAL` in `automation_finder.py`): a step the AI marked consequential, or one that approves, decides, moves money, decides eligibility, affects a job, has legal or safety impact, or handles an exception or escalation, becomes a **Human decision**, whatever the AI suggested. Steps that only receive, copy or record information are exempt. Every change is shown on the step and listed at the top.
4. The proposed workflow is rebuilt in a fixed order (Trigger → Automation → AI → Business rule → Human approval → Action), with invalid step references dropped. Decision steps are split out of AI or automation stages, and a human approval stage is added when needed.
5. **Metrics** are counts of the validated classifications, calculated in `compute_metrics()` and recalculated from the stored steps every time a saved analysis opens:
   - Total steps = number of steps
   - Automatable = Traditional automation + Business rule
   - AI candidates = AI candidate
   - Human decision points = Human decision (including policy moves)
6. The result is stored as a draft. **Save analysis** marks it saved, and only saved analyses are listed. Saving uses the server's stored copy, never data sent back from the browser.

## AI Workflow Builder (prototype)

A design and approval prototype. It does **not** execute workflows.

1. The employee describes a workflow. Claude returns a strict structure: nodes (`trigger`, `ai`, `condition`, `automation`, `human_approval`, `action`) with title, what it does, why it exists, AI involvement, data used and approval needs, plus connections with branch labels.
2. **Python validates the graph** (`workflow_designer.validate`):
   - exactly one trigger, with nothing leading into it
   - safe, unique IDs, and connections only between real steps
   - no self-loops; steps nothing connects to are removed
   - loops back to earlier steps become dashed "returns to" lines
   - every condition needs two or more labelled outcomes

   Every fix is listed under **OpsPilot checks**.
3. **Human-approval policy** (`_apply_human_policy`):
   - Consequential steps (approvals, money, eligibility, legal or safety calls) must sit behind a Human approval on every path.
   - A step where AI writes and sends a customer message gets an approval directly in front of it.
   - A message AI drafts must pass a person before any later step sends it.

   Hand-offs to people ("send to team lead") don't need an extra approval. Steps OpsPilot adds are marked **Policy**.
4. **Layout** is computed in Python (layered, No-branches left, Yes-branches right, orthogonal lines that avoid other steps). The browser only draws these coordinates and sets model text with `textContent`, so model output is never rendered as HTML.
5. **Status lifecycle** (`TRANSITIONS`, enforced on the server and by a database CHECK):
   - AI proposal → unsaved
   - Save draft → **Draft**
   - Request approval → **Pending Approval**
   - Approve and activate (a person, with confirmation) → **Active**
   - Disable → **Disabled**
   - Request approval again → **Pending Approval**

   Only unsaved proposals and Drafts can be regenerated. Every change is logged with who made it (AI, business rule or human).

## Operations overview (dashboard)

Read-only. `services/dashboard.py` calculates every figure from SQLite each time the page loads, using the same rules engine and Next Best Action logic as the project page.

- **Metrics**: Active projects, Needs attention, Open tasks, Human approvals, and Automated actions today (business-rule actions, with a 7-day bar chart). Each card links to the matching list.
- **Needs attention**: flagged, blocked or review projects, highest priority first. Each row shows the customer, the top open rule finding, the stage (step x of 6), priority, and the recommended action with its source (Rule / AI / AI + rules).
- **Operations pipeline**: project counts per workflow stage. Bars are colored by status; select a stage to open its projects.
- **Recent activity**: one timeline across project activity, Workflow Builder events and Automation Finder runs, filterable by AI / Rules / Human. AI entries from the Anthropic API carry a **Claude** badge with the response ID. Seeded entries say **Sample** and are not counted as AI actions.
- **Operational insights**: plain sentences built in Python from SQL counts (the most common attention reason, overdue tasks, approvals waiting, the most frequently missing document, workload by department, business-rule share of actions). Each has a **Source** note saying how it is calculated.
- **Summarize** (optional, real Anthropic API): sends only the verified insight sentences, with instructions not to add numbers or claims. Python then checks every number (digits and number words) against those sentences, and rejects trend words and formatting. A summary that fails is not shown, and the calculated insights stay on screen.
- **Pending approvals / Tasks due next**: the oldest waiting approvals, pending counts by type, and the next deadlines.

## AI Evaluation Lab

AI features are tested and measured, not assumed to work. The lab tests the **real project-analysis call** (`ai_service.analyze_project`, the same function the project page uses) against **10 synthetic test cases** with known answers.

**Each test case** is a fictional project record (service, bill, roof age, contact status, documents, customer notes) with:
- **Expected classification:** the accepted customer intents.
- **Expected important information:** items the model must report in its missing information or concerns (for example the utility bill, the HOA approval or the backup scope), plus items it must *not* claim are missing (for example a bill that is already verified).
- **Expected action category:** the accepted teams for the next step.

**A run** (Run AI Evaluation → Quick check 3 or Full 10):
1. OpsPilot builds each case's context exactly as in production: the same rules engine findings and the same context builder.
2. It sends one request per case to Claude and stores the validated structured output unchanged, with the response ID, latency and the token usage Anthropic reports.
3. Python compares the three fields. A case is **Passed** when all three pass, **Review required** when one does not, and **Failed** when two or more do not or the call failed. A failed call is stored as failed; no output is invented.
4. A person marks results **Correct**, **Partially correct** or **Incorrect**, with a note. Reviews are append-only.

**Metrics** are counted in Python from the stored expected and actual values. The model never grades itself.
- Classification accuracy: intents matched ÷ cases.
- Information detection accuracy: expected items handled correctly ÷ all expected items, including the "must not claim" ones.
- Recommendation agreement: accepted team ÷ cases.
- Cases requiring review: results that did not pass and have no human evaluation yet.

**Run history** stores the model, the prompt version (a fingerprint of the production prompt and schema), the time, the case count, every result and every human evaluation. It never stores the API key. **Compare with** shows the change per metric on the cases both runs share, and whether each case improved or regressed. Stored results cannot be edited (database trigger).

### Running it safely (API usage)
- **Nothing runs on its own.** Opening the page, or reloading it mid-run, never calls Claude. A run starts only after someone clicks **Run AI Evaluation** and confirms.
- **Small and predictable.** One request per case, at most 10 per run (`EVAL_MAX_CASES`). The confirmation shows the request count and a rough token estimate. The token usage Anthropic reports is stored with each result.
- **Start with a Quick check** (3 requests), or `python run_evaluation.py --dry-run` to see what would be sent without sending anything.
- **Server-side limits.** One run at a time, at least 60 s between runs (`EVAL_COOLDOWN_SECONDS`), and at most 10 runs per day (`EVAL_MAX_RUNS_PER_DAY`). They are checked on the server, so repeated clicks or scripts cannot get around them.
- **Stop any time.** "Stop after this case" takes effect before the next request. The run also stops by itself on authentication, quota, rate-limit or availability errors instead of retrying every case.
- A run left unfinished (tab closed) is not resumed automatically. The page offers **Continue** or **Stop**, and a run with no progress for 10 minutes is marked stopped.
- For a hard ceiling, also set a monthly spend limit in the Claude Console.

## Explainability ("Why?")

Every important AI output has a **Why?** button that opens a side panel:

| Where | What it explains |
|---|---|
| Project page: Next best action and AI analysis | The project analysis |
| Dashboard: Needs attention queue | The recommended action for that project |
| Approval Center: every item | Recommendations, decisions, message drafts and workflow proposals |
| Project page: Draft message | The draft just written |
| Automation Finder: each step | Why the step got its classification |
| Workflow Builder: header, overview and each step | The proposal, or one step |

The panel has four sections. Each has its own color, icon and label, so it is always clear where a statement comes from:

- **Evidence: fact from database** (blue). Values from SQLite: service, stage, status, priority, bill, roof age against the threshold, document statuses, contact details (on file or missing only), customer notes and open tasks. For a draft, the exact fields sent to Claude. Tags show which rule used a value and which values the AI cited.
- **AI interpretation** (indigo). Only the model's structured output: recommended action, owner, intent, summary, concerns and the short written justification. Each fact the AI cited is checked against the record and marked *Matches the record*, *Record shows…* (a contradiction) or *AI's reading* (no single field to check).
- **Business rules** (teal). The deterministic rules that fired, written as IF / THEN, with their result today (task status, *Still open* or *Resolved since*). Also the policy checks: AI task suggestions validated, information status set by rules, the confidence threshold, draft checks and the human-judgment policy. Rules that were checked but not triggered are listed in a collapsed section.
- **Human decision** (purple). Whether approval is required and why, what approve or reject does, and the recorded decision from the audit trail (who, when, where, their note), with a link to the Approval Center.

**Stored at analysis time.** Each analysis stores an `evidence_snapshot`: the project values, documents, contact status, open tasks, rule findings and actions, and which fields were sent to the model. If the record changes later, the panel still shows what the AI and the rules saw, marks changed values with **Now: …**, and shows a notice. Automation Finder steps also keep the AI's own justification when the policy reclassifies a step. Older analyses without a snapshot fall back to the current record and say so.

**No hidden reasoning.** OpsPilot never asks the model for its internal reasoning and never shows it. `GET /api/explain/<kind>/<id>` is read-only and never calls Claude. The panel is built from stored data and rendered with `textContent`.

## Human Approval Center

One place (**Approvals** in the sidebar) to review AI output before anything customer-facing or consequential happens. **Why?** Human approval is required before customer-facing or consequential actions.

**What lands in the queue**
| Filter | Items | Created by |
|---|---|---|
| Communications | Customer email drafts | **Draft communication** (here or **Draft message** on a project) → real Anthropic API |
| Recommendations | The latest pending AI next-step recommendation per project | Run AI analysis |
| Other | Recommendations that need human judgment (roof review, low confidence, unclear intent) | Run AI analysis |
| Workflows | Workflow proposals in **Pending Approval** | Request approval in the Workflow Builder |

Each item shows the AI recommendation, a short user-facing justification, the relevant business data (from the database, not from the model), confidence, OpsPilot checks, what happens if you approve or reject, and when it was created. OpsPilot never requests, stores or shows the model's internal reasoning.

**Decisions** (`approvals.decide`)
- **Approve**: carries out the effect. A message is marked approved. A recommendation creates a task, unless an open task already covers it. A workflow becomes **Active**.
- **Edit**: opens the original AI output beside an editable copy. Approving saves your version as the human correction.
- **Reject**: a message is discarded, a recommendation dismissed, and a workflow returns to **Draft**.

Every decision is logged as `HUMAN`, drafting as `AI`. Nothing is sent. Approving a message only marks it approved in this prototype.

**Audit trail**
- `approval_items.ai_output` holds the original AI output. A database trigger blocks any change to it.
- `approval_decisions` stores the decision, the original output, the human's final version, the changed fields, a note, who decided, where (Approval Center, Project page or Workflow Builder) and a timestamp. Triggers block updates and deletes, so it is append-only.
- **History** shows each decision with its record. Where a person edited, the original is shown next to the correction.

**What drafting sends to Claude** (`approvals.minimal_context`): the customer's first name, the service, the stage in plain words, the message goal, the names of missing documents or contact fields, whether a roof review is pending, whether the customer mentioned the roof, and the sign-off. It never sends the last name, email, phone, address, bill amount, roof age or notes. The exact payload is stored with the item and shown under **Data sent to Claude**.

**Draft checks** (Python, shown on the item): the greeting uses the first name, every missing item is mentioned, and it is signed "The Project Team". It has no prices, dates, guarantees or promises, and no placeholders. The length is 50–220 words. A failed check is flagged for the reviewer; it never silently changes the draft.

## Design system
All tokens and components live in `static/css/design-system.css`:
colors (neutrals, "grid teal" brand, semantic, and one color per actor: AI / Automation / Human), type scale (IBM Plex Sans, bundled under `static/fonts/` with its OFL license), 4px spacing scale, radius, shadows, motion; buttons, cards, badges, tables, forms, alerts, toasts, modals, tooltips (`data-tip="…"`), tabs, empty and loading states, stepper, timeline. Layout and breakpoints are in `static/css/layout.css`. No CDNs, so the app works offline.

## Demo walkthrough (about 3 minutes)
1. **Dashboard**: metrics, the Needs attention queue (reasons from the rules engine), the pipeline, and insights (every figure from SQLite). Click **Summarize** and point out "Every figure verified".
2. **Projects → John Smith**: rules already flagged two missing documents and roof age. Click **Run AI analysis**; point at the response ID. Click **Why?** to show evidence from the database, the AI's interpretation, the rules that fired and the human decision, each labelled by source.
3. **Approvals**: the new recommendation is waiting. Approve it; show the new task and the `HUMAN` entry in the timeline.
4. **Draft communication** for John Smith → *Acknowledge roof concern*. Click **Edit**, change the subject and approve. Open **History** to show the original AI output beside your correction (nothing is sent).
5. **Mark received** on the utility bill; automation closes its task.
6. **New project** with a missing phone number and a 22-year roof; rules flag it immediately.
7. **Evaluation Lab**: show the last run's metrics, open a failed case (expected vs actual, computed comparison), mark it Partially correct, and compare it with an earlier run. Run a Quick check live if there's time (3 requests).
