from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import yaml

from scripts import cv_import_contract as cv_contract
from scripts.cv_import_contract import (
    ImportContractError,
    adopt_confirmed,
    apply_ai_structure_request,
    capabilities,
    capture_profile_snapshot,
    extend_user_facts,
    extract_cv,
    list_adoptions,
    list_profile_snapshots,
    materialize_ai_structure_request,
    normalize_extracted_envelope,
    proposal_cas_sha256,
    recovery_status,
    restore_profile_snapshot,
    revoke_claims,
    validate_ai_structure_request,
    validate_proposal,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def office_archive(parts: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in parts.items():
            archive.writestr(name, content)
    return output.getvalue()


def ai_binding(proposal: dict[str, Any]) -> dict[str, str]:
    return {
        "source_id": proposal["source"]["id"],
        "source_sha256": proposal["source"]["sha256"],
        "text_sha256": proposal["extraction"]["text_sha256"],
        "base_proposal_sha256": proposal_cas_sha256(proposal),
    }


def ai_anchor(
    proposal: dict[str, Any], line_number: int, quote: str, occurrence: int = 0
) -> dict[str, Any]:
    line = proposal["extraction"]["line_manifest"][line_number - 1]["text"]
    start = -1
    search_from = 0
    for _ in range(occurrence + 1):
        start = line.index(quote, search_from)
        search_from = start + 1
    return {
        "line_start": line_number,
        "line_end": line_number,
        "char_start": start,
        "char_end": start + len(quote),
        "quote": quote,
    }


def ai_field(
    proposal: dict[str, Any],
    line_number: int | None,
    value: str | None,
    *,
    confidence: float = 0.9,
    occurrence: int = 0,
    alternatives: list[dict[str, Any]] | None = None,
    questions: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "value": value,
        "source_anchor": (
            ai_anchor(proposal, line_number, value, occurrence)
            if value is not None and line_number is not None
            else None
        ),
        "confidence": confidence if value is not None else 0,
        "alternatives": alternatives or [],
        "questions": questions
        or (["Welcher Wert ist korrekt?"] if value is None else []),
        "status": "unverified",
    }


def ai_alternative(
    proposal: dict[str, Any], line_number: int, value: str, confidence: float
) -> dict[str, Any]:
    return {
        "value": value,
        "source_anchor": ai_anchor(proposal, line_number, value),
        "confidence": confidence,
    }


def complex_ai_proposal(base: dict[str, Any]) -> dict[str, Any]:
    return {
        "contract": "ai-cv-structure-proposal",
        "contract_version": "1.0",
        "status": "unverified",
        "binding": ai_binding(base),
        "sections": [
            {
                "kind": "employment",
                "heading": ai_field(base, 2, "Beruflicher Werdegang", confidence=0.99),
                "status": "unverified",
            },
            {
                "kind": "education",
                "heading": ai_field(base, 8, "Aus- und Weiterbildung", confidence=0.99),
                "status": "unverified",
            },
        ],
        "employment": [
            {
                "employer": ai_field(base, 3, "Beispiel Systeme GmbH", confidence=0.97),
                "role": ai_field(
                    base,
                    3,
                    "Senior Softwareentwickler",
                    confidence=0.88,
                    alternatives=[ai_alternative(base, 3, "Softwareentwickler", 0.63)],
                    questions=[
                        "Ist der Senior-Titel die offizielle Rollenbezeichnung?"
                    ],
                ),
                "start_date": ai_field(base, 3, "03/2021", confidence=0.99),
                "end_date": ai_field(base, 3, "heute", confidence=0.99),
                "location": ai_field(base, 3, "Berlin", confidence=0.95),
                "details": [
                    ai_field(
                        base,
                        4,
                        "Modernisierte interne Prüfläufe für höhere Qualität.",
                        confidence=0.92,
                    )
                ],
                "status": "unverified",
            }
        ],
        "education": [
            {
                "institution": ai_field(
                    base, 9, "Hochschule Beispielstadt", confidence=0.96
                ),
                "qualification": ai_field(
                    base, 9, "Bachelor Informatik", confidence=0.94
                ),
                "start_date": ai_field(base, 9, "10/2013", confidence=0.98),
                "end_date": ai_field(base, 9, "09/2017", confidence=0.98),
                "location": ai_field(
                    base, 9, "Beispielstadt", confidence=0.8, occurrence=1
                ),
                "details": [],
                "status": "unverified",
            }
        ],
        "projects": [
            {
                "name": ai_field(base, 11, "Prüfportal", confidence=0.97),
                "role": ai_field(
                    base,
                    None,
                    None,
                    questions=["Welche Rolle hatte die Person im Prüfportal?"],
                ),
                "start_date": ai_field(base, 11, "2022", confidence=0.96),
                "end_date": ai_field(base, 11, "2023", confidence=0.96),
                "details": [
                    ai_field(
                        base, 12, "Technologien: TypeScript, Angular", confidence=0.75
                    )
                ],
                "technologies": [
                    ai_field(base, 12, "TypeScript", confidence=0.99),
                    ai_field(base, 12, "Angular", confidence=0.99),
                ],
                "status": "unverified",
            }
        ],
        "skills": [
            ai_field(base, 14, "Python", confidence=0.99),
            ai_field(base, 14, "PostgreSQL", confidence=0.99),
        ],
        "languages": [
            {
                "language": ai_field(base, 16, "Deutsch", confidence=0.99),
                "level": ai_field(base, 16, "C2", confidence=0.9),
                "status": "unverified",
            },
            {
                "language": ai_field(base, 16, "Englisch", confidence=0.99),
                "level": ai_field(base, 16, "B2", confidence=0.9),
                "status": "unverified",
            },
        ],
    }


def ai_validation_request(
    base: dict[str, Any], ai_proposal: dict[str, Any]
) -> dict[str, Any]:
    return {
        "contract": "ai-cv-structure-validation-request",
        "contract_version": "1.0",
        "base_proposal": base,
        "expected_proposal_sha256": proposal_cas_sha256(base),
        "ai_proposal": ai_proposal,
    }


def ai_materialization_request(
    base: dict[str, Any], ai_proposal: dict[str, Any]
) -> dict[str, Any]:
    return {
        "contract": "ai-cv-structure-materialization-request",
        "contract_version": "1.0",
        "base_proposal": base,
        "expected_proposal_sha256": proposal_cas_sha256(base),
        "ai_proposal": ai_proposal,
    }


class CvImportContractTests(unittest.TestCase):
    def test_capabilities_are_narrow_versioned_and_offline(self) -> None:
        result = capabilities()
        self.assertEqual(result["contract"], "cv-import-proposal")
        self.assertEqual(result["contract_version"], "1.0")
        self.assertFalse(result["network_access"])
        self.assertFalse(result["macros_allowed"])
        self.assertEqual(result["claim_status"], "unverified")
        self.assertEqual(
            result["ai_structuring"]["contract"], "ai-cv-structure-proposal"
        )
        self.assertTrue(result["ai_structuring"]["line_manifest_private"])
        self.assertIn("validate-ai-structure", result["commands"])
        self.assertIn("apply-ai-structure", result["commands"])
        self.assertIn("materialize-ai-structure", result["commands"])
        self.assertEqual(
            result["ai_structuring"]["materialization_request_contract"],
            "ai-cv-structure-materialization-request",
        )
        self.assertEqual(
            result["ai_structuring"]["materialization_request_schema"],
            "contracts/v1/ai-cv-structure-materialization-request.schema.json",
        )
        self.assertEqual(
            result["ai_structuring"]["materialization_mode"],
            "replace_recognition_version",
        )
        self.assertEqual(
            result["ai_structuring"]["materialization_output_contract"],
            "cv-import-proposal",
        )
        self.assertEqual(
            result["ai_structuring"]["materialization_output_contract_version"],
            "1.0",
        )
        self.assertEqual(
            result["ai_structuring"]["preserved_deterministic_scopes"],
            ["profile", "certifications"],
        )

    def test_html_import_is_deterministic_structured_and_unverified(self) -> None:
        first = extract_cv(FIXTURES / "synthetic-cv.html")
        second = extract_cv(FIXTURES / "synthetic-cv.html")
        self.assertEqual(first, second)
        self.assertEqual(first["state"], "needs_user_confirmation")
        self.assertFalse(first["publishable"])
        experience = first["proposal"]["experience"][0]
        self.assertEqual(experience["role"], "Software Engineer")
        self.assertEqual(experience["company"], "Mustertechnik GmbH")
        self.assertEqual(experience["start_date"], "2021-02")
        self.assertEqual(experience["end_date"], "present")
        self.assertTrue(
            all(item["status"] == "unverified" for item in first["proposal"]["claims"])
        )
        self.assertEqual(validate_proposal(first), [])

    def test_html_scripts_are_never_extracted_or_executed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cv.html"
            path.write_text(
                "<!doctype html><html><body><p>Visible</p><script>fetch('https://invalid.test');Hidden Fact</script></body></html>",
                encoding="utf-8",
            )
            result = extract_cv(path)
        statements = [item["statement"] for item in result["proposal"]["claims"]]
        self.assertIn("Visible", " ".join(statements))
        self.assertNotIn("Hidden Fact", " ".join(statements))

    def test_docx_is_extracted_from_document_xml_without_office(self) -> None:
        data = office_archive(
            {
                "[Content_Types].xml": b"<Types/>",
                "word/document.xml": (
                    b'<w:document xmlns:w="urn:w"><w:body>'
                    b"<w:p><w:r><w:t>Skills</w:t></w:r></w:p>"
                    b"<w:p><w:r><w:t>Python, SQL</w:t></w:r></w:p>"
                    b"</w:body></w:document>"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cv.docx"
            path.write_bytes(data)
            result = extract_cv(path)
        self.assertEqual(
            [item["name"] for item in result["proposal"]["skills"]], ["Python", "SQL"]
        )

    def test_odt_is_extracted_from_content_xml_without_libreoffice(self) -> None:
        data = office_archive(
            {
                "mimetype": b"application/vnd.oasis.opendocument.text",
                "content.xml": (
                    b'<office:document xmlns:office="urn:o" xmlns:text="urn:t">'
                    b"<text:h>Languages</text:h><text:p>Deutsch, Englisch</text:p>"
                    b"</office:document>"
                ),
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cv.odt"
            path.write_bytes(data)
            result = extract_cv(path)
        self.assertEqual(
            [item["language"] for item in result["proposal"]["languages"]],
            ["Deutsch", "Englisch"],
        )

    def test_office_macros_and_external_relationships_are_rejected(self) -> None:
        macro = office_archive(
            {"word/document.xml": b"<document/>", "word/vbaProject.bin": b"active"}
        )
        external = office_archive(
            {
                "word/document.xml": b"<document><p>Text</p></document>",
                "word/_rels/document.xml.rels": (
                    b'<Relationships><Relationship TargetMode="External" Target="https://invalid.test"/></Relationships>'
                ),
            }
        )
        for name, data, expected in (
            ("macro.docx", macro, "active_content"),
            ("external.docx", external, "external_relationship"),
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / name
                path.write_bytes(data)
                with self.assertRaises(ImportContractError) as caught:
                    extract_cv(path)
                self.assertEqual(caught.exception.code, expected)

    def test_pdf_uses_fixed_local_command_and_preserves_no_path_in_proposal(
        self,
    ) -> None:
        completed = subprocess.CompletedProcess([], 0, b"Skills\nPython, SQL\n", b"")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "private-name.pdf"
            path.write_bytes(b"%PDF-1.7\nsynthetic")
            with (
                patch(
                    "scripts.cv_import_contract.shutil.which",
                    return_value="/usr/bin/pdftotext",
                ),
                patch(
                    "scripts.cv_import_contract.subprocess.run", return_value=completed
                ) as run,
            ):
                result = extract_cv(path)
        command = run.call_args.args[0]
        self.assertEqual(
            command[:4], ["/usr/bin/pdftotext", "-enc", "UTF-8", "-nopgbrk"]
        )
        self.assertNotIn("private-name", json.dumps(result))
        self.assertEqual(result["proposal"]["skills"][0]["status"], "unverified")

    def test_pdf_active_content_and_extension_spoofing_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            active = Path(directory) / "active.pdf"
            active.write_bytes(b"%PDF-1.7\n/JavaScript")
            spoofed = Path(directory) / "spoofed.pdf"
            spoofed.write_bytes(b"not a pdf")
            for path, code in ((active, "active_content"), (spoofed, "type_mismatch")):
                with (
                    self.subTest(path=path.name),
                    self.assertRaises(ImportContractError) as caught,
                ):
                    extract_cv(path)
                self.assertEqual(caught.exception.code, code)

    def test_validator_rejects_status_upgrade_without_confirmation(self) -> None:
        result = extract_cv(FIXTURES / "synthetic-cv.html")
        result["proposal"]["claims"][0]["status"] = "verified"
        self.assertTrue(
            any("must be unverified" in item for item in validate_proposal(result))
        )

    def test_validator_rejects_empty_atomic_fact_or_claim_content(self) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        for collection, field in (("facts", "value"), ("claims", "statement")):
            with self.subTest(collection=collection, field=field):
                invalid = copy.deepcopy(proposal)
                invalid["proposal"][collection][0][field] = "   "
                self.assertTrue(
                    any(
                        "expected a non-empty string" in item
                        for item in validate_proposal(invalid)
                    )
                )

    def test_root_extracted_envelope_normalizes_atomic_field_facts(self) -> None:
        text = "Experience\n2020-01 - present: Engineer | Synthetic GmbH\nBuilt tests"
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "b" * 64,
                "byte_size": 1234,
                "media_type": "application/pdf",
            },
            "extraction": {
                "engine": "root-pdf-parse",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "warnings": [{"code": "layout", "detail": "Synthetic warning"}],
            },
        }
        result = normalize_extracted_envelope(envelope)
        record = result["proposal"]["experience"][0]
        self.assertEqual(
            set(record["field_fact_ids"]), {"role", "company", "start_date", "end_date"}
        )
        self.assertEqual(len(set(record["field_fact_ids"].values())), 4)
        self.assertTrue(
            all(
                item["source_anchor"]["source_sha256"] == "b" * 64
                for item in result["proposal"]["facts"]
            )
        )
        self.assertEqual(validate_proposal(result), [])

    def test_role_only_employment_stays_incomplete_without_empty_facts(self) -> None:
        text = "Experience\n2021-02 - present: Software Engineer"
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "c" * 64,
                "byte_size": len(text.encode()),
                "media_type": "text/html",
            },
            "extraction": {
                "engine": "root-html-passive",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "warnings": [],
            },
        }

        result = normalize_extracted_envelope(envelope)
        record = result["proposal"]["experience"][0]
        facts = result["proposal"]["facts"]
        claims = result["proposal"]["claims"]

        self.assertEqual(record["role"], "Software Engineer")
        self.assertEqual(record["company"], "")
        self.assertEqual(
            set(record["field_fact_ids"]), {"role", "start_date", "end_date"}
        )
        self.assertNotIn("company", record["field_claim_ids"])
        self.assertTrue(all(item["value"].strip() for item in facts))
        self.assertTrue(all(item["statement"].strip() for item in claims))
        self.assertTrue(all(item["status"] == "unverified" for item in facts))
        self.assertTrue(all(item["status"] == "unverified" for item in claims))
        self.assertEqual(validate_proposal(result), [])

    def test_empty_role_employment_stays_incomplete_without_empty_facts(self) -> None:
        text = "Experience\n2021-02 - present: , Synthetic GmbH"
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "e" * 64,
                "byte_size": len(text.encode()),
                "media_type": "text/html",
            },
            "extraction": {
                "engine": "root-html-passive",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "warnings": [],
            },
        }

        self.assertEqual(
            cv_contract._body_parts(", Synthetic GmbH"),
            ("", "Synthetic GmbH", ""),
        )
        result = normalize_extracted_envelope(envelope)
        record = result["proposal"]["experience"][0]
        facts = result["proposal"]["facts"]
        claims = result["proposal"]["claims"]

        self.assertEqual(record["role"], "")
        self.assertEqual(record["company"], "Synthetic GmbH")
        self.assertEqual(
            set(record["field_fact_ids"]), {"company", "start_date", "end_date"}
        )
        self.assertNotIn("role", record["field_claim_ids"])
        self.assertTrue(all(item["value"].strip() for item in facts))
        self.assertTrue(all(item["statement"].strip() for item in claims))
        self.assertTrue(all(item["status"] == "unverified" for item in facts))
        self.assertTrue(all(item["status"] == "unverified" for item in claims))
        self.assertEqual(validate_proposal(result), [])

    def test_pure_bullet_after_employment_is_ignored_without_empty_detail(self) -> None:
        text = "Experience\n2021-02 - present: Software Engineer | Synthetic GmbH\n-"
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "f" * 64,
                "byte_size": len(text.encode()),
                "media_type": "text/html",
            },
            "extraction": {
                "engine": "root-html-passive",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "warnings": [],
            },
        }

        result = normalize_extracted_envelope(envelope)
        record = result["proposal"]["experience"][0]
        facts = result["proposal"]["facts"]
        claims = result["proposal"]["claims"]

        self.assertEqual(record["details"], [])
        self.assertTrue(all(item["value"].strip() for item in facts))
        self.assertTrue(all(item["statement"].strip() for item in claims))
        self.assertEqual(validate_proposal(result), [])

    def test_duplicate_atomic_records_are_deduplicated_without_status_upgrade(
        self,
    ) -> None:
        text = (
            "Experience\n2020-01 - present: Engineer | Synthetic GmbH\nBuilt tests\n"
            "2020-01 - present: Engineer | Synthetic GmbH\nBuilt tests\n"
            "Skills\nTypeScript, TypeScript\n"
            "Languages\nDeutsch, deutsch\n"
            "Projects\nSynthetic Portal\nSynthetic Portal"
        )
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "d" * 64,
                "byte_size": len(text.encode()),
                "media_type": "text/html",
            },
            "extraction": {
                "engine": "root-html-passive",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "warnings": [],
            },
        }

        first = normalize_extracted_envelope(envelope)
        second = normalize_extracted_envelope(envelope)

        self.assertEqual(first, second)
        self.assertEqual(
            [item["name"] for item in first["proposal"]["skills"]], ["TypeScript"]
        )
        self.assertEqual(
            [item["language"] for item in first["proposal"]["languages"]], ["Deutsch"]
        )
        self.assertEqual(
            [item["name"] for item in first["proposal"]["projects"]],
            ["Synthetic Portal"],
        )
        self.assertEqual(len(first["proposal"]["experience"]), 1)
        self.assertEqual(
            [item["text"] for item in first["proposal"]["experience"][0]["details"]],
            ["Built tests"],
        )
        fact_ids = [item["id"] for item in first["proposal"]["facts"]]
        claim_ids = [item["id"] for item in first["proposal"]["claims"]]
        self.assertEqual(len(fact_ids), len(set(fact_ids)))
        self.assertEqual(len(claim_ids), len(set(claim_ids)))
        self.assertTrue(
            all(item["status"] == "unverified" for item in first["proposal"]["facts"])
        )
        self.assertTrue(
            all(item["status"] == "unverified" for item in first["proposal"]["claims"])
        )
        self.assertEqual(validate_proposal(first), [])

    def test_envelope_digest_mismatch_is_rejected(self) -> None:
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {"sha256": "b" * 64, "byte_size": 1, "media_type": "text/html"},
            "extraction": {
                "engine": "root",
                "text": "Text",
                "text_sha256": "0" * 64,
                "warnings": [],
            },
        }
        with self.assertRaises(ImportContractError) as caught:
            normalize_extracted_envelope(envelope)
        self.assertEqual(caught.exception.code, "digest_mismatch")

    def test_adoption_requires_each_employment_field_and_uses_candidate_cas(
        self,
    ) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        employment = proposal["proposal"]["experience"][0]
        all_core_facts = list(employment["field_fact_ids"].values())
        decisions = [
            {
                "fact_id": fact_id,
                "decision": "confirm",
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            }
            for fact_id in all_core_facts[:-1]
        ] + [{"fact_id": all_core_facts[-1], "decision": "pending"}]
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.yaml"
            candidate_path.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            partial = adopt_confirmed(proposal, candidate_path, decisions, digest)
            candidate = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
            self.assertEqual(partial["adopted_record_ids"], [])
            self.assertEqual(len(partial["adopted_fact_ids"]), 3)
            self.assertFalse(
                any(item["id"] == employment["id"] for item in candidate["experience"])
            )

            # Use a fresh profile because confirmed atomic claims were intentionally adopted above.
            candidate_path.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            complete = adopt_confirmed(
                proposal,
                candidate_path,
                [
                    {
                        "fact_id": fact_id,
                        "decision": "confirm",
                        "explicitly_confirmed": True,
                        "confirmation_origin": "explicit_local_user_action",
                    }
                    for fact_id in all_core_facts
                ],
                digest,
            )
            candidate = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
        self.assertEqual(complete["adopted_record_ids"], [employment["id"]])
        adopted = next(
            item for item in candidate["experience"] if item["id"] == employment["id"]
        )
        self.assertEqual(adopted["status"], "user_confirmed")
        self.assertEqual(len(adopted["claim_ids"]), 4)

    def test_normalize_cli_supports_private_stdin_stdout_transport(self) -> None:
        text = "Skills\nTypeScript, TypeScript"
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "c" * 64,
                "byte_size": 12,
                "media_type": "application/pdf",
            },
            "extraction": {
                "engine": "root-pdf-parse",
                "text": text,
                "text_sha256": hashlib.sha256(text.encode()).hexdigest(),
                "warnings": [],
            },
        }
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "normalize-extracted",
                "--extracted-envelope",
                "-",
                "--output",
                "-",
            ],
            cwd=ROOT,
            input=json.dumps(envelope),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(process.returncode, 0, process.stderr)
        proposal = json.loads(process.stdout)
        self.assertEqual(
            [item["name"] for item in proposal["proposal"]["skills"]], ["TypeScript"]
        )
        self.assertEqual(validate_proposal(proposal), [])

    def test_normalize_cli_uses_utf8_bytes_under_legacy_stdio_encoding(self) -> None:
        text = (
            "Skills\nGrößenprüfung, Qualität\n"
            "Experience\n2021-02 - present: Entwickler | Beispiel GmbH\n"
            "Überführte Abläufe"
        )
        encoded_text = text.encode("utf-8")
        envelope = {
            "contract": "extracted-cv-text",
            "contract_version": "1.0",
            "source": {
                "sha256": "e" * 64,
                "byte_size": len(encoded_text),
                "media_type": "text/html",
            },
            "extraction": {
                "engine": "root-html-passive",
                "text": text,
                "text_sha256": hashlib.sha256(encoded_text).hexdigest(),
                "warnings": [],
            },
        }
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        environment["PYTHONUTF8"] = "0"
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "normalize-extracted",
                "--extracted-envelope",
                "-",
                "--output",
                "-",
            ],
            cwd=ROOT,
            input=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=environment,
            check=False,
        )

        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace"),
        )
        proposal = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(
            [item["name"] for item in proposal["proposal"]["skills"]],
            ["Größenprüfung", "Qualität"],
        )
        self.assertIn(
            "Überführte Abläufe",
            [item["value"] for item in proposal["proposal"]["facts"]],
        )
        self.assertEqual(validate_proposal(proposal), [])

    def test_complex_german_cv_has_private_manifest_and_safe_deterministic_recognition(
        self,
    ) -> None:
        result = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        manifest = result["extraction"]["line_manifest"]
        experience = result["proposal"]["experience"]

        self.assertEqual([item["line"] for item in manifest], list(range(1, 17)))
        self.assertTrue(
            all(
                item["sha256"]
                == hashlib.sha256(item["text"].encode("utf-8")).hexdigest()
                for item in manifest
            )
        )
        self.assertEqual(
            [
                (
                    item["role"],
                    item["company"],
                    item["location"],
                    item["start_date"],
                    item["end_date"],
                )
                for item in experience
            ],
            [
                (
                    "Senior Softwareentwickler",
                    "Beispiel Systeme GmbH",
                    "Berlin",
                    "2021-03",
                    "present",
                ),
                (
                    "Softwareentwickler",
                    "Mustertechnik AG",
                    "Hamburg",
                    "2018-01",
                    "2021-02",
                ),
            ],
        )
        self.assertEqual(validate_proposal(result), [])

    def test_ai_structure_validation_is_strict_source_bound_and_deterministic(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        request = ai_validation_request(base, provider)

        first = validate_ai_structure_request(request)
        second = validate_ai_structure_request(request)
        suggestion_ids = [item["id"] for item in first["suggestions"]]
        role = next(
            item
            for item in first["suggestions"]
            if item["path"] == "employment[0].role"
        )
        unknown_role = next(
            item for item in first["suggestions"] if item["path"] == "projects[0].role"
        )

        self.assertEqual(first, second)
        self.assertEqual(first["contract"], "validated-ai-cv-structure-proposal")
        self.assertEqual(first["status"], "unverified")
        self.assertEqual(len(suggestion_ids), len(set(suggestion_ids)))
        self.assertRegex(role["id"], r"^suggestion-[a-f0-9]{16}$")
        self.assertRegex(role["alternatives"][0]["id"], r"^alternative-[a-f0-9]{16}$")
        self.assertEqual(
            role["questions"],
            ["Ist der Senior-Titel die offizielle Rollenbezeichnung?"],
        )
        self.assertIsNone(unknown_role["value"])
        self.assertTrue(unknown_role["questions"])
        self.assertTrue(
            all(item["status"] == "unverified" for item in first["suggestions"])
        )

    def test_ai_anchor_canonicalization_repairs_one_unique_off_by_one_span(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        raw_anchor = provider["employment"][0]["role"]["source_anchor"]
        expected_anchor = copy.deepcopy(raw_anchor)
        raw_anchor["char_end"] += 1
        unchanged_provider = copy.deepcopy(provider)

        validated = validate_ai_structure_request(ai_validation_request(base, provider))
        role = next(
            item
            for item in validated["suggestions"]
            if item["path"] == "employment[0].role"
        )

        self.assertEqual(role["value"], role["source_anchor"]["quote"])
        self.assertEqual(role["source_anchor"], expected_anchor)
        self.assertEqual(provider, unchanged_provider)
        self.assertEqual(role["status"], "unverified")

        materialized = materialize_ai_structure_request(
            ai_materialization_request(base, provider)
        )
        materialized_role = next(
            item
            for item in materialized["proposal"]["facts"]
            if item["value"] == role["value"]
            and item["source_anchor"].get("recognition_method") == "ai_assisted"
        )
        self.assertEqual(
            materialized_role["source_anchor"]["char_start"],
            expected_anchor["char_start"],
        )
        self.assertEqual(
            materialized_role["source_anchor"]["char_end"],
            expected_anchor["char_end"],
        )
        self.assertEqual(materialized_role["status"], "unverified")
        self.assertEqual(provider, unchanged_provider)

    def test_ai_anchor_canonicalization_keeps_an_exact_span_unchanged(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        expected_anchor = copy.deepcopy(
            provider["employment"][0]["role"]["source_anchor"]
        )

        validated = validate_ai_structure_request(ai_validation_request(base, provider))
        role = next(
            item
            for item in validated["suggestions"]
            if item["path"] == "employment[0].role"
        )

        self.assertEqual(role["source_anchor"], expected_anchor)

    def test_ai_anchor_canonicalization_rejects_a_non_unique_quote(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        location = provider["education"][0]["location"]
        location["source_anchor"]["char_start"] = 0
        location["source_anchor"]["char_end"] = 1

        with self.assertRaises(ImportContractError) as caught:
            validate_ai_structure_request(ai_validation_request(base, provider))

        self.assertEqual(caught.exception.code, "out_of_source_value")

    def test_ai_anchor_canonicalization_rejects_a_missing_quote(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        employer = provider["employment"][0]["employer"]
        employer["value"] = "Nicht vorhandene Synthetik GmbH"
        employer["source_anchor"] = {
            **employer["source_anchor"],
            "char_start": 0,
            "char_end": 1,
            "quote": employer["value"],
        }

        with self.assertRaises(ImportContractError) as caught:
            validate_ai_structure_request(ai_validation_request(base, provider))

        self.assertEqual(caught.exception.code, "out_of_source_value")

    def test_ai_anchor_canonicalization_rejects_value_quote_mismatch(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        provider["employment"][0]["employer"]["value"] = "Abweichende Synthetik GmbH"

        with self.assertRaises(ImportContractError) as caught:
            validate_ai_structure_request(ai_validation_request(base, provider))

        self.assertEqual(caught.exception.code, "out_of_source_value")

    def test_ai_structure_rejects_unknown_keys_binding_spans_and_out_of_source_values(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        mutations = []

        unknown = complex_ai_proposal(base)
        unknown["provider_note"] = "must be rejected"
        mutations.append((unknown, "invalid_ai_structure"))

        stale = complex_ai_proposal(base)
        stale["binding"]["text_sha256"] = "0" * 64
        mutations.append((stale, "ai_binding_mismatch"))

        outside = complex_ai_proposal(base)
        outside["employment"][0]["employer"]["source_anchor"]["line_start"] = 999
        outside["employment"][0]["employer"]["source_anchor"]["line_end"] = 999
        mutations.append((outside, "unsupported_source_span"))

        invented = complex_ai_proposal(base)
        invented["employment"][0]["employer"]["value"] = "Nicht in der Quelle GmbH"
        mutations.append((invented, "out_of_source_value"))

        upgraded = complex_ai_proposal(base)
        upgraded["employment"][0]["role"]["status"] = "verified"
        mutations.append((upgraded, "invalid_ai_status"))

        conflicting = complex_ai_proposal(base)
        conflicting["employment"][0]["start_date"] = ai_field(
            base, 11, "2023", confidence=0.8
        )
        conflicting["employment"][0]["end_date"] = ai_field(
            base, 11, "2022", confidence=0.8
        )
        mutations.append((conflicting, "conflicting_ai_structure"))

        for provider, expected_code in mutations:
            with (
                self.subTest(expected_code=expected_code),
                self.assertRaises(ImportContractError) as caught,
            ):
                validate_ai_structure_request(ai_validation_request(base, provider))
            self.assertEqual(caught.exception.code, expected_code)

        request = ai_validation_request(base, complex_ai_proposal(base))
        request["expected_proposal_sha256"] = "f" * 64
        with self.assertRaises(ImportContractError) as caught:
            validate_ai_structure_request(request)
        self.assertEqual(caught.exception.code, "cas_mismatch")

    def test_ai_apply_uses_only_selected_primary_or_alternative_as_unverified_facts(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        unchanged = copy.deepcopy(base)
        provider = complex_ai_proposal(base)
        validation = validate_ai_structure_request(
            ai_validation_request(base, provider)
        )
        by_path = {item["path"]: item for item in validation["suggestions"]}
        selected_paths = [
            "employment[0].employer",
            "employment[0].role",
            "employment[0].start_date",
            "employment[0].end_date",
            "employment[0].location",
            "employment[0].details[0]",
            "skills[0]",
        ]
        role = by_path["employment[0].role"]
        selections = [
            {
                "suggestion_id": by_path[path]["id"],
                "alternative_id": (
                    role["alternatives"][0]["id"]
                    if path == "employment[0].role"
                    else None
                ),
            }
            for path in selected_paths
        ]
        request = {
            "contract": "ai-cv-structure-apply-request",
            "contract_version": "1.0",
            "base_proposal": base,
            "expected_proposal_sha256": proposal_cas_sha256(base),
            "ai_proposal": provider,
            "selections": selections,
        }

        merged, summary = apply_ai_structure_request(request)
        ai_facts = [
            item
            for item in merged["proposal"]["facts"]
            if item["source_anchor"].get("recognition_method") == "ai_assisted"
        ]
        ai_records = [
            item
            for item in merged["proposal"]["experience"]
            if item["id"] == role["record_id"]
        ]

        self.assertEqual(base, unchanged)
        self.assertEqual(len(ai_facts), len(selected_paths))
        self.assertTrue(all(item["status"] == "unverified" for item in ai_facts))
        self.assertTrue(
            all(
                item["source_anchor"]["source_id"] == base["source"]["id"]
                for item in ai_facts
            )
        )
        self.assertTrue(
            all(item["source_anchor"]["suggestion_id"] for item in ai_facts)
        )
        self.assertEqual(
            next(item for item in ai_facts if item["field"] == "role")["value"],
            "Softwareentwickler",
        )
        self.assertEqual(
            next(item for item in ai_facts if item["field"] == "start_date")["value"],
            "2021-03",
        )
        self.assertEqual(len(ai_records), 1)
        self.assertEqual(ai_records[0]["role"], "Softwareentwickler")
        self.assertFalse(
            any(item["value"] == "PostgreSQL" for item in ai_facts),
            "Unselected suggestions must not become facts",
        )
        self.assertEqual(summary["status"], "ai_structure_applied_unverified")
        self.assertTrue(summary["requires_confirmation"])
        self.assertEqual(validate_proposal(merged), [])

        tampered = copy.deepcopy(merged)
        tampered_fact = next(
            item
            for item in tampered["proposal"]["facts"]
            if item["source_anchor"].get("recognition_method") == "ai_assisted"
        )
        tampered_fact["source_anchor"]["quote"] = "Nicht der Quelltext"
        self.assertTrue(
            any(
                "AI source span is invalid" in error
                for error in validate_proposal(tampered)
            )
        )

    def test_ai_materialization_replaces_deterministic_recognition_with_primaries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "complex-with-certificate.html"
            source.write_text(
                (FIXTURES / "synthetic-complex-german-cv.html")
                .read_text(encoding="utf-8")
                .replace(
                    "</body>",
                    "<h2>Zertifikate</h2>"
                    "<p>2020 - 2021: Local Safety Certificate</p></body>",
                ),
                encoding="utf-8",
            )
            base = extract_cv(source)
        base["legacy_raw_facts"] = [{"text": f"raw-{index}"} for index in range(200)]
        base["extraction"]["legacy_raw_facts"] = copy.deepcopy(base["legacy_raw_facts"])

        provider = complex_ai_proposal(base)
        provider["projects"][0]["role"]["alternatives"] = [
            ai_alternative(base, 12, "TypeScript", 0.4)
        ]
        validated = validate_ai_structure_request(ai_validation_request(base, provider))
        expected_primaries = [
            item
            for item in validated["suggestions"]
            if item["mergeable"] and item["value"] is not None
        ]
        expected_ids = {item["id"] for item in expected_primaries}
        null_role = next(
            item
            for item in validated["suggestions"]
            if item["path"] == "projects[0].role"
        )
        section_ids = {
            item["id"] for item in validated["suggestions"] if not item["mergeable"]
        }

        request = ai_materialization_request(base, provider)
        first = materialize_ai_structure_request(request)
        second = materialize_ai_structure_request(request)
        proposal = first["proposal"]
        ai_facts = [
            item
            for item in proposal["facts"]
            if item["source_anchor"].get("recognition_method") == "ai_assisted"
        ]
        by_suggestion = {
            item["source_anchor"]["suggestion_id"]: item for item in ai_facts
        }
        profile_fact_ids = {
            item["fact_id"] for item in base["proposal"]["profile"]["facts"]
        }
        certification_fact_ids = {
            fact_id
            for record in base["proposal"]["certifications"]
            for fact_id in record["field_fact_ids"].values()
        }
        preserved_fact_ids = profile_fact_ids | certification_fact_ids
        preserved_claim_ids = {
            item["claim_id"] for item in base["proposal"]["profile"]["facts"]
        } | {
            claim_id
            for record in base["proposal"]["certifications"]
            for claim_id in record["claim_ids"]
        }
        removed_deterministic_ids = {
            item["id"] for item in base["proposal"]["facts"]
        } - preserved_fact_ids
        removed_deterministic_claim_ids = {
            item["id"] for item in base["proposal"]["claims"]
        } - preserved_claim_ids

        self.assertEqual(first, second)
        self.assertEqual(proposal_cas_sha256(first), proposal_cas_sha256(second))
        self.assertEqual(set(by_suggestion), expected_ids)
        self.assertFalse(
            removed_deterministic_ids & {item["id"] for item in proposal["facts"]}
        )
        self.assertFalse(
            removed_deterministic_claim_ids
            & {item["id"] for item in proposal["claims"]}
        )
        self.assertEqual(proposal["additional_facts"], [])
        self.assertNotIn("legacy_raw_facts", first)
        self.assertNotIn("legacy_raw_facts", first["extraction"])
        self.assertEqual(proposal["profile"], base["proposal"]["profile"])
        self.assertEqual(proposal["certifications"], base["proposal"]["certifications"])
        self.assertEqual(
            {
                item["id"]
                for item in proposal["facts"]
                if item["source_anchor"].get("recognition_method") != "ai_assisted"
            },
            preserved_fact_ids,
        )
        self.assertTrue(all(item["status"] == "unverified" for item in ai_facts))
        ai_claim_ids = {item["claim_id"] for item in ai_facts}
        self.assertEqual(
            {item["id"] for item in proposal["claims"]},
            preserved_claim_ids | ai_claim_ids,
        )
        self.assertTrue(
            all(item["source_anchor"]["alternative_id"] is None for item in ai_facts)
        )
        self.assertNotIn(null_role["id"], by_suggestion)
        self.assertFalse(section_ids & set(by_suggestion))
        self.assertEqual(
            by_suggestion[
                next(
                    item["id"]
                    for item in expected_primaries
                    if item["path"] == "employment[0].role"
                )
            ]["value"],
            "Senior Softwareentwickler",
        )

        expected_records = {
            collection: {
                item["record_id"]
                for item in expected_primaries
                if item["collection"] == collection
            }
            for collection in (
                "experience",
                "education",
                "projects",
                "skills",
                "languages",
            )
        }
        for collection, expected_record_ids in expected_records.items():
            self.assertEqual(
                {item["id"] for item in proposal[collection]}, expected_record_ids
            )
            self.assertTrue(
                all(
                    set(record["claim_ids"]) <= ai_claim_ids
                    for record in proposal[collection]
                )
            )
        project = proposal["projects"][0]
        self.assertNotIn("role", project)
        self.assertNotIn("role", project["field_fact_ids"])

        base_extraction = {
            key: copy.deepcopy(base["extraction"][key])
            for key in (
                "engine",
                "text_sha256",
                "line_count",
                "line_manifest",
                "warnings",
            )
        }
        base_extraction["conflicts"] = copy.deepcopy(validated["conflicts"])
        materialized_extraction = copy.deepcopy(first["extraction"])
        audit = materialized_extraction.pop("ai_structuring")
        self.assertEqual(materialized_extraction, base_extraction)
        self.assertEqual(len(audit), 1)
        self.assertEqual(audit[0]["mode"], "replace_recognition_version")
        self.assertEqual(audit[0]["binding"], ai_binding(base))
        self.assertEqual(audit[0]["applied_suggestion_ids"], sorted(expected_ids))
        self.assertEqual(validate_proposal(first), [])

        tampered_output = copy.deepcopy(first)
        tampered_output_fact = next(
            item
            for item in tampered_output["proposal"]["facts"]
            if item["source_anchor"].get("recognition_method") == "ai_assisted"
        )
        tampered_output_fact["source_anchor"]["quote"] = "Nicht die Quelle"
        self.assertTrue(
            any(
                "AI source span is invalid" in error
                for error in validate_proposal(tampered_output)
            )
        )

    def test_ai_materialization_replaces_deterministic_structure_conflicts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "deterministic-conflict.html"
            source.write_text(
                """<!doctype html>
<html lang="de"><body>
<h1>Konflikt-Testprofil</h1>
<a href="https://example.invalid/portfolio">Portfolio</a>
<h2>Berufserfahrung</h2>
<p>01/2020 - 12/2021: Entwickler | Beispiel GmbH | Berlin</p>
<p>01/2020 - 12/2021: Lead Entwickler | Beispiel GmbH | Berlin</p>
</body></html>
""",
                encoding="utf-8",
            )
            base = extract_cv(source)

        self.assertEqual(
            [item["code"] for item in base["extraction"]["conflicts"]],
            ["ambiguous_employment_role"],
        )
        self.assertEqual(
            [item["code"] for item in base["extraction"]["warnings"]],
            ["external_references_ignored"],
        )
        ai_line = next(
            item["line"]
            for item in base["extraction"]["line_manifest"]
            if "Lead Entwickler" in item["text"]
        )
        provider = {
            "contract": "ai-cv-structure-proposal",
            "contract_version": "1.0",
            "status": "unverified",
            "binding": ai_binding(base),
            "sections": [],
            "employment": [
                {
                    "employer": ai_field(base, ai_line, "Beispiel GmbH"),
                    "role": ai_field(base, ai_line, "Lead Entwickler"),
                    "start_date": ai_field(base, ai_line, "01/2020"),
                    "end_date": ai_field(base, ai_line, "12/2021"),
                    "location": ai_field(base, ai_line, "Berlin"),
                    "details": [],
                    "status": "unverified",
                }
            ],
            "education": [],
            "projects": [],
            "skills": [],
            "languages": [],
        }
        validated = validate_ai_structure_request(ai_validation_request(base, provider))
        expected_suggestions = {
            item["id"]
            for item in validated["suggestions"]
            if item["mergeable"] and item["value"] is not None
        }
        self.assertEqual(validated["conflicts"], [])

        materialized = materialize_ai_structure_request(
            ai_materialization_request(base, provider)
        )
        ai_facts = [
            item
            for item in materialized["proposal"]["facts"]
            if item["source_anchor"].get("recognition_method") == "ai_assisted"
        ]
        ai_claim_ids = {item["claim_id"] for item in ai_facts}
        ai_claims = [
            item
            for item in materialized["proposal"]["claims"]
            if item["id"] in ai_claim_ids
        ]

        self.assertEqual(materialized["extraction"]["conflicts"], [])
        self.assertEqual(
            materialized["extraction"]["warnings"], base["extraction"]["warnings"]
        )
        self.assertEqual(
            {item["source_anchor"]["suggestion_id"] for item in ai_facts},
            expected_suggestions,
        )
        self.assertEqual(len(ai_facts), 5)
        self.assertEqual(len(ai_claims), 5)
        self.assertTrue(all(item["status"] == "unverified" for item in ai_facts))
        self.assertTrue(all(item["status"] == "unverified" for item in ai_claims))
        self.assertFalse(materialized["publishable"])
        self.assertEqual(len(materialized["proposal"]["experience"]), 1)
        self.assertEqual(
            materialized["proposal"]["experience"][0]["role"], "Lead Entwickler"
        )
        self.assertEqual(validate_proposal(materialized), [])

    def test_ai_materialization_revalidates_closed_request_cas_and_provider(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)

        unknown = ai_materialization_request(base, provider)
        unknown["selection_policy"] = "trust-provider"
        with self.assertRaises(ImportContractError) as caught:
            materialize_ai_structure_request(unknown)
        self.assertEqual(caught.exception.code, "invalid_request")

        stale = ai_materialization_request(base, provider)
        stale["expected_proposal_sha256"] = "f" * 64
        with self.assertRaises(ImportContractError) as caught:
            materialize_ai_structure_request(stale)
        self.assertEqual(caught.exception.code, "cas_mismatch")

        tampered = complex_ai_proposal(base)
        tampered["employment"][0]["employer"]["source_anchor"]["quote"] = (
            "Nicht die Quelle"
        )
        with self.assertRaises(ImportContractError) as caught:
            materialize_ai_structure_request(ai_materialization_request(base, tampered))
        self.assertEqual(caught.exception.code, "out_of_source_value")

        empty = complex_ai_proposal(base)
        for collection in (
            "employment",
            "education",
            "projects",
            "skills",
            "languages",
        ):
            empty[collection] = []
        with self.assertRaises(ImportContractError) as caught:
            materialize_ai_structure_request(ai_materialization_request(base, empty))
        self.assertEqual(caught.exception.code, "ai_materialization_no_usable_facts")

    def test_ai_apply_rejects_unknown_duplicate_and_non_mergeable_selections(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        validation = validate_ai_structure_request(
            ai_validation_request(base, provider)
        )
        section = next(
            item for item in validation["suggestions"] if not item["mergeable"]
        )
        skill = next(
            item for item in validation["suggestions"] if item["path"] == "skills[0]"
        )

        for selections in (
            [{"suggestion_id": "suggestion-0000000000000000", "alternative_id": None}],
            [{"suggestion_id": section["id"], "alternative_id": None}],
            [
                {"suggestion_id": skill["id"], "alternative_id": None},
                {"suggestion_id": skill["id"], "alternative_id": None},
            ],
        ):
            request = {
                "contract": "ai-cv-structure-apply-request",
                "contract_version": "1.0",
                "base_proposal": base,
                "expected_proposal_sha256": proposal_cas_sha256(base),
                "ai_proposal": provider,
                "selections": selections,
            }
            with (
                self.subTest(selections=selections),
                self.assertRaises(ImportContractError) as caught,
            ):
                apply_ai_structure_request(request)
            self.assertEqual(caught.exception.code, "invalid_selection")

    def test_ai_facts_still_require_individual_confirmation_before_profile_adoption(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        validation = validate_ai_structure_request(
            ai_validation_request(base, provider)
        )
        by_path = {item["path"]: item for item in validation["suggestions"]}
        selected_paths = [
            "employment[0].employer",
            "employment[0].role",
            "employment[0].start_date",
            "employment[0].end_date",
            "employment[0].location",
            "employment[0].details[0]",
        ]
        merged, _summary = apply_ai_structure_request(
            {
                "contract": "ai-cv-structure-apply-request",
                "contract_version": "1.0",
                "base_proposal": base,
                "expected_proposal_sha256": proposal_cas_sha256(base),
                "ai_proposal": provider,
                "selections": [
                    {"suggestion_id": by_path[path]["id"], "alternative_id": None}
                    for path in selected_paths
                ],
            }
        )
        ai_record_id = by_path["employment[0].role"]["record_id"]
        ai_record = next(
            item
            for item in merged["proposal"]["experience"]
            if item["id"] == ai_record_id
        )
        confirmed_fact_ids = {
            ai_record["field_fact_ids"][field]
            for field in ("company", "role", "start_date", "end_date")
        }
        decisions = [
            {
                "fact_id": fact_id,
                "decision": "confirm",
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            }
            for fact_id in sorted(confirmed_fact_ids)
        ]
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.yaml"
            candidate_path.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            result = adopt_confirmed(
                merged,
                candidate_path,
                decisions,
                hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
            )
            candidate = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
        adopted = next(
            item for item in candidate["experience"] if item["id"] == ai_record_id
        )
        self.assertIn(ai_record_id, result["adopted_record_ids"])
        self.assertNotIn("location", adopted)
        self.assertEqual(adopted["details"], [])
        self.assertNotIn(
            ai_record["field_fact_ids"]["location"], result["adopted_fact_ids"]
        )

    def test_ai_contract_schema_is_versioned_and_closed(self) -> None:
        schema = json.loads(
            (
                ROOT / "contracts" / "v1" / "ai-cv-structure-proposal.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(
            schema["$schema"], "https://json-schema.org/draft/2020-12/schema"
        )
        self.assertFalse(schema["additionalProperties"])
        self.assertEqual(
            schema["properties"]["contract"]["const"], "ai-cv-structure-proposal"
        )
        self.assertEqual(schema["properties"]["contract_version"]["const"], "1.0")
        self.assertFalse(schema["$defs"]["fieldSuggestion"]["additionalProperties"])

        materialization_schema = json.loads(
            (
                ROOT
                / "contracts"
                / "v1"
                / "ai-cv-structure-materialization-request.schema.json"
            ).read_text(encoding="utf-8")
        )
        self.assertFalse(materialization_schema["additionalProperties"])
        self.assertEqual(
            materialization_schema["properties"]["contract"]["const"],
            "ai-cv-structure-materialization-request",
        )
        self.assertEqual(
            materialization_schema["properties"]["ai_proposal"]["$ref"],
            "ai-cv-structure-proposal.schema.json",
        )

    def test_validate_ai_cli_preserves_utf8_under_legacy_stdio_encoding(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        request = ai_validation_request(base, complex_ai_proposal(base))
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        environment["PYTHONUTF8"] = "0"
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "validate-ai-structure",
                "--request",
                "-",
            ],
            cwd=ROOT,
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace"),
        )
        validated = json.loads(process.stdout.decode("utf-8"))
        role = next(
            item
            for item in validated["suggestions"]
            if item["path"] == "employment[0].role"
        )
        self.assertEqual(role["value"], "Senior Softwareentwickler")
        self.assertEqual(
            role["questions"],
            ["Ist der Senior-Titel die offizielle Rollenbezeichnung?"],
        )

    def test_apply_ai_cli_returns_normal_unverified_proposal_json(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        provider = complex_ai_proposal(base)
        validated = validate_ai_structure_request(ai_validation_request(base, provider))
        skill = next(
            item for item in validated["suggestions"] if item["path"] == "skills[0]"
        )
        request = {
            "contract": "ai-cv-structure-apply-request",
            "contract_version": "1.0",
            "base_proposal": base,
            "expected_proposal_sha256": proposal_cas_sha256(base),
            "ai_proposal": provider,
            "selections": [{"suggestion_id": skill["id"], "alternative_id": None}],
        }
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "apply-ai-structure",
                "--request",
                "-",
                "--output",
                "-",
            ],
            cwd=ROOT,
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace"),
        )
        merged = json.loads(process.stdout.decode("utf-8"))
        ai_facts = [
            item
            for item in merged["proposal"]["facts"]
            if item["source_anchor"].get("recognition_method") == "ai_assisted"
        ]
        self.assertEqual(
            [(item["value"], item["status"]) for item in ai_facts],
            [("Python", "unverified")],
        )
        self.assertEqual(validate_proposal(merged), [])

    def test_materialize_ai_cli_returns_utf8_complete_unverified_proposal(self) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        request = ai_materialization_request(base, complex_ai_proposal(base))
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp1252"
        environment["PYTHONUTF8"] = "0"
        process = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "materialize-ai-structure",
                "--request",
                "-",
                "--output",
                "-",
            ],
            cwd=ROOT,
            input=json.dumps(request, ensure_ascii=False).encode("utf-8"),
            capture_output=True,
            env=environment,
            check=False,
        )
        self.assertEqual(
            process.returncode,
            0,
            process.stderr.decode("utf-8", errors="replace"),
        )
        materialized = json.loads(process.stdout.decode("utf-8"))
        self.assertEqual(materialized["contract"], "cv-import-proposal")
        self.assertEqual(materialized["contract_version"], "1.0")
        self.assertEqual(
            materialized["proposal"]["experience"][0]["role"],
            "Senior Softwareentwickler",
        )
        self.assertEqual(validate_proposal(materialized), [])

    def test_materialized_ai_facts_require_later_explicit_confirmation_to_adopt(
        self,
    ) -> None:
        base = extract_cv(FIXTURES / "synthetic-complex-german-cv.html")
        materialized = materialize_ai_structure_request(
            ai_materialization_request(base, complex_ai_proposal(base))
        )
        ai_record = materialized["proposal"]["experience"][0]
        required_fact_ids = {
            ai_record["field_fact_ids"][field]
            for field in ("company", "role", "start_date", "end_date")
        }
        self.assertTrue(
            all(
                item["status"] == "unverified"
                for item in materialized["proposal"]["facts"]
            )
        )

        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.yaml"
            candidate_path.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            before = candidate_path.read_bytes()
            candidate_sha256 = hashlib.sha256(before).hexdigest()
            without_confirmation = adopt_confirmed(
                materialized, candidate_path, [], candidate_sha256
            )
            self.assertEqual(without_confirmation["status"], "no_confirmed_facts")
            self.assertEqual(candidate_path.read_bytes(), before)

            adopted = adopt_confirmed(
                materialized,
                candidate_path,
                [
                    {
                        "fact_id": fact_id,
                        "decision": "confirm",
                        "explicitly_confirmed": True,
                        "confirmation_origin": "explicit_local_user_action",
                    }
                    for fact_id in sorted(required_fact_ids)
                ],
                candidate_sha256,
            )
        self.assertIn(ai_record["id"], adopted["adopted_record_ids"])

    def test_user_additions_are_unverified_atomic_facts_until_adopted(self) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        employment = proposal["proposal"]["experience"][0]
        extended, fact_ids = extend_user_facts(
            proposal,
            [
                {
                    "id": "server-addition-1",
                    "collection": "experience",
                    "record_id": employment["id"],
                    "field": "detail",
                    "value": "Added an explicitly supplied synthetic fact.",
                    "category": "achievement",
                }
            ],
            proposal_cas_sha256(proposal),
        )
        fact = next(
            item for item in extended["proposal"]["facts"] if item["id"] == fact_ids[0]
        )
        self.assertEqual(fact["status"], "unverified")
        self.assertEqual(fact["source_anchor"]["origin"], "user_supplied")
        self.assertEqual(validate_proposal(extended), [])
        self.assertTrue(
            any(
                item["fact_id"] == fact_ids[0]
                for item in extended["proposal"]["experience"][0]["details"]
            )
        )

    def test_cli_detail_edit_adopts_new_fact_and_excludes_rejected_imported_detail(
        self,
    ) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        employment = proposal["proposal"]["experience"][0]
        old_detail = employment["details"][0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            proposal_path = root / "proposal.yaml"
            additions_path = root / "additions.json"
            extended_path = root / "extended.yaml"
            candidate = root / "candidate.yaml"
            decisions_path = root / "decisions.json"
            proposal_path.write_text(
                yaml.safe_dump(proposal, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            additions_path.write_text(
                json.dumps(
                    [
                        {
                            "id": "server-detail-edit",
                            "collection": "experience",
                            "record_id": employment["id"],
                            "field": "detail",
                            "value": "Improved synthetic achievement supplied by the candidate.",
                            "category": "achievement",
                        }
                    ]
                ),
                encoding="utf-8",
            )
            extend_process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "cv_import_contract.py"),
                    "extend-user-facts",
                    "--proposal",
                    str(proposal_path),
                    "--additions",
                    str(additions_path),
                    "--expected-proposal-sha256",
                    proposal_cas_sha256(proposal),
                    "--output",
                    str(extended_path),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(extend_process.returncode, 0, extend_process.stderr)
            extended_summary = json.loads(extend_process.stdout)
            new_fact_id = extended_summary["added_fact_ids"][0]
            extended = yaml.safe_load(extended_path.read_text(encoding="utf-8"))
            new_claim_id = next(
                item["claim_id"]
                for item in extended["proposal"]["facts"]
                if item["id"] == new_fact_id
            )
            decisions = [
                *[
                    {
                        "fact_id": fact_id,
                        "decision": "confirm",
                        "explicitly_confirmed": True,
                        "confirmation_origin": "explicit_local_user_action",
                    }
                    for fact_id in employment["field_fact_ids"].values()
                ],
                {"fact_id": old_detail["fact_id"], "decision": "reject"},
                {
                    "fact_id": new_fact_id,
                    "decision": "confirm",
                    "explicitly_confirmed": True,
                    "confirmation_origin": "explicit_local_user_action",
                },
            ]
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            candidate.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            expected_candidate = hashlib.sha256(candidate.read_bytes()).hexdigest()
            adopt_process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "cv_import_contract.py"),
                    "adopt-confirmed",
                    "--proposal",
                    str(extended_path),
                    "--candidate",
                    str(candidate),
                    "--decisions",
                    str(decisions_path),
                    "--expected-candidate-sha256",
                    expected_candidate,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            final_candidate = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        self.assertEqual(adopt_process.returncode, 0, adopt_process.stderr)
        adopted = next(
            item
            for item in final_candidate["experience"]
            if item["id"] == employment["id"]
        )
        self.assertEqual(
            adopted["details"],
            [
                {
                    "text": "Improved synthetic achievement supplied by the candidate.",
                    "claim_id": new_claim_id,
                }
            ],
        )
        final_claim_ids = {item["id"] for item in final_candidate["claims"]}
        self.assertIn(new_claim_id, final_claim_ids)
        self.assertNotIn(old_detail["claim_id"], final_claim_ids)

    def test_user_addition_requires_server_id_and_proposal_cas(self) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        addition = [
            {
                "id": "bad id",
                "collection": "skills",
                "record_key": "skill-1",
                "field": "name",
                "value": "Rust",
                "category": "skill",
            }
        ]
        with self.assertRaises(ImportContractError) as caught:
            extend_user_facts(proposal, addition, proposal_cas_sha256(proposal))
        self.assertEqual(caught.exception.code, "invalid_additions")
        addition[0]["id"] = "server-addition-2"
        with self.assertRaises(ImportContractError) as caught:
            extend_user_facts(proposal, addition, "0" * 64)
        self.assertEqual(caught.exception.code, "cas_mismatch")

    def test_two_process_adoptions_with_same_cas_have_exactly_one_winner(self) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        employment = proposal["proposal"]["experience"][0]
        decisions = [
            {
                "fact_id": fact_id,
                "decision": "confirm",
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            }
            for fact_id in employment["field_fact_ids"].values()
        ]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.yaml"
            proposal_path = root / "proposal.yaml"
            decisions_path = root / "decisions.json"
            candidate.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            proposal_path.write_text(
                yaml.safe_dump(proposal, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            expected = hashlib.sha256(candidate.read_bytes()).hexdigest()
            command = [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "adopt-confirmed",
                "--proposal",
                str(proposal_path),
                "--candidate",
                str(candidate),
                "--decisions",
                str(decisions_path),
                "--expected-candidate-sha256",
                expected,
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            outcomes = [process.communicate(timeout=20) for process in processes]
            return_codes = [process.returncode for process in processes]
            payloads = [json.loads(stdout) for stdout, _stderr in outcomes]
            history = [
                json.loads(line)
                for line in candidate.with_suffix(".yaml.history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            lock = candidate.with_name(f".{candidate.name}.adopt.lock")
            lock_exists = lock.exists()
        self.assertEqual(sorted(return_codes), [0, 2])
        winner = next(
            item for item in payloads if item.get("status") == "adopted_user_confirmed"
        )
        loser = next(item for item in payloads if item.get("status") == "rejected")
        self.assertEqual(loser["error"]["code"], "cas_mismatch")
        self.assertTrue(winner["transaction_id"])
        self.assertEqual(sum(item.get("state") == "intent" for item in history), 1)
        self.assertEqual(sum(item.get("state") == "committed" for item in history), 1)
        self.assertFalse(lock_exists)

    def test_adopt_and_profile_mutator_share_lock_and_cas_across_processes(
        self,
    ) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        employment = proposal["proposal"]["experience"][0]
        decisions = [
            {
                "fact_id": fact_id,
                "decision": "confirm",
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            }
            for fact_id in employment["field_fact_ids"].values()
        ]
        imported_claim_id = "claim-concurrent-profile-import"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            candidate = root / "candidate.yaml"
            proposal_path = root / "proposal.yaml"
            decisions_path = root / "decisions.json"
            imports_path = root / "imports.json"
            candidate.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            proposal_path.write_text(
                yaml.safe_dump(proposal, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
            imports_path.write_text(
                json.dumps(
                    [
                        {
                            "id": imported_claim_id,
                            "statement": "Synthetic concurrent profile import.",
                            "sha256": "e" * 64,
                        }
                    ]
                ),
                encoding="utf-8",
            )
            expected = hashlib.sha256(candidate.read_bytes()).hexdigest()
            adopt_command = [
                sys.executable,
                str(ROOT / "scripts" / "cv_import_contract.py"),
                "adopt-confirmed",
                "--proposal",
                str(proposal_path),
                "--candidate",
                str(candidate),
                "--decisions",
                str(decisions_path),
                "--expected-candidate-sha256",
                expected,
            ]
            profile_command = [
                sys.executable,
                str(ROOT / "scripts" / "profile_contract.py"),
                "add-import",
                "--candidate",
                str(candidate),
                "--proposals",
                str(imports_path),
                "--confirmed",
                "--expected-candidate-sha256",
                expected,
            ]
            processes = [
                subprocess.Popen(
                    command,
                    cwd=ROOT,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for command in (adopt_command, profile_command)
            ]
            outcomes = [process.communicate(timeout=20) for process in processes]
            return_codes = [process.returncode for process in processes]
            payloads = [json.loads(stdout) for stdout, _stderr in outcomes]
            final_candidate = yaml.safe_load(candidate.read_text(encoding="utf-8"))
            has_adopted_record = any(
                item["id"] == employment["id"] for item in final_candidate["experience"]
            )
            has_imported_claim = any(
                item["id"] == imported_claim_id for item in final_candidate["claims"]
            )
            history = [
                json.loads(line)
                for line in candidate.with_suffix(".yaml.history.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            lock_exists = candidate.with_name(f".{candidate.name}.adopt.lock").exists()
        self.assertEqual(sorted(return_codes), [0, 2])
        loser = next(item for item in payloads if item.get("status") == "rejected")
        self.assertEqual(loser["error"]["code"], "cas_mismatch")
        self.assertNotEqual(has_adopted_record, has_imported_claim)
        self.assertEqual(sum(item.get("state") == "intent" for item in history), 1)
        self.assertEqual(sum(item.get("state") == "committed" for item in history), 1)
        self.assertFalse(lock_exists)

    def test_incomplete_intent_is_diagnosed_and_safely_recovered_under_lock(
        self,
    ) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        pending = [
            {"fact_id": proposal["proposal"]["facts"][0]["id"], "decision": "pending"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            before = hashlib.sha256(candidate.read_bytes()).hexdigest()
            history_path = candidate.with_suffix(".yaml.history.jsonl")
            history_path.write_text(
                json.dumps(
                    {
                        "state": "intent",
                        "transaction_id": "synthetic-incomplete",
                        "before_sha256": before,
                        "after_sha256": "d" * 64,
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            status_before = recovery_status(candidate)
            result = adopt_confirmed(proposal, candidate, pending, before)
            status_after = recovery_status(candidate)
        self.assertEqual(status_before["status"], "recovery_required")
        self.assertEqual(
            status_before["incomplete_transactions"][0]["classification"],
            "not_applied",
        )
        self.assertEqual(result["status"], "no_confirmed_facts")
        self.assertEqual(result["recovered_transaction_ids"], ["synthetic-incomplete"])
        self.assertEqual(status_after["status"], "consistent")

    def test_stale_profile_lock_fails_closed_without_deleting_it(self) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        pending = [
            {"fact_id": proposal["proposal"]["facts"][0]["id"], "decision": "pending"}
        ]
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            expected = hashlib.sha256(candidate.read_bytes()).hexdigest()
            lock = candidate.with_name(f".{candidate.name}.adopt.lock")
            lock.write_text('{"token":"other"}', encoding="utf-8")
            stale = time.time() - 301
            os.utime(lock, (stale, stale))
            with self.assertRaises(ImportContractError) as caught:
                adopt_confirmed(proposal, candidate, pending, expected)
            still_exists = lock.exists()
        self.assertEqual(caught.exception.code, "profile_lock_stale")
        self.assertTrue(still_exists)

    def test_completion_write_failure_leaves_diagnosable_recoverable_intent(
        self,
    ) -> None:
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        employment = proposal["proposal"]["experience"][0]
        decisions = [
            {
                "fact_id": fact_id,
                "decision": "confirm",
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            }
            for fact_id in employment["field_fact_ids"].values()
        ]
        original_append = cv_contract._append_history_record
        calls = 0

        def fail_completion(path: Path, record: dict[str, object]) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise ImportContractError(
                    "history_write_failed", "Synthetic completion failure"
                )
            original_append(path, record)

        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate.yaml"
            candidate.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            before = hashlib.sha256(candidate.read_bytes()).hexdigest()
            with (
                patch.object(
                    cv_contract, "_append_history_record", side_effect=fail_completion
                ),
                self.assertRaises(ImportContractError) as caught,
            ):
                adopt_confirmed(proposal, candidate, decisions, before)
            after = hashlib.sha256(candidate.read_bytes()).hexdigest()
            status = recovery_status(candidate)
            pending = [
                {
                    "fact_id": proposal["proposal"]["facts"][0]["id"],
                    "decision": "pending",
                }
            ]
            recovered = adopt_confirmed(proposal, candidate, pending, after)
        self.assertEqual(caught.exception.code, "history_write_failed")
        self.assertNotEqual(before, after)
        self.assertEqual(status["status"], "recovery_required")
        self.assertEqual(
            status["incomplete_transactions"][0]["classification"],
            "applied_without_completion",
        )
        self.assertEqual(len(recovered["recovered_transaction_ids"]), 1)

    def test_cli_writes_private_artifact_and_prints_only_safe_summary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "proposal.yaml"
            process = subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts" / "cv_import_contract.py"),
                    "extract",
                    "--input",
                    str(FIXTURES / "synthetic-cv.html"),
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            proposal = yaml.safe_load(output.read_text(encoding="utf-8"))
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout)
        self.assertEqual(summary["status"], "proposal_created")
        self.assertNotIn("Mustertechnik", process.stdout)
        self.assertEqual(validate_proposal(proposal), [])


class RevokeAndSnapshotTest(unittest.TestCase):
    def adopt_everything(self, directory: str) -> tuple[Path, dict[str, Any], Any]:
        """Adopts every confirmable fact of the synthetic CV into a fresh profile."""
        proposal = extract_cv(FIXTURES / "synthetic-cv.html")
        candidate_path = Path(directory) / "candidate.yaml"
        candidate_path.write_text(
            (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
            encoding="utf-8",
        )
        digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
        decisions = [
            {
                "fact_id": fact["id"],
                "decision": "confirm",
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            }
            for fact in proposal["proposal"]["facts"]
        ]
        adopted = adopt_confirmed(proposal, candidate_path, decisions, digest)
        return candidate_path, adopted, proposal

    def test_revoke_removes_exactly_what_the_adoption_transaction_added(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path, adopted, _ = self.adopt_everything(directory)
            before = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
            self.assertTrue(adopted["adopted_claim_ids"])

            # A foreign claim must survive the revoke untouched.
            foreign = {
                "id": "claim-" + "f" * 16,
                "category": "skill",
                "statement": "name: Foreign",
                "status": "user_confirmed",
                "evidence_refs": ["source-user-foreign"],
                "allowed_outputs": ["cv"],
                "tags": [],
                "valid_from": None,
                "valid_to": None,
                "notes": "Not from the revoked adoption.",
            }
            before["claims"].append(foreign)
            before["sources"].append(
                {
                    "id": "source-user-foreign",
                    "type": "user_supplied_fact",
                    "label": "Foreign",
                    "location": "sha256:" + "0" * 64,
                }
            )
            candidate_path.write_text(
                yaml.safe_dump(before, allow_unicode=True, sort_keys=False),
                encoding="utf-8",
            )
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()

            result = revoke_claims(candidate_path, adopted["transaction_id"], digest)
            after = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "claims_revoked")
        self.assertEqual(
            result["revoked_claim_ids"], sorted(adopted["adopted_claim_ids"])
        )
        self.assertEqual(
            result["revoked_record_ids"], sorted(adopted["adopted_record_ids"])
        )
        baseline = yaml.safe_load(
            (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8")
        )
        # Everything the adoption added is gone; nothing else is.
        self.assertEqual(
            [item["id"] for item in after["claims"]],
            [item["id"] for item in baseline["claims"]] + [foreign["id"]],
        )
        self.assertEqual(
            [item["id"] for item in after["sources"]],
            [item["id"] for item in baseline["sources"]] + ["source-user-foreign"],
        )
        for collection in ("experience", "projects", "education", "certifications"):
            self.assertEqual(
                [item["id"] for item in after.get(collection) or []],
                [item["id"] for item in baseline.get(collection) or []],
            )
        self.assertEqual(result["revoked_transaction_id"], adopted["transaction_id"])
        self.assertTrue(result["replaced_snapshot_id"])

    def test_revoke_is_cas_bound_and_refuses_unknown_or_repeated_transactions(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path, adopted, _ = self.adopt_everything(directory)
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()

            with self.assertRaises(ImportContractError) as stale:
                revoke_claims(candidate_path, adopted["transaction_id"], "a" * 64)
            self.assertEqual(stale.exception.code, "cas_mismatch")

            with self.assertRaises(ImportContractError) as unknown:
                revoke_claims(candidate_path, "b" * 32, digest)
            self.assertEqual(unknown.exception.code, "unknown_transaction")

            with self.assertRaises(ImportContractError) as malformed:
                revoke_claims(candidate_path, "not-a-transaction", digest)
            self.assertEqual(malformed.exception.code, "invalid_transaction")

            revoked = revoke_claims(candidate_path, adopted["transaction_id"], digest)
            self.assertEqual(revoked["status"], "claims_revoked")

            # The profile is untouched, so the CAS hash from before still applies.
            after_digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            with self.assertRaises(ImportContractError) as repeated:
                revoke_claims(
                    candidate_path, adopted["transaction_id"], after_digest
                )
            self.assertEqual(repeated.exception.code, "already_revoked")

    def test_revoke_frees_the_claims_for_a_clean_re_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path, adopted, proposal = self.adopt_everything(directory)
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()

            decisions = [
                {
                    "fact_id": fact["id"],
                    "decision": "confirm",
                    "explicitly_confirmed": True,
                    "confirmation_origin": "explicit_local_user_action",
                }
                for fact in proposal["proposal"]["facts"]
            ]
            with self.assertRaises(ImportContractError) as collision:
                adopt_confirmed(proposal, candidate_path, decisions, digest)
            self.assertEqual(collision.exception.code, "claim_collision")

            revoke_claims(candidate_path, adopted["transaction_id"], digest)
            freed = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            re_adopted = adopt_confirmed(proposal, candidate_path, decisions, freed)

        self.assertEqual(re_adopted["status"], "adopted_user_confirmed")
        self.assertEqual(
            sorted(re_adopted["adopted_claim_ids"]),
            sorted(adopted["adopted_claim_ids"]),
        )

    def test_adoption_snapshot_restores_overwritten_profile_scalars(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path, adopted, _ = self.adopt_everything(directory)
            snapshot_id = adopted["replaced_snapshot_id"]
            self.assertTrue(snapshot_id)
            after_adopt = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()

            listed = list_profile_snapshots(candidate_path)
            self.assertIn(
                snapshot_id, [item["snapshot_id"] for item in listed["snapshots"]]
            )

            restored = restore_profile_snapshot(candidate_path, snapshot_id, digest)
            rolled_back = yaml.safe_load(candidate_path.read_text(encoding="utf-8"))

        baseline = yaml.safe_load(
            (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8")
        )
        self.assertEqual(restored["status"], "profile_snapshot_restored")
        self.assertEqual(rolled_back, baseline)
        # The scalar overwrite that a claim-scoped revoke cannot undo is gone.
        self.assertNotEqual(
            after_adopt["profile"]["full_name"], rolled_back["profile"]["full_name"]
        )
        self.assertEqual(rolled_back["profile"]["full_name"], "Erika Beispiel")
        self.assertTrue(restored["replaced_snapshot_id"])

    def test_snapshots_are_cas_bound_content_addressed_and_verified(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path = Path(directory) / "candidate.yaml"
            candidate_path.write_text(
                (FIXTURES / "valid-candidate.yaml").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()

            with self.assertRaises(ImportContractError) as stale:
                capture_profile_snapshot(candidate_path, "c" * 64)
            self.assertEqual(stale.exception.code, "cas_mismatch")

            first = capture_profile_snapshot(candidate_path, digest, "Vor dem Import")
            self.assertEqual(first["status"], "profile_snapshot_captured")
            self.assertFalse(first["snapshot"]["reused"])
            self.assertEqual(first["snapshot"]["label"], "Vor dem Import")

            # An unchanged profile reuses the stored bytes instead of duplicating.
            second = capture_profile_snapshot(candidate_path, digest)
            self.assertTrue(second["snapshot"]["reused"])
            self.assertEqual(
                second["snapshot"]["snapshot_id"], first["snapshot"]["snapshot_id"]
            )

            snapshot_id = first["snapshot"]["snapshot_id"]
            self.assertEqual(
                restore_profile_snapshot(candidate_path, snapshot_id, digest)["status"],
                "profile_already_at_snapshot",
            )

            with self.assertRaises(ImportContractError) as unknown:
                restore_profile_snapshot(
                    candidate_path, "profile-snapshot-" + "0" * 16, digest
                )
            self.assertEqual(unknown.exception.code, "unknown_snapshot")

            with self.assertRaises(ImportContractError) as malformed:
                restore_profile_snapshot(candidate_path, "../escape", digest)
            self.assertEqual(malformed.exception.code, "invalid_snapshot")

            content = (
                candidate_path.with_suffix(candidate_path.suffix + ".snapshots")
                / f"{snapshot_id}.yaml"
            )
            content.write_text("tampered: true\n", encoding="utf-8")
            with self.assertRaises(ImportContractError) as corrupted:
                restore_profile_snapshot(candidate_path, snapshot_id, digest)
            self.assertEqual(corrupted.exception.code, "snapshot_corrupted")

    def test_revoke_cli_reports_the_transaction_and_stays_quiet_about_content(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path, adopted, _ = self.adopt_everything(directory)
            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            process = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "scripts.cv_import_contract",
                    "revoke-claims",
                    "--candidate",
                    str(candidate_path),
                    "--transaction-id",
                    adopted["transaction_id"],
                    "--expected-candidate-sha256",
                    digest,
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        summary = json.loads(process.stdout)
        self.assertEqual(summary["status"], "claims_revoked")
        self.assertEqual(summary["revoked_transaction_id"], adopted["transaction_id"])
        self.assertNotIn("Mustertechnik", process.stdout)

    def test_adoption_ledger_lists_revocable_transactions_until_revoked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            candidate_path, adopted, _ = self.adopt_everything(directory)
            listed = list_adoptions(candidate_path)
            self.assertEqual(len(listed["adoptions"]), 1)
            entry = listed["adoptions"][0]
            self.assertEqual(entry["transaction_id"], adopted["transaction_id"])
            self.assertEqual(entry["claim_count"], len(adopted["adopted_claim_ids"]))
            self.assertEqual(entry["present_claim_count"], entry["claim_count"])
            self.assertTrue(entry["source_sha256"])

            digest = hashlib.sha256(candidate_path.read_bytes()).hexdigest()
            revoke_claims(candidate_path, adopted["transaction_id"], digest)
            self.assertEqual(list_adoptions(candidate_path)["adoptions"], [])

    def test_capabilities_announce_the_claim_management_commands(self) -> None:
        commands = capabilities()["commands"]
        for command in (
            "revoke-claims",
            "list-adoptions",
            "capture-profile-snapshot",
            "list-profile-snapshots",
            "restore-profile-snapshot",
        ):
            self.assertIn(command, commands)


if __name__ == "__main__":
    unittest.main()
