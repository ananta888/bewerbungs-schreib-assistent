from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import yaml

try:
    from .common import format_errors, load_yaml, validate_candidate
except ImportError:
    from common import format_errors, load_yaml, validate_candidate


ALLOWED_CLAIM_FIELDS = {
    "statement",
    "status",
    "evidence_refs",
    "allowed_outputs",
    "valid_from",
    "valid_to",
}
PUBLISHABLE_STATUSES = {"verified", "user_confirmed"}


def profile_summary(candidate_path: str | Path) -> dict[str, Any]:
    candidate = load_yaml(candidate_path)
    errors = validate_candidate(candidate)
    quality_findings = []
    source_ids = {
        str(item.get("id"))
        for item in candidate.get("sources", [])
        if isinstance(item, dict)
    }
    claim_ids = {
        str(item.get("id"))
        for item in candidate.get("claims", [])
        if isinstance(item, dict)
    }
    for claim in candidate.get("claims", []):
        if not isinstance(claim, dict):
            continue
        if claim.get("status") in {"unverified", "inferred"}:
            quality_findings.append(
                {
                    "claim_id": claim.get("id"),
                    "category": "non_publishable",
                    "severity": "medium",
                }
            )
        missing_sources = sorted(
            set(map(str, claim.get("evidence_refs", []))) - source_ids
        )
        if missing_sources:
            quality_findings.append(
                {
                    "claim_id": claim.get("id"),
                    "category": "orphan_evidence_reference",
                    "severity": "high",
                    "references": missing_sources,
                }
            )
        if (
            claim.get("category") == "metric"
            and not claim.get("evidence_refs")
            and claim.get("status") in PUBLISHABLE_STATUSES
        ):
            quality_findings.append(
                {
                    "claim_id": claim.get("id"),
                    "category": "unclear_metric",
                    "severity": "high",
                }
            )
        valid_to = str(claim.get("valid_to", ""))
        if valid_to and valid_to != "present":
            try:
                end = date.fromisoformat(
                    valid_to
                    if len(valid_to) == 10
                    else f"{valid_to}-01"
                    if len(valid_to) == 7
                    else f"{valid_to}-01-01"
                )
                if (
                    end < datetime.now(timezone.utc).date()
                    and claim.get("status") in PUBLISHABLE_STATUSES
                ):
                    quality_findings.append(
                        {
                            "claim_id": claim.get("id"),
                            "category": "review_due",
                            "severity": "medium",
                        }
                    )
            except ValueError:
                pass
    for collection in (
        "experience",
        "projects",
        "skills",
        "education",
        "certifications",
        "languages",
    ):
        for item in candidate.get(collection, []):
            if not isinstance(item, dict):
                continue
            missing_claims = sorted(
                set(map(str, item.get("claim_ids", []))) - claim_ids
            )
            if missing_claims:
                quality_findings.append(
                    {
                        "record_id": item.get("id", item.get("name", collection)),
                        "category": "orphan_claim_reference",
                        "severity": "high",
                        "references": missing_claims,
                    }
                )
    experiences = [
        item for item in candidate.get("experience", []) if isinstance(item, dict)
    ]
    for index, left in enumerate(experiences):
        for right in experiences[index + 1 :]:
            if (
                left.get("company") == right.get("company")
                and left.get("start_date") == right.get("start_date")
                and left.get("role") != right.get("role")
            ):
                quality_findings.append(
                    {
                        "record_id": left.get("id"),
                        "category": "possible_timeline_conflict",
                        "severity": "medium",
                        "conflicts_with": right.get("id"),
                    }
                )
    return {
        "contract": "candidate-profile",
        "contract_version": "1.0",
        "valid": not errors,
        "errors": errors,
        "quality_findings": quality_findings,
        "profile": candidate.get("profile", {}),
        "claims": [
            {
                "id": claim.get("id"),
                "statement": claim.get("statement"),
                "status": claim.get("status"),
                "evidence_refs": claim.get("evidence_refs", []),
                "allowed_outputs": claim.get("allowed_outputs", []),
                "valid_from": claim.get("valid_from"),
                "valid_to": claim.get("valid_to"),
            }
            for claim in candidate.get("claims", [])
            if isinstance(claim, dict)
        ],
    }


def apply_claim_patch(
    candidate_path: str | Path, operations: list[dict[str, Any]], confirmed: bool
) -> dict[str, Any]:
    path = Path(candidate_path)
    before_bytes = path.read_bytes()
    candidate = load_yaml(path)
    claims = {
        claim.get("id"): claim
        for claim in candidate.get("claims", [])
        if isinstance(claim, dict)
    }
    for operation in operations:
        claim_id = operation.get("claim_id")
        field = operation.get("field")
        if claim_id not in claims:
            raise ValueError(f"Unknown claim: {claim_id!r}")
        if field not in ALLOWED_CLAIM_FIELDS:
            raise ValueError(f"Field cannot be patched: {field!r}")
        value = operation.get("value")
        if field == "status" and value in PUBLISHABLE_STATUSES and not confirmed:
            raise ValueError(
                "Publishable claim status requires explicit user confirmation"
            )
        claims[claim_id][field] = value
    errors = validate_candidate(candidate)
    if errors:
        raise ValueError(format_errors(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(candidate, stream, allow_unicode=True, sort_keys=False)
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    updated_claim_ids = sorted({str(item["claim_id"]) for item in operations})
    history = {
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "claim_ids": updated_claim_ids,
        "fields": sorted({str(item["field"]) for item in operations}),
        "before_sha256": hashlib.sha256(before_bytes).hexdigest(),
        "after_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "explicitly_confirmed": confirmed,
        "confirmation_origin": "explicit_local_user_action"
        if confirmed
        else "not_required",
    }
    history_path = path.with_suffix(path.suffix + ".history.jsonl")
    with history_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(history, ensure_ascii=False) + "\n")
    return {
        "status": "updated",
        "updated_claim_ids": updated_claim_ids,
        "history_recorded": True,
    }


def add_import_proposals(
    candidate_path: str | Path, proposals: list[dict[str, Any]], confirmed: bool
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Import proposals require explicit user confirmation")
    path = Path(candidate_path)
    candidate = load_yaml(path)
    known = {
        claim.get("id")
        for claim in candidate.get("claims", [])
        if isinstance(claim, dict)
    }
    added: list[str] = []
    for proposal in proposals:
        claim_id = str(proposal.get("id", ""))
        statement = str(proposal.get("statement", "")).strip()
        sha256 = str(proposal.get("sha256", ""))
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", claim_id) or claim_id in known:
            raise ValueError(f"Invalid or duplicate claim id: {claim_id!r}")
        if not statement or not re.fullmatch(r"[a-f0-9]{64}", sha256):
            raise ValueError("Import proposal requires statement and sha256 provenance")
        source_id = f"source-import-{sha256[:12]}"
        if not any(
            item.get("id") == source_id for item in candidate.get("sources", [])
        ):
            candidate["sources"].append(
                {
                    "id": source_id,
                    "type": "user_import",
                    "label": "User-confirmed import",
                    "location": f"sha256:{sha256}",
                    "verified_at": datetime.now(timezone.utc).date().isoformat(),
                }
            )
        candidate["claims"].append(
            {
                "id": claim_id,
                "category": "imported_proposal",
                "statement": statement,
                "status": "unverified",
                "evidence_refs": [source_id],
                "allowed_outputs": [
                    "cv",
                    "cover_letter",
                    "email",
                    "linkedin",
                    "interview",
                ],
                "tags": [],
                "valid_from": None,
                "valid_to": None,
                "notes": "Imported and explicitly accepted as an unverified proposal.",
            }
        )
        known.add(claim_id)
        added.append(claim_id)
    errors = validate_candidate(candidate)
    if errors:
        raise ValueError(format_errors(errors))
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(candidate, stream, allow_unicode=True, sort_keys=False)
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise
    return {"status": "added_unverified", "added_claim_ids": added}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safe candidate profile read and patch contract"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show")
    show.add_argument("--candidate", required=True)
    patch = subparsers.add_parser("patch")
    patch.add_argument("--candidate", required=True)
    patch.add_argument("--operations", required=True)
    patch.add_argument("--confirmed", action="store_true")
    add = subparsers.add_parser("add-import")
    add.add_argument("--candidate", required=True)
    add.add_argument("--proposals", required=True)
    add.add_argument("--confirmed", action="store_true")
    args = parser.parse_args()
    if args.command == "show":
        result = profile_summary(args.candidate)
    elif args.command == "patch":
        operations = json.loads(Path(args.operations).read_text(encoding="utf-8"))
        if not isinstance(operations, list):
            raise ValueError("Operations must be a JSON list")
        result = apply_claim_patch(args.candidate, operations, args.confirmed)
    else:
        proposals = json.loads(Path(args.proposals).read_text(encoding="utf-8"))
        if not isinstance(proposals, list):
            raise ValueError("Proposals must be a JSON list")
        result = add_import_proposals(args.candidate, proposals, args.confirmed)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
