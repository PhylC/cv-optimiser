from __future__ import annotations

import io
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional

from docx import Document
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
FREE_ANALYSES_PER_DAY = int(os.getenv("FREE_ANALYSES_PER_DAY", "3"))

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ID = os.getenv("STRIPE_PRICE_ID", "").strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
stripe_client = StripeClient(STRIPE_SECRET_KEY) if STRIPE_SECRET_KEY else None
supabase_admin: Optional[Client] = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


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


def get_user_from_token(authorization: Optional[str]) -> dict[str, Any]:
    token = parse_bearer_token(authorization)
    user_result = require_supabase().auth.get_user(token)
    user = getattr(user_result, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid session.")
    return {"id": user.id, "email": getattr(user, "email", None)}


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


def build_prompt(job_description: str, cv_text: str) -> str:
    return f"""
You are an expert UK CV and job application coach.

Your task:
1. Identify the 3 most important requirements in the job description.
2. Assess how well the CV proves each one.
3. Identify where the CV is underselling relevant experience.
4. Identify important missing evidence or missing skills.
5. Rewrite CV bullet points so they fit the target role better, using only information that already exists in the CV.
6. Do not invent experience, tools, employers, metrics, or achievements.

Return valid JSON only in this exact structure:

{{
  "score": 0,
  "matchedKeywords": [],
  "missingKeywords": [],
  "strongPoints": [],
  "weakPoints": [],
  "bulletPoints": [],
  "nextStep": ""
}}

Rules:
- Score should be realistic, not inflated.
- matchedKeywords should be short phrases clearly reflected in the CV.
- missingKeywords should be short phrases that matter for the job but are absent or weak.
- strongPoints should be specific observations about what the CV already does well for this role.
- weakPoints should be specific observations about what is missing, vague, or undersold.
- bulletPoints must be rewritten CV bullets, not advice bullets.
- Rewritten bullets must sound professional and tailored to the role.
- nextStep should be a short, practical paragraph explaining the highest-impact change to make next.
- Keep the output concise but useful.
- No markdown.
- No text outside the JSON.

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


def get_active_subscription(user_id: str) -> Optional[dict[str, Any]]:
    result = (
        require_supabase()
        .table("subscriptions")
        .select("*")
        .eq("user_id", user_id)
        .eq("status", "active")
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else None


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


def get_plan_state(user_id: str) -> dict[str, Any]:
    active_subscription = get_active_subscription(user_id)
    if active_subscription:
        return {"plan": "pro", "is_pro": True, "remaining_free_analyses_today": None}
    used_today = count_usage_today(user_id)
    remaining = max(0, FREE_ANALYSES_PER_DAY - used_today)
    return {"plan": "free", "is_pro": False, "remaining_free_analyses_today": remaining}


@app.get("/")
def home() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/success")
def success() -> FileResponse:
    return FileResponse("static/success.html")


@app.get("/cancel")
def cancel() -> FileResponse:
    return FileResponse("static/cancel.html")


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
            .select("id, job_title, score, result_json, created_at")
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

    if not STRIPE_PRICE_ID:
        raise HTTPException(status_code=500, detail="Stripe price ID not configured.")

    session = require_stripe().checkout.Session.create(
    mode="subscription",
    success_url=f"{APP_BASE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
    cancel_url=f"{APP_BASE_URL}/cancel",
    line_items=[{"price": STRIPE_PRICE_ID, "quantity": 1}],
    customer_email=user["email"],
    client_reference_id=user["id"],
    metadata={"user_id": user["id"]},
)
    return {"url": session.url}


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
        subscription_id = getattr(session, "subscription", None)
        customer_email = getattr(session, "customer_email", None)

        if user_id:
            sb.table("subscriptions").upsert({
                "user_id": user_id,
                "stripe_subscription_id": subscription_id,
                "status": "active",
                "email": customer_email,
                "updated_at": current_utc().isoformat(),
            }).execute()

    elif event.type in {"customer.subscription.deleted", "customer.subscription.updated"}:
        subscription = event.data.object
        subscription_id = getattr(subscription, "id", None)
        status = getattr(subscription, "status", "canceled")

        if subscription_id:
            sb.table("subscriptions").update({
                "status": "active" if status == "active" else "inactive",
                "updated_at": current_utc().isoformat(),
            }).eq("stripe_subscription_id", subscription_id).execute()

    return JSONResponse({"received": True})


@app.post("/api/optimise")
async def optimise(
    request: Request,
    jobDescription: str = Form(""),
    cvText: str = Form(""),
    cvFile: Optional[UploadFile] = File(None),
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    user = get_user_from_token(authorization)
    upsert_profile(user["id"], user["email"])
    plan = get_plan_state(user["id"])

    if not plan["is_pro"] and (plan["remaining_free_analyses_today"] or 0) <= 0:
        return {
            "error": "You’ve used your free analyses for today. Upgrade to Pro for unlimited CV checks.",
            "code": "PAYWALL",
            "source": "error",
            "plan": plan,
        }

    job_description = jobDescription.strip()
    cv_text = cvText.strip()

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
        input=build_prompt(job_description, cv_text),
    ).output_text.strip()

    data = json.loads(raw)

    payload = {
        "score": data.get("score", 0),
        "matchedKeywords": data.get("matchedKeywords", []),
        "missingKeywords": data.get("missingKeywords", []),
        "strongPoints": data.get("strongPoints", []),
        "weakPoints": data.get("weakPoints", []),
        "bulletPoints": data.get("bulletPoints", []),
        "nextStep": data.get("nextStep", ""),
        "source": "openai",
    }

    save_usage_event(user["id"])
    save_analysis_history(user["id"], job_description, payload)
    payload["plan"] = get_plan_state(user["id"])
    return payload


app.mount("/static", StaticFiles(directory="static"), name="static")
