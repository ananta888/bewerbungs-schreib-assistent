---
name: bewerbungs-schreib-assistent
description: Create, review, and tailor truthful German or English CVs, resumes, cover letters, application emails, LinkedIn summaries, ATS reviews, recruiter reviews, candidate/job matches, and interview preparation. Use when a user provides a job advertisement, CV, candidate profile, writing sample, or requests job-specific application material that must remain ATS-readable, evidence-backed, authentic, and free of invented facts.
---

# Bewerbungs-Schreib-Assistent

Produce application material that passes both the machine filter and the human filter. Preserve factual accuracy, readable language, relevance, and the candidate's voice. Never trade credibility for keyword coverage.

## Locate and Validate Profiles

Resolve profile files in this order:

1. Use paths explicitly provided by the user.
2. Look for `candidate-profile.yaml` and `style-profile.yaml` in the current application project.
3. Look in a private profile directory explicitly configured by the user.
4. Use `*.example.yaml` only as schemas. Never treat example content as candidate facts.

When persistent profiles are missing, use only material supplied for the current task. Offer to initialize private profiles with `python scripts/init_profiles.py --target <directory>`. Never overwrite existing profiles without `--force` and explicit user approval.

Before drafting, run:

```bash
python scripts/validate_profiles.py --candidate <candidate-profile.yaml> --style <style-profile.yaml>
```

Stop and report conflicting dates, broken references, duplicate IDs, or invalid statuses. Do not silently repair factual conflicts.

When an existing CV in HTML, PDF, DOCX, or ODT form is supplied, read
[references/cv-import-contract.md](references/cv-import-contract.md) and create a versioned import
proposal with `scripts/cv_import_contract.py`. Keep it under `.application-work/`. Show its
extraction warnings, conflicts, employment periods, and atomic claims to the candidate. Do not
merge the proposal into `candidate-profile.yaml` and do not upgrade any imported status until the
candidate explicitly confirms the affected facts.

When the user wants a directly visible result, pass `--html-output` to `extract` or
`normalize-extracted` and return the generated self-contained HTML page alongside the private YAML
proposal. The HTML page is a display artifact, not a confirmation or profile mutation, and must
also remain under `.application-work/`.

If the root application offers optional AI-assisted CV structuring, keep the provider call outside
this submodule and require explicit user opt-in. Validate the provider output with the closed
`ai-cv-structure-proposal` contract from [references/cv-import-contract.md](references/cv-import-contract.md).
Apply only explicitly selected, exact-source-anchored suggestions through
`cv_import_contract.py apply-ai-structure`. Provider confidence is review metadata, never evidence;
all applied facts remain `unverified` until the candidate confirms them individually.

When the user explicitly chooses a complete AI recognition version instead of individual legacy
suggestions, use `cv_import_contract.py materialize-ai-structure`. This mode revalidates the entire
provider proposal, selects every mergeable non-null primary suggestion, and replaces the
deterministically recognized experience, education, project, skill, language, and additional-fact
view. It does not select alternatives or null suggestions. The resulting facts remain
`unverified`, are recognition evidence only, and still require individual candidate confirmation;
the command never mutates a candidate profile or makes a claim publishable.

## Apply the Evidence Policy

Read [references/evidence-policy.md](references/evidence-policy.md) for every task that creates or changes candidate claims.

Treat `candidate-profile.yaml` as the factual authority, subject to this precedence order:

1. Current explicit user confirmation.
2. Verified profile claim.
3. User-confirmed profile claim.
4. Current source material supplied by the user.
5. Inference, which must never be presented as fact.

Use only claims with status `verified` or `user_confirmed` in final application documents. Treat `inferred`, `unverified`, and `do_not_use` as unavailable. Propose profile changes as a patch and wait for confirmation before making them authoritative.

## Execute the Pipeline

Read [references/pipeline.md](references/pipeline.md) and complete these stages:

1. Extract the job into `job-analysis.yaml` using [assets/job-analysis.template.yaml](assets/job-analysis.template.yaml).
2. Classify each important requirement in `match-matrix.yaml` using [assets/match-matrix.template.yaml](assets/match-matrix.template.yaml).
3. Ask only high-value questions that could materially change a must-have match, factual accuracy, or document strategy.
4. Draft an annotated document. Attach `<!-- evidence: claim-id -->` to every factual content line. Use `<!-- evidence: editorial -->` only for non-factual transitions or motivation.
5. Audit the annotated draft:

```bash
python scripts/audit_claims.py --candidate <candidate-profile.yaml> --document <annotated.md> --output-type cv --strict
```

6. Run the style check:

```bash
python scripts/check_style.py --style <style-profile.yaml> --document <annotated.md> --document-type cv
```

7. Run a classical language check as described in [references/language-quality.md](references/language-quality.md). Prefer a local LanguageTool server. Treat suggestions as review findings, not automatic edits.
8. Execute the iterative review mode from [references/iteration-loop.md](references/iteration-loop.md). Use `standard` unless the user requests speed or maximum rigor.
9. Re-run claim, style, and language checks after revisions.
10. Strip internal evidence annotations only after all checks pass:

```bash
python scripts/audit_claims.py --candidate <candidate-profile.yaml> --document <annotated.md> --output-type cv --strict --strip-to <final.md>
```

Use `.application-work/` for intermediate artifacts unless the user requests another location. Do not expose internal evidence annotations in the final document.

## Match Job and Candidate

Classify every `must_have` and `important` requirement as exactly one of:

- `direct_match`
- `transferable_match`
- `partial_match`
- `gap`

Back every non-gap classification with claim IDs. Do not convert adjacent experience into direct experience. For example, RabbitMQ plus event-driven architecture can support a transferable Kafka match, but never a Kafka claim.

Prefer `Action + Context + Result`. Omit unknown metrics rather than estimating them. Surface gaps honestly and use adjacent evidence only when it helps a recruiter assess transferability.

## Route to the Relevant Rules

- Read [references/ats-rules.md](references/ats-rules.md) for CV creation and ATS review.
- Read [references/cv-rules.md](references/cv-rules.md) for CVs and resumes.
- Read [references/cover-letter-rules.md](references/cover-letter-rules.md) for cover letters and short application emails.
- Read [references/german-style.md](references/german-style.md) for German-language material.

Load only references relevant to the requested output.

## Iterate Through Review Roles

Use separate agents or fresh review contexts when available. Do not pass the author's hidden rationale to reviewers. Give each reviewer the raw job analysis, candidate claims, style profile, current revision, and its role-specific criteria.

Validate the review chain before finalization:

```bash
python scripts/validate_iteration.py --manifest .application-work/iteration.yaml
```

Do not describe a sequential same-context review as independent validation. Resolve every critical or high finding, or record an explicit accepted-risk rationale for the user.

## Apply the Style Profile

Use the document-specific section of `style-profile.yaml`. Preserve the candidate's stable voice, not spelling mistakes. Keep factual content identical across `conservative`, `professional`, and `personal`; vary only presentation and tone.

When producing multiple tone variants, verify identical evidence sets:

```bash
python scripts/compare_modes.py conservative.md professional.md personal.md
```

Do not imitate generic recruiter language unless the profile explicitly approves it.

## Return a Transparent Result

Alongside the requested document, report concisely:

- strongest direct matches
- transferable matches
- material gaps
- facts that still require confirmation
- profile updates worth considering

Never provide a fabricated ATS percentage. Report requirement coverage by category instead.

## Final Quality Gate

Before returning final material, verify:

- every factual statement maps to allowed evidence
- no dates, titles, employers, technologies, metrics, certifications, or team sizes were invented
- important job terminology appears naturally where supported
- unsupported keywords remain excluded
- strongest relevant information appears early
- output follows ATS-safe structure and document-specific rules
- tone matches the style profile and avoids generic AI phrasing
- factual evidence is identical across personalization modes
- classical language checks were run or their unavailability was disclosed
- critical and high review findings were resolved or explicitly accepted
