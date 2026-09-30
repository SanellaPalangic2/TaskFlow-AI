"""
Central configuration for OpsPilot AI.

Everything a demo operator might want to tune lives here. Secrets are never
stored in this file: the Anthropic key is read from the environment (.env) by
services/ai_service.py and nowhere else.
"""
import os
from pathlib import Path

import secrets

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent

# Load .env once, at import time. This only populates os.environ;
# the key itself is never copied into this module.
load_dotenv(BASE_DIR / ".env")

# --- Application -----------------------------------------------------------
# Signs flash-message cookies only. Without FLASK_SECRET_KEY a random key is generated at startup.
SECRET_KEY = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
DATABASE_PATH = Path(os.getenv("OPSPILOT_DB", BASE_DIR / "instance" / "opspilot.db"))

# Shown in the header as the signed-in user (placeholder, no auth in the prototype).
DEMO_USER = {"name": "Alex Rivera", "role": "Operations Manager", "initials": "AR"}

# --- Anthropic (Claude) -----------------------------------------------------
# A small, fast model keeps the live demo responsive. Override with ANTHROPIC_MODEL
# (for example claude-sonnet-5-5 for stronger answers at a higher cost).
AI_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
AI_TIMEOUT_SECONDS = float(os.getenv("ANTHROPIC_TIMEOUT_SECONDS", "45"))
AI_MAX_RETRIES = int(os.getenv("ANTHROPIC_MAX_RETRIES", "1"))

# AI Evaluation Lab cost controls. A run never starts on page load; it is started by a person.
EVAL_MAX_CASES = int(os.getenv("EVAL_MAX_CASES", "10"))              # hard cap: requests per run (max 10)
EVAL_COOLDOWN_SECONDS = int(os.getenv("EVAL_COOLDOWN_SECONDS", "60"))  # minimum time between runs
EVAL_MAX_RUNS_PER_DAY = int(os.getenv("EVAL_MAX_RUNS_PER_DAY", "10"))  # daily cap on runs

# --- Business rules (demo thresholds, not engineering guidance) -------------
ROOF_AGE_REVIEW_THRESHOLD = int(os.getenv("ROOF_AGE_REVIEW_THRESHOLD", "15"))

REQUIRED_CONTACT_FIELDS = ["email", "phone", "address"]

# Documents each service type needs before it can move past Document Review.
REQUIRED_DOCUMENTS = {
    "Solar": ["Utility Bill"],
    "Roofing": ["Roof Photos"],
    "Solar + Roofing": ["Utility Bill", "Roof Photos"],
    "Battery Storage": ["Utility Bill"],
    "Solar + Battery": ["Utility Bill"],
}

SERVICE_TYPES = list(REQUIRED_DOCUMENTS.keys())

DEPARTMENTS = ["Sales", "Customer Support", "Operations", "Project Review", "Scheduling"]

# Services where roof information (roof age) is needed before the project can move on.
ROOF_INFO_SERVICES = {"Solar", "Roofing", "Solar + Roofing", "Solar + Battery"}

# AI confidence below this always routes the analysis to a person.
HUMAN_REVIEW_CONFIDENCE_THRESHOLD = float(os.getenv("HUMAN_REVIEW_CONFIDENCE_THRESHOLD", "0.6"))
