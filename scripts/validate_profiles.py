from __future__ import annotations

import argparse

try:
    from .common import format_errors, load_yaml, validate_candidate, validate_style
except ImportError:  # Direct script execution.
    from common import format_errors, load_yaml, validate_candidate, validate_style


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate application profile schemas and references")
    parser.add_argument("--candidate", required=True)
    parser.add_argument("--style", required=True)
    args = parser.parse_args()
    try:
        candidate = load_yaml(args.candidate)
        style = load_yaml(args.style)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1

    errors = [f"candidate: {item}" for item in validate_candidate(candidate)]
    errors.extend(f"style: {item}" for item in validate_style(style))
    if errors:
        print(format_errors(errors))
        return 1
    print("Profiles are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
