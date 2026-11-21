from pathlib import Path


INDEX_HTML = Path(__file__).parent / "static" / "index.html"
MAIN_PY = Path(__file__).parent / "main.py"


def test_score_breakdown_helper_returns_canonical_items() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    helper_start = source.index("function getCanonicalScoreBreakdown")
    renderer_start = source.index("function renderScoreBreakdown", helper_start)
    helper_source = source[helper_start:renderer_start]

    assert "const breakdown = fallbackItems.map" in helper_source
    assert "return breakdown;" in helper_source


def test_model_scores_are_normalised_to_the_public_100_point_scale() -> None:
    source = MAIN_PY.read_text(encoding="utf-8")

    assert "def normalize_model_score" in source
    assert "if 0 < score <= 10:" in source
    assert "score *= 10" in source
    assert "round(score)" in source
    assert 'score = normalize_model_score(data.get("score", 0))' in source
    assert 'score = normalize_model_score(item.get("score", 0))' in source


def test_controlled_case_study_is_linked_and_transparent() -> None:
    main_source = MAIN_PY.read_text(encoding="utf-8")
    index_source = INDEX_HTML.read_text(encoding="utf-8")

    assert '@app.get("/cv-improvement-case-study"' in main_source
    assert "Controlled demonstration · fictional composite · not a customer testimonial" in main_source
    assert "does not prove that a score of 70 will produce interviews" in main_source
    assert 'href="/cv-improvement-case-study"' in index_source


def test_best_uk_cv_checker_page_matches_search_intent() -> None:
    source = MAIN_PY.read_text(encoding="utf-8")

    assert '"title": "How to Choose a CV Checker for UK Job Applications"' in source
    assert '"h1": "How to choose a CV checker for UK job applications"' in source
    assert '"Quick answer: what makes a good UK CV checker?"' in source
    assert '"What is the best CV checker in the UK?"' in source
    assert '"last_updated": "November 2026"' in source
    assert '("/best-cv-checker-for-uk-job-applications", "How to choose a UK CV checker")' in source


def test_homepage_search_snippet_describes_the_free_job_advert_check() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")

    title = "Free UK CV Checker for Your Job Advert | CV Optimiser"
    description = (
        "Check your CV against a UK job advert. "
        "Get a free role-fit score, missing keywords and priority fixes before you apply."
    )

    assert f"<title>{title}</title>" in source
    assert f'<meta name="description" content="{description}">' in source
    assert f'<meta property="og:title" content="{title}">' in source
    assert f'<meta name="twitter:title" content="{title}">' in source
    assert '<link rel="canonical" href="https://www.cv-optimiser.com/">' in source
    assert '"price": "0"' in source
    assert '"priceCurrency": "GBP"' in source


def test_sitemap_dates_only_the_pages_changed_today() -> None:
    source = MAIN_PY.read_text(encoding="utf-8")

    assert 'canonical_url("/"): "2026-11-21"' in source
    assert 'canonical_url("/best-cv-checker-for-uk-job-applications"): "2026-11-21"' in source
    assert 'canonical_url("/best-free-cv-checker-uk"): "2026-11-21"' in source
    assert 'lastmod = SITEMAP_LASTMOD_BY_LOC.get(entry["loc"])' in source
