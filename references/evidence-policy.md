# Evidence Policy

## Claim states

Use `verified` for a claim backed by a named source. Require at least one valid `evidence_refs` entry.

Use `user_confirmed` for a claim explicitly confirmed by the candidate. A source is recommended but not mandatory.

Use `inferred` for a plausible interpretation that has not been confirmed. Never publish it as fact.

Use `unverified` for imported or conflicting information awaiting review. Never publish it as fact.

Use `do_not_use` for information the candidate has prohibited or retracted. Never use it, even if another source contains it.

## Evidence rules

- Assign a stable lowercase ID to every source and claim.
- Make each claim atomic enough to approve or reject independently.
- Keep metrics in their own claims, including unit, scope, and source.
- Link roles, projects, skills, education, certifications, and languages to claim IDs.
- Do not treat a skill name alone as proof of depth, recency, leadership, or business impact.
- Do not infer team size, user count, budget, performance improvement, seniority, or ownership.
- Do not upgrade tool adjacency into direct experience.

## Conflict handling

When sources conflict, retain both source records, set affected claims to `unverified`, and request clarification. Do not choose the more favorable version.

Apply this precedence only after conflicts are resolved:

1. Current explicit user confirmation
2. Verified claim
3. User-confirmed claim
4. Current user-supplied source
5. Inference

## Profile updates

Show proposed additions or corrections as a YAML patch. Explain the source and affected claims. Apply the patch only after user confirmation. Never convert an inference to `user_confirmed` without an explicit answer.

## Document annotations

Annotate every factual content line in an intermediate Markdown draft:

```markdown
- Designed event-driven integrations using RabbitMQ. <!-- evidence: claim-event-driven-systems -->
```

Use multiple IDs when needed:

```markdown
- Coordinated implementation across frontend and backend services. <!-- evidence: claim-coordination, claim-services -->
```

Use `<!-- evidence: editorial -->` only for non-factual transitions, role motivation, or closing language. Never use it to hide candidate, company, technology, date, metric, credential, or achievement claims.
