from __future__ import annotations

import io
import errno
import html
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Optional

from docx import Document
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pypdf import PdfReader
import stripe
from supabase import Client, create_client

app = FastAPI(title="CV Optimiser V2")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def log_requests(request: Request, call_next):
    start_time = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - start_time) * 1000)
        print(
            f"REQUEST_LOG: {request.method} {request.url.path} "
            f"status=500 duration_ms={duration_ms}"
        )
        raise

    duration_ms = int((time.perf_counter() - start_time) * 1000)
    print(
        f"REQUEST_LOG: {request.method} {request.url.path} "
        f"status={response.status_code} duration_ms={duration_ms}"
    )
    return response

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
SITE_URL = "https://www.cv-optimiser.com"
FREE_ANALYSES_PER_DAY = int(os.getenv("FREE_ANALYSES_PER_DAY", "3").strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
supabase_admin: Optional[Client] = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

FAQ_ENTRIES: list[tuple[str, str]] = [
    ("Do I need to create an account?", "No. You can run a free CV check without signing up."),
    ("Is my CV stored?", "No. Your CV is only used to generate your result and is not stored."),
    (
        "How does the CV score work?",
        "The score is based on keyword relevance, structure, role alignment and recruiter-style best practices.",
    ),
    (
        "What do I get with the full report?",
        "The full report gives you a detailed improvement plan, keyword optimisation and stronger CV wording.",
    ),
    (
        "Can I use this for any job?",
        "Yes. Paste the job description for the role you want and the tool will compare your CV against it.",
    ),
]

SEO_PAGES: dict[str, dict[str, Any]] = {
    "cv-checker": {
        "title": "Free CV Checker – Get Your CV Score in 30 Seconds",
        "meta_description": "Check your CV against a job description and get your score, missing keywords and top fixes instantly.",
        "h1": "Free CV Checker",
        "intro": "Use CV Optimiser to check how well your CV matches a role before you apply. Upload your CV, paste the job description, and get a practical score with keyword gaps and clear next fixes.",
        "bullets": [
            "See how relevant your CV looks for a specific job",
            "Spot missing keywords before recruiters do",
            "Get the top fixes that will improve your next application",
        ],
    },
    "ats-cv-checker": {
        "title": "Free ATS CV Checker – Find Missing Keywords Before You Apply",
        "meta_description": "Paste a job description and check whether your CV includes the keywords, structure and relevance recruiters expect.",
        "h1": "ATS CV Checker",
        "intro": "Check whether your CV is likely to survive ATS screening before a recruiter sees it. CV Optimiser compares your CV against a job description and highlights the missing signals that can hold you back.",
        "bullets": [
            "Find missing ATS keywords and phrases",
            "Understand whether your CV structure is helping or hurting",
            "Improve role alignment before you send your application",
        ],
    },
    "cv-keyword-optimiser": {
        "title": "CV Keyword Optimiser – Match Your CV to Any Job Description",
        "meta_description": "Find missing role-specific keywords and improve your CV before applying.",
        "h1": "CV Keyword Optimiser",
        "intro": "Match your CV to any job description with clearer keyword coverage and role-specific language. This page is designed for job seekers who want to improve relevance without stuffing their CV.",
        "bullets": [
            "Highlight the exact keywords your CV is missing",
            "Improve how closely your CV matches the role",
            "Get practical suggestions you can actually use",
        ],
    },
    "cv-improvement-tool": {
        "title": "CV Improvement Tool – Get Practical Fixes for Your CV",
        "meta_description": "Get practical feedback on your CV including structure, summary, keyword gaps and priority improvements.",
        "h1": "CV Improvement Tool",
        "intro": "Get a recruiter-style diagnosis of what is holding your CV back. CV Optimiser gives you a clear score, identifies weak areas, and shows the most important improvements to make first.",
        "bullets": [
            "See the top changes that will improve interview chances",
            "Understand structure, wording, and keyword gaps",
            "Use the free check before deciding whether to unlock the full report",
        ],
    },
}

SUPPORT_PAGES: dict[str, dict[str, Any]] = {
    "how-it-works": {
        "title": "How CV Optimiser works",
        "description": "Learn how CV Optimiser checks your CV against a job description, calculates your score, and highlights missing keywords and improvements.",
        "h1": "How CV Optimiser works",
        "intro": "CV Optimiser compares your CV against a job description to show how well it matches, what recruiters may miss, and what to improve.",
        "sections": [
            {
                "title": "What this tool does",
                "bullets": [
                    "Compares your CV to a job description",
                    "Calculates a match score",
                    "Highlights missing keywords",
                    "Shows the most important improvements to make",
                ],
            },
            {
                "title": "How your CV score is calculated",
                "copy": "Your CV score is based on a combination of:",
                "bullets": [
                    "Keyword match: whether your CV includes the terms used in the job description",
                    "Relevance: how closely your experience aligns with the role",
                    "Structure: clarity, organisation and readability",
                    "Recruiter best practices: how clearly your impact and achievements are shown",
                ],
                "helper": "The score is designed to reflect how likely your CV is to pass initial screening and attract attention.",
            },
            {
                "title": "What ATS systems and recruiters look for",
                "copy": "Many companies use Applicant Tracking Systems (ATS) to filter CVs before a recruiter reviews them.",
                "bullets": [
                    "Relevant keywords from the job description",
                    "Clear, readable formatting",
                    "Experience that matches the role",
                    "Evidence of impact (results, numbers, outcomes)",
                ],
                "helper": "If key information is missing or unclear, your CV may be filtered out before a human sees it.",
            },
            {
                "title": "What you get from your CV check",
                "bullets": [
                    "A CV match score",
                    "Missing keywords for the role",
                    "Top priority fixes",
                    "Feedback on structure and clarity",
                ],
                "helper": "The full report (Pro) includes deeper improvements, rewrites and keyword optimisation.",
            },
            {
                "title": "How to use CV Optimiser",
                "bullets": [
                    "1. Upload your CV or paste the text",
                    "2. Paste the job description",
                    "3. Get your CV score and improvement suggestions",
                ],
            },
            {
                "title": "See an example CV report",
                "copy": "Want to see the type of feedback before you try it?",
                "link_href": "/example-cv-report",
                "link_label": "View example CV report →",
            },
            {
                "title": "Check your own CV",
                "copy": "Upload your CV, paste a job description and get your score in under 60 seconds.",
                "cta_href": "/#mainToolCard",
                "cta_label": "Get my CV score",
            },
        ],
    },
    "features": {
        "title": "Features | CV Optimiser",
        "description": "Explore the main CV Optimiser features including CV scoring, keyword gap detection, ATS checks and recruiter-style feedback.",
        "h1": "CV Optimiser features",
        "intro": "CV Optimiser focuses on the parts of CV feedback that matter most when you are applying for a real job and need clear next steps.",
        "sections": [
            ("CV match score", "See how closely your CV aligns with the role before you apply."),
            ("Missing keyword detection", "Spot the role-specific terms your CV is missing or not supporting strongly enough."),
            ("Priority fixes", "Get the top improvements most likely to raise your interview chances."),
            ("Full report upgrade", "Unlock deeper feedback, stronger wording and a more detailed improvement plan when you need more help."),
        ],
    },
    "about": {
        "title": "About | CV Optimiser",
        "description": "Learn what CV Optimiser is built for and why it focuses on fast, practical CV feedback for real job applications.",
        "h1": "About CV Optimiser",
        "intro": "CV Optimiser was built for job seekers who want quick, useful CV feedback before applying. Instead of generic advice, the goal is to help you compare your CV against a specific role and see what needs to improve first.",
        "sections": [
            ("Built for real applications", "The tool is designed around the way recruiters and ATS systems evaluate relevance, clarity and evidence of fit."),
            ("Practical before perfect", "The focus is on actionable improvements you can actually use, not bloated reports or vague encouragement."),
            ("Free first value", "You can run a free check before deciding whether you want to save your result or unlock the full report."),
        ],
    },
    "privacy": {
        "title": "Privacy | CV Optimiser",
        "description": "Read how CV Optimiser handles your CV, job descriptions, account details and support messages.",
        "h1": "Privacy",
        "intro": "CV Optimiser processes the information you provide so it can analyse your CV, return your result, and support account and billing functions where needed.",
        "sections": [
            ("What we process", "This can include CV text, job descriptions, account details and support messages that you choose to provide."),
            ("Payments and support", "Payments are handled by Stripe and support forms are handled by Formspree."),
            ("Using your result responsibly", "You should review and edit generated suggestions before using them in any application."),
        ],
    },
    "terms": {
        "title": "Terms | CV Optimiser",
        "description": "Read the core terms for using CV Optimiser, including your responsibility for reviewing generated CV suggestions.",
        "h1": "Terms",
        "intro": "CV Optimiser provides CV improvement suggestions and analysis for informational purposes. You are responsible for checking that your final CV remains truthful, accurate and appropriate for the role.",
        "sections": [
            ("Using the tool", "The service is designed to help you improve your CV, but you remain responsible for all final application content."),
            ("Subscriptions", "If you choose Pro, subscriptions renew according to your Stripe billing settings until cancelled."),
            ("Final responsibility", "You should review all generated suggestions and make sure they accurately reflect your real experience and achievements."),
        ],
    },
}

EXAMPLE_REPORT_PAGE: dict[str, Any] = {
    "title": "Example CV Report | CV Optimiser",
    "description": "See an example CV Optimiser report with match score, missing keywords, priority fixes and rewrite suggestions.",
    "h1": "Example CV report",
    "intro": "See the type of feedback CV Optimiser gives before you run your own check.",
}


def require_openai() -> OpenAI:
    if not openai_client:
        raise HTTPException(status_code=500, detail="OpenAI not configured.")
    return openai_client


def require_supabase() -> Client:
    if not supabase_admin:
        raise HTTPException(status_code=500, detail="Supabase not configured.")
    return supabase_admin


def require_stripe():
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured.")
    stripe.api_key = STRIPE_SECRET_KEY
    return stripe


def parse_bearer_token(authorization: Optional[str]) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Missing bearer token.")
    return authorization.split(" ", 1)[1].strip()


def retry_transient(fn, attempts: int = 4, delay_seconds: float = 1.0):
    last_error = None
    for attempt in range(attempts):
        try:
            return fn()
        except OSError as e:
            last_error = e
            if getattr(e, "errno", None) == errno.EAGAIN:
                if attempt < attempts - 1:
                    time.sleep(delay_seconds)
                    continue
            raise
        except Exception as e:
            last_error = e
            if attempt < attempts - 1:
                time.sleep(delay_seconds)
                continue
            raise
    if last_error:
        raise last_error


def get_user_from_token(authorization: Optional[str]) -> dict[str, Any]:
    token = parse_bearer_token(authorization)
    user_result = require_supabase().auth.get_user(token)
    user = getattr(user_result, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session.")
    return {
        "id": user.id,
        "email": getattr(user, "email", None),
        "password_ready": get_profile_password_ready(user.id),
    }


def current_utc() -> datetime:
    return datetime.now(timezone.utc)


def start_of_today_utc() -> str:
    now = current_utc()
    start = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    return start.isoformat()


def extract_text_from_pdf(file_bytes: bytes) -> str:
    reader = PdfReader(io.BytesIO(file_bytes))
    return "\n".join([(page.extract_text() or "") for page in reader.pages]).strip()


def extract_text_from_docx(file_bytes: bytes) -> str:
    document = Document(io.BytesIO(file_bytes))
    return "\n".join([p.text for p in document.paragraphs if p.text.strip()]).strip()


def extract_text_from_txt(file_bytes: bytes) -> str:
    return file_bytes.decode("utf-8", errors="ignore").strip()


def extract_cv_text(filename: str, file_bytes: bytes) -> str:
    lower_name = filename.lower()
    if lower_name.endswith(".pdf"):
        return extract_text_from_pdf(file_bytes)
    if lower_name.endswith(".docx"):
        return extract_text_from_docx(file_bytes)
    if lower_name.endswith(".txt"):
        return extract_text_from_txt(file_bytes)
    if lower_name.endswith(".doc"):
        raise ValueError(".doc files are not supported yet. Please save as .docx or PDF.")
    raise ValueError("Unsupported file type. Please upload a PDF, DOCX, or TXT file.")


def build_prompt(job_description: str, cv_text: str, is_pro: bool = False) -> str:
    if is_pro:
        output_schema = """
{
  "score": 0,
  "matchedKeywords": [],
  "missingKeywords": [],
  "strongPoints": [],
  "weakPoints": [],
  "bulletPoints": [],
  "nextStep": "",
  "professionalSummary": "",
  "priorityFixes": [],
  "skillsSection": [],
  "atsTips": [],
  "interviewRisks": []
}
""".strip()
    else:
        output_schema = """
{
  "score": 0,
  "matchedKeywords": [],
  "missingKeywords": [],
  "strongPoints": [],
  "weakPoints": [],
  "bulletPoints": [],
  "nextStep": ""
}
""".strip()

    pro_instructions = """
Additional Pro rules (this must feel like a senior recruiter review, not generic AI output):

- professionalSummary:
  Write a tight, high-quality CV summary tailored to this specific job.
  It should position the candidate strongly for THIS role, not generic roles.

- priorityFixes:
  Exactly 3 (not more) high-impact improvements.
  These must be the most important changes that would increase interview chances.
  Each should be specific, practical, and immediately actionable.

- skillsSection:
  6–10 role-aligned skills phrased the way recruiters expect to see them.

- atsTips:
  3–5 concrete keyword or phrasing improvements based on the job description.

- interviewRisks:
  3–5 realistic concerns a hiring manager or recruiter would have.
  These should feel honest and insightful, not generic.

CRITICAL QUALITY RULES:
- Be specific to THIS job, not generic advice
- Do not repeat content across sections
- Avoid generic phrases like "results-driven" unless clearly supported
- Make the output feel like it was written by an experienced recruiter
- Prioritise clarity and usefulness over length
""".strip() if is_pro else ""

    return f"""
You are an expert UK CV writer and recruiter.

Return exactly one valid JSON object.
Do not include markdown.
Do not include code fences.
Do not include explanations before or after the JSON.
Do not include trailing commas.
Do not include comments.
Do not omit required keys.
Every key in the schema must be present.
Use empty arrays or empty strings if needed.

Use this exact JSON structure:

{output_schema}

Quality rules:
- score must be realistic, not inflated
- matchedKeywords must be short phrases clearly supported by the CV
- missingKeywords must be genuinely important role terms missing or weak in the CV
- strongPoints must explain what already helps this CV for this role
- weakPoints must explain what is vague, weak, missing, or likely to hurt shortlist chances
- bulletPoints must be improved CV bullet points, not advice bullets
- bulletPoints must sound stronger, clearer, and more commercially useful than the original CV
- prefer quantified impact only if supported by the CV
- never invent responsibilities, tools, employers, achievements, or metrics
- nextStep must be a short paragraph describing the single highest-value improvement to make next

{pro_instructions}

JOB DESCRIPTION:
{job_description}

CV:
{cv_text}
""".strip()


def infer_job_title(job_description: str) -> str:
    first_line = job_description.strip().splitlines()[0][:120]
    return first_line or "Untitled role"


def upsert_profile(user_id: str, email: Optional[str]) -> None:
    require_supabase().table("profiles").upsert({
        "id": user_id,
        "email": email,
        "updated_at": current_utc().isoformat(),
    }).execute()


def get_profile_password_ready(user_id: str) -> bool:
    result = (
        require_supabase()
        .table("profiles")
        .select("password_ready")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    if not rows:
        return False
    return bool(rows[0].get("password_ready"))


def set_profile_password_ready(user_id: str, value: bool = True) -> None:
    require_supabase().table("profiles").update(
        {"password_ready": value}
    ).eq("id", user_id).execute()


def get_active_subscription(user_id: str) -> Optional[dict[str, Any]]:
    result = (
        require_supabase()
        .table("subscriptions")
        .select("*")
        .eq("user_id", user_id)
        .in_("status", ["active", "trialing"])
        .order("updated_at", desc=True)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def save_subscription_for_user(
    user_id: str,
    stripe_customer_id: Optional[str],
    stripe_subscription_id: str,
    status: str,
) -> None:
    existing = (
        require_supabase()
        .table("subscriptions")
        .select("id")
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    )
    existing_rows = existing.data or []

    payload = {
        "user_id": user_id,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "status": status,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    if existing_rows:
        require_supabase().table("subscriptions").update(payload).eq("user_id", user_id).execute()
    else:
        require_supabase().table("subscriptions").insert(payload).execute()


def get_stripe_customer_id_for_user(user_id: str) -> Optional[str]:
    active_subscription = get_active_subscription(user_id)
    subscription_id = active_subscription.get("stripe_subscription_id") if active_subscription else None
    if not subscription_id:
        return None

    subscription = require_stripe().Subscription.retrieve(subscription_id)
    customer = getattr(subscription, "customer", None)
    if not customer:
        return None
    return str(customer)


def count_usage_today(user_id: str) -> int:
    result = (
        require_supabase()
        .table("usage_events")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("created_at", start_of_today_utc())
        .execute()
    )
    return result.count or 0


def save_usage_event(user_id: str) -> None:
    require_supabase().table("usage_events").insert({
        "user_id": user_id,
        "event_type": "analysis",
    }).execute()


def save_analysis_history(user_id: str, job_description: str, payload: dict[str, Any]) -> None:
    require_supabase().table("analysis_history").insert({
        "user_id": user_id,
        "job_title": infer_job_title(job_description),
        "score": payload.get("score", 0),
        "result_json": payload,
    }).execute()


def track_event(
    event_name: str,
    user_id: Optional[str] = None,
    email: Optional[str] = None,
    metadata: Optional[dict[str, Any]] = None,
) -> None:
    try:
        require_supabase().table("analytics_events").insert(
            {
                "user_id": user_id,
                "email": email,
                "event_name": event_name,
                "metadata": metadata or {},
            }
        ).execute()
    except Exception as e:
        print("TRACK EVENT ERROR:", repr(e))


def parse_openai_json_output(raw: str) -> dict[str, Any]:
    text = (raw or "").strip()
    if not text:
        raise ValueError("OpenAI returned empty output.")

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        fenced_match = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced_match:
            try:
                parsed = json.loads(fenced_match.group(1))
            except json.JSONDecodeError:
                parsed = None
        else:
            parsed = None

        if parsed is None:
            start = text.find("{")
            end = text.rfind("}")
            if start != -1 and end != -1 and end > start:
                candidate = text[start:end + 1]
                parsed = json.loads(candidate)
            else:
                raise ValueError("OpenAI output did not contain valid JSON.")

    if not isinstance(parsed, dict):
        raise ValueError("OpenAI output was valid JSON but not an object.")

    return parsed


def extract_json_object(raw_text: str) -> dict[str, Any]:
    text = (raw_text or "").strip()

    # First try direct JSON parse
    try:
        return json.loads(text)
    except Exception:
        pass

    # Try to extract the first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidate = match.group(0)
        try:
            return json.loads(candidate)
        except Exception:
            pass

    raise ValueError("Model did not return valid JSON.")


def repair_json_with_model(raw_text: str) -> dict[str, Any]:
    repair_prompt = f"""
You will be given malformed output that was intended to be a JSON object.

Your task:
- return exactly one valid JSON object
- do not include markdown
- do not include explanations
- do not change the meaning of the content
- if a field is missing, add it with an empty string or empty array as appropriate

Malformed output:
{raw_text}
""".strip()

    repaired = require_openai().responses.create(
        model=OPENAI_MODEL,
        input=repair_prompt,
        max_output_tokens=900,
    ).output_text.strip()

    print("OPENAI REPAIRED OUTPUT START")
    print(repaired)
    print("OPENAI REPAIRED OUTPUT END")

    return extract_json_object(repaired)


def coerce_string(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def coerce_string_list(value: Any, max_items: int = 5) -> list[str]:
    if not isinstance(value, list):
        return []

    items: list[str] = []
    for item in value:
        text = coerce_string(item)
        if text:
            items.append(text)
        if len(items) >= max_items:
            break
    return items


def normalize_analysis_data(data: dict[str, Any], is_pro: bool) -> dict[str, Any]:
    try:
        score = int(data.get("score", 0))
    except Exception:
        score = 0
    score = max(0, min(100, score))

    normalized = {
        "score": score,
        "matchedKeywords": coerce_string_list(data.get("matchedKeywords")),
        "missingKeywords": coerce_string_list(data.get("missingKeywords")),
        "strongPoints": coerce_string_list(data.get("strongPoints")),
        "weakPoints": coerce_string_list(data.get("weakPoints")),
        "bulletPoints": coerce_string_list(data.get("bulletPoints")),
        "nextStep": coerce_string(data.get("nextStep")),
    }

    if is_pro:
        normalized.update({
            "professionalSummary": coerce_string(data.get("professionalSummary")),
            "priorityFixes": coerce_string_list(data.get("priorityFixes")),
            "skillsSection": coerce_string_list(data.get("skillsSection")),
            "atsTips": coerce_string_list(data.get("atsTips")),
            "interviewRisks": coerce_string_list(data.get("interviewRisks")),
            "strongerBullets": coerce_string_list(data.get("strongerBullets")),
        })
    else:
        normalized.update({
            "professionalSummary": "",
            "priorityFixes": [],
            "skillsSection": [],
            "atsTips": [],
            "interviewRisks": [],
            "strongerBullets": [],
        })

    return normalized


def build_anonymous_result_preview(data: dict[str, Any]) -> dict[str, Any]:
    priority_fixes: list[str] = []

    for item in data.get("weakPoints", []):
        text = coerce_string(item)
        if text and text not in priority_fixes:
            priority_fixes.append(text)
        if len(priority_fixes) >= 2:
            break

    next_step = coerce_string(data.get("nextStep"))
    if next_step and next_step not in priority_fixes and len(priority_fixes) < 3:
        priority_fixes.append(next_step)

    short_summary = (
        "Your CV shows some relevant alignment, but the biggest gains will come from "
        "tightening role-specific evidence and closing the most obvious fit gaps."
    )

    if data.get("score", 0) >= 75:
        short_summary = (
            "Your CV looks broadly aligned for this role, with a few targeted changes likely "
            "to improve clarity and interview potential."
        )
    elif data.get("score", 0) <= 45:
        short_summary = (
            "Your CV is not yet strongly aligned to this role, so clearer keyword coverage "
            "and stronger evidence of fit should be the first priorities."
        )

    missing_keywords = data.get("missingKeywords", [])
    keyword_gap_insight = ""
    if missing_keywords:
        keyword_gap_insight = (
            f"One obvious gap is '{missing_keywords[0]}'. Add it only if it genuinely matches "
            "your experience, and support it with a concrete example."
        )

    return {
        "shortSummary": short_summary,
        "previewPriorityFixes": priority_fixes[:3],
        "keywordGapInsight": keyword_gap_insight,
    }


def get_plan_state(user_id: str) -> dict[str, Any]:
    active_subscription = get_active_subscription(user_id)
    if active_subscription:
        return {"plan": "pro", "is_pro": True, "remaining_free_analyses_today": None}
    used_today = count_usage_today(user_id)
    remaining = max(0, FREE_ANALYSES_PER_DAY - used_today)
    return {"plan": "free", "is_pro": False, "remaining_free_analyses_today": remaining}


def build_faq_json_ld() -> str:
    return json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "FAQPage",
            "mainEntity": [
                {
                    "@type": "Question",
                    "name": question,
                    "acceptedAnswer": {
                        "@type": "Answer",
                        "text": answer,
                    },
                }
                for question, answer in FAQ_ENTRIES
            ],
        }
    )


def build_software_json_ld(url: str) -> str:
    return json.dumps(
        {
            "@context": "https://schema.org",
            "@type": "SoftwareApplication",
            "name": "CV Optimiser",
            "applicationCategory": "BusinessApplication",
            "operatingSystem": "Web",
            "description": (
                "Free CV checker that compares your CV against a job description "
                "and highlights score, missing keywords and top fixes."
            ),
            "url": url,
        }
    )


def build_site_footer() -> str:
    return """
    <footer class="site-footer">
      <div class="site-footer-grid">
        <div class="site-footer-brand">
          <div class="site-footer-title">CV Optimiser</div>
          <p>Fast, practical CV feedback for job applications</p>
        </div>
        <div class="site-footer-links-group">
          <a href="/cv-checker">CV Checker</a>
          <a href="/ats-cv-checker">ATS CV Checker</a>
          <a href="/cv-keyword-optimiser">CV Keyword Optimiser</a>
          <a href="/cv-improvement-tool">CV Improvement Tool</a>
          <a href="/example-cv-report">Example Report</a>
        </div>
        <div class="site-footer-links-group">
          <a href="/how-it-works">How it works</a>
          <a href="/features">Features</a>
          <a href="/faq">FAQ</a>
          <a href="/privacy">Privacy</a>
          <a href="/terms">Terms</a>
          <a href="/about">About</a>
        </div>
      </div>
      <div class="site-footer-bottom">
        <span>© 2026 CV Optimiser</span>
        <span>Secure • Private • No CV storage</span>
      </div>
    </footer>
    """


def render_cv_checker_page() -> str:
    page_url = f"{SITE_URL}/cv-checker"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Free CV Checker | Compare Your CV to Any Job Description</title>
        <meta name="description" content="Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.">
        <link rel="canonical" href="{page_url}">
        <meta property="og:title" content="Free CV Checker | Compare Your CV to Any Job Description">
        <meta property="og:description" content="Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="Free CV Checker | Compare Your CV to Any Job Description">
        <meta name="twitter:description" content="Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        <style>
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
          .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .logo {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
          }}
          .logo-mark {{
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: 800;
            font-size: 15px;
          }}
          .logo-title {{
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
          }}
          .logo-title strong {{ font-weight: 800; }}
          .logo-title span {{ font-weight: 400; }}
          .header-link, .text-link, .site-footer a {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
          }}
          .hero {{
            display: grid;
            gap: 18px;
            margin-bottom: 24px;
          }}
          .hero h1 {{
            margin: 0;
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
          }}
          .hero p {{
            margin: 0;
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 16px;
            max-width: 760px;
          }}
          .layout {{
            display: grid;
            grid-template-columns: minmax(0, 1.45fr) minmax(280px, 0.9fr);
            gap: 24px;
            align-items: start;
          }}
          .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          h2 {{
            margin: 0 0 10px;
            font-size: 22px;
            color: #EEF3FF;
          }}
          p, li {{
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 15px;
          }}
          ul {{
            margin: 12px 0 0;
            padding-left: 20px;
          }}
          li {{
            margin-bottom: 8px;
          }}
          .section-stack {{
            display: grid;
            gap: 20px;
            margin-top: 24px;
          }}
          .tool-frame {{
            width: 100%;
            min-height: 1400px;
            border: 0;
            border-radius: 18px;
            background: transparent;
          }}
          .example-mini {{
            display: grid;
            gap: 12px;
          }}
          .example-mini strong {{
            color: #EEF3FF;
            font-size: 15px;
          }}
          .cta-block {{
            text-align: center;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            margin-top: 18px;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 800;
            text-decoration: none;
          }}
          .helper-note {{
            margin-top: 10px;
            color: #9FB0D4;
            font-size: 13px;
          }}
          .site-footer {{
            margin-top: 32px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.24);
          }}
          .site-footer-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
            align-items: start;
          }}
          .site-footer-title {{
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 6px;
          }}
          .site-footer-brand p {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
            margin: 0;
          }}
          .site-footer-links-group {{
            display: grid;
            gap: 8px;
          }}
          .site-footer a:hover, .text-link:hover, .header-link:hover {{
            color: #FFFFFF;
          }}
          .site-footer-bottom {{
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid rgba(80, 103, 146, 0.16);
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            color: #8FA3CD;
            font-size: 12px;
          }}
          @media (max-width: 900px) {{
            .layout, .site-footer-grid {{
              grid-template-columns: 1fr;
            }}
            .tool-frame {{
              min-height: 1650px;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#mainToolCard" class="header-link">Homepage tool</a>
          </div>

          <div class="hero">
            <h1>Free CV Checker</h1>
            <p>See how well your CV matches a job description and what to fix.</p>
          </div>

          <div class="layout">
            <div>
              <div class="card">
                <h2>Check my CV</h2>
                <p>Most CVs get rejected in seconds — not because of experience, but because they don’t match the job.</p>
                <p style="margin-top:12px;">Paste your CV and a job description below to get your match score and improvement suggestions.</p>
                <iframe class="tool-frame" src="/?embed_tool=1" title="CV checker tool"></iframe>
              </div>

              <div class="section-stack">
                <div class="card">
                  <h2>What this CV checker does</h2>
                  <p>This CV checker compares your CV against a job description to show:</p>
                  <ul>
                    <li>Your CV match score</li>
                    <li>Missing keywords for the role</li>
                    <li>What recruiters may miss</li>
                    <li>The most important improvements to make</li>
                  </ul>
                  <p style="margin-top:12px;">It’s designed to reflect how your CV is likely to perform in real job applications.</p>
                </div>

                <div class="card">
                  <h2>Why most CVs get rejected</h2>
                  <p>Many CVs are rejected before a recruiter reads them properly.</p>
                  <p style="margin-top:12px;">This usually happens because:</p>
                  <ul>
                    <li>Important keywords from the job description are missing</li>
                    <li>Experience isn’t clearly aligned to the role</li>
                    <li>Achievements are vague or not measurable</li>
                    <li>The CV doesn’t quickly show relevance</li>
                  </ul>
                  <p style="margin-top:12px;">Fixing these issues can significantly improve your chances of getting interviews.</p>
                </div>

                <div class="card">
                  <h2>How the CV check works</h2>
                  <ul>
                    <li>1. Upload your CV or paste the text</li>
                    <li>2. Paste the job description</li>
                    <li>3. Get your CV score and improvement suggestions</li>
                  </ul>
                  <a href="/how-it-works" class="text-link">Learn more about how it works →</a>
                </div>

                <div class="card">
                  <h2>What you get from your CV check</h2>
                  <ul>
                    <li>CV match score</li>
                    <li>Missing keywords</li>
                    <li>Top priority fixes</li>
                    <li>Feedback on clarity and relevance</li>
                  </ul>
                  <p style="margin-top:12px;">The full report includes deeper improvements and rewrite suggestions.</p>
                </div>
              </div>
            </div>

            <div class="section-stack">
              <div class="card">
                <h2>Example CV diagnosis</h2>
                <div class="example-mini">
                  <strong>Score: 58/100 — likely to be skipped</strong>
                  <div>
                    <strong>Missing keywords</strong>
                    <ul>
                      <li>stakeholder management</li>
                      <li>forecasting</li>
                      <li>commercial planning</li>
                    </ul>
                  </div>
                  <div>
                    <strong>Top fixes</strong>
                    <ul>
                      <li>Add measurable results</li>
                      <li>Strengthen your summary</li>
                      <li>Match role keywords</li>
                    </ul>
                  </div>
                </div>
                <a href="/example-cv-report" class="text-link">View full example report →</a>
              </div>

              <div class="card cta-block">
                <h2>Check your CV now</h2>
                <p>Upload your CV, paste a job description and get your score in under 60 seconds.</p>
                <a href="/#mainToolCard" class="cta">Get my CV score</a>
                <div class="helper-note">Prefer the homepage flow? The same tool is available there too.</div>
              </div>
            </div>
          </div>

          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_ats_cv_checker_page() -> str:
    page_url = f"{SITE_URL}/ats-cv-checker"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>ATS CV Checker | Improve Your CV for Applicant Tracking Systems</title>
        <meta name="description" content="Check how your CV performs in ATS systems. Identify missing keywords, improve your match score and increase interview chances.">
        <link rel="canonical" href="{page_url}">
        <meta property="og:title" content="ATS CV Checker | Improve Your CV for Applicant Tracking Systems">
        <meta property="og:description" content="Check how your CV performs in ATS systems. Identify missing keywords, improve your match score and increase interview chances.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="ATS CV Checker | Improve Your CV for Applicant Tracking Systems">
        <meta name="twitter:description" content="Check how your CV performs in ATS systems. Identify missing keywords, improve your match score and increase interview chances.">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        <style>
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
          .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .logo {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
          }}
          .logo-mark {{
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: 800;
            font-size: 15px;
          }}
          .logo-title {{
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
          }}
          .logo-title strong {{ font-weight: 800; }}
          .logo-title span {{ font-weight: 400; }}
          .header-link, .text-link, .site-footer a {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
          }}
          .hero {{
            display: grid;
            gap: 18px;
            margin-bottom: 24px;
          }}
          .hero h1 {{
            margin: 0;
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
          }}
          .hero p {{
            margin: 0;
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 16px;
            max-width: 760px;
          }}
          .layout {{
            display: grid;
            grid-template-columns: minmax(0, 1.45fr) minmax(280px, 0.9fr);
            gap: 24px;
            align-items: start;
          }}
          .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          h2 {{
            margin: 0 0 10px;
            font-size: 22px;
            color: #EEF3FF;
          }}
          p, li {{
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 15px;
          }}
          ul {{
            margin: 12px 0 0;
            padding-left: 20px;
          }}
          li {{
            margin-bottom: 8px;
          }}
          .section-stack {{
            display: grid;
            gap: 20px;
            margin-top: 24px;
          }}
          .tool-frame {{
            width: 100%;
            min-height: 1400px;
            border: 0;
            border-radius: 18px;
            background: transparent;
          }}
          .cta-block {{
            text-align: left;
          }}
          .site-footer {{
            margin-top: 32px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.24);
          }}
          .site-footer-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
            align-items: start;
          }}
          .site-footer-title {{
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 6px;
          }}
          .site-footer-brand p {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
            margin: 0;
          }}
          .site-footer-links-group {{
            display: grid;
            gap: 8px;
          }}
          .site-footer a:hover, .text-link:hover, .header-link:hover {{
            color: #FFFFFF;
          }}
          .site-footer-bottom {{
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid rgba(80, 103, 146, 0.16);
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            color: #8FA3CD;
            font-size: 12px;
          }}
          @media (max-width: 900px) {{
            .layout, .site-footer-grid {{
              grid-template-columns: 1fr;
            }}
            .tool-frame {{
              min-height: 1650px;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#mainToolCard" class="header-link">Homepage tool</a>
          </div>

          <div class="hero">
            <h1>ATS CV Checker</h1>
            <p>See how your CV performs in applicant tracking systems (ATS).</p>
          </div>

          <div class="layout">
            <div>
              <div class="card">
                <h2>Check your CV against ATS filters</h2>
                <p>Most companies use ATS software to filter CVs before a human sees them.</p>
                <p style="margin-top:12px;">If your CV doesn’t match the job description, it may never be reviewed.</p>
                <p style="margin-top:12px;">Use the tool below to check your CV against a job description and identify what’s missing.</p>
                <iframe class="tool-frame" src="/?embed_tool=1" title="ATS CV checker tool"></iframe>
              </div>

              <div class="section-stack">
                <div class="card">
                  <h2>What is an ATS CV check?</h2>
                  <p>An Applicant Tracking System (ATS) scans your CV for keywords, experience and relevance to the job description.</p>
                  <p style="margin-top:12px;">If your CV doesn’t match closely enough, it may be filtered out automatically.</p>
                </div>

                <div class="card">
                  <h2>Why ATS matters</h2>
                  <ul>
                    <li>Filters candidates before recruiters review them</li>
                    <li>Looks for keywords from the job description</li>
                    <li>Prioritises relevant experience</li>
                    <li>Rewards clear, structured CVs</li>
                  </ul>
                </div>

                <div class="card">
                  <h2>What you get</h2>
                  <ul>
                    <li>ATS match score</li>
                    <li>Missing keywords</li>
                    <li>CV improvement suggestions</li>
                    <li>Priority fixes</li>
                  </ul>
                </div>
              </div>
            </div>

            <div class="section-stack">
              <div class="card cta-block">
                <h2>See a full example CV report</h2>
                <p>Want to see the type of diagnosis and rewrite guidance before you run your own check?</p>
                <a href="/example-cv-report" class="text-link">See a full example CV report →</a>
              </div>
            </div>
          </div>

          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_example_report_page() -> str:
    page_url = f"{SITE_URL}/example-cv-report"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(EXAMPLE_REPORT_PAGE["title"])}</title>
        <meta name="description" content="{html.escape(EXAMPLE_REPORT_PAGE["description"])}">
        <link rel="canonical" href="{page_url}">
        <meta property="og:title" content="{html.escape(EXAMPLE_REPORT_PAGE["title"])}">
        <meta property="og:description" content="{html.escape(EXAMPLE_REPORT_PAGE["description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(EXAMPLE_REPORT_PAGE["title"])}">
        <meta name="twitter:description" content="{html.escape(EXAMPLE_REPORT_PAGE["description"])}">
        <style>
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 1040px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
          .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .logo {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
          }}
          .logo-mark {{
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: 800;
            font-size: 15px;
          }}
          .logo-title {{
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
          }}
          .logo-title strong {{ font-weight: 800; }}
          .logo-title span {{ font-weight: 400; }}
          .header-link, .site-footer a, .text-link {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 13px;
          }}
          .hero-card, .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          .hero-card {{
            margin-bottom: 24px;
          }}
          h1 {{
            margin: 0 0 12px;
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
          }}
          h2 {{
            margin: 0 0 10px;
            font-size: 20px;
            color: #EEF3FF;
          }}
          p, li {{
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 15px;
          }}
          .report-grid {{
            display: grid;
            grid-template-columns: 1.2fr 1fr;
            gap: 24px;
          }}
          .score-block {{
            padding: 18px 20px;
            border-radius: 18px;
            background: linear-gradient(135deg, rgba(91,120,255,0.18), rgba(18,31,58,0.92));
            border: 1px solid rgba(91,120,255,0.32);
            margin-bottom: 18px;
          }}
          .score-value {{
            font-size: 52px;
            font-weight: 850;
            color: #FFFFFF;
            line-height: 1;
            margin-bottom: 8px;
          }}
          .section-list {{
            margin: 0;
            padding-left: 20px;
          }}
          .section-list li {{
            margin-bottom: 8px;
          }}
          .blurred {{
            filter: blur(4px);
            opacity: 0.72;
            user-select: none;
          }}
          .locked-block {{
            position: relative;
            overflow: hidden;
          }}
          .pro-badge {{
            display: inline-flex;
            align-items: center;
            padding: 6px 10px;
            border-radius: 999px;
            font-size: 11px;
            font-weight: 700;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            color: #DDE6FF;
            background: rgba(91,120,255,0.14);
            border: 1px solid rgba(91,120,255,0.35);
          }}
          .before-after {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
          }}
          .before-after-card {{
            padding: 18px;
            border-radius: 16px;
            background: rgba(10, 19, 35, 0.34);
            border: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .cta-row {{
            display: flex;
            gap: 12px;
            flex-wrap: wrap;
            margin-top: 20px;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 800;
            text-decoration: none;
          }}
          .secondary-cta {{
            background: rgba(10, 19, 35, 0.34);
            border: 1px solid rgba(92, 112, 150, 0.22);
          }}
          .eyebrow {{
            display: inline-flex;
            align-items: center;
            gap: 8px;
            margin-bottom: 12px;
            color: #AFC0FF;
            font-size: 12px;
            font-weight: 700;
            letter-spacing: 0.08em;
            text-transform: uppercase;
          }}
          .eyebrow::before {{
            content: "";
            width: 8px;
            height: 8px;
            border-radius: 999px;
            background: #5B78FF;
          }}
          .section-helper {{
            margin-top: 12px;
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
          }}
          .keyword-chip-row {{
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
            margin-top: 14px;
          }}
          .keyword-chip {{
            display: inline-flex;
            align-items: center;
            padding: 8px 12px;
            border-radius: 999px;
            border: 1px solid rgba(92, 112, 150, 0.24);
            background: rgba(12, 23, 43, 0.8);
            color: #E6EEFF;
            font-size: 13px;
            font-weight: 600;
          }}
          .priority-grid {{
            display: grid;
            gap: 14px;
            margin-top: 14px;
          }}
          .priority-card {{
            display: grid;
            grid-template-columns: auto 1fr;
            gap: 14px;
            align-items: start;
            padding: 16px;
            border-radius: 16px;
            background: rgba(10, 19, 35, 0.34);
            border: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .priority-number {{
            width: 34px;
            height: 34px;
            border-radius: 12px;
            display: inline-flex;
            align-items: center;
            justify-content: center;
            background: rgba(91, 120, 255, 0.16);
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 800;
          }}
          .priority-card strong {{
            display: block;
            color: #EEF3FF;
            font-size: 15px;
            margin-bottom: 6px;
          }}
          .priority-card p {{
            margin: 0;
          }}
          .locked-list p {{
            margin: 0 0 10px;
          }}
          .cta-panel {{
            margin-top: 24px;
            text-align: center;
          }}
          .site-footer {{
            margin-top: 32px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.24);
          }}
          .site-footer-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
            align-items: start;
          }}
          .site-footer-title {{
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 6px;
          }}
          .site-footer-brand p {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
            margin: 0;
          }}
          .site-footer-links-group {{
            display: grid;
            gap: 8px;
          }}
          .site-footer a:hover, .text-link:hover {{
            color: #FFFFFF;
          }}
          .site-footer-bottom {{
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid rgba(80, 103, 146, 0.16);
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            color: #8FA3CD;
            font-size: 12px;
          }}
          @media (max-width: 900px) {{
            .report-grid, .before-after, .site-footer-grid {{
              grid-template-columns: 1fr;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#mainToolCard" class="header-link">Try the tool</a>
          </div>

          <div class="hero-card">
            <div class="eyebrow">Example report</div>
            <h1>{html.escape(EXAMPLE_REPORT_PAGE["h1"])}</h1>
            <p>{html.escape(EXAMPLE_REPORT_PAGE["intro"])}</p>
            <div class="cta-row">
              <a href="/#mainToolCard" class="cta">Get my CV score</a>
            </div>
          </div>

          <div class="report-grid">
            <div>
              <div class="card">
                <h2>Score overview</h2>
                <div class="score-block">
                  <div class="score-value">Match Score: 58/100</div>
                  <p><strong>Likely to be skipped unless improved</strong></p>
                  <p>This CV has relevant experience, but the strongest achievements are not obvious and several role-specific keywords are missing.</p>
                </div>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Missing keywords</h2>
                <div class="keyword-chip-row">
                  <span class="keyword-chip">stakeholder management</span>
                  <span class="keyword-chip">forecasting</span>
                  <span class="keyword-chip">commercial planning</span>
                  <span class="keyword-chip">P&amp;L</span>
                  <span class="keyword-chip">retailer execution</span>
                  <span class="keyword-chip">category growth</span>
                </div>
                <p class="section-helper">These are examples of keywords a recruiter or ATS may expect for this type of role.</p>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>What recruiters may miss</h2>
                <ul class="section-list">
                  <li>Commercial impact is not clear enough.</li>
                  <li>Summary does not closely match the target role.</li>
                  <li>Achievements are written as responsibilities rather than outcomes.</li>
                  <li>Important role keywords are missing or buried.</li>
                </ul>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Top priority fixes</h2>
                <div class="priority-grid">
                  <div class="priority-card">
                    <span class="priority-number">1</span>
                    <div>
                      <strong>Add measurable impact</strong>
                      <p>Replace vague responsibilities with outcomes, numbers and commercial results.</p>
                    </div>
                  </div>
                  <div class="priority-card">
                    <span class="priority-number">2</span>
                    <div>
                      <strong>Rewrite the summary around the target role</strong>
                      <p>The summary should immediately show why this CV fits the job description.</p>
                    </div>
                  </div>
                  <div class="priority-card">
                    <span class="priority-number">3</span>
                    <div>
                      <strong>Mirror important job description language</strong>
                      <p>Use relevant role keywords naturally so the CV feels aligned to the vacancy.</p>
                    </div>
                  </div>
                </div>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Example improvement</h2>
                <div class="before-after">
                  <div class="before-after-card">
                    <strong>Before</strong>
                    <p>Responsible for managing customer accounts and sales targets.</p>
                  </div>
                  <div class="before-after-card">
                    <strong>After</strong>
                    <p>Drove account growth by turning customer plans into measurable revenue opportunities, improving retailer execution and strengthening commercial performance.</p>
                  </div>
                </div>
              </div>
            </div>

            <div>
              <div class="card locked-block">
                <div style="display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:12px;">
                  <h2 style="margin:0;">Full report preview</h2>
                  <span class="pro-badge">PRO</span>
                </div>
                <div class="locked-list blurred">
                  <p>• Full rewritten professional summary</p>
                  <p>• Stronger bullet points</p>
                  <p>• Full keyword optimisation plan</p>
                  <p>• Export-ready improvement checklist</p>
                </div>
                <p>The free check gives you the score and top fixes. The full report helps you rewrite and improve the CV properly.</p>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Unlock the full report</h2>
                <p>Get the full rewrite, deeper fixes, stronger role-specific phrasing and a more complete improvement plan tailored to your own CV.</p>
                <div class="cta-row">
                  <a href="/#mainToolCard" class="cta">Unlock full report</a>
                </div>
              </div>
            </div>
          </div>

          <div class="card cta-panel">
            <h2>Check your own CV</h2>
            <p>Upload your CV, paste a job description and get your score in under 60 seconds.</p>
            <div class="cta-row" style="justify-content:center;">
              <a href="/#mainToolCard" class="cta">Get my CV score</a>
            </div>
          </div>

          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_seo_page(slug: str, page: dict[str, Any]) -> str:
    page_url = f"{SITE_URL}/{slug}"
    faq_html = "".join(
        f"""
        <div class="faq-item">
          <strong>{html.escape(question)}</strong>
          <p>{html.escape(answer)}</p>
        </div>
        """
        for question, answer in FAQ_ENTRIES
    )
    bullet_html = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in page["bullets"]
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])} | CV Optimiser</title>
        <meta name="description" content="{html.escape(page["meta_description"])}">
        <link rel="canonical" href="{page_url}">
        <meta property="og:title" content="{html.escape(page["title"])} | CV Optimiser">
        <meta property="og:description" content="{html.escape(page["meta_description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])} | CV Optimiser">
        <meta name="twitter:description" content="{html.escape(page["meta_description"])}">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        <script type="application/ld+json">{build_faq_json_ld()}</script>
        <style>
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 1100px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
          .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .logo {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
          }}
          .logo-mark {{
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: 800;
            font-size: 15px;
          }}
          .logo-title {{
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
          }}
          .logo-title strong {{
            font-weight: 800;
          }}
          .logo-title span {{
            font-weight: 400;
          }}
          .header-link {{
            color: #DCE5FF;
            font-size: 14px;
            font-weight: 700;
            text-decoration: underline;
            text-underline-offset: 2px;
          }}
          .layout {{
            display: grid;
            grid-template-columns: minmax(0, 1.7fr) minmax(280px, 1fr);
            gap: 24px;
          }}
          .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          h1 {{
            margin: 0 0 12px;
            font-size: clamp(2rem, 4vw, 3.1rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
          }}
          h2 {{
            margin: 0 0 12px;
            font-size: 20px;
            color: #EEF3FF;
          }}
          p, li {{
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 15px;
          }}
          .trust {{
            margin: 14px 0 18px;
            color: #DCE6FF;
            font-weight: 600;
            font-size: 14px;
          }}
          ul {{
            margin: 0;
            padding-left: 20px;
          }}
          li {{
            margin-bottom: 8px;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            margin-top: 18px;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 800;
            text-decoration: none;
          }}
          .helper {{
            margin-top: 10px;
            color: #9FB0D4;
            font-size: 13px;
          }}
          .faq-list {{
            display: grid;
            gap: 14px;
          }}
          .faq-item strong {{
            display: block;
            margin-bottom: 6px;
            color: #EEF3FF;
            font-size: 14px;
          }}
          .site-footer {{
            margin-top: 32px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.24);
          }}
          .site-footer-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
            align-items: start;
          }}
          .site-footer-title {{
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 6px;
          }}
          .site-footer-brand p {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
            margin: 0;
          }}
          .site-footer-links-group {{
            display: grid;
            gap: 8px;
          }}
          .site-footer a {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 13px;
          }}
          .site-footer a:hover {{
            color: #FFFFFF;
          }}
          .site-footer-bottom {{
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid rgba(80, 103, 146, 0.16);
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            color: #8FA3CD;
            font-size: 12px;
          }}
          @media (max-width: 900px) {{
            .layout {{
              grid-template-columns: 1fr;
            }}
            .site-footer-grid {{
              grid-template-columns: 1fr;
              gap: 16px;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#authCard" class="header-link">Sign in</a>
          </div>

          <div class="layout">
            <div class="card">
              <h1>{html.escape(page["h1"])}</h1>
              <p>{html.escape(page["intro"])}</p>
              <p class="trust">Free check • No signup required • Your CV isn’t stored</p>
              <h2>What this page helps you do</h2>
              <ul>{bullet_html}</ul>
              <a href="/#mainToolCard" class="cta">Try the free CV checker</a>
              <p class="helper">Use the main tool to upload your CV, paste a job description, and get your result instantly.</p>
            </div>

            <div class="card">
              <h2>What you get</h2>
              <ul>
                <li>CV match score</li>
                <li>Missing keywords</li>
                <li>Top priority fixes</li>
                <li>Improvement suggestions</li>
              </ul>
              <p class="helper">Built for job seekers who want fast, practical CV feedback.</p>
            </div>
          </div>

          <div class="card" style="margin-top:24px;">
            <h2>Frequently asked questions</h2>
            <div class="faq-list">{faq_html}</div>
          </div>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_faq_page() -> str:
    faq_html = "".join(
        f"""
        <div class="faq-item">
          <strong>{html.escape(question)}</strong>
          <p>{html.escape(answer)}</p>
        </div>
        """
        for question, answer in FAQ_ENTRIES
    )
    page_url = f"{SITE_URL}/faq"
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>FAQ | CV Optimiser</title>
        <meta name="description" content="Frequently asked questions about CV Optimiser, including free usage, privacy, CV scoring and the full report.">
        <link rel="canonical" href="{page_url}">
        <meta property="og:title" content="FAQ | CV Optimiser">
        <meta property="og:description" content="Frequently asked questions about CV Optimiser, including free usage, privacy, CV scoring and the full report.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="FAQ | CV Optimiser">
        <meta name="twitter:description" content="Frequently asked questions about CV Optimiser, including free usage, privacy, CV scoring and the full report.">
        <script type="application/ld+json">{build_faq_json_ld()}</script>
        <style>
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 960px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
          .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .logo {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
          }}
          .logo-mark {{
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: 800;
            font-size: 15px;
          }}
          .logo-title {{
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
          }}
          .logo-title strong {{ font-weight: 800; }}
          .logo-title span {{ font-weight: 400; }}
          .header-link, .site-footer a {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 13px;
          }}
          .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          h1 {{
            margin: 0 0 12px;
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
          }}
          p {{
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 15px;
          }}
          .faq-list {{
            display: grid;
            gap: 16px;
            margin-top: 20px;
          }}
          .faq-item strong {{
            display: block;
            margin-bottom: 6px;
            color: #EEF3FF;
            font-size: 15px;
          }}
          .site-footer {{
            margin-top: 32px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.24);
          }}
          .site-footer-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
            align-items: start;
          }}
          .site-footer-title {{
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 6px;
          }}
          .site-footer-brand p {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
            margin: 0;
          }}
          .site-footer-links-group {{
            display: grid;
            gap: 8px;
          }}
          .site-footer a:hover {{
            color: #FFFFFF;
          }}
          .site-footer-bottom {{
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid rgba(80, 103, 146, 0.16);
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            color: #8FA3CD;
            font-size: 12px;
          }}
          @media (max-width: 900px) {{
            .site-footer-grid {{
              grid-template-columns: 1fr;
              gap: 16px;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#authCard" class="header-link">Sign in</a>
          </div>
          <div class="card">
            <h1>Frequently asked questions</h1>
            <p>Answers about free usage, privacy, CV scoring and what you get with the full report.</p>
            <div class="faq-list">{faq_html}</div>
          </div>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_support_page(slug: str, page: dict[str, Any]) -> str:
    page_url = f"{SITE_URL}/{slug}"
    section_parts = []
    for section in page["sections"]:
        if isinstance(section, tuple):
            title, copy = section
            section_parts.append(
                f"""
                <div class="section-block">
                  <h2>{html.escape(title)}</h2>
                  <p>{html.escape(copy)}</p>
                </div>
                """
            )
            continue

        title = html.escape(section["title"])
        copy_html = f"<p>{html.escape(section['copy'])}</p>" if section.get("copy") else ""
        bullets_html = ""
        if section.get("bullets"):
            bullets_html = "<ul class=\"section-list\">" + "".join(
                f"<li>{html.escape(item)}</li>"
                for item in section["bullets"]
            ) + "</ul>"
        helper_html = (
            f"<p class=\"section-helper\">{html.escape(section['helper'])}</p>"
            if section.get("helper")
            else ""
        )
        link_html = (
            f"<a href=\"{html.escape(section['link_href'])}\" class=\"text-link\">{html.escape(section['link_label'])}</a>"
            if section.get("link_href") and section.get("link_label")
            else ""
        )
        cta_html = (
            f"<div class=\"section-cta\"><a href=\"{html.escape(section['cta_href'])}\" class=\"cta\">{html.escape(section['cta_label'])}</a></div>"
            if section.get("cta_href") and section.get("cta_label")
            else ""
        )
        section_parts.append(
            f"""
            <div class="section-block">
              <h2>{title}</h2>
              {copy_html}
              {bullets_html}
              {helper_html}
              {link_html}
              {cta_html}
            </div>
            """
        )
    sections_html = "".join(section_parts)
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])}</title>
        <meta name="description" content="{html.escape(page["description"])}">
        <link rel="canonical" href="{page_url}">
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["description"])}">
        <style>
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 960px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
          .topbar {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .logo {{
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
          }}
          .logo-mark {{
            width: 40px;
            height: 40px;
            border-radius: 12px;
            background: rgba(255, 255, 255, 0.04);
            border: 1px solid rgba(255, 255, 255, 0.18);
            display: inline-flex;
            align-items: center;
            justify-content: center;
            color: #fff;
            font-weight: 800;
            font-size: 15px;
          }}
          .logo-title {{
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
          }}
          .logo-title strong {{ font-weight: 800; }}
          .logo-title span {{ font-weight: 400; }}
          .header-link, .site-footer a {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 13px;
          }}
          .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          h1 {{
            margin: 0 0 12px;
            font-size: clamp(2rem, 4vw, 3rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
          }}
          h2 {{
            margin: 0 0 10px;
            font-size: 20px;
            color: #EEF3FF;
          }}
          p {{
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 15px;
          }}
          .section-list {{
            margin: 12px 0 0;
            padding-left: 20px;
            color: #B7C6E6;
          }}
          .section-list li {{
            margin-bottom: 8px;
            line-height: 1.7;
          }}
          .section-helper {{
            margin-top: 12px;
            color: #9FB0D4;
            font-size: 14px;
          }}
          .text-link {{
            display: inline-block;
            margin-top: 10px;
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 14px;
            font-weight: 700;
          }}
          .section-cta {{
            margin-top: 16px;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 800;
            text-decoration: none;
          }}
          .section-block + .section-block {{
            margin-top: 18px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.18);
          }}
          .site-footer {{
            margin-top: 32px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.24);
          }}
          .site-footer-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
            align-items: start;
          }}
          .site-footer-title {{
            color: #EEF3FF;
            font-size: 14px;
            font-weight: 700;
            margin-bottom: 6px;
          }}
          .site-footer-brand p {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
            margin: 0;
          }}
          .site-footer-links-group {{
            display: grid;
            gap: 8px;
          }}
          .site-footer a:hover {{
            color: #FFFFFF;
          }}
          .site-footer-bottom {{
            margin-top: 18px;
            padding-top: 14px;
            border-top: 1px solid rgba(80, 103, 146, 0.16);
            display: flex;
            justify-content: space-between;
            gap: 12px;
            flex-wrap: wrap;
            color: #8FA3CD;
            font-size: 12px;
          }}
          @media (max-width: 900px) {{
            .site-footer-grid {{
              grid-template-columns: 1fr;
              gap: 16px;
            }}
          }}
        </style>
      </head>
      <body>
        <div class="page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#authCard" class="header-link">Sign in</a>
          </div>
          <div class="card">
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {sections_html}
          </div>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


@app.get("/")
def home() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/cv-checker", response_class=HTMLResponse)
def cv_checker_page() -> str:
    return render_cv_checker_page()


@app.get("/ats-cv-checker", response_class=HTMLResponse)
def ats_cv_checker_page() -> str:
    return render_ats_cv_checker_page()


@app.get("/cv-keyword-optimiser", response_class=HTMLResponse)
def cv_keyword_optimiser_page() -> str:
    return render_seo_page("cv-keyword-optimiser", SEO_PAGES["cv-keyword-optimiser"])


@app.get("/cv-improvement-tool", response_class=HTMLResponse)
def cv_improvement_tool_page() -> str:
    return render_seo_page("cv-improvement-tool", SEO_PAGES["cv-improvement-tool"])


@app.get("/example-cv-report", response_class=HTMLResponse)
def example_cv_report_page() -> str:
    return render_example_report_page()


@app.get("/google4cffcb1da00a66a5.html")
def google_verification() -> PlainTextResponse:
    return PlainTextResponse("google-site-verification: google4cffcb1da00a66a5.html")


@app.get("/sitemap.xml")
def sitemap() -> Response:
    xml_content = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">

  <url>
    <loc>https://www.cv-optimiser.com/</loc>
  </url>

  <url>
    <loc>https://www.cv-optimiser.com/how-it-works</loc>
  </url>

  <url>
    <loc>https://www.cv-optimiser.com/example-cv-report</loc>
  </url>

  <url>
    <loc>https://www.cv-optimiser.com/cv-checker</loc>
  </url>

</urlset>
"""
    return Response(content=xml_content, media_type="application/xml")


@app.get("/faq", response_class=HTMLResponse)
def faq_page() -> str:
    return render_faq_page()


@app.get("/how-it-works", response_class=HTMLResponse)
def how_it_works_page() -> str:
    return render_support_page("how-it-works", SUPPORT_PAGES["how-it-works"])


@app.get("/features", response_class=HTMLResponse)
def features_page() -> str:
    return render_support_page("features", SUPPORT_PAGES["features"])


@app.get("/about", response_class=HTMLResponse)
def about_page() -> str:
    return render_support_page("about", SUPPORT_PAGES["about"])


@app.get("/success")
def success() -> FileResponse:
    return FileResponse("static/success.html")


@app.get("/cancel")
def cancel() -> FileResponse:
    return FileResponse("static/cancel.html")


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> str:
    return render_support_page("privacy", SUPPORT_PAGES["privacy"])


@app.get("/terms", response_class=HTMLResponse)
def terms_page() -> str:
    return render_support_page("terms", SUPPORT_PAGES["terms"])


@app.get("/billing", response_class=HTMLResponse)
def billing_page() -> str:
    return """
    <html>
      <head>
        <title>Billing & Cancellation | CV Optimiser</title>
        <style>
          body { font-family: Inter, Arial, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; line-height: 1.7; }
          h1,h2 { color: #FFFFFF; }
          a { color: #9AB0FF; }
          p, li { color: #C7D3EE; }
        </style>
      </head>
      <body>
        <h1>Billing & Cancellation</h1>
        <p>Pro subscriptions are billed through Stripe. You can manage or cancel your subscription from the account menu inside the app.</p>
        <p>If you need billing help, please use the support form.</p>
        <p><a href="/">Back to CV Optimiser</a></p>
      </body>
    </html>
    """


@app.get("/health")
def health() -> dict[str, str]:
    return {
        "status": "ok",
        "openai_configured": "yes" if OPENAI_API_KEY else "no",
        "supabase_configured": "yes" if (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY) else "no",
        "stripe_configured": "yes" if STRIPE_SECRET_KEY else "no",
    }


@app.post("/api/track")
async def api_track(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        event_name = (body.get("event_name") or "").strip()
        metadata = body.get("metadata") or {}

        if not event_name:
            return {"error": "Missing event_name"}

        if event_name == "signup_prompt_shown_after_result":
            print("CONVERSION_EVENT: signup_prompt_shown_after_result")

        user_id = None
        email = None

        auth_header = request.headers.get("Authorization")
        if auth_header:
            try:
                user = get_user_from_token(auth_header)
                user_id = user["id"]
                email = user["email"]
            except Exception:
                pass

        track_event(
            event_name=event_name,
            user_id=user_id,
            email=email,
            metadata=metadata,
        )
        return {"ok": True}
    except Exception as e:
        print("API TRACK ERROR:", repr(e))
        return {"error": str(e)}


@app.get("/api/admin/analytics")
def admin_analytics(limit: int = 100) -> dict[str, Any]:
    try:
        result = (
            require_supabase()
            .table("analytics_events")
            .select("created_at,event_name,email,metadata")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return {"items": result.data or []}
    except Exception as e:
        print("ADMIN ANALYTICS ERROR:", repr(e))
        return {"error": str(e)}


@app.get("/admin-analytics", response_class=HTMLResponse)
def admin_analytics_page() -> str:
    return """
    <html>
      <head>
        <title>Analytics | CV Optimiser</title>
        <style>
          body { font-family: Inter, Arial, sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; }
          h1 { margin-bottom: 18px; }
          iframe { width: 100%; height: 80vh; border: 1px solid rgba(80,103,146,0.35); border-radius: 16px; background: white; }
          p, a { color: #C7D3EE; }
        </style>
      </head>
      <body>
        <h1>Analytics</h1>
        <p>Open the raw analytics endpoint here:</p>
        <p><a href="/api/admin/analytics" target="_blank">/api/admin/analytics</a></p>
      </body>
    </html>
    """


@app.get("/api/me")
def api_me(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)
        upsert_profile(user["id"], user["email"])
        return {"user": user, "plan": get_plan_state(user["id"])}
    except Exception as e:
        return {"error": str(e)}


@app.get("/api/history")
def api_history(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)

        result = (
            require_supabase()
            .table("analysis_history")
            .select("id, job_title, score, created_at")
            .eq("user_id", user["id"])
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )

        return {"items": result.data or []}

    except Exception as e:
        return {"error": str(e)}


@app.post("/api/create-checkout-session")
def create_checkout_session(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    user = get_user_from_token(authorization)
    upsert_profile(user["id"], user["email"])
    if not get_profile_password_ready(user["id"]):
        return {
            "error": "Please create a password before upgrading to Pro.",
            "code": "PASSWORD_SETUP_REQUIRED"
        }
    track_event(
        event_name="upgrade_clicked",
        user_id=user["id"],
        email=user["email"],
        metadata={}
    )
    active_subscription = get_active_subscription(user["id"])
    if active_subscription:
        return {"error": "You already have an active subscription.", "code": "ALREADY_PRO"}
    if not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe price ID not configured.")

    session = require_stripe().checkout.Session.create(
    mode="subscription",
    success_url=f"{APP_BASE_URL}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}",
    cancel_url=f"{APP_BASE_URL}/cancel",
    line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
    customer_email=user["email"],
    client_reference_id=user["id"],
    metadata={"user_id": user["id"]},
)
    return {"url": session.url}


@app.post("/api/create-portal-session")
def create_portal_session(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)
        upsert_profile(user["id"], user["email"])

        if not STRIPE_SECRET_KEY:
            return {"error": "Stripe secret key not configured."}

        if not APP_BASE_URL:
            return {"error": "App base URL not configured."}

        customer_id = None

        active_subscription = get_active_subscription(user["id"])
        if active_subscription:
            customer_id = active_subscription.get("stripe_customer_id")

        if not customer_id:
            customers = require_stripe().Customer.list(email=user["email"], limit=1)
            if customers and getattr(customers, "data", None):
                customer_id = customers.data[0].id

        if not customer_id:
            return {"error": "No Stripe customer found for this account."}

        session = require_stripe().billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{APP_BASE_URL}/"
        )

        return {"url": session.url}

    except Exception as e:
        print("STRIPE PORTAL ERROR:", repr(e))
        return {"error": str(e)}


@app.post("/api/mark-password-ready")
def mark_password_ready(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)
        upsert_profile(user["id"], user["email"])
        set_profile_password_ready(user["id"], True)
        return {"ok": True}
    except Exception as e:
        print("MARK PASSWORD READY ERROR:", repr(e))
        return {"error": str(e)}


@app.post("/api/confirm-checkout-session")
def confirm_checkout_session(
    session_id: str,
    authorization: Optional[str] = Header(None)
) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)
        upsert_profile(user["id"], user["email"])

        if not STRIPE_SECRET_KEY:
            return {"error": "Stripe secret key not configured."}

        if not session_id:
            return {"error": "Missing session ID."}

        def load_and_save():
            checkout_session = require_stripe().checkout.Session.retrieve(
                session_id,
                expand=["subscription", "customer"]
            )

            if not checkout_session:
                raise ValueError("Checkout session not found.")

            payment_status = checkout_session.get("payment_status")
            if payment_status not in ["paid", "no_payment_required"]:
                raise ValueError(f"Checkout session not paid yet (status: {payment_status}).")

            session_email = checkout_session.get("customer_details", {}).get("email") or checkout_session.get("customer_email")
            if session_email and user["email"] and session_email.lower() != user["email"].lower():
                raise ValueError(f"Checkout email mismatch: {session_email} vs {user['email']}")

            customer = checkout_session.get("customer")
            subscription = checkout_session.get("subscription")

            stripe_customer_id = customer.get("id") if isinstance(customer, dict) else customer
            stripe_subscription_id = subscription.get("id") if isinstance(subscription, dict) else subscription
            stripe_subscription_status = subscription.get("status") if isinstance(subscription, dict) else "active"

            if not stripe_subscription_id:
                raise ValueError("No subscription found on this checkout session.")

            if stripe_subscription_status not in ["active", "trialing"]:
                raise ValueError(f"Subscription is not active yet (status: {stripe_subscription_status}).")

            save_subscription_for_user(
                user_id=user["id"],
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                status=stripe_subscription_status,
            )

            fresh = get_active_subscription(user["id"])
            if not fresh:
                raise ValueError("Subscription row was not saved correctly.")

            track_event(
                event_name="pro_activated",
                user_id=user["id"],
                email=user["email"],
                metadata={
                    "stripe_subscription_id": fresh.get("stripe_subscription_id"),
                    "subscription_status": fresh.get("status"),
                }
            )

            return {
                "ok": True,
                "plan": "pro",
                "subscription_status": fresh.get("status"),
                "stripe_subscription_id": fresh.get("stripe_subscription_id"),
            }

        return retry_transient(load_and_save, attempts=4, delay_seconds=1.2)

    except Exception as e:
        print("CONFIRM CHECKOUT ERROR:", repr(e))
        return {"error": "Activation is still processing. Please wait a few seconds and refresh once."}


@app.post("/api/stripe-webhook")
async def stripe_webhook(request: Request) -> JSONResponse:
    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured.")

    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")
    if not sig_header:
        raise HTTPException(status_code=400, detail="Missing Stripe signature.")

    event = require_stripe().webhooks.construct_event(
        payload=payload,
        sig_header=sig_header,
        secret=STRIPE_WEBHOOK_SECRET,
    )

    sb = require_supabase()

    if event.type == "checkout.session.completed":
        session = event.data.object
        metadata = getattr(session, "metadata", None) or {}
        user_id = metadata.get("user_id") if isinstance(metadata, dict) else getattr(session, "client_reference_id", None)
        stripe_subscription_id = getattr(session, "subscription", None)
        stripe_customer_id = str(getattr(session, "customer", None)) if getattr(session, "customer", None) else None

        if user_id and stripe_subscription_id:
            save_subscription_for_user(
                user_id=user_id,
                stripe_customer_id=stripe_customer_id,
                stripe_subscription_id=stripe_subscription_id,
                status="active",
            )

    elif event.type in {"customer.subscription.deleted", "customer.subscription.updated"}:
        subscription = event.data.object
        stripe_subscription_id = getattr(subscription, "id", None)
        stripe_subscription_status = getattr(subscription, "status", "canceled")
        stripe_customer_id = str(getattr(subscription, "customer", None)) if getattr(subscription, "customer", None) else None

        if stripe_subscription_id:
            existing = (
                sb.table("subscriptions")
                .select("user_id")
                .eq("stripe_subscription_id", stripe_subscription_id)
                .limit(1)
                .execute()
            )
            existing_rows = existing.data or []
            user_id = existing_rows[0].get("user_id") if existing_rows else None

            if user_id:
                save_subscription_for_user(
                    user_id=user_id,
                    stripe_customer_id=stripe_customer_id,
                    stripe_subscription_id=stripe_subscription_id,
                    status=stripe_subscription_status,
                )

    return JSONResponse({"received": True})


@app.post("/api/optimise")
async def optimise(
    request: Request,
    jobDescription: str = Form(""),
    cvText: str = Form(""),
    cvFile: Optional[UploadFile] = File(None),
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    try:
        job_description_preview = jobDescription.strip()
        cv_text_preview = cvText.strip()
        has_cv_file = bool(cvFile is not None and cvFile.filename)
        has_job_description = bool(job_description_preview)
        has_cv_text = bool(cv_text_preview)

        if not has_job_description and not has_cv_text:
            try:
                body = await request.json()
            except Exception:
                body = {}

            if isinstance(body, dict):
                has_job_description = bool(str(body.get("jobDescription", "") or "").strip())
                has_cv_text = bool(str(body.get("cvText", "") or "").strip())

        print("CONVERSION_EVENT: optimise_endpoint_hit")
        print(
            "OPTIMISE_DEBUG:",
            json.dumps(
                {
                    "timestamp": current_utc().isoformat(),
                    "path": request.url.path,
                    "method": request.method,
                    "cv_or_file_submitted": has_cv_file or has_cv_text,
                    "job_description_submitted": has_job_description,
                }
            ),
        )

        job_description = jobDescription.strip()
        cv_text = cvText.strip()

        if not job_description and not cv_text:
            try:
                body = await request.json()
            except Exception:
                body = {}

            if isinstance(body, dict):
                job_description = str(body.get("jobDescription", "") or "").strip()
                cv_text = str(body.get("cvText", "") or "").strip()

        user = None
        plan = None
        is_anonymous = True

        if authorization:
            try:
                user = get_user_from_token(authorization)
                is_anonymous = False
            except Exception as auth_error:
                print("OPTIMISE AUTH FALLBACK:", repr(auth_error))
                user = None
                is_anonymous = True

        if user:
            upsert_profile(user["id"], user["email"])
            plan = get_plan_state(user["id"])
            track_event(
                event_name="optimise_started",
                user_id=user["id"],
                email=user["email"],
                metadata={"is_pro": bool(plan["is_pro"])}
            )

            if not plan["is_pro"] and (plan["remaining_free_analyses_today"] or 0) <= 0:
                return {
                    "error": "You’ve used your free analyses for today. Upgrade to Pro for unlimited CV checks.",
                    "code": "PAYWALL",
                    "source": "error",
                    "plan": plan,
                }

        if not job_description or len(job_description) < 20:
            return {"error": "Please paste a fuller job description.", "source": "error"}

        if cvFile is not None and cvFile.filename:
            try:
                file_bytes = await cvFile.read()
                extracted_text = extract_cv_text(cvFile.filename, file_bytes)
            except ValueError as exc:
                return {"error": str(exc), "source": "error"}
            except Exception:
                return {"error": "Could not read that file. Try a different PDF, DOCX, or TXT file.", "source": "error"}

            if extracted_text:
                cv_text = extracted_text

        if not cv_text or len(cv_text) < 20:
            return {"error": "Please paste your CV text or upload a readable PDF, DOCX, or TXT file.", "source": "error"}

        raw = require_openai().responses.create(
            model=OPENAI_MODEL,
            input=build_prompt(job_description, cv_text, is_pro=bool(plan and plan["is_pro"])),
            max_output_tokens=1100,
        ).output_text.strip()

        print("OPENAI RAW OUTPUT START")
        print(raw)
        print("OPENAI RAW OUTPUT END")

        try:
            data = extract_json_object(raw)
        except Exception as e:
            print("JSON PARSE ERROR:", repr(e))
            try:
                data = repair_json_with_model(raw)
            except Exception as repair_error:
                print("JSON REPAIR ERROR:", repr(repair_error))
                return JSONResponse(
                    status_code=500,
                    content={"error": "Model returned invalid JSON"}
                )

        data = normalize_analysis_data(data, is_pro=bool(plan and plan["is_pro"]))

        payload = {
            "score": data.get("score", 0),
            "matchedKeywords": data.get("matchedKeywords", []),
            "missingKeywords": data.get("missingKeywords", []),
            "strongPoints": data.get("strongPoints", []),
            "weakPoints": data.get("weakPoints", []),
            "bulletPoints": data.get("bulletPoints", []),
            "nextStep": data.get("nextStep", ""),
            "professionalSummary": data.get("professionalSummary", ""),
            "priorityFixes": data.get("priorityFixes", []),
            "skillsSection": data.get("skillsSection", []),
            "atsTips": data.get("atsTips", []),
            "interviewRisks": data.get("interviewRisks", []),
            "source": "openai",
        }

        if user:
            save_usage_event(user["id"])
            save_analysis_history(user["id"], job_description, payload)
            payload["plan"] = get_plan_state(user["id"])
            track_event(
                event_name="optimise_succeeded",
                user_id=user["id"],
                email=user["email"],
                metadata={
                    "is_pro": bool(plan["is_pro"]),
                    "score": payload.get("score", 0),
                }
            )
        else:
            payload.update(build_anonymous_result_preview(data))
            payload["isAnonymousResult"] = True
            payload["signupPrompt"] = "Create a free account to save this result and unlock the full report."
            print("CONVERSION_EVENT: anonymous_result_generated")

        return payload
    except HTTPException:
        raise
    except Exception as e:
        print("OPTIMISE ERROR:", repr(e))
        track_event(
            event_name="optimise_failed",
            metadata={"error": str(e)}
        )
        return JSONResponse(
            status_code=500,
            content={"error": str(e)}
        )


app.mount("/static", StaticFiles(directory="static"), name="static")
