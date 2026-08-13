# Application Pipeline

## 1. Intake

Resolve the requested output, language, personalization mode, deadline, target job, and available candidate sources. Proceed with reasonable defaults unless missing information could change factual accuracy or a must-have match.

## 2. Job analysis

Create `.application-work/job-analysis.yaml` from the supplied advertisement. Preserve the original wording and normalize each requirement into one underlying competency. Classify requirements as `must_have`, `important`, or `nice_to_have`.

Do not infer that repeated marketing language is a hard requirement. Separate company claims from role requirements.

## 3. Candidate matching

Create `.application-work/match-matrix.yaml`. Cover every `must_have` and `important` requirement. Assign exactly one match class and cite claim IDs for every non-gap classification.

Use `direct_match` only for explicit evidence of the requested capability. Use `transferable_match` for adjacent technology or comparable context. Use `partial_match` when evidence covers only part of the requirement. Use `gap` when no usable evidence exists.

## 4. Strategy

Select the strongest two to four relevance themes. Order the CV and supporting material around those themes. Shorten unrelated experience; never delete chronology needed to understand the candidate's career.

Ask at most three questions at once. Ask only when an answer could resolve a must-have gap, a factual conflict, or a material positioning choice.

## 5. Annotated drafting

Draft from claims, not memory. Add evidence annotations to every factual line. Keep intermediate files in `.application-work/` and never present them as final application documents.

## 6. Deterministic checks

Run profile validation, claim auditing, and style checking. Fix failures without weakening the checks. If a failure requires a new fact, ask the user instead of changing the profile.

## 7. Finalization

Strip annotations only after checks pass. Confirm that personalization variants use identical evidence sets. Return the document plus a short coverage report listing direct matches, transferable matches, gaps, and unresolved facts.
