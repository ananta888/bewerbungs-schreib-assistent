from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import yaml

from scripts.audit_claims import audit, strip_annotations
from scripts.check_style import check_style
from scripts.common import load_yaml, validate_candidate, validate_style
from scripts.compare_modes import compare_documents
from scripts.init_profiles import initialize_profiles
from scripts.language_check import check_hunspell, check_languagetool, is_remote_server, plain_text
from scripts.validate_iteration import validate_iteration


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


class ProfileValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_yaml(FIXTURES / "valid-candidate.yaml")
        self.style = load_yaml(FIXTURES / "valid-style.yaml")

    def test_valid_profiles_pass(self) -> None:
        self.assertEqual(validate_candidate(self.candidate), [])
        self.assertEqual(validate_style(self.style), [])

    def test_verified_claim_requires_source(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["claims"][0]["evidence_refs"] = []
        self.assertTrue(any("verified claims require evidence" in item for item in validate_candidate(broken)))

    def test_conflicting_dates_fail(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["experience"][0]["start_date"] = "2025-01"
        broken["experience"][0]["end_date"] = "2024-01"
        self.assertTrue(any("start_date is after end_date" in item for item in validate_candidate(broken)))

    def test_unknown_claim_reference_fails(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["skills"][0]["claim_ids"] = ["claim-does-not-exist"]
        self.assertTrue(any("unknown reference" in item for item in validate_candidate(broken)))

    def test_verified_record_requires_source(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["experience"][0]["evidence_refs"] = []
        self.assertTrue(any("verified records require evidence" in item for item in validate_candidate(broken)))


class ClaimAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_yaml(FIXTURES / "valid-candidate.yaml")

    def document(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_valid_annotated_cv_passes(self) -> None:
        self.assertEqual(audit(self.candidate, self.document("valid-annotated-cv.md"), "cv", strict=True), [])

    def test_kafka_overclaim_fails(self) -> None:
        errors = audit(self.candidate, self.document("kafka-overclaim.md"), "cv", strict=True)
        self.assertTrue(any("unknown claim" in item for item in errors))

    def test_unsupported_skill_fails_even_with_editorial_annotation(self) -> None:
        document = "# Profil <!-- evidence: editorial -->\n\nKafka <!-- evidence: editorial -->\n"
        errors = audit(self.candidate, document, "cv", strict=True)
        self.assertTrue(any("unsupported or forbidden term" in item for item in errors))

    def test_unverified_metric_fails(self) -> None:
        errors = audit(self.candidate, self.document("unverified-metric.md"), "cv", strict=True)
        self.assertTrue(any("non-publishable" in item for item in errors))

    def test_missing_annotation_fails(self) -> None:
        errors = audit(self.candidate, self.document("missing-evidence.md"), "cv", strict=True)
        self.assertTrue(any("lacks an evidence annotation" in item for item in errors))

    def test_factual_heading_requires_annotation(self) -> None:
        document = "# Erika Beispiel\n\n## Berufserfahrung\n"
        errors = audit(self.candidate, document, "cv", strict=True)
        self.assertTrue(any("line 1" in item for item in errors))

    def test_output_restriction_fails(self) -> None:
        document = "# E-Mail\n\nKoordinierte Teams. <!-- evidence: claim-coordination -->\n"
        errors = audit(self.candidate, document, "email", strict=True)
        self.assertTrue(any("not allowed in output" in item for item in errors))

    def test_strip_removes_internal_annotations(self) -> None:
        clean = strip_annotations(self.document("valid-annotated-cv.md"))
        self.assertNotIn("<!-- evidence:", clean)
        self.assertIn("RabbitMQ", clean)


class StyleAndModeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.style = load_yaml(FIXTURES / "valid-style.yaml")

    def document(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_valid_cv_style_passes(self) -> None:
        self.assertEqual(check_style(self.style, self.document("valid-annotated-cv.md"), "cv"), [])

    def test_generic_german_style_fails(self) -> None:
        errors = check_style(self.style, self.document("generic-style.md"), "cover_letter")
        self.assertTrue(any("avoid patterns found" in item for item in errors))

    def test_ascii_spelling_variant_also_fails(self) -> None:
        document = "Mit grosser Begeisterung bewerbe ich mich. <!-- evidence: editorial -->"
        errors = check_style(self.style, document, "cover_letter")
        self.assertTrue(any("avoid patterns found" in item for item in errors))

    def test_matching_modes_pass(self) -> None:
        paths = [FIXTURES / "mode-conservative.md", FIXTURES / "mode-professional.md"]
        self.assertEqual(compare_documents(paths), [])

    def test_mismatching_modes_fail(self) -> None:
        paths = [FIXTURES / "mode-conservative.md", FIXTURES / "mode-mismatch.md"]
        self.assertTrue(compare_documents(paths))


class InitializationTests(unittest.TestCase):
    def test_initialize_and_refuse_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            created = initialize_profiles(target)
            self.assertEqual(len(created), 2)
            self.assertEqual(yaml.safe_load(created[0].read_text(encoding="utf-8"))["schema_version"], 2)
            with self.assertRaises(FileExistsError):
                initialize_profiles(target)


class IterationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_yaml(ROOT / "assets" / "iteration.template.yaml")

    def test_standard_iteration_passes(self) -> None:
        self.assertEqual(validate_iteration(self.manifest), [])

    def test_wrong_role_order_fails(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["passes"][1]["role"] = "recruiter_style_reviewer"
        self.assertTrue(any("role order" in item for item in validate_iteration(broken)))

    def test_unresolved_high_finding_fails(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["passes"][1]["findings"] = [
            {
                "id": "finding-unsupported-kafka",
                "severity": "high",
                "category": "evidence",
                "description": "Kafka is unsupported.",
                "evidence_refs": [],
                "status": "open",
                "disposition": "",
            }
        ]
        self.assertTrue(any("unresolved" in item for item in validate_iteration(broken)))

    def test_accepted_risk_requires_disposition(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["passes"][1]["findings"] = [
            {
                "id": "finding-gap",
                "severity": "high",
                "category": "ats",
                "description": "A must-have remains a gap.",
                "evidence_refs": [],
                "status": "accepted_risk",
                "disposition": "",
            }
        ]
        self.assertTrue(any("disposition" in item for item in validate_iteration(broken)))

    def test_malformed_final_pass_returns_error(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["passes"][-1] = "invalid"
        errors = validate_iteration(broken)
        self.assertTrue(any("expected a mapping" in item for item in errors))
        self.assertTrue(any("must output revision" in item for item in errors))


class LanguageToolTests(unittest.TestCase):
    def test_remote_server_requires_explicit_permission(self) -> None:
        self.assertTrue(is_remote_server("https://api.languagetool.org/v2/check"))
        with self.assertRaises(ValueError):
            check_languagetool("Text", "de-DE", "https://api.languagetool.org/v2/check")

    def test_languagetool_response_and_allowlist(self) -> None:
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {
                "matches": [
                    {
                        "message": "Unknown word",
                        "offset": 0,
                        "length": 8,
                        "replacements": [{"value": "Rabbit"}],
                        "rule": {"id": "MORFOLOGIK_RULE_DE_DE"},
                    }
                ]
            }
        ).encode("utf-8")
        response.__exit__.return_value = False
        with patch("urllib.request.urlopen", return_value=response):
            issues = check_languagetool("RabbitMQ", "de-DE", "http://localhost:8010/v2/check")
            allowed = check_languagetool(
                "RabbitMQ", "de-DE", "http://localhost:8010/v2/check", allowlist={"rabbitmq"}
            )
        self.assertEqual(len(issues), 1)
        self.assertEqual(allowed, [])

    def test_hunspell_uses_dictionary_and_allowlist(self) -> None:
        result = SimpleNamespace(returncode=0, stdout="Fehlerwort\nRabbitMQ\n", stderr="")
        with patch("shutil.which", return_value="/usr/bin/hunspell"), patch(
            "subprocess.run", return_value=result
        ) as run:
            issues = check_hunspell("RabbitMQ Fehlerwort", "de_DE", {"rabbitmq"})
        self.assertEqual([issue["text"] for issue in issues], ["Fehlerwort"])
        self.assertEqual(run.call_args.args[0], ["/usr/bin/hunspell", "-d", "de_DE", "-l"])

    def test_markdown_and_evidence_are_removed(self) -> None:
        text = plain_text("# Titel\n- RabbitMQ <!-- evidence: claim-rabbitmq -->")
        self.assertEqual(text, "Titel\nRabbitMQ ")


if __name__ == "__main__":
    unittest.main()
