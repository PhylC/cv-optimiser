from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional

from docx import Document
from fastapi import FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
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

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://127.0.0.1:8000").strip().rstrip("/")
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


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> str:
    return """
    <html>
      <head>
        <title>Privacy | CV Optimiser</title>
        <style>
          body { font-family: Inter, Arial, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; line-height: 1.7; }
          h1,h2 { color: #FFFFFF; }
          a { color: #9AB0FF; }
          p, li { color: #C7D3EE; }
        </style>
      </head>
      <body>
        <h1>Privacy Policy</h1>
        <p>CV Optimiser processes the information you provide, including CV text, job descriptions, account details, and support messages, to deliver the service.</p>
        <p>Payments are handled by Stripe. Support forms are handled by Formspree. We do not display your email address publicly.</p>
        <p>You are responsible for reviewing and editing generated suggestions before using them.</p>
        <p><a href="/">Back to CV Optimiser</a></p>
      </body>
    </html>
    """


@app.get("/terms", response_class=HTMLResponse)
def terms_page() -> str:
    return """
    <html>
      <head>
        <title>Terms | CV Optimiser</title>
        <style>
          body { font-family: Inter, Arial, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; line-height: 1.7; }
          h1,h2 { color: #FFFFFF; }
          a { color: #9AB0FF; }
          p, li { color: #C7D3EE; }
        </style>
      </head>
      <body>
        <h1>Terms of Use</h1>
        <p>CV Optimiser provides CV improvement suggestions and analysis for informational purposes. You remain responsible for checking that all final content is truthful and appropriate.</p>
        <p>Subscriptions renew according to your Stripe billing settings until cancelled.</p>
        <p><a href="/">Back to CV Optimiser</a></p>
      </body>
    </html>
    """


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

        checkout_session = require_stripe().checkout.Session.retrieve(
            session_id,
            expand=["subscription", "customer"]
        )

        if not checkout_session:
            return {"error": "Checkout session not found."}

        payment_status = checkout_session.get("payment_status")
        if payment_status not in ["paid", "no_payment_required"]:
            return {"error": f"Checkout session not paid yet (status: {payment_status})."}

        session_email = checkout_session.get("customer_details", {}).get("email") or checkout_session.get("customer_email")
        if session_email and user["email"] and session_email.lower() != user["email"].lower():
            return {"error": f"Checkout email mismatch: {session_email} vs {user['email']}"}

        customer = checkout_session.get("customer")
        subscription = checkout_session.get("subscription")

        stripe_customer_id = customer.get("id") if isinstance(customer, dict) else customer
        stripe_subscription_id = subscription.get("id") if isinstance(subscription, dict) else subscription
        stripe_subscription_status = subscription.get("status") if isinstance(subscription, dict) else "active"

        if not stripe_subscription_id:
            return {"error": "No subscription found on this checkout session."}

        if stripe_subscription_status not in ["active", "trialing"]:
            return {"error": f"Subscription is not active yet (status: {stripe_subscription_status})."}

        save_subscription_for_user(
            user_id=user["id"],
            stripe_customer_id=stripe_customer_id,
            stripe_subscription_id=stripe_subscription_id,
            status=stripe_subscription_status,
        )

        fresh = get_active_subscription(user["id"])
        if not fresh:
            return {"error": "Subscription row was not saved correctly."}

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

    except Exception as e:
        print("CONFIRM CHECKOUT ERROR:", repr(e))
        return {"error": str(e)}


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

        user = get_user_from_token(authorization)
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
            input=build_prompt(job_description, cv_text, is_pro=plan["is_pro"]),
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

        data = normalize_analysis_data(data, is_pro=plan["is_pro"])

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
