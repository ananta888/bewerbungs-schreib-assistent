from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Iterable

import yaml


VALID_STATUSES = {"verified", "user_confirmed", "inferred", "unverified", "do_not_use"}
PUBLISHABLE_STATUSES = {"verified", "user_confirmed"}
VALID_OUTPUTS = {"cv", "cover_letter", "email", "linkedin", "interview"}
ID_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
DATE_PATTERN = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def load_yaml(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    try:
        data = yaml.safe_load(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"File not found: {source}") from exc
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML in {source}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping in {source}")
    return data


def duplicate_values(values: Iterable[str]) -> set[str]:
    seen: set[str] = set()
    duplicates: set[str] = set()
    for value in values:
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return duplicates


def parse_date(value: Any, field: str, errors: list[str], allow_present: bool = True) -> date | None:
    if value in (None, ""):
        return None
    text = str(value)
    if allow_present and text == "present":
        return None
    match = DATE_PATTERN.fullmatch(text)
    if not match:
        errors.append(f"{field}: expected YYYY, YYYY-MM, YYYY-MM-DD, or present; got {text!r}")
        return None
    year, month, day = match.groups()
    try:
        return date(int(year), int(month or 1), int(day or 1))
    except ValueError:
        errors.append(f"{field}: invalid calendar date {text!r}")
        return None


def require_list(data: dict[str, Any], key: str, errors: list[str], prefix: str = "") -> list[Any]:
    value = data.get(key)
    field = f"{prefix}{key}"
    if not isinstance(value, list):
        errors.append(f"{field}: expected a list")
        return []
    return value


def validate_id(value: Any, field: str, errors: list[str]) -> str | None:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        errors.append(f"{field}: expected a lowercase hyphenated ID")
        return None
    return value


def validate_candidate(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != 2:
        errors.append("schema_version: expected 2")

    for key in ("profile", "preferences", "constraints"):
        if not isinstance(data.get(key), dict):
            errors.append(f"{key}: expected a mapping")

    sources = require_list(data, "sources", errors)
    source_ids: list[str] = []
    for index, source in enumerate(sources):
        prefix = f"sources[{index}]"
        if not isinstance(source, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        source_id = validate_id(source.get("id"), f"{prefix}.id", errors)
        if source_id:
            source_ids.append(source_id)
        parse_date(source.get("verified_at"), f"{prefix}.verified_at", errors, allow_present=False)
    for source_id in sorted(duplicate_values(source_ids)):
        errors.append(f"sources: duplicate id {source_id!r}")
    source_id_set = set(source_ids)

    claims = require_list(data, "claims", errors)
    claim_ids: list[str] = []
    claim_map: dict[str, dict[str, Any]] = {}
    for index, claim in enumerate(claims):
        prefix = f"claims[{index}]"
        if not isinstance(claim, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        claim_id = validate_id(claim.get("id"), f"{prefix}.id", errors)
        if claim_id:
            claim_ids.append(claim_id)
            claim_map[claim_id] = claim
        if not str(claim.get("statement", "")).strip():
            errors.append(f"{prefix}.statement: required")
        status = claim.get("status")
        if status not in VALID_STATUSES:
            errors.append(f"{prefix}.status: expected one of {sorted(VALID_STATUSES)}")
        evidence_refs = claim.get("evidence_refs", [])
        if not isinstance(evidence_refs, list):
            errors.append(f"{prefix}.evidence_refs: expected a list")
            evidence_refs = []
        for ref in evidence_refs:
            if ref not in source_id_set:
                errors.append(f"{prefix}.evidence_refs: unknown source {ref!r}")
        if status == "verified" and not evidence_refs:
            errors.append(f"{prefix}.evidence_refs: verified claims require evidence")
        outputs = claim.get("allowed_outputs", [])
        if not isinstance(outputs, list) or not outputs:
            errors.append(f"{prefix}.allowed_outputs: expected a non-empty list")
        else:
            for output in outputs:
                if output not in VALID_OUTPUTS:
                    errors.append(f"{prefix}.allowed_outputs: unknown output {output!r}")
        start = parse_date(claim.get("valid_from"), f"{prefix}.valid_from", errors)
        end_value = claim.get("valid_to")
        end = parse_date(end_value, f"{prefix}.valid_to", errors)
        if start and end and start > end:
            errors.append(f"{prefix}: valid_from is after valid_to")
    for claim_id in sorted(duplicate_values(claim_ids)):
        errors.append(f"claims: duplicate id {claim_id!r}")
    claim_id_set = set(claim_ids)

    record_sections = ("experience", "projects", "education", "certifications")
    record_ids: list[str] = []
    for section in record_sections:
        records = require_list(data, section, errors)
        for index, record in enumerate(records):
            prefix = f"{section}[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            record_id = validate_id(record.get("id"), f"{prefix}.id", errors)
            if record_id:
                record_ids.append(record_id)
            _validate_status(record, prefix, errors)
            _validate_refs(record, prefix, "evidence_refs", source_id_set, errors)
            if record.get("status") == "verified" and not record.get("evidence_refs"):
                errors.append(f"{prefix}.evidence_refs: verified records require evidence")
            _validate_refs(record, prefix, "claim_ids", claim_id_set, errors)
            start = parse_date(record.get("start_date"), f"{prefix}.start_date", errors)
            end = parse_date(record.get("end_date"), f"{prefix}.end_date", errors)
            if start and end and start > end:
                errors.append(f"{prefix}: start_date is after end_date")
            if section == "certifications":
                issue = parse_date(record.get("issue_date"), f"{prefix}.issue_date", errors)
                expiry = parse_date(record.get("expiry_date"), f"{prefix}.expiry_date", errors)
                if issue and expiry and issue > expiry:
                    errors.append(f"{prefix}: issue_date is after expiry_date")
    for record_id in sorted(duplicate_values(record_ids)):
        errors.append(f"records: duplicate id {record_id!r}")

    for section in ("skills", "languages"):
        records = require_list(data, section, errors)
        for index, record in enumerate(records):
            prefix = f"{section}[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{prefix}: expected a mapping")
                continue
            _validate_status(record, prefix, errors)
            _validate_refs(record, prefix, "evidence_refs", source_id_set, errors, optional=True)
            _validate_refs(record, prefix, "claim_ids", claim_id_set, errors)

    constraints = data.get("constraints")
    if isinstance(constraints, dict):
        for key in (
            "do_not_claim",
            "unsupported_or_weak_skills",
            "sensitive_topics_to_avoid",
            "facts_requiring_user_confirmation",
        ):
            if not isinstance(constraints.get(key), list):
                errors.append(f"constraints.{key}: expected a list")

    return errors


def _validate_status(record: dict[str, Any], prefix: str, errors: list[str]) -> None:
    status = record.get("status")
    if status not in VALID_STATUSES:
        errors.append(f"{prefix}.status: expected one of {sorted(VALID_STATUSES)}")


def _validate_refs(
    record: dict[str, Any],
    prefix: str,
    key: str,
    valid_ids: set[str],
    errors: list[str],
    optional: bool = False,
) -> None:
    if optional and key not in record:
        return
    refs = record.get(key, [])
    if not isinstance(refs, list):
        errors.append(f"{prefix}.{key}: expected a list")
        return
    for ref in refs:
        if ref not in valid_ids:
            errors.append(f"{prefix}.{key}: unknown reference {ref!r}")


def validate_style(data: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if data.get("schema_version") != 2:
        errors.append("schema_version: expected 2")
    profile = data.get("style_profile")
    if not isinstance(profile, dict):
        errors.append("style_profile: expected a mapping")
        profile = {}
    for key in ("language", "locale", "tone", "formality"):
        if not str(profile.get(key, "")).strip():
            errors.append(f"style_profile.{key}: required")
    for key in ("preferred_patterns", "avoid_patterns"):
        if not isinstance(profile.get(key), list):
            errors.append(f"style_profile.{key}: expected a list")
    avoid = profile.get("avoid_patterns", [])
    if isinstance(avoid, list):
        normalized = [str(item).casefold().strip() for item in avoid]
        for duplicate in sorted(duplicate_values(normalized)):
            errors.append(f"style_profile.avoid_patterns: duplicate {duplicate!r}")

    document_styles = data.get("document_styles")
    if not isinstance(document_styles, dict):
        errors.append("document_styles: expected a mapping")
    else:
        for document_type in ("cv", "cover_letter", "email", "linkedin"):
            settings = document_styles.get(document_type)
            if not isinstance(settings, dict):
                errors.append(f"document_styles.{document_type}: expected a mapping")
                continue
            maximum = settings.get("max_sentence_words")
            if not isinstance(maximum, int) or maximum < 10:
                errors.append(f"document_styles.{document_type}.max_sentence_words: expected integer >= 10")

    levels = data.get("personalization_levels")
    if not isinstance(levels, dict):
        errors.append("personalization_levels: expected a mapping")
    else:
        if levels.get("default") not in {"conservative", "professional", "personal"}:
            errors.append("personalization_levels.default: invalid mode")
        for mode in ("conservative", "professional", "personal"):
            if not isinstance(levels.get(mode), dict):
                errors.append(f"personalization_levels.{mode}: expected a mapping")

    thresholds = data.get("quality_thresholds")
    if not isinstance(thresholds, dict):
        errors.append("quality_thresholds: expected a mapping")
    else:
        for key in ("max_repeated_sentence_starts", "max_avoid_pattern_matches"):
            if not isinstance(thresholds.get(key), int) or thresholds[key] < 0:
                errors.append(f"quality_thresholds.{key}: expected a non-negative integer")

    workflow = data.get("review_workflow")
    if not isinstance(workflow, dict):
        errors.append("review_workflow: expected a mapping")
    else:
        if workflow.get("default_mode") not in {"compact", "standard", "rigorous"}:
            errors.append("review_workflow.default_mode: invalid mode")
        cycles = workflow.get("max_revision_cycles")
        if not isinstance(cycles, int) or not 1 <= cycles <= 5:
            errors.append("review_workflow.max_revision_cycles: expected integer from 1 to 5")
        if not isinstance(workflow.get("prefer_independent_agents"), bool):
            errors.append("review_workflow.prefer_independent_agents: expected boolean")

    language_quality = data.get("language_quality")
    if not isinstance(language_quality, dict):
        errors.append("language_quality: expected a mapping")
    else:
        if language_quality.get("primary_backend") not in {"languagetool", "hunspell"}:
            errors.append("language_quality.primary_backend: expected languagetool or hunspell")
        if not str(language_quality.get("language", "")).strip():
            errors.append("language_quality.language: required")
        if not isinstance(language_quality.get("allow_remote_service"), bool):
            errors.append("language_quality.allow_remote_service: expected boolean")
        if not isinstance(language_quality.get("allowlist"), list):
            errors.append("language_quality.allowlist: expected a list")
    return errors


def format_errors(errors: list[str]) -> str:
    return "\n".join(f"ERROR: {error}" for error in errors)
