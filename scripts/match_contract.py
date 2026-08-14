from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

try:
    from .common import PUBLISHABLE_STATUSES, load_yaml, validate_candidate
except ImportError:
    from common import PUBLISHABLE_STATUSES, load_yaml, validate_candidate


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-") or "requirement"


def analyze_job(job: dict[str, Any]) -> dict[str, Any]:
    requirements = []
    for index, skill in enumerate(job.get("skills", [])):
        text = str(skill).strip()
        if not text:
            continue
        requirements.append(
            {
                "id": f"req-{index + 1}-{_slug(text)}",
                "original_text": text,
                "normalized_competency": text,
                "priority": "important",
                "category": "technology",
                "source_field": f"skills[{index}]",
                "inferred": False,
                "confidence": "explicit",
            }
        )
    description = str(job.get("description", ""))
    responsibilities: list[dict[str, Any]] = []
    known = {item["normalized_competency"].casefold() for item in requirements}
    for match in re.finditer(r"[^\n.!?]+(?:[.!?]|$)", description):
        sentence = match.group(0).strip()
        if not sentence:
            continue
        folded = sentence.casefold()
        anchor = {
            "source_field": "description",
            "start": match.start(),
            "end": match.end(),
            "text": sentence,
        }
        if re.search(
            r"\b(aufgabe|responsibilit|du wirst|you will|zuständig)\w*", folded
        ):
            responsibilities.append(
                {
                    "original_text": sentence,
                    "source_anchor": anchor,
                    "confidence": "explicit",
                }
            )
        priority = None
        if re.search(
            r"\b(must|required|mandatory|zwingend|erforderlich|voraussetzung)\w*",
            folded,
        ):
            priority = "must_have"
        elif re.search(
            r"\b(nice[ -]to[ -]have|optional|wünschenswert|von vorteil)\b", folded
        ):
            priority = "optional"
        elif re.search(r"\b(kenntnisse|experience|erfahrung|skills?)\b", folded):
            priority = "important"
        if priority and sentence.casefold() not in known:
            requirements.append(
                {
                    "id": f"req-{len(requirements) + 1}-{_slug(sentence[:48])}",
                    "original_text": sentence,
                    "normalized_competency": sentence,
                    "priority": priority,
                    "category": "explicit_statement",
                    "source_field": "description",
                    "source_anchor": anchor,
                    "inferred": False,
                    "confidence": "explicit",
                    "user_reviewable": True,
                }
            )
            known.add(sentence.casefold())
    source_hash = hashlib.sha256(
        json.dumps(job, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "contract": "bewerbungs-pipeline",
        "contract_version": "1.0",
        "schema_version": 1,
        "analysis_version": source_hash[:16],
        "source_sha256": source_hash,
        "job": {
            "id": str(job.get("id", "")),
            "title": str(job.get("title", "")),
            "company": str(job.get("company", "")),
            "location": str(job.get("location", "")),
            "source": str(job.get("sourceId", job.get("source", ""))),
            "language": str(job.get("language", "")),
        },
        "requirements": requirements,
        "responsibilities": responsibilities,
        "conditions": {
            "location": {
                "value": str(job.get("location", "")),
                "source_field": "location",
            },
            "employment_type": {
                "value": job.get("employmentType"),
                "source_field": "employmentType",
            },
            "language": {"value": job.get("language"), "source_field": "language"},
        },
        "company_context": [],
        "open_questions": ["Prioritäten der expliziten Skills prüfen."]
        if requirements
        else ["Stellenanforderungen manuell ergänzen."],
    }


def build_match_matrix(
    job_analysis: dict[str, Any], candidate: dict[str, Any], output_type: str
) -> dict[str, Any]:
    errors = validate_candidate(candidate)
    if errors:
        raise ValueError("Invalid candidate profile: " + "; ".join(errors))
    claims = {
        claim["id"]: claim
        for claim in candidate.get("claims", [])
        if isinstance(claim, dict)
        and claim.get("status") in PUBLISHABLE_STATUSES
        and output_type in claim.get("allowed_outputs", [])
    }
    skill_claims: dict[str, list[str]] = {}
    for skill in candidate.get("skills", []):
        if (
            not isinstance(skill, dict)
            or skill.get("status") not in PUBLISHABLE_STATUSES
        ):
            continue
        names = [
            skill.get("name"),
            skill.get("canonical_name"),
            *skill.get("aliases", []),
        ]
        allowed_ids = [
            claim_id for claim_id in skill.get("claim_ids", []) if claim_id in claims
        ]
        for name in names:
            if name:
                skill_claims[str(name).casefold()] = allowed_ids
    matches = []
    for requirement in job_analysis.get("requirements", []):
        competency = str(requirement.get("normalized_competency", ""))
        evidence = skill_claims.get(competency.casefold(), [])
        matches.append(
            {
                "requirement_id": requirement.get("id"),
                "competency": competency,
                "priority": requirement.get("priority", "important"),
                "classification": "direct_match" if evidence else "gap",
                "evidence_claim_ids": evidence,
                "supported_wording": [claims[item]["statement"] for item in evidence],
                "forbidden_wording": []
                if evidence
                else [f"Erfahrung mit {competency}"],
                "rationale": "Expliziter publizierbarer Skill-Claim."
                if evidence
                else "Keine zulässige Evidence im Kandidatenprofil.",
            }
        )
    coverage: dict[str, dict[str, int]] = {}
    for priority in ("must_have", "important"):
        coverage[priority] = {
            name: 0
            for name in ("direct_match", "transferable_match", "partial_match", "gap")
        }
    for item in matches:
        if item["priority"] in coverage:
            coverage[item["priority"]][item["classification"]] += 1
    return {
        "contract": "bewerbungs-pipeline",
        "contract_version": "1.0",
        "schema_version": 1,
        "job_title": job_analysis.get("job", {}).get("title", ""),
        "matches": matches,
        "coverage": coverage,
        "unresolved_questions": job_analysis.get("open_questions", []),
    }


def validate_match_matrix(
    matrix: dict[str, Any], candidate: dict[str, Any], output_type: str
) -> list[str]:
    """Validate user/agent-reviewed classifications without inventing evidence."""
    allowed_classes = {"direct_match", "transferable_match", "partial_match", "gap"}
    errors: list[str] = []
    candidate_errors = validate_candidate(candidate)
    if candidate_errors:
        return ["candidate: invalid profile"]
    allowed_claims = {
        str(claim["id"])
        for claim in candidate.get("claims", [])
        if isinstance(claim, dict)
        and claim.get("status") in PUBLISHABLE_STATUSES
        and output_type in claim.get("allowed_outputs", [])
    }
    matches = matrix.get("matches")
    if not isinstance(matches, list):
        return ["matches: expected a list"]
    seen: set[str] = set()
    for index, item in enumerate(matches):
        prefix = f"matches[{index}]"
        if not isinstance(item, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        requirement_id = item.get("requirement_id")
        if (
            not isinstance(requirement_id, str)
            or not requirement_id.strip()
            or requirement_id in seen
        ):
            errors.append(f"{prefix}: requirement_id fehlt oder ist doppelt")
        elif requirement_id:
            seen.add(requirement_id)
        classification = item.get("classification")
        evidence = item.get("evidence_claim_ids", [])
        if not isinstance(evidence, list) or any(
            not isinstance(claim_id, str) for claim_id in evidence
        ):
            errors.append(f"{prefix}: evidence_claim_ids muss eine String-Liste sein")
            evidence = []
        if classification not in allowed_classes:
            errors.append(f"{prefix}: ungültige classification")
        if classification != "gap" and not evidence:
            errors.append(f"{prefix}: Nicht-Gap benötigt Evidence")
        if classification == "gap" and evidence:
            errors.append(f"{prefix}: Gap darf keine Evidence enthalten")
        unknown = sorted(set(evidence) - allowed_claims)
        if unknown:
            errors.append(f"{prefix}: unzulässige Claim-IDs: {', '.join(unknown)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evidence-backed job analysis and matching contract"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    analyze = subparsers.add_parser("analyze")
    analyze.add_argument("--job", required=True)
    analyze.add_argument("--output")
    match = subparsers.add_parser("match")
    match.add_argument("--analysis", required=True)
    match.add_argument("--candidate", required=True)
    match.add_argument("--output-type", required=True)
    match.add_argument("--output")
    validate = subparsers.add_parser("validate-match")
    validate.add_argument("--matrix", required=True)
    validate.add_argument("--candidate", required=True)
    validate.add_argument("--output-type", required=True)
    validate.add_argument("--output")
    args = parser.parse_args()
    if args.command == "analyze":
        result = analyze_job(json.loads(Path(args.job).read_text(encoding="utf-8")))
    elif args.command == "match":
        result = build_match_matrix(
            load_yaml(args.analysis), load_yaml(args.candidate), args.output_type
        )
    else:
        errors = validate_match_matrix(
            load_yaml(args.matrix), load_yaml(args.candidate), args.output_type
        )
        result = {"valid": not errors, "errors": errors}
    rendered = yaml.safe_dump(result, allow_unicode=True, sort_keys=False)
    if args.output:
        Path(args.output).write_text(rendered, encoding="utf-8")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
