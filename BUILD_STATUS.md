# ProofOps Build Status — V1.0.0-rc11

## RC10 release-integrity repair
- `qualify_v10.py --require-live` now fails closed unless one complete, signed, identity-coherent RC10 receipt bundle contains PASS evidence for `postgresql`, `sheets`, `gmail`, `gemini`, and `deployed_e2e`.
- Configuration readiness is explicitly not qualification evidence.
- Gmail response-loss reconciliation: **PASS offline** — a durable pre-send history fence, opaque provider-visible reference, exact sender/recipient/subject/payload verification, and provider message-ID immutability replace caller RFC Message-ID authority.
- RC9 targeted Gmail regression suite: **7/7 PASS**.
- Alembic head is **0012**.
- Final V1.0 promotion remains blocked on the five live gates.

## Offline qualification
- V0.0–V0.9 deterministic/safety gates: **PASS**
- V1.0 RC2 recovery/session hardening: **PASS**
- V1.0 RC3 promotion-evidence hardening: **PASS**
- V1.0 RC4 authorization/evidence/promotion hardening: **PASS**
- V1.0 RC5 crash-recovery/deployment-attestation hardening: **PASS**
- V1.0 RC6 intent/freshness/approval/attestation hardening: **PASS**
- Full automated suite: **390/390 PASS**
- RC6 targeted hardening suite: **13/13 PASS**
- Fresh Alembic `0001 -> 0012`: **PASS**
- `0012 -> 0011 -> 0012` round-trip: **PASS**
- populated RC8 `0011 -> 0012`: **PASS**
- compile/import: **PASS**
- editable install using local toolchain: **PASS**
- authenticated API demo: **PASS; exactly one physical message**
- non-network V1 preflight: **PASS with five live prerequisites pending**
- `--require-live`: **fail-closed; complete signed five-gate evidence is required**
- `promote_v10.py` without explicit live execution: **correctly refused**

## Frozen identity
- Release-source SHA-256: see `release_identity.json`
- Runtime-payload SHA-256: see `release_identity.json`
- Alembic head: `0012`

## RC9 blockers closed
- explicit user no-contact goals cannot produce send actions
- obvious goal/company identity contradictions block before action creation
- original "without contacting unnecessarily" demo wording is not over-blocked
- same-version renewal-date/company drift invalidates approvals
- approval TTL/window cannot silently reuse another authorization
- stale approval recovery is bound to exact `api_request_id`
- Gmail RFC Message-ID rewriting cannot prevent response-loss reconciliation; provider-observed message identity is durable evidence
- frozen full-source and stripped runtime-payload identities are separated and verified
- login brute-force throttling is shared through durable DB state

## Live gates still pending here
1. PostgreSQL real concurrency
2. Google Sheets synthetic live gate
3. Gmail transport/business-evidence gate
4. Gemini semantic/containment gate
5. deployed real-provider E2E gate

Do not label RC9 final/live-qualified V1.0 until all five pass without source changes.
