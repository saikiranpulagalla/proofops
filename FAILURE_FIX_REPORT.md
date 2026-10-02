# ProofOps V1.0.0-rc10 — Failure/Fix Report

## RC9: Gmail RFC Message-ID rewrite after committed send

RC8 incorrectly treated a caller-selected RFC Message-ID as the cross-crash Gmail
identity. Live self-send qualification proved Gmail may rewrite that header after
committing the physical message, leaving an effect UNKNOWN after response loss.
RC9 persists a pre-send Gmail history fence and opaque effect reference, embeds the
reference in the provider-observable body, verifies exact sender/recipient/subject/
payload semantics, and binds Gmail's real message ID immutably. Zero or ambiguous
candidates remain unresolved; they never authorize a blind resend.

See `V1.0_RC6_FAILURE_FIX_REPORT.md` for prior adversarial findings and fixes.

RC10 closes the separate release-integrity defect: `qualify_v10.py --require-live`
now validates one signed, identity-coherent bundle containing all five required PASS
receipts. Configuration readiness alone cannot qualify a release.

Current qualification:
- **390/390 automated tests PASS**
- **7/7 RC9 Gmail reconciliation regressions PASS**
- Alembic head **0012**
- fresh/round-trip/populated-upgrade migrations **PASS**
- live PostgreSQL/Sheets/Gmail/Gemini/deployed-E2E receipts must be rerun against RC10
