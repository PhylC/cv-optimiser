from __future__ import annotations

import io
import errno
import html
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from docx import Document
from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
from pypdf import PdfReader
from starlette.middleware.sessions import SessionMiddleware
import stripe
from supabase import Client, create_client

BASE_DIR = Path(__file__).resolve().parent
STATIC_INDEX_PATH = BASE_DIR / "static" / "index.html"


def load_dotenv_file(path: Path = BASE_DIR / ".env") -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


load_dotenv_file()

app = FastAPI(title="CV Optimiser V2")
logger = logging.getLogger(__name__)

CANONICAL_SCHEME = "https"
CANONICAL_HOST = "www.cv-optimiser.com"
CANONICAL_ORIGIN = f"{CANONICAL_SCHEME}://{CANONICAL_HOST}"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "testserver"}


def canonical_path(path: str) -> str:
    clean_path = (path or "/").split("?", 1)[0].split("#", 1)[0].strip()
    if not clean_path.startswith("/"):
        clean_path = f"/{clean_path}"
    if clean_path != "/":
        clean_path = clean_path.rstrip("/")
    return clean_path or "/"


def canonical_url(path: str = "/") -> str:
    return f"{CANONICAL_ORIGIN}{canonical_path(path)}"


def canonical_link_tag(path: str = "/") -> str:
    return f'<link rel="canonical" href="{html.escape(canonical_url(path))}">'


GA4_MEASUREMENT_ID = "G-JKMYVYF743"
BING_WEBMASTER_VERIFICATION = "7FEC7FC5C79BA53B96B487318D777AE3"


def google_tag() -> str:
    escaped_id = html.escape(GA4_MEASUREMENT_ID)
    escaped_bing_verification = html.escape(BING_WEBMASTER_VERIFICATION)
    return f"""
        <meta name="msvalidate.01" content="{escaped_bing_verification}">
        <!-- Google tag (gtag.js) -->
        <script async src="https://www.googletagmanager.com/gtag/js?id={escaped_id}"></script>
        <script>
          window.dataLayer = window.dataLayer || [];
          function gtag(){{dataLayer.push(arguments);}}
          gtag('js', new Date());

          gtag('config', '{escaped_id}');
        </script>
    """


def _split_host(host_header: str) -> tuple[str, str]:
    host = (host_header or "").strip()
    if ":" not in host:
        return host.lower(), ""
    name, port = host.rsplit(":", 1)
    return name.lower(), f":{port}" if port else ""


@app.middleware("http")
async def canonical_redirects(request: Request, call_next):
    host_header = request.headers.get("host", "")
    host, _ = _split_host(host_header)
    is_local = host in LOCAL_HOSTS
    if request.method not in {"GET", "HEAD"}:
        return await call_next(request)

    query = f"?{request.url.query}" if request.url.query else ""
    if request.url.path != "/" and request.url.path.endswith("/"):
        if is_local:
            return RedirectResponse(
                url=f"{canonical_path(request.url.path)}{query}",
                status_code=301,
            )
        return RedirectResponse(
            url=f"{canonical_url(request.url.path)}{query}",
            status_code=301,
        )

    if is_local:
        return await call_next(request)

    forwarded_proto_header = request.headers.get("x-forwarded-proto", "")
    forwarded_proto = forwarded_proto_header.split(",")[0].strip().lower()
    forwarded_ssl = request.headers.get("x-forwarded-ssl", "").strip().lower()
    forwarded_header = request.headers.get("forwarded", "").lower()
    has_explicit_proxy_proto = bool(forwarded_proto_header or forwarded_ssl or forwarded_header)
    is_https = (
        forwarded_proto == CANONICAL_SCHEME
        or forwarded_ssl == "on"
        or "proto=https" in forwarded_header
        or (not has_explicit_proxy_proto and request.url.scheme == CANONICAL_SCHEME)
    )
    needs_host_redirect = host != CANONICAL_HOST
    needs_scheme_redirect = not is_https and (
        has_explicit_proxy_proto
        or host != CANONICAL_HOST
    )

    if needs_host_redirect or needs_scheme_redirect:
        return RedirectResponse(
            url=f"{CANONICAL_SCHEME}://{CANONICAL_HOST}{request.url.path}{query}",
            status_code=301,
        )

    return await call_next(request)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        CANONICAL_ORIGIN,
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
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
SITE_URL = CANONICAL_ORIGIN
FREE_ANALYSES_PER_DAY = int(os.getenv("FREE_ANALYSES_PER_DAY", "3").strip())

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "").strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ONE_TIME = os.getenv("STRIPE_PRICE_ONE_TIME", "").strip()
STRIPE_PRICE_PRO_MONTHLY = os.getenv("STRIPE_PRICE_PRO_MONTHLY", os.getenv("STRIPE_PRICE_ID", "")).strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "").strip()
ADMIN_SESSION_SECRET = (
    os.getenv("ADMIN_SESSION_SECRET")
    or os.getenv("SECRET_KEY")
    or SUPABASE_SERVICE_ROLE_KEY
    or OPENAI_API_KEY
    or "local-dev-admin-session-secret"
).strip()
ADMIN_COOKIE_SECURE_SETTING = os.getenv("ADMIN_COOKIE_SECURE", "auto").strip().lower()
ADMIN_COOKIE_SECURE = (
    ADMIN_COOKIE_SECURE_SETTING in {"1", "true", "yes"}
    or (
        ADMIN_COOKIE_SECURE_SETTING == "auto"
        and (APP_BASE_URL.startswith("https://") or os.getenv("RENDER", "").lower() == "true")
    )
)

app.add_middleware(
    SessionMiddleware,
    secret_key=ADMIN_SESSION_SECRET,
    same_site="lax",
    https_only=ADMIN_COOKIE_SECURE,
    max_age=60 * 60 * 12,
)

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
supabase_admin: Optional[Client] = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    try:
        supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)
    except Exception:
        logger.exception("Supabase admin client could not be initialized")
        supabase_admin = None

FAQ_ENTRIES: list[tuple[str, str]] = [
    (
        "Why can a CV get overlooked quickly?",
        "Many employers use ATS systems and fast manual review. If your CV does not contain relevant keywords or match the job description clearly, it may look less aligned with the role.",
    ),
    (
        "What is a good CV score?",
        "A good CV score means your CV matches the job description closely in skills, experience, and keywords. High relevance matters more than generic polish.",
    ),
    (
        "Do recruiters actually read CVs?",
        "They scan them first. Most recruiters spend seconds looking for relevant experience, keywords, and proof of impact before deciding whether to keep reading.",
    ),
    (
        "How many keywords should a CV have?",
        "Usually 10 to 30 relevant keywords, used naturally. Too few hurts relevance. Too many hurts readability.",
    ),
    (
        "Should I tailor my CV for every job?",
        "Yes. A tailored CV performs better because it shows the exact relevance recruiters and ATS systems are looking for.",
    ),
    (
        "Can I check ATS-style keyword gaps without stuffing keywords?",
        "No. If your CV doesn’t reflect the language and priorities in the job description, ATS systems have less evidence that you fit the role.",
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
        "intro": "Check how clearly your CV matches the job description before you apply. CV Optimiser compares your CV against a job description and highlights the missing signals that can hold you back.",
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
        "intro": "Get an AI-assisted review of what may be holding your CV back. CV Optimiser gives you a clear score, identifies weak areas, and shows the most important improvements to make first.",
        "bullets": [
            "See the top changes that may improve CV clarity and role match",
            "Understand structure, wording, and keyword gaps",
            "Use the free check before deciding whether to unlock the full report",
        ],
    },
}

SUPPORT_PAGES: dict[str, dict[str, Any]] = {
    "cv-statistics": {
        "title": "CV Statistics 2026 | Job Application, ATS and Hiring Data",
        "description": "Key CV and hiring statistics including ATS filtering rates, recruiter behaviour and job application trends.",
        "h1": "CV Statistics (2026)",
        "intro": "This page summarises key CV and hiring statistics to help job seekers understand how recruitment works today.",
        "sections": [
            {
                "title": "Key CV statistics",
                "bullets": [
                    "Recruiters often spend only 6 to 10 seconds on an initial CV scan",
                    "A large share of CVs are filtered by ATS systems before a recruiter reviews them properly",
                    "Tailored CVs perform better than generic versions because relevance is easier to see",
                ],
            },
            {
                "title": "ATS statistics",
                "bullets": [
                    "Many companies use Applicant Tracking Systems (ATS) to filter candidates",
                    "CVs missing relevant keywords are less likely to pass initial screening",
                    "Keyword alignment is one of the biggest factors in CV success",
                ],
            },
            {
                "title": "Job application statistics",
                "bullets": [
                    "Most job seekers apply to multiple roles before receiving employer responses",
                    "Response rates usually improve when CVs are tailored to the role instead of reused unchanged",
                ],
            },
            {
                "title": "Why these stats matter",
                "copy": "These patterns shape what happens in real job applications. If your CV is generic, missing the job language, or unclear about impact, it is easier for ATS systems and recruiters to skip. A stronger match, clearer structure, and better keyword coverage give your application a better chance of surviving that first screening stage.",
            },
            {
                "title": "Use the tool",
                "copy": "Want to see how your CV performs?",
                "cta_href": "/#tool",
                "cta_label": "Check your CV →",
            },
            {
                "title": "Related pages",
                "copy": "Use these pages if you want to understand the tool better or run your own check.",
                "links": [
                    ("/cv-checker", "CV Checker"),
                    ("/how-it-works", "How CV Optimiser works"),
                ],
            },
        ],
    },
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
                "cta_href": "/#tool",
                "cta_label": "Get my CV score",
            },
        ],
    },
    "how-cv-optimiser-scores-your-cv": {
        "title": "How CV Optimiser Scores Your CV | CV Match Score Methodology",
        "description": "See how CV Optimiser calculates your CV match score using job description relevance, keywords, clarity, evidence and ATS-friendly structure.",
        "h1": "How CV Optimiser scores your CV",
        "intro": "CV Optimiser scores your CV by comparing it with a specific job description, then looking for the signals that help recruiters and applicant tracking systems understand your fit.",
        "sections": [
            {
                "title": "The score is role-specific",
                "copy": "A strong CV for one job can be weak for another. CV Optimiser does not give a generic CV grade. It checks how clearly your CV matches the job description you paste in.",
                "bullets": [
                    "Role title and responsibility alignment",
                    "Relevant skills, tools and experience",
                    "Evidence that your previous work fits the target role",
                ],
            },
            {
                "title": "What the score looks at",
                "copy": "The score combines several signals rather than relying on one keyword count.",
                "bullets": [
                    "Keyword coverage: whether important role terms appear naturally in your CV",
                    "Relevance: whether your experience maps to the job requirements",
                    "Evidence: whether your bullet points show outcomes, scope and impact",
                    "Structure: whether the CV is easy to scan and understand",
                    "ATS readability: whether the content is likely to be parsed cleanly",
                ],
            },
            {
                "title": "Why missing keywords matter",
                "copy": "Recruiters and ATS systems often look for the same language used in the job description. Missing keywords can make a relevant candidate look less suitable than they are.",
                "helper": "CV Optimiser highlights missing terms so you can decide which ones honestly belong in your CV.",
            },
            {
                "title": "What the score does not mean",
                "copy": "The score is a practical guide, not a promise of interviews or job offers. Hiring decisions also depend on experience level, location, salary, competition, timing and employer judgement.",
                "bullets": [
                    "It does not guarantee ATS acceptance",
                    "It does not replace human judgement",
                    "It should not be used to add skills or experience you do not have",
                ],
            },
            {
                "title": "How to use your result",
                "copy": "Start with the priority fixes before rewriting the whole CV. The best improvements usually come from clearer role alignment, stronger evidence and natural keyword coverage.",
                "bullets": [
                    "Add relevant missing keywords where they truthfully fit",
                    "Rewrite vague bullets into measurable achievements",
                    "Move the most relevant experience closer to the top",
                    "Recheck the CV against the same job description after editing",
                ],
            },
            {
                "title": "Privacy and CV handling",
                "copy": "CVs can contain personal information, so the tool asks only for the content needed to compare your CV with a job description.",
                "links": [
                    ("/privacy", "Read the privacy policy"),
                    ("/terms", "Read the terms"),
                ],
            },
            {
                "title": "Try the scoring tool",
                "copy": "Paste your CV and a job description to see your match score, missing keywords and top improvements.",
                "cta_href": "/#tool",
                "cta_label": "Check your CV score",
            },
        ],
    },
    "features": {
        "title": "Features | CV Optimiser",
        "description": "Explore the main CV Optimiser features including CV scoring, keyword gap detection, ATS checks and AI-assisted CV suggestions.",
        "h1": "CV Optimiser features",
        "intro": "CV Optimiser focuses on the parts of CV feedback that matter most when you are applying for a real job and need clear next steps.",
        "sections": [
            ("CV match score", "See how closely your CV aligns with the role before you apply."),
            ("Missing keyword detection", "Spot the role-specific terms your CV is missing or not supporting strongly enough."),
            ("Priority fixes", "Get priority improvements for clearer role fit."),
            ("Full report upgrade", "Unlock deeper feedback, stronger wording and a more detailed improvement plan when you need more help."),
        ],
    },
    "pricing": {
        "title": "Pricing | CV Optimiser",
        "description": "See CV Optimiser pricing options for free checks, paid reports and Pro access.",
        "h1": "Pricing",
        "intro": "Free checks help you review your CV match. Paid reports and Pro access unlock fuller guidance before you apply.",
        "sections": [
            {
                "title": "Free check",
                "copy": "Use a limited CV check to compare CV content with a job description, see summary-level suggestions, and decide whether your CV needs work.",
                "bullets": [
                    "Run a limited CV check",
                    "Compare CV content with a job description",
                    "See summary-level suggestions",
                    "Useful for deciding whether your CV needs work",
                ],
            },
            {
                "title": "Paid report",
                "copy": "Unlock fuller report details for one CV result based on the existing product logic.",
                "bullets": [
                    "More detailed improvement suggestions",
                    "Keyword gaps and role-match guidance",
                    "Rewritten examples and priority fixes where available",
                    "One-time report access currently shown as £7.99",
                ],
            },
            {
                "title": "Pro access",
                "copy": "Pro access is for users who want ongoing checks and full reports while actively preparing applications.",
                "bullets": [
                    "Ongoing CV checks",
                    "Full reports included with your plan",
                    "Saved results where account history is available",
                    "Pro access currently shown as £9.99/month",
                ],
            },
            {
                "title": "Important note",
                "copy": "Paid access does not guarantee interviews, job offers, ATS acceptance, or employer responses.",
            },
        ],
    },
    "contact": {
        "title": "Contact | CV Optimiser",
        "description": "Contact CV Optimiser for account, billing or support questions.",
        "h1": "Contact",
        "intro": "For questions about CV Optimiser, payments, privacy, or account access, contact us at:",
        "sections": [
            ("Email", "support@cv-optimiser.com"),
            ("Billing", "Subscription and payment management is handled through Stripe from the account menu when available."),
            ("Privacy", "Do not send sensitive personal details unless they are needed to resolve your support request."),
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
        "title": "Privacy Policy | CV Optimiser",
        "description": "Read how CV Optimiser handles CV content, job descriptions, account details, payments and analytics data.",
        "h1": "Privacy Policy",
        "intro": "Last updated: May 2026. CV Optimiser is a CV checking website that helps users compare a CV with a job description and receive practical improvement suggestions.",
        "sections": [
            {
                "title": "Information we collect",
                "bullets": [
                    "CV text or uploaded CV content provided by the user",
                    "Job description text provided by the user",
                    "Account information if the user signs in",
                    "Payment status information from Stripe, but not full card details",
                    "Basic analytics and technical data such as pages visited, browser/device information, and approximate location where analytics tools provide this",
                ],
            },
            {
                "title": "How we use this information",
                "bullets": [
                    "To provide CV checks and reports",
                    "To compare CV content with job descriptions",
                    "To manage accounts, free usage, and paid access",
                    "To improve the website and understand usage",
                    "To prevent abuse, errors, and misuse",
                ],
            },
            ("CV uploads and sensitive information", "CVs can contain personal information. Please avoid uploading information that is not needed for a CV check, such as national insurance numbers, full home addresses, passport details, bank details, or other highly sensitive information."),
            ("Payments", "Payments are processed by Stripe. CV Optimiser does not store full card details."),
            ("AI processing", "CV checks may be processed using AI services to generate suggestions. Do not upload content you are not comfortable having processed for this purpose."),
            ("Data retention", "We only keep information for as long as reasonably needed to provide the service, manage accounts, maintain records, improve reliability, and meet legal or security obligations. If the service stores report history for signed-in users, that history may remain available in the user account unless deleted or removed as part of normal account management."),
            ("User rights", "Depending on your location, you may have rights to access, correct, delete, or restrict use of your personal information."),
            ("Contact", "Questions about privacy can be sent through the Contact page."),
        ],
    },
    "terms": {
        "title": "Terms | CV Optimiser",
        "description": "Read the core terms for using CV Optimiser, including guidance-only output, payments and acceptable use.",
        "h1": "Terms",
        "intro": "Last updated: May 2026",
        "sections": [
            ("What CV Optimiser does", "CV Optimiser provides AI-assisted CV checks, CV-to-job-description comparison, keyword suggestions, and practical improvement guidance."),
            ("Guidance only", "CV Optimiser does not guarantee interviews, job offers, ATS acceptance, recruiter responses, or employer decisions."),
            ("User responsibility", "Users are responsible for reviewing suggestions and deciding what to include in their CV."),
            ("No employment relationship", "CV Optimiser is not a recruitment agency, employer, ATS provider, or official hiring platform."),
            ("No affiliation", "References to job descriptions, ATS systems, employers, sectors, or role types are for descriptive purposes only and do not imply endorsement, partnership, or affiliation."),
            ("Payments and access", "Paid features, prices, and access levels are shown on the website before purchase. Payments are processed by Stripe."),
            {
                "title": "Acceptable use",
                "copy": "Users must not:",
                "bullets": [
                    "Upload unlawful content",
                    "Upload someone else's CV without permission",
                    "Attempt to misuse, scrape, reverse engineer, or overload the service",
                    "Use the site for fraudulent applications or misrepresentation",
                ],
            },
            ("Availability", "We aim to keep the service available but cannot guarantee uninterrupted access."),
            ("Contact", "Questions can be sent through the Contact page."),
        ],
    },
}

EXAMPLE_REPORT_PAGE: dict[str, Any] = {
    "title": "Example CV Report | CV Optimiser",
    "description": "See an example CV Optimiser report with match score, missing keywords, priority fixes and rewrite suggestions.",
    "h1": "Example CV report",
    "intro": "See the type of feedback CV Optimiser gives before you run your own check.",
}

ROLE_EXAMPLE_REPORTS: dict[str, dict[str, Any]] = {
    "sales-cv-example-report": {
        "title": "Sales CV Example Report | CV Optimiser",
        "description": "See an example sales CV report with match score, missing sales keywords, revenue evidence gaps and improved bullet examples.",
        "h1": "Sales CV example report",
        "intro": "See how CV Optimiser reviews a sales CV against a real sales job description.",
        "role_label": "Sales Executive",
        "cv_snippet": "with experience managing prospects, building relationships and supporting revenue growth across B2B accounts.",
        "job_snippet": "We are looking for a sales executive with pipeline generation, CRM discipline, discovery calls, negotiation, forecasting and quota achievement experience.",
        "score": "Match Score: 61/100",
        "score_label": "Commercial evidence needs to be sharper",
        "score_copy": "The CV shows sales experience, but target performance, pipeline ownership and CRM evidence are not strong enough for the role.",
        "keywords": ["pipeline generation", "quota achievement", "CRM", "forecasting", "discovery calls", "negotiation", "conversion rate"],
        "unclear": [
            "Revenue and target performance are not visible enough.",
            "Pipeline generation is mentioned indirectly but not evidenced.",
            "CRM usage appears generic rather than tied to sales process.",
            "Achievements need stronger numbers, deal context and outcomes.",
        ],
        "fixes": [
            ("Lead with sales outcomes", "Show quota, revenue, meetings booked, conversion rate or pipeline value where truthful."),
            ("Make pipeline ownership explicit", "Use job-description language around prospecting, discovery, qualification and forecasting."),
            ("Replace generic relationship wording", "Tie customer relationships to commercial outcomes, retention, revenue or deal progress."),
        ],
        "weak_bullet": "Responsible for building relationships with customers and supporting sales targets.",
        "strong_bullet": "Generated qualified pipeline through targeted outreach and discovery calls, supporting improved conversion and clearer monthly forecasting.",
        "ats_checks": [
            "Core sales terms are present, but several high-intent keywords from the advert are missing.",
            "Sales achievements need clearer metrics so they can be scanned quickly.",
            "The profile should point to the target sales role faster.",
        ],
        "action_plan": [
            "Rewrite the profile around target role, sales cycle and commercial evidence.",
            "Add truthful metrics for revenue, quota, pipeline, conversion or account growth.",
            "Mirror important sales terms from the job description where experience supports them.",
        ],
        "related": [
            ("/sales-cv-keywords", "Sales CV keywords"),
            ("/cv-checker-for-sales-jobs", "CV checker for sales jobs"),
            ("/cv-keyword-optimiser", "CV keyword optimiser"),
        ],
    },
    "account-manager-cv-example-report": {
        "title": "Account Manager CV Example Report | CV Optimiser",
        "description": "See an account manager CV example report with stakeholder, forecasting, retention, growth and commercial planning feedback.",
        "h1": "Account manager CV example report",
        "intro": "See how CV Optimiser reviews an account manager CV for retention, growth, stakeholders and role fit.",
        "role_label": "Account Manager",
        "cv_snippet": "with experience managing retail customers, coordinating account plans and supporting commercial targets.",
        "job_snippet": "We are looking for an account manager with stakeholder management, forecasting, commercial planning, retailer execution and P&L ownership experience.",
        "score": "Match Score: 58/100",
        "score_label": "Needs clearer role alignment",
        "score_copy": "This CV has relevant experience, but the strongest achievements are not obvious and several role-specific keywords are missing.",
        "keywords": ["stakeholder management", "forecasting", "commercial planning", "P&L", "retailer execution", "category growth"],
        "unclear": [
            "Commercial impact is not clear enough.",
            "Summary does not closely match the target role.",
            "Achievements are written as responsibilities rather than outcomes.",
            "Important role keywords are missing or buried.",
        ],
        "fixes": [
            ("Add measurable impact", "Replace vague responsibilities with outcomes, numbers and commercial results."),
            ("Rewrite the summary around the target role", "The summary should immediately show why this CV fits the job description."),
            ("Mirror important job description language", "Use relevant role keywords naturally so the CV feels aligned to the vacancy."),
        ],
        "weak_bullet": "Responsible for managing customer accounts and sales targets.",
        "strong_bullet": "Drove account growth by turning customer plans into measurable revenue opportunities, improving retailer execution and strengthening commercial performance.",
        "ats_checks": [
            "Core headings are readable, but the profile is too generic for the target role.",
            "Important account management language appears in the job description but not strongly enough in the CV.",
            "Bullets need clearer outcomes so a recruiter can scan impact quickly.",
        ],
        "action_plan": [
            "Rewrite the top profile around account ownership and commercial impact.",
            "Add missing job-description keywords where they genuinely match experience.",
            "Replace duty-only bullets with measurable customer and revenue outcomes.",
        ],
        "related": [
            ("/account-manager-cv-keywords", "Account manager CV keywords"),
            ("/account-manager-cv-checker", "Account manager CV checker"),
            ("/job-description-cv-match", "Job description CV match"),
        ],
    },
    "project-manager-cv-example-report": {
        "title": "Project Manager CV Example Report | CV Optimiser",
        "description": "See a project manager CV example report with delivery, stakeholder, budget, risk, dependency and outcome feedback.",
        "h1": "Project manager CV example report",
        "intro": "See how CV Optimiser reviews a project manager CV for delivery evidence, governance, risk and measurable outcomes.",
        "role_label": "Project Manager",
        "cv_snippet": "with experience coordinating teams, managing tasks and supporting project delivery across business change initiatives.",
        "job_snippet": "We are looking for a project manager with stakeholder governance, delivery planning, budget tracking, RAID management, dependencies and measurable implementation outcomes.",
        "score": "Match Score: 64/100",
        "score_label": "Delivery evidence is present but too broad",
        "score_copy": "The CV suggests project experience, but budget, risk, dependencies and measurable delivery outcomes need to be easier to see.",
        "keywords": ["stakeholder governance", "delivery planning", "RAID", "dependencies", "budget tracking", "implementation", "change management"],
        "unclear": [
            "Project scale, budget and timeline context are not specific enough.",
            "Risk and dependency management are missing from the strongest sections.",
            "Delivery outcomes are described too generally.",
            "Methodologies are mentioned without proof of successful delivery.",
        ],
        "fixes": [
            ("Add project scale and context", "Show budget, team size, timeline, workstream count or delivery environment where possible."),
            ("Evidence risk and dependency control", "Include RAID, governance, escalation and stakeholder cadence if they match your experience."),
            ("Turn delivery duties into outcomes", "Show what changed after delivery, not just what you coordinated."),
        ],
        "weak_bullet": "Managed project tasks and worked with stakeholders to deliver business change.",
        "strong_bullet": "Led cross-functional delivery planning across three workstreams, tracking risks, dependencies and milestones to support on-time implementation.",
        "ats_checks": [
            "Project management language is present, but risk, budget and governance terms are underused.",
            "The CV should make delivery ownership easier to identify.",
            "Outcomes need more concrete scope so seniority is clearer.",
        ],
        "action_plan": [
            "Add project scale, budget, timelines and delivery outcomes where truthful.",
            "Strengthen stakeholder governance, risk and dependency language.",
            "Rewrite broad coordination bullets into delivery-focused achievements.",
        ],
        "related": [
            ("/project-manager-cv-checker", "Project manager CV checker"),
            ("/cv-checker-for-management-jobs", "CV checker for management jobs"),
            ("/best-cv-format-for-ats", "Best CV format for ATS"),
        ],
    },
}

COMPARISON_PAGES: dict[str, dict[str, Any]] = {
    "cv-optimiser-vs-jobscan": {
        "title": "CV Optimiser vs Jobscan | Which CV Checker Should You Use?",
        "description": "Compare CV Optimiser and Jobscan for CV checking, ATS keywords, job-description matching, UK CV language and practical application feedback.",
        "h1": "CV Optimiser vs Jobscan",
        "intro": "If you want a broad resume-scanning platform, Jobscan is well known. If you want a focused UK CV checker that compares your CV with the exact job description and tells you what to fix before applying, CV Optimiser is built for that narrower job.",
        "positioning": "Our niche: UK CVs, job-description match, missing keywords, practical fixes and transparent example reports.",
        "competitor": "Jobscan",
        "best_for_competitor": "Job seekers who want a broad ATS resume scanner with detailed resume formatting checks, keyword matching and a larger job-search toolset.",
        "best_for_us": "UK job seekers who want fast CV-to-job-description feedback, clearer missing keywords, example report pages and a simple CV-first workflow without turning the task into a whole job-search platform.",
        "rows": [
            ("Primary focus", "Sharper for one task: check this CV against this job description and show the fixes.", "Broader resume scanning, ATS matching, formatting checks and job-search tools."),
            ("UK CV fit", "Uses CV language, UK application framing and role examples written for CV users.", "Uses resume-focused wording, which may suit US/international searches better."),
            ("Proof before upload", "Shows report-style examples by role so users can see the kind of feedback before trusting the tool.", "Shows scanner examples and match-report concepts on its product pages."),
            ("Best use case", "Quickly deciding whether your CV is ready for a specific UK role.", "Optimising a resume within a broader ATS and job-search workflow."),
        ],
        "choose_us": [
            "You write and apply with a UK CV rather than a US-style resume.",
            "You want a fast score, missing keywords and top fixes without a heavy workflow.",
            "You want to see role-specific example reports before trusting the tool.",
            "You want direct application feedback rather than another large job-search dashboard.",
        ],
        "choose_competitor": [
            "You want a larger resume/job-search platform around ATS scanning.",
            "You prefer resume terminology and a more established scanner brand.",
            "You want detailed formatting checks as a central part of the workflow.",
        ],
        "related": [
            ("/best-free-cv-checker-uk", "Best free CV checker UK"),
            ("/ats-cv-checker", "ATS CV checker"),
            ("/sales-cv-example-report", "Sales CV example report"),
        ],
    },
    "cv-optimiser-vs-resume-worded": {
        "title": "CV Optimiser vs Resume Worded | CV Checker Comparison",
        "description": "Compare CV Optimiser and Resume Worded for CV scoring, resume feedback, LinkedIn checks, keyword targeting and role-specific application support.",
        "h1": "CV Optimiser vs Resume Worded",
        "intro": "Resume Worded is a broad resume and LinkedIn feedback platform. CV Optimiser is deliberately narrower: it is for checking a UK CV against a specific job description and turning the result into practical fixes for that application.",
        "positioning": "Our niche: less general career tooling, more focused CV-to-job matching before you press apply.",
        "competitor": "Resume Worded",
        "best_for_competitor": "Job seekers who want resume feedback, LinkedIn profile help, rewrite support and templates in one broader platform.",
        "best_for_us": "Job seekers who want a direct CV match score against a specific job description, with missing keywords and role-specific guidance.",
        "rows": [
            ("Primary focus", "CV-to-job-description match, missing keywords, scoring and practical report guidance.", "Resume scoring, LinkedIn profile feedback, AI rewrites, templates and keyword targeting."),
            ("Workflow", "Paste a CV and job description, then review the role match quickly.", "Upload a resume or LinkedIn profile and use broader feedback features."),
            ("UK fit", "Built around CV wording and UK-focused examples, pages and user intent.", "Mostly uses resume wording, with international appeal."),
            ("Best use case", "Checking one application before you submit it.", "Improving a resume/LinkedIn profile across a wider job-search toolkit."),
        ],
        "choose_us": [
            "You want job-description matching rather than general resume polish.",
            "You want UK CV terminology and role-specific CV example reports.",
            "You want a lightweight check before deciding whether to unlock a fuller report.",
            "You want to improve the CV for the vacancy in front of you, not manage your whole career profile.",
        ],
        "choose_competitor": [
            "You want LinkedIn profile feedback alongside resume feedback.",
            "You want templates and rewrite tooling as part of a broader platform.",
            "You are happy using resume-focused language and workflows.",
        ],
        "related": [
            ("/cv-score-checker", "CV score checker"),
            ("/cv-job-description-match", "CV job description match"),
            ("/project-manager-cv-example-report", "Project manager CV example report"),
        ],
    },
    "best-ats-cv-checker-uk": {
        "title": "Best ATS CV Checker UK | What to Look For Before You Apply",
        "description": "Find out what makes a useful ATS CV checker for UK job applications, including keyword match, formatting, role relevance and clear improvement steps.",
        "h1": "Best ATS CV checker UK",
        "intro": "The best ATS CV checker for a UK job seeker should not behave like a magic keyword counter. It should show whether your CV is readable, relevant, honest and clearly matched to the job description you actually want.",
        "positioning": "CV Optimiser is built for the practical middle ground: not a generic spelling check, not a scary ATS promise, but a clear CV match score with next fixes.",
        "competitor": "Other ATS checkers",
        "best_for_competitor": "Some tools are useful for broad resume scans, template checks or large job-search workflows.",
        "best_for_us": "CV Optimiser is useful when you want a UK CV check focused on a specific job description and practical fixes before applying.",
        "rows": [
            ("Job-description matching", "Compares your CV with the exact advert so feedback is role-specific from the start.", "Some tools offer this, while others focus on general CV quality."),
            ("Keyword guidance", "Highlights missing terms and explains how to use them naturally rather than stuffing the CV.", "Keyword advice varies from simple matching to deeper weighting."),
            ("ATS readability", "Checks practical structure and readability signals while being honest that no tool can guarantee every ATS outcome.", "Tools vary widely and no checker can guarantee every employer system."),
            ("Trust", "Shows methodology, limitations, privacy notes and example reports so users know what they are getting.", "Look for clear explanations, not magic-score promises."),
        ],
        "choose_us": [
            "You want UK CV language and a simple application-focused check.",
            "You want a score plus missing keywords and priority fixes.",
            "You care about seeing examples before uploading your own CV.",
        ],
        "choose_competitor": [
            "You need a full resume builder or template library.",
            "You want platform-specific ATS claims for a named employer system.",
            "You want a broader job-search suite beyond CV checking.",
        ],
        "related": [
            ("/ats-cv-checker", "ATS CV checker"),
            ("/best-cv-format-for-ats", "Best CV format for ATS"),
            ("/how-cv-optimiser-scores-your-cv", "How scoring works"),
        ],
    },
    "free-cv-checker-vs-paid-cv-review": {
        "title": "Free CV Checker vs Paid CV Review | Which Do You Need?",
        "description": "Compare free CV checkers and paid CV reviews, including when a quick score is enough and when a deeper human or AI-assisted review may help.",
        "h1": "Free CV checker vs paid CV review",
        "intro": "A free CV checker should help you answer one urgent question: is this CV good enough for this job description? CV Optimiser starts there, then gives you the option to go deeper only if the gaps are worth fixing.",
        "positioning": "Use CV Optimiser first when you want quick evidence before spending money on a full review.",
        "competitor": "Paid CV review",
        "best_for_competitor": "People who want detailed human judgement, personal career context and hands-on rewriting support.",
        "best_for_us": "People who want quick, role-specific feedback before deciding whether a fuller report or paid review is worth it.",
        "rows": [
            ("Speed", "Fast score, missing keywords and priority fixes for the role in front of you.", "Usually slower, especially if human-reviewed."),
            ("Cost", "Starts with a free check, so you can see whether the CV has real gaps before paying.", "Often higher cost because it may involve expert time."),
            ("Personal judgement", "Useful for structured feedback, but still guidance only.", "Can add nuanced judgement for senior, complex or career-change applications."),
            ("Best use case", "Checking each application against a job description.", "Repositioning a CV, resolving career story issues or preparing for important roles."),
        ],
        "choose_us": [
            "You want to know what is missing before you spend money.",
            "You apply to multiple roles and need repeatable checks.",
            "You want role-specific feedback in under a minute.",
        ],
        "choose_competitor": [
            "You need a human to challenge your career story.",
            "You are applying for senior or specialist roles with high stakes.",
            "You want someone to rewrite large parts of the CV for you.",
        ],
        "related": [
            ("/free-cv-review", "Free CV review"),
            ("/example-cv-report", "Example CV report"),
            ("/cv-improvement-tool", "CV improvement tool"),
        ],
    },
}

TOOL_LANDING_PAGES: dict[str, dict[str, Any]] = {
    "cv-checker": {
        "title": "Free CV Checker | Compare Your CV to Any Job Description",
        "meta_description": "Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.",
        "h1": "Check your CV before applying — in under 60 seconds",
        "intro": "Compare your CV with a job description and get practical suggestions before you apply.",
        "tool_intro": [
            "Upload your CV and a job description to get your personalised score and fixes.",
        ],
        "tool_heading": "Check my CV",
        "sections": [
            {
                "title": "Why use CV Optimiser instead of a generic CV checker?",
                "copy": "CV Optimiser is built around one practical job-search moment: you have a CV, you have a job description, and you need to know whether the CV is strong enough before you apply.",
                "bullets": [
                    "Uses UK CV language rather than only resume wording",
                    "Scores your CV against the job description, not a generic ideal CV",
                    "Shows missing keywords, weak evidence and priority fixes",
                    "Links to example reports so you can see the type of feedback first",
                ],
                "link_href": "/how-cv-optimiser-scores-your-cv",
                "link_label": "See how the scoring works →",
            },
            {
                "title": "What this CV checker does",
                "copy": "This CV checker compares your CV against a job description to show:",
                "bullets": [
                    "Your CV match score",
                    "Missing keywords for the role",
                    "What may be unclear",
                    "The most important improvements to make",
                ],
                "helper": "It’s designed to reflect how your CV is aligned with the job description.",
            },
            {
                "title": "Why many CVs are overlooked",
                "copy": "Many CVs are overlooked when relevance is unclear.",
                "bullets": [
                    "Important keywords from the job description are missing",
                    "Experience isn’t clearly aligned to the role",
                    "Achievements are vague or not measurable",
                    "The CV doesn’t quickly show relevance",
                ],
                "helper": "Fixing these issues can help you submit a clearer, more targeted application.",
            },
            {
                "title": "How the CV check works",
                "bullets": [
                    "1. Upload your CV or paste the text",
                    "2. Paste the job description",
                    "3. Get your CV score and improvement suggestions",
                ],
                "link_href": "/how-it-works",
                "link_label": "Learn more about how it works →",
            },
            {
                "title": "What you get from your CV check",
                "bullets": [
                    "CV match score",
                    "Missing keywords",
                    "Top priority fixes",
                    "Feedback on clarity and relevance",
                ],
                "helper": "The full report includes deeper improvements and rewrite suggestions.",
            },
            {
                "title": "See the report style before you try it",
                "copy": "The site includes fictional example reports for sales, account manager and project manager CVs, so you can understand the type of output before using your own CV.",
                "link_href": "/example-cv-report",
                "link_label": "View example CV reports →",
            },
        ],
        "example_title": "Example CV diagnosis",
        "example_score": "Score: 58/100 — needs clearer role alignment",
        "example_keywords": ["stakeholder management", "forecasting", "commercial planning"],
        "example_fixes": ["Add measurable results", "Strengthen your summary", "Match role keywords"],
        "example_link_label": "View full example report →",
        "cta_title": "Check your CV now",
        "cta_copy": "Upload your CV, paste a job description and get your score in under 60 seconds.",
        "cta_label": "Get my CV score",
    },
    "cv-score-checker": {
        "title": "CV Score Checker | See How Your CV Performs",
        "meta_description": "Check how well your CV matches a job description and identify what is holding it back.",
        "h1": "CV Score Checker",
        "intro": "Check how well your CV matches a job description and identify what is holding it back.",
        "tool_intro": [
            "Most CVs do not fail because of experience. They fail because they do not clearly match the job.",
            "Use the tool below to get your CV score and see what to improve.",
        ],
        "tool_heading": "Check your CV score",
        "sections": [
            {
                "title": "What your CV score means",
                "copy": "Your score reflects:",
                "bullets": [
                    "keyword match to the job description",
                    "relevance of your experience",
                    "clarity and structure",
                    "how easily a recruiter can assess your fit",
                ],
            },
            {
                "title": "How to improve your score",
                "copy": "Improving your CV score usually involves:",
                "bullets": [
                    "adding measurable results",
                    "matching job-specific keywords",
                    "strengthening your summary",
                    "improving clarity",
                ],
            },
        ],
        "cta_title": "Check your CV now",
        "cta_copy": "Use the CV score checker to see how your CV performs and what to improve next.",
        "cta_label": "Check your CV now",
    },
    "job-description-cv-match": {
        "title": "Match Your CV to a Job Description | CV Optimiser",
        "meta_description": "Compare your CV to a job description and see how closely your experience matches the role.",
        "h1": "Match Your CV to Any Job Description",
        "intro": "See how closely your CV matches the job you are applying for.",
        "tool_intro": [
            "Recruiters look for alignment between your CV and the job description. If your CV does not reflect the role clearly, it is easy to overlook.",
        ],
        "tool_heading": "Check your CV against a job",
        "sections": [
            {
                "title": "Why matching matters",
                "copy": "Recruiters look for alignment between your CV and the job description. If your CV does not reflect the role clearly, it is easy to overlook.",
            },
            {
                "title": "What gets checked",
                "bullets": [
                    "keyword alignment",
                    "relevant experience",
                    "role-specific terminology",
                    "clarity of achievements",
                ],
            },
        ],
        "cta_title": "Check your CV against a job",
        "cta_copy": "Paste the job description, compare your CV, and see where your fit is strongest or weakest.",
        "cta_label": "Check your CV against a job",
    },
    "cv-keyword-optimiser": {
        "title": "CV Keyword Optimiser | Improve Your CV for Job Applications",
        "meta_description": "Find missing CV keywords and improve how well your CV matches a job description.",
        "h1": "CV Keyword Optimiser",
        "intro": "Find missing keywords and improve how clearly your CV matches the role.",
        "tool_intro": [
            "Recruiters and ATS systems often scan for specific terms from the job description. If those keywords are missing, your CV may look less aligned with the role.",
        ],
        "tool_heading": "Optimise your CV keywords",
        "sections": [
            {
                "title": "Why keywords matter",
                "copy": "Recruiters and ATS systems often scan for specific terms from the job description. If those keywords are missing, your CV may look less aligned with the role.",
            },
            {
                "title": "What you will find",
                "bullets": [
                    "missing keywords",
                    "keyword gaps",
                    "suggested improvements",
                    "role-specific terms to add naturally",
                ],
            },
        ],
        "cta_title": "Optimise your CV now",
        "cta_copy": "Find the keyword gaps that matter and improve your CV before you apply.",
        "cta_label": "Optimise your CV now",
    },
    "ats-cv-checker": {
        "title": "ATS CV Checker | Improve Your CV for Applicant Tracking Systems",
        "meta_description": "Check how your CV performs in ATS systems and identify missing keywords, structure issues and priority improvements.",
        "h1": "ATS CV Checker",
        "intro": "Check how your CV performs in Applicant Tracking Systems and identify what is missing.",
        "tool_intro": [
            "Most companies use ATS software to filter CVs before a human sees them.",
            "If your CV doesn’t match the job description closely, it may look less aligned in ATS-style screening before manual review.",
        ],
        "tool_heading": "Check your CV for ATS compatibility",
        "sections": [
            {
                "title": "What is an ATS?",
                "copy": "An Applicant Tracking System scans CVs for keywords, structure and relevance before a recruiter reviews them.",
            },
            {
                "title": "Why it matters",
                "copy": "If your CV does not match the job description closely, it may look less aligned in ATS-style screening before manual review.",
            },
            {
                "title": "What this checker helps with",
                "bullets": [
                    "ATS match score",
                    "missing keywords",
                    "CV improvement suggestions",
                    "priority fixes",
                ],
            },
        ],
        "cta_title": "Check your CV for ATS compatibility",
        "cta_copy": "Use the checker to review ATS-style readability and role match before you apply.",
        "cta_label": "Check your CV for ATS compatibility",
    },
    "cv-improvement-tool": {
        "title": "CV Improvement Tool | Get Actionable CV Feedback",
        "meta_description": "Get practical CV feedback including missing keywords, structure improvements and priority fixes.",
        "h1": "CV Improvement Tool",
        "intro": "Get practical feedback on your CV and learn what to improve.",
        "tool_intro": [
            "Most CVs can be improved with small changes that make a big difference. CV Optimiser helps you focus on the fixes that matter most.",
        ],
        "tool_heading": "Improve your CV",
        "sections": [
            {
                "title": "Improve the parts recruiters notice first",
                "copy": "Most CVs can be improved with small changes that make a big difference. CV Optimiser helps you focus on the fixes that matter most.",
            },
            {
                "title": "What you can improve",
                "bullets": [
                    "clarity",
                    "relevance",
                    "structure",
                    "impact",
                    "keyword alignment",
                ],
            },
        ],
        "cta_title": "Improve your CV now",
        "cta_copy": "Get actionable CV feedback and focus on practical improvements for role fit and clarity.",
        "cta_label": "Improve your CV now",
    },
}

BLOG_ARTICLES: dict[str, dict[str, Any]] = {
    "why-is-my-cv-not-getting-interviews": {
        "title": "Why Your CV May Not Be Getting Responses",
        "meta_description": "Applying for jobs and hearing nothing back? See why your CV is getting ignored and what to fix first.",
        "h1": "Why Your CV May Not Be Getting Responses",
        "intro": "If you're applying for jobs and hearing nothing back, your CV isn’t working. Not because you're unqualified — but because your CV isn’t aligned with how hiring actually works.",
        "summary_title": "Quick summary:",
        "summary_bullets": [
            "Your CV isn’t tailored to the job",
            "You’re missing key ATS keywords",
            "Your achievements aren’t clear or measurable",
        ],
        "top_cta": "Check your CV now",
        "bottom_cta": "Check your CV now",
        "sections": [
            {
                "title": "1. Your CV isn’t tailored to the job",
                "paragraphs": [
                    "Most CVs fail because they’re generic. Recruiters scan for relevance — not effort.",
                    "Fix: Match your CV to the job description. Use the same language and priorities.",
                ],
            },
            {
                "title": "2. You’re missing critical keywords",
                "paragraphs": [
                    "Applicant Tracking Systems scan CVs for keywords from the job description. If they don’t find them, your CV gets filtered out even if you could do the job.",
                    "Fix: Add the relevant skills, tools, responsibilities, and job terms naturally across your summary, experience, and skills sections.",
                ],
            },
            {
                "title": "3. Your CV lacks measurable impact",
                "paragraphs": [
                    "Recruiters don’t care about responsibilities. They care about results.",
                ],
                "examples": [
                    ("Weak", "Managed accounts"),
                    ("Strong", "Managed £2M account portfolio, delivering 18% growth"),
                ],
            },
            {
                "title": "What to do next",
                "paragraphs": [
                    "Most people don’t know what to fix — that’s the real problem.",
                    "Use the tool below to see exactly what’s missing from your CV and how to improve it.",
                ],
            },
        ],
        "related_links": [
            ("/how-to-tailor-your-cv", "How to tailor your CV to a job description"),
            ("/example-cv-report", "See an example CV report"),
            ("/cv-checker", "Use the CV checker"),
        ],
    },
    "how-to-tailor-cv-to-job-description": {
        "title": "How to Tailor Your CV to a Job Description | CV Optimiser",
        "meta_description": "Learn how to tailor your CV to a job description using keywords, relevant experience and clearer achievements.",
        "h1": "How to Tailor Your CV to a Job Description",
        "intro": "Tailoring your CV isn’t optional — it’s the difference between submitting a generic CV and a clearer targeted application.",
        "summary_bullets": [
            "Match keywords from the job description",
            "Reorder your experience to match priorities",
            "Highlight relevant achievements first",
        ],
        "top_cta": "Tailor your CV automatically",
        "bottom_cta": "Tailor your CV automatically",
        "sections": [
            {
                "title": "Step 1: Extract keywords",
                "paragraphs": [
                    "Look for repeated skills, tools, and job titles. Those repeated terms usually tell you what matters most.",
                ],
            },
            {
                "title": "Step 2: Mirror the language",
                "paragraphs": [
                    "Use the exact wording where possible. If the job description says stakeholder management, don’t hide behind a softer phrase like client coordination.",
                ],
            },
            {
                "title": "Step 3: Reorder your CV",
                "paragraphs": [
                    "Put the most relevant experience at the top. Recruiters scan, not read.",
                ],
                "examples": [
                    ("Before", "General experience first"),
                    ("After", "Role-relevant experience first"),
                ],
            },
            {
                "title": "What this changes",
                "paragraphs": [
                    "A tailored CV makes your fit easier to assess before you apply.",
                ],
            },
        ],
        "related_links": [
            ("/job-description-cv-match", "Match your CV to any job description"),
            ("/how-it-works", "Learn how CV Optimiser works"),
            ("/cv-score-checker", "Check your CV score"),
        ],
    },
    "ats-cv-keywords": {
        "title": "ATS CV Keywords Explained | CV Optimiser",
        "meta_description": "Learn what ATS CV keywords are, why they matter and how to find missing keywords in your CV.",
        "h1": "ATS CV Keywords: How to Check Role Match",
        "intro": "Many CVs are screened by software before manual review. ATS software scans for keywords that match the job description.",
        "top_cta": "Find missing keywords in your CV",
        "bottom_cta": "Find missing keywords in your CV",
        "sections": [
            {
                "title": "What are CV keywords?",
                "paragraphs": [
                    "Keywords are the skills, job titles, tools, qualifications, and industry terms that match what employers are looking for.",
                ],
            },
            {
                "title": "How many keywords should you include?",
                "paragraphs": [
                    "Usually 10 to 30 relevant keywords, used naturally across your CV. More is not better if the wording starts sounding forced.",
                ],
            },
            {
                "title": "Examples",
                "bullets": [
                    "Project management",
                    "Data analysis",
                    "Stakeholder management",
                    "Sales growth",
                ],
            },
            {
                "title": "Mistakes to avoid",
                "bullets": [
                    "Keyword stuffing",
                    "Using irrelevant skills",
                    "Generic wording",
                ],
            },
        ],
        "related_links": [
            ("/ats-cv-checker", "Check your CV for ATS compatibility"),
            ("/cv-keyword-optimiser", "Optimise your CV keywords"),
        ],
    },
    "cv-mistakes-that-cost-interviews": {
        "title": "CV Mistakes That Weaken Applications | CV Optimiser",
        "meta_description": "Avoid common CV mistakes that reduce clarity and role match, from vague achievements to missing keywords.",
        "h1": "CV Mistakes That Weaken Applications",
        "intro": "Small CV mistakes can make strong candidates look weaker than they are.",
        "top_cta": "Fix your CV now",
        "bottom_cta": "Fix your CV now",
        "sections": [
            {
                "title": "The mistakes",
                "bullets": [
                    "Generic CVs",
                    "No measurable achievements",
                    "Missing keywords",
                    "Poor structure",
                ],
            },
            {
                "title": "Biggest mistake: Writing for yourself",
                "paragraphs": [
                    "Your CV isn’t about you. It’s about what the employer needs and whether they can see that fit quickly.",
                ],
            },
            {
                "title": "Fix it",
                "paragraphs": [
                    "Align your CV with the role, not your history. Lead with relevance, impact, and proof.",
                ],
            },
        ],
        "related_links": [
            ("/how-to-improve-cv-score", "How to improve your CV score"),
            ("/example-cv-report", "See an example CV report"),
        ],
    },
    "how-to-improve-cv-score": {
        "title": "How to Improve Your CV Score | CV Optimiser",
        "meta_description": "Learn how to improve your CV score by adding keywords, measurable results and clearer role alignment.",
        "h1": "How to Improve Your CV Score",
        "intro": "Your CV score improves when your CV becomes clearer, more relevant and better matched to the job description.",
        "top_cta": "Check your CV score",
        "bottom_cta": "Check your CV score",
        "sections": [
            {
                "title": "Add measurable results",
                "paragraphs": [
                    "Use numbers, outcomes, and evidence of impact. Responsibilities don’t move the score much. Results do.",
                ],
            },
            {
                "title": "Match the job description",
                "paragraphs": [
                    "Use relevant keywords and make your most relevant experience easy to find. If the alignment is hidden, the score stays weak.",
                ],
            },
            {
                "title": "Improve your summary",
                "paragraphs": [
                    "Your summary should make your fit obvious fast. Generic opening lines waste valuable space.",
                ],
            },
            {
                "title": "Simplify the structure",
                "paragraphs": [
                    "A clear structure helps both recruiters and ATS systems understand your experience faster. If they have to work for it, they usually won’t.",
                ],
            },
        ],
        "related_links": [
            ("/cv-score-checker", "Use the CV score checker"),
            ("/how-it-works", "Learn how the score works"),
        ],
    },
}

CORE_SEO_PAGE_SPECS: dict[str, dict[str, Any]] = {
    "why-is-my-cv-not-getting-interviews": {
        "title": "Why Is My CV Not Getting Responses? | CV Optimiser",
        "meta_description": "Find out why your CV may not be getting responses, from weak role fit and missing evidence to poor formatting and job-description mismatch.",
        "h1": "Why is my CV not getting responses?",
        "intro": "If you are applying for relevant roles and hearing nothing back, the issue is often not your whole career history. It is usually the way your CV presents relevance, evidence and fit for the specific role.",
        "who": ["Job seekers applying regularly with little response", "People using the same CV for very different roles", "Candidates who are unsure what recruiters notice first"],
        "looks_for": ["A clear target role in the top third", "Evidence that matches the job description", "Strong outcomes rather than duty-only bullets", "Readable formatting that works when scanned quickly"],
        "manual": ["Read only the top third and ask what role it points to", "Highlight every line that directly supports the target job", "Check whether your strongest examples appear on page one", "Remove weak details that distract from the role"],
        "sections": [
            ("Common reasons CVs get ignored", "Generic positioning, unclear target roles, missing keywords, weak evidence and poor formatting can all make a good candidate look irrelevant. The first fix is to compare your CV against one job description, not against a vague idea of a good CV."),
            ("What to fix first", "Start with target role clarity, then strengthen the top third, then rewrite bullets so they show outcomes. Formatting matters too, but clear relevance usually makes the biggest difference."),
        ],
        "related": [("/cv-job-description-match", "CV job description match"), ("/cv-mistakes-that-cost-interviews", "CV mistakes that cost interviews"), ("/10-second-cv-test", "10-Second CV Test")],
    },
    "cv-job-description-match": {
        "title": "CV Job Description Match | CV Optimiser",
        "meta_description": "Compare your CV against a job description and see whether your evidence, keywords and experience match the role you want.",
        "h1": "CV job description match",
        "intro": "A strong CV is not just well written. It is clearly matched to the job description. This page helps you understand whether your CV gives recruiters enough evidence for the role.",
        "who": ["Anyone applying to a specific advertised role", "Candidates tailoring a CV before submitting", "Job seekers who want a focused relevance check"],
        "looks_for": ["Skills and responsibilities from the advert", "Examples that prove you have used those skills", "Language that mirrors the job naturally", "Gaps between the role requirements and your CV evidence"],
        "manual": ["Copy the essential requirements into a checklist", "Mark where each requirement appears in your CV", "Rewrite generic bullets around the employer's priorities", "Check whether your profile reflects this role, not every possible role"],
        "sections": [
            ("Why matching matters", "Recruiters usually scan for fit before they study detail. If your relevant experience is buried, vague or written in different language from the advert, it is easier to overlook."),
            ("How to improve the match", "Use the job description to choose which achievements to lead with. Add missing evidence where truthful, remove unrelated detail, and use role language naturally rather than forcing keywords into every line."),
        ],
        "related": [("/how-to-tailor-cv-to-a-job-description", "How to tailor a CV"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/ats-cv-checker", "ATS CV checker")],
        "tool": True,
    },
    "free-cv-review": {
        "title": "Free CV Review | CV Optimiser",
        "meta_description": "Get a fast initial CV review by checking your CV against a job description for relevance, keywords, clarity and formatting.",
        "h1": "Free CV review",
        "intro": "Use CV Optimiser as a quick first review before you apply. It checks how clearly your CV matches a job description and shows the biggest issues to fix first.",
        "who": ["Job seekers who want quick feedback before applying", "People who need an initial review, not a full rewrite", "Candidates checking whether a CV is too generic"],
        "looks_for": ["Role relevance", "Keyword gaps", "Evidence of impact", "Formatting and scan readability"],
        "manual": ["Check whether the first half page matches the role", "Look for duty-only bullets with no result", "Compare your wording with the job advert", "Paste the CV into plain text and check readability"],
        "sections": [
            ("What a useful CV review should cover", "A review should go beyond spelling and layout. It should ask whether the CV is relevant to the role, whether evidence is clear, and whether the most important information appears early enough."),
            ("When to go deeper", "If your score is weak or the same issues appear across several roles, you may need a more detailed rewrite plan. Start with the free check so you know where the problem is."),
        ],
        "related": [("/cv-score-checker", "CV score checker"), ("/why-is-my-cv-not-getting-interviews", "Why your CV may not be getting responses"), ("/cv-checker", "CV checker")],
        "tool": True,
    },
    "how-to-tailor-cv-to-a-job-description": {
        "title": "How to Tailor a CV to a Job Description | CV Optimiser",
        "meta_description": "Learn how to tailor your CV to a job description with role keywords, relevant evidence and stronger achievement bullets.",
        "h1": "How to tailor your CV to a job description",
        "intro": "Tailoring your CV means making your relevance obvious for one role. It does not mean inventing experience or stuffing keywords into every sentence.",
        "who": ["Applicants adapting one CV for a specific role", "Career changers deciding what to lead with", "Candidates who want practical tailoring steps"],
        "looks_for": ["Repeated skills and priorities in the advert", "Relevant achievements that prove those priorities", "A profile section that points to the target role", "Bullets that connect action, context and outcome"],
        "manual": ["Underline the top 8 to 12 requirements", "Move the most relevant examples higher", "Replace generic bullets with role-matched evidence", "Check that every added keyword is truthful and supported"],
        "sections": [
            ("A simple tailoring process", "Start by extracting the role priorities. Then map your experience to those priorities, rewrite the profile and strongest bullets, and remove details that dilute the message."),
            ("Example rewrite", "Generic: Responsible for managing customer accounts. Tailored: Managed a portfolio of customer accounts, improving retention through stakeholder planning, forecasting and commercial reviews."),
        ],
        "related": [("/cv-job-description-match", "CV job description match"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/career-change-cv-checker", "Career change CV checker")],
    },
    "cv-mistakes-that-cost-interviews": {
        "title": "CV Mistakes That Cost Interviews | CV Optimiser",
        "meta_description": "Avoid CV mistakes that cost interviews, including unclear targeting, weak bullets, poor formatting and using the same CV for every job.",
        "h1": "CV mistakes that cost interviews",
        "intro": "Most costly CV mistakes are not dramatic. They are small signals that make your CV look less relevant, less clear or harder to trust during a fast scan.",
        "who": ["People getting few responses despite relevant experience", "Candidates updating an old CV", "Job seekers who want a practical mistake checklist"],
        "looks_for": ["Unclear target role", "Responsibilities without outcomes", "Relevant experience buried too low", "Poor ATS readability", "Too much old or unrelated detail"],
        "manual": ["Check whether the profile could fit almost anyone", "Count how many bullets include outcomes", "Look for tables, icons or columns that may parse badly", "Remove old detail that competes with current relevance"],
        "sections": [
            ("The biggest pattern", "The most common mistake is making the recruiter work too hard. Your CV should show the role you want, the evidence you offer and the reason you match the job description quickly."),
            ("How to recover", "Tighten the top third, rewrite weak bullets, prioritise recent and relevant evidence, and check each application against the actual advert."),
        ],
        "related": [("/10-second-cv-test", "10-Second CV Test"), ("/why-is-my-cv-not-getting-interviews", "Why your CV may not be getting responses"), ("/best-cv-format-for-ats", "Best CV format for ATS")],
    },
    "best-cv-format-for-ats": {
        "title": "Best CV Format for ATS | CV Optimiser",
        "meta_description": "Learn the best CV format for ATS readability, including simple headings, reverse chronological order and plain text checks.",
        "h1": "Best CV format for ATS",
        "intro": "ATS-friendly formatting is about clarity. The safest CV format is simple, readable and easy to parse, while still making a strong case for your fit.",
        "who": ["Applicants worried their CV format is blocking them", "People using templates with columns, icons or graphics", "Candidates applying through online portals"],
        "looks_for": ["Clear standard headings", "Reverse chronological experience", "Plain text readability", "Skills and evidence that match the advert", "No essential information hidden in images"],
        "manual": ["Copy your CV into plain text and inspect the order", "Use headings such as Profile, Experience, Education and Skills", "Avoid tables, icons and complex columns for core content", "Check that dates, job titles and employers are easy to scan"],
        "sections": [
            ("Recommended structure", "Use a clear profile, key skills, reverse chronological experience, education and relevant extras. Keep design restrained so the content is easy to parse and easy for a recruiter to scan."),
            ("Formatting to avoid", "Avoid putting essential text inside images, using decorative icons as labels, or relying on complex columns where the reading order may break."),
        ],
        "related": [("/ats-cv-checker", "ATS CV checker"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/cv-mistakes-that-cost-interviews", "CV mistakes")],
    },
    "cv-cover-letter-match": {
        "title": "CV Cover Letter Match | CV Optimiser",
        "meta_description": "Check whether your CV and cover letter tell a consistent story for the role without repeating each other exactly.",
        "h1": "CV cover letter match",
        "intro": "Your CV and cover letter should support the same application story. The cover letter can explain motivation and fit, while the CV proves the evidence.",
        "who": ["Applicants sending both a CV and cover letter", "Candidates unsure whether their message is consistent", "People tailoring applications for competitive roles"],
        "looks_for": ["A consistent target role", "Cover letter examples backed by CV evidence", "No contradictions in dates, skills or focus", "Natural language from the job description"],
        "manual": ["Check whether the cover letter claims are proved in the CV", "Remove repeated paragraphs that add no new value", "Use the cover letter to explain why this role, not to rewrite the CV", "Make sure both documents point to the same strengths"],
        "sections": [
            ("How the two documents should work together", "The CV should carry the evidence: roles, outcomes, skills and achievements. The cover letter should connect that evidence to the employer's priorities and explain why the role makes sense."),
            ("Common mismatch", "A CV aimed at one role and a cover letter aimed at another makes the application feel unfocused. Align both documents before applying."),
        ],
        "related": [("/cv-job-description-match", "CV job description match"), ("/how-to-tailor-cv-to-a-job-description", "How to tailor a CV"), ("/marketing-cv-checker", "Marketing CV checker")],
    },
}

ROLE_PAGE_SPECS: dict[str, dict[str, Any]] = {
    "sales-cv-checker": {
        "role": "sales",
        "title": "Sales CV Checker | CV Optimiser",
        "meta_description": "Check your sales CV for revenue evidence, pipeline impact, negotiation skills, CRM keywords and role-specific relevance.",
        "h1": "Sales CV checker",
        "signals": ["revenue, quota and target performance", "pipeline generation and conversion", "negotiation, CRM and forecasting", "account growth and commercial outcomes"],
        "mistakes": ["listing duties without numbers", "hiding quota performance", "using generic relationship language without commercial evidence"],
        "keywords": ["revenue", "pipeline", "conversion", "CRM", "forecasting", "negotiation", "account growth"],
        "cta_support": "Check whether your sales CV proves revenue impact, pipeline ownership and account growth.",
    },
    "account-manager-cv-checker": {
        "role": "account manager",
        "title": "Account Manager CV Checker | CV Optimiser",
        "meta_description": "Check your account manager CV for retention, growth, stakeholder management, forecasting and customer relationship evidence.",
        "h1": "Account manager CV checker",
        "signals": ["account ownership and portfolio size", "retention, growth and renewal outcomes", "stakeholder management and forecasting", "commercial delivery and customer planning"],
        "mistakes": ["describing accounts without ownership", "missing retention or growth evidence", "not showing senior stakeholder work"],
        "keywords": ["retention", "growth", "stakeholder management", "forecasting", "customer relationships", "commercial planning"],
    },
    "customer-success-cv-checker": {
        "role": "customer success",
        "title": "Customer Success CV Checker | CV Optimiser",
        "meta_description": "Check your customer success CV for retention, onboarding, adoption, renewals, churn reduction and customer outcome evidence.",
        "h1": "Customer success CV checker",
        "signals": ["onboarding and adoption outcomes", "retention, renewals and churn reduction", "product usage and customer health", "stakeholder management and customer outcomes"],
        "mistakes": ["sounding like generic support", "missing adoption or renewal metrics", "not showing product or customer health work"],
        "keywords": ["retention", "onboarding", "adoption", "churn reduction", "renewals", "customer outcomes"],
    },
    "project-manager-cv-checker": {
        "role": "project manager",
        "title": "Project Manager CV Checker | CV Optimiser",
        "meta_description": "Check your project manager CV for delivery evidence, timelines, budgets, risk, stakeholders, dependencies and outcomes.",
        "h1": "Project manager CV checker",
        "signals": ["delivery against timelines and budgets", "risk, dependency and issue management", "stakeholder communication and governance", "measurable project outcomes"],
        "mistakes": ["listing methodologies without delivery evidence", "missing budget or timeline context", "not explaining outcomes"],
        "keywords": ["delivery", "stakeholders", "risk", "budget", "dependencies", "governance", "outcomes"],
        "cta_support": "Check whether your project manager CV proves delivery, stakeholder control, risk management and outcomes.",
    },
    "graduate-cv-checker": {
        "role": "graduate",
        "title": "Graduate CV Checker | CV Optimiser",
        "meta_description": "Check your graduate CV for education, placements, projects, internships, part-time work and transferable skills.",
        "h1": "Graduate CV checker",
        "signals": ["target role clarity", "education, projects and placements", "internships and part-time work", "transferable skills with evidence"],
        "mistakes": ["trying to sound senior", "burying relevant projects", "using a vague objective with no target role"],
        "keywords": ["placement", "internship", "project", "analysis", "teamwork", "communication", "initiative"],
    },
    "it-helpdesk-cv-checker": {
        "role": "IT helpdesk",
        "title": "IT Helpdesk CV Checker | CV Optimiser",
        "meta_description": "Check your IT helpdesk CV for troubleshooting, ticketing, Windows, Microsoft 365, SLAs, escalation and customer support evidence.",
        "h1": "IT helpdesk CV checker",
        "signals": ["troubleshooting and ticket resolution", "Windows, Microsoft 365 and hardware/software support", "SLAs, escalation and documentation", "customer service under pressure"],
        "mistakes": ["listing tools without support examples", "missing ticketing or SLA context", "forgetting customer service evidence"],
        "keywords": ["troubleshooting", "ticketing", "Windows", "Microsoft 365", "SLA", "escalation", "hardware support"],
        "cta_support": "Check whether your IT helpdesk CV highlights troubleshooting, ticketing, SLAs and user support.",
    },
    "software-developer-cv-checker": {
        "role": "software developer",
        "title": "Software Developer CV Checker | CV Optimiser",
        "meta_description": "Check your software developer CV for tech stack, projects, GitHub, APIs, databases, testing, shipped work and impact.",
        "h1": "Software developer CV checker",
        "signals": ["tech stack and role-specific tools", "shipped projects and production impact", "APIs, databases and testing", "GitHub or portfolio evidence where relevant"],
        "mistakes": ["dumping every technology without context", "missing shipped outcomes", "not matching the stack in the advert"],
        "keywords": ["JavaScript", "Python", "APIs", "databases", "testing", "GitHub", "CI/CD"],
    },
    "admin-cv-checker": {
        "role": "admin",
        "title": "Admin CV Checker | CV Optimiser",
        "meta_description": "Check your admin CV for organisation, accuracy, scheduling, systems, documentation, customer service and process support.",
        "h1": "Admin CV checker",
        "signals": ["organisation and accuracy", "scheduling, records and documentation", "systems and office support", "customer service and process improvement"],
        "mistakes": ["sounding too generic", "missing systems experience", "not proving accuracy or organisation"],
        "keywords": ["scheduling", "records", "documentation", "accuracy", "office support", "customer service"],
    },
    "marketing-cv-checker": {
        "role": "marketing",
        "title": "Marketing CV Checker | CV Optimiser",
        "meta_description": "Check your marketing CV for campaigns, content, analytics, SEO, paid media, email, CRM, reporting and conversion evidence.",
        "h1": "Marketing CV checker",
        "signals": ["campaign outcomes and channel ownership", "analytics, reporting and conversion", "SEO, paid media, email or CRM evidence", "content and brand relevance"],
        "mistakes": ["listing channels without results", "missing metrics", "not matching the role's marketing mix"],
        "keywords": ["campaigns", "content", "analytics", "SEO", "paid media", "email", "CRM", "conversion"],
    },
    "career-change-cv-checker": {
        "role": "career change",
        "title": "Career Change CV Checker | CV Optimiser",
        "meta_description": "Check your career change CV for transferable skills, role clarity, relevant evidence and a focused explanation of your new direction.",
        "h1": "Career change CV checker",
        "signals": ["a clear target role", "transferable skills with evidence", "relevant projects, training or experience", "less space for unrelated detail"],
        "mistakes": ["explaining the old career too much", "not proving the new direction", "using vague transferable skills with no evidence"],
        "keywords": ["transferable skills", "stakeholders", "analysis", "customer service", "projects", "training", "adaptability"],
    },
    "nhs-admin-cv-checker": {
        "role": "NHS admin",
        "title": "NHS Admin CV Checker | CV Optimiser",
        "meta_description": "Check your NHS admin CV for accuracy, confidentiality, records, scheduling, communication, systems and public-sector relevance.",
        "h1": "NHS admin CV checker",
        "signals": ["accuracy, confidentiality and records", "patient or customer handling", "scheduling, communication and process", "systems experience and public-sector language"],
        "mistakes": ["not showing confidentiality or accuracy", "missing systems and records context", "using generic admin wording"],
        "keywords": ["confidentiality", "records", "scheduling", "communication", "patient handling", "accuracy", "systems"],
    },
}


def build_role_page(slug: str, spec: dict[str, Any]) -> dict[str, Any]:
    role = spec["role"]
    title_role = role if role.upper() == "NHS" else role
    return {
        "title": spec["title"],
        "meta_description": spec["meta_description"],
        "h1": spec["h1"],
        "intro": f"Use this {role} CV checker guide to see whether your CV is focused on the evidence recruiters expect for this type of role. A generic CV can hide the strongest parts of your experience, even when you are a good fit.",
        "who": [
            f"People applying for {role} roles with a CV that feels too generic",
            "Candidates tailoring a CV to a specific job description",
            "Job seekers who want clearer role-specific evidence before applying",
        ],
        "looks_for": spec["signals"],
        "manual": [
            "Compare your profile with the first five requirements in the advert",
            f"Check whether your strongest {role} evidence appears on page one",
            "Rewrite duty-only bullets so they show context and outcome",
            "Use job-description language naturally where it matches your real experience",
        ],
        "sections": [
            (f"What recruiters look for in a {title_role} CV", "Recruiters need to see relevant evidence quickly. For this role, that means making the right skills, tools, outcomes and examples easy to scan rather than expecting the reader to infer your fit."),
            ("Common CV mistakes for this role", "Common mistakes include " + ", ".join(spec["mistakes"]) + ". These issues make the CV feel less focused, even when the experience is useful."),
            ("Useful keywords and evidence to include", "Look for truthful ways to include terms such as " + ", ".join(spec["keywords"]) + ". The strongest CVs support those words with examples, numbers, scope or outcomes."),
            ("How to tailor it to a job description", "Start with the advert. Identify the repeated responsibilities and required skills, then choose the most relevant examples from your experience. Remove or shorten details that do not support this role."),
        ],
        "related": [("/cv-job-description-match", "CV job description match"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/10-second-cv-test", "10-Second CV Test")],
        "tool": True,
        "cta_support": spec.get("cta_support") or f"Check whether your {role} CV highlights " + ", ".join(spec["keywords"][:3]) + " and relevant evidence for the job description.",
    }


SEO_LANDING_PAGES: dict[str, dict[str, Any]] = {
    **CORE_SEO_PAGE_SPECS,
    **{slug: build_role_page(slug, spec) for slug, spec in ROLE_PAGE_SPECS.items()},
    "ats-cv-checker": {
        "title": "ATS CV Checker | CV Optimiser",
        "meta_description": "Check your CV for ATS readability, plain text parsing, headings, formatting, keywords and job-description match.",
        "h1": "ATS CV checker",
        "intro": "ATS systems vary, but the practical risks are consistent: unclear structure, missing job-description language and weak evidence. CV Optimiser focuses on the things you can improve before applying.",
        "who": ["People applying through online application systems", "Candidates using designed CV templates", "Job seekers who want to check keyword and formatting risks"],
        "looks_for": ["Plain text readability", "Standard headings and logical order", "Relevant keywords used naturally", "Experience that matches the advert", "Evidence behind important skills rather than unsupported keyword lists"],
        "manual": ["Paste your CV into plain text and check the reading order", "Avoid tables or graphics for essential content", "Use clear headings such as Experience and Education", "Compare your skills section with the job description", "Check whether the first page clearly proves the target role"],
        "sections": [
            ("Why CV Optimiser is useful for ATS checks", "It does not pretend to know every employer's exact ATS setup. Instead, it checks the practical things that usually matter: readable structure, job-description match, missing keywords and whether your evidence is easy to scan."),
            ("What makes it different", "CV Optimiser is built for UK CVs and role-specific checks. The score is tied to the job description you paste in, which makes the feedback more useful than a generic CV quality score."),
            ("What to improve", "Use simple formatting, add truthful role language and make your strongest relevant examples easy to find. Then recheck the CV against the same job description before applying."),
            ("Proof before you use it", "If you want to see the kind of feedback first, review the example report pages for sales, account management and project management CVs."),
        ],
        "related": [("/best-ats-cv-checker-uk", "Best ATS CV checker UK"), ("/best-cv-format-for-ats", "Best CV format for ATS"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/cv-job-description-match", "CV job description match"), ("/how-cv-optimiser-scores-your-cv", "How scoring works")],
        "tool": True,
        "cta_support": "Use the ATS checker to find practical risks: missing role terms, unsupported keywords, weak first-page relevance and formatting choices that make your CV harder to read.",
        "faqs": [
            ("Can any ATS checker guarantee my CV will pass?", "No. ATS systems and employer settings vary. CV Optimiser focuses on practical readability and role-match issues you can improve before applying."),
            ("Is this built for UK CVs?", "Yes. CV Optimiser uses CV wording and UK-focused pages, examples and role guidance."),
            ("Should I add every missing keyword?", "No. Add only the keywords you can honestly support with experience, evidence or achievements."),
            ("What score should I aim for?", "There is no universal guaranteed score, but a stronger score usually means clearer role alignment, better keyword coverage and stronger evidence."),
            ("Can I see an example first?", "Yes. The site includes example CV reports that show scores, missing keywords, unclear areas and improved bullet examples."),
        ],
    },
    "cv-score-checker": {
        "title": "CV Score Checker | CV Optimiser",
        "meta_description": "Check your CV score based on clarity, relevance, evidence, formatting and role fit against a job description.",
        "h1": "CV score checker",
        "intro": "A CV score is useful only when it reflects role fit. CV Optimiser checks whether your CV is clear, relevant and supported by evidence for the job you want.",
        "who": ["Applicants who want a quick quality check", "People comparing several CV versions", "Candidates trying to improve role fit before applying"],
        "looks_for": ["Job-description relevance", "Missing keywords", "Evidence and measurable outcomes", "Readable structure"],
        "manual": ["Score each requirement as clear, weak or missing", "Check whether bullets show outcomes", "Look for generic wording in the profile", "Make sure page one carries the strongest evidence"],
        "sections": [("What the score should mean", "A good score should not reward keyword stuffing. It should reflect whether a recruiter can quickly see the role you want, the evidence you offer and the fit with the advert."), ("How to improve it", "Improve the score by tightening the profile, adding relevant evidence, strengthening bullets and making the CV easier to scan.")],
        "related": [("/how-to-tailor-cv-to-a-job-description", "How to tailor a CV"), ("/why-is-my-cv-not-getting-interviews", "Why your CV may not be getting responses")],
        "tool": True,
    },
    "cv-keyword-optimiser": {
        "title": "CV Keyword Optimiser | CV Optimiser",
        "meta_description": "Find missing CV keywords and learn how to use job-description language naturally without keyword stuffing.",
        "h1": "CV keyword optimiser",
        "intro": "CV keywords matter because they show relevance. The aim is not to stuff terms into your CV, but to use job-description language naturally where your experience supports it.",
        "who": ["Applicants tailoring a CV for one role", "People who suspect their CV is missing role language", "Candidates applying through ATS-heavy processes"],
        "looks_for": ["Repeated terms in the job advert", "Skills and tools missing from your CV", "Evidence behind each important keyword", "Natural wording, not forced repetition"],
        "manual": ["List the advert's repeated skills and tools", "Mark which terms are already supported in your CV", "Add missing truthful evidence", "Remove keywords that you cannot honestly support"],
        "sections": [("How to use keywords well", "Use keywords where they help explain real experience. A strong CV combines the right terms with proof, scope and outcomes."), ("What to avoid", "Do not paste a block of keywords into the CV. Recruiters still need readable, credible evidence.")],
        "related": [("/ats-cv-checker", "ATS CV checker"), ("/cv-job-description-match", "CV job description match"), ("/best-cv-format-for-ats", "Best CV format for ATS")],
        "tool": True,
    },
    "cv-cover-letter-match": CORE_SEO_PAGE_SPECS["cv-cover-letter-match"],
}

TEN_SECOND_CV_TEST_PAGE: dict[str, Any] = {
    "title": "10-Second CV Test | CV Optimiser",
    "meta_description": "Use the 10-second CV test to check whether your CV quickly shows the role you want, the experience you offer, and why you match the job description.",
    "h1": "The 10-Second CV Test",
    "intro": "Recruiters do not study your CV at first. They scan it. This quick test helps you see whether your CV makes the right impression fast.",
    "checks": [
        "Can someone tell the role you are targeting within 10 seconds?",
        "Does the top third of your CV match the job description?",
        "Are your strongest examples on page one?",
        "Do your bullets show outcomes, not just duties?",
        "Does your CV use the same language as the job advert naturally?",
        "Does your CV work when copied into plain text?",
        "Is your experience focused rather than trying to cover every possible role?",
        "Are weak or unrelated details taking up prime space?",
        "Is the CV easy to skim on mobile and desktop?",
        "Would a recruiter immediately understand why you are relevant?",
    ],
}


def guide_faqs(topic: str, outcome: str) -> list[tuple[str, str]]:
    return [
        (
            f"What should I check first for {topic}?",
            "Start with relevance. Your CV should show the target role, the right evidence and the language from the job description in the first page.",
        ),
        (
            "How many keywords should I add?",
            "Add the important keywords you can honestly support with experience. A smaller number of well-evidenced terms is better than a long list that feels forced.",
        ),
        (
            "Will an ATS reject my CV because of formatting?",
            "It can happen if core content is hidden in tables, images or unusual layouts. Clear headings, plain text order and readable bullet points are safer.",
        ),
        (
            "How does CV Optimiser help?",
            f"It compares your CV with a job description, shows missing keywords and highlights the fixes most likely to improve {outcome}.",
        ),
    ]


SEO_GUIDE_PAGE_DEFINITIONS: list[dict[str, Any]] = [
    {
        "slug": "sales-cv-keywords",
        "group": "Sales and management CVs",
        "title": "Sales CV Keywords UK | Revenue, Pipeline and Account Growth Terms",
        "meta_description": "Use the right sales CV keywords for UK roles, including revenue, pipeline, CRM, forecasting, negotiation and account growth.",
        "h1": "Sales CV keywords",
        "intro": "Sales CVs are screened for commercial evidence. If your CV talks about being personable but misses revenue, pipeline and target language, it can look weaker than your actual performance.",
        "practical": "Use keywords that match the sales model in the advert: new business, account growth, CRM, forecasting, quota, negotiation, pipeline generation, conversion and retention. Pair each term with proof such as targets, deal size, territory, account value or percentage growth.",
        "mistakes": "The common mistake is listing soft skills without commercial evidence. Avoid vague phrases such as strong communicator unless they are tied to outcomes like revenue, renewals, margin, meetings booked or pipeline created.",
        "helps": "CV Optimiser compares your sales CV with the job description and shows which commercial keywords are missing, weak or unsupported.",
        "related": [("/account-manager-cv-keywords", "Account manager CV keywords"), ("/cv-checker-for-sales-jobs", "Sales CV checker"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/example-cv-report", "Example CV report")],
    },
    {
        "slug": "account-manager-cv-keywords",
        "group": "Sales and management CVs",
        "title": "Account Manager CV Keywords UK | Retention, Growth and Stakeholder Terms",
        "meta_description": "Find account manager CV keywords for UK applications, including retention, renewals, stakeholder management, forecasting and account growth.",
        "h1": "Account manager CV keywords",
        "intro": "Account manager CVs need to prove ownership, retention and growth. A generic relationship-led CV can undersell you if it does not show portfolio size, stakeholders and commercial outcomes.",
        "practical": "Look for account ownership keywords in the advert: retention, renewals, upsell, cross-sell, forecasting, QBRs, stakeholder management, customer success, portfolio growth and commercial planning. Support them with account value, retention rate, growth percentage or renewal outcomes.",
        "mistakes": "Do not describe accounts without ownership or results. Recruiters need to see the size, complexity and outcome of your accounts, not just that you maintained relationships.",
        "helps": "CV Optimiser highlights gaps between your account management evidence and the terms recruiters are scanning for in the specific role.",
        "related": [("/sales-cv-keywords", "Sales CV keywords"), ("/cv-checker-for-sales-jobs", "Sales CV checker"), ("/job-description-cv-match", "Job description CV match"), ("/cv-summary-examples-uk", "CV summary examples")],
    },
    {
        "slug": "sales-director-cv-example",
        "group": "Sales and management CVs",
        "title": "Sales Director CV Example UK | Board-Level Commercial CV Guide",
        "meta_description": "See what a strong UK sales director CV should include, from revenue strategy and team leadership to forecasting and board reporting.",
        "h1": "Sales director CV example",
        "intro": "A sales director CV should read like a commercial leadership document, not a longer sales manager CV. It needs strategy, scale, numbers and leadership evidence.",
        "practical": "Lead with revenue responsibility, market scope, team size, strategic ownership and measurable outcomes. Include board reporting, forecasting, channel strategy, enterprise sales, margin, territory design and sales transformation where relevant.",
        "mistakes": "Avoid burying the numbers. If your CV does not quickly show revenue scale, growth, leadership scope and strategic impact, hiring managers may not see you at director level.",
        "helps": "CV Optimiser checks whether your CV gives enough leadership and commercial evidence for the seniority of the role.",
        "related": [("/sales-cv-keywords", "Sales CV keywords"), ("/cv-checker-for-management-jobs", "Management CV checker"), ("/best-cv-format-uk", "Best CV format UK"), ("/example-cv-report", "Example report")],
    },
    {
        "slug": "retail-manager-cv-example",
        "group": "Sales and management CVs",
        "title": "Retail Manager CV Example UK | Store Leadership CV Guide",
        "meta_description": "Build a stronger UK retail manager CV with examples of store performance, team leadership, stock control and customer service evidence.",
        "h1": "Retail manager CV example",
        "intro": "Retail manager CVs need to show operational control and people leadership. Hiring managers look for store performance, team management, standards and commercial awareness.",
        "practical": "Use evidence around sales performance, KPIs, team size, rota planning, stock control, loss prevention, customer experience, visual standards and training. Numbers help: store turnover, team headcount, shrinkage reduction or customer scores.",
        "mistakes": "Do not make the CV sound like a list of daily duties. Show what improved under your management and how you balanced people, standards and commercial targets.",
        "helps": "CV Optimiser compares your retail CV with a real advert and identifies missing management, operations and customer-service signals.",
        "related": [("/cv-checker-for-management-jobs", "Management CV checker"), ("/cv-mistakes-uk", "CV mistakes UK"), ("/cv-summary-examples-uk", "CV summary examples"), ("/job-description-cv-match", "Job description match")],
    },
    {
        "slug": "ats-cv-format-uk",
        "group": "ATS and keywords",
        "title": "ATS CV Format UK | Safe Formatting for Applicant Tracking Systems",
        "meta_description": "Use an ATS-friendly CV format for UK applications with clear headings, simple structure and readable keyword placement.",
        "h1": "ATS CV format UK",
        "intro": "ATS-friendly formatting is mostly about making your CV easy to parse. Good design is fine, but essential content must remain readable in plain text.",
        "practical": "Use standard headings, reverse chronological experience, simple bullet points and a clear skills section. Keep job titles, employers and dates easy to scan. Save decorative elements for non-essential details.",
        "mistakes": "Avoid putting key content inside images, icons, text boxes or complex tables. Also avoid clever headings that ATS systems and recruiters may not recognise.",
        "helps": "CV Optimiser checks readability, structure and keyword match so you can spot formatting and relevance issues before applying.",
        "related": [("/ats-cv-checker", "ATS CV checker"), ("/best-cv-format-uk", "Best CV format UK"), ("/cv-keywords-for-job-applications", "CV keywords"), ("/how-it-works", "How it works")],
    },
    {
        "slug": "cv-keywords-for-job-applications",
        "group": "ATS and keywords",
        "title": "CV Keywords for Job Applications UK | How to Use Them Naturally",
        "meta_description": "Learn how to find and use CV keywords from job descriptions without keyword stuffing or weakening your application.",
        "h1": "CV keywords for job applications",
        "intro": "CV keywords are not magic words. They are signals that your experience matches the role. The strongest CVs use them naturally and prove them with evidence.",
        "practical": "Pull keywords from repeated skills, responsibilities, tools, qualifications and outcomes in the advert. Add them to your profile, skills and experience only where they match your real background.",
        "mistakes": "Keyword stuffing makes a CV look thin and untrustworthy. Recruiters still need credible bullets, outcomes and context behind the terms.",
        "helps": "CV Optimiser finds missing and weak keywords, then helps you focus on terms that genuinely affect role fit.",
        "related": [("/cv-keyword-optimiser", "CV keyword optimiser"), ("/ats-cv-format-uk", "ATS CV format"), ("/sales-cv-keywords", "Sales CV keywords"), ("/job-description-cv-match", "Job description match")],
    },
    {
        "slug": "cv-summary-examples-uk",
        "group": "CV writing advice",
        "title": "CV Summary Examples UK | Strong Profile Openings by Role",
        "meta_description": "Write a stronger UK CV summary with practical examples for sales, account management, retail, management and career-change roles.",
        "h1": "CV summary examples UK",
        "intro": "Your CV summary should tell recruiters what role you fit and why. Generic profile lines waste the most valuable space on the page.",
        "practical": "Write three to five lines covering target role, relevant experience, strongest evidence and role-specific strengths. For sales, lead with revenue and pipeline. For management, lead with team, operations and outcomes.",
        "mistakes": "Avoid empty phrases such as hardworking, passionate and results-driven unless they are backed by specific evidence immediately after.",
        "helps": "CV Optimiser reviews whether your summary matches the job description and suggests the strongest areas to tighten.",
        "related": [("/why-is-my-cv-not-getting-interviews", "Why your CV may not be getting responses"), ("/how-to-tailor-cv-to-job-description", "Tailor your CV"), ("/account-manager-cv-keywords", "Account manager keywords"), ("/example-cv-report", "Example CV report")],
    },
    {
        "slug": "cv-mistakes-uk",
        "group": "CV writing advice",
        "title": "CV Mistakes UK | Common Issues That Cost Interviews",
        "meta_description": "Avoid common UK CV mistakes including vague profiles, weak bullets, missing keywords, poor formatting and untailored applications.",
        "h1": "CV mistakes UK",
        "intro": "Most CV mistakes are not dramatic. They are small relevance and clarity issues that make a good candidate look average during a fast scan.",
        "practical": "Check your target role, top-third summary, keyword match, bullet strength, formatting and evidence. Every major section should help the recruiter understand why you fit this job.",
        "mistakes": "The biggest mistake is using one generic CV for every application. Other costly issues include duty-only bullets, no numbers, unclear dates and weak job-description alignment.",
        "helps": "CV Optimiser gives you a score, missing keywords and priority fixes so you know what to change first.",
        "related": [("/why-is-my-cv-not-getting-interviews", "Why your CV may not be getting responses"), ("/best-cv-format-uk", "Best CV format UK"), ("/cv-score-checker", "CV score checker"), ("/example-cv-report", "Example report")],
    },
    {
        "slug": "best-cv-format-uk",
        "group": "CV writing advice",
        "title": "Best CV Format UK | Structure Your CV for Recruiters and ATS",
        "meta_description": "Use the best CV format for UK job applications with a clear profile, skills section, reverse chronological experience and ATS-friendly structure.",
        "h1": "Best CV format UK",
        "intro": "The best CV format is the one that makes relevance easy to see. For most UK job applications, simple structure beats decorative templates.",
        "practical": "Use a focused profile, key skills, reverse chronological work history, education and relevant extras. Keep the first page focused on the role you want now.",
        "mistakes": "Avoid layouts that look impressive but hide evidence. Columns, icons and heavy design can make scanning harder and parsing less reliable.",
        "helps": "CV Optimiser checks whether your CV structure and content support the role before you send it.",
        "related": [("/ats-cv-format-uk", "ATS CV format UK"), ("/ats-cv-checker", "ATS CV checker"), ("/cv-mistakes-uk", "CV mistakes UK"), ("/cv-summary-examples-uk", "CV summary examples")],
    },
    {
        "slug": "cv-checker-for-sales-jobs",
        "group": "CV checking tools",
        "title": "CV Checker for Sales Jobs | Sales CV Match and Keyword Tool",
        "meta_description": "Check a sales CV against a job description for revenue evidence, CRM terms, pipeline ownership, targets and missing keywords.",
        "h1": "CV checker for sales jobs",
        "intro": "Sales recruiters want proof of performance. A sales CV checker should look for revenue, targets, pipeline, CRM and commercial outcomes, not just confident wording.",
        "practical": "Run your CV against the advert and check whether your strongest commercial evidence is visible. Look for target achievement, deal size, sales cycle, territory, pipeline and account growth.",
        "mistakes": "Do not rely on generic sales language. If your CV does not show numbers, ownership and customer or account context, it may look light.",
        "helps": "CV Optimiser highlights sales-specific keyword gaps and shows the fixes most likely to improve match quality.",
        "related": [("/sales-cv-keywords", "Sales CV keywords"), ("/account-manager-cv-keywords", "Account manager keywords"), ("/cv-score-checker", "CV score checker"), ("/example-cv-report", "Example report")],
        "tool": True,
    },
    {
        "slug": "cv-checker-for-management-jobs",
        "group": "CV checking tools",
        "title": "CV Checker for Management Jobs | Leadership CV Review Tool",
        "meta_description": "Check a management CV for leadership scope, team results, operations, stakeholder evidence and role-specific keywords.",
        "h1": "CV checker for management jobs",
        "intro": "Management CVs need to show scope, judgement and outcomes. If your CV only lists responsibilities, it may not prove leadership level quickly enough.",
        "practical": "Check for team size, budget, KPIs, operational improvements, stakeholder management, hiring, coaching and delivery outcomes. Match the evidence to the management level in the advert.",
        "mistakes": "Avoid sounding senior without proof. Management CVs need context: scale, people, process, performance and measurable change.",
        "helps": "CV Optimiser compares your management CV with the role and shows missing leadership or operational signals.",
        "related": [("/retail-manager-cv-example", "Retail manager CV example"), ("/sales-director-cv-example", "Sales director CV example"), ("/best-cv-format-uk", "Best CV format UK"), ("/job-description-cv-match", "Job description match")],
        "tool": True,
    },
    {
        "slug": "job-description-cv-match",
        "group": "CV checking tools",
        "title": "Job Description CV Match | Compare Your CV to a Role",
        "meta_description": "Compare your CV with a job description and see whether your keywords, evidence and experience match the role.",
        "h1": "Job description CV match",
        "intro": "A strong CV is not just well written. It is clearly matched to the job description, with the most relevant evidence easy to find.",
        "practical": "Turn the advert into a checklist of must-have skills, responsibilities and outcomes. Then check where each item appears in your CV and whether it is supported by real evidence.",
        "mistakes": "Do not assume recruiters will infer relevance. If the CV uses different language or hides the strongest evidence, it can be skipped.",
        "helps": "CV Optimiser compares both documents and shows the missing keywords, weak areas and priority fixes.",
        "related": [("/how-to-tailor-cv-to-job-description", "How to tailor your CV"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/ats-cv-checker", "ATS CV checker"), ("/example-cv-report", "Example report")],
        "tool": True,
    },
]


for guide_page in SEO_GUIDE_PAGE_DEFINITIONS:
    guide_page["sections"] = [
        ("Practical advice", guide_page["practical"]),
        ("Common mistakes", guide_page["mistakes"]),
        ("How CV Optimiser helps", guide_page["helps"]),
    ]
    guide_page["who"] = guide_page.get("who", ["Job seekers preparing a targeted UK CV", "Applicants checking role fit before applying", "Candidates who want practical fixes rather than generic advice"])
    guide_page["looks_for"] = guide_page.get("looks_for", ["Relevant job-description keywords", "Evidence that proves the main requirements", "Clear CV structure and strong first-page positioning"])
    guide_page["manual"] = guide_page.get("manual", ["Compare the first page with the job advert", "Check whether each important keyword is supported by evidence", "Move the strongest relevant examples higher", "Remove detail that distracts from the target role"])
    guide_page["faqs"] = guide_page.get("faqs", guide_faqs(guide_page["h1"].lower(), "your CV match"))
    guide_page["cta_label"] = guide_page.get("cta_label", "Check your CV now")
    guide_page["cta_support"] = guide_page.get("cta_support", "Use the guide to tighten your CV, then run a real job-description match before you apply.")
    SEO_LANDING_PAGES[guide_page["slug"]] = guide_page

SEO_LANDING_PAGES["cv-keywords-for-job-applications"]["related"].append(("/guides", "All CV guides"))
SEO_LANDING_PAGES["cv-score-checker"]["faqs"] = guide_faqs("a CV score checker", "your CV score")
SEO_LANDING_PAGES["ats-cv-checker"]["faqs"] = SEO_LANDING_PAGES["ats-cv-checker"].get("faqs") or guide_faqs("an ATS CV checker", "ATS readability and role fit")
SEO_LANDING_PAGES["cv-keyword-optimiser"]["faqs"] = guide_faqs("a CV keyword optimiser", "keyword coverage")
SEO_LANDING_PAGES["why-is-my-cv-not-getting-interviews"]["faqs"] = guide_faqs("why a CV is not getting responses", "employer response")
SEO_LANDING_PAGES["how-to-tailor-cv-to-job-description"] = {
    **SEO_LANDING_PAGES["how-to-tailor-cv-to-a-job-description"],
    "slug": "how-to-tailor-cv-to-job-description",
    "title": "How to Tailor a CV to a Job Description | CV Optimiser",
    "meta_description": "Learn how to tailor your CV to a job description with keywords, relevant evidence and stronger achievement bullets.",
    "faqs": guide_faqs("tailoring a CV to a job description", "job-description match"),
    "related": [("/job-description-cv-match", "Job description CV match"), ("/cv-keyword-optimiser", "CV keyword optimiser"), ("/cv-score-checker", "CV score checker"), ("/example-cv-report", "Example CV report")],
}
SEO_LANDING_PAGES["cv-improvement-tool"] = {
    "slug": "cv-improvement-tool",
    "group": "CV checking tools",
    "title": "CV Improvement Tool | Practical CV Feedback for UK Applications",
    "meta_description": "Improve your CV with a role-specific score, missing keywords, priority fixes and practical feedback before you apply.",
    "h1": "CV improvement tool",
    "intro": "A useful CV improvement tool should tell you what to fix first. CV Optimiser focuses on role fit, missing evidence, keywords and clear next steps.",
    "sections": [
        ("Practical advice", "Improve the parts recruiters notice first: the profile, key skills, recent experience and achievement bullets. Each change should make your fit for the job description clearer."),
        ("Common mistakes", "Do not polish wording before fixing relevance. A tidy CV can still fail if it misses the role's keywords, hides strong evidence or reads like a generic career history."),
        ("How CV Optimiser helps", "CV Optimiser checks your CV against the job description, gives you a score and shows the priority fixes likely to make the biggest difference."),
    ],
    "who": ["Applicants who want clear next steps", "Job seekers improving a CV before applying", "Candidates comparing CV versions"],
    "looks_for": ["Role fit", "Keyword gaps", "Evidence strength", "CV structure and scan readability"],
    "manual": ["Start with the job description", "Fix the first page before minor wording", "Rewrite duty-only bullets", "Check the score again after changes"],
    "related": [("/cv-score-checker", "CV score checker"), ("/ats-cv-checker", "ATS CV checker"), ("/job-description-cv-match", "Job description match"), ("/example-cv-report", "Example report")],
    "faqs": guide_faqs("a CV improvement tool", "your CV match"),
    "tool": True,
    "cta_support": "Use CV Optimiser to move from general polish to role-specific CV fixes.",
}

BEST_FREE_CV_CHECKER_FAQS: list[tuple[str, str]] = [
    (
        "Is CV Optimiser free?",
        "You can run an initial CV check without needing to create an account. Paid options may be available for deeper guidance, saved results or additional features.",
    ),
    (
        "Does a CV checker guarantee interviews?",
        "No. A CV checker can help improve relevance, clarity and keyword match, but it cannot guarantee interviews. Hiring decisions depend on experience, competition, timing and employer preferences.",
    ),
    (
        "Should I check my CV against every job description?",
        "Yes, for important applications. A CV that works for one role may miss keywords or priorities for another.",
    ),
    (
        "What is an ATS CV checker?",
        "An ATS CV checker looks for issues that may affect how Applicant Tracking Systems and recruiters read your CV, such as missing keywords, unclear structure and poor role match.",
    ),
    (
        "Can I use this for UK CVs?",
        "Yes. The page and wording are aimed at UK job seekers, using CV rather than resume.",
    ),
    (
        "Should I still get a human to review my CV?",
        "For senior, specialist or high-value applications, a human review can still help. CV Optimiser is best for fast role-specific feedback and practical improvements.",
    ),
]

SITEMAP_URLS: list[dict[str, str]] = [
    {"group": "Core", "loc": canonical_url("/"), "priority": "1.0"},
    {"loc": canonical_url("/cv-checker"), "priority": "0.9"},
    {"loc": canonical_url("/best-free-cv-checker-uk"), "priority": "0.9"},
    {"loc": canonical_url("/guides"), "priority": "0.8"},
    {"group": "Guides (SEO drivers)", "loc": canonical_url("/why-is-my-cv-not-getting-interviews"), "priority": "0.8"},
    {"loc": canonical_url("/how-to-tailor-your-cv"), "priority": "0.8"},
    {"loc": canonical_url("/ats-cv-keywords"), "priority": "0.8"},
    {"loc": canonical_url("/cv-mistakes"), "priority": "0.8"},
    {"group": "Supporting", "loc": canonical_url("/how-it-works"), "priority": "0.6"},
    {"loc": canonical_url("/how-cv-optimiser-scores-your-cv"), "priority": "0.7"},
    {"loc": canonical_url("/faq"), "priority": "0.5"},
    {"loc": canonical_url("/pricing"), "priority": "0.5"},
    {"loc": canonical_url("/privacy"), "priority": "0.3"},
    {"loc": canonical_url("/terms"), "priority": "0.3"},
    {"loc": canonical_url("/contact"), "priority": "0.3"},
]

for slug in SEO_LANDING_PAGES:
    priority = "0.85" if slug in {
        "why-is-my-cv-not-getting-interviews",
        "ats-cv-checker",
        "cv-score-checker",
        "cv-keyword-optimiser",
        "cv-job-description-match",
        "free-cv-review",
    } else "0.75"
    loc = canonical_url(slug)
    if not any(entry["loc"] == loc for entry in SITEMAP_URLS):
        SITEMAP_URLS.append({"loc": loc, "priority": priority})

test_loc = canonical_url("/10-second-cv-test")
if not any(entry["loc"] == test_loc for entry in SITEMAP_URLS):
    SITEMAP_URLS.append({"loc": test_loc, "priority": "0.8"})

example_cv_report_loc = canonical_url("/example-cv-report")
if not any(entry["loc"] == example_cv_report_loc for entry in SITEMAP_URLS):
    SITEMAP_URLS.append({"loc": example_cv_report_loc, "priority": "0.7"})

for role_example_slug in ROLE_EXAMPLE_REPORTS:
    role_example_loc = canonical_url(f"/{role_example_slug}")
    if not any(entry["loc"] == role_example_loc for entry in SITEMAP_URLS):
        SITEMAP_URLS.append({"loc": role_example_loc, "priority": "0.75"})

for comparison_slug in COMPARISON_PAGES:
    comparison_loc = canonical_url(f"/{comparison_slug}")
    if not any(entry["loc"] == comparison_loc for entry in SITEMAP_URLS):
        SITEMAP_URLS.append({"loc": comparison_loc, "priority": "0.8"})


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


def is_admin_authenticated(request: Request) -> bool:
    return bool(request.session.get("admin_authenticated"))


def require_admin(request: Request) -> None:
    if not is_admin_authenticated(request):
        raise HTTPException(status_code=401, detail="Admin authentication required.")


def render_admin_login(error: str = "") -> str:
    error_html = (
        f"<p class='error'>{html.escape(error)}</p>"
        if error
        else ""
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Admin Login | CV Optimiser</title>
        <meta name="robots" content="noindex,nofollow">
        <style>
          body {{
            min-height: 100vh;
            margin: 0;
            display: grid;
            place-items: center;
            font-family: Inter, Arial, sans-serif;
            background: #07142D;
            color: #E8EEFC;
          }}
          main {{
            width: min(420px, calc(100vw - 36px));
            border: 1px solid rgba(80, 103, 146, 0.28);
            border-radius: 18px;
            background: rgba(10, 20, 40, 0.86);
            padding: 24px;
          }}
          h1 {{
            margin: 0 0 8px;
            font-size: 28px;
          }}
          p {{
            margin: 0 0 18px;
            color: #B7C6E6;
            line-height: 1.55;
          }}
          label {{
            display: block;
            margin-bottom: 8px;
            color: #9FB0D4;
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
          }}
          input {{
            width: 100%;
            box-sizing: border-box;
            min-height: 46px;
            border-radius: 12px;
            border: 1px solid rgba(160, 180, 230, 0.24);
            background: rgba(3, 10, 24, 0.72);
            color: #EEF3FF;
            padding: 10px 12px;
            font-size: 16px;
          }}
          button {{
            width: 100%;
            min-height: 46px;
            margin-top: 14px;
            border: 0;
            border-radius: 12px;
            background: #38D996;
            color: #041423;
            cursor: pointer;
            font-weight: 900;
          }}
          .error {{
            border-radius: 12px;
            background: rgba(58, 18, 29, 0.72);
            border: 1px solid rgba(192, 102, 112, 0.34);
            color: #FFD8DD;
            padding: 12px;
          }}
        </style>
      </head>
      <body>
        <main>
          <h1>Analytics Login</h1>
          <p>Enter the admin password to view internal conversion data.</p>
          {error_html}
          <form method="post" action="/admin-analytics/login">
            <label for="password">Password</label>
            <input id="password" name="password" type="password" autocomplete="current-password" autofocus>
            <button type="submit">Open dashboard</button>
          </form>
        </main>
      </body>
    </html>
    """


def mask_email(email: Optional[str]) -> str:
    clean = coerce_string(email)
    if not clean or "@" not in clean:
        return ""
    name, domain = clean.split("@", 1)
    if not name or not domain:
        return ""
    visible = name[:1]
    return f"{visible}***@{domain}"


def sanitize_analytics_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized = []
    for item in items:
        row = dict(item)
        row["email"] = mask_email(row.get("email"))
        sanitized.append(row)
    return sanitized


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
  "scoreBreakdown": [],
  "matchedKeywords": [],
  "missingKeywords": [],
  "keywordImportance": {
    "criticalMissing": [],
    "supportingKeywords": [],
    "coveredKeywords": []
  },
  "strongPoints": [],
  "weakPoints": [],
  "bulletPoints": [],
  "freeBulletRewrite": {
    "before": "",
    "whyWeak": "",
    "after": ""
  },
  "nextStep": "",
  "professionalSummary": "",
  "priorityFixes": [],
  "priorityFixDetails": [],
  "skillsSection": [],
  "atsTips": [],
  "interviewRisks": []
}
""".strip()
    else:
        output_schema = """
{
  "score": 0,
  "scoreBreakdown": [],
  "matchedKeywords": [],
  "missingKeywords": [],
  "keywordImportance": {
    "criticalMissing": [],
    "supportingKeywords": [],
    "coveredKeywords": []
  },
  "strongPoints": [],
  "weakPoints": [],
  "bulletPoints": [],
  "freeBulletRewrite": {
    "before": "",
    "whyWeak": "",
    "after": ""
  },
  "nextStep": ""
}
""".strip()

    pro_instructions = """
Additional Pro rules (this must feel like a senior recruiter review, not generic AI output):

- professionalSummary:
  Write a tight, high-quality CV summary tailored to this specific job.
  It should position the candidate strongly for THIS role, not generic roles.
  Make it 3-4 sentences and useful enough for the user to adapt into the top third of their CV.

- priorityFixes:
  Exactly 3 (not more) high-impact improvements.
  These must be the most important changes that would improve CV clarity and role match.
  Each should be specific, practical, and immediately actionable.

- priorityFixDetails:
  Exactly 3 structured fixes.
  Each item must include issue, why, and change.
  The issue should name the actual weakness.
  The why should explain recruiter/ATS impact.
  The change should be a concrete edit the user can make.

- skillsSection:
  6–10 role-aligned skills phrased the way recruiters expect to see them.

- atsTips:
  3–5 concrete keyword or phrasing improvements based on the job description.

- interviewRisks:
  3–5 realistic concerns a hiring manager or recruiter would have.
  These should feel honest and insightful, not generic.

- bulletPoints:
  Give 4–6 stronger CV bullets where the CV gives enough source material.
  Make them concise, evidence-led and suitable to paste into a CV after human review.
  Do not invent numbers, tools, employers, industries or outcomes.

CRITICAL QUALITY RULES:
- Be specific to THIS job, not generic advice
- Do not repeat content across sections
- Avoid generic phrases like "results-driven" unless clearly supported
- Make the output feel like it was written by an experienced recruiter
- Prioritise clarity and usefulness over length
- Use UK CV terminology
- Do not promise interviews, ATS success, or hiring outcomes
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
- scoreBreakdown must include role alignment, keyword coverage, evidence strength, ATS readability, and structure and clarity
- keywordImportance must separate critical missing keywords, useful supporting keywords, and keywords already covered
- strongPoints must explain what already helps this CV for this role
- weakPoints must explain what is vague, weak, missing, or likely to hurt shortlist chances
- bulletPoints must be improved CV bullet points, not advice bullets
- bulletPoints must sound stronger, clearer, and more commercially useful than the original CV
- freeBulletRewrite must rewrite one weak original CV bullet or sentence where possible
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


def stripe_id(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id")
    object_id = getattr(value, "id", None)
    return str(object_id) if object_id else str(value)


def get_available_report_purchase(user_id: str) -> Optional[dict[str, Any]]:
    result = (
        require_supabase()
        .table("report_purchases")
        .select("id, stripe_checkout_session_id")
        .eq("user_id", user_id)
        .is_("consumed_at", "null")
        .eq("status", "paid")
        .order("created_at", desc=False)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return rows[0] if rows else None


def count_available_report_purchases(user_id: str) -> int:
    result = (
        require_supabase()
        .table("report_purchases")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .is_("consumed_at", "null")
        .eq("status", "paid")
        .execute()
    )
    return result.count or 0


def grant_report_purchase(
    user_id: str,
    email: Optional[str],
    stripe_checkout_session_id: str,
    stripe_customer_id: Optional[str],
    stripe_payment_intent_id: Optional[str],
) -> None:
    existing = (
        require_supabase()
        .table("report_purchases")
        .select("id")
        .eq("stripe_checkout_session_id", stripe_checkout_session_id)
        .limit(1)
        .execute()
    )
    if existing.data:
        return

    try:
        require_supabase().table("report_purchases").insert({
            "user_id": user_id,
            "email": email,
            "stripe_checkout_session_id": stripe_checkout_session_id,
            "stripe_customer_id": stripe_customer_id,
            "stripe_payment_intent_id": stripe_payment_intent_id,
            "status": "paid",
        }).execute()
    except Exception:
        duplicate = (
            require_supabase()
            .table("report_purchases")
            .select("id")
            .eq("stripe_checkout_session_id", stripe_checkout_session_id)
            .limit(1)
            .execute()
        )
        if duplicate.data:
            return
        raise


def consume_report_purchase(user_id: str) -> Optional[str]:
    purchase = get_available_report_purchase(user_id)
    if not purchase:
        return None

    require_supabase().table("report_purchases").update({
        "consumed_at": current_utc().isoformat(),
    }).eq("id", purchase["id"]).execute()
    return coerce_string(purchase.get("stripe_checkout_session_id")) or None


def get_user_plan(user: Optional[dict[str, Any]]) -> str:
    if not user:
        return "free"
    return "pro" if get_active_subscription(user["id"]) else "free"


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


def get_report_type(payload: dict[str, Any]) -> str:
    report_access = coerce_string(payload.get("reportAccess"))
    if report_access == "one_time" or payload.get("oneTimeReportConsumed"):
        return "One-time report"
    if report_access == "subscription" or payload.get("fullReportUnlocked") or payload.get("professionalSummary"):
        return "Pro report"
    return "Free check"


FUNNEL_STEPS = [
    ("free_result_shown", "Free result shown"),
    ("unlock_intent", "Unlock intent"),
    ("checkout_started", "Checkout started"),
    ("payment_success_seen", "Payment success seen"),
    ("one_time_report_activated", "One-time report activated"),
    ("one_time_report_generated", "One-time report generated"),
]

UNLOCK_INTENT_EVENTS = {
    "unlock_clicked",
    "pro_unlock_clicked",
    "unlock_full_report_clicked",
    "upgrade_clicked",
}

TREND_EVENTS = {
    "page_view",
    "content_page_view",
    "content_to_checker_clicked",
    "cv_check_started",
    "free_result_shown",
    "unlock_intent",
    "checkout_started",
    "payment_success_seen",
    "one_time_report_generated",
}


def percent(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round((numerator / denominator) * 100, 1)


def analytics_dimension_value(metadata: dict[str, Any], keys: list[str], fallback: str = "unknown") -> str:
    for key in keys:
        value = coerce_string(metadata.get(key))
        if value:
            return value[:120]
    return fallback


def analytics_date_key(value: Any) -> str:
    raw = coerce_string(value)
    if not raw:
        return "unknown"
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except Exception:
        return raw[:10] or "unknown"


def make_dimension_row(label: str, counts: dict[str, int]) -> dict[str, Any]:
    free_results = counts.get("free_result_shown", 0)
    unlocks = counts.get("unlock_intent", 0)
    checkouts = counts.get("checkout_started", 0)
    reports = counts.get("one_time_report_generated", 0)
    page_views = counts.get("page_view", 0) + counts.get("content_page_view", 0)
    content_clicks = counts.get("content_to_checker_clicked", 0)
    return {
        "label": label,
        "page_views": page_views,
        "checker_clicks": content_clicks,
        "cv_checks": counts.get("cv_check_started", 0),
        "free_results": free_results,
        "unlock_clicks": unlocks,
        "checkout_starts": checkouts,
        "paid_reports": reports,
        "checker_click_rate": percent(content_clicks, page_views),
        "unlock_rate": percent(unlocks, free_results),
        "checkout_rate": percent(checkouts, unlocks),
        "paid_report_rate": percent(reports, checkouts),
    }


def score_band(score: Optional[int]) -> str:
    if score is None:
        return "unknown"
    if score < 45:
        return "0-44"
    if score < 60:
        return "45-59"
    if score < 75:
        return "60-74"
    return "75+"


def increment_counter(counter: dict[str, int], value: str) -> None:
    clean = coerce_string(value).strip().lower()
    if not clean:
        return
    clean = re.sub(r"\s+", " ", clean)[:120]
    counter[clean] = counter.get(clean, 0) + 1


def increment_list_counter(counter: dict[str, int], values: Any, limit: int = 8) -> None:
    if isinstance(values, list):
        for value in values[:limit]:
            if isinstance(value, dict):
                increment_counter(counter, value.get("title") or value.get("issue") or value.get("keyword") or value.get("text"))
            else:
                increment_counter(counter, value)
    elif values:
        increment_counter(counter, values)


def top_counter_items(counter: dict[str, int], limit: int = 12) -> list[dict[str, Any]]:
    return [
        {"label": label, "count": count}
        for label, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def build_analytics_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    checkout_counts: dict[str, int] = {}
    source_counts: dict[str, int] = {}
    dimension_counts: dict[str, dict[str, dict[str, int]]] = {
        "sources": {},
        "landing_pages": {},
        "campaigns": {},
    }
    trend_counts: dict[str, dict[str, int]] = {}
    score_bands: dict[str, int] = {"0-44": 0, "45-59": 0, "60-74": 0, "75+": 0}
    missing_keyword_counts: dict[str, int] = {}
    weak_point_counts: dict[str, int] = {}
    priority_fix_counts: dict[str, int] = {}
    ats_tip_counts: dict[str, int] = {}
    low_score_sources: dict[str, int] = {}
    score_total = 0
    score_count = 0
    unique_emails = set()

    for row in rows:
        event_name = coerce_string(row.get("event_name"))
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        event_keys = [event_name] if event_name else []
        if event_name in UNLOCK_INTENT_EVENTS:
            event_keys.append("unlock_intent")
        email = coerce_string(row.get("email"))
        if email:
            unique_emails.add(email.lower())
        if event_name:
            counts[event_name] = counts.get(event_name, 0) + 1
            if event_name in UNLOCK_INTENT_EVENTS:
                counts["unlock_intent"] = counts.get("unlock_intent", 0) + 1
        date_key = analytics_date_key(row.get("created_at"))
        if date_key != "unknown":
            trend_day = trend_counts.setdefault(date_key, {})
            for key in event_keys:
                if key in TREND_EVENTS:
                    trend_day[key] = trend_day.get(key, 0) + 1
        checkout_plan = coerce_string(metadata.get("checkout_plan"))
        if checkout_plan and event_name == "checkout_started":
            checkout_counts[checkout_plan] = checkout_counts.get(checkout_plan, 0) + 1
        source = coerce_string(metadata.get("source"))
        if source:
            source_counts[source] = source_counts.get(source, 0) + 1
        dimensions = {
            "sources": analytics_dimension_value(metadata, ["source", "first_source", "current_source"], "direct"),
            "landing_pages": analytics_dimension_value(metadata, ["first_landing_path", "landing_path", "current_path"], "unknown"),
            "campaigns": analytics_dimension_value(metadata, ["first_campaign", "campaign", "current_campaign"], "none"),
        }
        for dimension_name, dimension_label in dimensions.items():
            dimension_bucket = dimension_counts[dimension_name].setdefault(dimension_label, {})
            for key in event_keys:
                dimension_bucket[key] = dimension_bucket.get(key, 0) + 1
        try:
            score = int(metadata.get("score"))
        except Exception:
            score = None
        if score is not None:
            score_total += score
            score_count += 1
            band = score_band(score)
            score_bands[band] = score_bands.get(band, 0) + 1
            if score < 60:
                increment_counter(low_score_sources, dimensions["sources"])
        if event_name == "cv_check_completed":
            increment_list_counter(missing_keyword_counts, metadata.get("missing_keywords_top"))
            increment_list_counter(weak_point_counts, metadata.get("weak_points_top"))
            increment_list_counter(priority_fix_counts, metadata.get("priority_fixes_top"))
            increment_list_counter(ats_tip_counts, metadata.get("ats_tips_top"))

    funnel = []
    previous_count: Optional[int] = None
    first_count = counts.get(FUNNEL_STEPS[0][0], 0)
    for key, label in FUNNEL_STEPS:
        count = counts.get(key, 0)
        funnel.append({
            "key": key,
            "label": label,
            "count": count,
            "from_previous_rate": percent(count, previous_count) if previous_count is not None else 100.0,
            "from_start_rate": percent(count, first_count) if first_count else 0.0,
            "drop_from_previous": max(0, (previous_count or count) - count) if previous_count is not None else 0,
        })
        previous_count = count

    dimension_tables = {
        name: sorted(
            [make_dimension_row(label, bucket_counts) for label, bucket_counts in buckets.items()],
            key=lambda row: (
                row["paid_reports"],
                row["checkout_starts"],
                row["unlock_clicks"],
                row["free_results"],
                row["page_views"],
            ),
            reverse=True,
        )[:25]
        for name, buckets in dimension_counts.items()
    }
    daily_trends = [
        {"date": date, **{key: trend_counts[date].get(key, 0) for key in sorted(TREND_EVENTS)}}
        for date in sorted(trend_counts)
    ]

    return {
        "total_events": len(rows),
        "unique_emails": len(unique_emails),
        "average_score": round(score_total / score_count, 1) if score_count else None,
        "counts": counts,
        "checkout_counts": checkout_counts,
        "source_counts": source_counts,
        "dimension_tables": dimension_tables,
        "daily_trends": daily_trends,
        "quality_audit": {
            "analysed_results": score_count,
            "score_bands": score_bands,
            "common_missing_keywords": top_counter_items(missing_keyword_counts),
            "common_weak_points": top_counter_items(weak_point_counts),
            "common_priority_fixes": top_counter_items(priority_fix_counts),
            "common_ats_tips": top_counter_items(ats_tip_counts),
            "low_score_sources": top_counter_items(low_score_sources, 8),
        },
        "funnel": funnel,
        "key_metrics": {
            "free_results": counts.get("free_result_shown", 0),
            "unlock_clicks": counts.get("unlock_intent", 0),
            "checkout_starts": counts.get("checkout_started", 0),
            "payment_successes": counts.get("payment_success_seen", 0),
            "one_time_reports_generated": counts.get("one_time_report_generated", 0),
            "saved_report_downloads": counts.get("saved_report_downloaded", 0),
            "report_downloads": counts.get("report_downloaded", 0),
        },
    }


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


def coerce_named_score_list(value: Any, max_items: int = 5) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []

    items: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        label = coerce_string(item.get("label") or item.get("name") or item.get("area"))
        detail = coerce_string(item.get("detail") or item.get("reason") or item.get("copy"))
        try:
            score = int(item.get("score", 0))
        except Exception:
            score = 0
        score = max(0, min(100, score))
        if label:
            items.append({"label": label, "score": score, "detail": detail})
        if len(items) >= max_items:
            break
    return items


def coerce_priority_fix_details(value: Any, max_items: int = 3) -> list[dict[str, str]]:
    if not isinstance(value, list):
        return []

    items: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            issue = coerce_string(item.get("issue") or item.get("title"))
            why = coerce_string(item.get("why") or item.get("whyItMatters") or item.get("reason"))
            change = coerce_string(item.get("change") or item.get("whatToChange") or item.get("fix"))
        else:
            issue = coerce_string(item)
            why = ""
            change = ""
        if issue:
            items.append({"issue": issue, "why": why, "change": change})
        if len(items) >= max_items:
            break
    return items


def coerce_keyword_importance(value: Any) -> dict[str, list[str]]:
    if not isinstance(value, dict):
        value = {}
    return {
        "criticalMissing": coerce_string_list(value.get("criticalMissing"), max_items=5),
        "supportingKeywords": coerce_string_list(value.get("supportingKeywords"), max_items=6),
        "coveredKeywords": coerce_string_list(value.get("coveredKeywords"), max_items=6),
    }


def coerce_free_bullet_rewrite(value: Any) -> dict[str, str]:
    if not isinstance(value, dict):
        value = {}
    return {
        "before": coerce_string(value.get("before")),
        "whyWeak": coerce_string(value.get("whyWeak") or value.get("why")),
        "after": coerce_string(value.get("after") or value.get("improved")),
    }


def clamp_score(value: int) -> int:
    return max(0, min(100, value))


def build_score_breakdown(data: dict[str, Any]) -> list[dict[str, Any]]:
    existing = coerce_named_score_list(data.get("scoreBreakdown"))
    if len(existing) >= 5:
        return existing[:5]

    score = int(data.get("score", 0) or 0)
    matched = len(data.get("matchedKeywords", []))
    missing = len(data.get("missingKeywords", []))
    strong = len(data.get("strongPoints", []))
    weak = len(data.get("weakPoints", []))
    bullets = len(data.get("bulletPoints", []))
    total_keywords = max(1, matched + missing)
    keyword_score = clamp_score(round((matched / total_keywords) * 100))

    return [
        {
            "label": "Role alignment",
            "score": clamp_score(score + (strong * 3) - (weak * 4)),
            "detail": "How clearly the CV matches the responsibilities and seniority in the job description.",
        },
        {
            "label": "Keyword coverage",
            "score": keyword_score,
            "detail": "Whether important role terms are present naturally in the CV.",
        },
        {
            "label": "Evidence strength",
            "score": clamp_score(score - 8 + (bullets * 4) + (strong * 2)),
            "detail": "How well the CV proves impact with relevant examples rather than duty-only wording.",
        },
        {
            "label": "ATS readability",
            "score": clamp_score(score + 4 - (missing * 3)),
            "detail": "How easy the CV is likely to be for screening tools and recruiters to scan.",
        },
        {
            "label": "Structure and clarity",
            "score": clamp_score(score + (strong * 2) - (weak * 2)),
            "detail": "Whether the strongest information is clear, specific, and easy to find.",
        },
    ]


def build_priority_fix_details(data: dict[str, Any]) -> list[dict[str, str]]:
    existing = coerce_priority_fix_details(data.get("priorityFixDetails"))
    if len(existing) >= 3:
        return existing[:3]

    fixes: list[dict[str, str]] = []
    missing_keywords = data.get("missingKeywords", [])

    if missing_keywords:
        joined = ", ".join(missing_keywords[:3])
        fixes.append({
            "issue": f"Important role keywords are missing or too weak: {joined}.",
            "why": "Recruiters and ATS-style screening can miss relevant experience if the same language is not visible.",
            "change": "Add these terms only where they are true, then back them up with a concrete example or outcome.",
        })

    for weak_point in data.get("weakPoints", []):
        text = coerce_string(weak_point)
        if text:
            fixes.append({
                "issue": text,
                "why": "This makes the CV harder to shortlist because the role fit is not obvious quickly.",
                "change": "Rewrite the affected section so it names the skill, context, and result more directly.",
            })
        if len(fixes) >= 3:
            break

    next_step = coerce_string(data.get("nextStep"))
    if next_step and len(fixes) < 3:
        fixes.append({
            "issue": "The highest-value next improvement is not yet reflected strongly enough.",
            "why": "Fixing the main gap first usually improves the whole application more than small wording edits.",
            "change": next_step,
        })

    fallback_fixes = [
        {
            "issue": "The top third of the CV needs sharper role positioning.",
            "why": "Recruiters often decide quickly whether the CV is worth reading in full.",
            "change": "Open with the target role, strongest relevant skills, and one or two proof points from your experience.",
        },
        {
            "issue": "Some bullets read like responsibilities rather than evidence.",
            "why": "Duty-only wording makes it harder to see what you personally changed, improved, or delivered.",
            "change": "Rewrite bullets with action, context, and outcome, using numbers only when they are true.",
        },
        {
            "issue": "The CV needs more natural job-description language.",
            "why": "Relevant wording helps both human review and keyword-based screening.",
            "change": "Mirror the advert's important terms across your profile, skills, and recent experience sections.",
        },
    ]

    for fallback in fallback_fixes:
        if len(fixes) >= 3:
            break
        fixes.append(fallback)

    return fixes[:3]


def build_keyword_importance(data: dict[str, Any]) -> dict[str, list[str]]:
    existing = coerce_keyword_importance(data.get("keywordImportance"))
    if any(existing.values()):
        return existing

    missing = data.get("missingKeywords", [])
    matched = data.get("matchedKeywords", [])
    return {
        "criticalMissing": missing[:3],
        "supportingKeywords": missing[3:8],
        "coveredKeywords": matched[:6],
    }


def build_free_bullet_rewrite(data: dict[str, Any]) -> dict[str, str]:
    existing = coerce_free_bullet_rewrite(data.get("freeBulletRewrite"))
    if existing["before"] and existing["after"]:
        if not existing["whyWeak"]:
            existing["whyWeak"] = "The original wording does not make the result or role relevance clear enough."
        return existing

    improved = ""
    if data.get("bulletPoints"):
        improved = coerce_string(data["bulletPoints"][0])

    return {
        "before": "Responsible for managing customer accounts.",
        "whyWeak": "This describes a duty, but it does not show scope, action, or the value created.",
        "after": improved or "Managed key customer accounts by strengthening stakeholder contact, spotting commercial opportunities, and improving follow-up on priority actions.",
    }


def normalize_analysis_data(data: dict[str, Any], is_pro: bool) -> dict[str, Any]:
    try:
        score = int(data.get("score", 0))
    except Exception:
        score = 0
    score = max(0, min(100, score))

    normalized = {
        "score": score,
        "scoreBreakdown": build_score_breakdown(data),
        "matchedKeywords": coerce_string_list(data.get("matchedKeywords")),
        "missingKeywords": coerce_string_list(data.get("missingKeywords")),
        "keywordImportance": build_keyword_importance(data),
        "strongPoints": coerce_string_list(data.get("strongPoints")),
        "weakPoints": coerce_string_list(data.get("weakPoints")),
        "bulletPoints": coerce_string_list(data.get("bulletPoints")),
        "freeBulletRewrite": build_free_bullet_rewrite(data),
        "nextStep": coerce_string(data.get("nextStep")),
    }

    if is_pro:
        normalized.update({
            "professionalSummary": coerce_string(data.get("professionalSummary")),
            "priorityFixes": coerce_string_list(data.get("priorityFixes")),
            "priorityFixDetails": build_priority_fix_details(data),
            "skillsSection": coerce_string_list(data.get("skillsSection")),
            "atsTips": coerce_string_list(data.get("atsTips")),
            "interviewRisks": coerce_string_list(data.get("interviewRisks")),
            "strongerBullets": coerce_string_list(data.get("strongerBullets")),
        })
    else:
        normalized.update({
            "professionalSummary": "",
            "priorityFixes": [],
            "priorityFixDetails": build_priority_fix_details(data),
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
        report_credits = count_available_report_purchases(user_id)
        return {
            "plan": "pro",
            "is_pro": True,
            "report_credits": report_credits,
            "has_report_credit": report_credits > 0,
            "remaining_free_analyses_today": None,
        }
    used_today = count_usage_today(user_id)
    remaining = max(0, FREE_ANALYSES_PER_DAY - used_today)
    report_credits = count_available_report_purchases(user_id)
    return {
        "plan": "free",
        "is_pro": False,
        "report_credits": report_credits,
        "has_report_credit": report_credits > 0,
        "remaining_free_analyses_today": remaining,
    }


def build_faq_json_ld() -> str:
    return build_faq_json_ld_for_entries(FAQ_ENTRIES)


def build_faq_json_ld_for_entries(faqs: list[tuple[str, str]]) -> str:
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
                for question, answer in faqs
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


def log_seo_page_hit(path: str) -> None:
    print(f"SEO_PAGE_HIT: {path}")


def build_site_header_css() -> str:
    return """
          .site-header {
            margin-bottom: 24px;
            padding-bottom: 14px;
            border-bottom: 1px solid rgba(80, 103, 146, 0.24);
          }
          .site-header-inner {
            display: flex;
            flex-direction: column;
            gap: 10px;
          }
          .site-header-main {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            width: 100%;
          }
          .site-logo {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            text-decoration: none;
            min-width: 0;
          }
          .site-logo-mark {
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
            flex-shrink: 0;
          }
          .site-logo-title {
            color: #E8EEFC;
            font-size: 24px;
            letter-spacing: -0.03em;
            line-height: 1;
          }
          .site-logo-title strong { font-weight: 800; }
          .site-logo-title span { font-weight: 400; }
          .site-header-right {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-left: auto;
            min-width: 0;
          }
          .header-actions {
            display: flex;
            align-items: center;
            gap: 12px;
            margin-left: auto;
            min-width: 0;
          }
          .site-nav {
            display: flex;
            align-items: center;
            gap: 18px;
            flex-wrap: wrap;
          }
          .site-header-account-row {
            display: flex;
            align-items: center;
            gap: 10px;
            justify-content: flex-end;
            min-height: 34px;
            width: 100%;
          }
          .auth-loading-text {
            min-height: 32px;
            display: inline-flex;
            align-items: center;
            color: #AAB7D4;
            font-size: 13px;
            font-weight: 700;
          }
          .site-nav-link {
            color: #C7D4F1;
            font-size: 14px;
            font-weight: 600;
            text-decoration: none;
            transition: color 0.12s ease;
          }
          .site-nav-link:hover,
          .site-nav-link.is-active {
            color: #FFFFFF;
          }
          .site-header-cta {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 12px 16px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: #FFFFFF;
            font-size: 14px;
            font-weight: 800;
            text-decoration: none;
            box-shadow: 0 10px 24px rgba(91, 120, 255, 0.22);
            white-space: nowrap;
          }
          .header-signin-link {
            display: inline-flex;
            align-items: center;
            color: #C7D4F1;
            font-size: 14px;
            font-weight: 600;
            text-decoration: none;
            white-space: nowrap;
            transition: color 0.12s ease;
          }
          .header-signin-link:hover {
            color: #FFFFFF;
          }
          body[data-auth-state="loading"] #signInLink,
          body[data-auth-state="loading"] #accountMenuWrap {
            display: none !important;
          }
          body[data-auth-state="pro"] #upgradeLink {
            visibility: hidden;
          }
          body[data-auth-state="loading"] .site-header-cta,
          body[data-auth-state="pro"] .site-header-cta {
            visibility: hidden;
          }
          body[data-auth-plan-pending="true"] #accountMenuWrap {
            display: inline-flex !important;
          }
          .auth-placeholder {
            display: none !important;
          }
          body[data-auth-state="signed_out"] #authLoadingPlaceholder,
          body[data-auth-state="free"] #authLoadingPlaceholder,
          body[data-auth-state="pro"] #authLoadingPlaceholder,
          body[data-auth-plan-pending="true"] #authLoadingPlaceholder {
            display: none !important;
          }
          .hidden {
            display: none !important;
          }
          .account-menu-wrap {
            position: relative;
          }
          .account-menu-button,
          .account-pill {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            min-height: 34px;
            max-width: min(100%, 440px);
            padding: 6px 12px;
            border-radius: 999px;
            border: 1px solid rgba(255, 255, 255, 0.08);
            background: rgba(255, 255, 255, 0.05);
            backdrop-filter: blur(8px);
            color: #E8EEFC;
            cursor: pointer;
            text-align: left;
            box-shadow: none;
            transition: background 0.2s ease, border-color 0.2s ease, transform 0.12s ease;
            white-space: nowrap;
          }
          .account-menu-button:hover,
          .account-pill:hover {
            border-color: rgba(255, 255, 255, 0.15);
            background: rgba(255, 255, 255, 0.08);
          }
          .account-chip-text {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            min-width: 0;
          }
          .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #22c55e;
            flex: 0 0 auto;
          }
          .account-text {
            color: rgba(255, 255, 255, 0.6);
            font-size: 13px;
            font-weight: 500;
            line-height: 1.2;
            flex: 0 0 auto;
          }
          .divider {
            width: 1px;
            height: 14px;
            background: rgba(255, 255, 255, 0.15);
            flex: 0 0 auto;
          }
          .account-email {
            max-width: 220px;
            color: #FFFFFF;
            font-size: 13px;
            font-weight: 500;
            line-height: 1.2;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
            min-width: 0;
          }
          .account-avatar {
            display: none;
            width: 22px;
            height: 22px;
            border-radius: 999px;
            align-items: center;
            justify-content: center;
            background: rgba(255, 255, 255, 0.1);
            border: 1px solid rgba(255, 255, 255, 0.16);
            color: #FFFFFF;
            font-size: 11px;
            font-weight: 700;
            line-height: 1;
            text-transform: uppercase;
            flex: 0 0 auto;
          }
          .account-pill .account-plan,
          .account-pill .plan-badge {
            font-size: 11px;
            font-weight: 600;
            line-height: 1;
            text-transform: uppercase;
            padding: 2px 8px;
            border-radius: 999px;
            white-space: nowrap;
            flex: 0 0 auto;
          }
          .account-pill .account-plan.pro,
          .account-pill .plan-badge.pro {
            background: rgba(99, 102, 241, 0.2);
            color: #A5B4FC;
            border: 1px solid rgba(99, 102, 241, 0.3);
          }
          .account-pill .account-plan.free,
          .account-pill .plan-badge.free {
            background: rgba(148, 163, 184, 0.14);
            color: rgba(226, 232, 240, 0.78);
            border: 1px solid rgba(148, 163, 184, 0.24);
          }
          .account-caret,
          .dropdown-arrow {
            color: rgba(255, 255, 255, 0.5);
            font-size: 12px;
            flex: 0 0 auto;
          }
          .account-dropdown {
            position: absolute;
            right: 0;
            top: calc(100% + 10px);
            width: 240px;
            padding: 10px;
            border-radius: 16px;
            border: 1px solid rgba(80, 103, 146, 0.42);
            background: rgba(18, 29, 52, 0.98);
            box-shadow: 0 24px 60px rgba(0, 0, 0, 0.28);
            z-index: 50;
          }
          .account-dropdown.hidden {
            display: none !important;
            visibility: hidden !important;
            opacity: 0 !important;
            pointer-events: none !important;
          }
          .account-dropdown:not(.hidden) {
            display: block;
            visibility: visible;
            opacity: 1;
            pointer-events: auto;
          }
          .account-dropdown a,
          .account-dropdown button,
          .account-dropdown div {
            display: flex;
            align-items: center;
            width: 100%;
            padding: 10px 12px;
            border-radius: 12px;
            border: 0;
            background: transparent;
            color: #DCE6FF;
            font-size: 14px;
            text-decoration: none;
            text-align: left;
            box-shadow: none;
            margin: 0 0 4px;
          }
          .account-dropdown a:hover,
          .account-dropdown button:hover {
            background: rgba(31, 50, 84, 0.82);
          }
          .account-dropdown button:last-child,
          .account-dropdown div:last-child,
          .account-dropdown a:last-child {
            margin-bottom: 0;
          }
          .account-dropdown-note {
            color: #9FB0D4;
            cursor: default;
          }
          @media (max-width: 768px) {
            html,
            body {
              max-width: 100%;
              overflow-x: hidden;
            }
            .site-header {
              margin-bottom: 12px;
              padding-bottom: 10px;
            }
            .site-header-inner {
              display: flex;
              flex-direction: column;
              gap: 0;
              align-items: stretch;
              position: relative;
            }
            .site-header-main {
              display: grid;
              grid-template-columns: 1fr auto;
              gap: 10px;
              align-items: center;
              padding-right: 118px;
              width: 100%;
              max-width: 100%;
              min-width: 0;
              box-sizing: border-box;
            }
            .site-nav,
            .site-header-cta,
            .header-cta {
              display: none !important;
            }
            .site-header-right {
              display: none;
              margin-left: 0;
            }
            .header-actions {
              display: none;
              margin-left: 0;
            }
            .site-logo {
              min-width: 0;
              max-width: 100%;
              overflow: hidden;
            }
            .site-logo-title {
              font-size: 22px;
              min-width: 0;
              overflow: hidden;
              text-overflow: ellipsis;
              white-space: nowrap;
            }
            .header-signin-link {
              width: auto;
            }
            .site-header-account-row {
              position: absolute;
              top: 0;
              right: 0;
              width: auto;
              min-height: 0;
              justify-content: flex-end;
            }
            .site-header-cta {
              grid-column: 2;
              grid-row: 1;
              margin-left: 0;
              padding: 11px 14px;
              font-size: 13px;
              white-space: nowrap;
            }
            .account-menu-wrap {
              width: auto;
            }
            .account-menu-button {
              width: auto;
              max-width: 100%;
              justify-content: center;
              gap: 6px;
              padding: 6px 8px;
            }
            .account-chip-text {
              flex: 0 0 auto;
              gap: 6px;
              overflow: visible;
            }
            .account-pill .status-dot,
            .account-pill .account-text,
            .account-pill .divider,
            .account-pill .account-email {
              display: none !important;
            }
            .account-email {
              display: none;
            }
            .account-avatar {
              display: inline-flex;
              width: 34px;
              height: 34px;
              font-size: 14px;
            }
            .account-pill .account-plan,
            .account-pill .plan-badge {
              font-size: 10px;
              padding: 2px 6px;
            }
            .dropdown-arrow {
              font-size: 11px;
            }
            .account-dropdown {
              position: absolute;
              right: 0;
              top: calc(100% + 8px);
              width: 180px;
              margin-top: 8px;
            }
            .auth-loading-text {
              min-height: 34px;
              font-size: 12px;
              white-space: nowrap;
            }
            .hero,
            .page-hero {
              margin-top: 28px !important;
            }
            .hero h1,
            .page-hero h1 {
              font-size: clamp(32px, 9vw, 44px) !important;
              line-height: 1.08 !important;
            }
            .card,
            .checker-card,
            .upload-card,
            .tool-card,
            .hero-card {
              width: 100%;
              max-width: 100%;
              box-sizing: border-box;
              padding: 20px 16px !important;
              border-radius: 20px !important;
            }
            img,
            picture,
            video,
            canvas,
            iframe {
              max-width: 100%;
            }
            img {
              height: auto;
            }
          }
    """


def build_typography_css() -> str:
    return """
          h1 {
            margin: 0 0 12px;
            font-size: clamp(2.35rem, 4.8vw, 3rem);
            line-height: 1.04;
            letter-spacing: -0.04em;
            color: #F4F7FF;
            font-weight: 820;
          }
          h2 {
            margin: 32px 0 16px;
            font-size: clamp(1.5rem, 3vw, 2rem);
            line-height: 1.12;
            color: #EEF3FF;
            font-weight: 780;
          }
          h3 {
            margin: 32px 0 16px;
            font-size: clamp(1.2rem, 2.2vw, 1.45rem);
            line-height: 1.2;
            color: #EEF3FF;
            font-weight: 760;
          }
          h1:first-child,
          h2:first-child,
          h3:first-child {
            margin-top: 0;
          }
          p, li {
            color: #B7C6E6;
            line-height: 1.7;
            font-size: 16px;
          }
          @media (max-width: 900px) {
            h1 {
              font-size: clamp(2rem, 8vw, 2.55rem);
            }
            h2 {
              font-size: clamp(1.4rem, 6vw, 1.8rem);
            }
            h3 {
              font-size: clamp(1.1rem, 4.8vw, 1.3rem);
            }
            p, li {
              font-size: 15px;
              line-height: 1.6;
            }
          }
    """


def build_cta_spacing_css() -> str:
    return """
          .cta-block {
            margin-top: 32px;
            margin-bottom: 40px;
          }
          .cta-block-tight {
            margin-top: 24px;
            margin-bottom: 32px;
          }
          .cta-block-large {
            margin-top: 40px;
            margin-bottom: 56px;
          }
          .cta-button {
            display: inline-block;
            margin-top: 16px;
          }
    """


def build_mobile_layout_css() -> str:
    return """
          .page-shell,
          .page.content-page,
          .page.landing-page,
          .page.seo-page,
          .seo-page,
          .content-page,
          .landing-page {
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 48px 24px;
            box-sizing: border-box;
            border: 0;
            box-shadow: none;
            background: transparent;
          }

          .seo-page > .card,
          .content-page > .card,
          .landing-page > .card,
          .page-shell > .card,
          .page.content-page > .card,
          .page.landing-page > .card,
          .page.seo-page > .card,
          .seo-page > .content-card,
          .content-page > .content-card,
          .landing-page > .content-card,
          .page-shell > .content-card,
          .page.content-page > .content-card,
          .page.landing-page > .content-card,
          .page.seo-page > .content-card {
            border: 0;
            box-shadow: none;
            background: transparent;
          }

          .card .card,
          .card .content-card,
          .card .section-card,
          .card .info-card,
          .card .summary-box,
          .card .quick-answers-card,
          .content-card .card,
          .content-card .content-card,
          .content-card .section-card,
          .content-card .info-card,
          .content-card .summary-box,
          .content-card .quick-answers-card,
          .section-card .card,
          .section-card .content-card,
          .section-card .section-card,
          .section-card .info-card,
          .section-card .summary-box,
          .section-card .quick-answers-card {
            box-shadow: none;
          }

          /* Site-wide content layout cleanup: ordinary SEO/support sections are flat, not cards. */
          .flat-section,
          .content-section,
          .seo-section,
          .checklist-section,
          .faq-section,
          .plain-section,
          .seo-card,
          .content-card,
          .section-card,
          .info-card,
          .guide-card,
          .faq-card,
          .feature-card,
          .checklist-card {
            border: 0 !important;
            box-shadow: none !important;
            background: transparent !important;
            border-radius: 0 !important;
          }

          .flat-section,
          .content-section,
          .seo-section,
          .checklist-section,
          .faq-section,
          .plain-section,
          .seo-card {
            padding: 44px 0;
            margin: 0;
          }

          .flat-section + .flat-section,
          .content-section + .content-section,
          .seo-section + .seo-section,
          .checklist-section + .checklist-section,
          .faq-section + .faq-section,
          .plain-section + .plain-section,
          .seo-card + .seo-card {
            border-top: 1px solid rgba(160, 180, 230, 0.12) !important;
          }

          .seo-page .content-card,
          .seo-page .section-card,
          .seo-page .info-card,
          .seo-page .guide-card,
          .seo-page .faq-card,
          .seo-page .feature-card,
          .seo-page .checklist-card,
          .content-page .content-card,
          .content-page .section-card,
          .content-page .info-card,
          .content-page .guide-card,
          .content-page .faq-card,
          .content-page .feature-card,
          .content-page .checklist-card {
            border: 0 !important;
            box-shadow: none !important;
            background: transparent !important;
            border-radius: 0 !important;
            padding-left: 0 !important;
            padding-right: 0 !important;
          }

          .tool-card,
          .checker-card,
          .pricing-card,
          .auth-card,
          .cta-card,
          .highlight-card,
          .result-preview-card,
          .upgrade-card,
          .payment-card {
            border-radius: 24px;
          }

          @media (max-width: 768px) {
            html,
            body {
              width: 100%;
              max-width: 100%;
              overflow-x: hidden;
            }

            main,
            .page,
            .page-wrap,
            .page-wrapper,
            .page-container,
            .container,
            .content-container,
            .page-shell,
            .page.content-page,
            .page.landing-page,
            .page.seo-page,
            .content-page,
            .seo-page,
            .tool-page {
              width: 100%;
              max-width: 100%;
              padding-left: 16px;
              padding-right: 16px;
              box-sizing: border-box;
            }

            .hero,
            .hero-section,
            .hero-card,
            .content-section,
            .tool-section,
            .seo-section,
            .section-stack,
            .layout,
            .content-grid,
            .report-grid,
            .upgrade-grid {
              width: 100%;
              max-width: 100%;
              min-width: 0;
              box-sizing: border-box;
            }

            .layout,
            .content-grid,
            .report-grid,
            .before-after,
            .upgrade-grid {
              grid-template-columns: 1fr !important;
            }

            .card,
            .content-card,
            .tool-card,
            .example-card,
            .report-card,
            .preview-card,
            .ats-card,
            .checker-card,
            .hero-card,
            .cta-card,
            .upgrade-card,
            .upgrade-active-state,
            .final-cta,
            .result-preview-card,
            .score-block,
            .priority-card,
            .before-after-card,
            .summary-box,
            .example-row,
            .faq-item {
              width: 100%;
              max-width: 100%;
              min-width: 0;
              box-sizing: border-box;
              padding: 20px 16px;
              border-radius: 20px;
            }

            .hero-card,
            .hero-panel,
            .seo-card,
            .tool-feature,
            .bottom-cta,
            .final-cta,
            .result-preview-card,
            .card,
            .tool-card,
            .report-card,
            .preview-card,
            .example-card {
              box-shadow: none !important;
            }

            .nested-card,
            .inner-card,
            .example-inner-card,
            .report-inner-card,
            .content-card .content-card,
            .tool-card .content-card,
            .tool-card .card,
            .card .card,
            .card .content-card,
            .card .section-card,
            .card .info-card,
            .card .summary-box,
            .card .quick-answers-card,
            .content-card .card,
            .content-card .section-card,
            .content-card .info-card,
            .content-card .summary-box,
            .content-card .quick-answers-card,
            .section-card .card,
            .section-card .content-card,
            .section-card .section-card,
            .section-card .info-card,
            .section-card .summary-box,
            .section-card .quick-answers-card,
            .report-grid .card .card,
            .hero-card .card,
            .seo-card .card,
            .tool-feature .card {
              border: 0;
              box-shadow: none;
              background: transparent;
              padding: 0;
            }

            .content-card {
              padding: 18px 0 !important;
              border-radius: 0;
            }

            .page,
            .page-shell {
              padding-left: 16px !important;
              padding-right: 16px !important;
            }

            .seo-page > .card,
            .content-page > .card,
            .landing-page > .card,
            .page-shell > .card,
            .page.content-page > .card,
            .page.landing-page > .card,
            .page.seo-page > .card,
            .seo-page > .content-card,
            .content-page > .content-card,
            .landing-page > .content-card,
            .page-shell > .content-card,
            .page.content-page > .content-card,
            .page.landing-page > .content-card,
            .page.seo-page > .content-card {
              border: 0 !important;
              box-shadow: none !important;
              background: transparent !important;
              padding-left: 0 !important;
              padding-right: 0 !important;
            }

            .seo-hero,
            .content-grid,
            .report-grid {
              gap: 14px !important;
              margin-top: 18px !important;
              margin-bottom: 18px !important;
            }

            .hero-panel {
              padding: 16px 0 !important;
              border: 0 !important;
              border-top: 1px solid rgba(92, 112, 150, 0.16) !important;
              border-radius: 0 !important;
              background: transparent !important;
            }

            .bottom-cta,
            .final-cta {
              padding: 18px 14px !important;
              border-radius: 16px !important;
              background: rgba(15, 28, 50, 0.54) !important;
            }

            .flat-section,
            .content-section,
            .seo-section,
            .checklist-section,
            .faq-section,
            .plain-section,
            .seo-card {
              padding: 28px 0 !important;
              border-left: 0 !important;
              border-right: 0 !important;
              border-radius: 0 !important;
              background: transparent !important;
              box-shadow: none !important;
            }

            .tool-feature,
            .tool-card.tool-shell,
            .tool-shell {
              border: 0;
              box-shadow: none;
              background: transparent;
              padding: 0 !important;
              border-radius: 0;
            }

            .tool-feature {
              margin: 20px 0 !important;
            }

            .tool-frame {
              width: 100%;
              max-width: 100%;
              min-width: 0;
              border-radius: 0;
              box-sizing: border-box;
            }

            .report-grid > div > .card {
              padding: 16px 0 !important;
              border: 0 !important;
              border-top: 1px solid rgba(92, 112, 150, 0.16) !important;
              border-radius: 0 !important;
              background: transparent !important;
              box-shadow: none !important;
            }

            .report-grid > div > .card:first-child {
              border-top: 0 !important;
              padding-top: 0 !important;
            }

            .score-block,
            .priority-card,
            .before-after-card {
              padding: 14px !important;
              border-radius: 14px !important;
              background: rgba(10, 19, 35, 0.30) !important;
              box-shadow: none !important;
            }

            .faq-page .faq-item {
              padding: 18px 0 !important;
              border-radius: 0 !important;
              background: transparent !important;
              border-bottom: 1px solid rgba(92, 112, 150, 0.18) !important;
              box-shadow: none !important;
            }

            .faq-page .faq-item:last-child {
              border-bottom: 0 !important;
            }

            .example-improvement-section {
              margin-top: 18px !important;
              padding-top: 18px !important;
            }

            .example-improvement-grid,
            .priority-grid,
            .keyword-chip-row {
              gap: 10px !important;
            }

            .site-header,
            .site-header-account-row {
              gap: 8px !important;
            }

            .account-menu-button,
            .account-pill {
              min-height: 34px !important;
              padding: 6px 8px !important;
              border-radius: 12px !important;
            }

            .account-avatar {
              width: 24px !important;
              height: 24px !important;
              font-size: 11px !important;
            }

            .account-pill .account-plan,
            .account-pill .plan-badge,
            .plan-badge,
            .pro-badge {
              padding: 3px 7px !important;
              font-size: 10px !important;
            }

            img,
            picture,
            video,
            canvas,
            svg,
            iframe,
            .screenshot,
            .example-image,
            .report-preview,
            .preview-image {
              max-width: 100%;
              height: auto;
              box-sizing: border-box;
            }

            textarea,
            input,
            button,
            select {
              max-width: 100%;
              box-sizing: border-box;
            }

            h1 {
              font-size: clamp(32px, 9vw, 44px);
              line-height: 1.08;
            }

            h2 {
              font-size: clamp(24px, 7vw, 34px);
              line-height: 1.15;
            }

            h3 {
              font-size: clamp(21px, 6vw, 28px);
              line-height: 1.2;
            }

            p,
            li {
              font-size: 18px;
              line-height: 1.55;
              overflow-wrap: anywhere;
            }

            ul,
            ol {
              max-width: 100%;
              box-sizing: border-box;
              padding-left: 22px;
            }

            .cta,
            .cta-button,
            .checkout-btn {
              width: 100%;
              box-sizing: border-box;
              text-align: center;
            }
          }
    """


def build_site_header(active_key: Optional[str] = None, cta_href: str = "/#tool") -> str:
    nav_items = [
        ("cv-checker", "/cv-checker", "CV Checker"),
        ("guides", "/guides", "Guides"),
        ("how-it-works", "/how-it-works", "How it works"),
        ("example-cv-report", "/example-cv-report", "Example Report"),
        ("upgrade", "/upgrade", "Upgrade"),
    ]
    nav_html = "".join(
        f'<a href="{href}"'
        f'{" id=\"upgradeLink\"" if key == "upgrade" else ""}'
        f' class="site-nav-link{" is-active" if active_key == key else ""}"'
        f'{" data-upgrade-link" if key == "upgrade" else ""}>{label}</a>'
        for key, href, label in nav_items
    )
    return f"""
    <header id="siteHeader" class="site-header">
      <div class="site-header-inner">
        <div class="site-header-main">
          <a href="/" class="site-logo">
            <span class="site-logo-mark">CV</span>
            <span class="site-logo-title"><strong>CV</strong> <span>Optimiser</span></span>
          </a>
          <div class="site-header-right header-actions">
            <nav class="site-nav" aria-label="Primary">
              {nav_html}
            </nav>
            <a href="{html.escape(cta_href)}" class="site-header-cta header-cta">Check my CV</a>
          </div>
        </div>
        <div class="site-header-account-row">
          <span id="authLoadingPlaceholder" class="auth-placeholder"></span>
          <span id="authLoadingText" class="auth-loading-text">Checking account...</span>
          <a href="/#authCard" id="signInLink" class="header-signin-link hidden">Sign in</a>
          <div id="accountMenuWrap" class="account-menu-wrap hidden">
            <button id="accountMenuButton" class="account-menu-button account-pill" type="button" aria-expanded="false" aria-controls="accountDropdown">
              <span class="status-dot"></span>
              <span id="accountPillStatus" class="account-text">Signed in</span>
              <span class="divider"></span>
              <span class="account-chip-text">
                <span id="accountAvatar" class="account-avatar">A</span>
                <span id="accountEmail" class="account-email">Account</span>
                <span id="accountPlan" class="account-plan plan-badge hidden">Checking plan...</span>
              </span>
              <span class="account-caret dropdown-arrow">▾</span>
            </button>
            <div id="accountDropdown" class="account-dropdown hidden" aria-hidden="true">
              <a href="#" id="headerAccountLink" data-account-action="account">Account</a>
              <a href="/billing" id="menuManageSubBtn">Billing</a>
              <div id="headerBillingNote" class="account-dropdown-note hidden">Billing management is not available yet.</div>
              <button id="menuLogoutBtn" type="button" data-account-action="signout">Sign out</button>
            </div>
          </div>
        </div>
      </div>
    </header>
    """


def build_attribution_script() -> str:
    return """
        <script>
          (function() {
            if (window.location.pathname.indexOf("/admin") === 0) return;
            var storageKey = "cv_optimiser_attribution";

            function parseStored(value) {
              try { return JSON.parse(value); } catch (error) { return null; }
            }

            function referrerSource() {
              if (!document.referrer) return "";
              try {
                var referrerUrl = new URL(document.referrer);
                if (referrerUrl.hostname === window.location.hostname) return "";
                return referrerUrl.hostname.replace(/^www\\./, "");
              } catch (error) {
                return "";
              }
            }

            function currentAttribution() {
              var params = new URLSearchParams(window.location.search);
              var source = params.get("utm_source") || referrerSource() || "direct";
              return {
                source: source,
                medium: params.get("utm_medium") || "",
                campaign: params.get("utm_campaign") || "",
                term: params.get("utm_term") || "",
                content: params.get("utm_content") || "",
                referrer: document.referrer || "",
                landing_path: window.location.pathname,
                landing_query: window.location.search || "",
                captured_at: new Date().toISOString()
              };
            }

            function storedAttribution() {
              try { return parseStored(window.localStorage.getItem(storageKey)); } catch (error) { return null; }
            }

            function saveAttribution(value) {
              try { window.localStorage.setItem(storageKey, JSON.stringify(value)); } catch (error) {}
            }

            function metadata() {
              var current = currentAttribution();
              var first = storedAttribution();
              var hasCampaignSignal = Boolean(
                current.source !== "direct" ||
                current.medium ||
                current.campaign ||
                current.term ||
                current.content
              );
              if (!first || (first.source === "direct" && hasCampaignSignal)) {
                first = current;
                saveAttribution(first);
              }
              return {
                source: first.source || current.source || "direct",
                first_source: first.source || "direct",
                first_medium: first.medium || "",
                first_campaign: first.campaign || "",
                first_term: first.term || "",
                first_content: first.content || "",
                first_landing_path: first.landing_path || "",
                first_landing_query: first.landing_query || "",
                first_referrer: first.referrer || "",
                current_source: current.source || "direct",
                current_medium: current.medium || "",
                current_campaign: current.campaign || "",
                current_path: window.location.pathname,
                current_query: window.location.search || "",
                page_type: "content"
              };
            }

            window.CV_OPTIMISER_ATTRIBUTION = metadata;
            document.addEventListener("click", function(event) {
              var link = event.target && event.target.closest ? event.target.closest("a[href]") : null;
              if (!link) return;
              var href = link.getAttribute("href") || "";
              var destination = "";
              try {
                destination = new URL(href, window.location.origin).pathname;
              } catch (error) {
                destination = href.split("?")[0] || "";
              }
              if (destination !== "/cv-checker" && destination !== "/") return;
              fetch("/api/track", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                  event_name: "content_to_checker_clicked",
                  metadata: Object.assign(metadata(), {
                    destination_path: destination,
                    link_text: (link.textContent || "").trim().slice(0, 120)
                  })
                })
              }).catch(function() {});
            });
            window.addEventListener("load", function() {
              fetch("/api/track", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ event_name: "content_page_view", metadata: metadata() })
              }).catch(function() {});
            });
          })();
        </script>
    """


def build_footer_assets_head() -> str:
    return (
        '<link rel="stylesheet" href="/static/global-footer.css">'
        '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
        f"<script>window.CV_OPTIMISER_SUPABASE_URL = {json.dumps(SUPABASE_URL)};"
        f"window.CV_OPTIMISER_SUPABASE_ANON_KEY = {json.dumps(SUPABASE_ANON_KEY)};</script>"
        '<script src="/static/global-account.js"></script>'
        + build_attribution_script()
    )


def render_static_index() -> str:
    html_content = STATIC_INDEX_PATH.read_text(encoding="utf-8")
    return (
        html_content
        .replace('"__SUPABASE_URL__"', json.dumps(SUPABASE_URL))
        .replace('"__SUPABASE_ANON_KEY__"', json.dumps(SUPABASE_ANON_KEY))
    )


def build_site_footer() -> str:
    return '<div id="siteFooter"></div><script src="/static/global-footer.js" defer></script>'


def build_compliance_notice() -> str:
    return (
        '<section class="compliance-notice">'
        "CV Optimiser provides AI-assisted CV checks and practical suggestions. "
        "It does not guarantee interviews, job offers, ATS acceptance, or employer responses. "
        "Results are guidance only and should be reviewed before you apply."
        "</section>"
    )


def build_compliance_notice_css() -> str:
    return """
          .compliance-notice {
            margin: 22px 0;
            padding: 14px 16px;
            border: 1px solid rgba(147, 168, 218, 0.18);
            border-radius: 14px;
            background: rgba(10, 19, 35, 0.34);
            color: #AFC0E4;
            font-size: 13px;
            line-height: 1.6;
          }
    """


def build_tool_embed_script() -> str:
    return """
        <script>
          (function () {
            function resizeToolFrame(targetFrame, nextHeight) {
              if (!targetFrame || !nextHeight) return;
              const safeHeight = Math.max(Number(nextHeight) || 0, 320);
              targetFrame.style.minHeight = "0";
              targetFrame.style.height = safeHeight + "px";
              targetFrame.dataset.loaded = "true";
            }

            window.addEventListener("message", function (event) {
              if (!event || !event.data || event.data.type !== "cv-optimiser-embed-height") return;
              document.querySelectorAll("iframe.tool-frame").forEach(function (frame) {
                if (frame.contentWindow === event.source) {
                  resizeToolFrame(frame, event.data.height);
                }
              });
            });

            window.addEventListener("load", function () {
              document.querySelectorAll("iframe.tool-frame").forEach(function (frame) {
                frame.setAttribute("scrolling", "no");
                frame.style.overflow = "hidden";
              });
            });
          })();
        </script>
    """


def render_tool_landing_page(slug: str, page: dict[str, Any]) -> str:
    page_url = canonical_url(slug)
    upgrade_notice_html = ""
    upgrade_notice_script = ""
    conversion_preview_html = ""
    if slug == "cv-checker":
        upgrade_notice_html = """
          <div id="upgradeRequiredBanner" class="card upgrade-required-banner hidden" role="status" aria-live="polite">
            <strong>You need to run a CV check first before unlocking your full report.</strong>
          </div>
        """
        upgrade_notice_script = """
          <script>
            (function () {
              const banner = document.getElementById("upgradeRequiredBanner");
              if (!banner) return;
              try {
                const params = new URLSearchParams(window.location.search);
                if (params.get("upgrade_required") === "1") {
                  banner.classList.remove("hidden");
                  banner.scrollIntoView({ behavior: "smooth", block: "start" });
                }
              } catch (error) {}
            })();
          </script>
        """
        conversion_preview_html = """
          <div class="conversion-trust-row" aria-label="CV check details">
            <div class="conversion-trust-item"><span class="conversion-trust-icon">✓</span><span>No signup required</span></div>
            <div class="conversion-trust-item"><span class="conversion-trust-icon">✓</span><span>CV handling explained</span></div>
            <div class="conversion-trust-item"><span class="conversion-trust-icon">✓</span><span>Takes ~60 seconds</span></div>
          </div>
          <div class="result-preview-card" aria-label="Example CV result preview">
            <div class="result-preview-header">
              <div class="result-preview-title">Example CV Result</div>
              <div class="result-preview-score">CV Score: <span>74</span> → <strong>89</strong></div>
            </div>
            <div class="result-preview-label">Top improvements</div>
            <ul class="result-preview-list">
              <li>Add missing keywords: stakeholder, revenue, pipeline</li>
              <li>Strengthen bullet points with impact verbs</li>
              <li>Optimise formatting for ATS scanning</li>
            </ul>
            <a class="result-preview-link" href="/example-cv-report">See full example report</a>
          </div>
        """
    section_html = "".join(
        f"""
        <div class="card content-card">
          <h2>{html.escape(section["title"])}</h2>
          {f'<p>{html.escape(section["copy"])}</p>' if section.get("copy") else ""}
          {('<ul>' + ''.join(f'<li>{html.escape(item)}</li>' for item in section["bullets"]) + '</ul>') if section.get("bullets") else ""}
          {f'<p class="helper">{html.escape(section["helper"])}</p>' if section.get("helper") else ""}
          {f'<a href="{html.escape(section["link_href"])}" class="text-link">{html.escape(section["link_label"])}</a>' if section.get("link_href") and section.get("link_label") else ""}
        </div>
        """
        for section in page["sections"]
    )
    example_title = page.get("example_title", "Example CV diagnosis")
    example_score = page.get("example_score", "Score: 58/100 — needs clearer role alignment")
    example_keywords = page.get("example_keywords", ["stakeholder management", "forecasting", "commercial planning"])
    example_fixes = page.get("example_fixes", ["Add measurable results", "Strengthen your summary", "Match role keywords"])
    tool_intro_html = "".join(f"<p>{html.escape(paragraph)}</p>" for paragraph in page["tool_intro"])
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])}</title>
        <meta name="description" content="{html.escape(page["meta_description"])}">
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["meta_description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["meta_description"])}">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        {build_footer_assets_head()}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
{build_compliance_notice_css()}
.text-link {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
          }}
          .hero {{
            display: grid;
            gap: 16px;
            margin-bottom: 24px;
          }}
          .tool-card, .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          .tool-shell {{
            background: transparent;
            border: 0;
            border-radius: 0;
            padding: 0 !important;
            box-shadow: none;
          }}
          .tool-card h2, .card h2 {{
            margin: 0 0 10px;
          }}
          .tool-embed {{
            height: auto;
            max-height: none;
            overflow: visible;
          }}
          .tool-frame {{
            width: 100%;
            min-height: 980px;
            height: auto;
            max-height: none;
            border: 0;
            border-radius: 18px;
            background: transparent;
            margin-top: 18px;
            overflow: visible;
            display: block;
          }}
          .content-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1.35fr) minmax(280px, 0.9fr);
            gap: 24px;
            margin-top: 24px;
            align-items: start;
          }}
          .section-stack {{
            display: grid;
            gap: 0;
          }}
          .content-card {{
            background: transparent;
            border: 0;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
            border-radius: 0;
            padding: 24px 0;
            box-shadow: none;
          }}
          .section-stack .content-card:first-child {{
            border-top: 0;
            padding-top: 0;
          }}
          .example-card {{
            background: rgba(15, 28, 50, 0.58);
            border-color: rgba(105, 125, 170, 0.20);
          }}
          ul {{
            margin: 12px 0 0;
            padding-left: 20px;
          }}
          li {{
            margin-bottom: 8px;
          }}
          .example-mini {{
            display: grid;
            gap: 12px;
          }}
          .example-mini strong {{
            color: #EEF3FF;
            font-size: 15px;
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
          .helper {{
            margin-top: 12px;
            color: #9FB0D4;
            font-size: 13px;
          }}
          .conversion-trust-row {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            margin: 24px 0 0;
            color: rgba(211, 221, 244, 0.78);
            font-size: 13px;
            font-weight: 650;
          }}
          .conversion-trust-item {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
            white-space: nowrap;
          }}
          .conversion-trust-icon {{
            display: inline-flex;
            width: 18px;
            height: 18px;
            border-radius: 999px;
            align-items: center;
            justify-content: center;
            background: rgba(34, 197, 94, 0.12);
            border: 1px solid rgba(34, 197, 94, 0.22);
            color: #86efac;
            font-size: 11px;
            line-height: 1;
          }}
          .result-preview-card {{
            margin-top: 28px;
            padding: 22px 0 0;
            border: 0;
            border-top: 1px solid rgba(105, 125, 170, 0.22);
            border-radius: 0;
            background: transparent;
            box-shadow: none;
          }}
          .result-preview-header {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            margin-bottom: 14px;
          }}
          .result-preview-title {{
            color: #F4F7FF;
            font-size: 15px;
            font-weight: 780;
          }}
          .result-preview-score {{
            color: #C7D4F1;
            font-size: 14px;
            font-weight: 750;
            white-space: nowrap;
          }}
          .result-preview-score strong {{
            color: #86efac;
          }}
          .result-preview-label {{
            color: rgba(211, 221, 244, 0.72);
            font-size: 12px;
            font-weight: 720;
            text-transform: uppercase;
            letter-spacing: 0.04em;
            margin-bottom: 8px;
          }}
          .result-preview-list {{
            margin: 0;
            padding-left: 18px;
            color: #DCE6FF;
            font-size: 14px;
            line-height: 1.55;
          }}
          .result-preview-list li {{
            margin-bottom: 6px;
          }}
          .result-preview-link {{
            display: inline-flex;
            margin-top: 14px;
            color: #AFC0FF;
            font-size: 13px;
            font-weight: 750;
            text-decoration: underline;
            text-underline-offset: 3px;
          }}
          .upgrade-required-banner {{
            margin-bottom: 20px;
            border-color: rgba(91, 120, 255, 0.28);
            background: linear-gradient(180deg, rgba(19, 34, 64, 0.96), rgba(13, 25, 49, 0.96));
          }}
          .final-cta {{
            margin-top: 56px;
            margin-bottom: 56px;
            padding: 32px;
            border-radius: 20px;
            border: 1px solid rgba(92, 112, 150, 0.22);
            background: rgba(15, 28, 50, 0.72);
            text-align: left;
          }}
          .final-cta h2 {{
            margin-bottom: 12px;
          }}
          .final-cta p {{
            max-width: 640px;
            margin-bottom: 20px;
          }}

          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{
            .content-grid {{
              grid-template-columns: 1fr;
            }}
            .tool-frame {{
              min-height: 1120px;
            }}
          }}
          @media (max-width: 768px) {{
            .page {{
              padding: 16px 10px 44px;
            }}
            .hero {{
              gap: 12px;
              margin-bottom: 18px;
            }}
            .content-grid {{
              gap: 14px;
              margin-top: 16px;
            }}
            .section-stack {{
              gap: 0;
              margin-top: 16px;
            }}
            .content-card {{
              padding: 18px 0 !important;
            }}
            .tool-frame {{
              border-radius: 14px;
              margin-top: 14px;
            }}
            .conversion-trust-row {{
              align-items: flex-start;
              flex-direction: column;
              gap: 10px;
              margin-top: 22px;
              font-size: 13px;
            }}
            .result-preview-card {{
              margin-top: 26px;
              padding: 16px 0 0;
              border-radius: 0;
            }}
            .result-preview-header {{
              align-items: flex-start;
              flex-direction: column;
              gap: 6px;
            }}
            .result-preview-score {{
              white-space: normal;
            }}
            .final-cta {{
              margin-top: 32px;
              margin-bottom: 36px;
              padding: 18px 14px;
              border-radius: 16px;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page seo-page">
          {build_site_header(
              "upgrade" if slug == "cv-improvement-tool" else (
                  "cv-checker" if slug == "cv-checker" else (
                      "how-it-works" if slug == "how-it-works" else None
                  )
              )
          )}
          {upgrade_notice_html}
          <div class="hero">
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {conversion_preview_html}
          </div>
          <div id="landing-tool" class="tool-card tool-shell">
            <h2>{html.escape(page["tool_heading"])}</h2>
            {tool_intro_html}
            {build_compliance_notice()}
            <iframe class="tool-frame tool-embed compact" src="/?embed_tool=1&compact=1" title="{html.escape(page['h1'])} tool"></iframe>
          </div>
          <div class="content-grid">
            <div class="section-stack">{section_html}</div>
            <div class="section-stack">
              <div class="card example-card">
                <h2>{html.escape(example_title)}</h2>
                <div class="example-mini">
                  <strong>{html.escape(example_score)}</strong>
                  <div>
                    <strong>Missing keywords</strong>
                    <ul>{"".join(f"<li>{html.escape(item)}</li>" for item in example_keywords)}</ul>
                  </div>
                  <div>
                    <strong>Top fixes</strong>
                    <ul>{"".join(f"<li>{html.escape(item)}</li>" for item in example_fixes)}</ul>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <section class="final-cta">
            <h2>Check your CV now</h2>
            <p>Upload your CV, paste a job description, and get your score in under 60 seconds.</p>
            {build_compliance_notice()}
            <a href="/#tool" class="cta cta-button">Check your CV now</a>
          </section>
          {build_site_footer()}
        </div>
        {build_tool_embed_script()}
        {upgrade_notice_script}
      </body>
    </html>
    """


def render_article_page(slug: str, page: dict[str, Any]) -> str:
    page_url = canonical_url(slug)
    section_parts = []
    for section in page["sections"]:
        paragraphs_html = "".join(
            f"<p>{html.escape(paragraph)}</p>"
            for paragraph in section.get("paragraphs", [])
        )
        if section.get("copy"):
            paragraphs_html = f"<p>{html.escape(section['copy'])}</p>" + paragraphs_html
        bullets_html = ""
        if section.get("bullets"):
            bullets_html = '<ul class="section-list">' + "".join(
                f"<li>{html.escape(item)}</li>"
                for item in section["bullets"]
            ) + "</ul>"
        examples_html = ""
        if section.get("examples"):
            examples_html = '<div class="example-stack">' + "".join(
                f'<div class="example-row"><strong>{html.escape(label)}:</strong><span>{html.escape(copy)}</span></div>'
                for label, copy in section["examples"]
            ) + "</div>"
        section_parts.append(
            f"""
            <div class="section-block">
              <h2>{html.escape(section["title"])}</h2>
              {paragraphs_html}
              {bullets_html}
              {examples_html}
            </div>
            """
        )
    sections_html = "".join(section_parts)
    related_html = ""
    if page.get("related_links"):
        related_html = (
            '<div class="section-block"><h2>Related pages</h2><ul class="section-list">' +
            "".join(
                f'<li><a href="{html.escape(href)}" class="text-link inline-link">{html.escape(label)}</a></li>'
                for href, label in page["related_links"]
            ) +
            "</ul></div>"
        )
    summary_html = ""
    if page.get("summary_bullets"):
        summary_html = (
            f'<div class="summary-box"><strong>{html.escape(page.get("summary_title", "Quick summary:"))}</strong>'
            '<ul class="section-list">' +
            "".join(f"<li>{html.escape(item)}</li>" for item in page["summary_bullets"]) +
            "</ul></div>"
        )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])}</title>
        <meta name="description" content="{html.escape(page["meta_description"])}">
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["meta_description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="article">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["meta_description"])}">
        {build_footer_assets_head()}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 48px 24px 64px;
            box-sizing: border-box;
            border: 0;
            background: transparent;
            box-shadow: none;
          }}
          .page-hero {{
            margin-bottom: 32px;
          }}
          .page-hero h1 {{
            margin-bottom: 16px;
          }}
          .page-hero p {{
            max-width: 760px;
            margin-bottom: 22px;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
{build_compliance_notice_css()}
.text-link {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
          }}
          .card, .cta-card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          .summary-box {{
            margin-top: 20px;
            padding: 18px 20px;
            border-radius: 16px;
            background: rgba(10, 19, 35, 0.44);
            border: 1px solid rgba(92, 112, 150, 0.2);
          }}
          .summary-box strong {{
            display: block;
            color: #EEF3FF;
            margin-bottom: 10px;
            font-size: 15px;
          }}
          .section-list {{
            margin: 12px 0 0;
            padding-left: 20px;
          }}
          .section-list li {{
            margin-bottom: 8px;
          }}
          .example-stack {{
            display: grid;
            gap: 12px;
            margin-top: 14px;
          }}
          .example-row {{
            display: grid;
            gap: 6px;
            padding: 14px 16px;
            border-radius: 14px;
            background: rgba(10, 19, 35, 0.34);
            border: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .example-row strong {{
            color: #EEF3FF;
          }}
          .inline-link {{
            display: inline;
            margin-top: 0;
            font-size: inherit;
            font-weight: 600;
          }}
          .section-block + .section-block {{
            margin-top: 22px;
            padding-top: 22px;
            border-top: 1px solid rgba(80, 103, 146, 0.18);
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
          .final-cta {{
            margin-top: 56px;
            margin-bottom: 56px;
            padding: 32px;
            border-radius: 20px;
            border: 1px solid rgba(92, 112, 150, 0.22);
            background: rgba(15, 28, 50, 0.72);
            text-align: left;
          }}
          .final-cta h2 {{
            margin-bottom: 12px;
          }}
          .final-cta p {{
            max-width: 640px;
            margin-bottom: 20px;
          }}

          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 768px) {{
            .page {{
              padding: 16px 10px 44px;
            }}
            .card, .cta-card {{
              padding: 18px 14px;
              border-radius: 16px;
            }}
            .summary-box,
            .example-row {{
              padding: 14px;
              border-radius: 14px;
            }}
            .section-block + .section-block {{
              margin-top: 18px;
              padding-top: 18px;
            }}
            .final-cta {{
              margin-top: 32px;
              margin-bottom: 36px;
              padding: 18px 14px;
              border-radius: 16px;
            }}
            .cta {{
              width: 100%;
              box-sizing: border-box;
              text-align: center;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page landing-page">
          {build_site_header()}
          <section class="page-hero">
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {summary_html}
            <div class="cta-block-tight">
              <a href="/cv-checker" class="cta cta-button">{html.escape(page["top_cta"])}</a>
            </div>
          </section>
          {sections_html}
          {related_html}
          <section class="final-cta">
            <h2>Check your CV now</h2>
            <p>Use the CV checker to compare your CV against a real job description and see what to improve.</p>
            <a href="/cv-checker" class="cta cta-button">{html.escape(page["bottom_cta"])}</a>
          </section>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_cv_checker_page() -> str:
    page_url = canonical_url("/cv-checker")
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Free CV Checker | Compare Your CV to Any Job Description</title>
        <meta name="description" content="Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.">
        {canonical_link_tag("/cv-checker")}
        {google_tag()}
        <meta property="og:title" content="Free CV Checker | Compare Your CV to Any Job Description">
        <meta property="og:description" content="Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="Free CV Checker | Compare Your CV to Any Job Description">
        <meta name="twitter:description" content="Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        {build_footer_assets_head()}
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
{build_site_header_css()}
.text-link {{
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
          .tool-embed {{
            height: auto;
            max-height: none;
            overflow: visible;
          }}
          .tool-frame {{
            width: 100%;
            min-height: 980px;
            height: auto;
            max-height: none;
            border: 0;
            border-radius: 18px;
            background: transparent;
            overflow: visible;
            display: block;
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

          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{
            .layout {{
              grid-template-columns: 1fr;
            }}
            .tool-frame {{
              min-height: 1120px;
            }}
          }}
          @media (max-width: 768px) {{
            .page {{
              padding: 16px 10px 44px;
            }}
            .topbar {{
              margin-bottom: 18px;
            }}
            .hero {{
              gap: 12px;
              margin-bottom: 18px;
            }}
            .hero h1 {{
              font-size: clamp(32px, 9vw, 44px);
              line-height: 1.08;
            }}
            .layout,
            .section-stack {{
              gap: 14px;
            }}
            .section-stack {{
              margin-top: 16px;
            }}
            .card {{
              width: 100%;
              max-width: 100%;
              box-sizing: border-box;
              padding: 18px 14px;
              border-radius: 16px;
            }}
            h2 {{
              font-size: 20px;
              line-height: 1.2;
            }}
            p, li {{
              font-size: 15px;
              line-height: 1.6;
            }}
            .tool-frame {{
              border-radius: 14px;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page seo-page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#tool" class="header-link">Homepage tool</a>
          </div>

          <div class="hero">
            <h1>Free CV Checker</h1>
            <p>See how well your CV matches a job description and what to fix.</p>
          </div>

          <div class="layout">
            <div>
              <div class="card">
                <h2>Check my CV</h2>
                <p>Many CVs are overlooked when they do not clearly match the job description.</p>
                <p style="margin-top:12px;">Paste your CV and a job description below to get your match score and improvement suggestions.</p>
                <iframe class="tool-frame tool-embed compact" src="/?embed_tool=1&compact=1" title="CV checker tool"></iframe>
              </div>

              <div class="section-stack">
                <div class="card">
                  <h2>What this CV checker does</h2>
                  <p>This CV checker compares your CV against a job description to show:</p>
                  <ul>
                    <li>Your CV match score</li>
                    <li>Missing keywords for the role</li>
                    <li>What may be unclear</li>
                    <li>The most important improvements to make</li>
                  </ul>
                  <p style="margin-top:12px;">It’s designed to reflect how your CV is aligned with the job description.</p>
                </div>

                <div class="card">
                  <h2>Why many CVs are overlooked</h2>
                  <p>Many CVs are overlooked when relevance is unclear.</p>
                  <p style="margin-top:12px;">This usually happens because:</p>
                  <ul>
                    <li>Important keywords from the job description are missing</li>
                    <li>Experience isn’t clearly aligned to the role</li>
                    <li>Achievements are vague or not measurable</li>
                    <li>The CV doesn’t quickly show relevance</li>
                  </ul>
                  <p style="margin-top:12px;">Fixing these issues can help you submit a clearer, more targeted application.</p>
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
                  <strong>Score: 58/100 — needs clearer role alignment</strong>
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
              </div>

              <div class="card cta-block">
                <h2>Check your CV now</h2>
                <p>Upload your CV, paste a job description and get your score in under 60 seconds.</p>
                <a href="/#tool" class="cta">Check your CV now</a>
                <div class="helper-note">Prefer the homepage flow? The same tool is available there too.</div>
              </div>
            </div>
          </div>

          {build_site_footer()}
        </div>
        {build_tool_embed_script()}
      </body>
    </html>
    """


def render_ats_cv_checker_page() -> str:
    page_url = canonical_url("/ats-cv-checker")
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>ATS CV Checker | Improve Your CV for Applicant Tracking Systems</title>
        <meta name="description" content="Check how your CV performs in ATS systems. Identify missing keywords, improve your match score and improve CV clarity and role match.">
        {canonical_link_tag("/ats-cv-checker")}
        {google_tag()}
        <meta property="og:title" content="ATS CV Checker | Improve Your CV for Applicant Tracking Systems">
        <meta property="og:description" content="Check how your CV performs in ATS systems. Identify missing keywords, improve your match score and improve CV clarity and role match.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="ATS CV Checker | Improve Your CV for Applicant Tracking Systems">
        <meta name="twitter:description" content="Check how your CV performs in ATS systems. Identify missing keywords, improve your match score and improve CV clarity and role match.">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        {build_footer_assets_head()}
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
{build_typography_css()}
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
          .header-link, .text-link {{
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
          .tool-embed {{
            height: auto;
            max-height: none;
            overflow: visible;
          }}
          .tool-frame {{
            width: 100%;
            min-height: 980px;
            height: auto;
            max-height: none;
            border: 0;
            border-radius: 18px;
            background: transparent;
            overflow: visible;
            display: block;
          }}
          .cta-block {{
            text-align: left;
          }}

          .text-link:hover, .header-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{
            .layout {{
              grid-template-columns: 1fr;
            }}
            .tool-frame {{
              min-height: 1120px;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page">
          <div class="topbar">
            <a href="/" class="logo">
              <span class="logo-mark">CV</span>
              <span class="logo-title"><strong>CV</strong> <span>Optimiser</span></span>
            </a>
            <a href="/#tool" class="header-link">Homepage tool</a>
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
                <iframe class="tool-frame tool-embed compact" src="/?embed_tool=1&compact=1" title="ATS CV checker tool"></iframe>
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

            <div class="section-stack"></div>
          </div>

          {build_site_footer()}
        </div>
        {build_tool_embed_script()}
      </body>
    </html>
    """


def render_example_report_page(slug: str = "example-cv-report") -> str:
    page_url = canonical_url(slug)
    page = ROLE_EXAMPLE_REPORTS.get(slug, {
        **EXAMPLE_REPORT_PAGE,
        "role_label": "Account Manager",
        "cv_snippet": "with experience managing retail customers, coordinating account plans and supporting commercial targets.",
        "job_snippet": "We are looking for an account manager with stakeholder management, forecasting, commercial planning, retailer execution and P&L ownership experience.",
        "score": "Match Score: 58/100",
        "score_label": "Needs clearer role alignment",
        "score_copy": "This CV has relevant experience, but the strongest achievements are not obvious and several role-specific keywords are missing.",
        "keywords": ["stakeholder management", "forecasting", "commercial planning", "P&L", "retailer execution", "category growth"],
        "unclear": [
            "Commercial impact is not clear enough.",
            "Summary does not closely match the target role.",
            "Achievements are written as responsibilities rather than outcomes.",
            "Important role keywords are missing or buried.",
        ],
        "fixes": [
            ("Add measurable impact", "Replace vague responsibilities with outcomes, numbers and commercial results."),
            ("Rewrite the summary around the target role", "The summary should immediately show why this CV fits the job description."),
            ("Mirror important job description language", "Use relevant role keywords naturally so the CV feels aligned to the vacancy."),
        ],
        "weak_bullet": "Responsible for managing customer accounts and sales targets.",
        "strong_bullet": "Drove account growth by turning customer plans into measurable revenue opportunities, improving retailer execution and strengthening commercial performance.",
        "ats_checks": [
            "Core headings are readable, but the profile is too generic for the target role.",
            "Important role language appears in the job description but not strongly enough in the CV.",
            "Bullets need clearer outcomes so a recruiter can scan impact quickly.",
        ],
        "action_plan": [
            "Rewrite the top profile around account ownership and commercial impact.",
            "Add missing job-description keywords where they genuinely match experience.",
            "Replace duty-only bullets with measurable customer and revenue outcomes.",
        ],
        "related": [
            ("/account-manager-cv-example-report", "Account manager CV example report"),
            ("/sales-cv-example-report", "Sales CV example report"),
            ("/project-manager-cv-example-report", "Project manager CV example report"),
        ],
    })
    keyword_html = "".join(
        f'<span class="keyword-chip">{html.escape(keyword)}</span>'
        for keyword in page["keywords"]
    )
    unclear_html = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in page["unclear"]
    )
    fixes_html = "".join(
        f"""
        <div class="priority-card">
          <span class="priority-number">{index}</span>
          <div>
            <strong>{html.escape(title)}</strong>
            <p>{html.escape(copy)}</p>
          </div>
        </div>
        """
        for index, (title, copy) in enumerate(page["fixes"], start=1)
    )
    ats_checks_html = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in page["ats_checks"]
    )
    action_plan_html = "".join(
        f"<li>{html.escape(item)}</li>"
        for item in page["action_plan"]
    )
    related_html = "".join(
        f'<li><a href="{html.escape(href)}" class="text-link">{html.escape(label)}</a></li>'
        for href, label in page.get("related", [])
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])}</title>
        <meta name="description" content="{html.escape(page["description"])}">
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["description"])}">
        {build_footer_assets_head()}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
{build_compliance_notice_css()}
          .header-link, .text-link {{
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
          .example-improvement-section {{
            margin-top: 28px;
            padding-top: 28px;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .example-improvement-grid {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 16px;
            margin-top: 16px;
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
          .final-cta {{
            margin-top: 56px;
            margin-bottom: 56px;
            padding: 32px;
            border-radius: 20px;
            border: 1px solid rgba(92, 112, 150, 0.22);
            background: rgba(15, 28, 50, 0.72);
            text-align: left;
          }}
          .final-cta h2 {{
            margin-bottom: 12px;
          }}
          .final-cta p {{
            max-width: 640px;
            margin-bottom: 20px;
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

          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{
            .report-grid, .before-after {{
              grid-template-columns: 1fr;
            }}
          }}
          @media (max-width: 768px) {{
            .page {{
              padding: 16px 10px 44px;
            }}
            .hero-card, .card {{
              padding: 18px 14px;
              border-radius: 16px;
            }}
            .hero-card {{
              margin-bottom: 16px;
            }}
            .report-grid {{
              gap: 14px;
            }}
            .score-block,
            .before-after-card,
            .priority-card {{
              padding: 14px;
              border-radius: 14px;
            }}
            .score-value {{
              font-size: 34px;
              line-height: 1.08;
            }}
            .priority-card {{
              grid-template-columns: 1fr;
              gap: 10px;
            }}
            .example-improvement-section {{
              margin-top: 20px;
              padding-top: 20px;
            }}
            .example-improvement-grid {{
              grid-template-columns: 1fr;
              gap: 12px;
            }}
            .cta-row {{
              flex-direction: column;
            }}
            .cta {{
              width: 100%;
              box-sizing: border-box;
              text-align: center;
            }}
            .final-cta {{
              margin-top: 32px;
              margin-bottom: 36px;
              padding: 18px 14px;
              border-radius: 16px;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page seo-page">
          {build_site_header("example-cv-report")}

          <div class="hero-card">
            <div class="eyebrow">Example report</div>
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {build_compliance_notice()}
            <div class="cta-row cta-block-tight">
              <a href="/#tool" class="cta cta-button">Check your CV now</a>
            </div>
          </div>

          <div class="report-grid">
            <div>
              <div class="card">
                <h2>Example CV snippet</h2>
                <p><strong>{html.escape(page["role_label"])}</strong> {html.escape(page["cv_snippet"])}</p>
                <p class="section-helper">This is a short fictional snippet used to show the type of analysis a paid report can include.</p>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Example job description snippet</h2>
                <p>{html.escape(page["job_snippet"])}</p>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Score overview</h2>
                <div class="score-block">
                  <div class="score-value">{html.escape(page["score"])}</div>
                  <p><strong>{html.escape(page["score_label"])}</strong></p>
                  <p>{html.escape(page["score_copy"])}</p>
                </div>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Missing keywords</h2>
                <div class="keyword-chip-row">
                  {keyword_html}
                </div>
                <p class="section-helper">These are examples of keywords a recruiter or ATS may expect for this type of role.</p>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>What may be unclear</h2>
                <ul class="section-list">
                  {unclear_html}
                </ul>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Top priority fixes</h2>
                <div class="priority-grid">
                  {fixes_html}
                </div>
              </div>

              <section class="example-improvement-section">
                <h2>Weak and improved bullet examples</h2>
                <div class="example-improvement-grid">
                  <div class="before-after-card">
                    <strong>Weak bullet</strong>
                    <p>{html.escape(page["weak_bullet"])}</p>
                  </div>
                  <div class="before-after-card">
                    <strong>Improved bullet</strong>
                    <p>{html.escape(page["strong_bullet"])}</p>
                  </div>
                </div>
              </section>

              <div class="card" style="margin-top:24px;">
                <h2>ATS/readability checks</h2>
                <ul class="section-list">
                  {ats_checks_html}
                </ul>
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
                <h2>Priority action plan</h2>
                <ul class="section-list">
                  {action_plan_html}
                </ul>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Related examples and guides</h2>
                <ul class="section-list">
                  {related_html}
                </ul>
              </div>

              <div class="card" style="margin-top:24px;">
                <h2>Get this report for your CV</h2>
                <p>Run your own CV and job description through CV Optimiser to unlock the full analysis, keyword gaps, rewritten examples and priority fixes.</p>
                {build_compliance_notice()}
                <div class="cta-row cta-block-tight">
                  <a href="/#tool" class="cta cta-button">Get this report for your CV</a>
                </div>
              </div>
            </div>
          </div>

          <section class="final-cta">
            <h2>Get this report for your CV</h2>
            <p>Upload your CV, paste a job description and see the report for your own application.</p>
            {build_compliance_notice()}
            <div class="cta-row cta-block-tight">
              <a href="/#tool" class="cta cta-button">Get this report for your CV</a>
            </div>
          </section>

          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_comparison_page(slug: str, page: dict[str, Any]) -> str:
    page_url = canonical_url(slug)
    rows_html = "".join(
        f"""
        <tr>
          <th scope="row">{html.escape(label)}</th>
          <td>{html.escape(cv_optimiser)}</td>
          <td>{html.escape(alternative)}</td>
        </tr>
        """
        for label, cv_optimiser, alternative in page["rows"]
    )
    choose_us_html = "".join(f"<li>{html.escape(item)}</li>" for item in page["choose_us"])
    choose_competitor_html = "".join(f"<li>{html.escape(item)}</li>" for item in page["choose_competitor"])
    related_html = "".join(
        f'<li><a href="{html.escape(href)}" class="text-link">{html.escape(label)}</a></li>'
        for href, label in page.get("related", [])
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])}</title>
        <meta name="description" content="{html.escape(page["description"])}">
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["description"])}">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        {build_footer_assets_head()}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
{build_compliance_notice_css()}
          .comparison-hero {{
            padding-bottom: 28px;
            border-bottom: 1px solid rgba(92, 112, 150, 0.16);
          }}
          .comparison-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 22px;
            margin: 30px 0;
          }}
          .strength-strip {{
            display: grid;
            grid-template-columns: minmax(0, 1.35fr) minmax(260px, 0.75fr);
            gap: 22px;
            margin: 30px 0;
            padding: 24px;
            border-radius: 20px;
            border: 1px solid rgba(91, 120, 255, 0.28);
            background: linear-gradient(135deg, rgba(91, 120, 255, 0.16), rgba(15, 28, 50, 0.66));
          }}
          .strength-strip h2 {{
            margin-top: 0;
          }}
          .strength-points {{
            display: grid;
            gap: 10px;
            margin: 0;
            padding: 0;
            list-style: none;
          }}
          .strength-points li {{
            padding: 10px 0;
            border-top: 1px solid rgba(180, 197, 245, 0.14);
            color: #DCE6FF;
          }}
          .strength-points li:first-child {{
            border-top: 0;
          }}
          .comparison-card {{
            padding: 24px;
            border-radius: 18px;
            border: 1px solid rgba(92, 112, 150, 0.22);
            background: rgba(15, 28, 50, 0.62);
          }}
          .comparison-card h2 {{
            margin-top: 0;
          }}
          .comparison-table-wrap {{
            overflow-x: auto;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
            border-bottom: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .comparison-table {{
            width: 100%;
            border-collapse: collapse;
            min-width: 720px;
          }}
          .comparison-table th,
          .comparison-table td {{
            padding: 18px 14px;
            vertical-align: top;
            border-top: 1px solid rgba(92, 112, 150, 0.16);
            color: #B7C6E6;
            line-height: 1.55;
            text-align: left;
          }}
          .comparison-table thead th {{
            color: #EEF3FF;
            border-top: 0;
            font-size: 14px;
          }}
          .comparison-table tbody th {{
            width: 22%;
            color: #E8EEFC;
            font-size: 14px;
          }}
          .section-block {{
            padding: 32px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.16);
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
          .text-link {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 14px;
            font-weight: 700;
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
          .note {{
            color: #9FB0D4;
            font-size: 13px;
            line-height: 1.6;
          }}
          @media (max-width: 768px) {{
            .page {{
              padding: 16px 10px 44px;
            }}
            .comparison-grid {{
              grid-template-columns: 1fr;
              gap: 14px;
              margin: 22px 0;
            }}
            .strength-strip {{
              grid-template-columns: 1fr;
              gap: 14px;
              margin: 22px 0;
              padding: 18px 14px;
              border-radius: 16px;
            }}
            .comparison-card {{
              padding: 18px 14px;
              border-radius: 16px;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page seo-page">
          {build_site_header("guides")}
          <section class="comparison-hero">
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {build_compliance_notice()}
            <div class="cta-block-tight">
              <a href="/#tool" class="cta cta-button">Check your CV now</a>
            </div>
            <p class="note">This comparison is written from publicly available product positioning and is intended to help users choose the right type of tool.</p>
          </section>

          <section class="strength-strip">
            <div>
              <h2>Why CV Optimiser stands out</h2>
              <p>{html.escape(page["positioning"])}</p>
            </div>
            <ul class="strength-points">
              <li>Built around UK CV wording, not only resume wording.</li>
              <li>Starts with the job description, so the feedback is tied to the role.</li>
              <li>Shows example reports and scoring methodology before users commit.</li>
            </ul>
          </section>

          <section class="comparison-grid">
            <div class="comparison-card">
              <h2>CV Optimiser is best for</h2>
              <p>{html.escape(page["best_for_us"])}</p>
            </div>
            <div class="comparison-card">
              <h2>{html.escape(page["competitor"])} is best for</h2>
              <p>{html.escape(page["best_for_competitor"])}</p>
            </div>
          </section>

          <section class="section-block">
            <h2>Feature comparison</h2>
            <div class="comparison-table-wrap">
              <table class="comparison-table">
                <thead>
                  <tr>
                    <th>Area</th>
                    <th>CV Optimiser</th>
                    <th>{html.escape(page["competitor"])}</th>
                  </tr>
                </thead>
                <tbody>{rows_html}</tbody>
              </table>
            </div>
          </section>

          <section class="comparison-grid">
            <div class="comparison-card">
              <h2>Choose CV Optimiser if</h2>
              <ul class="section-list">{choose_us_html}</ul>
            </div>
            <div class="comparison-card">
              <h2>Consider {html.escape(page["competitor"])} if</h2>
              <ul class="section-list">{choose_competitor_html}</ul>
            </div>
          </section>

          <section class="section-block">
            <h2>Related pages</h2>
            <ul class="section-list">{related_html}</ul>
          </section>

          <section class="section-block">
            <h2>Try CV Optimiser</h2>
            <p>Paste your CV and a job description to see your match score, missing keywords and top improvements.</p>
            {build_compliance_notice()}
            <div class="cta-block-tight">
              <a href="/#tool" class="cta cta-button">Check your CV now</a>
            </div>
          </section>

          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_seo_page(slug: str, page: dict[str, Any]) -> str:
    page_url = canonical_url(slug)
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
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])} | CV Optimiser">
        <meta property="og:description" content="{html.escape(page["meta_description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])} | CV Optimiser">
        <meta name="twitter:description" content="{html.escape(page["meta_description"])}">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        <script type="application/ld+json">{build_faq_json_ld()}</script>
        {build_footer_assets_head()}
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
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
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

          .text-link {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 13px;
          }}
          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{
            .layout {{
              grid-template-columns: 1fr;
            }}

          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page seo-page">
          {build_site_header("upgrade" if slug == "cv-improvement-tool" else None)}

          <div class="layout">
            <div class="card">
              <h1>{html.escape(page["h1"])}</h1>
              <p>{html.escape(page["intro"])}</p>
              <p class="trust">Built for real job applications</p>
              <h2>What this page helps you do</h2>
              <ul>{bullet_html}</ul>
            <div class="cta-block">
              <a href="/#tool" class="cta cta-button">Check your CV now</a>
            </div>
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


def render_seo_landing_page(slug: str, page: dict[str, Any]) -> str:
    page_url = canonical_url(slug)
    cta_label = page.get("cta_label", "Check your CV against a job description")
    cta_href = page.get("cta_href", "/#tool")
    cta = f'<a href="{html.escape(cta_href)}" class="cta cta-button">{html.escape(cta_label)}</a>'
    related_html = "".join(
        f'<a href="{html.escape(href)}">{html.escape(label)}</a>'
        for href, label in page.get("related", [])
    )
    faqs = page.get("faqs") or guide_faqs(page["h1"].lower(), "your CV match")
    faq_html = "".join(
        f"""
        <div class="faq-item">
          <strong>{html.escape(question)}</strong>
          <p>{html.escape(answer)}</p>
        </div>
        """
        for question, answer in faqs[:5]
    )
    sections_html = "".join(
        f"""
        <section class="seo-section">
          <h2>{html.escape(title)}</h2>
          <p>{html.escape(copy)}</p>
        </section>
        """
        for title, copy in page.get("sections", [])
    )
    list_blocks = [
        ("Who this is for", page.get("who", [])),
        ("What the checker looks for", page.get("looks_for", [])),
        ("Quick manual checks", page.get("manual", [])),
    ]
    list_html = "".join(
        f"""
        <section class="checklist-section seo-card">
          <h2>{html.escape(title)}</h2>
          <ul>{"".join(f"<li>{html.escape(item)}</li>" for item in items)}</ul>
        </section>
        """
        for title, items in list_blocks
    )
    tool_html = ""
    if page.get("tool"):
        tool_html = """
        <section class="tool-feature" id="checker">
          <div>
            <p class="eyebrow">Try it now</p>
            <h2>Check your CV against the role</h2>
            <p>Paste your CV and a job description to see match score, missing keywords and priority fixes.</p>
          </div>
          <iframe class="tool-frame tool-embed compact" src="/?embed_tool=1&compact=1" title="CV Optimiser checker"></iframe>
        </section>
        """
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(page["title"])}</title>
        <meta name="description" content="{html.escape(page["meta_description"])}">
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["meta_description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["meta_description"])}">
        <script type="application/ld+json">{build_software_json_ld(page_url)}</script>
        <script type="application/ld+json">{build_faq_json_ld_for_entries(faqs[:5])}</script>
        {build_footer_assets_head()}
        <style>
          html, body {{
            width: 100%;
            max-width: 100%;
            overflow-x: hidden;
          }}
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page-shell {{
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
          .seo-hero {{
            display: grid;
            grid-template-columns: minmax(0, 1.5fr) minmax(280px, 0.85fr);
            gap: 24px;
            align-items: end;
            margin: 30px 0 28px;
          }}
          .seo-hero p {{
            max-width: 760px;
          }}
          .hero-panel,
          .tool-feature,
          .bottom-cta {{
            background: rgba(15, 28, 50, 0.68);
            border: 1px solid rgba(92, 112, 150, 0.20);
            border-radius: 18px;
            padding: 24px;
          }}
          .hero-panel {{
            align-self: stretch;
          }}
          .eyebrow {{
            margin: 0 0 8px;
            color: #AFC0FF;
            font-size: 12px;
            font-weight: 780;
            letter-spacing: 0.08em;
            text-transform: uppercase;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 820;
            text-decoration: none;
          }}
          .seo-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 24px;
            margin: 24px 0;
          }}
          .seo-card h2,
          .seo-section h2,
          .tool-feature h2,
          .bottom-cta h2 {{
            margin-top: 0;
          }}
          .seo-card ul {{
            margin: 12px 0 0;
            padding-left: 20px;
          }}
          .seo-card li {{
            margin-bottom: 8px;
          }}
          .seo-card,
          .checklist-section {{
            padding: 24px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
            background: transparent;
            border-radius: 0;
            box-shadow: none;
          }}
          .seo-section {{
            padding: 24px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .seo-section:first-of-type {{
            border-top: 0;
          }}
          .tool-feature {{
            margin: 28px 0;
            padding: 0;
            background: transparent;
            border: 0;
          }}
          .tool-frame {{
            width: 100%;
            min-height: 980px;
            height: auto;
            border: 0;
            border-radius: 18px;
            background: transparent;
            display: block;
            margin-top: 16px;
          }}
          .related-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 18px;
          }}
          .related-links a {{
            color: #C9D7FF;
            text-decoration: none;
            border: 1px solid rgba(92, 112, 150, 0.24);
            background: rgba(10, 19, 35, 0.42);
            border-radius: 999px;
            padding: 8px 11px;
            font-size: 13px;
            font-weight: 700;
          }}
          .bottom-cta {{
            margin: 30px 0 44px;
          }}
          .faq-section {{
            padding: 28px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .faq-list {{
            display: grid;
            gap: 16px;
            margin-top: 16px;
          }}
          .faq-item {{
            padding: 0 0 16px;
            border-bottom: 1px solid rgba(92, 112, 150, 0.14);
          }}
          .faq-item:last-child {{
            border-bottom: 0;
            padding-bottom: 0;
          }}
          .faq-item strong {{
            display: block;
            color: #EEF3FF;
            margin-bottom: 6px;
          }}
          img, svg, canvas, video, iframe {{
            max-width: 100%;
            height: auto;
          }}
          @media (max-width: 900px) {{
            .seo-hero,
            .seo-grid {{
              grid-template-columns: 1fr;
            }}
          }}
          @media (max-width: 768px) {{
            .page-shell {{
              max-width: 100%;
              padding: 16px;
            }}
            .seo-hero {{
              margin-top: 24px;
              gap: 16px;
            }}
            .hero-panel,
            .bottom-cta {{
              padding: 20px 16px;
              border-radius: 18px;
            }}
            .seo-card,
            .checklist-section {{
              padding: 22px 0;
              border-radius: 0;
            }}
            .tool-feature {{
              padding: 0;
            }}
            .tool-frame {{
              min-height: 1120px;
              border-radius: 14px;
            }}
            .cta,
            .cta-button {{
              width: 100%;
              box-sizing: border-box;
              text-align: center;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page-shell content-page">
          {build_site_header(None)}

          <section class="seo-hero">
            <div>
              <p class="eyebrow">CV Optimiser guide</p>
              <h1>{html.escape(page["h1"])}</h1>
              <p>{html.escape(page["intro"])}</p>
              <div class="cta-block-tight">{cta}</div>
            </div>
            <aside class="hero-panel">
              <h2>Fast role-fit check</h2>
              <p>{html.escape(page.get("cta_support", "Use this page to tighten your CV, then run the checker against a real job description before you apply."))}</p>
            </aside>
          </section>

          {tool_html}

          <div class="seo-grid">{list_html}</div>
          {sections_html}

          <section class="faq-section">
            <h2>Frequently asked questions</h2>
            <div class="faq-list">{faq_html}</div>
          </section>

          <section class="bottom-cta">
            <h2>Check your CV before you apply</h2>
            <p>Paste your CV and a job description into CV Optimiser to get a more detailed match report.</p>
            <div class="cta-block-tight">{cta}</div>
            <div class="related-links">{related_html}</div>
          </section>

          {build_site_footer()}
        </div>
        {build_tool_embed_script() if page.get("tool") else ""}
      </body>
    </html>
    """


def render_ten_second_cv_test_page() -> str:
    page_url = canonical_url("/10-second-cv-test")
    checks_html = "".join(
        f'<div class="check-card"><span>□</span><p>{html.escape(item)}</p></div>'
        for item in TEN_SECOND_CV_TEST_PAGE["checks"]
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(TEN_SECOND_CV_TEST_PAGE["title"])}</title>
        <meta name="description" content="{html.escape(TEN_SECOND_CV_TEST_PAGE["meta_description"])}">
        {canonical_link_tag("/10-second-cv-test")}
        {google_tag()}
        <meta property="og:title" content="{html.escape(TEN_SECOND_CV_TEST_PAGE["title"])}">
        <meta property="og:description" content="{html.escape(TEN_SECOND_CV_TEST_PAGE["meta_description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        {build_footer_assets_head()}
        <style>
          html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page-shell {{
            width: 100%;
            max-width: 1040px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
          .hero {{
            background: rgba(15, 28, 50, 0.68);
            border: 1px solid rgba(92, 112, 150, 0.20);
            border-radius: 18px;
            padding: 24px;
          }}
          .hero {{
            margin: 30px 0 22px;
          }}
          .panel {{
            padding: 32px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.16);
            background: transparent;
            border-radius: 0;
            box-shadow: none;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 820;
            text-decoration: none;
          }}
          .check-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
          }}
          .check-card {{
            display: grid;
            grid-template-columns: auto 1fr;
            gap: 10px;
            align-items: start;
            padding: 16px;
            border-radius: 16px;
            background: rgba(10, 19, 35, 0.44);
            border: 1px solid rgba(92, 112, 150, 0.20);
          }}
          .check-card span {{
            color: #AFC0FF;
            font-weight: 900;
          }}
          .section-stack {{
            display: grid;
            gap: 0;
          }}
          .score-grid {{
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
          }}
          .mini-card {{
            padding: 16px;
            border-radius: 16px;
            background: rgba(10, 19, 35, 0.44);
            border: 1px solid rgba(92, 112, 150, 0.20);
          }}
          @media (max-width: 768px) {{
            .page-shell {{ padding: 16px; }}
            .hero {{ padding: 20px 16px; }}
            .panel {{ padding: 26px 0; }}
            .check-grid, .score-grid {{ grid-template-columns: 1fr; }}
            .cta, .cta-button {{ width: 100%; box-sizing: border-box; text-align: center; }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page-shell content-page">
          {build_site_header(None)}
          <section class="hero">
            <h1>{html.escape(TEN_SECOND_CV_TEST_PAGE["h1"])}</h1>
            <p>{html.escape(TEN_SECOND_CV_TEST_PAGE["intro"])}</p>
            <div class="cta-block-tight"><a href="/#tool" class="cta cta-button">Check your CV against a job description</a></div>
          </section>
          <div class="section-stack">
            <section class="panel">
              <h2>The test</h2>
              <div class="check-grid">{checks_html}</div>
            </section>
            <section class="panel">
              <h2>Score guide</h2>
              <div class="score-grid">
                <div class="mini-card"><strong>8-10 yes answers</strong><p>Strong starting point, but still check role match.</p></div>
                <div class="mini-card"><strong>5-7 yes answers</strong><p>Likely needs clearer positioning.</p></div>
                <div class="mini-card"><strong>0-4 yes answers</strong><p>Probably too generic or hard to scan.</p></div>
              </div>
            </section>
            <section class="panel">
              <h2>What to fix first</h2>
              <ul>
                <li>Target role clarity</li>
                <li>Top-third positioning</li>
                <li>Job-description match</li>
                <li>Bullet strength</li>
                <li>Formatting and ATS readability</li>
              </ul>
            </section>
            <section class="panel">
              <h2>Get a more detailed match report</h2>
              <p>Paste your CV and a job description into CV Optimiser to get a more detailed match report.</p>
              <div class="cta-block-tight"><a href="/#tool" class="cta cta-button">Check your CV against a job description</a></div>
            </section>
          </div>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_best_free_cv_checker_page() -> str:
    page_url = canonical_url("/best-free-cv-checker-uk")
    meta_title = "Best Free CV Checker UK | Check Your CV Against a Job Description"
    meta_description = (
        "Use CV Optimiser to check your CV against a job description, find missing keywords, "
        "improve weak bullet points and spot ATS issues before you apply."
    )
    trust_bullets = [
        "No signup needed for your first result",
        "Paste your CV and job description",
        "Your CV is not stored permanently",
        "Takes around 60 seconds",
    ]
    best_for = [
        "UK job seekers applying for office, commercial, sales, marketing, finance, operations or professional roles",
        "People who have a job description and want to tailor their CV quickly",
        "Candidates getting few replies despite relevant experience",
        "People who want to improve keywords, structure and measurable achievements",
        "Anyone who wants quick feedback before paying for a full CV rewrite",
    ]
    not_for = [
        "People who need a full human-written CV from scratch",
        "Highly specialist academic, medical or legal CVs that need expert review",
        "Anyone expecting a certain interview outcome",
        "People who do not have enough work history or achievements to assess yet",
    ]
    checker_steps = [
        "Paste your CV",
        "Paste the job description",
        "Run the checker",
        "Review your score, missing keywords and suggested improvements",
        "Use the advice to tailor your CV before applying",
    ]
    checker_signals = [
        "Missing role-specific keywords",
        "Weak or generic bullet points",
        "Responsibilities without outcomes",
        "Poor match between CV and job description",
        "ATS readability issues",
        "Missing measurable achievements",
        "Overly broad personal summaries",
    ]
    strength_points = [
        "Built around UK CV wording and job-search intent",
        "Checks the CV against the exact job description instead of only giving generic advice",
        "Shows practical missing keywords, weak evidence and priority fixes",
        "Includes example reports so you can see the output before using your own CV",
        "Keeps the first check fast, with no signup needed for the first result",
    ]
    comparison_rows = [
        ("CV Optimiser", "Quick UK CV checks against a real job description, with score, missing keywords and practical next fixes", "It is automated, so important applications may still benefit from human review"),
        ("Generic CV templates", "Improving layout and structure", "They do not check fit against a specific role"),
        ("Paid CV writing services", "Full rewrite and human judgement", "More expensive and slower"),
        ("Manual self-review", "Quick final checks", "Easy to miss keyword gaps and weak evidence"),
    ]
    internal_links = [
        ("/", "CV checker"),
        ("/cv-checker", "Free CV checker"),
        ("/ats-cv-checker", "ATS CV checker"),
        ("/cv-score-checker", "CV score checker"),
        ("/cv-keyword-optimiser", "CV keyword optimiser"),
        ("/job-description-cv-match", "CV checker against job description"),
        ("/example-cv-report", "Example CV report"),
        ("/sales-cv-example-report", "Sales CV example report"),
        ("/account-manager-cv-example-report", "Account manager example report"),
        ("/cv-optimiser-vs-jobscan", "CV Optimiser vs Jobscan"),
        ("/cv-optimiser-vs-resume-worded", "CV Optimiser vs Resume Worded"),
        ("/privacy", "Privacy Policy"),
    ]
    faq_html = "".join(
        f"""
        <div class="faq-item">
          <h3>{html.escape(question)}</h3>
          <p>{html.escape(answer)}</p>
        </div>
        """
        for question, answer in BEST_FREE_CV_CHECKER_FAQS
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(meta_title)}</title>
        <meta name="description" content="{html.escape(meta_description)}">
        {canonical_link_tag("/best-free-cv-checker-uk")}
        {google_tag()}
        <meta property="og:title" content="{html.escape(meta_title)}">
        <meta property="og:description" content="{html.escape(meta_description)}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(meta_title)}">
        <meta name="twitter:description" content="{html.escape(meta_description)}">
        <script type="application/ld+json">{build_faq_json_ld_for_entries(BEST_FREE_CV_CHECKER_FAQS)}</script>
        {build_footer_assets_head()}
        <style>
          html,
          body {{
            width: 100%;
            max-width: 100%;
            overflow-x: hidden;
          }}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
          .hero {{
            display: grid;
            grid-template-columns: minmax(0, 1.45fr) minmax(280px, 0.9fr);
            gap: 28px;
            align-items: start;
            padding: 30px 0 34px;
          }}
          .hero-copy {{
            max-width: 760px;
          }}
          .hero-actions,
          .cta-actions {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 22px;
          }}
          .cta {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 14px 18px;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: white;
            font-weight: 820;
            text-decoration: none;
          }}
          .secondary-cta {{
            background: rgba(10, 19, 35, 0.42);
            border: 1px solid rgba(92, 112, 150, 0.24);
            color: #EAF0FF;
          }}
          .trust-box,
          .cta-panel {{
            padding: 22px;
            border-radius: 18px;
            border: 1px solid rgba(92, 112, 150, 0.22);
            background: rgba(15, 28, 50, 0.68);
          }}
          .trust-box h2,
          .cta-panel h2 {{
            margin-top: 0;
          }}
          .trust-list {{
            margin: 0;
            padding-left: 20px;
          }}
          .trust-list li {{
            margin-bottom: 8px;
          }}
          .content-section,
          .flat-section,
          .faq-section {{
            padding: 34px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .content-section h2,
          .flat-section h2,
          .faq-section h2 {{
            margin-top: 0;
          }}
          .split-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 28px;
          }}
          .section-list {{
            margin: 14px 0 0;
            padding-left: 20px;
          }}
          .section-list li {{
            margin-bottom: 9px;
          }}
          .numbered-list {{
            margin: 14px 0 0;
            padding-left: 24px;
          }}
          .numbered-list li {{
            margin-bottom: 10px;
          }}
          .comparison-wrap {{
            width: 100%;
            overflow-x: auto;
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            background: rgba(15, 28, 50, 0.58);
          }}
          .comparison-table {{
            width: 100%;
            min-width: 720px;
            border-collapse: collapse;
          }}
          .comparison-table th,
          .comparison-table td {{
            padding: 16px;
            text-align: left;
            vertical-align: top;
            border-bottom: 1px solid rgba(92, 112, 150, 0.16);
            color: #B7C6E6;
            line-height: 1.6;
          }}
          .comparison-table th {{
            color: #EEF3FF;
            font-size: 13px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            background: rgba(10, 19, 35, 0.44);
          }}
          .comparison-table tr:last-child td {{
            border-bottom: 0;
          }}
          .comparison-table strong {{
            color: #F4F7FF;
          }}
          .privacy-box {{
            padding: 22px;
            border-radius: 18px;
            border: 1px solid rgba(147, 168, 218, 0.18);
            background: rgba(10, 19, 35, 0.36);
          }}
          .text-link,
          .internal-links a {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-weight: 700;
          }}
          .internal-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px 16px;
            margin-top: 14px;
          }}
          .faq-list {{
            display: grid;
            gap: 18px;
            margin-top: 18px;
          }}
          .faq-item {{
            padding-bottom: 18px;
            border-bottom: 1px solid rgba(92, 112, 150, 0.14);
          }}
          .faq-item:last-child {{
            padding-bottom: 0;
            border-bottom: 0;
          }}
          .faq-item h3 {{
            margin: 0 0 8px;
            font-size: 18px;
          }}
          @media (max-width: 900px) {{
            .hero,
            .split-grid {{
              grid-template-columns: 1fr;
            }}
          }}
          @media (max-width: 768px) {{
            .page {{
              max-width: 100%;
              padding: 16px;
            }}
            .hero {{
              gap: 18px;
              padding: 24px 0 28px;
            }}
            .trust-box,
            .cta-panel,
            .privacy-box {{
              padding: 18px 16px;
              border-radius: 16px;
            }}
            .content-section,
            .flat-section,
            .faq-section {{
              padding: 28px 0;
            }}
            .hero-actions,
            .cta-actions {{
              flex-direction: column;
            }}
            .cta,
            .cta-button {{
              width: 100%;
              box-sizing: border-box;
              text-align: center;
            }}
            .comparison-wrap {{
              border-radius: 16px;
            }}
            .comparison-table th,
            .comparison-table td {{
              padding: 14px;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page seo-page">
          {build_site_header(None)}

          <section class="hero">
            <div class="hero-copy">
              <h1>Best free CV checker for UK job seekers</h1>
              <p>CV Optimiser helps you check your CV against a real job description before you apply. It highlights missing keywords, weak bullet points, ATS issues and practical ways to improve your match.</p>
              <div class="hero-actions">
                <a href="/#tool" class="cta cta-button">Check my CV</a>
                <a href="/example-cv-report" class="cta secondary-cta">See example report</a>
              </div>
            </div>
            <aside class="trust-box">
              <h2>What to expect</h2>
              <ul class="trust-list">
                {"".join(f"<li>{html.escape(item)}</li>" for item in trust_bullets)}
              </ul>
            </aside>
          </section>

          <section class="content-section">
            <h2>Quick answer: what is the best free CV checker?</h2>
            <p>The best free CV checker is one that compares your CV against the role you are applying for, not just against generic formatting rules. CV Optimiser is useful because it checks your CV against a job description and shows where your wording, keywords and evidence could be stronger.</p>
            <p>It is best suited for people who want quick, practical, role-specific feedback before applying. It is not a replacement for every type of human CV review.</p>
          </section>

          <section class="content-section">
            <h2>Why choose CV Optimiser?</h2>
            <p>CV Optimiser is intentionally focused. It is not trying to be a full job board, template library or career-management suite. It is built to answer the question that matters right before you apply: does this CV clearly match this job?</p>
            <ul class="section-list">
              {"".join(f"<li>{html.escape(item)}</li>" for item in strength_points)}
            </ul>
          </section>

          <section class="flat-section split-grid">
            <div>
              <h2>Who CV Optimiser is best for</h2>
              <ul class="section-list">
                {"".join(f"<li>{html.escape(item)}</li>" for item in best_for)}
              </ul>
            </div>
            <div>
              <h2>Who it is not for</h2>
              <ul class="section-list">
                {"".join(f"<li>{html.escape(item)}</li>" for item in not_for)}
              </ul>
            </div>
          </section>

          <section class="content-section">
            <h2>How the free CV checker works</h2>
            <ol class="numbered-list">
              {"".join(f"<li>{html.escape(item)}</li>" for item in checker_steps)}
            </ol>
          </section>

          <section class="content-section">
            <h2>What the checker looks for</h2>
            <ul class="section-list">
              {"".join(f"<li>{html.escape(item)}</li>" for item in checker_signals)}
            </ul>
          </section>

          <section class="content-section">
            <h2>CV Optimiser compared with other options</h2>
            <div class="comparison-wrap">
              <table class="comparison-table">
                <thead>
                  <tr>
                    <th>Option</th>
                    <th>Best for</th>
                    <th>Limitations</th>
                  </tr>
                </thead>
                <tbody>
                  {"".join(f"<tr><td><strong>{html.escape(option)}</strong></td><td>{html.escape(best)}</td><td>{html.escape(limit)}</td></tr>" for option, best, limit in comparison_rows)}
                </tbody>
              </table>
            </div>
          </section>

          <section class="content-section">
            <div class="privacy-box">
              <h2>Is it safe to use?</h2>
              <p>CV Optimiser is designed for quick CV feedback. You can run a check without creating an account for your first result, and the site explains how your CV text is handled. Avoid uploading sensitive personal information you do not need for a CV review.</p>
              <p><a href="/privacy" class="text-link">Read the Privacy Policy</a></p>
            </div>
          </section>

          <section class="content-section">
            <h2>Useful CV checker pages</h2>
            <p>These related pages explain specific parts of the CV checking process.</p>
            <div class="internal-links">
              {"".join(f'<a href="{html.escape(href)}">{html.escape(label)}</a>' for href, label in internal_links)}
            </div>
          </section>

          <section class="cta-panel">
            <h2>Check your CV before you apply</h2>
            <p>Paste your CV and the job description to see how well your CV matches the role and what to improve first.</p>
            <div class="cta-actions">
              <a href="/#tool" class="cta cta-button">Check my CV</a>
            </div>
          </section>

          <section class="faq-section">
            <h2>FAQs</h2>
            <div class="faq-list">{faq_html}</div>
          </section>

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
    page_url = canonical_url("/faq")
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>CV FAQ | ATS, CV Scores and Why Your CV Gets Ignored</title>
        <meta name="description" content="Direct answers on ATS filters, CV scores, keywords, tailoring your CV, and why strong candidates still get ignored.">
        {canonical_link_tag("/faq")}
        {google_tag()}
        <meta property="og:title" content="CV FAQ | ATS, CV Scores and Why Your CV Gets Ignored">
        <meta property="og:description" content="Direct answers on ATS filters, CV scores, keywords, tailoring your CV, and why strong candidates still get ignored.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="CV FAQ | ATS, CV Scores and Why Your CV Gets Ignored">
        <meta name="twitter:description" content="Direct answers on ATS filters, CV scores, keywords, tailoring your CV, and why strong candidates still get ignored.">
        <script type="application/ld+json">{build_faq_json_ld()}</script>
        {build_footer_assets_head()}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 48px 24px 64px;
            box-sizing: border-box;
            border: 0;
            background: transparent;
            box-shadow: none;
          }}
          .faq-hero {{
            margin-bottom: 28px;
          }}
          .faq-hero h1 {{
            margin-bottom: 16px;
          }}
          .faq-list {{
            display: grid;
            gap: 18px;
            margin-top: 24px;
          }}
          .faq-item {{
            padding: 20px 0;
            border-bottom: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .faq-item:last-child {{
            border-bottom: 0;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
.text-link {{
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
          .summary-box {{
            margin-top: 20px;
            padding: 18px 20px;
            border-radius: 16px;
            background: rgba(10, 19, 35, 0.44);
            border: 1px solid rgba(92, 112, 150, 0.2);
          }}
          .summary-box strong {{
            display: block;
            color: #EEF3FF;
            margin-bottom: 10px;
            font-size: 15px;
          }}
          .summary-box ul {{
            margin: 0;
            padding-left: 20px;
          }}
          .summary-box li {{
            color: #DCE6FB;
            line-height: 1.7;
            margin-bottom: 8px;
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
          .final-cta {{
            margin-top: 56px;
            margin-bottom: 56px;
            padding: 32px;
            border-radius: 20px;
            border: 1px solid rgba(92, 112, 150, 0.22);
            background: rgba(15, 28, 50, 0.72);
            text-align: left;
          }}
          .final-cta h2 {{
            margin-bottom: 12px;
          }}
          .final-cta p {{
            max-width: 640px;
            margin-bottom: 20px;
          }}
          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{

          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page content-page faq-page">
          {build_site_header()}
          <section class="faq-hero">
            <h1>Frequently asked questions</h1>
            <p>If your CV keeps getting ignored, these are the questions that actually matter.</p>
          </section>
          <section class="summary-box quick-answers-card">
            <strong>Quick answers:</strong>
            <ul>
              <li>ATS systems filter weak matches before recruiters see them</li>
              <li>Keywords matter, but only when they reflect real relevance</li>
              <li>Generic CVs lose because they make fit harder to see</li>
            </ul>
          </section>
          <div class="faq-list">{faq_html}</div>
          <section class="final-cta">
            <h2>Check your CV now</h2>
            <p>Upload your CV, paste a job description, and get your score in under 60 seconds.</p>
            <a href="/cv-checker" class="cta cta-button">Check your CV now</a>
          </section>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_guides_page() -> str:
    page_url = canonical_url("/guides")
    groups = [
        ("CV checking tools", [
            ("best-free-cv-checker-uk", "/best-free-cv-checker-uk", "Best Free CV Checker UK", "Compare your CV with a job description and see where keywords, evidence and role fit could be stronger."),
            ("cv-checker", "/cv-checker", TOOL_LANDING_PAGES["cv-checker"]["title"], "Run a general CV check and see your score, missing keywords and priority fixes."),
            ("ats-cv-checker", "/ats-cv-checker", SEO_LANDING_PAGES["ats-cv-checker"]["title"], SEO_LANDING_PAGES["ats-cv-checker"]["intro"]),
            ("cv-score-checker", "/cv-score-checker", SEO_LANDING_PAGES["cv-score-checker"]["title"], SEO_LANDING_PAGES["cv-score-checker"]["intro"]),
            ("job-description-cv-match", "/job-description-cv-match", SEO_LANDING_PAGES["job-description-cv-match"]["title"], SEO_LANDING_PAGES["job-description-cv-match"]["intro"]),
            ("cv-keyword-optimiser", "/cv-keyword-optimiser", SEO_LANDING_PAGES["cv-keyword-optimiser"]["title"], SEO_LANDING_PAGES["cv-keyword-optimiser"]["intro"]),
            ("cv-improvement-tool", "/cv-improvement-tool", SEO_LANDING_PAGES["cv-improvement-tool"]["title"], SEO_LANDING_PAGES["cv-improvement-tool"]["intro"]),
            ("cv-checker-for-sales-jobs", "/cv-checker-for-sales-jobs", SEO_LANDING_PAGES["cv-checker-for-sales-jobs"]["title"], SEO_LANDING_PAGES["cv-checker-for-sales-jobs"]["intro"]),
            ("cv-checker-for-management-jobs", "/cv-checker-for-management-jobs", SEO_LANDING_PAGES["cv-checker-for-management-jobs"]["title"], SEO_LANDING_PAGES["cv-checker-for-management-jobs"]["intro"]),
        ]),
        ("ATS and keywords", [
            ("ats-cv-format-uk", "/ats-cv-format-uk", SEO_LANDING_PAGES["ats-cv-format-uk"]["title"], SEO_LANDING_PAGES["ats-cv-format-uk"]["intro"]),
            ("cv-keywords-for-job-applications", "/cv-keywords-for-job-applications", SEO_LANDING_PAGES["cv-keywords-for-job-applications"]["title"], SEO_LANDING_PAGES["cv-keywords-for-job-applications"]["intro"]),
            ("cv-keyword-optimiser", "/cv-keyword-optimiser", SEO_LANDING_PAGES["cv-keyword-optimiser"]["title"], "Find missing role keywords and use them naturally."),
        ]),
        ("Sales and management CVs", [
            ("sales-cv-keywords", "/sales-cv-keywords", SEO_LANDING_PAGES["sales-cv-keywords"]["title"], SEO_LANDING_PAGES["sales-cv-keywords"]["intro"]),
            ("account-manager-cv-keywords", "/account-manager-cv-keywords", SEO_LANDING_PAGES["account-manager-cv-keywords"]["title"], SEO_LANDING_PAGES["account-manager-cv-keywords"]["intro"]),
            ("sales-director-cv-example", "/sales-director-cv-example", SEO_LANDING_PAGES["sales-director-cv-example"]["title"], SEO_LANDING_PAGES["sales-director-cv-example"]["intro"]),
            ("retail-manager-cv-example", "/retail-manager-cv-example", SEO_LANDING_PAGES["retail-manager-cv-example"]["title"], SEO_LANDING_PAGES["retail-manager-cv-example"]["intro"]),
        ]),
        ("CV writing advice", [
            ("why-is-my-cv-not-getting-interviews", "/why-is-my-cv-not-getting-interviews", SEO_LANDING_PAGES["why-is-my-cv-not-getting-interviews"]["title"], SEO_LANDING_PAGES["why-is-my-cv-not-getting-interviews"]["intro"]),
            ("how-to-tailor-cv-to-job-description", "/how-to-tailor-cv-to-job-description", SEO_LANDING_PAGES["how-to-tailor-cv-to-job-description"]["title"], SEO_LANDING_PAGES["how-to-tailor-cv-to-job-description"]["intro"]),
            ("cv-summary-examples-uk", "/cv-summary-examples-uk", SEO_LANDING_PAGES["cv-summary-examples-uk"]["title"], SEO_LANDING_PAGES["cv-summary-examples-uk"]["intro"]),
            ("cv-mistakes-uk", "/cv-mistakes-uk", SEO_LANDING_PAGES["cv-mistakes-uk"]["title"], SEO_LANDING_PAGES["cv-mistakes-uk"]["intro"]),
            ("best-cv-format-uk", "/best-cv-format-uk", SEO_LANDING_PAGES["best-cv-format-uk"]["title"], SEO_LANDING_PAGES["best-cv-format-uk"]["intro"]),
        ]),
        ("Examples and reports", [
            ("example-cv-report", "/example-cv-report", EXAMPLE_REPORT_PAGE["title"], EXAMPLE_REPORT_PAGE["intro"]),
            ("sales-cv-example-report", "/sales-cv-example-report", ROLE_EXAMPLE_REPORTS["sales-cv-example-report"]["title"], ROLE_EXAMPLE_REPORTS["sales-cv-example-report"]["intro"]),
            ("account-manager-cv-example-report", "/account-manager-cv-example-report", ROLE_EXAMPLE_REPORTS["account-manager-cv-example-report"]["title"], ROLE_EXAMPLE_REPORTS["account-manager-cv-example-report"]["intro"]),
            ("project-manager-cv-example-report", "/project-manager-cv-example-report", ROLE_EXAMPLE_REPORTS["project-manager-cv-example-report"]["title"], ROLE_EXAMPLE_REPORTS["project-manager-cv-example-report"]["intro"]),
            ("how-it-works", "/how-it-works", SUPPORT_PAGES["how-it-works"]["title"], SUPPORT_PAGES["how-it-works"]["intro"]),
            ("how-cv-optimiser-scores-your-cv", "/how-cv-optimiser-scores-your-cv", SUPPORT_PAGES["how-cv-optimiser-scores-your-cv"]["title"], SUPPORT_PAGES["how-cv-optimiser-scores-your-cv"]["intro"]),
        ]),
        ("Comparisons", [
            ("cv-optimiser-vs-jobscan", "/cv-optimiser-vs-jobscan", COMPARISON_PAGES["cv-optimiser-vs-jobscan"]["title"], COMPARISON_PAGES["cv-optimiser-vs-jobscan"]["intro"]),
            ("cv-optimiser-vs-resume-worded", "/cv-optimiser-vs-resume-worded", COMPARISON_PAGES["cv-optimiser-vs-resume-worded"]["title"], COMPARISON_PAGES["cv-optimiser-vs-resume-worded"]["intro"]),
            ("best-ats-cv-checker-uk", "/best-ats-cv-checker-uk", COMPARISON_PAGES["best-ats-cv-checker-uk"]["title"], COMPARISON_PAGES["best-ats-cv-checker-uk"]["intro"]),
            ("free-cv-checker-vs-paid-cv-review", "/free-cv-checker-vs-paid-cv-review", COMPARISON_PAGES["free-cv-checker-vs-paid-cv-review"]["title"], COMPARISON_PAGES["free-cv-checker-vs-paid-cv-review"]["intro"]),
        ]),
    ]
    groups_html = "".join(
        f"""
        <section class="guide-group">
          <h2>{html.escape(group_title)}</h2>
          <div class="guide-grid">
            {''.join(
                f'''
                <article class="guide-item">
                  <h3>{html.escape(title)}</h3>
                  <p>{html.escape(summary)}</p>
                  <a href="{html.escape(href)}">Read guide</a>
                </article>
                '''
                for _, href, title, summary in items
            )}
          </div>
        </section>
        """
        for group_title, items in groups
    )
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>CV Guides and Resources | CV Optimiser</title>
        <meta name="description" content="Browse practical UK CV guides covering CV checkers, ATS keywords, sales CVs, management CVs, CV formats and example reports.">
        {canonical_link_tag("/guides")}
        {google_tag()}
        <meta property="og:title" content="CV Guides and Resources | CV Optimiser">
        <meta property="og:description" content="Browse practical UK CV guides covering CV checkers, ATS keywords, sales CVs, management CVs, CV formats and example reports.">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        {build_footer_assets_head()}
        <style>
          html, body {{ width: 100%; max-width: 100%; overflow-x: hidden; }}
          body {{
            font-family: Inter, Arial, sans-serif;
            margin: 0;
            background:
              radial-gradient(circle at top left, rgba(91, 120, 255, 0.18), transparent 28%),
              radial-gradient(circle at top right, rgba(91, 120, 255, 0.10), transparent 24%),
              #07142D;
            color: #E8EEFC;
          }}
          .page-shell {{
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 32px 24px 64px;
            box-sizing: border-box;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
          .guides-hero {{
            margin: 30px 0 28px;
            max-width: 820px;
          }}
          .guide-group {{
            padding: 30px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.18);
          }}
          .guide-grid {{
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 18px 26px;
            margin-top: 16px;
          }}
          .guide-item {{
            padding: 0 0 18px;
            border-bottom: 1px solid rgba(92, 112, 150, 0.14);
          }}
          .guide-item h3 {{
            font-size: 18px;
            margin-bottom: 8px;
          }}
          .guide-item a {{
            display: inline-flex;
            margin-top: 10px;
            color: #AFC0FF;
            font-size: 13px;
            font-weight: 800;
            text-decoration: underline;
            text-underline-offset: 3px;
          }}
          @media (max-width: 768px) {{
            .page-shell {{ padding: 16px; }}
            .guide-grid {{ grid-template-columns: 1fr; gap: 14px; }}
            .guide-group {{ padding: 24px 0; }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page-shell content-page">
          {build_site_header(None)}
          <section class="guides-hero">
            <h1>CV guides and resources</h1>
            <p>Practical UK CV advice for checking role fit, improving ATS readability, finding keywords and making your CV more credible before you apply.</p>
          </section>
          {groups_html}
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_support_page(slug: str, page: dict[str, Any]) -> str:
    page_url = canonical_url(slug)
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
        links_html = ""
        if section.get("links"):
            links_html = "<div class=\"section-links\">" + "".join(
                f'<a href="{html.escape(href)}" class="text-link">{html.escape(label)}</a>'
                for href, label in section["links"]
            ) + "</div>"
        link_html = (
            f"<a href=\"{html.escape(section['link_href'])}\" class=\"text-link\">{html.escape(section['link_label'])}</a>"
            if section.get("link_href") and section.get("link_label")
            else ""
        )
        cta_html = (
            f"<div class=\"section-cta cta-block-tight\"><a href=\"{html.escape(section['cta_href'])}\" class=\"cta cta-button\">{html.escape(section['cta_label'])}</a></div>"
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
              {links_html}
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
        {canonical_link_tag(slug)}
        {google_tag()}
        <meta property="og:title" content="{html.escape(page["title"])}">
        <meta property="og:description" content="{html.escape(page["description"])}">
        <meta property="og:url" content="{page_url}">
        <meta property="og:type" content="website">
        <meta name="twitter:card" content="summary_large_image">
        <meta name="twitter:title" content="{html.escape(page["title"])}">
        <meta name="twitter:description" content="{html.escape(page["description"])}">
        {build_footer_assets_head()}
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
            width: 100%;
            max-width: 1120px;
            margin: 0 auto;
            padding: 48px 24px 64px;
            box-sizing: border-box;
            border: 0;
            background: transparent;
            box-shadow: none;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
.text-link {{
            color: #AFC0FF;
            text-decoration: underline;
            text-underline-offset: 2px;
            font-size: 13px;
          }}
          .support-hero {{
            padding: 0 0 28px;
            margin-bottom: 8px;
            border-bottom: 1px solid rgba(92, 112, 150, 0.16);
          }}
          .support-content {{
            display: grid;
            gap: 0;
          }}
          .section-block {{
            padding: 32px 0;
            border-top: 1px solid rgba(92, 112, 150, 0.16);
            background: transparent;
            border-radius: 0;
            box-shadow: none;
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
          .section-links {{
            display: flex;
            flex-wrap: wrap;
            gap: 14px;
            margin-top: 10px;
          }}
          .section-links .text-link {{
            margin-top: 0;
          }}
          .section-cta {{
            margin-top: 0;
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
            margin-top: 0;
            padding-top: 32px;
            border-top: 1px solid rgba(80, 103, 146, 0.18);
          }}
          @media (max-width: 768px) {{
            .page {{
              max-width: 100%;
              padding: 32px 16px 48px;
            }}
            .support-hero {{
              padding-bottom: 22px;
            }}
            .section-block,
            .section-block + .section-block {{
              padding: 26px 0;
            }}
          }}

          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{

          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header("how-it-works" if slug == "how-it-works" else None)}
          <section class="support-hero">
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {build_compliance_notice()}
          </section>
          <div class="support-content">
            {sections_html}
          </div>
          {build_site_footer()}
        </div>
      </body>
    </html>
    """


def render_upgrade_page() -> str:
    page_url = canonical_url("/upgrade")
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Upgrade | CV Optimiser</title>
        <meta name="description" content="Choose between a one-time full CV report or an ongoing Pro plan.">
        {canonical_link_tag("/upgrade")}
        {google_tag()}
        <meta property="og:url" content="{page_url}">
        {build_footer_assets_head()}
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
{build_site_header_css()}
{build_typography_css()}
{build_compliance_notice_css()}
          .hero {{
            display: grid;
            gap: 14px;
            margin-bottom: 24px;
          }}
          .hero p {{
            margin: 0;
            max-width: 60ch;
          }}
          .upgrade-grid {{
            display: grid;
            grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
            gap: 24px;
          }}
          .upgrade-card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
          }}
          .upgrade-card-primary {{
            border-color: rgba(91, 120, 255, 0.34);
            box-shadow: 0 14px 30px rgba(91, 120, 255, 0.14);
          }}
          .price {{
            font-size: 34px;
            line-height: 1;
            color: #FFFFFF;
            font-weight: 820;
            margin: 8px 0 18px;
          }}
          .upgrade-card ul {{
            margin: 0;
            padding-left: 20px;
          }}
          .upgrade-card li {{
            margin-bottom: 8px;
          }}
          .checkout-btn {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            margin-top: 18px;
            padding: 14px 18px;
            border: 0;
            border-radius: 14px;
            background: linear-gradient(135deg, #5B78FF, #3E5EFF);
            color: #FFFFFF;
            font-size: 15px;
            font-weight: 800;
            cursor: pointer;
          }}
          .checkout-btn.secondary {{
            background: rgba(10, 19, 35, 0.34);
            border: 1px solid rgba(92, 112, 150, 0.24);
            color: #EAF0FF;
          }}
          .upgrade-helper {{
            margin-top: 12px;
            color: #9FB0D4;
            font-size: 13px;
          }}
          .hidden {{
            display: none !important;
          }}
          .upgrade-inline-error {{
            margin-top: 12px;
            padding: 12px 14px;
            border-radius: 14px;
            border: 1px solid rgba(192, 102, 112, 0.34);
            background: rgba(58, 18, 29, 0.9);
            color: #FFD8DD;
            font-size: 14px;
          }}
          .upgrade-active-state {{
            padding: 28px;
            border-radius: 20px;
            border: 1px solid rgba(91, 120, 255, 0.26);
            background: linear-gradient(180deg, rgba(17, 31, 58, 0.94), rgba(11, 23, 43, 0.96));
            box-shadow: 0 14px 30px rgba(91, 120, 255, 0.1);
          }}
          .upgrade-active-actions {{
            display: flex;
            flex-wrap: wrap;
            gap: 12px;
            margin-top: 20px;
          }}
          .upgrade-secondary-link {{
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 14px 18px;
            border-radius: 14px;
            border: 1px solid rgba(92, 112, 150, 0.24);
            background: rgba(10, 19, 35, 0.34);
            color: #EAF0FF;
            font-size: 15px;
            font-weight: 700;
            text-decoration: none;
          }}
          .upgrade-loading-state {{
            padding: 24px;
            border-radius: 18px;
            border: 1px solid rgba(92, 112, 150, 0.24);
            background: rgba(15, 28, 50, 0.72);
            color: #DCE6FF;
            font-weight: 700;
          }}
          @media (max-width: 900px) {{
            .upgrade-grid {{
              grid-template-columns: 1fr;
            }}
            .upgrade-active-actions {{
              flex-direction: column;
            }}
          }}
{build_mobile_layout_css()}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header("upgrade")}
          <div class="hero">
            <h1>Pricing</h1>
            <p>Choose a free check, paid report, or Pro access based on how much detail you need.</p>
            {build_compliance_notice()}
          </div>

          <div id="upgradeLoadingState" class="upgrade-loading-state"></div>

          <div id="upgradeGrid" class="upgrade-grid hidden">
            <div id="oneTimeCard" class="upgrade-card upgrade-card-primary">
              <h2>Paid report</h2>
              <div class="price">£7.99 one-time</div>
              <ul>
                <li>Fuller report details for one CV result</li>
                <li>More detailed improvement suggestions</li>
                <li>Keyword gaps and role-match guidance</li>
                <li>Step-by-step improvement plan</li>
              </ul>
              <button class="checkout-btn unlock-report" data-checkout-plan="one_time" type="button">Buy paid report</button>
              <p class="upgrade-helper">Sign in first so the paid report can be attached to your account.</p>
            </div>

            <div id="proCard" class="upgrade-card">
              <h2>Pro access</h2>
              <div class="price">£9.99/month</div>
              <ul>
                <li>Unlimited CV checks</li>
                <li>Full reports</li>
                <li>Saved results</li>
                <li>Ongoing improvements</li>
              </ul>
              <button class="checkout-btn secondary pro-monthly" data-checkout-plan="pro_monthly" type="button">Go Pro — £9.99/month</button>
              <p id="proSignedOutPrompt" class="upgrade-helper hidden">Sign in to start monthly Pro access.</p>
              <p id="proSignedInPrompt" class="upgrade-helper hidden">Monthly Pro access is available for signed-in free accounts.</p>
              <div id="upgradeInlineError" class="upgrade-inline-error hidden">Please sign in to start Pro monthly.</div>
            </div>
          </div>

          <div id="alreadyProState" class="upgrade-active-state hidden">
            <h2>You're already on Pro</h2>
            <p>Your Pro access is active. You can run ongoing CV checks and access full reports.</p>
            <div class="upgrade-active-actions">
              <a href="/#tool" class="checkout-btn">Go to CV checker</a>
              <a href="/" class="upgrade-secondary-link">Manage account</a>
            </div>
          </div>

          {build_site_footer()}
        </div>
        <script>
          const upgradeInlineError = document.getElementById("upgradeInlineError");
          const upgradeGrid = document.getElementById("upgradeGrid");
          const upgradeLoadingState = document.getElementById("upgradeLoadingState");
          const oneTimeCard = document.getElementById("oneTimeCard");
          const proCard = document.getElementById("proCard");
          const alreadyProState = document.getElementById("alreadyProState");
          const oneTimeButton = document.querySelector('[data-checkout-plan="one_time"]');
          const proSignedOutPrompt = document.getElementById("proSignedOutPrompt");
          const proSignedInPrompt = document.getElementById("proSignedInPrompt");

          function hasStoredCvResult() {{
            try {{
              return window.localStorage.getItem("has_cv_result") === "true";
            }} catch (error) {{
              return false;
            }}
          }}

          function redirectToCvCheckerForUpgrade() {{
            window.location.href = "/cv-checker?upgrade_required=1";
          }}

          function updateOneTimeButtonState() {{
            if (!oneTimeButton) return;
            if (hasStoredCvResult()) {{
              oneTimeButton.textContent = "Unlock my full CV report";
              return;
            }}
            oneTimeButton.textContent = "Run CV check first";
          }}

          function showUpgradeInlineError(message) {{
            if (!upgradeInlineError) return;
            upgradeInlineError.textContent = message || "Please sign in to start Pro monthly.";
            upgradeInlineError.classList.remove("hidden");
          }}

          function hideUpgradeInlineError() {{
            if (!upgradeInlineError) return;
            upgradeInlineError.classList.add("hidden");
            upgradeInlineError.textContent = "Please sign in to start Pro monthly.";
          }}

          function applyUpgradePageState(account) {{
            const state = account || {{ signedIn: null, plan: null, planKnown: false }};
            const planKnown = state.planKnown !== false && !!state.plan;
            const isLoading = state.signedIn === null || (state.signedIn && !planKnown);
            const isPro = planKnown && state.plan === "pro";
            console.log("Upgrade account state:", {{
              signedIn: !!state.signedIn,
              plan: isLoading ? "loading" : (isPro ? "signed_in_pro" : (state.signedIn ? "signed_in_free" : "signed_out"))
            }});

            if (isLoading) {{
              if (upgradeLoadingState) upgradeLoadingState.classList.remove("hidden");
              if (upgradeGrid) upgradeGrid.classList.add("hidden");
              if (alreadyProState) alreadyProState.classList.add("hidden");
              if (oneTimeCard) oneTimeCard.classList.add("hidden");
              if (proCard) proCard.classList.add("hidden");
              hideUpgradeInlineError();
              return;
            }}

            if (upgradeLoadingState) upgradeLoadingState.classList.add("hidden");

            if (isPro) {{
              if (upgradeGrid) upgradeGrid.classList.add("hidden");
              if (alreadyProState) alreadyProState.classList.remove("hidden");
              if (oneTimeCard) oneTimeCard.classList.add("hidden");
              if (proCard) proCard.classList.add("hidden");
              hideUpgradeInlineError();
              return;
            }}

            if (upgradeGrid) upgradeGrid.classList.remove("hidden");
            if (alreadyProState) alreadyProState.classList.add("hidden");
            if (oneTimeCard) oneTimeCard.classList.remove("hidden");
            if (proCard) proCard.classList.remove("hidden");
            if (proSignedOutPrompt) proSignedOutPrompt.classList.toggle("hidden", !!state.signedIn);
            if (proSignedInPrompt) proSignedInPrompt.classList.toggle("hidden", !state.signedIn);
          }}

          async function refreshUpgradePageState() {{
            if (typeof window.getAccountState !== "function") {{
              applyUpgradePageState({{ signedIn: null, email: null, plan: null, token: null, planKnown: false }});
              updateOneTimeButtonState();
              return {{ signedIn: null, email: null, plan: null, token: null, planKnown: false }};
            }}
            const account = await window.getAccountState({{ forceRefresh: true }});
            applyUpgradePageState(account);
            updateOneTimeButtonState();
            return account;
          }}

          async function startCheckout(plan, button) {{
            const originalText = button.textContent;
            let shouldResetButton = true;
            console.log("Checkout clicked:", plan);
            hideUpgradeInlineError();

            try {{
              button.disabled = true;
              button.textContent = "Opening checkout…";

              const requiresSignIn = plan === "pro_monthly" || plan === "one_time";
              const account = await refreshUpgradePageState();
              const token = account.token;
              if (account.plan === "pro") {{
                showUpgradeInlineError(plan === "one_time" ? "You already have Pro access." : "You are already on Pro.");
                return;
              }}
              if (plan === "one_time" && !hasStoredCvResult()) {{
                showUpgradeInlineError("Run your free CV check first to unlock your personalised report.");
                redirectToCvCheckerForUpgrade();
                return;
              }}
              if (requiresSignIn && !account.signedIn) {{
                showUpgradeInlineError(plan === "one_time" ? "Please sign in to unlock a paid report." : "Please sign in to start Pro monthly.");
                return;
              }}

              const response = await fetch("/api/create-checkout-session", {{
                method: "POST",
                headers: {{
                  "Content-Type": "application/json",
                  "Authorization": "Bearer " + (token || "")
                }},
                body: JSON.stringify({{ type: plan }})
              }});

              const data = await response.json();

              if (!response.ok || !data.url) {{
                if (requiresSignIn && response.status === 401) {{
                  showUpgradeInlineError(data.detail || "Please sign in to start Pro monthly.");
                  return;
                }}
                throw new Error(data.detail || data.error || "Checkout could not be opened");
              }}

              window.location.href = data.url;
              shouldResetButton = false;
            }} catch (error) {{
              console.error("Checkout error:", error);
              showUpgradeInlineError("Could not open checkout. Please try again.");
              return;
            }} finally {{
              if (shouldResetButton) {{
                button.disabled = false;
                button.textContent = originalText;
              }}
            }}
          }}

          document.addEventListener("click", function(event) {{
            const button = event.target.closest("[data-checkout-plan]");
            if (!button) return;
            event.preventDefault();
            const plan = button.getAttribute("data-checkout-plan");
            startCheckout(plan, button);
          }});

          document.addEventListener("cv-account-state-changed", function(event) {{
            applyUpgradePageState((event.detail && event.detail.account) || null);
            updateOneTimeButtonState();
          }});

          window.addEventListener("load", function() {{
            refreshUpgradePageState();
          }});
        </script>
      </body>
    </html>
    """


def render_status_page(path: str, title: str, heading: str, copy: str) -> str:
    page_url = canonical_url(path)
    success_script = """
        <script>
          (function () {
            if (!/payment successful/i.test(document.title)) return;
            try {
              fetch("/api/track", {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({event_name: "payment_success_seen", metadata: {source: "success_page"}})
              }).catch(function () {});
            } catch (error) {}
          })();
        </script>
    """ if "successful" in title.lower() else ""
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(title)}</title>
        {canonical_link_tag(path)}
        {google_tag()}
        <meta property="og:url" content="{page_url}">
        {build_footer_assets_head()}
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
            max-width: 860px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
          .card {{
            background: rgba(15, 28, 50, 0.72);
            border: 1px solid rgba(92, 112, 150, 0.22);
            border-radius: 18px;
            padding: 24px;
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header()}
          <div class="card">
            <h1>{html.escape(heading)}</h1>
            <p>{html.escape(copy)}</p>
            <div class="cta-block">
              <a href="/#tool" class="cta cta-button">Check my CV</a>
            </div>
          </div>
          {build_site_footer()}
        </div>
        {success_script}
      </body>
    </html>
    """


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return render_static_index()


@app.get("/cv-checker", response_class=HTMLResponse)
@app.get("/cv-checker/", response_class=HTMLResponse, include_in_schema=False)
def cv_checker_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("cv-checker", TOOL_LANDING_PAGES["cv-checker"])


@app.get("/best-free-cv-checker-uk", response_class=HTMLResponse)
@app.get("/best-free-cv-checker-uk/", response_class=HTMLResponse, include_in_schema=False)
def best_free_cv_checker_uk_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_best_free_cv_checker_page()


@app.get("/cv-score-checker", response_class=HTMLResponse)
@app.get("/cv-score-checker/", response_class=HTMLResponse, include_in_schema=False)
def cv_score_checker_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_seo_landing_page("cv-score-checker", SEO_LANDING_PAGES["cv-score-checker"])


@app.get("/job-description-cv-match", response_class=HTMLResponse)
@app.get("/job-description-cv-match/", response_class=HTMLResponse, include_in_schema=False)
def job_description_cv_match_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_seo_landing_page("job-description-cv-match", SEO_LANDING_PAGES["job-description-cv-match"])


@app.get("/ats-cv-checker", response_class=HTMLResponse)
@app.get("/ats-cv-checker/", response_class=HTMLResponse, include_in_schema=False)
def ats_cv_checker_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_seo_landing_page("ats-cv-checker", SEO_LANDING_PAGES["ats-cv-checker"])


@app.get("/cv-keyword-optimiser", response_class=HTMLResponse)
@app.get("/cv-keyword-optimiser/", response_class=HTMLResponse, include_in_schema=False)
def cv_keyword_optimiser_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_seo_landing_page("cv-keyword-optimiser", SEO_LANDING_PAGES["cv-keyword-optimiser"])


@app.get("/cv-improvement-tool", response_class=HTMLResponse)
@app.get("/cv-improvement-tool/", response_class=HTMLResponse, include_in_schema=False)
def cv_improvement_tool_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_seo_landing_page("cv-improvement-tool", SEO_LANDING_PAGES["cv-improvement-tool"])


@app.get("/example-cv-report", response_class=HTMLResponse)
@app.get("/example-cv-report/", response_class=HTMLResponse, include_in_schema=False)
def example_cv_report_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_example_report_page("example-cv-report")


def make_role_example_report_handler(slug: str):
    def handler(request: Request) -> str:
        log_seo_page_hit(request.url.path)
        return render_example_report_page(slug)

    handler.__name__ = f"role_example_report_{slug.replace('-', '_')}"
    return handler


for role_example_slug in ROLE_EXAMPLE_REPORTS:
    app.add_api_route(
        f"/{role_example_slug}",
        make_role_example_report_handler(role_example_slug),
        methods=["GET"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        f"/{role_example_slug}/",
        make_role_example_report_handler(role_example_slug),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )


def make_comparison_page_handler(slug: str):
    def handler(request: Request) -> str:
        log_seo_page_hit(request.url.path)
        return render_comparison_page(slug, COMPARISON_PAGES[slug])

    handler.__name__ = f"comparison_page_{slug.replace('-', '_')}"
    return handler


for comparison_slug in COMPARISON_PAGES:
    app.add_api_route(
        f"/{comparison_slug}",
        make_comparison_page_handler(comparison_slug),
        methods=["GET"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        f"/{comparison_slug}/",
        make_comparison_page_handler(comparison_slug),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )


@app.get("/example-report", include_in_schema=False)
@app.get("/example-report/", include_in_schema=False)
def example_report_redirect() -> RedirectResponse:
    return RedirectResponse(url="/example-cv-report", status_code=301)


@app.get("/google4cffcb1da00a66a5.html")
def google_verification() -> PlainTextResponse:
    return PlainTextResponse("google-site-verification: google4cffcb1da00a66a5.html")


@app.get("/sitemap.xml")
def sitemap() -> Response:
    parts = []
    for entry in SITEMAP_URLS:
        if entry.get("group"):
            parts.append(f"  <!-- {html.escape(entry['group'])} -->")
        parts.append(
            f"""  <url>
    <loc>{html.escape(entry["loc"])}</loc>
    <priority>{html.escape(entry["priority"])}</priority>
  </url>"""
        )
    url_entries = "\n\n".join(parts)
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{url_entries}
</urlset>
"""
    return Response(content=xml_content, media_type="application/xml")


@app.get("/robots.txt")
def robots_txt() -> PlainTextResponse:
    return PlainTextResponse(
        "\n".join(
            [
                "User-agent: *",
                "Allow: /",
                "Disallow: /api/",
                "Disallow: /admin-analytics",
                "Sitemap: https://www.cv-optimiser.com/sitemap.xml",
            ]
        )
        + "\n"
    )


def make_seo_landing_handler(slug: str):
    def handler() -> HTMLResponse:
        return HTMLResponse(render_seo_landing_page(slug, SEO_LANDING_PAGES[slug]))

    handler.__name__ = f"seo_landing_{slug.replace('-', '_')}"
    return handler


for seo_slug in SEO_LANDING_PAGES:
    app.add_api_route(
        f"/{seo_slug}",
        make_seo_landing_handler(seo_slug),
        methods=["GET"],
        response_class=HTMLResponse,
    )
    app.add_api_route(
        f"/{seo_slug}/",
        make_seo_landing_handler(seo_slug),
        methods=["GET"],
        response_class=HTMLResponse,
        include_in_schema=False,
    )


@app.get("/10-second-cv-test", response_class=HTMLResponse)
@app.get("/10-second-cv-test/", response_class=HTMLResponse, include_in_schema=False)
def ten_second_cv_test_page() -> str:
    return render_ten_second_cv_test_page()


@app.get("/faq", response_class=HTMLResponse)
def faq_page() -> str:
    return render_faq_page()


@app.get("/guides", response_class=HTMLResponse)
@app.get("/guides/", response_class=HTMLResponse, include_in_schema=False)
def guides_page() -> str:
    return render_guides_page()


@app.get("/how-it-works", response_class=HTMLResponse)
@app.get("/how-it-works/", response_class=HTMLResponse, include_in_schema=False)
def how_it_works_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_support_page("how-it-works", SUPPORT_PAGES["how-it-works"])


@app.get("/how-cv-optimiser-scores-your-cv", response_class=HTMLResponse)
@app.get("/how-cv-optimiser-scores-your-cv/", response_class=HTMLResponse, include_in_schema=False)
def cv_score_methodology_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_support_page("how-cv-optimiser-scores-your-cv", SUPPORT_PAGES["how-cv-optimiser-scores-your-cv"])


@app.get("/cv-statistics", response_class=HTMLResponse)
@app.get("/cv-statistics/", response_class=HTMLResponse, include_in_schema=False)
def cv_statistics_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_support_page("cv-statistics", SUPPORT_PAGES["cv-statistics"])


@app.get("/why-your-cv-is-not-getting-interviews", include_in_schema=False)
@app.get("/why-your-cv-is-not-getting-interviews/", include_in_schema=False)
def why_cv_not_getting_interviews_redirect() -> RedirectResponse:
    return RedirectResponse(url="/why-is-my-cv-not-getting-interviews", status_code=301)


@app.get("/how-to-tailor-cv-to-job-description", response_class=HTMLResponse)
@app.get("/how-to-tailor-cv-to-job-description/", response_class=HTMLResponse, include_in_schema=False)
def tailor_cv_to_job_description_alias_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("how-to-tailor-cv-to-job-description", BLOG_ARTICLES["how-to-tailor-cv-to-job-description"])


@app.get("/how-to-tailor-your-cv", response_class=HTMLResponse)
@app.get("/how-to-tailor-your-cv/", response_class=HTMLResponse, include_in_schema=False)
def tailor_cv_to_job_description_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("how-to-tailor-your-cv", BLOG_ARTICLES["how-to-tailor-cv-to-job-description"])


@app.get("/ats-cv-keywords", response_class=HTMLResponse)
@app.get("/ats-cv-keywords/", response_class=HTMLResponse, include_in_schema=False)
def ats_cv_keywords_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("ats-cv-keywords", BLOG_ARTICLES["ats-cv-keywords"])


@app.get("/cv-mistakes", response_class=HTMLResponse)
@app.get("/cv-mistakes/", response_class=HTMLResponse, include_in_schema=False)
def cv_mistakes_that_cost_interviews_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("cv-mistakes", BLOG_ARTICLES["cv-mistakes-that-cost-interviews"])


@app.get("/how-to-improve-cv-score", response_class=HTMLResponse)
@app.get("/how-to-improve-cv-score/", response_class=HTMLResponse, include_in_schema=False)
def how_to_improve_cv_score_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("how-to-improve-cv-score", BLOG_ARTICLES["how-to-improve-cv-score"])


@app.get("/features", response_class=HTMLResponse)
def features_page() -> str:
    return render_support_page("features", SUPPORT_PAGES["features"])


@app.get("/pricing", response_class=HTMLResponse)
def pricing_page() -> str:
    return render_support_page("pricing", SUPPORT_PAGES["pricing"])


@app.get("/contact", response_class=HTMLResponse)
def contact_page() -> str:
    return render_support_page("contact", SUPPORT_PAGES["contact"])


@app.get("/about", response_class=HTMLResponse)
def about_page() -> str:
    return render_support_page("about", SUPPORT_PAGES["about"])


@app.get("/upgrade", response_class=HTMLResponse)
@app.get("/upgrade/", response_class=HTMLResponse, include_in_schema=False)
def upgrade_page() -> str:
    return render_upgrade_page()


@app.get("/success", response_class=HTMLResponse)
def success() -> str:
    return render_status_page(
        "/success",
        "Payment successful | CV Optimiser",
        "Payment successful",
        "Your full CV improvement plan is ready.",
    )


@app.get("/cancel", response_class=HTMLResponse)
def cancel() -> str:
    return render_status_page(
        "/cancel",
        "Payment cancelled | CV Optimiser",
        "Payment cancelled",
        "You can return to your CV check anytime.",
    )


@app.get("/privacy", response_class=HTMLResponse)
def privacy_page() -> str:
    return render_support_page("privacy", SUPPORT_PAGES["privacy"])


@app.get("/terms", response_class=HTMLResponse)
def terms_page() -> str:
    return render_support_page("terms", SUPPORT_PAGES["terms"])


@app.get("/billing", response_class=HTMLResponse)
def billing_page() -> str:
    return f"""
    <html>
      <head>
        <title>Billing & Cancellation | CV Optimiser</title>
        <meta name="description" content="Billing and cancellation information for CV Optimiser subscriptions.">
        {canonical_link_tag("/billing")}
        {google_tag()}
        {build_footer_assets_head()}
        <style>
          body {{ font-family: Inter, Arial, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; line-height: 1.7; }}
          h1,h2 {{ color: #FFFFFF; }}
          a {{ color: #9AB0FF; }}
          p, li {{ color: #C7D3EE; }}
        </style>
      </head>
      <body data-auth-state="loading">
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
        "supabase_public_configured": "yes" if (SUPABASE_URL and SUPABASE_ANON_KEY) else "no",
        "supabase_admin_configured": "yes" if supabase_admin else "no",
        "stripe_configured": "yes" if STRIPE_SECRET_KEY else "no",
        "stripe_one_time_price_configured": "yes" if STRIPE_PRICE_ONE_TIME else "no",
        "stripe_monthly_price_configured": "yes" if STRIPE_PRICE_PRO_MONTHLY else "no",
        "stripe_webhook_configured": "yes" if STRIPE_WEBHOOK_SECRET else "no",
    }


@app.post("/api/track")
async def api_track(request: Request) -> dict[str, Any]:
    try:
        body = await request.json()
        event_name = (body.get("event_name") or "").strip()
        metadata = body.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}

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
        return {"error": "tracking_unavailable"}


@app.post("/admin-analytics/login", response_class=HTMLResponse)
async def admin_analytics_login(request: Request, password: str = Form("")) -> Response:
    if not ADMIN_PASSWORD:
        return HTMLResponse(
            render_admin_login("ADMIN_PASSWORD is not configured yet."),
            status_code=503,
        )
    if not secrets.compare_digest(password, ADMIN_PASSWORD):
        return HTMLResponse(render_admin_login("That password was not recognised."), status_code=401)
    request.session["admin_authenticated"] = True
    return RedirectResponse(url="/admin-analytics", status_code=303)


@app.post("/admin-analytics/logout")
async def admin_analytics_logout(request: Request) -> Response:
    request.session.pop("admin_authenticated", None)
    return RedirectResponse(url="/admin-analytics", status_code=303)


@app.get("/api/admin/analytics")
def admin_analytics(request: Request, limit: int = 250, days: int = 30) -> dict[str, Any]:
    require_admin(request)
    try:
        limit = max(20, min(limit, 1000))
        days = max(1, min(days, 365))
        since = current_utc() - timedelta(days=days)
        result = (
            require_supabase()
            .table("analytics_events")
            .select("created_at,event_name,email,metadata")
            .gte("created_at", since.isoformat())
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        items = result.data or []
        return {
            "window_days": days,
            "limit": limit,
            "summary": build_analytics_summary(items),
            "items": sanitize_analytics_items(items),
        }
    except Exception as e:
        print("ADMIN ANALYTICS ERROR:", repr(e))
        return {"error": "analytics_unavailable"}


@app.get("/admin-analytics", response_class=HTMLResponse)
def admin_analytics_page(request: Request) -> str:
    if not is_admin_authenticated(request):
        return render_admin_login()
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Analytics | CV Optimiser</title>
        <meta name="description" content="Internal analytics dashboard for CV Optimiser.">
        {canonical_link_tag("/admin-analytics")}
        {google_tag()}
        {build_footer_assets_head()}
        <style>
          body {{
            margin: 0;
            font-family: Inter, Arial, sans-serif;
            background: #07142D;
            color: #E8EEFC;
          }}
          .page {{
            max-width: 1180px;
            margin: 0 auto;
            padding: 34px 20px 70px;
          }}
          .topbar {{
            display: flex;
            align-items: flex-start;
            justify-content: space-between;
            gap: 18px;
            margin-bottom: 24px;
          }}
          h1 {{
            margin: 0 0 8px;
            font-size: 34px;
          }}
          p {{
            margin: 0;
            color: #B7C6E6;
            line-height: 1.6;
          }}
          .controls {{
            display: flex;
            align-items: center;
            gap: 10px;
            flex-wrap: wrap;
          }}
          .logout-form {{
            margin: 0;
          }}
          select,
          button {{
            min-height: 40px;
            border-radius: 12px;
            border: 1px solid rgba(160, 180, 230, 0.24);
            background: rgba(10, 20, 40, 0.82);
            color: #EEF3FF;
            padding: 9px 12px;
            font-weight: 800;
          }}
          button {{
            cursor: pointer;
          }}
          .secondary-button {{
            background: rgba(10, 20, 40, 0.82);
          }}
          .grid {{
            display: grid;
            gap: 14px;
          }}
          .hidden {{
            display: none;
          }}
          .kpi-grid {{
            grid-template-columns: repeat(4, minmax(0, 1fr));
            margin-bottom: 18px;
          }}
          .panel {{
            border: 1px solid rgba(80, 103, 146, 0.28);
            border-radius: 18px;
            background: rgba(10, 20, 40, 0.82);
            padding: 18px;
          }}
          .kpi span {{
            display: block;
            color: #9FB0D4;
            font-size: 12px;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
          }}
          .kpi strong {{
            display: block;
            margin-top: 8px;
            color: #FFFFFF;
            font-size: 31px;
          }}
          .dashboard-grid {{
            grid-template-columns: minmax(0, 1.3fr) minmax(320px, 0.7fr);
            align-items: start;
          }}
          .wide-panel {{
            margin-top: 18px;
          }}
          .chart-wrap {{
            overflow-x: auto;
          }}
          .trend-chart {{
            width: 100%;
            min-width: 720px;
            height: 280px;
            display: block;
          }}
          .chart-axis {{
            stroke: rgba(159, 176, 212, 0.28);
            stroke-width: 1;
          }}
          .chart-label {{
            fill: #9FB0D4;
            font-size: 11px;
          }}
          .chart-line {{
            fill: none;
            stroke-width: 3;
            stroke-linecap: round;
            stroke-linejoin: round;
          }}
          .chart-dot {{
            stroke: #07142D;
            stroke-width: 2;
          }}
          .legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px 16px;
            margin-top: 12px;
            color: #C7D3EE;
            font-size: 13px;
          }}
          .legend span {{
            display: inline-flex;
            align-items: center;
            gap: 7px;
          }}
          .legend i {{
            width: 10px;
            height: 10px;
            border-radius: 999px;
            display: inline-block;
          }}
          .conversion-grid {{
            grid-template-columns: repeat(2, minmax(0, 1fr));
            margin-top: 18px;
          }}
          .table-scroll {{
            overflow-x: auto;
          }}
          .dimension-label {{
            max-width: 260px;
            overflow-wrap: anywhere;
            color: #EEF3FF;
            font-weight: 800;
          }}
          .quality-grid {{
            grid-template-columns: repeat(3, minmax(0, 1fr));
            margin-top: 18px;
          }}
          .bar-row {{
            display: grid;
            grid-template-columns: 64px 1fr 44px;
            align-items: center;
            gap: 10px;
            margin: 9px 0;
            color: #DCE6FF;
            font-size: 13px;
          }}
          .bar-track {{
            height: 9px;
            border-radius: 999px;
            background: rgba(159, 176, 212, 0.18);
            overflow: hidden;
          }}
          .bar-fill {{
            height: 100%;
            border-radius: inherit;
            background: #38D996;
          }}
          h2 {{
            margin: 0 0 12px;
            font-size: 20px;
          }}
          table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 14px;
          }}
          th,
          td {{
            padding: 11px 8px;
            border-bottom: 1px solid rgba(80, 103, 146, 0.28);
            text-align: left;
            vertical-align: top;
          }}
          th {{
            color: #9FB0D4;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.05em;
          }}
          .rate {{
            color: #B7F7C4;
            font-weight: 800;
          }}
          .drop {{
            color: #FFB8A0;
            font-weight: 800;
          }}
          .count-list {{
            display: grid;
            gap: 9px;
          }}
          .count-row {{
            display: flex;
            justify-content: space-between;
            gap: 10px;
            color: #DCE6FF;
          }}
          .count-row span {{
            color: #9FB0D4;
          }}
          .recent-events {{
            margin-top: 18px;
          }}
          .event-name {{
            color: #EEF3FF;
            font-weight: 800;
          }}
          .metadata {{
            max-width: 430px;
            color: #9FB0D4;
            font-size: 12px;
            overflow-wrap: anywhere;
          }}
          .empty,
          .error {{
            padding: 18px;
            border-radius: 14px;
            background: rgba(58, 18, 29, 0.72);
            border: 1px solid rgba(192, 102, 112, 0.34);
            color: #FFD8DD;
          }}
          @media (max-width: 900px) {{
            .topbar,
            .dashboard-grid {{
              grid-template-columns: 1fr;
              display: grid;
            }}
            .conversion-grid {{
              grid-template-columns: 1fr;
            }}
            .quality-grid {{
              grid-template-columns: 1fr;
            }}
            .kpi-grid {{
              grid-template-columns: repeat(2, minmax(0, 1fr));
            }}
          }}
          @media (max-width: 620px) {{
            .kpi-grid {{
              grid-template-columns: 1fr;
            }}
            table {{
              font-size: 13px;
            }}
          }}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          <div class="topbar">
            <div>
              <h1>Analytics</h1>
              <p>Conversion dashboard for CV checks, paid report unlocks, checkout and saved reports.</p>
            </div>
            <div class="controls">
              <select id="windowSelect" aria-label="Analytics window">
                <option value="7">Last 7 days</option>
                <option value="30" selected>Last 30 days</option>
                <option value="90">Last 90 days</option>
              </select>
              <button id="refreshBtn" type="button">Refresh</button>
              <form class="logout-form" method="post" action="/admin-analytics/logout">
                <button class="secondary-button" type="submit">Logout</button>
              </form>
            </div>
          </div>

          <div id="status" class="panel">Loading analytics...</div>
          <div id="dashboard" class="hidden">
            <div id="kpiGrid" class="grid kpi-grid"></div>
            <div class="grid dashboard-grid">
              <div class="panel">
                <h2>Revenue Funnel</h2>
                <div id="funnelTable"></div>
              </div>
              <div class="grid">
                <div class="panel">
                  <h2>Event Counts</h2>
                  <div id="eventCounts" class="count-list"></div>
                </div>
                <div class="panel">
                  <h2>Checkout Split</h2>
                  <div id="checkoutCounts" class="count-list"></div>
                </div>
                <div class="panel">
                  <h2>Traffic Sources</h2>
                  <div id="sourceCounts" class="count-list"></div>
                </div>
              </div>
            </div>
            <div class="panel recent-events">
              <h2>Recent Events</h2>
              <div id="recentEvents"></div>
            </div>
            <div class="panel wide-panel">
              <h2>Daily Trends</h2>
              <div id="trendChart"></div>
            </div>
            <div class="panel wide-panel">
              <h2>Source Conversion</h2>
              <div id="sourceConversion"></div>
            </div>
            <div class="grid conversion-grid">
              <div class="panel">
                <h2>Landing Page Conversion</h2>
                <div id="landingConversion"></div>
              </div>
              <div class="panel">
                <h2>Campaign Conversion</h2>
                <div id="campaignConversion"></div>
              </div>
            </div>
            <div class="grid quality-grid">
              <div class="panel">
                <h2>Score Bands</h2>
                <div id="scoreBands"></div>
              </div>
              <div class="panel">
                <h2>Common Gaps</h2>
                <div id="commonGaps" class="count-list"></div>
              </div>
              <div class="panel">
                <h2>Quality Signals</h2>
                <div id="qualitySignals" class="count-list"></div>
              </div>
            </div>
          </div>
        </div>
        <script>
          const statusEl = document.getElementById("status");
          const dashboardEl = document.getElementById("dashboard");
          const kpiGrid = document.getElementById("kpiGrid");
          const funnelTable = document.getElementById("funnelTable");
          const eventCounts = document.getElementById("eventCounts");
          const checkoutCounts = document.getElementById("checkoutCounts");
          const sourceCounts = document.getElementById("sourceCounts");
          const recentEvents = document.getElementById("recentEvents");
          const trendChart = document.getElementById("trendChart");
          const sourceConversion = document.getElementById("sourceConversion");
          const landingConversion = document.getElementById("landingConversion");
          const campaignConversion = document.getElementById("campaignConversion");
          const scoreBands = document.getElementById("scoreBands");
          const commonGaps = document.getElementById("commonGaps");
          const qualitySignals = document.getElementById("qualitySignals");
          const windowSelect = document.getElementById("windowSelect");
          const refreshBtn = document.getElementById("refreshBtn");
          const chartSeries = [
            ["page_view", "Page views", "#8FB3FF"],
            ["content_page_view", "Content views", "#B7F7C4"],
            ["cv_check_started", "CV checks", "#38D996"],
            ["unlock_intent", "Unlock intent", "#FFD166"],
            ["checkout_started", "Checkouts", "#FF8A65"],
            ["one_time_report_generated", "Paid reports", "#F472B6"]
          ];

          function escapeHtml(value) {{
            return String(value ?? "")
              .replace(/&/g, "&amp;")
              .replace(/</g, "&lt;")
              .replace(/>/g, "&gt;")
              .replace(/"/g, "&quot;")
              .replace(/'/g, "&#039;");
          }}

          function formatNumber(value) {{
            if (value === null || typeof value === "undefined") return "0";
            return Number(value).toLocaleString("en-GB");
          }}

          function renderCountList(target, counts) {{
            const entries = Object.entries(counts || {{}})
              .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
            if (!entries.length) {{
              target.innerHTML = "<p>No events yet.</p>";
              return;
            }}
            target.innerHTML = entries.slice(0, 18).map(([name, count]) => (
              "<div class='count-row'><span>" + escapeHtml(name) + "</span><strong>" + formatNumber(count) + "</strong></div>"
            )).join("");
          }}

          function renderCountItems(target, rows) {{
            const data = rows || [];
            if (!data.length) {{
              target.innerHTML = "<p>No quality data yet.</p>";
              return;
            }}
            target.innerHTML = data.slice(0, 10).map((row) => (
              "<div class='count-row'><span>" + escapeHtml(row.label) + "</span><strong>" + formatNumber(row.count) + "</strong></div>"
            )).join("");
          }}

          function renderScoreBands(target, bands) {{
            const entries = Object.entries(bands || {{}});
            const total = entries.reduce((sum, [, count]) => sum + Number(count || 0), 0);
            if (!total) {{
              target.innerHTML = "<p>No scored results yet.</p>";
              return;
            }}
            target.innerHTML = entries.map(([label, count]) => {{
              const pct = Math.round((Number(count || 0) / total) * 100);
              return "<div class='bar-row'><span>" + escapeHtml(label) + "</span><div class='bar-track'><div class='bar-fill' style='width:" + pct + "%'></div></div><strong>" + formatNumber(count) + "</strong></div>";
            }}).join("");
          }}

          function renderDimensionTable(target, rows) {{
            const data = (rows || []).slice(0, 15);
            if (!data.length) {{
              target.innerHTML = "<p>No attribution data yet.</p>";
              return;
            }}
            target.innerHTML = "<div class='table-scroll'><table><thead><tr><th>Name</th><th>Views</th><th>Clicks</th><th>CTR</th><th>Checks</th><th>Free results</th><th>Unlocks</th><th>Checkouts</th><th>Paid</th><th>Unlock rate</th><th>Checkout rate</th></tr></thead><tbody>" +
              data.map((row) => (
                "<tr><td class='dimension-label'>" + escapeHtml(row.label) + "</td><td>" + formatNumber(row.page_views) +
                "</td><td>" + formatNumber(row.checker_clicks) + "</td><td class='rate'>" + escapeHtml(row.checker_click_rate) +
                "%</td><td>" + formatNumber(row.cv_checks) + "</td><td>" + formatNumber(row.free_results) +
                "</td><td>" + formatNumber(row.unlock_clicks) + "</td><td>" + formatNumber(row.checkout_starts) +
                "</td><td>" + formatNumber(row.paid_reports) + "</td><td class='rate'>" + escapeHtml(row.unlock_rate) +
                "%</td><td class='rate'>" + escapeHtml(row.checkout_rate) + "%</td></tr>"
              )).join("") + "</tbody></table></div>";
          }}

          function renderTrendChart(target, rows) {{
            const data = rows || [];
            if (!data.length) {{
              target.innerHTML = "<p>No trend data yet.</p>";
              return;
            }}
            const width = 860;
            const height = 280;
            const pad = {{ left: 46, right: 18, top: 20, bottom: 42 }};
            const plotWidth = width - pad.left - pad.right;
            const plotHeight = height - pad.top - pad.bottom;
            const maxValue = Math.max(1, ...data.flatMap((row) => chartSeries.map(([key]) => Number(row[key] || 0))));
            const xFor = (index) => pad.left + (data.length === 1 ? plotWidth / 2 : (index / (data.length - 1)) * plotWidth);
            const yFor = (value) => pad.top + plotHeight - (Number(value || 0) / maxValue) * plotHeight;
            const grid = [0, 0.25, 0.5, 0.75, 1].map((ratio) => {{
              const y = pad.top + plotHeight - ratio * plotHeight;
              const value = Math.round(maxValue * ratio);
              return "<line class='chart-axis' x1='" + pad.left + "' y1='" + y + "' x2='" + (width - pad.right) + "' y2='" + y + "'></line>" +
                "<text class='chart-label' x='8' y='" + (y + 4) + "'>" + value + "</text>";
            }}).join("");
            const labels = data.map((row, index) => {{
              if (data.length > 10 && index % Math.ceil(data.length / 8) !== 0 && index !== data.length - 1) return "";
              return "<text class='chart-label' text-anchor='middle' x='" + xFor(index) + "' y='" + (height - 14) + "'>" + escapeHtml(String(row.date).slice(5)) + "</text>";
            }}).join("");
            const lines = chartSeries.map(([key, label, color]) => {{
              const points = data.map((row, index) => xFor(index) + "," + yFor(row[key])).join(" ");
              const dots = data.map((row, index) => "<circle class='chart-dot' cx='" + xFor(index) + "' cy='" + yFor(row[key]) + "' r='3' fill='" + color + "'><title>" + escapeHtml(label + " " + row.date + ": " + formatNumber(row[key] || 0)) + "</title></circle>").join("");
              return "<polyline class='chart-line' stroke='" + color + "' points='" + points + "'></polyline>" + dots;
            }}).join("");
            const legend = "<div class='legend'>" + chartSeries.map(([, label, color]) => "<span><i style='background:" + color + "'></i>" + escapeHtml(label) + "</span>").join("") + "</div>";
            target.innerHTML = "<div class='chart-wrap'><svg class='trend-chart' viewBox='0 0 " + width + " " + height + "' role='img' aria-label='Daily analytics trends'>" +
              grid + labels + lines + "</svg></div>" + legend;
          }}

          function renderDashboard(data) {{
            const summary = data.summary || {{}};
            const metrics = summary.key_metrics || {{}};
            const unlockRate = metrics.free_results ? ((metrics.unlock_clicks || 0) / metrics.free_results * 100).toFixed(1) + "%" : "0%";
            const checkoutRate = metrics.unlock_clicks ? ((metrics.checkout_starts || 0) / metrics.unlock_clicks * 100).toFixed(1) + "%" : "0%";
            const paidReportRate = metrics.checkout_starts ? ((metrics.one_time_reports_generated || 0) / metrics.checkout_starts * 100).toFixed(1) + "%" : "0%";
            const kpis = [
              ["Free results", metrics.free_results || 0],
              ["Unlock rate", unlockRate],
              ["Checkout starts", metrics.checkout_starts || 0],
              ["Paid reports generated", metrics.one_time_reports_generated || 0],
              ["Payment success", metrics.payment_successes || 0],
              ["Avg score", summary.average_score ?? "n/a"],
              ["Known emails", summary.unique_emails || 0],
              ["Downloads", (metrics.report_downloads || 0) + (metrics.saved_report_downloads || 0)]
            ];
            kpiGrid.innerHTML = kpis.map(([label, value]) => (
              "<div class='panel kpi'><span>" + escapeHtml(label) + "</span><strong>" + escapeHtml(value) + "</strong></div>"
            )).join("");

            const funnelRows = (summary.funnel || []).map((step) => (
              "<tr><td>" + escapeHtml(step.label) + "</td><td>" + formatNumber(step.count) + "</td><td class='rate'>" +
              escapeHtml(step.from_previous_rate) + "%</td><td class='rate'>" + escapeHtml(step.from_start_rate) +
              "%</td><td class='drop'>" + formatNumber(step.drop_from_previous) + "</td></tr>"
            )).join("");
            funnelTable.innerHTML = "<table><thead><tr><th>Step</th><th>Events</th><th>From previous</th><th>From free result</th><th>Drop</th></tr></thead><tbody>" + funnelRows + "</tbody></table>" +
              "<p style='margin-top:12px;'>Unlock rate: <strong>" + escapeHtml(unlockRate) + "</strong>. Checkout start rate from unlock clicks: <strong>" + escapeHtml(checkoutRate) + "</strong>. Paid report generation from checkout starts: <strong>" + escapeHtml(paidReportRate) + "</strong>.</p>";

            renderCountList(eventCounts, summary.counts || {{}});
            renderCountList(checkoutCounts, summary.checkout_counts || {{}});
            renderCountList(sourceCounts, summary.source_counts || {{}});
            renderTrendChart(trendChart, summary.daily_trends || []);
            renderDimensionTable(sourceConversion, (summary.dimension_tables || {{}}).sources || []);
            renderDimensionTable(landingConversion, (summary.dimension_tables || {{}}).landing_pages || []);
            renderDimensionTable(campaignConversion, (summary.dimension_tables || {{}}).campaigns || []);
            const quality = summary.quality_audit || {{}};
            renderScoreBands(scoreBands, quality.score_bands || {{}});
            renderCountItems(commonGaps, quality.common_missing_keywords || []);
            renderCountItems(qualitySignals, [
              ...((quality.common_weak_points || []).slice(0, 5)),
              ...((quality.common_priority_fixes || []).slice(0, 5))
            ]);

            recentEvents.innerHTML = "<table><thead><tr><th>Time</th><th>Event</th><th>Email</th><th>Metadata</th></tr></thead><tbody>" +
              (data.items || []).slice(0, 40).map((item) => (
                "<tr><td>" + escapeHtml(new Date(item.created_at).toLocaleString("en-GB")) + "</td><td class='event-name'>" +
                escapeHtml(item.event_name) + "</td><td>" + escapeHtml(item.email || "") + "</td><td class='metadata'>" +
                escapeHtml(JSON.stringify(item.metadata || {{}})) + "</td></tr>"
              )).join("") + "</tbody></table>";
          }}

          async function loadAnalytics() {{
            statusEl.className = "panel";
            statusEl.textContent = "Loading analytics...";
            dashboardEl.classList.add("hidden");
            try {{
              const days = windowSelect.value || "30";
              const response = await fetch("/api/admin/analytics?days=" + encodeURIComponent(days) + "&limit=1000");
              const data = await response.json();
              if (!response.ok || data.error) {{
                throw new Error(data.error || "analytics_unavailable");
              }}
              renderDashboard(data);
              statusEl.textContent = "Showing last " + data.window_days + " days. Total events: " + formatNumber(data.summary.total_events || 0) + ".";
              dashboardEl.classList.remove("hidden");
            }} catch (error) {{
              console.error(error);
              statusEl.className = "error";
              statusEl.textContent = "Analytics are not available right now.";
            }}
          }}

          refreshBtn.addEventListener("click", loadAnalytics);
          windowSelect.addEventListener("change", loadAnalytics);
          loadAnalytics();
        </script>
      </body>
    </html>
    """


@app.get("/api/me", response_model=None)
def api_me(authorization: Optional[str] = Header(None)) -> dict[str, Any] | JSONResponse:
    try:
        user: Optional[dict[str, Any]] = None
        if authorization and authorization.lower().startswith("bearer "):
            try:
                user = get_user_from_token(authorization)
            except HTTPException:
                user = None

        if not user:
            print("API_ME_AUTH: signed_out")
            print("API_ME_USER: None")
            print("API_ME_PLAN: free")
            return {
                "authenticated": False,
                "signed_in": False,
                "email": None,
                "plan": "free",
                "pro": False,
                "plan_state": None,
                "user": None,
                "user_id": None,
                "account_status_available": True,
            }

        upsert_profile(user["id"], user["email"])
        plan_state = get_plan_state(user["id"])
        plan_name = get_user_plan(user)
        print("API_ME_AUTH: signed_in")
        print(f"API_ME_USER: {user['email']}")
        print(f"API_ME_PLAN: {plan_name}")
        return {
            "authenticated": True,
            "signed_in": True,
            "email": user["email"],
            "plan": plan_name,
            "pro": plan_name == "pro",
            "plan_state": plan_state,
            "user": user,
            "user_id": user["id"],
            "account_status_available": True,
        }
    except Exception:
        logger.exception("Failed to load account status")
        return JSONResponse(
            status_code=200,
            content={
                "authenticated": False,
                "signed_in": False,
                "email": None,
                "plan": "free",
                "pro": False,
                "plan_state": None,
                "user": None,
                "user_id": None,
                "account_status_available": False,
            },
        )


@app.get("/api/history")
def api_history(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)

        result = (
            require_supabase()
            .table("analysis_history")
            .select("id, job_title, score, created_at, result_json")
            .eq("user_id", user["id"])
            .order("created_at", desc=True)
            .limit(20)
            .execute()
        )

        items = []
        for row in result.data or []:
            payload = row.get("result_json") if isinstance(row.get("result_json"), dict) else {}
            items.append({
                "id": row.get("id"),
                "job_title": row.get("job_title"),
                "score": row.get("score"),
                "created_at": row.get("created_at"),
                "report_type": get_report_type(payload),
                "full_report": bool(payload.get("fullReportUnlocked") or payload.get("professionalSummary")),
            })

        return {"items": items}

    except Exception as e:
        print("API_HISTORY_ERROR:", repr(e))
        return {"error": "history_unavailable"}


@app.get("/api/history/{analysis_id}")
def api_history_detail(analysis_id: int, authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)

        result = (
            require_supabase()
            .table("analysis_history")
            .select("id, job_title, score, created_at, result_json")
            .eq("user_id", user["id"])
            .eq("id", analysis_id)
            .limit(1)
            .execute()
        )
        rows = result.data or []
        if not rows:
            raise HTTPException(status_code=404, detail="Saved report not found.")

        row = rows[0]
        payload = row.get("result_json") if isinstance(row.get("result_json"), dict) else {}
        return {
            "item": {
                "id": row.get("id"),
                "job_title": row.get("job_title"),
                "score": row.get("score"),
                "created_at": row.get("created_at"),
                "report_type": get_report_type(payload),
                "full_report": bool(payload.get("fullReportUnlocked") or payload.get("professionalSummary")),
                "result": payload,
            }
        }

    except HTTPException:
        raise
    except Exception as e:
        print("API_HISTORY_DETAIL_ERROR:", repr(e))
        return {"error": "history_detail_unavailable"}


@app.post("/api/create-checkout-session")
def create_checkout_session(
    payload: Optional[dict[str, Any]] = Body(default=None),
    authorization: Optional[str] = Header(None),
) -> dict[str, Any]:
    raw_checkout_type = (payload or {}).get("plan") or (payload or {}).get("type") or "pro_monthly"
    checkout_plan = {
        "one_time": "one_time",
        "one-time": "one_time",
        "payment": "one_time",
        "pro_monthly": "pro_monthly",
        "pro": "pro_monthly",
        "subscription": "pro_monthly",
    }.get(str(raw_checkout_type).strip().lower())
    if checkout_plan not in {"one_time", "pro_monthly"}:
        return {"error": "Invalid checkout plan.", "code": "INVALID_PLAN"}

    user: Optional[dict[str, Any]] = None
    if authorization:
        try:
            user = get_user_from_token(authorization)
        except HTTPException:
            user = None

    print("CHECKOUT_AUTH:", "signed_in" if user else "signed_out")
    active_subscription = get_active_subscription(user["id"]) if user else None
    checkout_user_plan = "anonymous" if not user else get_user_plan(user)
    print("CHECKOUT_PLAN:", checkout_user_plan)

    if not user:
        detail = "Please sign in to unlock a paid report." if checkout_plan == "one_time" else "Please sign in to start Pro monthly."
        raise HTTPException(status_code=401, detail=detail)

    if checkout_plan == "pro_monthly":
        upsert_profile(user["id"], user["email"])
        if active_subscription:
            raise HTTPException(status_code=400, detail="You are already on Pro.")
    else:
        upsert_profile(user["id"], user["email"])
        if active_subscription:
            raise HTTPException(status_code=400, detail="You already have Pro access.")

    track_event(
        event_name="upgrade_clicked",
        user_id=user["id"] if user else None,
        email=user["email"] if user else None,
        metadata={"checkout_plan": checkout_plan}
    )
    track_event(
        event_name="checkout_started",
        user_id=user["id"] if user else None,
        email=user["email"] if user else None,
        metadata={"checkout_plan": checkout_plan}
    )

    if checkout_plan == "one_time":
        print("CHECKOUT_SESSION_REQUEST: one_time")
        price_id = STRIPE_PRICE_ONE_TIME
        mode = "payment"
    else:
        print("CHECKOUT_SESSION_REQUEST: pro_monthly")
        price_id = STRIPE_PRICE_PRO_MONTHLY
        mode = "subscription"

    if not price_id:
        raise HTTPException(status_code=500, detail="Stripe price ID not configured.")

    session = require_stripe().checkout.Session.create(
        mode=mode,
        success_url=f"{SITE_URL}/?checkout=success&session_id={{CHECKOUT_SESSION_ID}}#tool",
        cancel_url=f"{SITE_URL}/cancel",
        line_items=[{"price": price_id, "quantity": 1}],
        customer_email=user["email"] if user and user.get("email") else None,
        client_reference_id=user["id"] if user else None,
        metadata={
            "user_id": user["id"] if user else "",
            "checkout_plan": checkout_plan,
        },
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
        return {"error": "Billing management is not available yet."}


@app.post("/api/create-billing-portal-session")
def create_billing_portal_session(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    return create_portal_session(authorization)


@app.post("/api/mark-password-ready")
def mark_password_ready(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
    try:
        user = get_user_from_token(authorization)
        upsert_profile(user["id"], user["email"])
        set_profile_password_ready(user["id"], True)
        return {"ok": True}
    except Exception as e:
        print("MARK PASSWORD READY ERROR:", repr(e))
        return {"error": "password_update_unavailable"}


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

            metadata = checkout_session.get("metadata") or {}
            checkout_plan = metadata.get("checkout_plan") if hasattr(metadata, "get") else None
            checkout_mode = checkout_session.get("mode")
            customer = checkout_session.get("customer")
            subscription = checkout_session.get("subscription")

            stripe_customer_id = stripe_id(customer)

            if checkout_plan == "one_time" or checkout_mode == "payment":
                grant_report_purchase(
                    user_id=user["id"],
                    email=user["email"],
                    stripe_checkout_session_id=checkout_session.get("id") or session_id,
                    stripe_customer_id=stripe_customer_id,
                    stripe_payment_intent_id=stripe_id(checkout_session.get("payment_intent")),
                )
                report_credits = count_available_report_purchases(user["id"])
                track_event(
                    event_name="one_time_report_activated",
                    user_id=user["id"],
                    email=user["email"],
                    metadata={
                        "stripe_checkout_session_id": checkout_session.get("id") or session_id,
                        "report_credits": report_credits,
                    }
                )
                return {
                    "ok": True,
                    "plan": "free",
                    "report_credit_granted": True,
                    "report_credits": report_credits,
                }

            stripe_subscription_id = stripe_id(subscription)
            stripe_subscription_status = subscription.get("status") if hasattr(subscription, "get") else getattr(subscription, "status", "active")

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
        checkout_mode = getattr(session, "mode", None)
        session_id = getattr(session, "id", None)
        customer_details = getattr(session, "customer_details", None)
        customer_email = None
        if customer_details is not None:
            customer_email = getattr(customer_details, "email", None)
            if customer_email is None and isinstance(customer_details, dict):
                customer_email = customer_details.get("email")
        if customer_email is None:
            customer_email = getattr(session, "customer_email", None)
        print(
            f"PAYMENT_EVENT: checkout_completed mode={checkout_mode} "
            f"customer_email={customer_email or ''} session_id={session_id or ''}"
        )
        user_id = metadata.get("user_id") if hasattr(metadata, "get") else getattr(session, "client_reference_id", None)
        checkout_plan = metadata.get("checkout_plan") if hasattr(metadata, "get") else None
        stripe_subscription_id = stripe_id(getattr(session, "subscription", None))
        stripe_customer_id = stripe_id(getattr(session, "customer", None))

        if user_id and (checkout_plan == "one_time" or checkout_mode == "payment"):
            grant_report_purchase(
                user_id=user_id,
                email=customer_email,
                stripe_checkout_session_id=session_id,
                stripe_customer_id=stripe_customer_id,
                stripe_payment_intent_id=stripe_id(getattr(session, "payment_intent", None)),
            )
        elif user_id and stripe_subscription_id:
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
            has_subscription_access = bool(plan["is_pro"])
            has_one_time_report_credit = bool(plan.get("report_credits", 0) > 0)
            should_generate_full_report = has_subscription_access or has_one_time_report_credit
            track_event(
                event_name="optimise_started",
                user_id=user["id"],
                email=user["email"],
                metadata={
                    "is_pro": has_subscription_access,
                    "has_one_time_report_credit": has_one_time_report_credit,
                }
            )
            track_event(
                event_name="cv_check_started",
                user_id=user["id"],
                email=user["email"],
                metadata={
                    "is_pro": has_subscription_access,
                    "has_one_time_report_credit": has_one_time_report_credit,
                }
            )

            if not should_generate_full_report and (plan["remaining_free_analyses_today"] or 0) <= 0:
                return {
                    "error": "You’ve used your free analyses for today. Upgrade to Pro for unlimited CV checks.",
                    "code": "PAYWALL",
                    "source": "error",
                    "plan": plan,
                }
        else:
            should_generate_full_report = False

        if not job_description or len(job_description) < 20:
            return {"error": "Please paste a fuller job description.", "source": "error"}

        if cvFile is not None and cvFile.filename:
            try:
                file_bytes = await cvFile.read()
                extracted_text = extract_cv_text(cvFile.filename, file_bytes)
            except ValueError as exc:
                logger.info("CV file validation failed: %s", exc)
                return {"error": "Could not read that file. Try a different PDF, DOCX, or TXT file.", "source": "error"}
            except Exception:
                return {"error": "Could not read that file. Try a different PDF, DOCX, or TXT file.", "source": "error"}

            if extracted_text:
                cv_text = extracted_text

        if not cv_text or len(cv_text) < 20:
            return {"error": "Please paste your CV text or upload a readable PDF, DOCX, or TXT file.", "source": "error"}

        raw = require_openai().responses.create(
            model=OPENAI_MODEL,
            input=build_prompt(job_description, cv_text, is_pro=should_generate_full_report),
            max_output_tokens=2200 if should_generate_full_report else 1300,
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

        data = normalize_analysis_data(data, is_pro=should_generate_full_report)

        payload = {
            "score": data.get("score", 0),
            "scoreBreakdown": data.get("scoreBreakdown", []),
            "matchedKeywords": data.get("matchedKeywords", []),
            "missingKeywords": data.get("missingKeywords", []),
            "keywordImportance": data.get("keywordImportance", {}),
            "strongPoints": data.get("strongPoints", []),
            "weakPoints": data.get("weakPoints", []),
            "bulletPoints": data.get("bulletPoints", []),
            "freeBulletRewrite": data.get("freeBulletRewrite", {}),
            "nextStep": data.get("nextStep", ""),
            "professionalSummary": data.get("professionalSummary", ""),
            "priorityFixes": data.get("priorityFixes", []),
            "priorityFixDetails": data.get("priorityFixDetails", []),
            "skillsSection": data.get("skillsSection", []),
            "atsTips": data.get("atsTips", []),
            "interviewRisks": data.get("interviewRisks", []),
            "source": "openai",
        }

        if not bool(plan and plan["is_pro"]):
            payload.update(build_anonymous_result_preview(data))

        if user:
            save_usage_event(user["id"])
            consumed_report_session_id = None
            if should_generate_full_report and plan and not plan["is_pro"] and plan.get("report_credits", 0) > 0:
                consumed_report_session_id = consume_report_purchase(user["id"])
                payload["oneTimeReportConsumed"] = True
                payload["consumedReportSessionId"] = consumed_report_session_id
                payload["fullReportUnlocked"] = True
            if should_generate_full_report:
                payload["fullReportUnlocked"] = True
                payload["reportAccess"] = "subscription" if bool(plan and plan["is_pro"]) else "one_time"
            save_analysis_history(user["id"], job_description, payload)
            payload["plan"] = get_plan_state(user["id"])
            track_event(
                event_name="optimise_succeeded",
                user_id=user["id"],
                email=user["email"],
                metadata={
                    "is_pro": bool(plan["is_pro"]),
                    "report_access": payload.get("reportAccess", "free"),
                    "one_time_report_consumed": bool(consumed_report_session_id),
                    "score": payload.get("score", 0),
                }
            )
            track_event(
                event_name="cv_check_completed",
                user_id=user["id"],
                email=user["email"],
                metadata={
                    "is_pro": bool(plan["is_pro"]),
                    "report_access": payload.get("reportAccess", "free"),
                    "score": payload.get("score", 0),
                }
            )
        else:
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
            content={"error": "We could not analyse your CV right now. Please try again."}
        )


app.mount("/static", StaticFiles(directory="static"), name="static")
