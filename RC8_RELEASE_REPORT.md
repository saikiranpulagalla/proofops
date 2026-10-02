# ProofOps V1.0.0-rc8 Release Report

RC8 fixes Windows-safe cleanup for `scripts/demo_v09_api.py`. The authenticated
demo now closes FastAPI's test-client lifespan and disposes the SQLite-owning
SQLAlchemy engine before temporary-directory cleanup. This preserves the
existing workflow result: `COMPLETED_VERIFIED`, one physical message, three
evidence rows, and idempotent replay.

Offline qualification is limited to local deterministic checks. PostgreSQL,
Sheets, Gmail, Gemini, and deployed HTTPS end-to-end gates remain pending.
