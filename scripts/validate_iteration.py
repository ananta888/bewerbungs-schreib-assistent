from __future__ import annotations

import argparse

try:
    from .common import duplicate_values, format_errors, load_yaml, validate_id
except ImportError:  # Direct script execution.
    from common import duplicate_values, format_errors, load_yaml, validate_id


ROLE_SEQUENCES = {
    "compact": ["author", "combined_reviewer", "finalizer"],
    "standard": ["author", "evidence_ats_reviewer", "recruiter_style_reviewer", "finalizer"],
    "rigorous": ["author", "evidence_reviewer", "ats_reviewer", "recruiter_style_reviewer", "finalizer"],
}
SEVERITIES = {"critical", "high", "medium", "low"}
CATEGORIES = {"evidence", "ats", "recruiter", "style", "language", "structure"}
FINDING_STATUSES = {"open", "resolved", "accepted_risk", "rejected"}


def validate_iteration(data: dict) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != 1:
        errors.append("schema_version: expected 1")
    mode = data.get("mode")
    if mode not in ROLE_SEQUENCES:
        errors.append(f"mode: expected one of {sorted(ROLE_SEQUENCES)}")
    execution = data.get("execution")
    if execution not in {"independent_agents", "sequential_single_agent"}:
        errors.append("execution: expected independent_agents or sequential_single_agent")
    cycle = data.get("cycle")
    if not isinstance(cycle, int) or cycle < 1:
        errors.append("cycle: expected a positive integer")

    passes = data.get("passes")
    if not isinstance(passes, list):
        errors.append("passes: expected a list")
        return errors
    roles = [item.get("role") for item in passes if isinstance(item, dict)]
    if mode in ROLE_SEQUENCES and roles != ROLE_SEQUENCES[mode]:
        errors.append(f"passes: role order for {mode!r} must be {ROLE_SEQUENCES[mode]}")

    pass_ids: list[str] = []
    finding_ids: list[str] = []
    previous_output: str | None = None
    for index, review_pass in enumerate(passes):
        prefix = f"passes[{index}]"
        if not isinstance(review_pass, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        pass_id = validate_id(review_pass.get("id"), f"{prefix}.id", errors)
        if pass_id:
            pass_ids.append(pass_id)
        if execution == "independent_agents" and review_pass.get("independent_context") is not True:
            errors.append(f"{prefix}.independent_context: must be true for independent_agents")
        input_revision = review_pass.get("input_revision")
        output_revision = review_pass.get("output_revision")
        if not isinstance(input_revision, str) or not input_revision:
            errors.append(f"{prefix}.input_revision: required")
        if not isinstance(output_revision, str) or not output_revision:
            errors.append(f"{prefix}.output_revision: required")
        if previous_output is not None and input_revision != previous_output:
            errors.append(f"{prefix}.input_revision: expected {previous_output!r}")
        previous_output = output_revision if isinstance(output_revision, str) else None

        findings = review_pass.get("findings")
        if not isinstance(findings, list):
            errors.append(f"{prefix}.findings: expected a list")
            continue
        for finding_index, finding in enumerate(findings):
            finding_prefix = f"{prefix}.findings[{finding_index}]"
            if not isinstance(finding, dict):
                errors.append(f"{finding_prefix}: expected a mapping")
                continue
            finding_id = validate_id(finding.get("id"), f"{finding_prefix}.id", errors)
            if finding_id:
                finding_ids.append(finding_id)
            severity = finding.get("severity")
            status = finding.get("status")
            if severity not in SEVERITIES:
                errors.append(f"{finding_prefix}.severity: invalid value")
            if finding.get("category") not in CATEGORIES:
                errors.append(f"{finding_prefix}.category: invalid value")
            if not str(finding.get("description", "")).strip():
                errors.append(f"{finding_prefix}.description: required")
            if not isinstance(finding.get("evidence_refs"), list):
                errors.append(f"{finding_prefix}.evidence_refs: expected a list")
            if status not in FINDING_STATUSES:
                errors.append(f"{finding_prefix}.status: invalid value")
            if severity in {"critical", "high"} and status not in {"resolved", "accepted_risk"}:
                errors.append(f"{finding_prefix}: critical/high finding is unresolved")
            if status in {"resolved", "accepted_risk", "rejected"} and not str(finding.get("disposition", "")).strip():
                errors.append(f"{finding_prefix}.disposition: required for status {status!r}")

    for pass_id in sorted(duplicate_values(pass_ids)):
        errors.append(f"passes: duplicate id {pass_id!r}")
    for finding_id in sorted(duplicate_values(finding_ids)):
        errors.append(f"findings: duplicate id {finding_id!r}")
    if passes:
        final_pass = passes[-1]
        if not isinstance(final_pass, dict) or final_pass.get("output_revision") != "final":
            errors.append("passes: finalizer must output revision 'final'")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a multi-role application review manifest")
    parser.add_argument("--manifest", required=True)
    args = parser.parse_args()
    try:
        data = load_yaml(args.manifest)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    errors = validate_iteration(data)
    if errors:
        print(format_errors(errors))
        return 1
    print("Iteration manifest is valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
