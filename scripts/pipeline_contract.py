from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1.0"
STAGES = [
    "validate_profiles",
    "analyze_job",
    "build_match_matrix",
    "draft",
    "review",
    "audit",
    "finalize",
]
ARTIFACT_ORDER = [
    "job-analysis.yaml",
    "match-matrix.yaml",
    "annotated.md",
    "iteration.yaml",
    "final.md",
]


def capabilities() -> dict[str, Any]:
    return {
        "contract": "bewerbungs-pipeline",
        "contract_version": CONTRACT_VERSION,
        "compatible_major_versions": [1],
        "stages": STAGES,
        "document_types": ["cv", "cover_letter", "email", "linkedin", "interview"],
        "finding_severities": ["low", "medium", "high", "critical"],
        "blocking_severities": ["high", "critical"],
        "review_roles": [
            "author",
            "evidence_ats_reviewer",
            "recruiter_style_reviewer",
            "finalizer",
        ],
        "publishable_claim_statuses": ["verified", "user_confirmed"],
        "transport": "local_cli_and_artifacts",
        "network_required": False,
    }


def pipeline_status(work_directory: str | Path, run_id: str) -> dict[str, Any]:
    root = Path(work_directory)
    artifacts = [name for name in ARTIFACT_ORDER if (root / name).is_file()]
    state_by_artifact = {
        "job-analysis.yaml": "analysis",
        "match-matrix.yaml": "matching",
        "annotated.md": "draft",
        "iteration.yaml": "review",
        "final.md": "final",
    }
    state = state_by_artifact[artifacts[-1]] if artifacts else "created"
    return {
        "contract": "bewerbungs-pipeline",
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "state": state,
        "artifacts": artifacts,
    }


def finalize_pipeline(
    candidate: str,
    style: str,
    document: str,
    manifest: str,
    output_type: str,
    final_document: str,
) -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    commands = [
        ["validate_profiles.py", "--candidate", candidate, "--style", style],
        ["validate_iteration.py", "--manifest", manifest],
        [
            "audit_claims.py",
            "--candidate",
            candidate,
            "--document",
            document,
            "--output-type",
            output_type,
            "--strict",
        ],
        [
            "check_style.py",
            "--style",
            style,
            "--document",
            document,
            "--document-type",
            output_type,
        ],
        [
            "audit_claims.py",
            "--candidate",
            candidate,
            "--document",
            document,
            "--output-type",
            output_type,
            "--strict",
            "--strip-to",
            final_document,
        ],
    ]
    completed: list[str] = []
    for command in commands:
        process = subprocess.run(
            [sys.executable, str(root / command[0]), *command[1:]],
            capture_output=True,
            text=True,
            check=False,
        )
        if process.returncode:
            return {
                "contract": "bewerbungs-pipeline",
                "contract_version": CONTRACT_VERSION,
                "status": "rejected",
                "failed_stage": command[0].removesuffix(".py"),
                "error": {
                    "code": "policy_gate_failed",
                    "safe_detail": (
                        "Die lokale Pipeline-Prüfung wurde abgelehnt; "
                        "Eingaben und zugehörige Artefakte prüfen."
                    ),
                },
                "completed_stages": completed,
            }
        completed.append(command[0].removesuffix(".py"))
    return {
        "contract": "bewerbungs-pipeline",
        "contract_version": CONTRACT_VERSION,
        "status": "final",
        "artifact": {"kind": "final_document", "path": str(Path(final_document).name)},
        "findings": [],
        "completed_stages": completed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Machine-readable application pipeline contract"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("capabilities")
    status = subparsers.add_parser("status")
    status.add_argument("--work-directory", required=True)
    status.add_argument("--run-id", required=True)
    finalize = subparsers.add_parser("finalize")
    finalize.add_argument("--candidate", required=True)
    finalize.add_argument("--style", required=True)
    finalize.add_argument("--document", required=True)
    finalize.add_argument("--manifest", required=True)
    finalize.add_argument("--output-type", required=True)
    finalize.add_argument("--final-document", required=True)
    args = parser.parse_args()
    if args.command == "capabilities":
        result = capabilities()
    elif args.command == "status":
        result = pipeline_status(args.work_directory, args.run_id)
    else:
        result = finalize_pipeline(
            args.candidate,
            args.style,
            args.document,
            args.manifest,
            args.output_type,
            args.final_document,
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
