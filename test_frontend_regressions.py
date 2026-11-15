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
