# ProofOps — Verified Autonomous Operations Agent

ProofOps is a bounded autonomous operations agent for renewal outreach. The LLM proposes a
typed semantic plan; deterministic code owns user-intent constraints, identity, policy,
human approval, side effects, reconciliation, evidence and completion.

Core safety chain:

`Goal -> deterministic GoalIntent -> durable run -> CRM/Gmail evidence -> GoalContract -> typed plan -> deterministic validation -> approval -> durable effect -> reconciliation -> evidence -> proven outcome`

## RC11 qualification status

- the checked-in deterministic test suite is included and can be run locally with `pytest -q`
- Alembic head: **0012**
- authenticated API demo: **PASS, one physical effect, deterministic SQLite cleanup**
- fresh live Sheets and Gmail qualification: **PASS**
- full five-gate release qualification remains fail-closed: Gemini qualification is externally blocked under the zero-budget constraint, and deployed E2E is therefore incomplete.

RC11 preserves Gmail response-loss reconciliation and fail-closed live
qualification evidence validation:

1. explicit user no-contact intent and obvious goal/company conflicts are deterministic constraints, not planner suggestions;
2. pre-effect business freshness re-evaluates material fields and business eligibility even when CRM version does not change;
3. approval authorization windows and stale HTTP recovery are causally bound to the exact approval request;
4. final deployment evidence requires an externally supplied immutable deployment revision rather than worker/boot identity;
5. an immutable durable effect reference plus a pre-send Gmail history fence identifies a committed message even if Gmail rewrites the caller-selected RFC Message-ID and the response is lost;
6. `--require-live` validates a complete signed bundle for `postgresql`, `sheets`, `gmail`, `gemini`, and `deployed_e2e` against the exact release, deployment revision, and artifact digest.

Frozen identities:

- release source: recorded in `release_identity.json` after final source freeze
- runtime payload: recorded in `release_identity.json` after final source freeze

Use `deploy.env.example` for non-secret configuration names. Keep OAuth tokens, Gemini keys,
database credentials, session secrets, UI passwords and the Ed25519 release private key outside
the repository.

Offline checks:

```bash
pytest -q
python scripts/qualify_v10.py
python scripts/demo_v09_api.py
```

Final promotion, only in the dedicated synthetic live deployment:

```bash
python scripts/promote_v10.py --execute-live \
  --google-token "$PROOFOPS_GOOGLE_TOKEN_FILE" \
  --spreadsheet-id "$PROOFOPS_SHEETS_SPREADSHEET_ID" \
  --customer-id "$PROOFOPS_E2E_CUSTOMER_ID" \
  --renewal-id "$PROOFOPS_QUALIFICATION_RENEWAL_ID"
```

RC11 must remain unchanged between successful live qualification and final V1.0 tagging. Any
source change creates a new RC.
