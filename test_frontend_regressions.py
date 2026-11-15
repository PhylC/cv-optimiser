from pathlib import Path


INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def test_score_breakdown_helper_returns_canonical_items() -> None:
    source = INDEX_HTML.read_text(encoding="utf-8")
    helper_start = source.index("function getCanonicalScoreBreakdown")
    renderer_start = source.index("function renderScoreBreakdown", helper_start)
    helper_source = source[helper_start:renderer_start]

    assert "const breakdown = fallbackItems.map" in helper_source
    assert "return breakdown;" in helper_source
