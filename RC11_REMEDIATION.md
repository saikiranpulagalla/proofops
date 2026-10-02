# ProofOps V1.0.0-rc11 remediation scope

RC11 is a narrow remediation of the Astra RC10 reliability findings.  It does not
claim live Gemini qualification, and it does not change business workflow,
qualification thresholds, Google OAuth scopes, or the database migration head.

## Restored invariants

- An ambiguous consequential Gmail effect remains `UNKNOWN` until authoritative
  reconciliation proves one exact provider message.  Empty reads, elapsed time,
  repeated execution requests, a new approval, and process recovery cannot turn
  it into resend authority.
- The production CRM-bound Gmail composition explicitly preserves history-fence and
  effect-reference reconciliation capabilities.  Missing capabilities fail closed.
- Gmail POST dispatch uses a no-replay `httplib2` transport.  A connection loss is
  surfaced as an ambiguous outcome to the durable effect ledger rather than retried
  below it.
- SQLite approval ownership uses a conditional state update.  The permanent
  independent-process regression counts actual provider sends and permits at most
  one claimant.
- Receipt validation binds the configured qualification session and verifies the
  complete fixed Gemini corpus cardinality.  Duplicate JSON keys are rejected.
- Reconciliation rejects candidate MIME messages with conflicting recipient-visible
  text alternatives, and Gmail history/search pagination must be exhausted before
  uniqueness is claimed.
- Invalid Unicode passwords are rejected through the normal authentication failure
  path rather than raising a server error.

## Deliberate boundaries

Google Sheets offers optimistic version checking only; it is not represented as a
provider-side atomic compare-and-swap across independent writers.  Cross-provider
freshness reduces but cannot atomically eliminate the interval between evidence
reads and Gmail send.  Promotion signature verification is cryptographic evidence
verification; the release validator remains the authority for validating the
five-gate qualification contract before promotion is generated.

## Evidence

The permanent regression suite includes `test_rc11_sqlite_claim_race.py`, real
`httplib2` connection-drop coverage, Gmail history/search pagination tests,
production-wrapper reconciliation tests, MIME alternative tests, and release
receipt session/cardinality/strict-JSON tests.  The RC10 frozen candidate is not
modified by this work.
