"""Check that application-ready CV wording is supported by the submitted CV."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable
from unicodedata import normalize

NUMBER_WORDS = {
    word: str(value)
    for value, word in enumerate(
        ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve")
    )
}
NUMBER_PATTERN = re.compile(
    r"(?<![\w.])(\d[\d,]*(?:\.\d+)?|" + "|".join(NUMBER_WORDS) + r")(?!\w)(?:\s*(%|percent\b|per cent\b))?",
    re.IGNORECASE,
)


def comparable(text: str) -> str:
    return " ".join(normalize("NFKC", text).translate(str.maketrans("’‘“”", "\x27\x27\x22\x22")).casefold().split())


def quantities(text: str) -> set[str]:
    return {
        NUMBER_WORDS.get(number.casefold(), number.replace(",", "")) + ("%" if percentage else "")
        for number, percentage in NUMBER_PATTERN.findall(text)
    }


@dataclass(frozen=True)
class Rewrite:
    text: str
    before: str = ""

    def can_review(self, source: str, source_quantities: set[str]) -> bool:
        if not self.text.strip():
            return False
        proposed = quantities(self.text)
        if not proposed <= source_quantities:
            return False
        return not self.before or (
            comparable(self.before) in source and quantities(self.before) <= proposed
        )


def ground_cv_rewrites(
    report: dict[str, Any],
    cv: str,
    review: Callable[[str, list[dict[str, Any]]], Any],
) -> dict[str, Any]:
    """Keep only verified CV text. Advice and scoring stay available on review failure."""
    free = report["freeBulletRewrite"]
    free_candidate = Rewrite(free["after"], free["before"])
    paired_groups = {
        "bulletRewriteDetails": ("after", "before"),
        "lineFeedback": ("improvedLine", "originalLine"),
    }
    list_fields = ("bulletPoints", "skillsSection", "strongerBullets")
    source = comparable(cv)
    source_quantities = quantities(cv)
    candidates: dict[Rewrite, int] = {}

    def add(candidate: Rewrite) -> Rewrite:
        if candidate not in candidates and candidate.can_review(source, source_quantities):
            candidates[candidate] = len(candidates) + 1
        return candidate

    if free["before"]:
        add(free_candidate)
    scalar = add(Rewrite(report["professionalSummary"]))
    list_rewrites = {
        field: [add(Rewrite(text)) for text in report[field]]
        for field in list_fields
    }
    paired_rewrites = {
        field: [add(Rewrite(item[after], item[before])) for item in report[field] if item[before]]
        for field, (after, before) in paired_groups.items()
    }
    sections = [add(Rewrite(item["exampleLine"])) for item in report["sectionRewritePlan"]]

    approved_ids: set[int] = set()
    if candidates:
        entries = [
            {"id": identifier, "suggestion": candidate.text, "original": candidate.before}
            for candidate, identifier in candidates.items()
        ]
        verdicts = review(cv, entries)
        if isinstance(verdicts, list):
            for verdict in verdicts:
                if not isinstance(verdict, dict) or type(verdict.get("id")) is not int:
                    continue
                evidence = verdict.get("evidence")
                if isinstance(evidence, list) and evidence and all(
                    isinstance(quote, str) and comparable(quote) and comparable(quote) in source
                    for quote in evidence
                ):
                    approved_ids.add(verdict["id"])

    def approved(candidate: Rewrite) -> bool:
        return candidates.get(candidate) in approved_ids

    output = dict(report)
    output["professionalSummary"] = scalar.text if approved(scalar) else ""
    for field, rewrites in list_rewrites.items():
        output[field] = [candidate.text for candidate in rewrites if approved(candidate)]
    for field, (after, before) in paired_groups.items():
        approved_pairs = {candidate for candidate in paired_rewrites[field] if approved(candidate)}
        output[field] = [item for item in report[field] if Rewrite(item[after], item[before]) in approved_pairs]

    if approved(free_candidate) and free["before"]:
        output["freeBulletRewrite"] = free
    elif output["bulletRewriteDetails"]:
        first = output["bulletRewriteDetails"][0]
        output["freeBulletRewrite"] = {"before": first["before"], "after": first["after"], "whyWeak": first["whyBetter"]}
    else:
        output["freeBulletRewrite"] = {"before": "", "after": "", "whyWeak": ""}

    output["sectionRewritePlan"] = [
        {**item, "exampleLine": candidate.text if approved(candidate) else ""}
        for item, candidate in zip(report["sectionRewritePlan"], sections)
    ]
    return output
