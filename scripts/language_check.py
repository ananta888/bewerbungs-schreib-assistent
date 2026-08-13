from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

try:
    from .audit_claims import EVIDENCE_PATTERN
except ImportError:  # Direct script execution.
    from audit_claims import EVIDENCE_PATTERN


LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def plain_text(document: str) -> str:
    text = EVIDENCE_PATTERN.sub("", document)
    text = re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s{0,3}(?:#{1,6}|[-*+])\s+", "", text, flags=re.MULTILINE)
    text = text.replace("`", "")
    return text


def load_allowlist(path: str | None, words: list[str]) -> set[str]:
    allowed = {word.strip().casefold() for word in words if word.strip()}
    if path:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            word = line.strip()
            if word and not word.startswith("#"):
                allowed.add(word.casefold())
    return allowed


def is_remote_server(server: str) -> bool:
    hostname = urllib.parse.urlparse(server).hostname
    return hostname not in LOCAL_HOSTS


def check_languagetool(
    document: str,
    language: str,
    server: str,
    allow_remote: bool = False,
    allowlist: set[str] | None = None,
    timeout: float = 15.0,
) -> list[dict[str, Any]]:
    if is_remote_server(server) and not allow_remote:
        raise ValueError("Refusing to send application text to a remote LanguageTool server without --allow-remote")
    text = plain_text(document)
    payload = urllib.parse.urlencode({"text": text, "language": language}).encode("utf-8")
    request = urllib.request.Request(server, data=payload, method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.loads(response.read().decode("utf-8"))
    matches = result.get("matches", [])
    if not isinstance(matches, list):
        raise ValueError("LanguageTool returned an invalid response")

    allowed = allowlist or set()
    issues: list[dict[str, Any]] = []
    for match in matches:
        if not isinstance(match, dict):
            continue
        offset = int(match.get("offset", 0))
        length = int(match.get("length", 0))
        matched_text = text[offset : offset + length]
        if matched_text.casefold() in allowed:
            continue
        replacements = match.get("replacements", [])
        issues.append(
            {
                "backend": "languagetool",
                "message": match.get("message", "Language issue"),
                "text": matched_text,
                "offset": offset,
                "length": length,
                "suggestions": [item.get("value") for item in replacements[:5] if isinstance(item, dict)],
                "rule_id": match.get("rule", {}).get("id") if isinstance(match.get("rule"), dict) else None,
            }
        )
    return issues


def check_hunspell(
    document: str,
    dictionary: str,
    allowlist: set[str] | None = None,
    executable: str = "hunspell",
) -> list[dict[str, Any]]:
    binary = shutil.which(executable)
    if not binary:
        raise ValueError(f"Hunspell executable not found: {executable}")
    process = subprocess.run(
        [binary, "-d", dictionary, "-l"],
        input=plain_text(document),
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip() or f"exit code {process.returncode}"
        raise ValueError(f"Hunspell failed: {detail}")
    allowed = allowlist or set()
    words = sorted({line.strip() for line in process.stdout.splitlines() if line.strip()}, key=str.casefold)
    return [
        {"backend": "hunspell", "message": "Word not found in dictionary", "text": word, "suggestions": []}
        for word in words
        if word.casefold() not in allowed
    ]


def print_issues(issues: list[dict[str, Any]], output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"issue_count": len(issues), "issues": issues}, ensure_ascii=False, indent=2))
        return
    for issue in issues:
        suggestions = ", ".join(issue.get("suggestions", [])) or "none"
        print(f"{issue['backend']}: {issue['text']!r}: {issue['message']} (suggestions: {suggestions})")
    print(f"Language issues: {len(issues)}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a classical spelling and grammar check")
    parser.add_argument("--backend", choices=["languagetool", "hunspell"], required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--language", default="auto")
    parser.add_argument("--server", default="http://localhost:8010/v2/check")
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--dictionary", default="de_DE")
    parser.add_argument("--allowlist")
    parser.add_argument("--allow-word", action="append", default=[])
    parser.add_argument("--max-issues", type=int, default=0)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    args = parser.parse_args()
    try:
        document = Path(args.document).read_text(encoding="utf-8")
        allowed = load_allowlist(args.allowlist, args.allow_word)
        if args.backend == "languagetool":
            issues = check_languagetool(document, args.language, args.server, args.allow_remote, allowed)
        else:
            issues = check_hunspell(document, args.dictionary, allowed)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}")
        return 2
    print_issues(issues, args.format)
    return 1 if len(issues) > args.max_issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
