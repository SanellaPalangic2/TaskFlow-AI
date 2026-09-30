# OpsPilot AI: Interview demo guide

OpsPilot is an internal operations prototype for a residential solar and roofing company. It shows how **deterministic business rules**, the **real Anthropic API** and **human decisions** work together, with every action logged and attributed. All customers and projects are **synthetic**.

---

## How to start

Requires Python 3.10 or newer.

**Windows (PowerShell)**
```powershell
cd opspilot-ai
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env          # then add your key (see API setup)
python verify_ai.py             # checks the real Claude connection
python app.py
```

**macOS / Linux**
```bash
cd opspilot-ai
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # then add your key (see API setup)
python verify_ai.py
python app.py
```

Open **http://127.0.0.1:5000**. Stop the app with **Ctrl+C**.

If PowerShell blocks `Activate.ps1`, run `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass` once in that window. If port 5000 is busy, the app says so; close the other program first.

---

## API setup

1. Open `.env` (created from `.env.example`) in a text editor.
2. Replace `your_api_key_here` so the line reads `ANTHROPIC_API_KEY=` followed by your key (it starts with `sk-ant-`). Save the file.
3. Run `python verify_ai.py`. It sends one small request and prints Anthropic's **message ID**, model, latency and token counts. It never prints the key.
4. Start the app. The sidebar should say **Claude connected**.

How the key is protected:
- It is read from the environment on the server only (`services/ai_service.py`). It is never placed in HTML or JavaScript, stored in SQLite, logged, or returned by any endpoint. Error messages never include it.
- `.env` is in `.gitignore`. If you use Git, confirm before pushing:
  - `git check-ignore -v .env` should print a line that ends in `.env`.
  - `git ls-files .env` should print nothing.
  - `git log --all --oneline -S "sk-ant-"` should print nothing. It lists matching commits only, without showing their contents.
- Never paste the key into chat, screenshots or slides. If it is ever exposed, revoke it in the Claude Console (console.anthropic.com → API keys) and create a new one.
- Optional: set a monthly spend limit in the Claude Console for a hard spending ceiling.

---

## Demo reset

```bash
python reset_demo_data.py --yes
```
This restores the clean synthetic dataset: 14 projects (7 need attention), 21 open tasks, 2 pending approvals and 10 evaluation cases. It changes only `instance/opspilot.db` and saves a backup to `instance/backups/` first. It makes no API calls. It is safe to run with the app running; if Windows reports the file is in use, stop the app, run it again and restart.

### Optional: populate the AI pages with real results (run once, before the interview)
These use the real API. Each request is small, but each command costs money, so run them once, not on every rehearsal.

| Command | Requests | Fills |
|---|---|---|
| `python run_evaluation.py` | 10 | Evaluation Lab: one full run with real metrics |
| `python run_workflow_examples.py` | 5 | Workflows: 3 sample designs saved as Drafts |
| `python run_automation_examples.py` | 6 | Automation Finder: 3 saved analyses |

`python run_evaluation.py --dry-run` shows what would be sent without calling the API.

---

## Pre-interview checklist (5 minutes before)
1. `python reset_demo_data.py --yes`
2. (Optional, once) the three commands above.
3. `python verify_ai.py`: expect **SUCCESS** and a response ID.
4. `python app.py`, then open http://127.0.0.1:5000 in a fresh browser window at 100% zoom.
5. Close other tabs and notifications. Keep the terminal hidden; it shows request logs, never the key.

---

## 3-minute demo

Say at the start: *"Everything here is synthetic data. The AI calls are real."*

1. **Dashboard (20 s).** Point out the five metric cards, the **Needs attention** queue ("what needs my attention right now"), the **Operations pipeline** and **Operational insights**. Say: *"Every number is calculated from the database. No figure on this page is written by AI."*
2. In **Needs attention**, click the **John Smith** row. The project page opens.
3. **Business rules (20 s).** Point at **Next best action**: the rules have already flagged the 19-year roof and two missing documents. Scroll to **Open tasks**: each shows **Business rule**. Say: *"Rules are deterministic, so the same record always gives the same result."*
4. **Real AI (30 s).** In the **AI analysis** card, click **Analyze project**. It takes a few seconds. Point at **Live from Claude**, the detected intent, the concerns found in the customer's notes, and the response ID at the bottom of the card.
5. **Explainability (30 s).** In **Next best action**, click **Why?**. Walk through the four color-coded sections: **Evidence** (facts from the database), **AI interpretation** (each cited fact checked against the record), **Business rules** (which rules fired), and **Human decision**. Press **Esc** to close.
6. **Human approval (20 s).** Click **Approve** in Next best action, then **Approve** in the dialog. Point at the toast and at the new **Human** entry at the bottom of the **Activity timeline**.
7. **Customer message (30 s).** Click **Draft message** (top right), choose **Acknowledge roof concern**, and click **Draft with AI**. Read the draft, change a word, and click **Approve message**. Say: *"AI never sends anything. Approving only marks it approved, and the original AI draft is kept in the audit trail."*
8. **Audit trail (15 s).** In the sidebar, click **Approvals**, then **History**. Click the message: the original AI output sits next to your correction, with who, when and where.
9. **Close (15 s).** In the sidebar, click **Evaluation Lab**. Say: *"AI features are tested, not assumed. Ten synthetic cases with known answers run through the same production call, Python scores them, and a person reviews the disagreements."* If you ran `run_evaluation.py` beforehand, click **Details** on a *Review required* row to show expected against actual.

---

## 5-minute demo

Do steps 1–8 of the 3-minute demo, then:

9. **Operations Copilot (40 s).** In the sidebar, click **Copilot**, then the suggestion **Which projects need attention?**. Point at **Grounded in OpsPilot data**, the linked projects, and **How this was answered**, which lists the exact read-only queries. Then type *"What's the weather tomorrow?"*. It answers *"I don't have enough information in OpsPilot to answer that."*
10. **Automation Opportunity Finder (40 s).** Click **Automation Finder**, then the example **Document intake**, then **Analyze process**. Point at the step classifications, the purple box where OpsPilot's policy kept a person in charge of decisions, and the metrics, which are counts calculated in Python rather than AI estimates. Click **Why?** on a step marked *Human decision*.
11. **Workflow Builder (40 s).** Click **Workflows**, then **New workflow**, then the example **Customer message triage**, then **Generate workflow**. Click a step to see its details. Click **Save draft**, then **Request approval** and confirm with **Request approval** in the dialog. Say: *"It cannot become Active until a person approves it in the Approval Center."*
12. **Evaluation Lab (30 s).** Click **Run AI Evaluation**, choose **Quick check: 3 cases** (3 requests), and click **Start run**. Rows fill in as each result arrives. Open one with **Details**, choose **Partially correct**, and click **Save evaluation**. If an earlier run exists, pick it under **Compare with**.
13. **Business rule automation (10 s).** On John Smith's project, click **Mark received** next to **Utility Bill**. Automation closes its task and the timeline shows the rule.

If an AI call fails during the demo (network, rate limit), the card shows a clear error with **Retry**, and nothing is invented. That is itself worth pointing out.

---

## Architecture

```
Browser (HTML, CSS, vanilla JS)  ── never sees the API key
        │  page requests + small JSON calls
        ▼
Flask (app.py) ── routes only
   ├── services/rules_engine.py   deterministic business rules (tasks, flags, priority)
   ├── services/workflow.py       every state change + activity log (AI / AUTOMATION / HUMAN)
   ├── services/intelligence.py   checks AI output against rules; Next best action
   ├── services/ai_service.py     the ONLY file that calls Claude (Messages API, strict JSON schemas)
   ├── services/copilot.py        10 read-only query tools the model may call
   ├── services/approvals.py      Human Approval Center, audit trail
   ├── services/explain.py        "Why?" explanations from stored data
   ├── services/evaluation.py     Evaluation Lab
   └── database.py                SQLite (built-in sqlite3), schema upgrades in place
```

- **Structured outputs:** every Claude call must answer through a tool whose input is a strict JSON schema (Pydantic). Python then validates the result again, checking ranges, lengths and allowed values. Anything invalid is rejected with an error, never repaired with made-up content.
- **Grounding:** the Copilot can only call 10 predefined, parameterized, read-only queries. The model never writes SQL.
- **Minimal data:** customer message drafts send only a first name, the service, the stage and the missing items. No last name, email, phone, address or notes.

---

## AI vs automation vs human

| | Does | Never does |
|---|---|---|
| **Business rules** (Python) | Check contact details, required documents and roof age; create and close tasks; flag status; raise priority; validate AI suggestions | Guess, interpret free text, or move a project's stage |
| **AI** (Claude, via the Anthropic API) | Read customer notes; summarize; detect intent and concerns; suggest a next action; draft messages; propose workflows; classify process steps; answer questions from retrieved data | Change project data, create tasks, send anything, make engineering, safety, legal or financial calls, or grade itself |
| **People** | Approve, edit or reject AI output; move stages; mark documents received; evaluate AI quality | Nothing is hidden from them: every AI and rule action is in the timeline |

Rule of thumb: **rules for the predictable, AI for the unstructured, people for the consequential.** Every activity entry is tagged AI, Business rule or Human, and AI entries carry Anthropic's message ID.

---

## Likely interview questions (with short answers)

1. **Why use deterministic rules at all if you have an LLM?** Rules are predictable, testable and free. The AI handles what rules can't: free-text notes and intent. Rules run the same way whatever the AI says, so an AI mistake can't create or remove a required task.
2. **How do you stop the model from inventing things?** Strict JSON schemas, a second validation pass in Python, facts cross-checked against the database (the "Why?" panel marks mismatches), and fixed fallback sentences for ungrounded Copilot answers.
3. **What happens when the Anthropic API fails?** Each error type (missing key, bad key, rate limit, quota, timeout, server error, invalid output, refusal) maps to a plain-language message with Retry. Nothing is saved, and no fallback content is invented.
4. **How is the API key protected?** It is read on the server only and never reaches the browser, the database, the logs or any response. `.env` is gitignored. It was tested by scanning every response, the logs and the database file for a test key.
5. **How does the Copilot avoid SQL injection or data leaks?** The model can only call 10 predefined tools with validated arguments, and the SQL is fixed and parameterized. Unknown tools or bad arguments are rejected before any query runs.
6. **Why structured outputs rather than free text?** They can be validated, stored, compared and tested. Free text can't be scored reliably.
7. **How do you measure whether the AI is any good?** The Evaluation Lab runs 10 synthetic cases through the production call. Python compares intent, required information and the suggested team against expected values, and people review disagreements. Runs are stored and compared by model and prompt version.
8. **Why not let the AI grade its own answers?** It is circular and unreliable. Metrics come from deterministic comparisons, and quality judgment comes from people.
9. **How do you keep humans in control?** AI output is stored as a pending suggestion. Customer-facing and consequential actions go through the Approval Center, and approving a message only marks it approved. A human-judgment policy moves approvals, money, eligibility, legal and safety steps to people.
10. **How is the audit trail protected?** The original AI output can't be changed (a SQLite trigger blocks updates), and decisions and evaluations are append-only. Edits are stored beside the original, never over it.
11. **How do you explain a recommendation without exposing chain-of-thought?** The model returns a short user-facing justification and structured fields. The "Why?" panel combines those with a snapshot of the database facts taken at analysis time and the rules that fired. OpsPilot never asks for or stores internal reasoning.
12. **What data do you send to Claude?** Only what each task needs. Project analysis sends the project facts and notes, with contact details reduced to "on file" or "missing". Message drafts send a first name, service, stage and missing items only.
13. **How would this scale?** Move to PostgreSQL, send AI calls through a background job queue, cache read-only aggregates, and run behind a production WSGI server with several workers.
14. **How do you control API cost?** Nothing calls the API on page load. Calls are user-initiated, and the Evaluation Lab has a per-run cap, a cooldown, a daily cap and a stop button. Token usage is recorded per result.
15. **What would you test next?** A larger labelled evaluation set, from real, anonymized and consented data. Also prompt-injection cases in customer notes, regression runs on every prompt change, and load tests.

---

## Production limitations (honest list)

- **No authentication or roles.** There is a single demo user. A real deployment needs SSO, role-based permissions (who may approve what) and per-user audit identities.
- **No CSRF protection or rate limiting** on forms and JSON endpoints. Both are required before exposing it on a network.
- **Development server.** `python app.py` runs Flask's development server. Production needs a WSGI server (gunicorn or waitress), HTTPS, and a secrets manager instead of `.env`.
- **SQLite with in-place upgrades.** Fine for a demo. Production needs PostgreSQL, real migrations (for example Alembic), backups and retention policies.
- **Synchronous AI calls.** Requests wait for Claude. Production should use a job queue, timeouts per user, retries with backoff and observability (latency, error rate, cost dashboards).
- **Privacy and compliance.** Customer notes are sent to Claude for analysis. A company would need a data processing agreement, retention settings, PII redaction and customer consent.
- **Prompt injection.** Customer notes are untrusted input. Schemas and rules limit the damage, but a production system needs dedicated injection tests and filtering.
- **Small, synthetic evaluation set.** Ten cases show the method, not statistical confidence. Real evaluation needs hundreds of labelled, representative cases and inter-reviewer agreement.
- **Nothing is executed.** Messages are never sent and workflows are never run. Integrations (email, CRM, scheduling) would each need their own approval and failure handling.
- **Automated tests live outside the app.** The project ships real-API check scripts (`verify_ai.py`, `run_*.py`). A production codebase needs a unit and integration test suite in CI, with the Anthropic client mocked.
- **Accessibility and browser support** have been checked informally (keyboard focus, contrast, laptop and phone widths) but not audited formally.
