from __future__ import annotations

import argparse
import re
from pathlib import Path

try:
    from .common import PUBLISHABLE_STATUSES, VALID_OUTPUTS, format_errors, load_yaml, validate_candidate
except ImportError:  # Direct script execution.
    from common import PUBLISHABLE_STATUSES, VALID_OUTPUTS, format_errors, load_yaml, validate_candidate


EVIDENCE_PATTERN = re.compile(r"<!--\s*evidence:\s*([^>]+?)\s*-->", re.IGNORECASE)
COMMENT_ONLY_PATTERN = re.compile(r"^\s*<!--.*-->\s*$")
STRUCTURAL_HEADINGS = {
    "berufserfahrung",
    "professional experience",
    "kurzprofil",
    "professional summary",
    "skills",
    "kenntnisse",
    "projekte",
    "projects",
    "ausbildung",
    "education",
    "zertifizierungen",
    "certifications",
    "sprachen",
    "languages",
}


def evidence_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in EVIDENCE_PATTERN.finditer(text):
        tokens.extend(item.strip() for item in match.group(1).split(",") if item.strip())
    return tokens


def requires_evidence(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped in {"---", "***"}:
        return False
    if COMMENT_ONLY_PATTERN.fullmatch(stripped):
        return False
    if stripped.startswith("#"):
        heading = EVIDENCE_PATTERN.sub("", stripped).lstrip("#").strip().casefold()
        return heading not in STRUCTURAL_HEADINGS
    return True


def profile_claims(candidate: dict) -> dict[str, dict]:
    profile = candidate.get("profile", {})
    synthetic: dict[str, dict] = {}
    fields = {
        "profile-full-name": profile.get("full_name"),
        "profile-location": profile.get("location"),
        "profile-work-authorization": profile.get("work_authorization"),
        "profile-availability": profile.get("availability"),
        "profile-target-title": profile.get("target_titles"),
    }
    contact = profile.get("contact", {}) if isinstance(profile.get("contact"), dict) else {}
    for key in ("email", "phone", "linkedin", "github", "portfolio"):
        fields[f"profile-{key}"] = contact.get(key)
    for claim_id, value in fields.items():
        if value not in (None, "", []):
            synthetic[claim_id] = {
                "id": claim_id,
                "status": "user_confirmed",
                "allowed_outputs": sorted(VALID_OUTPUTS),
            }
    return synthetic


def contains_term(document: str, term: str) -> bool:
    escaped = re.escape(term.casefold())
    if re.fullmatch(r"[a-z0-9]+", term.casefold()):
        return re.search(rf"(?<!\w){escaped}(?!\w)", document.casefold()) is not None
    return escaped in document.casefold()


def audit(candidate: dict, document: str, output_type: str, strict: bool = False) -> list[str]:
    errors = [f"candidate profile: {item}" for item in validate_candidate(candidate)]
    if output_type not in VALID_OUTPUTS:
        errors.append(f"output type: expected one of {sorted(VALID_OUTPUTS)}")
        return errors

    claims = {
        claim["id"]: claim
        for claim in candidate.get("claims", [])
        if isinstance(claim, dict) and isinstance(claim.get("id"), str)
    }
    claims.update(profile_claims(candidate))
    forbidden = set(candidate.get("constraints", {}).get("do_not_claim", []))
    annotations = evidence_tokens(document)
    for token in annotations:
        if token == "editorial":
            continue
        claim = claims.get(token)
        if claim is None:
            errors.append(f"unknown claim annotation {token!r}")
            continue
        if token in forbidden or claim.get("status") == "do_not_use":
            errors.append(f"claim {token!r} is forbidden")
        if claim.get("status") not in PUBLISHABLE_STATUSES:
            errors.append(f"claim {token!r} has non-publishable status {claim.get('status')!r}")
        if output_type not in claim.get("allowed_outputs", []):
            errors.append(f"claim {token!r} is not allowed in output {output_type!r}")

    constraints = candidate.get("constraints", {})
    blocked_terms = list(constraints.get("unsupported_or_weak_skills", []))
    blocked_terms.extend(item for item in forbidden if item not in claims)
    clean_document = EVIDENCE_PATTERN.sub("", document)
    for term in blocked_terms:
        if isinstance(term, str) and term and contains_term(clean_document, term):
            errors.append(f"unsupported or forbidden term appears in document: {term!r}")

    if strict:
        for number, line in enumerate(document.splitlines(), start=1):
            if requires_evidence(line) and not EVIDENCE_PATTERN.search(line):
                errors.append(f"line {number}: factual or editorial content lacks an evidence annotation")
    if not annotations:
        errors.append("document contains no evidence annotations")
    return errors


def strip_annotations(document: str) -> str:
    lines = [EVIDENCE_PATTERN.sub("", line).rstrip() for line in document.splitlines()]
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit annotated application text against candidate claims")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--output-type", required=True, choices=sorted(VALID_OUTPUTS))
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--strip-to", help="Write a clean document only if the audit passes")
    args = parser.parse_args()
    try:
        candidate = load_yaml(args.candidate)
        document = Path(args.document).read_text(encoding="utf-8")
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1

    errors = audit(candidate, document, args.output_type, args.strict)
    if errors:
        print(format_errors(errors))
        return 1
    if args.strip_to:
        Path(args.strip_to).write_text(strip_annotations(document), encoding="utf-8")
        print(f"Claim audit passed; wrote {args.strip_to}")
    else:
        print("Claim audit passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
