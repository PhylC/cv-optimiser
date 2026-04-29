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
from fastapi import Body, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
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
SITE_URL = os.getenv("SITE_URL", "https://www.cv-optimiser.com").strip().rstrip("/")
FREE_ANALYSES_PER_DAY = int(os.getenv("FREE_ANALYSES_PER_DAY", "3").strip())

DEFAULT_SUPABASE_URL = "https://zsooelsnjplxnqjvzuab.supabase.co"
DEFAULT_SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Inpzb29lbHNuanBseG5xanZ6dWFiIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NzU4OTg1MjMsImV4cCI6MjA5MTQ3NDUyM30."
    "m3ego7yz2vHwoeM7Uj3EmNOXTZZx7Ca7VCmeW5DQmgY"
)

SUPABASE_URL = os.getenv("SUPABASE_URL", DEFAULT_SUPABASE_URL).strip()
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", DEFAULT_SUPABASE_ANON_KEY).strip()
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()

STRIPE_SECRET_KEY = os.getenv("STRIPE_SECRET_KEY", "").strip()
STRIPE_PRICE_ONE_TIME = os.getenv("STRIPE_PRICE_ONE_TIME", "").strip()
STRIPE_PRICE_PRO_MONTHLY = os.getenv("STRIPE_PRICE_PRO_MONTHLY", os.getenv("STRIPE_PRICE_ID", "")).strip()
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

openai_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None
supabase_admin: Optional[Client] = None

if SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY:
    supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)

FAQ_ENTRIES: list[tuple[str, str]] = [
    (
        "Why does my CV get rejected instantly?",
        "Most CVs are filtered by ATS systems before a recruiter sees them. If your CV doesn’t contain the right keywords or match the job description, it can be rejected automatically.",
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
        "Can I beat ATS without keywords?",
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
                    "Most job seekers apply to multiple roles before getting interviews",
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

TOOL_LANDING_PAGES: dict[str, dict[str, Any]] = {
    "cv-checker": {
        "title": "Free CV Checker | Compare Your CV to Any Job Description",
        "meta_description": "Use our free CV checker to compare your CV to any job description. Get your match score, missing keywords and top improvements in seconds.",
        "h1": "Free CV Checker",
        "intro": "See how well your CV matches a job description and what to fix.",
        "tool_intro": [
            "Most CVs get rejected in seconds — not because of experience, but because they don’t match the job.",
            "Paste your CV and a job description below to get your match score and improvement suggestions.",
        ],
        "tool_heading": "Check my CV",
        "sections": [
            {
                "title": "What this CV checker does",
                "copy": "This CV checker compares your CV against a job description to show:",
                "bullets": [
                    "Your CV match score",
                    "Missing keywords for the role",
                    "What recruiters may miss",
                    "The most important improvements to make",
                ],
                "helper": "It’s designed to reflect how your CV is likely to perform in real job applications.",
            },
            {
                "title": "Why most CVs get rejected",
                "copy": "Many CVs are rejected before a recruiter reads them properly.",
                "bullets": [
                    "Important keywords from the job description are missing",
                    "Experience isn’t clearly aligned to the role",
                    "Achievements are vague or not measurable",
                    "The CV doesn’t quickly show relevance",
                ],
                "helper": "Fixing these issues can significantly improve your chances of getting interviews.",
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
        ],
        "example_title": "Example CV diagnosis",
        "example_score": "Score: 58/100 — likely to be skipped",
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
        "intro": "Find the keywords your CV is missing and improve your chances of getting interviews.",
        "tool_intro": [
            "Recruiters and ATS systems often scan for specific terms from the job description. If those keywords are missing, your CV may not be shortlisted.",
        ],
        "tool_heading": "Optimise your CV keywords",
        "sections": [
            {
                "title": "Why keywords matter",
                "copy": "Recruiters and ATS systems often scan for specific terms from the job description. If those keywords are missing, your CV may not be shortlisted.",
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
            "If your CV doesn’t match the job description closely, it may be filtered out or ranked lower before a human reviews it.",
        ],
        "tool_heading": "Check your CV for ATS compatibility",
        "sections": [
            {
                "title": "What is an ATS?",
                "copy": "An Applicant Tracking System scans CVs for keywords, structure and relevance before a recruiter reviews them.",
            },
            {
                "title": "Why it matters",
                "copy": "If your CV does not match the job description closely, it may be filtered out or ranked lower before a human reviews it.",
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
        "cta_copy": "Use the checker to see whether your CV is likely to survive ATS screening before you apply.",
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
        "cta_copy": "Get actionable CV feedback and focus on the improvements most likely to help you win interviews.",
        "cta_label": "Improve your CV now",
    },
}

BLOG_ARTICLES: dict[str, dict[str, Any]] = {
    "why-is-my-cv-not-getting-interviews": {
        "title": "Why Your CV Isn’t Getting Interviews (And How to Fix It Fast)",
        "meta_description": "Applying for jobs and hearing nothing back? See why your CV is getting ignored and what to fix first.",
        "h1": "Why Your CV Isn’t Getting Interviews",
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
            ("/how-to-tailor-cv-to-job-description", "How to tailor your CV to a job description"),
            ("/example-cv-report", "See an example CV report"),
            ("/cv-checker", "Use the CV checker"),
        ],
    },
    "how-to-tailor-cv-to-job-description": {
        "title": "How to Tailor Your CV to a Job Description | CV Optimiser",
        "meta_description": "Learn how to tailor your CV to a job description using keywords, relevant experience and clearer achievements.",
        "h1": "How to Tailor Your CV to a Job Description",
        "intro": "Tailoring your CV isn’t optional — it’s the difference between getting ignored and getting interviews.",
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
                    "A tailored CV makes your fit obvious faster. That is what gets interviews.",
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
        "h1": "ATS CV Keywords: How to Get Past Filters",
        "intro": "Most CVs fail before a human ever sees them. ATS software scans for keywords that match the job description.",
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
        "title": "CV Mistakes That Cost You Interviews | CV Optimiser",
        "meta_description": "Avoid common CV mistakes that reduce your chances of getting interviews, from vague achievements to missing keywords.",
        "h1": "CV Mistakes That Cost You Interviews",
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

SITEMAP_URLS: list[str] = [
    f"{SITE_URL}/",
    f"{SITE_URL}/cv-checker",
    f"{SITE_URL}/cv-score-checker",
    f"{SITE_URL}/job-description-cv-match",
    f"{SITE_URL}/cv-keyword-optimiser",
    f"{SITE_URL}/ats-cv-checker",
    f"{SITE_URL}/cv-improvement-tool",
    f"{SITE_URL}/upgrade",
    f"{SITE_URL}/cv-statistics",
    f"{SITE_URL}/faq",
    f"{SITE_URL}/how-it-works",
    f"{SITE_URL}/example-cv-report",
    f"{SITE_URL}/why-is-my-cv-not-getting-interviews",
    f"{SITE_URL}/how-to-tailor-cv-to-job-description",
    f"{SITE_URL}/ats-cv-keywords",
    f"{SITE_URL}/cv-mistakes-that-cost-interviews",
    f"{SITE_URL}/how-to-improve-cv-score",
    f"{SITE_URL}/about",
    f"{SITE_URL}/privacy",
    f"{SITE_URL}/terms",
]


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
            align-items: center;
            justify-content: space-between;
            gap: 16px;
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
            gap: 14px;
            flex-wrap: wrap;
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
          body[data-auth-state="loading"] #upgradeLink,
          body[data-auth-state="loading"] #accountMenuWrap {
            display: none !important;
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
          .account-menu-button {
            display: inline-flex;
            align-items: center;
            gap: 10px;
            height: 44px;
            max-width: 240px;
            padding: 0 14px;
            border-radius: 14px;
            border: 1px solid rgba(92, 112, 150, 0.26);
            background: rgba(10, 19, 35, 0.6);
            color: #E8EEFC;
            cursor: pointer;
            text-align: left;
            box-shadow: none;
            transition: border-color 0.12s ease, background 0.12s ease, transform 0.12s ease;
          }
          .account-menu-button:hover {
            border-color: rgba(120, 140, 194, 0.34);
            background: rgba(14, 25, 46, 0.8);
            transform: translateY(-1px);
          }
          .account-chip-text {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            min-width: 0;
          }
          .account-mobile-label {
            display: none;
            color: #F4F7FF;
            font-size: 13px;
            font-weight: 700;
            line-height: 1.2;
          }
          .account-email {
            max-width: 220px;
            color: #F4F7FF;
            font-size: 13px;
            font-weight: 700;
            line-height: 1.2;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
          }
          .account-plan {
            color: #DCE6FF;
            font-size: 11px;
            font-weight: 800;
            line-height: 1;
            text-transform: uppercase;
            padding: 3px 7px;
            border-radius: 999px;
            background: rgba(91, 120, 255, 0.18);
            border: 1px solid rgba(91, 120, 255, 0.24);
            white-space: nowrap;
          }
          .account-caret {
            color: #9FB0D4;
            font-size: 12px;
            flex-shrink: 0;
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
            .site-header-inner {
              display: grid;
              grid-template-columns: minmax(0, 1fr) auto;
              gap: 12px;
              align-items: center;
            }
            .site-nav {
              display: none;
            }
            .site-header-right {
              display: contents;
            }
            .header-actions {
              display: contents;
            }
            .site-logo {
              min-width: 0;
            }
            .site-logo-title {
              font-size: 22px;
            }
            .header-signin-link {
              grid-column: 1 / -1;
              width: 100%;
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
              grid-column: 1 / -1;
              width: 100%;
            }
            .account-menu-button {
              width: 100%;
              max-width: 100%;
              justify-content: space-between;
              padding: 0 12px;
            }
            .account-email {
              display: none;
            }
            .account-mobile-label {
              display: inline;
            }
            .account-dropdown {
              position: static;
              width: 100%;
              margin-top: 8px;
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


def build_site_header(active_key: Optional[str] = None, cta_href: str = "/#tool") -> str:
    nav_items = [
        ("cv-checker", "/cv-checker", "CV Checker"),
        ("how-it-works", "/how-it-works", "How it works"),
        ("example-report", "/example-cv-report", "Example Report"),
        ("upgrade", "/upgrade", "Upgrade"),
    ]
    nav_html = "".join(
        f'<a href="{href}"'
        f'{" id=\"upgradeLink\"" if key == "upgrade" else ""}'
        f' class="site-nav-link{" hidden" if key == "upgrade" else ""}{" is-active" if active_key == key else ""}"'
        f'{" data-upgrade-link" if key == "upgrade" else ""}>{label}</a>'
        for key, href, label in nav_items
    )
    return f"""
    <header id="siteHeader" class="site-header">
      <div class="site-header-inner">
        <a href="/" class="site-logo">
          <span class="site-logo-mark">CV</span>
          <span class="site-logo-title"><strong>CV</strong> <span>Optimiser</span></span>
        </a>
        <div class="site-header-right header-actions">
          <nav class="site-nav" aria-label="Primary">
            {nav_html}
          </nav>
          <div id="authLoadingPlaceholder" class="auth-placeholder"></div>
          <a href="/#authCard" id="signInLink" class="header-signin-link hidden">Sign in</a>
          <div id="accountMenuWrap" class="account-menu-wrap hidden">
            <button id="accountMenuButton" class="account-menu-button" type="button" aria-expanded="false" aria-controls="accountDropdown">
              <span class="account-chip-text">
                <span class="account-mobile-label">Account</span>
                <span id="accountEmail" class="account-email">Account</span>
                <span id="accountPlan" class="account-plan">Checking plan...</span>
              </span>
              <span class="account-caret">▾</span>
            </button>
            <div id="accountDropdown" class="account-dropdown hidden" aria-hidden="true">
              <a href="/#authCard" id="headerAccountLink" data-account-action="account">Account</a>
              <button id="menuManageSubBtn" type="button" data-account-action="billing">Manage subscription</button>
              <div id="headerBillingNote" class="account-dropdown-note hidden">Billing management is not available yet.</div>
              <button id="menuLogoutBtn" type="button" data-account-action="signout">Sign out</button>
            </div>
          </div>
          <a href="{html.escape(cta_href)}" class="site-header-cta">Check my CV</a>
        </div>
      </div>
    </header>
    """


def build_footer_assets_head() -> str:
    return (
        '<link rel="stylesheet" href="/static/global-footer.css">'
        '<script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2"></script>'
        f"<script>window.CV_OPTIMISER_SUPABASE_URL = {json.dumps(SUPABASE_URL)};"
        f"window.CV_OPTIMISER_SUPABASE_ANON_KEY = {json.dumps(SUPABASE_ANON_KEY)};</script>"
        '<script src="/static/global-account.js"></script>'
    )


def build_site_footer() -> str:
    return '<div id="siteFooter"></div><script src="/static/global-footer.js" defer></script>'


def build_tool_embed_script() -> str:
    return """
        <script>
          (function () {
            function resizeToolFrame(targetFrame, nextHeight) {
              if (!targetFrame || !nextHeight) return;
              const safeHeight = Math.max(Number(nextHeight) || 0, 960);
              targetFrame.style.height = safeHeight + "px";
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
    page_url = f"{SITE_URL}/{slug}"
    upgrade_notice_html = ""
    upgrade_notice_script = ""
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
    section_html = "".join(
        f"""
        <div class="card">
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
    example_score = page.get("example_score", "Score: 58/100 — likely to be skipped")
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
        <link rel="canonical" href="{page_url}">
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
            max-width: 1100px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
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
            gap: 20px;
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
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
          </div>
          <div id="landing-tool" class="tool-card">
            <h2>{html.escape(page["tool_heading"])}</h2>
            {tool_intro_html}
            <iframe class="tool-frame tool-embed compact" src="/?embed_tool=1&compact=1" title="{html.escape(page['h1'])} tool"></iframe>
          </div>
          <div class="content-grid">
            <div class="section-stack">{section_html}</div>
            <div class="section-stack">
              <div class="card">
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
    page_url = f"{SITE_URL}/{slug}"
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
        <link rel="canonical" href="{page_url}">
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
            max-width: 960px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
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

          @media (max-width: 900px) {{

          }}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header()}
          <div class="card">
            <h1>{html.escape(page["h1"])}</h1>
            <p>{html.escape(page["intro"])}</p>
            {summary_html}
            <div class="cta-block-tight">
              <a href="/cv-checker" class="cta cta-button">{html.escape(page["top_cta"])}</a>
            </div>
            {sections_html}
            {related_html}
          </div>
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
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
                <p>Most CVs get rejected in seconds — not because of experience, but because they don’t match the job.</p>
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
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
            max-width: 1040px;
            margin: 0 auto;
            padding: 28px 20px 60px;
          }}
{build_site_header_css()}
{build_typography_css()}
{build_cta_spacing_css()}
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header("example-report")}

          <div class="hero-card">
            <div class="eyebrow">Example report</div>
            <h1>{html.escape(EXAMPLE_REPORT_PAGE["h1"])}</h1>
            <p>{html.escape(EXAMPLE_REPORT_PAGE["intro"])}</p>
            <div class="cta-row cta-block-tight">
              <a href="/#tool" class="cta cta-button">Check your CV now</a>
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
                <div class="cta-row cta-block-tight">
                  <a href="/upgrade" class="cta cta-button" data-upgrade-link>Unlock full report</a>
                </div>
              </div>
            </div>
          </div>

          <section class="final-cta">
            <h2>Check your CV now</h2>
            <p>Upload your CV, paste a job description and get your score in under 60 seconds.</p>
            <div class="cta-row cta-block-tight">
              <a href="/#tool" class="cta cta-button">Check your CV now</a>
            </div>
          </section>

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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
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
        <title>CV FAQ | ATS, CV Scores and Why Your CV Gets Ignored</title>
        <meta name="description" content="Direct answers on ATS filters, CV scores, keywords, tailoring your CV, and why strong candidates still get ignored.">
        <link rel="canonical" href="{page_url}">
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
            max-width: 960px;
            margin: 0 auto;
            padding: 28px 20px 60px;
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header()}
          <div class="card">
            <h1>Frequently asked questions</h1>
            <p>If your CV keeps getting ignored, these are the questions that actually matter.</p>
            <div class="summary-box">
              <strong>Quick answers:</strong>
              <ul>
                <li>ATS systems filter weak matches before recruiters see them</li>
                <li>Keywords matter, but only when they reflect real relevance</li>
                <li>Generic CVs lose because they make fit harder to see</li>
              </ul>
            </div>
            <div class="faq-list">{faq_html}</div>
          </div>
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
        <link rel="canonical" href="{page_url}">
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
            max-width: 960px;
            margin: 0 auto;
            padding: 28px 20px 60px;
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
            margin-top: 18px;
            padding-top: 18px;
            border-top: 1px solid rgba(80, 103, 146, 0.18);
          }}

          .text-link:hover {{
            color: #FFFFFF;
          }}

          @media (max-width: 900px) {{

          }}
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header("how-it-works" if slug == "how-it-works" else None)}
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


def render_upgrade_page() -> str:
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Upgrade | CV Optimiser</title>
        <meta name="description" content="Choose between a one-time full CV report or an ongoing Pro plan.">
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
        </style>
      </head>
      <body data-auth-state="loading">
        <div class="page">
          {build_site_header("upgrade")}
          <div class="hero">
            <h1>Unlock your full CV improvement plan</h1>
            <p>Choose how you want to improve your CV.</p>
          </div>

          <div id="upgradeLoadingState" class="upgrade-loading-state">Checking account status...</div>

          <div id="upgradeGrid" class="upgrade-grid hidden">
            <div id="oneTimeCard" class="upgrade-card upgrade-card-primary">
              <h2>Unlock this report</h2>
              <div class="price">£7.99 one-time</div>
              <ul>
                <li>Full rewritten professional summary</li>
                <li>Stronger bullet points tailored to the job</li>
                <li>Complete keyword optimisation</li>
                <li>Step-by-step improvement plan</li>
              </ul>
              <button class="checkout-btn unlock-report" data-checkout-plan="one_time" type="button">Unlock full report — £7.99</button>
              <p class="upgrade-helper">No account needed for one-time checkout.</p>
            </div>

            <div id="proCard" class="upgrade-card">
              <h2>Go Pro</h2>
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
            <p>Your Pro access is active. You can run unlimited CV checks and access full reports.</p>
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
              oneTimeButton.textContent = "Unlock full report — £7.99";
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

              const requiresSignIn = plan === "pro_monthly";
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
                showUpgradeInlineError("Please sign in to start Pro monthly.");
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
              showUpgradeInlineError(error.message || "Could not open checkout. Please try again.");
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


def render_status_page(title: str, heading: str, copy: str) -> str:
    return f"""
    <!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>{html.escape(title)}</title>
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
      </body>
    </html>
    """


@app.get("/")
def home() -> FileResponse:
    return FileResponse("static/index.html")


@app.get("/cv-checker", response_class=HTMLResponse)
@app.get("/cv-checker/", response_class=HTMLResponse, include_in_schema=False)
def cv_checker_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("cv-checker", TOOL_LANDING_PAGES["cv-checker"])


@app.get("/cv-score-checker", response_class=HTMLResponse)
@app.get("/cv-score-checker/", response_class=HTMLResponse, include_in_schema=False)
def cv_score_checker_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("cv-score-checker", TOOL_LANDING_PAGES["cv-score-checker"])


@app.get("/job-description-cv-match", response_class=HTMLResponse)
@app.get("/job-description-cv-match/", response_class=HTMLResponse, include_in_schema=False)
def job_description_cv_match_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("job-description-cv-match", TOOL_LANDING_PAGES["job-description-cv-match"])


@app.get("/ats-cv-checker", response_class=HTMLResponse)
@app.get("/ats-cv-checker/", response_class=HTMLResponse, include_in_schema=False)
def ats_cv_checker_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("ats-cv-checker", TOOL_LANDING_PAGES["ats-cv-checker"])


@app.get("/cv-keyword-optimiser", response_class=HTMLResponse)
@app.get("/cv-keyword-optimiser/", response_class=HTMLResponse, include_in_schema=False)
def cv_keyword_optimiser_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("cv-keyword-optimiser", TOOL_LANDING_PAGES["cv-keyword-optimiser"])


@app.get("/cv-improvement-tool", response_class=HTMLResponse)
@app.get("/cv-improvement-tool/", response_class=HTMLResponse, include_in_schema=False)
def cv_improvement_tool_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_tool_landing_page("cv-improvement-tool", TOOL_LANDING_PAGES["cv-improvement-tool"])


@app.get("/example-cv-report", response_class=HTMLResponse)
@app.get("/example-cv-report/", response_class=HTMLResponse, include_in_schema=False)
def example_cv_report_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_example_report_page()


@app.get("/google4cffcb1da00a66a5.html")
def google_verification() -> PlainTextResponse:
    return PlainTextResponse("google-site-verification: google4cffcb1da00a66a5.html")


@app.get("/sitemap.xml")
def sitemap() -> Response:
    url_entries = "\n".join(
        f"""  <url>
    <loc>{html.escape(url)}</loc>
  </url>"""
        for url in SITEMAP_URLS
    )
    xml_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
{url_entries}
</urlset>
"""
    return Response(content=xml_content, media_type="application/xml")


@app.get("/faq", response_class=HTMLResponse)
def faq_page() -> str:
    return render_faq_page()


@app.get("/how-it-works", response_class=HTMLResponse)
@app.get("/how-it-works/", response_class=HTMLResponse, include_in_schema=False)
def how_it_works_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_support_page("how-it-works", SUPPORT_PAGES["how-it-works"])


@app.get("/cv-statistics", response_class=HTMLResponse)
@app.get("/cv-statistics/", response_class=HTMLResponse, include_in_schema=False)
def cv_statistics_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_support_page("cv-statistics", SUPPORT_PAGES["cv-statistics"])


@app.get("/why-is-my-cv-not-getting-interviews", response_class=HTMLResponse)
@app.get("/why-is-my-cv-not-getting-interviews/", response_class=HTMLResponse, include_in_schema=False)
def why_cv_not_getting_interviews_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("why-is-my-cv-not-getting-interviews", BLOG_ARTICLES["why-is-my-cv-not-getting-interviews"])


@app.get("/how-to-tailor-cv-to-job-description", response_class=HTMLResponse)
@app.get("/how-to-tailor-cv-to-job-description/", response_class=HTMLResponse, include_in_schema=False)
def tailor_cv_to_job_description_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("how-to-tailor-cv-to-job-description", BLOG_ARTICLES["how-to-tailor-cv-to-job-description"])


@app.get("/ats-cv-keywords", response_class=HTMLResponse)
@app.get("/ats-cv-keywords/", response_class=HTMLResponse, include_in_schema=False)
def ats_cv_keywords_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("ats-cv-keywords", BLOG_ARTICLES["ats-cv-keywords"])


@app.get("/cv-mistakes-that-cost-interviews", response_class=HTMLResponse)
@app.get("/cv-mistakes-that-cost-interviews/", response_class=HTMLResponse, include_in_schema=False)
def cv_mistakes_that_cost_interviews_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("cv-mistakes-that-cost-interviews", BLOG_ARTICLES["cv-mistakes-that-cost-interviews"])


@app.get("/how-to-improve-cv-score", response_class=HTMLResponse)
@app.get("/how-to-improve-cv-score/", response_class=HTMLResponse, include_in_schema=False)
def how_to_improve_cv_score_page(request: Request) -> str:
    log_seo_page_hit(request.url.path)
    return render_article_page("how-to-improve-cv-score", BLOG_ARTICLES["how-to-improve-cv-score"])


@app.get("/features", response_class=HTMLResponse)
def features_page() -> str:
    return render_support_page("features", SUPPORT_PAGES["features"])


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
        "Payment successful | CV Optimiser",
        "Payment successful",
        "Your full CV improvement plan is ready.",
    )


@app.get("/cancel", response_class=HTMLResponse)
def cancel() -> str:
    return render_status_page(
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
    return """
    <html>
      <head>
        <title>Billing & Cancellation | CV Optimiser</title>
        {build_footer_assets_head()}
        <style>
          body { font-family: Inter, Arial, sans-serif; max-width: 860px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; line-height: 1.7; }
          h1,h2 { color: #FFFFFF; }
          a { color: #9AB0FF; }
          p, li { color: #C7D3EE; }
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
        {build_footer_assets_head()}
        <style>
          body { font-family: Inter, Arial, sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px 60px; background: #07142D; color: #E8EEFC; }
          h1 { margin-bottom: 18px; }
          iframe { width: 100%; height: 80vh; border: 1px solid rgba(80,103,146,0.35); border-radius: 16px; background: white; }
          p, a { color: #C7D3EE; }
        </style>
      </head>
      <body data-auth-state="loading">
        <h1>Analytics</h1>
        <p>Open the raw analytics endpoint here:</p>
        <p><a href="/api/admin/analytics" target="_blank">/api/admin/analytics</a></p>
      </body>
    </html>
    """


@app.get("/api/me")
def api_me(authorization: Optional[str] = Header(None)) -> dict[str, Any]:
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
                "signed_in": False,
                "email": None,
                "plan": "free",
                "plan_state": None,
                "user": None,
                "user_id": None,
            }

        upsert_profile(user["id"], user["email"])
        plan_state = get_plan_state(user["id"])
        plan_name = get_user_plan(user)
        print("API_ME_AUTH: signed_in")
        print(f"API_ME_USER: {user['email']}")
        print(f"API_ME_PLAN: {plan_name}")
        return {
            "signed_in": True,
            "email": user["email"],
            "plan": plan_name,
            "plan_state": plan_state,
            "user": user,
            "user_id": user["id"],
        }
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

    if checkout_plan == "pro_monthly":
        if not user:
            raise HTTPException(status_code=401, detail="Please sign in to start Pro monthly.")
        upsert_profile(user["id"], user["email"])
        if active_subscription:
            raise HTTPException(status_code=400, detail="You are already on Pro.")
    elif user:
        upsert_profile(user["id"], user["email"])
        if active_subscription:
            raise HTTPException(status_code=400, detail="You already have Pro access.")

    track_event(
        event_name="upgrade_clicked",
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
        success_url=f"{SITE_URL}/success?session_id={{CHECKOUT_SESSION_ID}}",
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
        return {"error": str(e)}


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
