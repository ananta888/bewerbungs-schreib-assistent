from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
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
from scripts.language_check import (
    check_hunspell,
    check_languagetool,
    is_remote_server,
    plain_text,
)
from scripts.match_contract import (
    analyze_job,
    build_match_matrix,
    validate_match_matrix,
)
from scripts.pipeline_contract import capabilities, finalize_pipeline, pipeline_status
from scripts.profile_contract import (
    add_import_proposals,
    apply_claim_patch,
    profile_evidence,
    profile_summary,
)
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
        self.assertTrue(
            any(
                "verified claims require evidence" in item
                for item in validate_candidate(broken)
            )
        )

    def test_conflicting_dates_fail(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["experience"][0]["start_date"] = "2025-01"
        broken["experience"][0]["end_date"] = "2024-01"
        self.assertTrue(
            any(
                "start_date is after end_date" in item
                for item in validate_candidate(broken)
            )
        )

    def test_unknown_claim_reference_fails(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["skills"][0]["claim_ids"] = ["claim-does-not-exist"]
        self.assertTrue(
            any("unknown reference" in item for item in validate_candidate(broken))
        )

    def test_verified_record_requires_source(self) -> None:
        broken = copy.deepcopy(self.candidate)
        broken["experience"][0]["evidence_refs"] = []
        self.assertTrue(
            any(
                "verified records require evidence" in item
                for item in validate_candidate(broken)
            )
        )


class ClaimAuditTests(unittest.TestCase):
    def setUp(self) -> None:
        self.candidate = load_yaml(FIXTURES / "valid-candidate.yaml")

    def document(self, name: str) -> str:
        return (FIXTURES / name).read_text(encoding="utf-8")

    def test_valid_annotated_cv_passes(self) -> None:
        self.assertEqual(
            audit(
                self.candidate,
                self.document("valid-annotated-cv.md"),
                "cv",
                strict=True,
            ),
            [],
        )

    def test_kafka_overclaim_fails(self) -> None:
        errors = audit(
            self.candidate, self.document("kafka-overclaim.md"), "cv", strict=True
        )
        self.assertTrue(any("unknown claim" in item for item in errors))

    def test_unsupported_skill_fails_even_with_editorial_annotation(self) -> None:
        document = "# Profil <!-- evidence: editorial -->\n\nKafka <!-- evidence: editorial -->\n"
        errors = audit(self.candidate, document, "cv", strict=True)
        self.assertTrue(any("unsupported or forbidden term" in item for item in errors))

    def test_unverified_metric_fails(self) -> None:
        errors = audit(
            self.candidate, self.document("unverified-metric.md"), "cv", strict=True
        )
        self.assertTrue(any("non-publishable" in item for item in errors))

    def test_missing_annotation_fails(self) -> None:
        errors = audit(
            self.candidate, self.document("missing-evidence.md"), "cv", strict=True
        )
        self.assertTrue(any("lacks an evidence annotation" in item for item in errors))

    def test_factual_heading_requires_annotation(self) -> None:
        document = "# Erika Beispiel\n\n## Berufserfahrung\n"
        errors = audit(self.candidate, document, "cv", strict=True)
        self.assertTrue(any("line 1" in item for item in errors))

    def test_output_restriction_fails(self) -> None:
        document = (
            "# E-Mail\n\nKoordinierte Teams. <!-- evidence: claim-coordination -->\n"
        )
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
        self.assertEqual(
            check_style(self.style, self.document("valid-annotated-cv.md"), "cv"), []
        )

    def test_visible_html_is_checked_without_markup_or_stylesheet_noise(self) -> None:
        document = """<!doctype html><html><head><style>
            article article article { display: block; }
            .hidden::after { content: 'Mit großer Begeisterung'; }
          </style></head><body><main>
            <h1>Erika Beispiel</h1>
            <h2>Berufserfahrung</h2>
            <p>Entwickelte robuste Schnittstellen.</p>
            <p>Automatisierte wichtige Regressionstests.</p>
          </main></body></html>"""
        self.assertEqual(check_style(self.style, document, "cv"), [])

    def test_exact_repeated_cv_role_fragments_do_not_count_as_prose_starts(self) -> None:
        document = """# Erika Beispiel
## Berufserfahrung
Software-Entwicklerin
Software-Entwicklerin
Software-Entwicklerin
"""
        self.assertEqual(check_style(self.style, document, "cv"), [])

    def test_generic_german_style_fails(self) -> None:
        errors = check_style(
            self.style, self.document("generic-style.md"), "cover_letter"
        )
        self.assertTrue(any("avoid patterns found" in item for item in errors))

    def test_ascii_spelling_variant_also_fails(self) -> None:
        document = (
            "Mit grosser Begeisterung bewerbe ich mich. <!-- evidence: editorial -->"
        )
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
            self.assertEqual(
                yaml.safe_load(created[0].read_text(encoding="utf-8"))[
                    "schema_version"
                ],
                2,
            )
            with self.assertRaises(FileExistsError):
                initialize_profiles(target)


class PipelineContractTests(unittest.TestCase):
    def test_capabilities_are_versioned_and_machine_readable(self) -> None:
        result = capabilities()
        self.assertEqual(result["contract"], "bewerbungs-pipeline")
        self.assertEqual(result["contract_version"], "1.0")
        self.assertIn("finalize", result["stages"])
        self.assertIn("critical", result["blocking_severities"])

    def test_status_reports_artifacts_without_reading_their_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "job-analysis.yaml").write_text(
                "schema_version: 1\n", encoding="utf-8"
            )
            result = pipeline_status(root, "synthetic-run")
        self.assertEqual(result["state"], "analysis")
        self.assertEqual(result["artifacts"], ["job-analysis.yaml"])
        self.assertNotIn("content", result)

    def test_finalize_command_owns_all_policy_gates_and_strips_annotations_last(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            final = Path(directory) / "final.md"
            result = finalize_pipeline(
                str(FIXTURES / "valid-candidate.yaml"),
                str(FIXTURES / "valid-style.yaml"),
                str(FIXTURES / "valid-annotated-cv.md"),
                str(ROOT / "assets" / "iteration.template.yaml"),
                "cv",
                str(final),
            )
            self.assertEqual(result["status"], "final")
            self.assertNotIn("<!-- evidence:", final.read_text(encoding="utf-8"))

    def test_finalize_command_returns_safe_structured_policy_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            result = finalize_pipeline(
                str(FIXTURES / "valid-candidate.yaml"),
                str(FIXTURES / "valid-style.yaml"),
                str(FIXTURES / "missing-evidence.md"),
                str(ROOT / "assets" / "iteration.template.yaml"),
                "cv",
                str(Path(directory) / "final.md"),
            )
            self.assertEqual(result["status"], "rejected")
            self.assertEqual(result["error"]["code"], "policy_gate_failed")

    def test_finalize_error_does_not_expose_private_paths(self) -> None:
        marker = "synthetic-private-candidate-name"
        result = finalize_pipeline(
            f"X:/{marker}/candidate.yaml",
            str(FIXTURES / "valid-style.yaml"),
            str(FIXTURES / "valid-annotated-cv.md"),
            str(ROOT / "assets" / "iteration.template.yaml"),
            "cv",
            f"X:/{marker}/final.md",
        )
        detail = result["error"]["safe_detail"]
        self.assertNotIn(marker, detail)
        self.assertNotIn("candidate.yaml", detail)


class ProfileContractTests(unittest.TestCase):
    def test_import_acceptance_adds_only_unverified_claim_with_hash_provenance(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "candidate.yaml"
            target.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = add_import_proposals(
                target,
                [
                    {
                        "id": "claim-imported",
                        "statement": "User supplied statement",
                        "sha256": "a" * 64,
                    }
                ],
                True,
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )
            candidate = load_yaml(target)
            claim = next(
                item for item in candidate["claims"] if item["id"] == "claim-imported"
            )
            self.assertEqual(result["status"], "added_unverified")
            self.assertEqual(claim["status"], "unverified")
            self.assertEqual(candidate["sources"][-1]["location"], f"sha256:{'a' * 64}")

    def test_summary_exposes_claim_provenance_and_status(self) -> None:
        result = profile_summary(FIXTURES / "valid-candidate.yaml")
        claim = result["claims"][0]
        self.assertIn("status", claim)
        self.assertIn("evidence_refs", claim)
        self.assertIn("allowed_outputs", claim)

    def test_evidence_snapshot_keeps_only_publishable_output_bound_records(
        self,
    ) -> None:
        result = profile_evidence(FIXTURES / "valid-candidate.yaml", "cv")
        claim_ids = {item["id"] for item in result["claims"]}

        self.assertEqual(result["contract"], "candidate-evidence-snapshot")
        self.assertTrue(result["valid"])
        self.assertNotIn("profile", result)
        self.assertIn("claim-role", claim_ids)
        self.assertIn("claim-coordination", claim_ids)
        self.assertNotIn("claim-users", claim_ids)
        self.assertEqual(result["records"]["experience"][0]["id"], "experience-example")
        self.assertEqual(
            set(result["records"]["experience"][0]["claim_ids"]),
            {"claim-role", "claim-rabbitmq", "claim-coordination"},
        )
        self.assertEqual(result["records"]["languages"], [])

        email = profile_evidence(FIXTURES / "valid-candidate.yaml", "email")
        self.assertEqual(email["records"]["experience"], [])

    def test_patch_requires_confirmation_for_publishable_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "candidate.yaml"
            target.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                apply_claim_patch(
                    target,
                    [
                        {
                            "claim_id": "claim-role",
                            "field": "status",
                            "value": "user_confirmed",
                        }
                    ],
                    False,
                    hashlib.sha256(target.read_bytes()).hexdigest(),
                )

    def test_valid_patch_is_atomic_and_revalidated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "candidate.yaml"
            target.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = apply_claim_patch(
                target,
                [
                    {
                        "claim_id": "claim-role",
                        "field": "statement",
                        "value": "Senior Engineer bei Example GmbH",
                    }
                ],
                True,
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )
            self.assertEqual(result["status"], "updated")
            self.assertTrue(result["history_recorded"])
            self.assertEqual(
                load_yaml(target)["claims"][0]["statement"],
                "Senior Engineer bei Example GmbH",
            )
            history = target.with_suffix(".yaml.history.jsonl").read_text(
                encoding="utf-8"
            )
            self.assertIn("before_sha256", history)
            self.assertNotIn("Senior Engineer bei Example GmbH", history)

    def test_summary_reports_non_publishable_claim_quality(self) -> None:
        result = profile_summary(FIXTURES / "valid-candidate.yaml")
        self.assertTrue(
            any(
                item["claim_id"] == "claim-users"
                and item["category"] == "non_publishable"
                for item in result["quality_findings"]
            )
        )

    def test_summary_reports_orphan_evidence_and_claim_references(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate = load_yaml(FIXTURES / "valid-candidate.yaml")
            candidate["claims"][0]["evidence_refs"] = ["missing-source"]
            candidate["skills"][0]["claim_ids"] = ["missing-claim"]
            target = Path(directory) / "candidate.yaml"
            target.write_text(yaml.safe_dump(candidate), encoding="utf-8")
            categories = {
                item["category"] for item in profile_summary(target)["quality_findings"]
            }
            self.assertIn("orphan_evidence_reference", categories)
            self.assertIn("orphan_claim_reference", categories)


class MatchContractTests(unittest.TestCase):
    def test_analysis_preserves_explicit_source_text(self) -> None:
        analysis = analyze_job(
            {
                "id": "job-1",
                "title": "Engineer",
                "company": "Example",
                "description": "RabbitMQ required",
                "skills": ["RabbitMQ", "Kafka"],
            }
        )
        self.assertEqual(analysis["requirements"][0]["original_text"], "RabbitMQ")
        self.assertEqual(analysis["requirements"][0]["source_field"], "skills[0]")
        self.assertFalse(analysis["requirements"][0]["inferred"])

    def test_analysis_extracts_explicit_bilingual_anchors_without_inference(
        self,
    ) -> None:
        analysis = analyze_job(
            {
                "id": "job-2",
                "title": "Engineer",
                "location": "Berlin",
                "language": "de",
                "description": (
                    "Kafka ist zwingend erforderlich. "
                    "Nice-to-have: Terraform. Du wirst Plattformen betreiben."
                ),
            }
        )
        extracted = {item["priority"]: item for item in analysis["requirements"]}
        self.assertIn("Kafka", extracted["must_have"]["source_anchor"]["text"])
        self.assertEqual(extracted["optional"]["source_field"], "description")
        self.assertFalse(extracted["must_have"]["inferred"])
        self.assertEqual(len(analysis["responsibilities"]), 1)
        self.assertEqual(analysis["conditions"]["location"]["value"], "Berlin")
        self.assertEqual(len(analysis["analysis_version"]), 16)

    def test_analysis_recognizes_german_umlaut_anchors(self) -> None:
        analysis = analyze_job(
            {
                "id": "job-umlaut",
                "title": "Engineer",
                "description": (
                    "Terraform ist wünschenswert. "
                    "Du bist für Plattformen zuständig."
                ),
            }
        )
        self.assertTrue(
            any(item["priority"] == "optional" for item in analysis["requirements"])
        )
        self.assertEqual(len(analysis["responsibilities"]), 1)

    def test_matrix_uses_only_publishable_claims_and_leaves_kafka_as_gap(self) -> None:
        candidate = load_yaml(FIXTURES / "valid-candidate.yaml")
        analysis = analyze_job(
            {
                "id": "job-1",
                "title": "Engineer",
                "company": "Example",
                "description": "",
                "skills": ["RabbitMQ", "Kafka"],
            }
        )
        matrix = build_match_matrix(analysis, candidate, "cover_letter")
        by_competency = {item["competency"]: item for item in matrix["matches"]}
        self.assertEqual(by_competency["RabbitMQ"]["classification"], "direct_match")
        self.assertEqual(
            by_competency["RabbitMQ"]["evidence_claim_ids"], ["claim-rabbitmq"]
        )
        self.assertEqual(by_competency["Kafka"]["classification"], "gap")
        self.assertEqual(by_competency["Kafka"]["evidence_claim_ids"], [])

    def test_reviewed_transferable_match_requires_publishable_evidence(self) -> None:
        candidate = load_yaml(FIXTURES / "valid-candidate.yaml")
        valid = {
            "matches": [
                {
                    "requirement_id": "req-kafka",
                    "classification": "transferable_match",
                    "evidence_claim_ids": ["claim-rabbitmq"],
                }
            ]
        }
        self.assertEqual(validate_match_matrix(valid, candidate, "cover_letter"), [])
        valid["matches"][0]["evidence_claim_ids"] = []
        self.assertIn(
            "benötigt Evidence",
            validate_match_matrix(valid, candidate, "cover_letter")[0],
        )

    def test_malformed_match_matrix_returns_structured_errors(self) -> None:
        candidate = load_yaml(FIXTURES / "valid-candidate.yaml")
        self.assertEqual(
            validate_match_matrix({"matches": None}, candidate, "cover_letter"),
            ["matches: expected a list"],
        )
        errors = validate_match_matrix(
            {
                "matches": [
                    None,
                    {
                        "requirement_id": "req-kafka",
                        "classification": "gap",
                        "evidence_claim_ids": "claim-rabbitmq",
                    },
                ]
            },
            candidate,
            "cover_letter",
        )
        self.assertIn("matches[0]: expected a mapping", errors)
        self.assertIn(
            "matches[1]: evidence_claim_ids muss eine String-Liste sein", errors
        )

    def test_validate_match_cli_returns_json_without_output_argument(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            matrix_path = Path(directory) / "matrix.yaml"
            matrix_path.write_text(
                yaml.safe_dump(
                    {
                        "matches": [
                            {
                                "requirement_id": "req-kafka",
                                "classification": "transferable_match",
                                "evidence_claim_ids": ["claim-rabbitmq"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "match_contract.py"),
                    "validate-match",
                    "--matrix",
                    str(matrix_path),
                    "--candidate",
                    str(FIXTURES / "valid-candidate.yaml"),
                    "--output-type",
                    "cover_letter",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout), {"valid": True, "errors": []})


class IterationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manifest = load_yaml(ROOT / "assets" / "iteration.template.yaml")

    def test_standard_iteration_passes(self) -> None:
        self.assertEqual(validate_iteration(self.manifest), [])

    def test_wrong_role_order_fails(self) -> None:
        broken = copy.deepcopy(self.manifest)
        broken["passes"][1]["role"] = "recruiter_style_reviewer"
        self.assertTrue(
            any("role order" in item for item in validate_iteration(broken))
        )

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
        self.assertTrue(
            any("unresolved" in item for item in validate_iteration(broken))
        )

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
        self.assertTrue(
            any("disposition" in item for item in validate_iteration(broken))
        )

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
            issues = check_languagetool(
                "RabbitMQ", "de-DE", "http://localhost:8010/v2/check"
            )
            allowed = check_languagetool(
                "RabbitMQ",
                "de-DE",
                "http://localhost:8010/v2/check",
                allowlist={"rabbitmq"},
            )
        self.assertEqual(len(issues), 1)
        self.assertEqual(allowed, [])

    def test_hunspell_uses_dictionary_and_allowlist(self) -> None:
        result = SimpleNamespace(
            returncode=0, stdout="Fehlerwort\nRabbitMQ\n", stderr=""
        )
        with (
            patch("shutil.which", return_value="/usr/bin/hunspell"),
            patch("subprocess.run", return_value=result) as run,
        ):
            issues = check_hunspell("RabbitMQ Fehlerwort", "de_DE", {"rabbitmq"})
        self.assertEqual([issue["text"] for issue in issues], ["Fehlerwort"])
        self.assertEqual(
            run.call_args.args[0], ["/usr/bin/hunspell", "-d", "de_DE", "-l"]
        )

    def test_markdown_and_evidence_are_removed(self) -> None:
        text = plain_text("# Titel\n- RabbitMQ <!-- evidence: claim-rabbitmq -->")
        self.assertEqual(text, "Titel\nRabbitMQ ")

    def test_html_markup_and_non_visible_head_content_are_removed(self) -> None:
        text = plain_text("""<!doctype html><html><head><title>Intern</title>
            <style>.secret { content: 'nicht sichtbar'; }</style></head>
            <body><h1>Lebenslauf</h1><p>Testautomatisierung &amp; Qualität</p></body></html>""")
        self.assertEqual(text, "Lebenslauf\n\nTestautomatisierung & Qualität")


if __name__ == "__main__":
    unittest.main()
