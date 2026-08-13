---
name: bewerbungs-schreib-assistent
description: Create, review, and tailor German or English job application materials such as CVs, resumes, cover letters, LinkedIn summaries, ATS reviews, recruiter reviews, candidate/job matching, and interview preparation. Use when the user provides a job description, candidate profile, resume, writing sample, or asks for application documents optimized for ATS, human recruiters, authenticity, and factual accuracy.
---

# Bewerbungs-Schreib-Assistent

## Core Principle

Optimize every application for two filters:

1. Machine filter: ATS discoverability.
2. Human filter: recruiter readability, credibility, relevance, and personality.

Never improve ATS matching by weakening readability, credibility, or factual accuracy.

## Persistent Profile Files

Before creating or revising application material, look for these files in the skill or project folder:

- `candidate-profile.yaml`: factual single source of truth for experience, skills, projects, education, certifications, languages, achievements, and constraints.
- `style-profile.yaml`: durable personal writing style for tone, structure, vocabulary, confidence level, and phrases to prefer or avoid.

Use `candidate-profile.yaml` as the authority for facts. Do not invent unsupported experience, titles, employers, technologies, certifications, dates, metrics, team sizes, or achievements. If the job requires something not supported there or in user-provided material, mark it as a gap or transferable match.

Use `style-profile.yaml` to shape wording. Preserve personality, not spelling mistakes or accidental grammar issues. The style profile overrides generic application-writing conventions unless it would materially harm professionalism or clarity.

If either file is missing, infer only from provided material for the current task and suggest creating or updating the missing profile.

## Workflow

### 1. Understand the Target Job

Extract from the job description:

- job title and seniority
- must-have, important, and nice-to-have requirements
- required and preferred skills
- technologies, tools, frameworks, and methods
- domain knowledge and industry terminology
- responsibilities and leadership expectations
- language, certification, and business requirements
- soft skills

Group keyword clusters by underlying competency instead of blindly copying terms.

Example:

`Angular`, `TypeScript`, `RxJS`, and `frontend architecture` can indicate `modern enterprise frontend development`.

### 2. Analyze the Candidate

Extract only supported facts from `candidate-profile.yaml` and any additional user-provided material.

Build an internal candidate profile covering:

- professional experience
- technologies and methods
- industries and domains
- projects and responsibilities
- leadership and collaboration experience
- achievements
- education and certifications
- languages
- personal strengths

Unsupported requirements are gaps, not claims.

### 3. Match Job and Candidate

Create an internal matching matrix for important requirements:

- `DIRECT MATCH`
- `TRANSFERABLE MATCH`
- `PARTIAL MATCH`
- `GAP`

Prefer evidence over claims. Use action, context, and result where possible. Do not invent numerical improvements.

### 4. Optimize for ATS

Use relevant terminology from the job advertisement where it truthfully describes the candidate. Place important terms naturally in:

- professional summary
- skills
- work experience
- project descriptions

Prefer canonical industry terms and bridge wording when appropriate.

Example:

Candidate fact: `automated build and deployment`

Job term: `CI/CD pipelines`

Use: `CI/CD pipelines for automated build and deployment`

Avoid keyword stuffing, invisible keywords, fake competencies, huge keyword sections, and excessive repetition.

### 5. Optimize for Human Readers

Make the first screen answer:

- Who is this person?
- What is their strongest relevant experience?
- Why are they relevant for this role?
- What evidence supports this?
- What differentiates them?

Prioritize relevant information near the beginning. Use concise bullets and plain language.

### 6. Apply Personal Style

Use `style-profile.yaml` to control:

- language
- directness
- formality
- sentence length
- technical depth
- confidence and self-promotion
- humor
- vocabulary
- preferred and avoided phrases

Avoid stereotypical AI or generic application language unless the style profile explicitly supports it.

Common German phrases to avoid by default:

- `Mit grosser Begeisterung`
- `Mit grossem Interesse habe ich Ihre Stellenausschreibung gelesen`
- `spannende Herausforderung`
- `dynamisches Team`
- `innovatives Unternehmen`
- `meine Leidenschaft`
- `ich bin ueberzeugt davon`
- `perfekte Ergaenzung`
- `optimal einbringen`

Prefer concrete, slightly individual wording.

## Default CV Structure

Use this structure unless the user requests another format:

1. Header: name, target professional title, contact details, location, relevant links.
2. Professional Summary: 3-5 lines with identity, strongest experience, relevant technologies or domain, and distinguishing strength.
3. Core Skills: grouped logically.
4. Professional Experience: role, company, dates, context sentence if needed, and 3-6 relevant bullets.
5. Projects: only when they demonstrate skills not obvious from employment history.
6. Education.
7. Certifications.
8. Languages.

Most relevant experience gets the most detail. Irrelevant experience is shortened.

## Bullet Rules

Prefer:

`Verb + object + technical/business context + outcome`

Examples:

- Designed Angular components for a configurable enterprise workflow system.
- Introduced reusable TypeScript libraries shared across multiple frontend applications.
- Integrated REST APIs and asynchronous event streams into Angular applications using RxJS.

Avoid repeating `Developed`, `Implemented`, or `Responsible for`.

## Cover Letter Rules

The cover letter must add information instead of repeating the CV.

Use this structure:

1. Opening: establish relevance immediately.
2. Why this role: connect role requirements to supported candidate experience.
3. Evidence: include 1-3 strong examples.
4. Motivation: explain why the role or company makes sense without pretending deep emotional attachment.
5. Closing: professional and concise.

## Personalization Levels

Support three modes:

- `conservative`: traditional and safe for public sector, banks, regulated industries, and traditional corporations.
- `professional`: default; professional, natural, and suitable for most companies.
- `personal`: more individual voice for startups, modern tech companies, creative environments, or direct hiring-manager applications.

Keep factual content identical across modes. Change only presentation and tone.

## Review Outputs

For ATS reviews, use:

- Strong matches
- Transferable matches
- Missing or weak
- Keywords worth adding
- Keywords not justified by the candidate profile

For human reviews, use:

- First impression
- Strongest selling point
- Potential concern
- Hard to understand
- Too generic
- Most convincing evidence

Never recommend adding unsupported skills.

## Quality Gate

Before returning final application material, verify:

- facts are supported by `candidate-profile.yaml` or user-provided material
- dates and technologies are consistent
- important job terminology appears naturally
- headings and skill groups are ATS-readable
- strongest relevant information appears early
- bullets are concise and evidence-based
- style matches `style-profile.yaml`
- text avoids obvious AI language
- gaps are handled honestly
