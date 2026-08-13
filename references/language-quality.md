# Classical Language Quality Checks

## Tool order

1. Run the skill's deterministic claim and style checks.
2. Run LanguageTool for spelling, grammar, punctuation, and rule-based style.
3. Optionally run Hunspell as a second local dictionary check.
4. Let the language reviewer classify findings and reject false positives.
5. Re-run claim auditing after corrections.

LanguageTool supports many languages and can expose a local HTTP server. Prefer the local endpoint configured in `style-profile.yaml` because application documents contain personal data.

```bash
python scripts/language_check.py --backend languagetool --document final-draft.md --language de-DE --server http://localhost:8010/v2/check
```

Use a remote LanguageTool endpoint only with explicit user approval and `--allow-remote`. Never silently send names, contact details, employment history, or application text to an external service.

Hunspell performs dictionary-based spelling and morphological checks. It does not replace grammar review:

```bash
python scripts/language_check.py --backend hunspell --document final-draft.md --dictionary de_DE --allowlist technical-terms.txt
```

Use `en_US`, `en_GB`, `de_DE`, or another installed dictionary matching the document locale. Report missing binaries or dictionaries rather than skipping silently.

Vale may be added for organization-specific terminology and style rules, but it is not a general grammar checker. The built-in `check_style.py` already covers the skill's stable personal rules.

## Correction policy

- Never auto-apply all suggestions.
- Protect names, company names, technologies, certifications, URLs, and approved terminology with an allowlist.
- Reject suggestions that change a factual claim, tone, locale, or intended technical meaning.
- Record accepted corrections in the relevant review pass.
- Run the checker again until no accepted issue remains or a false positive is documented.
