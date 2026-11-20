from __future__ import annotations

import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from cv_rewrite_grounding import ground_cv_rewrites

SOURCE = (
    "Customer Support Assistant\n"
    "I chaired two support stand-ups while the team leader was away.\n"
    "I passed escalations to the duty manager.\n"
    "I did not delegate tasks or approve refunds."
)
BEFORE = "I chaired two support stand-ups while the team leader was away."
GROUNDED = "Chaired 2 support stand-ups while the team leader was away."
OUTCOME = "Chaired two support stand-ups while the team leader was away, improving resolution times."
INFLATED = "Led ongoing support stand-ups and delegated tasks."
ADVERT = "Customer Support Team Leader. Lead daily support meetings and approve refunds."


class RewriteGroundingTests(unittest.TestCase):
    def test_review_only_accepts_candidates_with_real_cv_evidence_and_preserves_scope(self):
        data = main.normalize_analysis_data({
            "score": 62,
            "freeBulletRewrite": {"before": BEFORE, "after": INFLATED},
            "bulletRewriteDetails": [
                {"before": BEFORE, "after": GROUNDED},
                {"before": BEFORE, "after": OUTCOME},
                {"before": "Managed a customer support team.", "after": "Managed a customer support team."},
            ],
            "bulletPoints": ["Reduced delays by 20%.", OUTCOME, "Passed escalations to the duty manager."],
            "professionalSummary": "Customer Support Assistant. Improved escalation performance.",
            "skillsSection": ["Refund approvals", "Escalation forwarding"],
            "sectionRewritePlan": [
                {"section": "Recent experience", "rewriteDirection": "Describe what you did.", "exampleLine": OUTCOME},
            ],
            "lineFeedback": [{"section": "Recent experience", "originalLine": BEFORE, "improvedLine": OUTCOME}],
        }, is_pro=True)

        def review(cv, entries):
            self.assertEqual(cv, SOURCE)
            suggestions = {entry["suggestion"]: entry["id"] for entry in entries}
            self.assertNotIn(INFLATED, suggestions)
            self.assertNotIn("Reduced delays by 20%.", suggestions)
            self.assertNotIn("Managed a customer support team.", suggestions)
            return [
                {"id": suggestions[GROUNDED], "evidence": [BEFORE]},
                {"id": suggestions["Passed escalations to the duty manager."], "evidence": ["I passed escalations to the duty manager."]},
                {"id": suggestions["Escalation forwarding"], "evidence": ["I passed escalations to the duty manager."]},
                {"id": suggestions["Refund approvals"], "evidence": ["I was authorised to approve refunds."]},
            ]

        output = ground_cv_rewrites(data, SOURCE, review)
        self.assertEqual(output["score"], 62)
        self.assertEqual(output["freeBulletRewrite"]["after"], GROUNDED)
        self.assertEqual(output["bulletPoints"], ["Passed escalations to the duty manager."])
        self.assertEqual(output["skillsSection"], ["Escalation forwarding"])
        self.assertEqual(output["professionalSummary"], "")
        self.assertEqual([item["improvedLine"] for item in output["lineFeedback"]], [GROUNDED])
        self.assertTrue(all(item["exampleLine"] == "" for item in output["sectionRewritePlan"]))

    def test_missing_model_rewrites_do_not_create_unrelated_examples(self):
        result = main.normalize_analysis_data({"score": 48, "missingKeywords": ["Refund approvals"]}, is_pro=True)
        self.assertEqual(result["freeBulletRewrite"]["after"], "")
        self.assertEqual(result["bulletRewriteDetails"], [])
        self.assertEqual(result["lineFeedback"], [])
        self.assertTrue(all(item["exampleLine"] == "" for item in result["sectionRewritePlan"]))

    def test_free_api_retains_the_analysis_when_verification_is_unavailable(self):
        generation = SimpleNamespace(output_text=json.dumps({
            "score": 64,
            "priorityFixDetails": [{
                "issue": "No evidence of refund approval", "change": "Show relevant experience from the CV.",
            }],
            "freeBulletRewrite": {"before": BEFORE, "after": OUTCOME},
            "bulletPoints": [OUTCOME],
        }))
        fake_client = SimpleNamespace(
            responses=SimpleNamespace(create=lambda **kwargs: generation),
            with_options=lambda **kwargs: SimpleNamespace(responses=SimpleNamespace(create=offline_review)),
        )
        with patch.object(main, "require_openai", return_value=fake_client):
            response = TestClient(main.app).post("/api/optimise", data={"jobDescription": ADVERT, "cvText": SOURCE})

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertTrue(result["isAnonymousResult"])
        self.assertEqual(result["score"], 64)
        self.assertEqual(result["freeBulletRewrite"]["after"], "")
        self.assertEqual(result["bulletPoints"], [])
        self.assertTrue(result["priorityFixDetails"])


def offline_review(**kwargs):
    raise TimeoutError("Verification unavailable")
