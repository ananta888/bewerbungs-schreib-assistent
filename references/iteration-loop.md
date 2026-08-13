# Iterative Multi-Role Review

## Modes

Use `compact` for short emails, minor revisions, or explicit speed requests:

1. `author`
2. `combined_reviewer`
3. `finalizer`

Use `standard` by default for CVs and cover letters:

1. `author`
2. `evidence_ats_reviewer`
3. `recruiter_style_reviewer`
4. `finalizer`

Use `rigorous` for important applications, major career changes, executive roles, regulated industries, or explicit maximum-quality requests:

1. `author`
2. `evidence_reviewer`
3. `ats_reviewer`
4. `recruiter_style_reviewer`
5. `finalizer`

Do not add review rounds without a specific unresolved risk. Stop after the configured `max_revision_cycles` and ask the user when a high-impact factual conflict remains.

## Context isolation

Prefer separate subagents or fresh contexts. Pass raw artifacts and the current revision, but not previous hidden reasoning or the expected verdict. This reduces agreement bias.

When separate agents are unavailable, run the same stages sequentially and set `execution: sequential_single_agent`. Never label that result independent validation.

## Role contracts

### Author

Build the first annotated revision from the job analysis, match matrix, candidate claims, and style profile. Optimize relevance without hiding gaps.

### Evidence reviewer

Inspect every factual line against claim IDs, statuses, source references, output permissions, dates, and constraints. Reject unsupported metrics, technologies, seniority, leadership, scope, and achievements. Do not improve prose except where needed to remove an unsupported claim.

### ATS reviewer

Compare the revision against every must-have and important requirement. Check canonical terminology, headings, reading order, keyword placement, unsupported keywords, and repetition. Report coverage by class; never invent a percentage.

### Recruiter and style reviewer

Review the document as a skeptical recruiter with a short initial scan. Identify the likely professional identity, strongest selling point, concern, unclear passage, generic wording, and most convincing evidence. Then verify authenticity against the style profile and improve only supported wording.

### Combined reviewer

Perform evidence, ATS, recruiter, and style checks in that order. Keep the categories separate in the report.

### Finalizer

Read reviewer findings and dispositions. Apply only changes supported by existing evidence. Do not introduce new claims while polishing. Re-run deterministic checks and strip annotations only after validation passes.

## Finding policy

Record every finding with a stable ID, severity, category, description, evidence references, status, and disposition.

- `critical`: fabricated, contradictory, prohibited, or materially misleading information
- `high`: missing must-have evidence, invalid ATS structure, or serious credibility problem
- `medium`: weak prioritization, unclear evidence, or noticeable style mismatch
- `low`: polish that does not affect correctness or positioning

Before finalization, set critical and high findings to `resolved` or `accepted_risk`. Require a concrete disposition for `accepted_risk`. Never silently discard a reviewer finding.

Use [assets/iteration.template.yaml](../assets/iteration.template.yaml) and validate it with `scripts/validate_iteration.py`.
