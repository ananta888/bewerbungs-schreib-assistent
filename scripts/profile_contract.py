from __future__ import annotations

import argparse
import copy
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

try:
    from .common import format_errors, load_yaml, validate_candidate
    from .cv_import_contract import (
        ImportContractError,
        _candidate_digest,
        _commit_candidate_profile_locked,
        _exclusive_profile_lock,
        _recover_incomplete_intents,
    )
except ImportError:
    from common import format_errors, load_yaml, validate_candidate
    from cv_import_contract import (
        ImportContractError,
        _candidate_digest,
        _commit_candidate_profile_locked,
        _exclusive_profile_lock,
        _recover_incomplete_intents,
    )


ALLOWED_CLAIM_FIELDS = {
    "statement",
    "status",
    "evidence_refs",
    "allowed_outputs",
    "valid_from",
    "valid_to",
}
PUBLISHABLE_STATUSES = {"verified", "user_confirmed"}
OUTPUT_TYPES = {"cv", "cover_letter", "email", "linkedin", "interview"}
EVIDENCE_COLLECTIONS = (
    "experience",
    "projects",
    "skills",
    "education",
    "certifications",
    "languages",
)


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
                "category": claim.get("category"),
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


def profile_evidence(
    candidate_path: str | Path, output_type: str
) -> dict[str, Any]:
    """Return only publishable claims and fully claim-bound structured records."""
    if output_type not in OUTPUT_TYPES:
        raise ValueError("Unsupported candidate evidence output type")
    candidate = load_yaml(candidate_path)
    errors = validate_candidate(candidate)
    if errors:
        return {
            "contract": "candidate-evidence-snapshot",
            "contract_version": "1.0",
            "output_type": output_type,
            "valid": False,
            "errors": errors,
            "claims": [],
            "records": {collection: [] for collection in EVIDENCE_COLLECTIONS},
        }

    publishable_claims: dict[str, dict[str, Any]] = {}
    for claim in candidate.get("claims", []):
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("id", ""))
        if (
            claim_id
            and claim.get("status") in PUBLISHABLE_STATUSES
            and output_type in claim.get("allowed_outputs", [])
            and (
                claim.get("status") == "user_confirmed"
                or bool(claim.get("evidence_refs"))
            )
        ):
            publishable_claims[claim_id] = claim

    records: dict[str, list[dict[str, Any]]] = {}
    for collection in EVIDENCE_COLLECTIONS:
        projected: list[dict[str, Any]] = []
        for item in candidate.get(collection, []):
            if not isinstance(item, dict) or item.get("status") not in PUBLISHABLE_STATUSES:
                continue
            claim_ids = [str(value) for value in item.get("claim_ids", [])]
            if not claim_ids or any(value not in publishable_claims for value in claim_ids):
                continue
            projected.append(copy.deepcopy(item))
        records[collection] = projected

    return {
        "contract": "candidate-evidence-snapshot",
        "contract_version": "1.0",
        "output_type": output_type,
        "valid": True,
        "errors": [],
        "claims": [
            {
                "id": claim.get("id"),
                "category": claim.get("category"),
                "statement": claim.get("statement"),
                "status": claim.get("status"),
                "evidence_refs": claim.get("evidence_refs", []),
                "allowed_outputs": claim.get("allowed_outputs", []),
                "tags": claim.get("tags", []),
                "valid_from": claim.get("valid_from"),
                "valid_to": claim.get("valid_to"),
            }
            for claim in publishable_claims.values()
        ],
        "records": records,
    }


def apply_claim_patch(
    candidate_path: str | Path,
    operations: list[dict[str, Any]],
    confirmed: bool,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        before_sha256 = _candidate_digest(path)
        recovered = _recover_incomplete_intents(path, before_sha256)
        before_sha256 = _candidate_digest(path)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256)
            or before_sha256 != expected_candidate_sha256
        ):
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed since it was loaded"
            )
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
            if (
                field == "status"
                and value in PUBLISHABLE_STATUSES
                and not confirmed
            ):
                raise ValueError(
                    "Publishable claim status requires explicit user confirmation"
                )
            claims[claim_id][field] = value
        errors = validate_candidate(candidate)
        if errors:
            raise ValueError(format_errors(errors))
        updated_claim_ids = sorted({str(item["claim_id"]) for item in operations})
        after_sha256, transaction_id = _commit_candidate_profile_locked(
            path,
            expected_candidate_sha256,
            before_sha256,
            candidate,
            "patch_candidate_claims",
            {
                "claim_ids": updated_claim_ids,
                "fields": sorted({str(item["field"]) for item in operations}),
                "explicitly_confirmed": confirmed,
                "confirmation_origin": "explicit_local_user_action"
                if confirmed
                else "not_required",
            },
        )
        return {
            "status": "updated",
            "updated_claim_ids": updated_claim_ids,
            "candidate_sha256": after_sha256,
            "transaction_id": transaction_id,
            "recovered_transaction_ids": recovered,
            "history_recorded": True,
        }


def add_import_proposals(
    candidate_path: str | Path,
    proposals: list[dict[str, Any]],
    confirmed: bool,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    if not confirmed:
        raise ValueError("Import proposals require explicit user confirmation")
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        before_sha256 = _candidate_digest(path)
        recovered = _recover_incomplete_intents(path, before_sha256)
        before_sha256 = _candidate_digest(path)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256)
            or before_sha256 != expected_candidate_sha256
        ):
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed since it was loaded"
            )
        candidate = load_yaml(path)
        known = {
            claim.get("id")
            for claim in candidate.get("claims", [])
            if isinstance(claim, dict)
        }
        added: list[str] = []
        proposal_hashes: set[str] = set()
        for proposal in proposals:
            claim_id = str(proposal.get("id", ""))
            statement = str(proposal.get("statement", "")).strip()
            sha256 = str(proposal.get("sha256", ""))
            if (
                not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", claim_id)
                or claim_id in known
            ):
                raise ValueError(f"Invalid or duplicate claim id: {claim_id!r}")
            if not statement or not re.fullmatch(r"[a-f0-9]{64}", sha256):
                raise ValueError(
                    "Import proposal requires statement and sha256 provenance"
                )
            proposal_hashes.add(sha256)
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
        after_sha256, transaction_id = _commit_candidate_profile_locked(
            path,
            expected_candidate_sha256,
            before_sha256,
            candidate,
            "add_candidate_import_proposals",
            {
                "claim_ids": sorted(added),
                "proposal_sha256_values": sorted(proposal_hashes),
                "explicitly_confirmed": True,
                "confirmation_origin": "explicit_local_user_action",
            },
        )
        return {
            "status": "added_unverified",
            "added_claim_ids": added,
            "candidate_sha256": after_sha256,
            "transaction_id": transaction_id,
            "recovered_transaction_ids": recovered,
            "history_recorded": True,
        }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safe candidate profile read and patch contract"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    show = subparsers.add_parser("show")
    show.add_argument("--candidate", required=True)
    evidence = subparsers.add_parser("evidence")
    evidence.add_argument("--candidate", required=True)
    evidence.add_argument("--output-type", required=True, choices=sorted(OUTPUT_TYPES))
    patch = subparsers.add_parser("patch")
    patch.add_argument("--candidate", required=True)
    patch.add_argument("--operations", required=True)
    patch.add_argument("--confirmed", action="store_true")
    patch.add_argument("--expected-candidate-sha256", required=True)
    add = subparsers.add_parser("add-import")
    add.add_argument("--candidate", required=True)
    add.add_argument("--proposals", required=True)
    add.add_argument("--confirmed", action="store_true")
    add.add_argument("--expected-candidate-sha256", required=True)
    args = parser.parse_args()
    try:
        if args.command == "show":
            result = profile_summary(args.candidate)
        elif args.command == "evidence":
            result = profile_evidence(args.candidate, args.output_type)
        elif args.command == "patch":
            operations = json.loads(Path(args.operations).read_text(encoding="utf-8"))
            if not isinstance(operations, list):
                raise ValueError("Operations must be a JSON list")
            result = apply_claim_patch(
                args.candidate,
                operations,
                args.confirmed,
                args.expected_candidate_sha256,
            )
        else:
            proposals = json.loads(Path(args.proposals).read_text(encoding="utf-8"))
            if not isinstance(proposals, list):
                raise ValueError("Proposals must be a JSON list")
            result = add_import_proposals(
                args.candidate,
                proposals,
                args.confirmed,
                args.expected_candidate_sha256,
            )
    except ImportContractError as exc:
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "error": {"code": exc.code, "safe_detail": exc.detail},
                },
                ensure_ascii=False,
            )
        )
        return 2
    except (OSError, ValueError, json.JSONDecodeError):
        print(
            json.dumps(
                {
                    "status": "rejected",
                    "error": {
                        "code": "candidate_profile_mutation_rejected",
                        "safe_detail": "Candidate profile request is invalid",
                    },
                },
                ensure_ascii=False,
            )
        )
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
