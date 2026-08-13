from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

try:
    from .common import format_errors, load_yaml, validate_style
    from .audit_claims import EVIDENCE_PATTERN
except ImportError:  # Direct script execution.
    from common import format_errors, load_yaml, validate_style
    from audit_claims import EVIDENCE_PATTERN


WORD_PATTERN = re.compile(r"\b[\wÄÖÜäöüß-]+\b", re.UNICODE)
SENTENCE_PATTERN = re.compile(r"[^.!?\n]+[.!?]?", re.UNICODE)


def check_style(style: dict, document: str, document_type: str) -> list[str]:
    errors = [f"style profile: {item}" for item in validate_style(style)]
    settings = style.get("document_styles", {}).get(document_type)
    if not isinstance(settings, dict):
        errors.append(f"document type {document_type!r} is not configured")
        return errors

    clean = EVIDENCE_PATTERN.sub("", document)
    folded = clean.casefold()
    avoid_patterns = style.get("style_profile", {}).get("avoid_patterns", [])
    matches: list[str] = []
    for pattern in avoid_patterns:
        if str(pattern).casefold() in folded:
            matches.append(str(pattern))
    allowed_matches = style.get("quality_thresholds", {}).get("max_avoid_pattern_matches", 0)
    if len(matches) > allowed_matches:
        errors.append(f"avoid patterns found: {', '.join(matches)}")

    maximum = settings.get("max_sentence_words", 30)
    sentence_starts: list[str] = []
    for sentence in SENTENCE_PATTERN.findall(clean):
        words = WORD_PATTERN.findall(sentence)
        if not words:
            continue
        if len(words) > maximum:
            preview = " ".join(words[:8])
            errors.append(f"sentence exceeds {maximum} words ({len(words)}): {preview}...")
        first = words[0].casefold()
        if first not in {"und", "oder", "aber"}:
            sentence_starts.append(first)

    allowed_repeats = style.get("quality_thresholds", {}).get("max_repeated_sentence_starts", 2)
    repeated = sorted(word for word, count in Counter(sentence_starts).items() if count > allowed_repeats)
    if repeated:
        errors.append(f"repeated sentence starts exceed limit: {', '.join(repeated)}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Check application text against a style profile")
    parser.add_argument("--style", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--document-type", required=True, choices=["cv", "cover_letter", "email", "linkedin"])
    args = parser.parse_args()
    try:
        style = load_yaml(args.style)
        document = Path(args.document).read_text(encoding="utf-8")
    except (ValueError, OSError) as exc:
        print(f"ERROR: {exc}")
        return 1
    errors = check_style(style, document, args.document_type)
    if errors:
        print(format_errors(errors))
        return 1
    print("Style check passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
