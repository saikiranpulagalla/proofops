#!/usr/bin/env python3
"""Live RC4 PostgreSQL schema + concurrency qualification.

Uses only synthetic rows and removes them before exit.  The release gate proves the
PostgreSQL behavior ProofOps relies on but SQLite cannot establish:
- one business-operation owner under a concurrent claim
- one approval claimant under concurrent execution requests
- one API idempotency owner for an identical key
- one active effect attempt for a logical action
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import os
from pathlib import Path
import sys
import threading
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import text

from app.api.live_runtime import assert_database_revision
from app.domain.enums import ActionState, RunState
from app.domain.models import utcnow
from app.persistence.db import make_engine, make_session_factory
from app.persistence.repository import ActionExecutionError, ApprovalClaimError, LedgerRepository
from app.release.qualification import safe_database_identity, write_receipt


def _parallel_two(fn1, fn2):
    barrier = threading.Barrier(2)
    def wrap(fn):
        barrier.wait(timeout=10)
        return fn()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(wrap, fn1), pool.submit(wrap, fn2)]
        out = []
        for f in futures:
            try:
                out.append(("ok", f.result(timeout=20)))
            except Exception as exc:  # qualification records the losing concurrency path
                out.append(("error", exc))
        return out


def _advance_run(repo: LedgerRepository, run_id: int, *states: RunState) -> None:
    for state in states:
        repo.transition_run_state(run_id, state)


def _cleanup(engine, marker: str) -> None:
    like = marker + "%"
    with engine.begin() as conn:
        run_sub = "SELECT id FROM runs WHERE goal_text LIKE :like"
        action_sub = f"SELECT id FROM actions WHERE run_id IN ({run_sub})"
        conn.execute(text(f"DELETE FROM evidence WHERE run_id IN ({run_sub})"), {"like": like})
        conn.execute(text(f"DELETE FROM effects WHERE action_id IN ({action_sub})"), {"like": like})
        conn.execute(text(f"DELETE FROM approvals WHERE run_id IN ({run_sub})"), {"like": like})
        conn.execute(text(f"DELETE FROM plans WHERE run_id IN ({run_sub})"), {"like": like})
        conn.execute(text(f"DELETE FROM actions WHERE run_id IN ({run_sub})"), {"like": like})
        conn.execute(text(f"DELETE FROM business_operations WHERE owner_run_id IN ({run_sub})"), {"like": like})
        conn.execute(text(f"DELETE FROM audit_events WHERE run_id IN ({run_sub})"), {"like": like})
        conn.execute(text("DELETE FROM api_requests WHERE idempotency_key LIKE :like"), {"like": like})
        conn.execute(text(f"DELETE FROM runs WHERE id IN ({run_sub})"), {"like": like})


def main() -> int:
    if os.environ.get("PROOFOPS_ALLOW_LIVE_POSTGRES_SMOKE") != "YES":
        print("Refusing live PostgreSQL qualification. Set PROOFOPS_ALLOW_LIVE_POSTGRES_SMOKE=YES explicitly.", file=sys.stderr)
        return 2
    url = os.environ.get("PROOFOPS_DATABASE_URL", "").strip()
    if not url.startswith("postgresql"):
        print("PROOFOPS_DATABASE_URL must point to PostgreSQL.", file=sys.stderr)
        return 2

    engine = make_engine(url)
    assert_database_revision(engine)
    repo = LedgerRepository(make_session_factory(engine))
    marker = f"SMOKE-RC4-{uuid.uuid4()}"

    try:
        # Basic transactional readback first.
        with engine.connect() as conn:
            tx = conn.begin()
            row_id = conn.execute(
                text("INSERT INTO runs(goal_text, state, created_at, updated_at) VALUES (:g, 'CREATED', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP) RETURNING id"),
                {"g": marker + "-rollback"},
            ).scalar_one()
            observed = conn.execute(text("SELECT goal_text FROM runs WHERE id=:id"), {"id": row_id}).scalar_one()
            if observed != marker + "-rollback":
                raise RuntimeError("PostgreSQL transaction readback mismatch")
            tx.rollback()

        # 1) Business-operation serialization: exactly one owner.
        r1 = repo.create_run(marker + "-op-a", owner_actor="rc4")
        r2 = repo.create_run(marker + "-op-b", owner_actor="rc4")
        op_key = marker + ":operation"
        op_results = _parallel_two(
            lambda: repo.claim_business_operation(operation_key=op_key, run_id=r1.id, lease_seconds=60),
            lambda: repo.claim_business_operation(operation_key=op_key, run_id=r2.id, lease_seconds=60),
        )
        acquired = [value for kind, value in op_results if kind == "ok" and value[1]]
        if len(acquired) != 1:
            raise RuntimeError(f"business-operation concurrency expected one winner, got {op_results!r}")

        # 2) Prepare one run/action/approval and race the approval claim.
        run = repo.create_run(marker + "-approval", owner_actor="rc4")
        _advance_run(repo, run.id, RunState.RESOLVING, RunState.PLANNING, RunState.EXECUTING, RunState.WAITING_APPROVAL)
        action, created = repo.get_or_create_action(
            run_id=run.id,
            logical_key=marker + ":send",
            action_type="SEND_EMAIL",
            target="proofops-test@example.com",
            payload_hash="a" * 64,
            payload_json='{"customer_id":"SMOKE-C001","renewal_id":"SMOKE-R1","purpose":"INITIAL_RENEWAL_OUTREACH","to":["proofops-test@example.com"],"cc":[],"bcc":[],"subject":"Synthetic","body_text":"Synthetic","thread_id":null,"attachments":[]}',
            initial_state=ActionState.PROPOSED,
        )
        if not created:
            raise RuntimeError("synthetic action unexpectedly pre-existed")
        repo.transition_action_state(action.id, ActionState.POLICY_CHECKED, expected=ActionState.PROPOSED)
        repo.transition_action_state(action.id, ActionState.APPROVAL_REQUIRED, expected=ActionState.POLICY_CHECKED)
        approval = repo.create_or_get_active_approval(
            run_id=run.id,
            action_id=action.id,
            payload_hash=action.payload_hash,
            actor="rc4",
            nonce=marker + "-nonce",
            expires_at=utcnow() + timedelta(minutes=10),
        )

        def claim_approval():
            return repo.claim_approval(
                approval_id=approval.id,
                run_id=run.id,
                action_id=action.id,
                payload_hash=action.payload_hash,
                actor="rc4",
            )

        approval_results = _parallel_two(claim_approval, claim_approval)
        approval_successes = sum(1 for kind, _ in approval_results if kind == "ok")
        if approval_successes != 1:
            raise RuntimeError(f"approval concurrency expected one claimant, got {approval_results!r}")
        if not any(kind == "error" and isinstance(value, ApprovalClaimError) for kind, value in approval_results):
            raise RuntimeError(f"approval losing request did not fail closed: {approval_results!r}")

        # 3) Two workers may not both start an effect attempt for the same action.
        def start_effect():
            return repo.start_attempt(
                action.id,
                "gmail",
                marker + "@proofops.local",
                approval_id=approval.id,
                lease_seconds=30,
            )

        effect_results = _parallel_two(start_effect, start_effect)
        effect_successes = sum(1 for kind, _ in effect_results if kind == "ok")
        if effect_successes != 1:
            raise RuntimeError(f"effect concurrency expected one active attempt, got {effect_results!r}")
        if not any(kind == "error" and isinstance(value, ActionExecutionError) for kind, value in effect_results):
            raise RuntimeError(f"effect losing request did not fail closed: {effect_results!r}")

        # 4) API idempotency unique key: one creator, one observer.
        key = marker + "-idem"
        request_hash = "b" * 64
        def claim_idem():
            return repo.claim_api_request(
                actor="rc4", route="qualification", idempotency_key=key,
                request_hash=request_hash, lease_seconds=60,
            )
        idem_results = _parallel_two(claim_idem, claim_idem)
        creator_count = sum(1 for kind, value in idem_results if kind == "ok" and value[1])
        if creator_count != 1:
            raise RuntimeError(f"idempotency concurrency expected one creator, got {idem_results!r}")

        write_receipt(
            gate="postgresql",
            target=safe_database_identity(url),
            metrics={
                "transaction_readback": True,
                "business_operation_single_owner": True,
                "approval_single_claimant": True,
                "effect_single_active_attempt": True,
                "idempotency_single_creator": True,
            },
        )
        print("PASS: PostgreSQL schema + transaction + concurrency qualification")
        print("business_operation=1/1 approval_claim=1/1 effect_claim=1/1 idempotency_owner=1/1")
        return 0
    finally:
        _cleanup(engine, marker)
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
