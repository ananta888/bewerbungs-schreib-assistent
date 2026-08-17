from __future__ import annotations

import argparse
import copy
import hashlib
import html
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, ClassVar
from xml.etree import ElementTree

import yaml

try:
    from .common import load_yaml, validate_candidate
except ImportError:  # Direct script execution.
    from common import load_yaml, validate_candidate

CONTRACT = "cv-import-proposal"
CONTRACT_VERSION = "1.0"
SCHEMA_VERSION = 1
AI_STRUCTURE_CONTRACT = "ai-cv-structure-proposal"
AI_STRUCTURE_CONTRACT_VERSION = "1.0"
AI_VALIDATED_CONTRACT = "validated-ai-cv-structure-proposal"
AI_VALIDATION_REQUEST_CONTRACT = "ai-cv-structure-validation-request"
AI_APPLY_REQUEST_CONTRACT = "ai-cv-structure-apply-request"
AI_MATERIALIZATION_REQUEST_CONTRACT = "ai-cv-structure-materialization-request"
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_CONTRACT_STDIN_BYTES = 16 * 1024 * 1024
MAX_AI_COLLECTION_ITEMS = 250
MAX_AI_ALTERNATIVES = 10
MAX_AI_QUESTIONS = 10
MAX_AI_MATERIALIZED_FACTS = 2_000
MAX_ARCHIVE_ENTRIES = 512
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 20 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
PDF_TIMEOUT_SECONDS = 20
PROFILE_LOCK_WAIT_SECONDS = 5.0
PROFILE_LOCK_STALE_SECONDS = 300.0
MAX_PROFILE_SNAPSHOTS = 50
MAX_PROFILE_SNAPSHOT_BYTES = 4 * 1024 * 1024
PROFILE_SNAPSHOT_LABEL_MAX = 120
ALLOWED_EXTENSIONS = {".html", ".htm", ".pdf", ".docx", ".odt"}
ALLOWED_MEDIA_TYPES = {
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".odt": "application/vnd.oasis.opendocument.text",
}
ACTIVE_PDF_MARKERS = (b"/JavaScript", b"/JS", b"/OpenAction", b"/Launch")
SECTION_ALIASES = {
    "experience": {
        "berufserfahrung",
        "beruflicher werdegang",
        "berufliche stationen",
        "erfahrung",
        "praxis",
        "experience",
        "employment",
        "work experience",
    },
    "education": {
        "ausbildung",
        "aus- und weiterbildung",
        "studium",
        "studium und ausbildung",
        "bildung",
        "education",
    },
    "skills": {"kenntnisse", "fähigkeiten", "skills", "technologien", "technologies"},
    "languages": {"sprachen", "languages"},
    "certifications": {"zertifikate", "certifications", "certificates"},
    "projects": {"projekte", "projects"},
}
DATE_TOKEN_PATTERN = (
    r"(?:\d{4}(?:[-/.](?:0?[1-9]|1[0-2]))?"
    r"|(?:0?[1-9]|1[0-2])[-/.]\d{4}"
    r"|heute|aktuell|present|current)"
)
DATE_RANGE = re.compile(
    rf"^(?P<start>{DATE_TOKEN_PATTERN})\s*(?:-|–|—|bis|to)\s*"
    rf"(?P<end>{DATE_TOKEN_PATTERN})\s*[:|,-]?\s*(?P<body>.+)$",
    re.IGNORECASE,
)
BULLET = re.compile(r"^[\s\-–—*•·▪◦]+")
BULLET_START = re.compile(r"^[\s]*[-–—*•·▪◦]\s+")
DOCUMENT_TITLES = {
    "lebenslauf", "curriculum vitae", "cv", "resume", "résumé", "bewerbung",
}
"""Two to four plain words: a person's name, not a label, address or heading."""
PERSON_NAME = re.compile(r"^[^\d@:/|,]+$")


def _looks_like_person_name(line: str) -> bool:
    if not PERSON_NAME.match(line):
        return False
    if line.casefold().strip() in DOCUMENT_TITLES:
        return False
    words = line.split()
    if not 2 <= len(words) <= 4:
        return False
    return all(word[:1].isalpha() and word[:1].isupper() for word in words)
INLINE_BULLET = re.compile(r"\s+[•·▪◦]\s+")
BARE_DATE_RANGE = re.compile(
    rf"^(?P<start>{DATE_TOKEN_PATTERN})\s*(?:-|–|—|bis|to)\s*(?P<end>{DATE_TOKEN_PATTERN})\s*$",
    re.IGNORECASE,
)
"""A line is a wrapped continuation when the previous one was cut mid-sentence."""
SENTENCE_END = re.compile(r"[.;:!?]$")


def _repair_wrapped_lines(entries: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Rejoins lines that a PDF broke mid-sentence.

    Without this a wrapped bullet ("… zur sicheren Orchestrierung" / "von
    KI-Agenten …") is indistinguishable from a new job heading, because both
    arrive as plain lines without a bullet marker.
    """
    repaired: list[tuple[int, str]] = []
    for line_no, line in entries:
        previous = repaired[-1][1] if repaired else ""
        continues = (
            bool(repaired)
            and not BULLET_START.match(line)
            and not DATE_RANGE.match(line)
            and not BARE_DATE_RANGE.match(line)
            and bool(previous)
            and not SENTENCE_END.search(previous)
        )
        if continues:
            repaired[-1] = (repaired[-1][0], f"{previous} {line.strip()}")
        else:
            repaired.append((line_no, line))
    return repaired


def _split_inline_bullets(line: str) -> list[str]:
    """Splits several bullet points that share one extracted line."""
    parts = INLINE_BULLET.split(line)
    return [part for part in (item.strip() for item in parts) if part]


class ImportContractError(ValueError):
    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class _SafeHtmlTextExtractor(HTMLParser):
    BLOCK_TAGS: ClassVar[set[str]] = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
    IGNORED_TAGS: ClassVar[set[str]] = {
        "head",
        "script",
        "style",
        "template",
        "noscript",
        "svg",
        "canvas",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0
        self.external_references = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.casefold()
        if tag in self.IGNORED_TAGS:
            self.ignored_depth += 1
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")
        for name, value in attrs:
            if (
                name.casefold() in {"src", "href", "action"}
                and value
                and re.match(r"^(?:https?:)?//", value.strip(), re.IGNORECASE)
            ):
                self.external_references += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag in self.IGNORED_TAGS and self.ignored_depth:
            self.ignored_depth -= 1
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def capabilities() -> dict[str, Any]:
    return {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "input_formats": sorted(set(ALLOWED_MEDIA_TYPES.values())),
        "max_input_bytes": MAX_INPUT_BYTES,
        "output_formats": ["application/yaml", "application/json"],
        "claim_status": "unverified",
        "commands": [
            "capabilities",
            "extract",
            "normalize-extracted",
            "extend-user-facts",
            "validate",
            "adopt-confirmed",
            "revoke-claims",
            "list-adoptions",
            "capture-profile-snapshot",
            "list-profile-snapshots",
            "restore-profile-snapshot",
            "recovery-status",
            "validate-ai-structure",
            "apply-ai-structure",
            "materialize-ai-structure",
        ],
        "pdf_backend": "pdftotext" if shutil.which("pdftotext") else "unavailable",
        "network_access": False,
        "macros_allowed": False,
        "user_fact_fields": {
            key: sorted(value) for key, value in USER_FACT_FIELDS.items()
        },
        "ai_structuring": {
            "contract": AI_STRUCTURE_CONTRACT,
            "contract_version": AI_STRUCTURE_CONTRACT_VERSION,
            "schema": "contracts/v1/ai-cv-structure-proposal.schema.json",
            "validation_request_contract": AI_VALIDATION_REQUEST_CONTRACT,
            "apply_request_contract": AI_APPLY_REQUEST_CONTRACT,
            "materialization_request_contract": AI_MATERIALIZATION_REQUEST_CONTRACT,
            "materialization_request_schema": (
                "contracts/v1/ai-cv-structure-materialization-request.schema.json"
            ),
            "materialization_output_contract": CONTRACT,
            "materialization_output_contract_version": CONTRACT_VERSION,
            "materialization_mode": "replace_recognition_version",
            "materialization_selection_policy": "all_mergeable_non_null_primary",
            "max_materialized_facts": MAX_AI_MATERIALIZED_FACTS,
            "preserved_deterministic_scopes": ["profile", "certifications"],
            "validated_contract": AI_VALIDATED_CONTRACT,
            "line_manifest_private": True,
            "network_access": False,
            "statuses": ["unverified"],
        },
    }


def _read_bounded(path: Path) -> bytes:
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ImportContractError(
            "input_unreadable", "Input file cannot be read"
        ) from exc
    if size <= 0:
        raise ImportContractError("input_empty", "Input file is empty")
    if size > MAX_INPUT_BYTES:
        raise ImportContractError(
            "input_too_large", f"Input exceeds {MAX_INPUT_BYTES} bytes"
        )
    return path.read_bytes()


MIN_COLUMN_GUTTER = 4
MIN_COLUMN_WIDTH = 12
MIN_COLUMN_SHARE = 0.12


def _page_columns(lines: list[str]) -> list[tuple[int, int]]:
    """Column spans of one page, detected from persistent vertical whitespace.

    A layout-preserved page keeps every glyph at its horizontal position, so a
    column boundary shows up as a run of character positions that is blank on
    *every* line. Returns a single span when nothing convincing is found, which
    keeps single-column documents on their previous behaviour.
    """
    width = max((len(line) for line in lines), default=0)
    if width < MIN_COLUMN_WIDTH * 2 + MIN_COLUMN_GUTTER:
        return [(0, width)]
    counts = [0] * width
    total = 0
    body = [line for line in lines if line.strip()]
    for line in body:
        for index, char in enumerate(line):
            if char != " ":
                counts[index] += 1
                total += 1
    if total == 0:
        return [(0, width)]
    # A gutter must be *almost* empty, not perfectly empty: a single overlong
    # line would otherwise erase an obvious column boundary.
    tolerance = max(1, int(len(body) * 0.03))
    occupied = [count > tolerance for count in counts]

    spans: list[tuple[int, int]] = []
    start: int | None = None
    for index in range(width):
        if occupied[index] and start is None:
            start = index
        elif not occupied[index] and start is not None:
            if index - start >= 1:
                spans.append((start, index))
            start = None
    if start is not None:
        spans.append((start, width))
    if not spans:
        return [(0, width)]

    # Merge spans separated by less than a full gutter; those are word gaps.
    merged: list[list[int]] = [list(spans[0])]
    for begin, end in spans[1:]:
        if begin - merged[-1][1] < MIN_COLUMN_GUTTER:
            merged[-1][1] = end
        else:
            merged.append([begin, end])
    columns = [(begin, end) for begin, end in merged if end - begin >= MIN_COLUMN_WIDTH]
    if len(columns) < 2:
        return [(0, width)]

    # Every column must carry a real share of the page, otherwise a stray
    # right-aligned date or page number would masquerade as a column.
    weights = []
    for begin, end in columns:
        weight = sum(
            1 for line in lines for char in line[begin:end] if char != " "
        )
        weights.append(weight)
    if any(weight < total * MIN_COLUMN_SHARE for weight in weights):
        return [(0, width)]
    return columns


def _reading_order(text: str) -> str:
    """Rewrites a layout-preserved extraction into single-column reading order.

    `pdftotext` emits a multi-column page line by line across all columns, so a
    sidebar and the main column arrive interleaved: a date lands between the
    previous entry's heading and its bullet list. Everything downstream is line
    based and single column, so the columns are separated here, before the
    leading whitespace that carries the layout is normalised away.
    """
    pages = text.replace("\r\n", "\n").replace("\r", "\n").split("\f")
    ordered: list[str] = []
    for page in pages:
        lines = page.split("\n")
        if not any(line.strip() for line in lines):
            continue
        columns = _page_columns(lines)
        for begin, end in columns:
            column = [line[begin:end].rstrip() for line in lines]
            if any(part.strip() for part in column):
                ordered.extend(column)
                ordered.append("")
    return "\n".join(ordered)


def _normalize_lines(text: str) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    if len(text) > MAX_TEXT_CHARS:
        raise ImportContractError(
            "extracted_text_too_large", "Extracted text exceeds the limit"
        )
    lines: list[str] = []
    for raw in text.split("\n"):
        line = re.sub(r"[\t\u00a0 ]+", " ", html.unescape(raw)).strip()
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    if not lines:
        raise ImportContractError("no_text", "No readable CV text was found")
    return lines


def _extract_html(data: bytes) -> tuple[list[str], str, list[dict[str, str]]]:
    head = data[:1024].lstrip().lower()
    if not (head.startswith((b"<!doctype html", b"<html")) or b"<body" in head):
        raise ImportContractError(
            "type_mismatch", "HTML signature does not match extension"
        )
    try:
        source = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportContractError(
            "invalid_encoding", "HTML input must be UTF-8"
        ) from exc
    parser = _SafeHtmlTextExtractor()
    parser.feed(source)
    warnings = []
    if parser.external_references:
        warnings.append(
            {
                "code": "external_references_ignored",
                "detail": f"Ignored {parser.external_references} external HTML reference(s); no network request was made",
            }
        )
    return _normalize_lines("".join(parser.parts)), "stdlib-html-parser", warnings


def _safe_archive(data: bytes, expected: str) -> zipfile.ZipFile:
    try:
        # Construct from memory so archive members never become filesystem paths.
        import io

        archive = zipfile.ZipFile(io.BytesIO(data))
    except (OSError, zipfile.BadZipFile) as exc:
        raise ImportContractError(
            "invalid_archive", "Office document is not a valid ZIP archive"
        ) from exc
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        archive.close()
        raise ImportContractError(
            "archive_limit", "Office archive contains too many entries"
        )
    total = sum(item.file_size for item in infos)
    if total > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
        archive.close()
        raise ImportContractError(
            "archive_limit", "Office archive expands beyond the safety limit"
        )
    for item in infos:
        normalized = item.filename.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/"):
            archive.close()
            raise ImportContractError(
                "unsafe_archive_path", "Office archive contains an unsafe path"
            )
        if item.file_size and item.compress_size == 0:
            archive.close()
            raise ImportContractError(
                "archive_limit", "Office archive has an invalid compression ratio"
            )
        if (
            item.compress_size
            and item.file_size / item.compress_size > MAX_COMPRESSION_RATIO
        ):
            archive.close()
            raise ImportContractError(
                "archive_limit", "Office archive compression ratio is unsafe"
            )
    lower_names = {item.filename.casefold() for item in infos}
    if any(
        "vbaproject" in name or name.endswith((".bin", ".vbs", ".js"))
        for name in lower_names
    ):
        archive.close()
        raise ImportContractError(
            "active_content", "Macros or executable office content are not allowed"
        )
    if expected not in lower_names:
        archive.close()
        raise ImportContractError(
            "type_mismatch", "Required office document part is missing"
        )
    return archive


def _xml_text(xml_bytes: bytes, paragraph_tags: set[str]) -> list[str]:
    try:
        root = ElementTree.fromstring(xml_bytes)
    except ElementTree.ParseError as exc:
        raise ImportContractError(
            "invalid_xml", "Office document XML is invalid"
        ) from exc
    lines: list[str] = []
    for element in root.iter():
        local = element.tag.rsplit("}", 1)[-1]
        if local in paragraph_tags:
            value = "".join(element.itertext())
            if value.strip():
                lines.append(value)
    return _normalize_lines("\n".join(lines))


def _reject_external_relationships(archive: zipfile.ZipFile) -> None:
    for name in archive.namelist():
        if not name.casefold().endswith(".rels"):
            continue
        try:
            root = ElementTree.fromstring(archive.read(name))
        except ElementTree.ParseError as exc:
            raise ImportContractError(
                "invalid_xml", "Office relationship XML is invalid"
            ) from exc
        for relationship in root.iter():
            if relationship.attrib.get("TargetMode", "").casefold() == "external":
                raise ImportContractError(
                    "external_relationship",
                    "External office relationships are not allowed",
                )


def _extract_docx(data: bytes) -> tuple[list[str], str, list[dict[str, str]]]:
    archive = _safe_archive(data, "word/document.xml")
    try:
        _reject_external_relationships(archive)
        lines = _xml_text(archive.read("word/document.xml"), {"p"})
    finally:
        archive.close()
    return lines, "stdlib-docx-xml", []


def _extract_odt(data: bytes) -> tuple[list[str], str, list[dict[str, str]]]:
    archive = _safe_archive(data, "content.xml")
    try:
        try:
            media = archive.read("mimetype").decode("ascii").strip()
        except (KeyError, UnicodeDecodeError) as exc:
            raise ImportContractError(
                "type_mismatch", "ODT mimetype part is missing or invalid"
            ) from exc
        if media != ALLOWED_MEDIA_TYPES[".odt"]:
            raise ImportContractError(
                "type_mismatch", "ODT mimetype does not match extension"
            )
        _reject_external_relationships(archive)
        lines = _xml_text(archive.read("content.xml"), {"p", "h"})
    finally:
        archive.close()
    return lines, "stdlib-odt-xml", []


def _extract_pdf(
    path: Path, data: bytes
) -> tuple[list[str], str, list[dict[str, str]]]:
    if not data.startswith(b"%PDF-"):
        raise ImportContractError(
            "type_mismatch", "PDF signature does not match extension"
        )
    if any(marker in data for marker in ACTIVE_PDF_MARKERS):
        raise ImportContractError("active_content", "PDF active content is not allowed")
    executable = shutil.which("pdftotext")
    if not executable:
        raise ImportContractError(
            "extractor_unavailable", "Local pdftotext is required for PDF input"
        )
    try:
        result = subprocess.run(
            # `-layout` keeps horizontal positions so columns stay separable,
            # and page breaks are kept so each page is analysed on its own.
            [executable, "-enc", "UTF-8", "-layout", str(path.resolve()), "-"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=PDF_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ImportContractError(
            "extraction_failed", "PDF extraction failed or timed out"
        ) from exc
    if result.returncode != 0:
        raise ImportContractError("extraction_failed", "pdftotext rejected the PDF")
    try:
        text = result.stdout.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportContractError(
            "invalid_encoding", "PDF extractor did not return UTF-8"
        ) from exc
    return _normalize_lines(_reading_order(text)), "pdftotext", []


def _stable_id(prefix: str, source_id: str, kind: str, value: str) -> str:
    digest = hashlib.sha256(
        f"{source_id}\0{kind}\0{value.casefold().strip()}".encode()
    ).hexdigest()[:16]
    return f"{prefix}-{digest}"


def _date(value: str) -> str:
    normalized = value.strip().casefold().replace(".", "-").replace("/", "-")
    if normalized in {"heute", "aktuell", "present", "current"}:
        return "present"
    if re.fullmatch(r"\d{4}", normalized):
        return normalized
    year_first = re.fullmatch(r"(?P<year>\d{4})-(?P<month>\d{1,2})", normalized)
    month_first = re.fullmatch(r"(?P<month>\d{1,2})-(?P<year>\d{4})", normalized)
    match = year_first or month_first
    if match is None or not 1 <= int(match.group("month")) <= 12:
        raise ImportContractError(
            "invalid_date", "Date must be YYYY, YYYY-MM, MM/YYYY, or present"
        )
    return f"{match.group('year')}-{int(match.group('month')):02d}"


def _claim(source_id: str, category: str, statement: str, line: int) -> dict[str, Any]:
    return {
        "id": _stable_id("claim", source_id, category, statement),
        "category": category,
        "statement": statement,
        "status": "unverified",
        "evidence_refs": [source_id],
        "allowed_outputs": ["cv", "cover_letter", "email", "linkedin", "interview"],
        "source_spans": [{"line_start": line, "line_end": line}],
    }


def _body_parts(body: str) -> tuple[str, str, str]:
    pipe_parts = [
        part.strip() for part in re.split(r"\s*[|@]\s*", body) if part.strip()
    ]
    if len(pipe_parts) >= 2:
        return (
            pipe_parts[0],
            pipe_parts[1],
            pipe_parts[2] if len(pipe_parts) == 3 else "",
        )
    # German CVs commonly write "Firma - Rolle"; the organisation comes first.
    dash = [part.strip() for part in re.split(r"\s+[-–—]\s+", body, maxsplit=1)]
    if len(dash) == 2 and dash[0] and dash[1]:
        return dash[1], dash[0], ""
    parts = [
        part.strip()
        for part in re.split(
            r"\s+bei\s+|\s+at\s+", body, maxsplit=1, flags=re.IGNORECASE
        )
    ]
    if len(parts) == 2:
        company_parts = [part.strip() for part in parts[1].rsplit(",", 1)]
        if len(company_parts) == 2 and company_parts[1]:
            return parts[0], company_parts[0], company_parts[1]
        return parts[0], parts[1], ""
    comma = [part.strip() for part in body.split(",", 1)]
    return (comma[0], comma[1], "") if len(comma) == 2 else (body.strip(), "", "")


def _normalize(
    source_id: str, lines: list[str]
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    sections: dict[str, list[tuple[int, str]]] = {key: [] for key in SECTION_ALIASES}
    sections["other"] = []
    section = "other"
    for index, line in enumerate(lines, start=1):
        heading = re.sub(r"[:\s]+$", "", line).casefold()
        matched = next(
            (key for key, aliases in SECTION_ALIASES.items() if heading in aliases),
            None,
        )
        if matched:
            section = matched
        else:
            sections[section].append((index, line))

    claims: list[dict[str, Any]] = []
    experience: list[dict[str, Any]] = []
    projects: list[dict[str, Any]] = []
    education: list[dict[str, Any]] = []
    certifications: list[dict[str, Any]] = []
    skills: list[dict[str, Any]] = []
    languages: list[dict[str, Any]] = []
    additional: list[dict[str, Any]] = []
    profile_facts: list[dict[str, Any]] = []

    def add_claim(category: str, statement: str, line_no: int) -> str:
        item = _claim(source_id, category, statement, line_no)
        if not any(existing["id"] == item["id"] for existing in claims):
            claims.append(item)
        return item["id"]

    for line_no, line in sections["experience"]:
        match = DATE_RANGE.match(line)
        if match:
            role, company, location = _body_parts(match.group("body"))
            claim_id = add_claim("employment", line, line_no)
            experience.append(
                {
                    "id": _stable_id("experience", source_id, "employment", line),
                    "role": role,
                    "company": company,
                    "location": location,
                    "start_date": _date(match.group("start")),
                    "end_date": _date(match.group("end")),
                    "employment_type": "",
                    "status": "unverified",
                    "evidence_refs": [source_id],
                    "claim_ids": [claim_id],
                    "details": [],
                }
            )
        elif experience:
            statement = BULLET.sub("", line).strip()
            claim_id = add_claim("experience_detail", statement, line_no)
            experience[-1]["claim_ids"].append(claim_id)
            experience[-1]["details"].append({"text": statement, "claim_id": claim_id})
        else:
            additional.append(
                {"text": line, "claim_id": add_claim("other", line, line_no)}
            )

    for section_name, target, category in (
        ("projects", projects, "project"),
        ("education", education, "education"),
        ("certifications", certifications, "certification"),
    ):
        for line_no, line in sections[section_name]:
            match = DATE_RANGE.match(line)
            body = match.group("body") if match else line
            claim_id = add_claim(category, line, line_no)
            record = {
                "id": _stable_id(category, source_id, category, line),
                "name": body,
                "status": "unverified",
                "evidence_refs": [source_id],
                "claim_ids": [claim_id],
            }
            if match:
                record.update(
                    start_date=_date(match.group("start")),
                    end_date=_date(match.group("end")),
                )
            target.append(record)

    for section_name, target, category, name_key in (
        ("skills", skills, "skill", "name"),
        ("languages", languages, "language", "language"),
    ):
        for line_no, line in sections[section_name]:
            for token in (item.strip() for item in re.split(r"[,;•]", line)):
                if not token:
                    continue
                claim_id = add_claim(category, token, line_no)
                target.append(
                    {
                        name_key: token,
                        "status": "unverified",
                        "evidence_refs": [source_id],
                        "claim_ids": [claim_id],
                    }
                )

    for line_no, line in sections["other"]:
        field = ""
        value = line
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", line):
            field = "contact.email"
        elif re.match(r"^(?:\+|00)?[\d ()/-]{7,}$", line):
            field = "contact.phone"
        elif re.search(
            r"(?:linkedin\.com/in/|github\.com/|https?://)", line, re.IGNORECASE
        ):
            field = "contact.url"
        elif (
            not any(item.get("field") == "full_name" for item in profile_facts)
            and line_no <= 60
            and _looks_like_person_name(line)
        ):
            field = "full_name"
        if field:
            claim_id = add_claim("profile", f"{field}: {value}", line_no)
            profile_facts.append(
                {
                    "field": field,
                    "value": value,
                    "status": "unverified",
                    "evidence_refs": [source_id],
                    "claim_id": claim_id,
                }
            )
        else:
            additional.append(
                {"text": line, "claim_id": add_claim("other", line, line_no)}
            )

    conflicts: list[dict[str, str]] = []
    seen_periods: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in experience:
        key = (record["company"].casefold(), record["start_date"], record["end_date"])
        previous = seen_periods.get(key)
        if previous and previous["role"].casefold() != record["role"].casefold():
            conflicts.append(
                {
                    "code": "ambiguous_employment_role",
                    "left_record_id": previous["id"],
                    "right_record_id": record["id"],
                    "detail": "Same employer and period were extracted with different roles",
                }
            )
        seen_periods[key] = record
    return (
        {
            "profile": {"facts": profile_facts},
            "claims": claims,
            "experience": experience,
            "projects": projects,
            "education": education,
            "certifications": certifications,
            "skills": skills,
            "languages": languages,
            "additional_facts": additional,
        },
        conflicts,
    )


def _normalize_atomic(
    source_id: str, source_sha256: str, lines: list[str], engine: str = ""
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    sections: dict[str, list[tuple[int, str]]] = {key: [] for key in SECTION_ALIASES}
    sections["other"] = []
    current = "other"
    named = False
    for line_no, line in enumerate(lines, start=1):
        heading = re.sub(r"[:\s]+$", "", line).casefold()
        matched = next(
            (key for key, aliases in SECTION_ALIASES.items() if heading in aliases),
            None,
        )
        if matched:
            current = matched
            continue
        # A document title or the candidate's name starts a new block. In a
        # multi-column layout this is where the sidebar ends and the main
        # column begins, and without the reset the last sidebar section keeps
        # collecting: the name and the title were filed as certificates.
        if heading in DOCUMENT_TITLES:
            current = "other"
            continue
        # Inside an open section only an all-caps line may claim the name;
        # a title-case entry there is far more likely to be content.
        if (
            not named
            and _looks_like_person_name(line)
            and (current == "other" or line.isupper())
        ):
            named = True
            current = "other"
            sections["other"].append((line_no, line))
            continue
        sections[current].append((line_no, line))

    if engine == "pdftotext":
        # Only a PDF cuts a sentence across lines; the other extractors deliver
        # logical lines, where rejoining would glue unrelated entries together.
        # Prose sections only: certificates, skills and the contact block are
        # short label/value lines that rejoining would merge into one blob.
        sections = {
            key: _repair_wrapped_lines(value)
            if key in {"experience", "projects", "education"}
            else value
            for key, value in sections.items()
        }

    facts: list[dict[str, Any]] = []
    claims: list[dict[str, Any]] = []
    records: dict[str, list[dict[str, Any]]] = {
        key: []
        for key in (
            "experience",
            "projects",
            "education",
            "certifications",
            "skills",
            "languages",
        )
    }
    profile_facts: list[dict[str, Any]] = []
    additional: list[dict[str, Any]] = []
    facts_by_id: dict[str, dict[str, Any]] = {}
    claims_by_id: dict[str, dict[str, Any]] = {}
    records_by_id: dict[str, dict[str, dict[str, Any]]] = {
        collection: {} for collection in records
    }
    profile_fact_ids: set[str] = set()
    additional_record_ids: set[str] = set()
    experience_detail_keys: dict[str, set[str]] = {}

    def add_fact(
        category: str, record_id: str, field: str, value: str, line_no: int
    ) -> tuple[str, str]:
        if not value.strip():
            raise ImportContractError(
                "normalization_failed", "Atomic fact values must not be empty"
            )
        fact_id = _stable_id(
            "fact", source_id, category, f"{record_id}\0{field}\0{value}"
        )
        claim_id = _stable_id("claim", source_id, category, fact_id)
        existing_fact = facts_by_id.get(fact_id)
        existing_claim = claims_by_id.get(claim_id)
        if existing_fact is not None and existing_claim is not None:
            return fact_id, claim_id
        anchor = {
            "source_id": source_id,
            "source_sha256": source_sha256,
            "line_start": line_no,
            "line_end": line_no,
        }
        fact = {
            "id": fact_id,
            "claim_id": claim_id,
            "category": category,
            "record_id": record_id,
            "field": field,
            "value": value,
            "status": "unverified",
            "evidence_refs": [source_id],
            "source_anchor": anchor,
        }
        claim = {
            "id": claim_id,
            "fact_id": fact_id,
            "category": category,
            "statement": f"{field}: {value}",
            "status": "unverified",
            "evidence_refs": [source_id],
            "allowed_outputs": ["cv", "cover_letter", "email", "linkedin", "interview"],
            "source_spans": [{"line_start": line_no, "line_end": line_no}],
        }
        facts.append(fact)
        claims.append(claim)
        facts_by_id[fact_id] = fact
        claims_by_id[claim_id] = claim
        return fact_id, claim_id

    def create_record(
        collection: str,
        category: str,
        record_id: str,
        values: dict[str, str],
        line_no: int,
    ) -> dict[str, Any]:
        existing = records_by_id[collection].get(record_id)
        if existing is not None:
            return existing
        fact_map: dict[str, str] = {}
        claim_map: dict[str, str] = {}
        for field, value in values.items():
            if not value.strip():
                continue
            fact_id, claim_id = add_fact(category, record_id, field, value, line_no)
            fact_map[field] = fact_id
            claim_map[field] = claim_id
        record = {
            "id": record_id,
            **values,
            "status": "unverified",
            "evidence_refs": [source_id],
            "claim_ids": list(claim_map.values()),
            "field_fact_ids": fact_map,
            "field_claim_ids": claim_map,
        }
        records[collection].append(record)
        records_by_id[collection][record_id] = record
        return record

    current_experience: dict[str, Any] | None = None
    # A two-column CV prints the period in its own column, so a date can arrive
    # a whole entry before the heading it belongs to. Dates that show up without
    # a heading are queued and handed to the next heading that has none of its
    # own; a date sharing its line with a heading always stays with it.
    pending_periods: list[tuple[str, str]] = []
    for line_no, line in sections["experience"]:
        bare = BARE_DATE_RANGE.match(line)
        if bare:
            pending_periods.append((bare.group("start"), bare.group("end")))
            continue
        match = DATE_RANGE.match(line)
        heading: str | None = None
        period: tuple[str, str] | None = None
        if match and not BULLET_START.match(match.group("body")):
            heading = match.group("body")
            period = (match.group("start"), match.group("end"))
        elif match:
            # Date plus bullet: the period belongs to a later heading, the text
            # to the entry currently being read.
            pending_periods.append((match.group("start"), match.group("end")))
            line = match.group("body")
        elif not BULLET_START.match(line) and pending_periods:
            heading = line
            period = pending_periods.pop(0)
        if heading is not None and period is not None:
            role, company, location = _body_parts(heading)
            record_id = _stable_id("experience", source_id, "employment", heading)
            values = {
                "role": role,
                "company": company,
                "start_date": _date(period[0]),
                "end_date": _date(period[1]),
            }
            if location:
                values["location"] = location
            record = create_record(
                "experience",
                "employment",
                record_id,
                values,
                line_no,
            )
            record.setdefault("location", "")
            record.setdefault("employment_type", "")
            record.setdefault("details", [])
            current_experience = record
        elif current_experience is not None:
            record = current_experience
            seen_details = experience_detail_keys.setdefault(record["id"], set())
            # One extracted line can carry several bullet points.
            for part in _split_inline_bullets(line):
                statement = BULLET.sub("", part).strip()
                if not statement:
                    continue
                detail_key = statement.casefold()
                if detail_key in seen_details:
                    continue
                detail_index = len(record["details"])
                fact_id, claim_id = add_fact(
                    "experience_detail",
                    record["id"],
                    f"details[{detail_index}]",
                    statement,
                    line_no,
                )
                record["claim_ids"].append(claim_id)
                record["details"].append(
                    {"text": statement, "fact_id": fact_id, "claim_id": claim_id}
                )
                seen_details.add(detail_key)
        else:
            record_id = _stable_id("additional", source_id, "other", line)
            fact_id, claim_id = add_fact("other", record_id, "text", line, line_no)
            if record_id not in additional_record_ids:
                additional.append(
                    {
                        "id": record_id,
                        "text": line,
                        "fact_id": fact_id,
                        "claim_id": claim_id,
                    }
                )
                additional_record_ids.add(record_id)

    for collection, category in (
        ("projects", "project"),
        ("education", "education"),
        ("certifications", "certification"),
    ):
        for line_no, line in sections[collection]:
            match = DATE_RANGE.match(line)
            values = {"name": match.group("body") if match else line}
            if match:
                values.update(
                    start_date=_date(match.group("start")),
                    end_date=_date(match.group("end")),
                )
            create_record(
                collection,
                category,
                _stable_id(category, source_id, category, line),
                values,
                line_no,
            )

    for collection, category, field in (
        ("skills", "skill", "name"),
        ("languages", "language", "language"),
    ):
        for line_no, line in sections[collection]:
            for token in (part.strip() for part in re.split(r"[,;•]", line)):
                if token:
                    create_record(
                        collection,
                        category,
                        _stable_id(category, source_id, category, token),
                        {field: token},
                        line_no,
                    )

    for line_no, line in sections["other"]:
        if re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", line):
            field = "contact.email"
        elif re.match(r"^(?:\+|00)?[\d ()/-]{7,}$", line):
            field = "contact.phone"
        elif re.search(
            r"(?:linkedin\.com/in/|github\.com/|https?://)", line, re.IGNORECASE
        ):
            field = "contact.url"
        elif (
            not any(item.get("field") == "full_name" for item in profile_facts)
            and _looks_like_person_name(line)
        ):
            field = "full_name"
        else:
            field = ""
        if field:
            fact_id, claim_id = add_fact("profile", "profile", field, line, line_no)
            if fact_id not in profile_fact_ids:
                profile_facts.append(
                    {
                        "id": fact_id,
                        "field": field,
                        "value": line,
                        "status": "unverified",
                        "evidence_refs": [source_id],
                        "fact_id": fact_id,
                        "claim_id": claim_id,
                    }
                )
                profile_fact_ids.add(fact_id)
        else:
            record_id = _stable_id("additional", source_id, "other", line)
            fact_id, claim_id = add_fact("other", record_id, "text", line, line_no)
            if record_id not in additional_record_ids:
                additional.append(
                    {
                        "id": record_id,
                        "text": line,
                        "fact_id": fact_id,
                        "claim_id": claim_id,
                    }
                )
                additional_record_ids.add(record_id)

    conflicts: list[dict[str, str]] = []
    seen: dict[tuple[str, str, str], dict[str, Any]] = {}
    for record in records["experience"]:
        key = (record["company"].casefold(), record["start_date"], record["end_date"])
        previous = seen.get(key)
        if previous and previous["role"].casefold() != record["role"].casefold():
            conflicts.append(
                {
                    "code": "ambiguous_employment_role",
                    "left_record_id": previous["id"],
                    "right_record_id": record["id"],
                    "detail": "Same employer and period were extracted with different roles",
                }
            )
        seen[key] = record
    return (
        {
            "profile": {"facts": profile_facts},
            "facts": facts,
            "claims": claims,
            **records,
            "additional_facts": additional,
        },
        conflicts,
    )


def extract_cv(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    extension = source.suffix.casefold()
    if extension not in ALLOWED_EXTENSIONS:
        raise ImportContractError(
            "unsupported_type",
            f"Allowed extensions: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
        )
    data = _read_bounded(source)
    sha256 = hashlib.sha256(data).hexdigest()
    source_id = f"source-cv-{sha256[:16]}"
    if extension in {".html", ".htm"}:
        lines, engine, warnings = _extract_html(data)
    elif extension == ".docx":
        lines, engine, warnings = _extract_docx(data)
    elif extension == ".odt":
        lines, engine, warnings = _extract_odt(data)
    else:
        lines, engine, warnings = _extract_pdf(source, data)
    return _build_proposal(
        source_id=source_id,
        source_sha256=sha256,
        byte_size=len(data),
        media_type=ALLOWED_MEDIA_TYPES[extension],
        lines=lines,
        engine=engine,
        warnings=warnings,
    )


def _build_proposal(
    *,
    source_id: str,
    source_sha256: str,
    byte_size: int,
    media_type: str,
    lines: list[str],
    engine: str,
    warnings: list[dict[str, str]],
) -> dict[str, Any]:
    normalized, conflicts = _normalize_atomic(source_id, source_sha256, lines, engine)
    text_sha = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    result = {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "state": "needs_user_confirmation",
        "publishable": False,
        "source": {
            "id": source_id,
            "type": "cv_import",
            "media_type": media_type,
            "sha256": source_sha256,
            "byte_size": byte_size,
        },
        "sources": [
            {
                "id": source_id,
                "type": "cv_import",
                "media_type": media_type,
                "sha256": source_sha256,
                "byte_size": byte_size,
            }
        ],
        "extraction": {
            "engine": engine,
            "text_sha256": text_sha,
            "line_count": len(lines),
            "line_manifest": [
                {
                    "line": index,
                    "text": line,
                    "sha256": hashlib.sha256(line.encode("utf-8")).hexdigest(),
                }
                for index, line in enumerate(lines, start=1)
            ],
            "warnings": warnings,
            "conflicts": conflicts,
        },
        "proposal": normalized,
        "confirmation": {
            "required": True,
            "rule": "Each record and atomic claim remains unverified until explicitly confirmed by the candidate",
        },
    }
    errors = validate_proposal(result)
    if errors:
        raise ImportContractError("normalization_failed", "; ".join(errors))
    return result


def normalize_extracted_envelope(envelope: Any) -> dict[str, Any]:
    if not isinstance(envelope, dict):
        raise ImportContractError(
            "invalid_envelope", "Extracted text envelope must be a mapping"
        )
    if (
        envelope.get("contract") != "extracted-cv-text"
        or envelope.get("contract_version") != "1.0"
    ):
        raise ImportContractError(
            "invalid_envelope", "Unsupported extracted text contract"
        )
    source = envelope.get("source")
    extraction = envelope.get("extraction")
    if not isinstance(source, dict) or not isinstance(extraction, dict):
        raise ImportContractError(
            "invalid_envelope", "Source and extraction mappings are required"
        )
    source_sha = str(source.get("sha256", ""))
    if not re.fullmatch(r"[a-f0-9]{64}", source_sha):
        raise ImportContractError("invalid_envelope", "Source SHA-256 is required")
    byte_size = source.get("byte_size")
    if not isinstance(byte_size, int) or not 0 < byte_size <= MAX_INPUT_BYTES:
        raise ImportContractError(
            "invalid_envelope", "Source byte size is outside the safety limit"
        )
    media_type = str(source.get("media_type", ""))
    if media_type not in ALLOWED_MEDIA_TYPES.values():
        raise ImportContractError(
            "unsupported_type", "Envelope media type is not supported"
        )
    text = extraction.get("text")
    engine = extraction.get("engine")
    if (
        not isinstance(text, str)
        or not isinstance(engine, str)
        or not engine.strip()
        or len(engine) > 100
    ):
        raise ImportContractError(
            "invalid_envelope", "Extraction text and engine are required"
        )
    provided_text_sha = str(extraction.get("text_sha256", ""))
    actual_raw_text_sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if provided_text_sha != actual_raw_text_sha:
        raise ImportContractError(
            "digest_mismatch", "Extracted text SHA-256 does not match its content"
        )
    warnings = extraction.get("warnings", [])
    if not isinstance(warnings, list) or any(
        not isinstance(item, dict)
        or not isinstance(item.get("code"), str)
        or not isinstance(item.get("detail"), str)
        for item in warnings
    ):
        raise ImportContractError(
            "invalid_envelope", "Extraction warnings must be structured"
        )
    safe_warnings = [
        {"code": item["code"][:100], "detail": item["detail"][:500]}
        for item in warnings
    ]
    return _build_proposal(
        source_id=f"source-cv-{source_sha[:16]}",
        source_sha256=source_sha,
        byte_size=byte_size,
        media_type=media_type,
        lines=_normalize_lines(text),
        engine=engine.strip(),
        warnings=safe_warnings,
    )


def validate_proposal(data: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["root: expected a mapping"]
    if (
        data.get("contract") != CONTRACT
        or data.get("contract_version") != CONTRACT_VERSION
    ):
        errors.append("contract: unsupported contract or version")
    if data.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version: unsupported schema")
    if (
        data.get("state") != "needs_user_confirmation"
        or data.get("publishable") is not False
    ):
        errors.append(
            "state: imported proposal must be non-publishable and require confirmation"
        )
    source = data.get("source")
    if not isinstance(source, dict) or not re.fullmatch(
        r"source-cv-[a-f0-9]{16}", str(source.get("id", ""))
    ):
        errors.append("source.id: invalid stable source id")
        source_ids: set[str] = set()
    else:
        source_ids = {source["id"]}
    if not isinstance(source, dict) or not re.fullmatch(
        r"[a-f0-9]{64}", str(source.get("sha256", ""))
    ):
        errors.append("source.sha256: expected SHA-256")
    sources = data.get("sources")
    source_map: dict[str, dict[str, Any]] = {}
    if not isinstance(sources, list) or not sources:
        errors.append("sources: expected a non-empty list")
    else:
        for index, item in enumerate(sources):
            if not isinstance(item, dict) or not re.fullmatch(
                r"source-(?:cv|user)-[a-f0-9]{16}", str(item.get("id", ""))
            ):
                errors.append(f"sources[{index}].id: invalid source id")
                continue
            if not re.fullmatch(r"[a-f0-9]{64}", str(item.get("sha256", ""))):
                errors.append(f"sources[{index}].sha256: expected SHA-256")
            source_map[str(item["id"])] = item
        source_ids = set(source_map)
        if isinstance(source, dict) and source.get("id") not in source_ids:
            errors.append("source: primary source is absent from sources")
    private_manifest_lines: list[str] | None = None
    extraction = data.get("extraction")
    if (
        not isinstance(extraction, dict)
        or not isinstance(extraction.get("warnings"), list)
        or not isinstance(extraction.get("conflicts"), list)
    ):
        errors.append("extraction: warnings and conflicts must be lists")
    elif "line_manifest" in extraction:
        manifest = extraction.get("line_manifest")
        if not isinstance(manifest, list) or not manifest:
            errors.append(
                "extraction.line_manifest: expected a non-empty private line list"
            )
        else:
            manifest_lines: list[str] = []
            for index, item in enumerate(manifest, start=1):
                prefix = f"extraction.line_manifest[{index - 1}]"
                if not isinstance(item, dict) or set(item) != {
                    "line",
                    "text",
                    "sha256",
                }:
                    errors.append(f"{prefix}: expected only line, text, and sha256")
                    continue
                text = item.get("text")
                if item.get("line") != index or not isinstance(text, str) or not text:
                    errors.append(
                        f"{prefix}: line must be sequential and text non-empty"
                    )
                    continue
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if item.get("sha256") != digest:
                    errors.append(f"{prefix}.sha256: line digest mismatch")
                manifest_lines.append(text)
            if len(manifest_lines) == len(manifest):
                private_manifest_lines = manifest_lines
                joined_sha = hashlib.sha256(
                    "\n".join(manifest_lines).encode("utf-8")
                ).hexdigest()
                if extraction.get("text_sha256") != joined_sha:
                    errors.append(
                        "extraction.text_sha256: line manifest digest mismatch"
                    )
                if extraction.get("line_count") != len(manifest_lines):
                    errors.append("extraction.line_count: line manifest count mismatch")
    proposal = data.get("proposal")
    if not isinstance(proposal, dict):
        return errors + ["proposal: expected a mapping"]
    facts = proposal.get("facts")
    fact_ids: set[str] = set()
    fact_claim_map: dict[str, str] = {}
    if not isinstance(facts, list):
        errors.append("proposal.facts: expected a list")
        facts = []
    for index, fact in enumerate(facts):
        prefix = f"proposal.facts[{index}]"
        if not isinstance(fact, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        fact_id = str(fact.get("id", ""))
        if not re.fullmatch(r"fact-[a-f0-9]{16}", fact_id) or fact_id in fact_ids:
            errors.append(f"{prefix}.id: invalid or duplicate stable fact id")
        fact_ids.add(fact_id)
        fact_claim_map[fact_id] = str(fact.get("claim_id", ""))
        value = fact.get("value")
        if not isinstance(value, str) or not value.strip():
            errors.append(f"{prefix}.value: expected a non-empty string")
        if fact.get("status") != "unverified":
            errors.append(f"{prefix}.status: imported facts must be unverified")
        anchor = fact.get("source_anchor")
        anchor_source = (
            source_map.get(str(anchor.get("source_id", "")))
            if isinstance(anchor, dict)
            else None
        )
        if (
            not isinstance(anchor, dict)
            or anchor_source is None
            or anchor.get("source_sha256") != anchor_source.get("sha256")
        ):
            errors.append(f"{prefix}.source_anchor: source provenance is invalid")
        elif anchor.get("origin") == "ai_structuring" or "recognition_method" in anchor:
            ai_anchor_keys = {
                "origin",
                "recognition_method",
                "source_id",
                "source_sha256",
                "line_start",
                "line_end",
                "char_start",
                "char_end",
                "quote",
                "suggestion_id",
                "alternative_id",
            }
            if set(anchor) != ai_anchor_keys:
                errors.append(f"{prefix}.source_anchor: AI provenance keys are invalid")
            elif (
                anchor.get("origin") != "ai_structuring"
                or anchor.get("recognition_method") != "ai_assisted"
                or not re.fullmatch(
                    r"suggestion-[a-f0-9]{16}", str(anchor.get("suggestion_id", ""))
                )
                or (
                    anchor.get("alternative_id") is not None
                    and not re.fullmatch(
                        r"alternative-[a-f0-9]{16}",
                        str(anchor.get("alternative_id", "")),
                    )
                )
            ):
                errors.append(
                    f"{prefix}.source_anchor: AI recognition provenance is invalid"
                )
            elif private_manifest_lines is None:
                errors.append(
                    f"{prefix}.source_anchor: private line manifest is required"
                )
            else:
                provider_anchor = {
                    key: anchor[key]
                    for key in (
                        "line_start",
                        "line_end",
                        "char_start",
                        "char_end",
                        "quote",
                    )
                }
                try:
                    _verified_anchor, quoted = _anchor_value(
                        private_manifest_lines,
                        provider_anchor,
                        f"{prefix}.source_anchor",
                    )
                    fact_value = fact.get("value")
                    if fact.get("field") in {"start_date", "end_date"}:
                        if (
                            not isinstance(fact_value, str)
                            or _date(quoted) != fact_value
                        ):
                            errors.append(
                                f"{prefix}.value: normalized AI date is not source-bound"
                            )
                    elif fact_value != quoted:
                        errors.append(f"{prefix}.value: AI value is not source-bound")
                except ImportContractError:
                    errors.append(f"{prefix}.source_anchor: AI source span is invalid")
    claim_ids: set[str] = set()
    claims = proposal.get("claims")
    if not isinstance(claims, list):
        return errors + ["proposal.claims: expected a list"]
    for index, claim in enumerate(claims):
        prefix = f"proposal.claims[{index}]"
        if not isinstance(claim, dict):
            errors.append(f"{prefix}: expected a mapping")
            continue
        claim_id = str(claim.get("id", ""))
        if not re.fullmatch(r"claim-[a-f0-9]{16}", claim_id) or claim_id in claim_ids:
            errors.append(f"{prefix}.id: invalid or duplicate stable claim id")
        claim_ids.add(claim_id)
        if (
            claim.get("fact_id") not in fact_ids
            or fact_claim_map.get(str(claim.get("fact_id"))) != claim_id
        ):
            errors.append(f"{prefix}.fact_id: claim must map one-to-one to a fact")
        if claim.get("status") != "unverified":
            errors.append(f"{prefix}.status: imported claims must be unverified")
        statement = claim.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            errors.append(f"{prefix}.statement: expected a non-empty string")
        evidence_refs = set(claim.get("evidence_refs", []))
        if not evidence_refs or not evidence_refs <= source_ids:
            errors.append(f"{prefix}.evidence_refs: must reference a proposal source")
        spans = claim.get("source_spans")
        if not isinstance(spans, list) or not spans:
            errors.append(f"{prefix}.source_spans: provenance is required")
    profile = proposal.get("profile")
    if not isinstance(profile, dict) or not isinstance(profile.get("facts"), list):
        errors.append("proposal.profile.facts: expected a list")
    else:
        for index, fact in enumerate(profile["facts"]):
            prefix = f"proposal.profile.facts[{index}]"
            if not isinstance(fact, dict) or fact.get("status") != "unverified":
                errors.append(
                    f"{prefix}.status: imported profile facts must be unverified"
                )
                continue
            if fact.get("claim_id") not in claim_ids:
                errors.append(f"{prefix}.claim_id: unknown claim reference")
            if fact.get("fact_id") not in fact_ids:
                errors.append(f"{prefix}.fact_id: unknown fact reference")
    for collection in (
        "experience",
        "projects",
        "education",
        "certifications",
        "skills",
        "languages",
    ):
        records = proposal.get(collection)
        if not isinstance(records, list):
            errors.append(f"proposal.{collection}: expected a list")
            continue
        for index, record in enumerate(records):
            prefix = f"proposal.{collection}[{index}]"
            if not isinstance(record, dict) or record.get("status") != "unverified":
                errors.append(f"{prefix}.status: imported records must be unverified")
                continue
            evidence_refs = set(record.get("evidence_refs", []))
            if not evidence_refs or not evidence_refs <= source_ids:
                errors.append(
                    f"{prefix}.evidence_refs: must reference proposal sources"
                )
            refs = record.get("claim_ids", [])
            if not isinstance(refs, list) or any(ref not in claim_ids for ref in refs):
                errors.append(f"{prefix}.claim_ids: unknown claim reference")
            fact_map = record.get("field_fact_ids")
            claim_map = record.get("field_claim_ids")
            if not isinstance(fact_map, dict) or not isinstance(claim_map, dict):
                errors.append(f"{prefix}: field fact and claim maps are required")
            elif set(fact_map) != set(claim_map) or any(
                fact_id not in fact_ids
                or fact_claim_map.get(fact_id) != claim_map[field]
                for field, fact_id in fact_map.items()
            ):
                errors.append(f"{prefix}: field fact/claim mapping is invalid")
    return errors


USER_FACT_FIELDS = {
    "profile": {"full_name", "contact.email", "contact.phone", "contact.url"},
    "experience": {
        "role",
        "company",
        "start_date",
        "end_date",
        "location",
        "employment_type",
        "detail",
    },
    "projects": {"name", "role", "context", "start_date", "end_date", "link"},
    "education": {
        "name",
        "degree",
        "institution",
        "location",
        "start_date",
        "end_date",
    },
    "certifications": {"name", "issuer", "issue_date", "expiry_date", "credential_url"},
    "skills": {"name", "canonical_name", "category", "level", "last_used"},
    "languages": {"language", "level"},
    "additional_facts": {"text"},
}
USER_FACT_CATEGORIES = {
    "profile",
    "employment",
    "achievement",
    "technology",
    "metric",
    "project",
    "education",
    "certification",
    "skill",
    "language",
    "other",
}


def proposal_cas_sha256(data: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            data, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


AI_SECTION_KINDS = {
    "profile",
    "employment",
    "education",
    "projects",
    "skills",
    "languages",
    "other",
}
AI_BLOCK_FIELDS: dict[str, tuple[str, ...]] = {
    "employment": ("employer", "role", "start_date", "end_date", "location", "details"),
    "education": (
        "institution",
        "qualification",
        "start_date",
        "end_date",
        "location",
        "details",
    ),
    "projects": ("name", "role", "start_date", "end_date", "details", "technologies"),
    "languages": ("language", "level"),
}
AI_LIST_FIELDS = {"details", "technologies"}
AI_TARGETS: dict[str, tuple[str, str, dict[str, str]]] = {
    "employment": (
        "experience",
        "employment",
        {
            "employer": "company",
            "role": "role",
            "start_date": "start_date",
            "end_date": "end_date",
            "location": "location",
        },
    ),
    "education": (
        "education",
        "education",
        {
            "institution": "institution",
            "qualification": "name",
            "start_date": "start_date",
            "end_date": "end_date",
            "location": "location",
        },
    ),
    "projects": (
        "projects",
        "project",
        {
            "name": "name",
            "role": "role",
            "start_date": "start_date",
            "end_date": "end_date",
        },
    ),
    "languages": (
        "languages",
        "language",
        {"language": "language", "level": "level"},
    ),
}
AI_RECORD_PREFIXES = {
    "experience": "experience",
    "education": "education",
    "projects": "project",
    "skills": "skill",
    "languages": "language",
}


def _require_exact_keys(
    value: Any, required: set[str], path: str, *, code: str = "invalid_ai_structure"
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ImportContractError(code, f"{path} must be a mapping")
    actual = set(value)
    if actual != required:
        missing = sorted(required - actual)
        unknown = sorted(actual - required)
        detail = f"{path} keys do not match the contract"
        if missing:
            detail += f"; missing: {', '.join(missing)}"
        if unknown:
            detail += f"; unknown key count: {len(unknown)}"
        raise ImportContractError(code, detail)
    return value


def _private_line_manifest(
    base_proposal: dict[str, Any],
) -> tuple[list[str], dict[str, str]]:
    errors = validate_proposal(base_proposal)
    if errors:
        raise ImportContractError("invalid_proposal", "; ".join(errors))
    source = base_proposal["source"]
    extraction = base_proposal["extraction"]
    manifest = extraction.get("line_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise ImportContractError(
            "line_manifest_required",
            "AI structuring requires the private normalized line manifest",
        )
    lines: list[str] = []
    for index, item in enumerate(manifest, start=1):
        _require_exact_keys(
            item,
            {"line", "text", "sha256"},
            f"base_proposal.extraction.line_manifest[{index - 1}]",
            code="invalid_proposal",
        )
        text = item["text"]
        if item["line"] != index or not isinstance(text, str) or not text:
            raise ImportContractError(
                "invalid_proposal", "Private line manifest is not sequential"
            )
        if item["sha256"] != hashlib.sha256(text.encode("utf-8")).hexdigest():
            raise ImportContractError(
                "digest_mismatch", "Private line manifest digest does not match"
            )
        lines.append(text)
    text_sha256 = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    if (
        extraction.get("line_count") != len(lines)
        or extraction.get("text_sha256") != text_sha256
    ):
        raise ImportContractError(
            "digest_mismatch",
            "Private line manifest is not bound to extraction metadata",
        )
    return lines, {
        "source_id": source["id"],
        "source_sha256": source["sha256"],
        "text_sha256": text_sha256,
    }


def _validate_ai_base(
    base_proposal: Any, expected_proposal_sha256: Any
) -> tuple[dict[str, Any], list[str], dict[str, str]]:
    if not isinstance(base_proposal, dict):
        raise ImportContractError("invalid_proposal", "base_proposal must be a mapping")
    expected = str(expected_proposal_sha256)
    if not re.fullmatch(r"[a-f0-9]{64}", expected):
        raise ImportContractError(
            "invalid_request", "Expected proposal SHA-256 is invalid"
        )
    actual = proposal_cas_sha256(base_proposal)
    if expected != actual:
        raise ImportContractError(
            "cas_mismatch", "CV proposal changed since AI structuring began"
        )
    lines, binding = _private_line_manifest(base_proposal)
    binding["base_proposal_sha256"] = actual
    return base_proposal, lines, binding


def _anchor_value(
    lines: list[str], anchor: Any, path: str
) -> tuple[dict[str, Any], str]:
    value = _require_exact_keys(
        anchor,
        {"line_start", "line_end", "char_start", "char_end", "quote"},
        path,
    )
    line_start = value["line_start"]
    line_end = value["line_end"]
    char_start = value["char_start"]
    char_end = value["char_end"]
    quote = value["quote"]
    integers = (line_start, line_end, char_start, char_end)
    if any(not isinstance(item, int) or isinstance(item, bool) for item in integers):
        raise ImportContractError(
            "unsupported_source_span", f"{path} offsets must be integers"
        )
    if not 1 <= line_start <= line_end <= len(lines):
        raise ImportContractError(
            "unsupported_source_span", f"{path} line range is outside the manifest"
        )
    first = lines[line_start - 1]
    last = lines[line_end - 1]
    if line_start == line_end:
        if not 0 <= char_start < char_end <= len(first):
            raise ImportContractError(
                "unsupported_source_span", f"{path} character range is invalid"
            )
        extracted = first[char_start:char_end]
    else:
        if not 0 <= char_start < len(first) or not 0 < char_end <= len(last):
            raise ImportContractError(
                "unsupported_source_span", f"{path} character range is invalid"
            )
        extracted = "\n".join(
            [first[char_start:], *lines[line_start : line_end - 1], last[:char_end]]
        )
    if (
        not isinstance(quote, str)
        or not quote
        or len(quote) > 5000
        or quote != extracted
    ):
        raise ImportContractError(
            "out_of_source_value", f"{path}.quote does not match the exact source span"
        )
    return copy.deepcopy(value), extracted


def _canonicalize_ai_anchor(
    lines: list[str], anchor: Any, candidate_value: str, path: str
) -> tuple[dict[str, Any], str]:
    """Repair only uniquely source-bound AI coordinates inside the declared lines."""
    value = _require_exact_keys(
        anchor,
        {"line_start", "line_end", "char_start", "char_end", "quote"},
        path,
    )
    quote = value["quote"]
    if not isinstance(quote, str) or not quote or len(quote) > 5000:
        raise ImportContractError("out_of_source_value", f"{path}.quote is invalid")
    if candidate_value != quote:
        raise ImportContractError(
            "out_of_source_value", f"{path}.value must equal its source quote"
        )

    line_start = value["line_start"]
    line_end = value["line_end"]
    char_start = value["char_start"]
    char_end = value["char_end"]
    integers = (line_start, line_end, char_start, char_end)
    if any(not isinstance(item, int) or isinstance(item, bool) for item in integers):
        raise ImportContractError(
            "unsupported_source_span", f"{path} offsets must be integers"
        )
    if not 1 <= line_start <= line_end <= len(lines):
        raise ImportContractError(
            "unsupported_source_span", f"{path} line range is outside the manifest"
        )

    try:
        exact_anchor, extracted = _anchor_value(lines, value, path)
    except ImportContractError as exc:
        if exc.code not in {"out_of_source_value", "unsupported_source_span"}:
            raise
    else:
        return exact_anchor, extracted

    declared_text = "\n".join(lines[line_start - 1 : line_end])
    occurrences: list[int] = []
    search_from = 0
    while len(occurrences) < 2:
        occurrence = declared_text.find(quote, search_from)
        if occurrence < 0:
            break
        occurrences.append(occurrence)
        search_from = occurrence + 1
    if len(occurrences) != 1:
        reason = "not present" if not occurrences else "not unique"
        raise ImportContractError(
            "out_of_source_value",
            f"{path}.quote is {reason} inside the declared line range",
        )

    match_start = occurrences[0]
    match_end = match_start + len(quote)
    before_match = declared_text[:match_start]
    through_match = declared_text[:match_end]
    canonical = {
        "line_start": line_start + before_match.count("\n"),
        "line_end": line_start + through_match.count("\n"),
        "char_start": len(before_match.rsplit("\n", 1)[-1]),
        "char_end": len(through_match.rsplit("\n", 1)[-1]),
        "quote": quote,
    }
    return _anchor_value(lines, canonical, path)


def _confidence(value: Any, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ImportContractError(
            "invalid_ai_structure", f"{path} must be a number from 0 to 1"
        )
    number = float(value)
    if not 0 <= number <= 1:
        raise ImportContractError(
            "invalid_ai_structure", f"{path} must be a number from 0 to 1"
        )
    return number


def _questions(value: Any, path: str) -> list[str]:
    if not isinstance(value, list) or len(value) > MAX_AI_QUESTIONS:
        raise ImportContractError(
            "invalid_ai_structure", f"{path} must be a bounded list"
        )
    questions: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip() or len(item) > 500:
            raise ImportContractError(
                "invalid_ai_structure", f"{path}[{index}] is invalid"
            )
        questions.append(item.strip())
    return questions


def _validate_ai_alternative(raw: Any, lines: list[str], path: str) -> dict[str, Any]:
    alternative = _require_exact_keys(
        raw, {"value", "source_anchor", "confidence"}, path
    )
    value = alternative["value"]
    if not isinstance(value, str) or not value or len(value) > 5000:
        raise ImportContractError(
            "invalid_ai_structure", f"{path}.value must be non-empty"
        )
    anchor, extracted = _canonicalize_ai_anchor(
        lines,
        alternative["source_anchor"],
        value,
        f"{path}.source_anchor",
    )
    if value != extracted:
        raise ImportContractError(
            "out_of_source_value", f"{path}.value must equal its exact source span"
        )
    return {
        "value": value,
        "source_anchor": anchor,
        "confidence": _confidence(alternative["confidence"], f"{path}.confidence"),
    }


def _validate_ai_field(raw: Any, lines: list[str], path: str) -> dict[str, Any]:
    field = _require_exact_keys(
        raw,
        {"value", "source_anchor", "confidence", "alternatives", "questions", "status"},
        path,
    )
    if field["status"] != "unverified":
        raise ImportContractError(
            "invalid_ai_status", f"{path}.status must be unverified"
        )
    questions = _questions(field["questions"], f"{path}.questions")
    confidence = _confidence(field["confidence"], f"{path}.confidence")
    alternatives_raw = field["alternatives"]
    if (
        not isinstance(alternatives_raw, list)
        or len(alternatives_raw) > MAX_AI_ALTERNATIVES
    ):
        raise ImportContractError(
            "invalid_ai_structure", f"{path}.alternatives must be a bounded list"
        )
    alternatives = [
        _validate_ai_alternative(item, lines, f"{path}.alternatives[{index}]")
        for index, item in enumerate(alternatives_raw)
    ]
    value = field["value"]
    anchor_raw = field["source_anchor"]
    if value is None:
        if anchor_raw is not None or confidence != 0 or not questions:
            raise ImportContractError(
                "invalid_ai_structure",
                f"{path} needs a question, null anchor, and zero confidence when value is null",
            )
        anchor = None
    else:
        if not isinstance(value, str) or not value or len(value) > 5000:
            raise ImportContractError(
                "invalid_ai_structure", f"{path}.value must be non-empty or null"
            )
        anchor, extracted = _canonicalize_ai_anchor(
            lines, anchor_raw, value, f"{path}.source_anchor"
        )
        if value != extracted:
            raise ImportContractError(
                "out_of_source_value", f"{path}.value must equal its exact source span"
            )
    candidates = [] if value is None else [(value, anchor)]
    candidates.extend((item["value"], item["source_anchor"]) for item in alternatives)
    fingerprints = [
        (candidate, json.dumps(source_anchor, sort_keys=True, ensure_ascii=False))
        for candidate, source_anchor in candidates
    ]
    if len(fingerprints) != len(set(fingerprints)):
        raise ImportContractError(
            "conflicting_ai_structure", f"{path} contains duplicate candidates"
        )
    return {
        "value": value,
        "source_anchor": anchor,
        "confidence": confidence,
        "alternatives": alternatives,
        "questions": questions,
        "status": "unverified",
    }


def _field_identity(field: dict[str, Any]) -> str:
    candidates: list[dict[str, Any]] = []
    if field["value"] is not None:
        candidates.append(
            {"value": field["value"], "source_anchor": field["source_anchor"]}
        )
    candidates.extend(
        {"value": item["value"], "source_anchor": item["source_anchor"]}
        for item in field["alternatives"]
    )
    return json.dumps(
        candidates, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def _record_id(
    source_id: str, collection: str, fields: list[tuple[str, dict[str, Any]]]
) -> str:
    identity = "\0".join(f"{name}\0{_field_identity(field)}" for name, field in fields)
    if not any(
        field["value"] is not None or field["alternatives"] for _, field in fields
    ):
        raise ImportContractError(
            "invalid_ai_structure", f"{collection} block has no source-backed candidate"
        )
    prefix = AI_RECORD_PREFIXES[collection]
    return _stable_id(prefix, source_id, "ai-structure-record", identity)


def _suggestion(
    *,
    source_id: str,
    path: str,
    collection: str,
    record_id: str | None,
    field_name: str,
    category: str,
    field: dict[str, Any],
    mergeable: bool,
    section_kind: str | None = None,
) -> dict[str, Any]:
    identity = f"{record_id or section_kind}\0{field_name}\0{_field_identity(field)}"
    suggestion_id = _stable_id("suggestion", source_id, "ai-structure-field", identity)
    alternatives = []
    for item in field["alternatives"]:
        alternative_id = _stable_id(
            "alternative",
            source_id,
            "ai-structure-alternative",
            f"{suggestion_id}\0{item['value']}\0{json.dumps(item['source_anchor'], sort_keys=True)}",
        )
        alternatives.append({"id": alternative_id, **copy.deepcopy(item)})
    result = {
        "id": suggestion_id,
        "path": path,
        "collection": collection,
        "record_id": record_id,
        "field": field_name,
        "category": category,
        "mergeable": mergeable,
        "value": field["value"],
        "source_anchor": copy.deepcopy(field["source_anchor"]),
        "confidence": field["confidence"],
        "alternatives": alternatives,
        "questions": list(field["questions"]),
        "status": "unverified",
    }
    if section_kind is not None:
        result["section_kind"] = section_kind
    return result


def _validate_date_field(field: dict[str, Any], path: str) -> None:
    for candidate in [field, *field["alternatives"]]:
        value = candidate.get("value")
        if value is None:
            continue
        try:
            _date(value)
        except ImportContractError as exc:
            raise ImportContractError(
                "invalid_ai_structure", f"{path} is not a supported date"
            ) from exc


def _date_order(start: str | None, end: str | None, path: str) -> None:
    if start is None or end is None:
        return
    normalized_start = _date(start)
    normalized_end = _date(end)
    if normalized_start == "present" or (
        normalized_end != "present" and normalized_start > normalized_end
    ):
        raise ImportContractError(
            "conflicting_ai_structure", f"{path} has a conflicting date range"
        )


def _validate_provider_ai_proposal(
    raw: Any, lines: list[str], expected_binding: dict[str, str]
) -> dict[str, Any]:
    proposal = _require_exact_keys(
        raw,
        {
            "contract",
            "contract_version",
            "status",
            "binding",
            "sections",
            "employment",
            "education",
            "projects",
            "skills",
            "languages",
        },
        "ai_proposal",
    )
    if (
        proposal["contract"] != AI_STRUCTURE_CONTRACT
        or proposal["contract_version"] != AI_STRUCTURE_CONTRACT_VERSION
    ):
        raise ImportContractError(
            "invalid_ai_structure", "Unsupported AI structure contract"
        )
    if proposal["status"] != "unverified":
        raise ImportContractError(
            "invalid_ai_status", "AI structure proposal must be unverified"
        )
    binding = _require_exact_keys(
        proposal["binding"],
        {"source_id", "source_sha256", "text_sha256", "base_proposal_sha256"},
        "ai_proposal.binding",
    )
    if any(binding.get(key) != expected_binding[key] for key in expected_binding):
        raise ImportContractError(
            "ai_binding_mismatch", "AI proposal is not bound to this CV artifact"
        )

    suggestions: list[dict[str, Any]] = []
    sections = proposal["sections"]
    if not isinstance(sections, list) or len(sections) > MAX_AI_COLLECTION_ITEMS:
        raise ImportContractError(
            "invalid_ai_structure", "ai_proposal.sections must be a bounded list"
        )
    for index, raw_section in enumerate(sections):
        section = _require_exact_keys(
            raw_section, {"kind", "heading", "status"}, f"ai_proposal.sections[{index}]"
        )
        if section["kind"] not in AI_SECTION_KINDS or section["status"] != "unverified":
            raise ImportContractError(
                "invalid_ai_structure", f"ai_proposal.sections[{index}] is invalid"
            )
        heading = _validate_ai_field(
            section["heading"], lines, f"ai_proposal.sections[{index}].heading"
        )
        suggestions.append(
            _suggestion(
                source_id=expected_binding["source_id"],
                path=f"sections[{index}].heading",
                collection="sections",
                record_id=None,
                field_name="heading",
                category="section",
                field=heading,
                mergeable=False,
                section_kind=section["kind"],
            )
        )

    record_ids: set[str] = set()
    for provider_collection in ("employment", "education", "projects", "languages"):
        raw_records = proposal[provider_collection]
        if (
            not isinstance(raw_records, list)
            or len(raw_records) > MAX_AI_COLLECTION_ITEMS
        ):
            raise ImportContractError(
                "invalid_ai_structure",
                f"ai_proposal.{provider_collection} must be a bounded list",
            )
        target_collection, category, scalar_targets = AI_TARGETS[provider_collection]
        expected_fields = set(AI_BLOCK_FIELDS[provider_collection]) | {"status"}
        for index, raw_record in enumerate(raw_records):
            path = f"ai_proposal.{provider_collection}[{index}]"
            record = _require_exact_keys(raw_record, expected_fields, path)
            if record["status"] != "unverified":
                raise ImportContractError(
                    "invalid_ai_status", f"{path}.status must be unverified"
                )
            validated_fields: list[tuple[str, dict[str, Any]]] = []
            for provider_field in AI_BLOCK_FIELDS[provider_collection]:
                raw_field = record[provider_field]
                if provider_field in AI_LIST_FIELDS:
                    if (
                        not isinstance(raw_field, list)
                        or len(raw_field) > MAX_AI_COLLECTION_ITEMS
                    ):
                        raise ImportContractError(
                            "invalid_ai_structure",
                            f"{path}.{provider_field} must be a bounded list",
                        )
                    for field_index, item in enumerate(raw_field):
                        validated_fields.append(
                            (
                                f"{provider_field}[{field_index}]",
                                _validate_ai_field(
                                    item,
                                    lines,
                                    f"{path}.{provider_field}[{field_index}]",
                                ),
                            )
                        )
                else:
                    field = _validate_ai_field(
                        raw_field, lines, f"{path}.{provider_field}"
                    )
                    if provider_field in {"start_date", "end_date"}:
                        _validate_date_field(field, f"{path}.{provider_field}")
                    validated_fields.append((provider_field, field))
            by_name = dict(validated_fields)
            _date_order(
                by_name.get("start_date", {}).get("value"),
                by_name.get("end_date", {}).get("value"),
                path,
            )
            primary_anchors = [
                json.dumps(field["source_anchor"], sort_keys=True, ensure_ascii=False)
                for _, field in validated_fields
                if field["source_anchor"] is not None
            ]
            if len(primary_anchors) != len(set(primary_anchors)):
                raise ImportContractError(
                    "conflicting_ai_structure",
                    f"{path} reuses one source span for multiple fields",
                )
            record_id = _record_id(
                expected_binding["source_id"], target_collection, validated_fields
            )
            if record_id in record_ids:
                raise ImportContractError(
                    "conflicting_ai_structure", f"{path} duplicates another record"
                )
            record_ids.add(record_id)
            for provider_field, field in validated_fields:
                if provider_field.startswith("details["):
                    target_field = provider_field
                    field_category = (
                        "achievement"
                        if provider_collection == "employment"
                        else category
                    )
                elif provider_field.startswith("technologies["):
                    target_field = provider_field
                    field_category = "technology"
                else:
                    target_field = scalar_targets[provider_field]
                    field_category = category
                suggestions.append(
                    _suggestion(
                        source_id=expected_binding["source_id"],
                        path=f"{provider_collection}[{index}].{provider_field}",
                        collection=target_collection,
                        record_id=record_id,
                        field_name=target_field,
                        category=field_category,
                        field=field,
                        mergeable=True,
                    )
                )

    skills = proposal["skills"]
    if not isinstance(skills, list) or len(skills) > MAX_AI_COLLECTION_ITEMS:
        raise ImportContractError(
            "invalid_ai_structure", "ai_proposal.skills must be a bounded list"
        )
    for index, raw_skill in enumerate(skills):
        path = f"ai_proposal.skills[{index}]"
        field = _validate_ai_field(raw_skill, lines, path)
        record_id = _record_id(
            expected_binding["source_id"], "skills", [("name", field)]
        )
        if record_id in record_ids:
            raise ImportContractError(
                "conflicting_ai_structure", f"{path} duplicates another skill"
            )
        record_ids.add(record_id)
        suggestions.append(
            _suggestion(
                source_id=expected_binding["source_id"],
                path=f"skills[{index}]",
                collection="skills",
                record_id=record_id,
                field_name="name",
                category="skill",
                field=field,
                mergeable=True,
            )
        )

    suggestion_ids = [item["id"] for item in suggestions]
    if len(suggestion_ids) != len(set(suggestion_ids)):
        raise ImportContractError(
            "conflicting_ai_structure", "AI suggestions have duplicate stable IDs"
        )
    suggestions.sort(
        key=lambda item: (
            (item["source_anchor"] or {"line_start": 10**9})["line_start"],
            (item["source_anchor"] or {"char_start": 10**9})["char_start"],
            item["path"],
        )
    )
    return {
        "contract": AI_VALIDATED_CONTRACT,
        "contract_version": AI_STRUCTURE_CONTRACT_VERSION,
        "status": "unverified",
        "binding": copy.deepcopy(expected_binding),
        "suggestions": suggestions,
        "conflicts": [],
    }


def validate_ai_structure_request(request_data: Any) -> dict[str, Any]:
    request = _require_exact_keys(
        request_data,
        {
            "contract",
            "contract_version",
            "base_proposal",
            "expected_proposal_sha256",
            "ai_proposal",
        },
        "request",
        code="invalid_request",
    )
    if (
        request["contract"] != AI_VALIDATION_REQUEST_CONTRACT
        or request["contract_version"] != AI_STRUCTURE_CONTRACT_VERSION
    ):
        raise ImportContractError(
            "invalid_request", "Unsupported AI validation request contract"
        )
    _base, lines, binding = _validate_ai_base(
        request["base_proposal"], request["expected_proposal_sha256"]
    )
    return _validate_provider_ai_proposal(request["ai_proposal"], lines, binding)


def _selected_candidate(
    suggestion: dict[str, Any], alternative_id: str | None
) -> tuple[str, dict[str, Any], float, str | None]:
    if alternative_id is None:
        if suggestion["value"] is None or suggestion["source_anchor"] is None:
            raise ImportContractError(
                "invalid_selection", "A null primary suggestion cannot be applied"
            )
        return (
            suggestion["value"],
            suggestion["source_anchor"],
            suggestion["confidence"],
            None,
        )
    alternative = next(
        (item for item in suggestion["alternatives"] if item["id"] == alternative_id),
        None,
    )
    if alternative is None:
        raise ImportContractError(
            "invalid_selection", "Selection references an unknown alternative"
        )
    return (
        alternative["value"],
        alternative["source_anchor"],
        alternative["confidence"],
        alternative_id,
    )


def _project_ai_structure(
    base: dict[str, Any],
    binding: dict[str, str],
    selected: list[tuple[dict[str, Any], str, dict[str, Any], float, str | None]],
    *,
    audit_mode: str | None = None,
    replace_ai_audit: bool = False,
) -> tuple[dict[str, Any], dict[str, Any]]:
    selected_by_record: dict[str, dict[str, str]] = {}
    for suggestion, value, _anchor, _confidence_value, _alternative in selected:
        if suggestion["field"] in {"start_date", "end_date"}:
            selected_by_record.setdefault(suggestion["record_id"], {})[
                suggestion["field"]
            ] = value
    for record_id, dates in selected_by_record.items():
        _date_order(dates.get("start_date"), dates.get("end_date"), record_id)

    updated = copy.deepcopy(base)
    proposal = updated["proposal"]
    record_maps = {
        collection: {record["id"]: record for record in proposal[collection]}
        for collection in ("experience", "education", "projects", "skills", "languages")
    }
    added_fact_ids: list[str] = []
    added_claim_ids: list[str] = []
    applied_suggestion_ids: set[str] = set()
    for suggestion, raw_value, anchor, confidence, selected_alternative_id in selected:
        collection = suggestion["collection"]
        record_id = suggestion["record_id"]
        field = suggestion["field"]
        value = _date(raw_value) if field in {"start_date", "end_date"} else raw_value
        record = record_maps[collection].get(record_id)
        if record is None:
            record = {
                "id": record_id,
                "status": "unverified",
                "evidence_refs": [binding["source_id"]],
                "claim_ids": [],
                "field_fact_ids": {},
                "field_claim_ids": {},
            }
            if collection == "experience":
                record.update(location="", employment_type="", details=[])
            if collection in {"education", "projects"}:
                record["details"] = []
            if collection == "projects":
                record["technologies"] = []
            proposal[collection].append(record)
            record_maps[collection][record_id] = record
        elif record.get("status") != "unverified":
            raise ImportContractError(
                "conflicting_ai_structure",
                "AI apply cannot modify a publishable record",
            )

        fact_id = _stable_id(
            "fact",
            binding["source_id"],
            suggestion["category"],
            f"{record_id}\0{field}\0{value}\0{suggestion['id']}\0"
            f"{selected_alternative_id or 'primary'}",
        )
        claim_id = _stable_id(
            "claim", binding["source_id"], suggestion["category"], fact_id
        )
        if any(item["id"] == fact_id for item in proposal["facts"]):
            raise ImportContractError(
                "conflicting_ai_structure", "AI selection duplicates an existing fact"
            )
        source_anchor = {
            "origin": "ai_structuring",
            "recognition_method": "ai_assisted",
            "source_id": binding["source_id"],
            "source_sha256": binding["source_sha256"],
            **copy.deepcopy(anchor),
            "suggestion_id": suggestion["id"],
            "alternative_id": selected_alternative_id,
        }
        proposal["facts"].append(
            {
                "id": fact_id,
                "claim_id": claim_id,
                "category": suggestion["category"],
                "record_id": record_id,
                "field": field,
                "value": value,
                "status": "unverified",
                "evidence_refs": [binding["source_id"]],
                "source_anchor": source_anchor,
                "proposal_metadata": {
                    "confidence": confidence,
                    "questions": list(suggestion["questions"]),
                    "suggestion_id": suggestion["id"],
                    "selected_alternative_id": selected_alternative_id,
                },
            }
        )
        proposal["claims"].append(
            {
                "id": claim_id,
                "fact_id": fact_id,
                "category": suggestion["category"],
                "statement": f"{field}: {value}",
                "status": "unverified",
                "evidence_refs": [binding["source_id"]],
                "allowed_outputs": [
                    "cv",
                    "cover_letter",
                    "email",
                    "linkedin",
                    "interview",
                ],
                "source_spans": [
                    {"line_start": anchor["line_start"], "line_end": anchor["line_end"]}
                ],
            }
        )
        record["claim_ids"].append(claim_id)
        if field.startswith("details["):
            record.setdefault("details", []).append(
                {"text": value, "fact_id": fact_id, "claim_id": claim_id}
            )
        elif field.startswith("technologies["):
            record.setdefault("technologies", []).append(
                {"name": value, "fact_id": fact_id, "claim_id": claim_id}
            )
        else:
            existing_fact = record["field_fact_ids"].get(field)
            if existing_fact is not None:
                raise ImportContractError(
                    "conflicting_ai_structure",
                    "Selections assign conflicting values to one record field",
                )
            record[field] = value
            record["field_fact_ids"][field] = fact_id
            record["field_claim_ids"][field] = claim_id
        added_fact_ids.append(fact_id)
        added_claim_ids.append(claim_id)
        applied_suggestion_ids.add(suggestion["id"])

    audit_entry = {
        "contract": AI_VALIDATED_CONTRACT,
        "contract_version": AI_STRUCTURE_CONTRACT_VERSION,
        "status": "unverified",
        "binding": copy.deepcopy(binding),
        "applied_suggestion_ids": sorted(applied_suggestion_ids),
    }
    if audit_mode is not None:
        audit_entry["mode"] = audit_mode
    if replace_ai_audit:
        updated["extraction"]["ai_structuring"] = [audit_entry]
    else:
        updated["extraction"].setdefault("ai_structuring", []).append(audit_entry)
    final_errors = validate_proposal(updated)
    if final_errors:
        raise ImportContractError("invalid_ai_merge", "; ".join(final_errors))
    return updated, {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "status": "ai_structure_applied_unverified",
        "proposal_sha256": proposal_cas_sha256(updated),
        "added_fact_ids": added_fact_ids,
        "added_claim_ids": added_claim_ids,
        "applied_suggestion_ids": sorted(applied_suggestion_ids),
        "requires_confirmation": True,
    }


def apply_ai_structure_request(
    request_data: Any,
) -> tuple[dict[str, Any], dict[str, Any]]:
    request = _require_exact_keys(
        request_data,
        {
            "contract",
            "contract_version",
            "base_proposal",
            "expected_proposal_sha256",
            "ai_proposal",
            "selections",
        },
        "request",
        code="invalid_request",
    )
    if (
        request["contract"] != AI_APPLY_REQUEST_CONTRACT
        or request["contract_version"] != AI_STRUCTURE_CONTRACT_VERSION
    ):
        raise ImportContractError(
            "invalid_request", "Unsupported AI apply request contract"
        )
    base, lines, binding = _validate_ai_base(
        request["base_proposal"], request["expected_proposal_sha256"]
    )
    validated = _validate_provider_ai_proposal(request["ai_proposal"], lines, binding)
    suggestions = {item["id"]: item for item in validated["suggestions"]}
    selections = request["selections"]
    if not isinstance(selections, list) or not selections or len(selections) > 2000:
        raise ImportContractError(
            "invalid_selection", "Selections must be a non-empty bounded list"
        )
    selected: list[tuple[dict[str, Any], str, dict[str, Any], float, str | None]] = []
    seen: set[str] = set()
    for index, raw_selection in enumerate(selections):
        selection = _require_exact_keys(
            raw_selection,
            {"suggestion_id", "alternative_id"},
            f"request.selections[{index}]",
            code="invalid_selection",
        )
        suggestion_id = selection["suggestion_id"]
        alternative_id = selection["alternative_id"]
        if not isinstance(suggestion_id, str) or suggestion_id in seen:
            raise ImportContractError(
                "invalid_selection", "Suggestion selections must be unique"
            )
        if alternative_id is not None and not isinstance(alternative_id, str):
            raise ImportContractError(
                "invalid_selection", "alternative_id must be a string or null"
            )
        suggestion = suggestions.get(suggestion_id)
        if suggestion is None or not suggestion["mergeable"]:
            raise ImportContractError(
                "invalid_selection",
                "Selection references an unknown or non-mergeable suggestion",
            )
        value, anchor, confidence, selected_alternative_id = _selected_candidate(
            suggestion, alternative_id
        )
        selected.append(
            (suggestion, value, anchor, confidence, selected_alternative_id)
        )
        seen.add(suggestion_id)
    return _project_ai_structure(base, binding, selected)


def _materialization_base(
    base: dict[str, Any], validated_ai_conflicts: list[dict[str, Any]]
) -> dict[str, Any]:
    source_proposal = base["proposal"]
    confirmation = base.get("confirmation")
    if (
        not isinstance(confirmation, dict)
        or confirmation.get("required") is not True
        or not isinstance(confirmation.get("rule"), str)
        or not confirmation["rule"].strip()
    ):
        raise ImportContractError(
            "invalid_proposal", "Base proposal confirmation policy is invalid"
        )
    profile = copy.deepcopy(source_proposal["profile"])
    certifications = copy.deepcopy(source_proposal["certifications"])
    preserved_claim_ids = {item["claim_id"] for item in profile["facts"]} | {
        claim_id for record in certifications for claim_id in record["claim_ids"]
    }
    preserved_claims = [
        copy.deepcopy(item)
        for item in source_proposal["claims"]
        if item["id"] in preserved_claim_ids
    ]
    preserved_fact_ids = {item["fact_id"] for item in preserved_claims}
    preserved_facts = [
        copy.deepcopy(item)
        for item in source_proposal["facts"]
        if item["id"] in preserved_fact_ids
    ]
    if len(preserved_claims) != len(preserved_claim_ids) or len(preserved_facts) != len(
        preserved_fact_ids
    ):
        raise ImportContractError(
            "invalid_proposal",
            "Preserved profile or certification provenance is incomplete",
        )

    preserved_source_ids = {base["source"]["id"]}
    for item in [*preserved_facts, *preserved_claims, *profile["facts"]]:
        preserved_source_ids.update(item.get("evidence_refs", []))
    for record in certifications:
        preserved_source_ids.update(record.get("evidence_refs", []))

    source_keys = ("id", "type", "media_type", "sha256", "byte_size")
    extraction_keys = (
        "engine",
        "text_sha256",
        "line_count",
        "line_manifest",
        "warnings",
    )
    retained_sources = [
        item for item in base["sources"] if item["id"] in preserved_source_ids
    ]
    if (
        any(key not in base["source"] for key in source_keys)
        or any(key not in base["extraction"] for key in extraction_keys)
        or any(any(key not in item for key in source_keys) for item in retained_sources)
    ):
        raise ImportContractError(
            "invalid_proposal", "Base source or extraction metadata is incomplete"
        )
    updated = {
        "contract": CONTRACT,
        "contract_version": CONTRACT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "state": "needs_user_confirmation",
        "publishable": False,
        "source": {
            key: copy.deepcopy(base["source"][key])
            for key in source_keys
            if key in base["source"]
        },
        "sources": [
            {key: copy.deepcopy(item[key]) for key in source_keys if key in item}
            for item in retained_sources
        ],
        "extraction": {
            **{
                key: copy.deepcopy(base["extraction"][key])
                for key in extraction_keys
                if key in base["extraction"]
            },
            "conflicts": copy.deepcopy(validated_ai_conflicts),
        },
        "confirmation": {
            "required": True,
            "rule": confirmation["rule"],
        },
    }
    updated["proposal"] = {
        "profile": profile,
        "facts": preserved_facts,
        "claims": preserved_claims,
        "experience": [],
        "projects": [],
        "education": [],
        "certifications": certifications,
        "skills": [],
        "languages": [],
        "additional_facts": [],
    }
    return updated


def materialize_ai_structure_request(request_data: Any) -> dict[str, Any]:
    request = _require_exact_keys(
        request_data,
        {
            "contract",
            "contract_version",
            "base_proposal",
            "expected_proposal_sha256",
            "ai_proposal",
        },
        "request",
        code="invalid_request",
    )
    if (
        request["contract"] != AI_MATERIALIZATION_REQUEST_CONTRACT
        or request["contract_version"] != AI_STRUCTURE_CONTRACT_VERSION
    ):
        raise ImportContractError(
            "invalid_request", "Unsupported AI materialization request contract"
        )
    base, lines, binding = _validate_ai_base(
        request["base_proposal"], request["expected_proposal_sha256"]
    )
    validated = _validate_provider_ai_proposal(request["ai_proposal"], lines, binding)
    primary_suggestions = [
        suggestion
        for suggestion in validated["suggestions"]
        if suggestion["mergeable"] and suggestion["value"] is not None
    ]
    if not primary_suggestions:
        raise ImportContractError(
            "ai_materialization_no_usable_facts",
            "AI recognition version has no mergeable non-null primary facts",
        )
    if len(primary_suggestions) > MAX_AI_MATERIALIZED_FACTS:
        raise ImportContractError(
            "ai_materialization_too_large",
            "AI recognition version exceeds the materialized fact limit",
        )
    selected = []
    for suggestion in primary_suggestions:
        value, anchor, confidence, selected_alternative_id = _selected_candidate(
            suggestion, None
        )
        selected.append(
            (suggestion, value, anchor, confidence, selected_alternative_id)
        )
    materialized, _summary = _project_ai_structure(
        _materialization_base(base, validated["conflicts"]),
        binding,
        selected,
        audit_mode="replace_recognition_version",
        replace_ai_audit=True,
    )
    return materialized


def extend_user_facts(
    proposal_data: Any, additions: Any, expected_proposal_sha256: str
) -> tuple[dict[str, Any], list[str]]:
    errors = validate_proposal(proposal_data)
    if errors:
        raise ImportContractError("invalid_proposal", "; ".join(errors))
    actual_digest = proposal_cas_sha256(proposal_data)
    if expected_proposal_sha256 != actual_digest:
        raise ImportContractError(
            "cas_mismatch", "CV proposal changed since it was loaded"
        )
    if not isinstance(additions, list) or not additions:
        raise ImportContractError(
            "invalid_additions", "User additions must be a non-empty list"
        )
    canonical_additions = json.dumps(
        additions, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    source_sha = hashlib.sha256(canonical_additions.encode("utf-8")).hexdigest()
    source_id = f"source-user-{source_sha[:16]}"
    updated = copy.deepcopy(proposal_data)
    updated["sources"].append(
        {
            "id": source_id,
            "type": "user_supplied_facts",
            "media_type": "application/vnd.bewerbung.user-facts+json",
            "sha256": source_sha,
            "byte_size": len(canonical_additions.encode("utf-8")),
        }
    )
    proposal = updated["proposal"]
    record_maps = {
        collection: {record["id"]: record for record in proposal[collection]}
        for collection in USER_FACT_FIELDS
        if collection not in {"profile", "additional_facts"}
    }
    added_fact_ids: list[str] = []
    seen_addition_ids: set[str] = set()
    prefixes = {
        "experience": "experience",
        "projects": "project",
        "education": "education",
        "certifications": "certification",
        "skills": "skill",
        "languages": "language",
    }
    for index, addition in enumerate(additions):
        if not isinstance(addition, dict):
            raise ImportContractError(
                "invalid_additions", f"Addition {index} must be a mapping"
            )
        addition_id = str(addition.get("id", ""))
        collection = str(addition.get("collection", ""))
        field = str(addition.get("field", ""))
        value = addition.get("value")
        category = str(addition.get("category", "other"))
        if (
            not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", addition_id)
            or addition_id in seen_addition_ids
        ):
            raise ImportContractError(
                "invalid_additions",
                f"Addition {index} has an invalid or duplicate server ID",
            )
        seen_addition_ids.add(addition_id)
        if (
            collection not in USER_FACT_FIELDS
            or field not in USER_FACT_FIELDS[collection]
        ):
            raise ImportContractError(
                "invalid_additions",
                f"Addition {index} has a disallowed collection or field",
            )
        if category not in USER_FACT_CATEGORIES:
            raise ImportContractError(
                "invalid_additions", f"Addition {index} has a disallowed category"
            )
        if not isinstance(value, str) or not value.strip() or len(value) > 5000:
            raise ImportContractError(
                "invalid_additions", f"Addition {index} value is empty or too long"
            )
        value = value.strip()
        if (
            field.endswith("date")
            and value != "present"
            and not re.fullmatch(r"\d{4}(?:-\d{2}(?:-\d{2})?)?", value)
        ):
            raise ImportContractError(
                "invalid_additions",
                f"Addition {index} date must use YYYY, YYYY-MM, YYYY-MM-DD, or present",
            )

        if collection == "profile":
            record_id = "profile"
            record = None
        elif collection == "additional_facts":
            record_id = _stable_id(
                "additional", source_id, "user-supplied", addition_id
            )
            record = None
        else:
            supplied_record_id = str(addition.get("record_id", ""))
            if supplied_record_id:
                record = record_maps[collection].get(supplied_record_id)
                if record is None:
                    raise ImportContractError(
                        "invalid_additions",
                        f"Addition {index} references an unknown record",
                    )
                record_id = supplied_record_id
            else:
                record_key = str(addition.get("record_key", ""))
                if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", record_key):
                    raise ImportContractError(
                        "invalid_additions",
                        f"Addition {index} needs a server-generated record_key",
                    )
                record_id = _stable_id(
                    prefixes[collection], source_id, "user-supplied", record_key
                )
                record = record_maps[collection].get(record_id)
                if record is None:
                    record = {
                        "id": record_id,
                        "status": "unverified",
                        "evidence_refs": [source_id],
                        "claim_ids": [],
                        "field_fact_ids": {},
                        "field_claim_ids": {},
                    }
                    if collection == "experience":
                        record.update(details=[], location="", employment_type="")
                    proposal[collection].append(record)
                    record_maps[collection][record_id] = record

        stored_field = field
        if field == "detail" and record is not None:
            stored_field = f"details[{len(record.get('details', []))}]"
        fact_id = _stable_id(
            "fact",
            source_id,
            category,
            f"{addition_id}\0{record_id}\0{stored_field}\0{value}",
        )
        claim_id = _stable_id("claim", source_id, category, fact_id)
        anchor = {
            "origin": "user_supplied",
            "source_id": source_id,
            "source_sha256": source_sha,
            "addition_id": addition_id,
            "line_start": 0,
            "line_end": 0,
        }
        proposal["facts"].append(
            {
                "id": fact_id,
                "claim_id": claim_id,
                "category": category,
                "record_id": record_id,
                "field": stored_field,
                "value": value,
                "status": "unverified",
                "evidence_refs": [source_id],
                "source_anchor": anchor,
            }
        )
        proposal["claims"].append(
            {
                "id": claim_id,
                "fact_id": fact_id,
                "category": category,
                "statement": f"{stored_field}: {value}",
                "status": "unverified",
                "evidence_refs": [source_id],
                "allowed_outputs": [
                    "cv",
                    "cover_letter",
                    "email",
                    "linkedin",
                    "interview",
                ],
                "source_spans": [{"line_start": 0, "line_end": 0}],
            }
        )
        if collection == "profile":
            proposal["profile"]["facts"].append(
                {
                    "id": fact_id,
                    "field": field,
                    "value": value,
                    "status": "unverified",
                    "evidence_refs": [source_id],
                    "fact_id": fact_id,
                    "claim_id": claim_id,
                }
            )
        elif collection == "additional_facts":
            proposal["additional_facts"].append(
                {
                    "id": record_id,
                    "text": value,
                    "fact_id": fact_id,
                    "claim_id": claim_id,
                }
            )
        elif record is not None:
            if field == "detail":
                record.setdefault("details", []).append(
                    {"text": value, "fact_id": fact_id, "claim_id": claim_id}
                )
            else:
                previous_fact = record["field_fact_ids"].get(field)
                if previous_fact:
                    updated["extraction"]["conflicts"].append(
                        {
                            "code": "user_supplied_field_conflict",
                            "record_id": record_id,
                            "field": field,
                            "left_fact_id": previous_fact,
                            "right_fact_id": fact_id,
                            "detail": "Imported and user-supplied values require an explicit choice",
                        }
                    )
                record[field] = value
                record["field_fact_ids"][field] = fact_id
                record["field_claim_ids"][field] = claim_id
            record["claim_ids"].append(claim_id)
            if source_id not in record["evidence_refs"]:
                record["evidence_refs"].append(source_id)
        added_fact_ids.append(fact_id)
    final_errors = validate_proposal(updated)
    if final_errors:
        raise ImportContractError("invalid_additions", "; ".join(final_errors))
    return updated, added_fact_ids


def _candidate_digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ImportContractError(
            "candidate_unreadable", "Candidate profile cannot be read"
        ) from exc


def _history_path(candidate_path: Path) -> Path:
    return candidate_path.with_suffix(candidate_path.suffix + ".history.jsonl")


def _append_jsonl_record(path: Path, record: dict[str, Any], error_code: str, error_detail: str) -> None:
    payload = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            offset = 0
            while offset < len(payload):
                written = os.write(descriptor, payload[offset:])
                if written <= 0:
                    raise OSError("append made no progress")
                offset += written
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ImportContractError(error_code, error_detail) from exc


def _append_history_record(path: Path, record: dict[str, Any]) -> None:
    _append_jsonl_record(
        path,
        record,
        "history_write_failed",
        "Candidate history could not be durably written",
    )


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        # Python cannot obtain a directory handle with fsync semantics on Windows;
        # the replacement file itself is fsynced before the atomic os.replace.
        return
    try:
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ImportContractError(
            "candidate_write_failed", "Candidate directory metadata could not be synced"
        ) from exc


def _read_history(path: Path) -> list[dict[str, Any]]:
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ImportContractError(
            "recovery_required", "Candidate history cannot be read"
        ) from exc
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImportContractError(
                "recovery_required", "Candidate history contains an incomplete record"
            ) from exc
        if not isinstance(record, dict):
            raise ImportContractError(
                "recovery_required", "Candidate history contains an invalid record"
            )
        records.append(record)
    return records


def _incomplete_intents(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    intents = {
        str(record.get("transaction_id")): record
        for record in history
        if record.get("state") == "intent" and record.get("transaction_id")
    }
    completed = {
        str(record.get("transaction_id"))
        for record in history
        if record.get("state")
        in {"committed", "committed_recovered", "aborted", "aborted_recovered"}
        and record.get("transaction_id")
    }
    return [
        record
        for transaction_id, record in intents.items()
        if transaction_id not in completed
    ]


def _recover_incomplete_intents(candidate_path: Path, current_sha256: str) -> list[str]:
    history_path = _history_path(candidate_path)
    recovered: list[str] = []
    for intent in _incomplete_intents(_read_history(history_path)):
        transaction_id = str(intent.get("transaction_id"))
        before = str(intent.get("before_sha256", ""))
        after = str(intent.get("after_sha256", ""))
        if current_sha256 == before:
            state = "aborted_recovered"
        elif current_sha256 == after:
            state = "committed_recovered"
        else:
            raise ImportContractError(
                "recovery_required",
                "Candidate and incomplete history intent cannot be reconciled safely",
            )
        _append_history_record(
            history_path,
            {
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "operation": str(intent.get("operation", "candidate_profile_mutation")),
                "state": state,
                "transaction_id": transaction_id,
                "candidate_sha256": current_sha256,
            },
        )
        recovered.append(transaction_id)
    return recovered


def recovery_status(candidate_path: str | Path) -> dict[str, Any]:
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        current_sha256 = _candidate_digest(path)
        incomplete = _incomplete_intents(_read_history(_history_path(path)))
        return {
            "status": "recovery_required" if incomplete else "consistent",
            "candidate_sha256": current_sha256,
            "incomplete_transactions": [
                {
                    "transaction_id": item.get("transaction_id"),
                    "before_sha256": item.get("before_sha256"),
                    "after_sha256": item.get("after_sha256"),
                    "classification": "not_applied"
                    if current_sha256 == item.get("before_sha256")
                    else "applied_without_completion"
                    if current_sha256 == item.get("after_sha256")
                    else "ambiguous",
                }
                for item in incomplete
            ],
        }


@contextmanager
def _exclusive_profile_lock(candidate_path: Path) -> Iterator[None]:
    lock_path = candidate_path.with_name(f".{candidate_path.name}.adopt.lock")
    token = secrets.token_hex(16)
    deadline = time.monotonic() + PROFILE_LOCK_WAIT_SECONDS
    descriptor: int | None = None
    while descriptor is None:
        try:
            descriptor = os.open(lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            try:
                age = max(0.0, time.time() - lock_path.stat().st_mtime)
            except FileNotFoundError:
                continue
            if age > PROFILE_LOCK_STALE_SECONDS:
                raise ImportContractError(
                    "profile_lock_stale",
                    "A stale candidate profile lock requires explicit recovery",
                ) from exc
            if time.monotonic() >= deadline:
                raise ImportContractError(
                    "profile_locked", "Candidate profile is locked by another process"
                ) from exc
            time.sleep(0.025)
        except OSError as exc:
            raise ImportContractError(
                "profile_lock_failed", "Candidate profile lock could not be created"
            ) from exc
    try:
        lock_payload = json.dumps(
            {
                "token": token,
                "pid": os.getpid(),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
            sort_keys=True,
        ).encode()
        offset = 0
        while offset < len(lock_payload):
            written = os.write(descriptor, lock_payload[offset:])
            if written <= 0:
                raise OSError("lock write made no progress")
            offset += written
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        yield
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            lock_record = json.loads(lock_path.read_text(encoding="utf-8"))
            if isinstance(lock_record, dict) and lock_record.get("token") == token:
                lock_path.unlink()
        except (OSError, json.JSONDecodeError):
            # Fail closed: never delete a lock whose ownership cannot be proven.
            pass


def _commit_candidate_profile_locked(
    path: Path,
    expected_candidate_sha256: str,
    before_sha256: str,
    updated_candidate: dict[str, Any],
    operation: str,
    intent_metadata: dict[str, Any],
) -> tuple[str, str]:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(
                updated_candidate, stream, allow_unicode=True, sort_keys=False
            )
            stream.flush()
            os.fsync(stream.fileno())
        after_sha256 = hashlib.sha256(temporary_path.read_bytes()).hexdigest()
        if _candidate_digest(path) != expected_candidate_sha256:
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed during mutation"
            )
        transaction_id = secrets.token_hex(16)
        intent = {
            **intent_metadata,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "state": "intent",
            "transaction_id": transaction_id,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
        }
        _append_history_record(_history_path(path), intent)
        if _candidate_digest(path) != expected_candidate_sha256:
            _append_history_record(
                _history_path(path),
                {
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "operation": operation,
                    "state": "aborted",
                    "transaction_id": transaction_id,
                    "candidate_sha256": _candidate_digest(path),
                },
            )
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed during mutation"
            )
        try:
            os.replace(temporary_name, path)
            _fsync_parent_directory(path.parent)
        except OSError as exc:
            if _candidate_digest(path) == before_sha256:
                _append_history_record(
                    _history_path(path),
                    {
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "operation": operation,
                        "state": "aborted",
                        "transaction_id": transaction_id,
                        "candidate_sha256": before_sha256,
                    },
                )
            raise ImportContractError(
                "candidate_write_failed", "Candidate profile could not be replaced"
            ) from exc
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    actual_after = _candidate_digest(path)
    if actual_after != intent["after_sha256"]:
        raise ImportContractError(
            "recovery_required",
            "Candidate replacement digest does not match its intent",
        )
    _append_history_record(
        _history_path(path),
        {
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "operation": operation,
            "state": "committed",
            "transaction_id": transaction_id,
            "candidate_sha256": actual_after,
        },
    )
    return actual_after, transaction_id


def _append_snapshot_index_record(path: Path, record: dict[str, Any]) -> None:
    """Snapshot bookkeeping is a separate ledger from the candidate history."""
    _append_jsonl_record(
        path,
        record,
        "snapshot_write_failed",
        "Profile snapshot index could not be durably written",
    )


def _snapshot_root(candidate_path: Path) -> Path:
    return candidate_path.with_suffix(candidate_path.suffix + ".snapshots")


def _snapshot_index_path(candidate_path: Path) -> Path:
    return _snapshot_root(candidate_path) / "index.jsonl"


def _snapshot_content_path(candidate_path: Path, snapshot_id: str) -> Path:
    if not re.fullmatch(r"profile-snapshot-[a-f0-9]{16}", snapshot_id):
        raise ImportContractError(
            "invalid_snapshot", "Snapshot ID is not a valid profile snapshot ID"
        )
    return _snapshot_root(candidate_path) / f"{snapshot_id}.yaml"


def _read_snapshot_index(candidate_path: Path) -> list[dict[str, Any]]:
    """Snapshot index entries in append order; the newest entry per ID wins."""
    path = _snapshot_index_path(candidate_path)
    try:
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ImportContractError(
            "snapshot_unreadable", "Profile snapshot index cannot be read"
        ) from exc
    entries: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ImportContractError(
                "snapshot_unreadable", "Profile snapshot index contains an invalid record"
            ) from exc
        if not isinstance(record, dict):
            raise ImportContractError(
                "snapshot_unreadable", "Profile snapshot index contains an invalid record"
            )
        snapshot_id = str(record.get("snapshot_id", ""))
        if not re.fullmatch(r"profile-snapshot-[a-f0-9]{16}", snapshot_id):
            raise ImportContractError(
                "snapshot_unreadable", "Profile snapshot index contains an invalid ID"
            )
        if record.get("state") == "removed":
            entries.pop(snapshot_id, None)
            continue
        if snapshot_id not in entries:
            order.append(snapshot_id)
        entries[snapshot_id] = record
    return [entries[snapshot_id] for snapshot_id in order if snapshot_id in entries]


def _write_snapshot_content(path: Path, payload: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
        _fsync_parent_directory(path.parent)
    except OSError as exc:
        temporary_path.unlink(missing_ok=True)
        raise ImportContractError(
            "snapshot_write_failed", "Profile snapshot could not be durably written"
        ) from exc


def _capture_profile_snapshot_locked(
    candidate_path: Path,
    reason: str,
    label: str | None = None,
    related_transaction_id: str | None = None,
) -> dict[str, Any]:
    """Copies the current profile byte-for-byte into the snapshot store.

    Content is addressed by its own digest, so repeating a snapshot of an
    unchanged profile reuses the stored bytes instead of duplicating them.
    """
    try:
        payload = candidate_path.read_bytes()
    except OSError as exc:
        raise ImportContractError(
            "candidate_unreadable", "Candidate profile cannot be read"
        ) from exc
    if len(payload) > MAX_PROFILE_SNAPSHOT_BYTES:
        raise ImportContractError(
            "snapshot_too_large", "Candidate profile exceeds the snapshot size limit"
        )
    digest = hashlib.sha256(payload).hexdigest()
    if label is not None and (
        not label.strip() or len(label) > PROFILE_SNAPSHOT_LABEL_MAX
    ):
        raise ImportContractError(
            "invalid_snapshot", "Snapshot label is empty or too long"
        )
    root = _snapshot_root(candidate_path)
    try:
        root.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise ImportContractError(
            "snapshot_write_failed", "Profile snapshot directory cannot be created"
        ) from exc
    existing = _read_snapshot_index(candidate_path)
    reused = next(
        (item for item in existing if str(item.get("candidate_sha256")) == digest), None
    )
    if reused is not None:
        return {**reused, "reused": True}
    snapshot_id = f"profile-snapshot-{secrets.token_hex(8)}"
    _write_snapshot_content(_snapshot_content_path(candidate_path, snapshot_id), payload)
    document = yaml.safe_load(payload.decode("utf-8"))
    entry = {
        "snapshot_id": snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "candidate_sha256": digest,
        "byte_size": len(payload),
        "reason": reason,
        "claim_count": len(document.get("claims", []))
        if isinstance(document, dict)
        else 0,
        **({"label": label} if label else {}),
        **(
            {"related_transaction_id": related_transaction_id}
            if related_transaction_id
            else {}
        ),
    }
    _append_snapshot_index_record(_snapshot_index_path(candidate_path), entry)
    _prune_profile_snapshots(candidate_path)
    return {**entry, "reused": False}


def _prune_profile_snapshots(candidate_path: Path) -> None:
    """Drops the oldest snapshots beyond the retention bound, oldest first."""
    entries = _read_snapshot_index(candidate_path)
    surplus = len(entries) - MAX_PROFILE_SNAPSHOTS
    if surplus <= 0:
        return
    for entry in entries[:surplus]:
        snapshot_id = str(entry["snapshot_id"])
        _snapshot_content_path(candidate_path, snapshot_id).unlink(missing_ok=True)
        _append_snapshot_index_record(
            _snapshot_index_path(candidate_path),
            {
                "snapshot_id": snapshot_id,
                "state": "removed",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "reason": "retention_limit",
            },
        )


def _read_profile_snapshot(candidate_path: Path, snapshot_id: str) -> tuple[bytes, dict[str, Any]]:
    # Validate the ID shape before it is used for lookup or path construction.
    path = _snapshot_content_path(candidate_path, snapshot_id)
    entry = next(
        (
            item
            for item in _read_snapshot_index(candidate_path)
            if str(item.get("snapshot_id")) == snapshot_id
        ),
        None,
    )
    if entry is None:
        raise ImportContractError("unknown_snapshot", "Profile snapshot does not exist")
    try:
        payload = path.read_bytes()
    except OSError as exc:
        raise ImportContractError(
            "snapshot_unreadable", "Profile snapshot content cannot be read"
        ) from exc
    if hashlib.sha256(payload).hexdigest() != str(entry.get("candidate_sha256")):
        raise ImportContractError(
            "snapshot_corrupted", "Profile snapshot content does not match its digest"
        )
    return payload, entry


def capture_profile_snapshot(
    candidate_path: str | Path,
    expected_candidate_sha256: str,
    label: str | None = None,
    reason: str = "manual",
) -> dict[str, Any]:
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        actual_digest = _candidate_digest(path)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256)
            or actual_digest != expected_candidate_sha256
        ):
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed since it was loaded"
            )
        snapshot = _capture_profile_snapshot_locked(path, reason, label)
        return {
            "status": "profile_snapshot_captured",
            "candidate_sha256": actual_digest,
            "snapshot": snapshot,
        }


def list_profile_snapshots(candidate_path: str | Path) -> dict[str, Any]:
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        current = _candidate_digest(path)
        snapshots = _read_snapshot_index(path)
        return {
            "status": "profile_snapshot_list",
            "candidate_sha256": current,
            "snapshots": [
                {**item, "current": str(item.get("candidate_sha256")) == current}
                for item in snapshots
            ],
        }


def restore_profile_snapshot(
    candidate_path: str | Path,
    snapshot_id: str,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        actual_digest = _candidate_digest(path)
        recovered_transaction_ids = _recover_incomplete_intents(path, actual_digest)
        actual_digest = _candidate_digest(path)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256)
            or actual_digest != expected_candidate_sha256
        ):
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed since it was loaded"
            )
        payload, entry = _read_profile_snapshot(path, snapshot_id)
        if str(entry.get("candidate_sha256")) == actual_digest:
            return {
                "status": "profile_already_at_snapshot",
                "candidate_sha256": actual_digest,
                "snapshot_id": snapshot_id,
                "recovered_transaction_ids": recovered_transaction_ids,
            }
        try:
            restored = yaml.safe_load(payload.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ImportContractError(
                "snapshot_corrupted", "Profile snapshot is not readable YAML"
            ) from exc
        if not isinstance(restored, dict):
            raise ImportContractError(
                "snapshot_corrupted", "Profile snapshot is not a candidate profile"
            )
        errors = validate_candidate(restored)
        if errors:
            raise ImportContractError("snapshot_invalid", "; ".join(errors))
        # Snapshot the pre-restore state so a restore is itself reversible.
        replaced = _capture_profile_snapshot_locked(path, "pre_restore")
        after_digest, transaction_id = _commit_candidate_profile_locked(
            path,
            expected_candidate_sha256,
            actual_digest,
            restored,
            "restore_candidate_profile_snapshot",
            {
                "restored_snapshot_id": snapshot_id,
                "replaced_snapshot_id": replaced["snapshot_id"],
            },
        )
        return {
            "status": "profile_snapshot_restored",
            "candidate_sha256": after_digest,
            "snapshot_id": snapshot_id,
            "replaced_snapshot_id": replaced["snapshot_id"],
            "transaction_id": transaction_id,
            "recovered_transaction_ids": recovered_transaction_ids,
        }


def _adopted_transaction_ledger(
    candidate_path: Path, transaction_id: str
) -> dict[str, Any]:
    """Resolves the committed adoption intent that a revoke is scoped to.

    The intent record is the only place that lists exactly what one adoption
    added, so revoking anything else would either orphan or over-delete claims.
    """
    if not re.fullmatch(r"[a-f0-9]{32}", transaction_id):
        raise ImportContractError(
            "invalid_transaction", "Transaction ID is not a valid adoption transaction"
        )
    history = _read_history(_history_path(candidate_path))
    intent = next(
        (
            record
            for record in history
            if record.get("state") == "intent"
            and str(record.get("transaction_id")) == transaction_id
        ),
        None,
    )
    if intent is None:
        raise ImportContractError(
            "unknown_transaction", "Adoption transaction is not present in the history"
        )
    if str(intent.get("operation")) != "adopt_confirmed_cv_facts":
        raise ImportContractError(
            "invalid_transaction", "Transaction is not a CV adoption"
        )
    completed = any(
        str(record.get("transaction_id")) == transaction_id
        and record.get("state") in {"committed", "committed_recovered"}
        for record in history
    )
    if not completed:
        raise ImportContractError(
            "invalid_transaction", "Adoption transaction was never committed"
        )
    if any(
        str(record.get("revoked_transaction_id")) == transaction_id
        and record.get("state") == "intent"
        for record in history
    ):
        raise ImportContractError(
            "already_revoked", "Adoption transaction was already revoked"
        )
    return intent


def list_adoptions(candidate_path: str | Path) -> dict[str, Any]:
    """Committed adoptions that are still revocable, oldest first.

    The server record can lose its adoption link (a re-review clears it) while
    the claims stay in the profile, so the ledger is the authority on what is
    still revocable.
    """
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        current = _candidate_digest(path)
        history = _read_history(_history_path(path))
        completed = {
            str(record.get("transaction_id"))
            for record in history
            if record.get("state") in {"committed", "committed_recovered"}
        }
        revoked = {
            str(record.get("revoked_transaction_id"))
            for record in history
            if record.get("state") == "intent" and record.get("revoked_transaction_id")
        }
        candidate = load_yaml(path)
        present = {
            str(item.get("id"))
            for item in candidate.get("claims", []) or []
            if isinstance(item, dict)
        }
        adoptions = []
        for record in history:
            transaction_id = str(record.get("transaction_id", ""))
            if (
                record.get("state") != "intent"
                or record.get("operation") != "adopt_confirmed_cv_facts"
                or transaction_id not in completed
                or transaction_id in revoked
            ):
                continue
            claim_ids = [
                str(item)
                for item in record.get("adopted_claim_ids", []) or []
                if isinstance(item, str)
            ]
            adoptions.append(
                {
                    "transaction_id": transaction_id,
                    "occurred_at": record.get("occurred_at"),
                    "source_sha256": record.get("proposal_source_sha256"),
                    "claim_count": len(claim_ids),
                    "present_claim_count": len([c for c in claim_ids if c in present]),
                    "before_sha256": record.get("before_sha256"),
                    "after_sha256": record.get("after_sha256"),
                    **(
                        {"replaced_snapshot_id": record["replaced_snapshot_id"]}
                        if record.get("replaced_snapshot_id")
                        else {}
                    ),
                }
            )
        return {
            "status": "adoption_list",
            "candidate_sha256": current,
            "adoptions": adoptions,
        }


def revoke_claims(
    candidate_path: str | Path,
    transaction_id: str,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    """Removes exactly what one committed adoption added, CAS-bound.

    Claims, adopted records and now-unreferenced sources are dropped. Profile
    scalars that the adoption overwrote cannot be reconstructed from the ledger;
    the pre-revoke snapshot and the adoption's `before_sha256` are reported so a
    caller can offer a full rollback instead.
    """
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        actual_digest = _candidate_digest(path)
        recovered_transaction_ids = _recover_incomplete_intents(path, actual_digest)
        actual_digest = _candidate_digest(path)
        if (
            not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256)
            or actual_digest != expected_candidate_sha256
        ):
            raise ImportContractError(
                "cas_mismatch", "Candidate profile changed since it was loaded"
            )
        intent = _adopted_transaction_ledger(path, transaction_id)
        revoked_claim_ids = {
            str(item)
            for item in intent.get("adopted_claim_ids", [])
            if isinstance(item, str)
        }
        revoked_record_ids = {
            str(item)
            for item in intent.get("adopted_record_ids", [])
            if isinstance(item, str)
        }
        if not revoked_claim_ids:
            raise ImportContractError(
                "invalid_transaction", "Adoption transaction recorded no claims"
            )
        candidate = load_yaml(path)
        updated = copy.deepcopy(candidate)
        present_claim_ids = {
            str(item.get("id"))
            for item in updated.get("claims", [])
            if isinstance(item, dict)
        } & revoked_claim_ids
        if not present_claim_ids:
            return {
                "status": "no_revocable_claims",
                "candidate_sha256": actual_digest,
                "transaction_id": transaction_id,
                "revoked_claim_ids": [],
                "revoked_record_ids": [],
                "recovered_transaction_ids": recovered_transaction_ids,
            }
        updated["claims"] = [
            item
            for item in updated.get("claims", [])
            if not (isinstance(item, dict) and str(item.get("id")) in revoked_claim_ids)
        ]
        removed_record_ids: list[str] = []
        for collection in (
            "experience",
            "projects",
            "education",
            "certifications",
            "skills",
            "languages",
        ):
            retained: list[Any] = []
            for record in updated.get(collection, []) or []:
                if not isinstance(record, dict):
                    retained.append(record)
                    continue
                record_id = str(record.get("id"))
                remaining = [
                    claim_id
                    for claim_id in record.get("claim_ids", []) or []
                    if str(claim_id) not in revoked_claim_ids
                ]
                if record_id in revoked_record_ids and not remaining:
                    removed_record_ids.append(record_id)
                    continue
                # A record the adoption only partly contributed to survives with
                # its foreign claims intact.
                record["claim_ids"] = remaining
                retained.append(record)
            updated[collection] = retained
        referenced_sources = {
            str(ref)
            for item in updated.get("claims", [])
            if isinstance(item, dict)
            for ref in item.get("evidence_refs", []) or []
        }
        removed_source_ids = sorted(
            {
                str(item.get("id"))
                for item in updated.get("sources", []) or []
                if isinstance(item, dict) and str(item.get("id")) not in referenced_sources
            }
        )
        updated["sources"] = [
            item
            for item in updated.get("sources", []) or []
            if not isinstance(item, dict) or str(item.get("id")) in referenced_sources
        ]
        errors = validate_candidate(updated)
        if errors:
            raise ImportContractError("candidate_validation_failed", "; ".join(errors))
        # Snapshot before mutating so the revoke itself stays reversible.
        replaced = _capture_profile_snapshot_locked(
            path, "pre_revoke", related_transaction_id=transaction_id
        )
        before_sha256 = str(intent.get("before_sha256", ""))
        rollback_snapshot_id = next(
            (
                str(item.get("snapshot_id"))
                for item in _read_snapshot_index(path)
                if str(item.get("candidate_sha256")) == before_sha256
            ),
            None,
        )
        after_digest, revoke_transaction_id = _commit_candidate_profile_locked(
            path,
            expected_candidate_sha256,
            actual_digest,
            updated,
            "revoke_adopted_cv_claims",
            {
                "revoked_transaction_id": transaction_id,
                "revoked_claim_ids": sorted(present_claim_ids),
                "revoked_record_ids": sorted(removed_record_ids),
                "replaced_snapshot_id": replaced["snapshot_id"],
            },
        )
        return {
            "status": "claims_revoked",
            "candidate_sha256": after_digest,
            "transaction_id": revoke_transaction_id,
            "revoked_transaction_id": transaction_id,
            "revoked_claim_ids": sorted(present_claim_ids),
            "revoked_record_ids": sorted(removed_record_ids),
            "removed_source_ids": removed_source_ids,
            "replaced_snapshot_id": replaced["snapshot_id"],
            "adoption_before_sha256": before_sha256,
            **(
                {"rollback_snapshot_id": rollback_snapshot_id}
                if rollback_snapshot_id
                else {}
            ),
            "recovered_transaction_ids": recovered_transaction_ids,
        }


def _adopt_confirmed_locked(
    proposal_data: Any,
    candidate_path: str | Path,
    decisions: Any,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    errors = validate_proposal(proposal_data)
    if errors:
        raise ImportContractError("invalid_proposal", "; ".join(errors))
    if not isinstance(decisions, list):
        raise ImportContractError("invalid_decisions", "Decisions must be a list")
    path = Path(candidate_path)
    actual_digest = _candidate_digest(path)
    recovered_transaction_ids = _recover_incomplete_intents(path, actual_digest)
    actual_digest = _candidate_digest(path)
    if (
        not re.fullmatch(r"[a-f0-9]{64}", expected_candidate_sha256)
        or actual_digest != expected_candidate_sha256
    ):
        raise ImportContractError(
            "cas_mismatch", "Candidate profile changed since it was loaded"
        )
    candidate = load_yaml(path)
    proposal = proposal_data["proposal"]
    proposed_claims = {item["id"]: item for item in proposal["claims"]}
    proposed_facts = {item["id"]: item for item in proposal["facts"]}
    selected_facts: set[str] = set()
    seen: set[str] = set()
    for index, decision in enumerate(decisions):
        if not isinstance(decision, dict):
            raise ImportContractError(
                "invalid_decisions", f"Decision {index} must be a mapping"
            )
        fact_id = str(decision.get("fact_id", ""))
        if fact_id not in proposed_facts or fact_id in seen:
            raise ImportContractError(
                "invalid_decisions",
                f"Decision {index} has an unknown or duplicate fact ID",
            )
        seen.add(fact_id)
        action = decision.get("decision")
        if action not in {"confirm", "reject", "pending"}:
            raise ImportContractError(
                "invalid_decisions", f"Decision {index} has an invalid action"
            )
        if action == "confirm":
            if (
                decision.get("explicitly_confirmed") is not True
                or decision.get("confirmation_origin") != "explicit_local_user_action"
            ):
                raise ImportContractError(
                    "confirmation_required",
                    f"Fact {fact_id} lacks explicit local confirmation",
                )
            selected_facts.add(fact_id)
    selected_claims = {
        proposed_facts[fact_id]["claim_id"] for fact_id in selected_facts
    }
    if not selected_facts:
        return {
            "status": "no_confirmed_facts",
            "candidate_sha256": actual_digest,
            "adopted_fact_ids": [],
            "adopted_claim_ids": [],
            "adopted_record_ids": [],
            "recovered_transaction_ids": recovered_transaction_ids,
        }

    existing_claim_ids = {
        str(item.get("id"))
        for item in candidate.get("claims", [])
        if isinstance(item, dict)
    }
    collisions = sorted(selected_claims & existing_claim_ids)
    if collisions:
        raise ImportContractError(
            "claim_collision", "Confirmed claims already exist in the candidate profile"
        )
    updated = copy.deepcopy(candidate)
    source = proposal_data["source"]
    proposal_sources = {item["id"]: item for item in proposal_data["sources"]}
    selected_source_ids = {
        evidence_ref
        for claim_id in selected_claims
        for evidence_ref in proposed_claims[claim_id]["evidence_refs"]
    }
    for selected_source_id in sorted(selected_source_ids):
        selected_source = proposal_sources[selected_source_id]
        if not any(item.get("id") == selected_source_id for item in updated["sources"]):
            updated["sources"].append(
                {
                    "id": selected_source_id,
                    "type": "user_cv_import"
                    if selected_source_id.startswith("source-cv-")
                    else "user_supplied_fact",
                    "label": "Candidate-confirmed existing CV import"
                    if selected_source_id.startswith("source-cv-")
                    else "Candidate-confirmed user supplied fact",
                    "location": f"sha256:{selected_source['sha256']}",
                }
            )
    for claim_id in sorted(selected_claims):
        source_claim = proposed_claims[claim_id]
        updated["claims"].append(
            {
                "id": claim_id,
                "category": source_claim["category"],
                "statement": source_claim["statement"],
                "status": "user_confirmed",
                "evidence_refs": source_claim["evidence_refs"],
                "allowed_outputs": source_claim["allowed_outputs"],
                "tags": [],
                "valid_from": None,
                "valid_to": None,
                "notes": "Explicitly confirmed from an imported existing CV.",
            }
        )

    for fact in proposal["profile"]["facts"]:
        if fact["fact_id"] not in selected_facts:
            continue
        field = fact["field"]
        if field == "full_name":
            updated["profile"]["full_name"] = fact["value"]
        elif field == "contact.email":
            updated["profile"]["contact"]["email"] = fact["value"]
        elif field == "contact.phone":
            updated["profile"]["contact"]["phone"] = fact["value"]
        elif field == "contact.url":
            lowered = fact["value"].casefold()
            key = (
                "linkedin"
                if "linkedin.com" in lowered
                else "github"
                if "github.com" in lowered
                else "portfolio"
            )
            updated["profile"]["contact"][key] = fact["value"]

    adopted_record_ids: list[str] = []
    required_fields = {
        "experience": {"role", "company", "start_date", "end_date"},
        "projects": {"name"},
        "education": {"name"},
        "certifications": {"name"},
        "skills": {"name"},
        "languages": {"language"},
    }
    for collection in (
        "experience",
        "projects",
        "education",
        "certifications",
        "skills",
        "languages",
    ):
        for record in proposal[collection]:
            field_fact_ids = record["field_fact_ids"]
            if any(
                field_fact_ids.get(field) not in selected_facts
                for field in required_fields[collection]
            ):
                continue
            adopted = copy.deepcopy(record)
            adopted["status"] = "user_confirmed"
            adopted["claim_ids"] = [
                claim_id
                for claim_id in record["claim_ids"]
                if proposed_claims[claim_id]["fact_id"] in selected_facts
            ]
            adopted["evidence_refs"] = sorted(
                {
                    ref
                    for claim_id in adopted["claim_ids"]
                    for ref in proposed_claims[claim_id]["evidence_refs"]
                }
            )
            for field, fact_id in field_fact_ids.items():
                if fact_id not in selected_facts:
                    adopted.pop(field, None)
            adopted.pop("field_fact_ids", None)
            adopted.pop("field_claim_ids", None)
            if "details" in adopted:
                adopted["details"] = [
                    item
                    for item in adopted.get("details", [])
                    if item["fact_id"] in selected_facts
                ]
                for detail in adopted["details"]:
                    detail.pop("fact_id", None)
            if "technologies" in adopted:
                adopted["technologies"] = [
                    item
                    for item in adopted.get("technologies", [])
                    if item["fact_id"] in selected_facts
                ]
                for technology in adopted["technologies"]:
                    technology.pop("fact_id", None)
            updated[collection].append(adopted)
            adopted_record_ids.append(record["id"])
    candidate_errors = validate_candidate(updated)
    if candidate_errors:
        raise ImportContractError(
            "candidate_validation_failed", "; ".join(candidate_errors)
        )

    # Capture the pre-adoption profile so a later revoke can offer a full
    # rollback, including profile scalars this adoption is about to overwrite.
    replaced_snapshot = _capture_profile_snapshot_locked(path, "pre_adoption")
    after_digest, transaction_id = _commit_candidate_profile_locked(
        path,
        expected_candidate_sha256,
        actual_digest,
        updated,
        "adopt_confirmed_cv_facts",
        {
            "replaced_snapshot_id": replaced_snapshot["snapshot_id"],
            "proposal_source_sha256": source["sha256"],
            "adopted_fact_ids": sorted(selected_facts),
            "adopted_claim_ids": sorted(selected_claims),
            "adopted_record_ids": sorted(adopted_record_ids),
            "confirmation_origin": "explicit_local_user_action",
        },
    )
    return {
        "status": "adopted_user_confirmed",
        "candidate_sha256": after_digest,
        "adopted_fact_ids": sorted(selected_facts),
        "adopted_claim_ids": sorted(selected_claims),
        "adopted_record_ids": sorted(adopted_record_ids),
        "rejected_or_pending_fact_ids": sorted(set(proposed_facts) - selected_facts),
        "replaced_snapshot_id": replaced_snapshot["snapshot_id"],
        "history_recorded": True,
        "transaction_id": transaction_id,
        "recovered_transaction_ids": recovered_transaction_ids,
    }


def adopt_confirmed(
    proposal_data: Any,
    candidate_path: str | Path,
    decisions: Any,
    expected_candidate_sha256: str,
) -> dict[str, Any]:
    path = Path(candidate_path)
    with _exclusive_profile_lock(path):
        return _adopt_confirmed_locked(
            proposal_data, path, decisions, expected_candidate_sha256
        )


def _write_yaml_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            yaml.safe_dump(data, stream, allow_unicode=True, sort_keys=False)
        os.replace(temporary_name, path)
    except Exception:
        Path(temporary_name).unlink(missing_ok=True)
        raise


def _read_stdin_utf8() -> str:
    """Read the stdio contract as UTF-8 bytes, independent of the host locale."""
    binary_stream = getattr(sys.stdin, "buffer", None)
    if (
        binary_stream is None
    ):  # Supports in-process StringIO callers without weakening CLI stdio.
        value = sys.stdin.read()
        if len(value.encode("utf-8")) > MAX_CONTRACT_STDIN_BYTES:
            raise ImportContractError(
                "input_too_large", "Standard input exceeds the contract limit"
            )
        return value
    try:
        payload = binary_stream.read(MAX_CONTRACT_STDIN_BYTES + 1)
        if len(payload) > MAX_CONTRACT_STDIN_BYTES:
            raise ImportContractError(
                "input_too_large", "Standard input exceeds the contract limit"
            )
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportContractError(
            "invalid_encoding", "Standard input must be UTF-8"
        ) from exc


def _write_stdout_json(data: Any, *, pretty: bool = False) -> None:
    """Write one UTF-8 JSON record without using the locale-bound text wrapper."""
    serialized = json.dumps(
        data,
        ensure_ascii=False,
        indent=2 if pretty else None,
    )
    payload = f"{serialized}\n".encode("utf-8")  # noqa: UP012 - stdio contract is explicitly UTF-8.
    binary_stream = getattr(sys.stdout, "buffer", None)
    if binary_stream is None:  # Supports in-process StringIO callers.
        sys.stdout.write(payload.decode("utf-8"))
        sys.stdout.flush()
        return
    binary_stream.write(payload)
    binary_stream.flush()


def _load_contract_json(text: str) -> Any:
    def object_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ImportContractError(
                    "invalid_json",
                    "JSON contract objects must not contain duplicate keys",
                )
            result[key] = value
        return result

    def reject_non_finite(_value: str) -> None:
        raise ImportContractError(
            "invalid_json", "JSON contract numbers must be finite"
        )

    return json.loads(
        text,
        object_pairs_hook=object_without_duplicates,
        parse_constant=reject_non_finite,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evidence-safe existing CV import contract"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("capabilities")
    extract = commands.add_parser("extract")
    extract.add_argument("--input", required=True)
    extract.add_argument("--output", required=True)
    normalize = commands.add_parser("normalize-extracted")
    normalize.add_argument("--extracted-envelope", required=True)
    normalize.add_argument("--output", required=True)
    extend = commands.add_parser("extend-user-facts")
    extend.add_argument("--proposal", required=True)
    extend.add_argument("--additions", required=True)
    extend.add_argument("--expected-proposal-sha256", required=True)
    extend.add_argument("--output", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--proposal", required=True)
    adopt = commands.add_parser("adopt-confirmed")
    adopt.add_argument("--proposal", required=True)
    adopt.add_argument("--candidate", required=True)
    adopt.add_argument("--decisions", required=True)
    adopt.add_argument("--expected-candidate-sha256", required=True)
    list_adoptions_command = commands.add_parser("list-adoptions")
    list_adoptions_command.add_argument("--candidate", required=True)
    revoke = commands.add_parser("revoke-claims")
    revoke.add_argument("--candidate", required=True)
    revoke.add_argument("--transaction-id", required=True)
    revoke.add_argument("--expected-candidate-sha256", required=True)
    capture_snapshot = commands.add_parser("capture-profile-snapshot")
    capture_snapshot.add_argument("--candidate", required=True)
    capture_snapshot.add_argument("--expected-candidate-sha256", required=True)
    capture_snapshot.add_argument("--label")
    capture_snapshot.add_argument("--reason", default="manual")
    list_snapshots = commands.add_parser("list-profile-snapshots")
    list_snapshots.add_argument("--candidate", required=True)
    restore_snapshot = commands.add_parser("restore-profile-snapshot")
    restore_snapshot.add_argument("--candidate", required=True)
    restore_snapshot.add_argument("--snapshot-id", required=True)
    restore_snapshot.add_argument("--expected-candidate-sha256", required=True)
    recovery = commands.add_parser("recovery-status")
    recovery.add_argument("--candidate", required=True)
    validate_ai = commands.add_parser("validate-ai-structure")
    validate_ai.add_argument("--request", required=True)
    apply_ai = commands.add_parser("apply-ai-structure")
    apply_ai.add_argument("--request", required=True)
    apply_ai.add_argument("--output", required=True)
    materialize_ai = commands.add_parser("materialize-ai-structure")
    materialize_ai.add_argument("--request", required=True)
    materialize_ai.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        if args.command == "capabilities":
            result = capabilities()
        elif args.command == "extract":
            proposal = extract_cv(args.input)
            _write_yaml_atomic(Path(args.output), proposal)
            result = {
                "contract": CONTRACT,
                "contract_version": CONTRACT_VERSION,
                "status": "proposal_created",
                "source_id": proposal["source"]["id"],
                "claim_count": len(proposal["proposal"]["claims"]),
                "requires_confirmation": True,
            }
        elif args.command == "normalize-extracted":
            envelope_text = (
                _read_stdin_utf8()
                if args.extracted_envelope == "-"
                else Path(args.extracted_envelope).read_text(encoding="utf-8")
            )
            envelope = _load_contract_json(envelope_text)
            proposal = normalize_extracted_envelope(envelope)
            if args.output == "-":
                _write_stdout_json(proposal)
                return 0
            _write_yaml_atomic(Path(args.output), proposal)
            result = {
                "contract": CONTRACT,
                "contract_version": CONTRACT_VERSION,
                "status": "proposal_created",
                "source_id": proposal["source"]["id"],
                "claim_count": len(proposal["proposal"]["claims"]),
                "requires_confirmation": True,
            }
        elif args.command == "extend-user-facts":
            proposal = yaml.safe_load(Path(args.proposal).read_text(encoding="utf-8"))
            additions = json.loads(Path(args.additions).read_text(encoding="utf-8"))
            extended, added_fact_ids = extend_user_facts(
                proposal, additions, args.expected_proposal_sha256
            )
            _write_yaml_atomic(Path(args.output), extended)
            result = {
                "contract": CONTRACT,
                "contract_version": CONTRACT_VERSION,
                "status": "proposal_extended",
                "added_fact_ids": added_fact_ids,
                "proposal_sha256": proposal_cas_sha256(extended),
                "requires_confirmation": True,
            }
        elif args.command == "validate":
            candidate = yaml.safe_load(Path(args.proposal).read_text(encoding="utf-8"))
            errors = validate_proposal(candidate)
            result = {"valid": not errors, "errors": errors}
            _write_stdout_json(result, pretty=True)
            return 0 if not errors else 2
        elif args.command == "validate-ai-structure":
            request_text = (
                _read_stdin_utf8()
                if args.request == "-"
                else Path(args.request).read_text(encoding="utf-8")
            )
            result = validate_ai_structure_request(_load_contract_json(request_text))
        elif args.command == "apply-ai-structure":
            request_text = (
                _read_stdin_utf8()
                if args.request == "-"
                else Path(args.request).read_text(encoding="utf-8")
            )
            merged, result = apply_ai_structure_request(
                _load_contract_json(request_text)
            )
            if args.output == "-":
                _write_stdout_json(merged)
                return 0
            _write_yaml_atomic(Path(args.output), merged)
        elif args.command == "materialize-ai-structure":
            request_text = (
                _read_stdin_utf8()
                if args.request == "-"
                else Path(args.request).read_text(encoding="utf-8")
            )
            materialized = materialize_ai_structure_request(
                _load_contract_json(request_text)
            )
            if args.output == "-":
                _write_stdout_json(materialized)
                return 0
            _write_yaml_atomic(Path(args.output), materialized)
            result = {
                "contract": CONTRACT,
                "contract_version": CONTRACT_VERSION,
                "status": "ai_recognition_version_materialized_unverified",
                "proposal_sha256": proposal_cas_sha256(materialized),
                "requires_confirmation": True,
            }
        elif args.command == "adopt-confirmed":
            proposal = yaml.safe_load(Path(args.proposal).read_text(encoding="utf-8"))
            decisions = json.loads(Path(args.decisions).read_text(encoding="utf-8"))
            result = adopt_confirmed(
                proposal,
                args.candidate,
                decisions,
                args.expected_candidate_sha256,
            )
        elif args.command == "list-adoptions":
            result = list_adoptions(args.candidate)
        elif args.command == "revoke-claims":
            result = revoke_claims(
                args.candidate,
                args.transaction_id,
                args.expected_candidate_sha256,
            )
        elif args.command == "capture-profile-snapshot":
            result = capture_profile_snapshot(
                args.candidate,
                args.expected_candidate_sha256,
                args.label,
                args.reason,
            )
        elif args.command == "list-profile-snapshots":
            result = list_profile_snapshots(args.candidate)
        elif args.command == "restore-profile-snapshot":
            result = restore_profile_snapshot(
                args.candidate,
                args.snapshot_id,
                args.expected_candidate_sha256,
            )
        else:
            result = recovery_status(args.candidate)
    except ImportContractError as exc:
        _write_stdout_json(
            {
                "status": "rejected",
                "error": {"code": exc.code, "safe_detail": exc.detail},
            }
        )
        return 2
    except (OSError, yaml.YAMLError, json.JSONDecodeError):
        _write_stdout_json(
            {
                "status": "rejected",
                "error": {
                    "code": "input_unreadable",
                    "safe_detail": "Input or proposal could not be read",
                },
            }
        )
        return 2
    _write_stdout_json(result, pretty=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
