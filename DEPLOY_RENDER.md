# Deploy OpsPilot to Render (via GitHub)

Files added for Render:
- `render.yaml`: the Render Blueprint (build command, start command, health check, environment variables).
- `gunicorn` in `requirements.txt`: the production web server Render runs. Locally you still use `python app.py`.

The app creates and seeds its SQLite database automatically on startup, so no setup step is needed on Render.

---

## 1. Put the project on GitHub

Create an **empty private repository** on GitHub, for example `opspilot-ai`, with no README.

Then, in the project folder:
```bash
git init
git add .
git commit -m "OpsPilot AI"
git branch -M main
git remote add origin https://github.com/YOUR-USERNAME/opspilot-ai.git
git push -u origin main
```

Before pushing, check that your key file is not included:
```bash
git status --ignored     # .env and instance/ must appear under "Ignored files"
git ls-files .env        # must print nothing
```
`.gitignore` already excludes `.env`, `venv/`, `instance/` (the database) and `__pycache__/`.

## 2. Create the service on Render

**Option A: Blueprint (uses `render.yaml`)**
1. Sign in at render.com and connect your GitHub account.
2. Click **New → Blueprint** and choose the `opspilot-ai` repository.
3. When asked for **ANTHROPIC_API_KEY**, paste your key. It is stored by Render, not in GitHub.
4. Click **Apply**. The first build takes a few minutes.

**Option B: manual Web Service**
1. Click **New → Web Service** and choose the repository.
2. Set **Runtime** to Python 3.
3. Set **Build Command** to `pip install -r requirements.txt`.
4. Set **Start Command** to `gunicorn app:app --workers 1 --threads 4 --timeout 120 --bind 0.0.0.0:$PORT`.
5. Under **Environment**, add:
   - `ANTHROPIC_API_KEY`: your key
   - `PYTHON_VERSION`: `3.11.9`
   - `FLASK_SECRET_KEY`: any long random text
6. Set **Health Check Path** to `/api/health`.

When it's live, open the `https://….onrender.com` URL. The sidebar should say **Claude connected**.

## 3. Things to know before the interview

- **The free plan sleeps.** After about 15 minutes without traffic the service stops, and the next visit takes 30–60 seconds to wake. Open the URL a few minutes before the interview.
- **The data resets on every deploy and restart.** Render's free filesystem is temporary, so the database is recreated from the synthetic demo data each time. For the demo this acts as an automatic reset. Keeping data between restarts needs a paid persistent disk.
- **Anyone with the URL can use it, and the AI calls are billed to your key.** The prototype has no login. Keep the URL private, set a **monthly spend limit** in the Claude Console, and suspend or delete the service after the interview. The Evaluation Lab has its own caps; the other AI buttons do not.
- **The key stays on Render's servers.** It is never sent to the browser. To change or revoke it, edit `ANTHROPIC_API_KEY` in the Render dashboard (**Environment**); the service restarts with the new value.
- **One worker, four threads.** SQLite works best with a single process. Several people can still use the app at once through the threads.

## 4. Updating

Push to `main` and Render redeploys automatically. Each redeploy starts again from the clean demo data.

**If the service was created before the switch to Claude:** in the Render dashboard, open the service → **Environment**, add `ANTHROPIC_API_KEY` (your key from console.anthropic.com, starting with `sk-ant-`), delete the old `OPENAI_API_KEY` and `OPENAI_MODEL` entries, and save. Then push the new code. Optional: add `ANTHROPIC_MODEL` to choose another Claude model; the default is `claude-haiku-4-5-20251001`.
