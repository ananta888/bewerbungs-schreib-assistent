# Existing CV import contract

The import boundary accepts local HTML, PDF, DOCX, and ODT files and emits a versioned YAML
proposal. It can additionally emit a self-contained static HTML review page. It does not modify
`candidate-profile.yaml` and it never contacts a remote service.

```bash
python scripts/cv_import_contract.py capabilities
python scripts/cv_import_contract.py extract --input private/cv.docx --output .application-work/cv-import.yaml --html-output .application-work/cv-import.html
python scripts/cv_import_contract.py normalize-extracted --extracted-envelope - --output .application-work/cv-import.yaml --html-output .application-work/cv-import.html
python scripts/cv_import_contract.py validate --proposal .application-work/cv-import.yaml
python scripts/cv_import_contract.py validate-ai-structure --request -
python scripts/cv_import_contract.py apply-ai-structure --request - --output -
python scripts/cv_import_contract.py materialize-ai-structure --request - --output -
```

The root application should invoke this CLI with an argument array (never a shell string), keep
the output under `.application-work/`, and present records and atomic claims for individual review.
Stable source and claim IDs are derived from the input hash and normalized source text. Re-importing
identical input therefore produces identical IDs.

`--html-output` is optional on both import commands. It writes an escaped, script-free page with
the records, warnings, conflicts, and import counts so the result can be opened immediately in a
browser. The page intentionally excludes the private line manifest and remains an unconfirmed,
non-publishable display of the same proposal.

Normalization collapses repeated case-insensitive copies of the same atomic token or structured
record deterministically. The first occurrence and its source anchor are retained, and its status
remains `unverified`; repetition never upgrades or strengthens the evidence state.

All records and claims have status `unverified`, even when extraction has no warning. A separate,
explicit candidate action is required before applying a proposed profile patch or changing a claim
to `user_confirmed`. Extraction warnings and conflicts must stay attached to the proposal and must
be visible during review. The import itself is not an evidence verification.

For the production adapter, `normalize-extracted` accepts an `extracted-cv-text` JSON envelope from
standard input when its argument is `-`. With `--output -`, it returns the full proposal as JSON on
standard output, so private extracted text need not be written to a temporary file. The envelope
contains source SHA-256, byte size, media type, extractor name, text, text SHA-256, and structured
warnings. Every normalized field becomes a stable atomic `fact-*` plus a corresponding `claim-*`.
Both standard-input envelopes and JSON standard-output responses are UTF-8 bytes independent of
the operating-system locale; callers must not use a locale-specific text encoding for this pipe.

The private normalization artifact also contains `extraction.line_manifest`. Each entry is exactly
`{line, text, sha256}` with one-based sequential line numbers. `sha256` binds the UTF-8 line and
`extraction.text_sha256` binds all manifest lines joined by `\n`. This manifest contains CV text:
it must remain encrypted at rest, must never appear in a public/list DTO or log, and must be deleted
with the import retention record. Public import records may expose only hashes and counts.

## Optional AI-assisted structure proposals

The submodule does not call an AI provider. An explicitly opted-in root service may give a provider
the private line manifest and receive an `ai-cv-structure-proposal` version `1.0`. Its closed JSON
Schema is [ai-cv-structure-proposal.schema.json](../contracts/v1/ai-cv-structure-proposal.schema.json).
The proposal is always `unverified` and binds exactly to:

- the CV `source_id` and source SHA-256
- the normalized line-manifest SHA-256
- the canonical SHA-256 of the complete base `cv-import-proposal`

The provider envelope has only these root fields:

```json
{
  "contract": "ai-cv-structure-proposal",
  "contract_version": "1.0",
  "status": "unverified",
  "binding": {
    "source_id": "source-cv-0000000000000000",
    "source_sha256": "<64hex>",
    "text_sha256": "<64hex>",
    "base_proposal_sha256": "<64hex>"
  },
  "sections": [],
  "employment": [],
  "education": [],
  "projects": [],
  "skills": [],
  "languages": []
}
```

Employment blocks contain `employer`, `role`, `start_date`, `end_date`, `location`, and `details`.
Education contains institution, qualification, dates, location, and details. Projects contain name,
role, dates, details, and technologies. Skills are field suggestions; languages contain language and
level. Every content field is shaped as:

```json
{
  "value": "exact source text or null",
  "source_anchor": {
    "line_start": 1,
    "line_end": 1,
    "char_start": 0,
    "char_end": 10,
    "quote": "exact source text"
  },
  "confidence": 0.9,
  "alternatives": [
    {
      "value": "another exact source span",
      "source_anchor": {
        "line_start": 1,
        "line_end": 1,
        "char_start": 2,
        "char_end": 8,
        "quote": "source"
      },
      "confidence": 0.5
    }
  ],
  "questions": [],
  "status": "unverified"
}
```

Line numbers are one-based. Character offsets are zero-based Unicode-code-point offsets; the end is
exclusive in the last line. For a multi-line anchor, the exact quote is the first-line suffix,
intermediate lines, and last-line prefix joined by `\n`. Every non-null primary or alternative value
must equal that exact quote. A null primary has a null anchor, zero confidence, and at least one
question. A confidence value never upgrades evidence status.

The validator keeps an already exact source span unchanged. If only the declared character or line
coordinates are inaccurate, it may canonicalize those coordinates from the exact quote, but only
inside the provider-declared line range and only when that quote occurs there exactly once. The
non-null value must already equal the quote byte-for-byte as Unicode text. No whitespace, case,
punctuation, date, or other content normalization is permitted. A missing or repeated quote, an
out-of-manifest declared line range, or a value/quote mismatch is rejected. The canonicalized
projection remains `unverified`; it does not confirm evidence. Root integrations retain their hash
of the original provider output, while apply and materialization repeat the same deterministic
canonicalization before their ordinary exact-span validation.

`validate-ai-structure` accepts a UTF-8 `ai-cv-structure-validation-request` version `1.0`:

```json
{
  "contract": "ai-cv-structure-validation-request",
  "contract_version": "1.0",
  "base_proposal": {},
  "expected_proposal_sha256": "<64hex>",
  "ai_proposal": {}
}
```

It checks the base CAS, all source/text/proposal digests, the private manifest, exact closed keys,
source spans, quotes, candidate values, statuses, supported dates, duplicates, and conflicting date
ranges. It returns `validated-ai-cv-structure-proposal` version `1.0`. Each field receives a stable
`suggestion-*` ID; each source-backed alternative receives a stable `alternative-*` ID. Section
heading classifications are visible but non-mergeable.

`apply-ai-structure` accepts the same base and provider proposal in a closed
`ai-cv-structure-apply-request` plus:

```json
{
  "selections": [
    {
      "suggestion_id": "suggestion-0000000000000000",
      "alternative_id": null
    }
  ]
}
```

It revalidates the complete provider proposal and every binding before applying anything. Only the
selected primary value or selected alternative becomes a normal atomic fact and claim. Dates are
normalized deterministically only after their original text has passed the exact-span check. Every
result remains `unverified`; unselected, null, unknown, duplicate, stale, conflicting, and
out-of-source suggestions fail closed or remain absent. AI is recorded only as recognition
provenance (`recognition_method: ai_assisted` plus suggestion/alternative IDs). The evidence source
remains the original CV source ID and SHA-256. This command never reads or writes a candidate
profile, never confirms facts, and never makes content publishable.

### Complete AI recognition version

`materialize-ai-structure` is the explicit full-version alternative to the legacy, individually
selected `apply-ai-structure` merge. It accepts a closed
`ai-cv-structure-materialization-request` version `1.0`; its schema is
[ai-cv-structure-materialization-request.schema.json](../contracts/v1/ai-cv-structure-materialization-request.schema.json):

```json
{
  "contract": "ai-cv-structure-materialization-request",
  "contract_version": "1.0",
  "base_proposal": {},
  "expected_proposal_sha256": "<64hex>",
  "ai_proposal": {}
}
```

The command fully repeats the base-proposal CAS, private line-manifest, provider binding, exact
source-span, date, duplicate, and closed-key validation performed by `validate-ai-structure`.
After validation it automatically materializes every mergeable suggestion whose primary value and
primary source anchor are non-null. A primary remains the choice even when alternatives exist.
Alternatives, null primaries (including null primaries that have alternatives), and non-mergeable
section classifications are not materialized. More than 2,000 resulting facts fail closed; the
command never truncates the recognition version. A proposal without any mergeable non-null primary
facts fails with `ai_materialization_no_usable_facts`; it can never activate an almost-empty AI
recognition version.

Standard output is the complete `cv-import-proposal` version `1.0`, not a summary envelope. Its
`experience`, `education`, `projects`, `skills`, and `languages` collections and all facts and
claims referenced by those collections come only from the validated AI suggestions. The previous
deterministic records in those collections and every `additional_facts` entry are removed rather
than retained as a raw fallback list. The following deterministic carve-outs are preserved and
remain separately identifiable by their original, non-AI source anchors:

- the top-level primary source plus only source metadata still referenced by retained content
- the defined extraction engine, hashes, counts, source/passive safety warnings, and private
  `line_manifest`
- `proposal.profile.facts` and their exact one-to-one facts and claims
- `proposal.certifications` and their exact referenced facts and claims

The result is rebuilt from those defined fields. Unknown legacy root or extraction extensions are
not copied, so an old raw fallback list cannot survive under a different key.
`extraction.conflicts` is rebuilt from the validated AI proposal. Deterministic structure conflicts
that refer to the replaced recognition records are not copied into the new version. The current AI
validator rejects conflicting provider structures and therefore materializes an empty conflict
list; source/passive safety warnings from extraction remain unchanged.

The materialized extraction audit contains exactly one current AI entry with
`mode: replace_recognition_version`, the original base binding, and the sorted IDs of all
materialized primary suggestions. Previous AI audit entries are replaced for this recognition
version. All generated records, facts, and claims have status `unverified`; their source anchors
carry `origin: ai_structuring`, `recognition_method: ai_assisted`, the suggestion ID, and a null
alternative ID. Provider confidence remains review metadata. The command does not read or write a
candidate profile. `adopt-confirmed` can use the result only after a later explicit local
confirmation decision for each fact to be adopted.

The artifact SHA-256 is `proposal_cas_sha256`: SHA-256 of compact, key-sorted UTF-8 JSON for the
entire returned proposal, including its private extraction audit and line manifest. The request's
`expected_proposal_sha256` and the provider binding always refer to the complete input base
proposal, not to the returned replacement artifact.

`adopt-confirmed` requires decisions by `fact_id`, explicit-local-confirmation markers, and the
expected SHA-256 of the candidate profile. It performs a compare-and-swap before its atomic write.
Pending and rejected facts are never copied. Employment records materialize only after company,
role, start, and end facts have each been confirmed.

User-entered additions use the same review path:

```bash
python scripts/cv_import_contract.py extend-user-facts \
  --proposal .application-work/cv-import.yaml \
  --additions .application-work/cv-user-facts.json \
  --expected-proposal-sha256 <canonical-json-sha256> \
  --output .application-work/cv-import-extended.yaml
```

An addition is shaped as `{id, collection, field, value, category}`. `id` is a unique lowercase
hyphenated ID created by the root server. To attach a fact, include an existing `record_id`; to
build a new record, provide the same server-created `record_key` for each of its atomic fields.
Allowed collections and fields are returned by `capabilities.user_fact_fields`. The canonical
proposal digest is SHA-256 over compact, key-sorted UTF-8 JSON and is returned after extension.
Added facts remain `unverified` and use a separate hash-only `source-user-*` provenance record.

Adoption decisions are JSON objects shaped as
`{fact_id, decision, explicitly_confirmed, confirmation_origin}`. `decision` is `confirm`, `reject`,
or `pending`; `confirm` additionally requires `explicitly_confirmed: true` and
`confirmation_origin: explicit_local_user_action`. The adoption response reports the new candidate
SHA-256 and adopted fact, claim, and structured-record IDs.

## Concurrent adoption and recovery

Candidate adoption holds an exclusive `O_EXCL` lock file in the candidate profile directory across
the complete read, CAS check, validation, intent journal, profile replacement, and completion
journal. A competing process waits for a bounded interval and then fails closed. Locks older than
five minutes are reported as `profile_lock_stale`; they are never silently removed because liveness
cannot be proven from a PID alone.

The same lock and journal boundary protects every existing-profile mutation exposed by
`profile_contract.py`: both `patch` and `add-import` require
`--expected-candidate-sha256 <sha256>`. Function callers must likewise pass the digest of the exact
candidate bytes on which their decision was based. There is no fallback that silently refreshes a
stale digest. A CV adoption and a profile patch/import therefore cannot overwrite one another: the
first valid CAS commits and the other process is rejected after acquiring the shared lock.

Before replacing the profile, the CLI appends and `fsync`s an intent containing transaction ID and
before/after hashes. It `fsync`s the proposed profile, performs an atomic replace, and then appends
and `fsync`s the completion record. An interrupted intent is classified under the same exclusive
lock: current profile equal to the before hash is safely marked aborted; equality with the after
hash is safely marked committed. Any other state returns `recovery_required` without writing the
profile.

Read-only diagnostics are available as:

```bash
python scripts/cv_import_contract.py recovery-status --candidate private/candidate-profile.yaml
```

The result contains only hashes, transaction IDs, and the classification `not_applied`,
`applied_without_completion`, or `ambiguous`; it does not expose candidate content.

Safety limits are fixed by the capability response. HTML scripts/styles are ignored. DOCX and ODT
are parsed from their XML without launching Microsoft Office or LibreOffice; macros, executable
archive parts, unsafe archive paths, excessive expansion, and external relationships are rejected.
PDF uses only a locally installed `pdftotext` executable with a timeout and a fixed argument list;
active PDF content is rejected. No extractor uses network access.
