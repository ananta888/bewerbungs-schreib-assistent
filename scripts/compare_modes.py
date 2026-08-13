from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

try:
    from .audit_claims import evidence_tokens
except ImportError:  # Direct script execution.
    from audit_claims import evidence_tokens


def claim_counter(document: str) -> Counter[str]:
    return Counter(token for token in evidence_tokens(document) if token != "editorial")


def compare_documents(paths: list[Path], exact_counts: bool = False) -> list[str]:
    errors: list[str] = []
    documents = [path.read_text(encoding="utf-8") for path in paths]
    counters = [claim_counter(document) for document in documents]
    baseline = counters[0]
    for path, current in zip(paths[1:], counters[1:]):
        if exact_counts:
            equal = current == baseline
        else:
            equal = set(current) == set(baseline)
        if not equal:
            missing = sorted(set(baseline) - set(current))
            added = sorted(set(current) - set(baseline))
            errors.append(f"{path}: evidence differs; missing={missing}, added={added}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Ensure tone variants use identical factual evidence")
    parser.add_argument("documents", nargs="+", type=Path)
    parser.add_argument("--exact-counts", action="store_true")
    args = parser.parse_args()
    if len(args.documents) < 2:
        parser.error("provide at least two documents")
    try:
        errors = compare_documents(args.documents, args.exact_counts)
    except OSError as exc:
        print(f"ERROR: {exc}")
        return 1
    if errors:
        print(format_errors(errors))
        return 1
    print("Personalization modes use identical factual evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
