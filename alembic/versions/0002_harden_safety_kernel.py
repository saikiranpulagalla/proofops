"""harden safety kernel after adversarial audit

Revision ID: 0002
Revises: 0001
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runs") as b:
        b.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE runs SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("runs") as b:
        b.alter_column("updated_at", nullable=False)

    with op.batch_alter_table("actions") as b:
        b.add_column(sa.Column("payload_json", sa.Text(), nullable=True))
        b.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE actions SET payload_json = '{}' WHERE payload_json IS NULL")
    op.execute("UPDATE actions SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("actions") as b:
        b.alter_column("payload_json", nullable=False)
        b.alter_column("updated_at", nullable=False)

    with op.batch_alter_table("approvals") as b:
        b.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("created_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE approvals SET run_id = (SELECT run_id FROM actions WHERE actions.id = approvals.action_id) WHERE run_id IS NULL")
    op.execute("UPDATE approvals SET created_at = CURRENT_TIMESTAMP WHERE created_at IS NULL")
    with op.batch_alter_table("approvals") as b:
        b.alter_column("run_id", nullable=False)
        b.alter_column("created_at", nullable=False)
        b.create_foreign_key("fk_approvals_run", "runs", ["run_id"], ["id"])

    with op.batch_alter_table("effects") as b:
        b.add_column(sa.Column("approval_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("reconcile_count", sa.Integer(), nullable=False, server_default="0"))
        b.add_column(sa.Column("first_unknown_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
        b.create_foreign_key("fk_effects_approval", "approvals", ["approval_id"], ["id"])
    op.execute("UPDATE effects SET updated_at = created_at WHERE updated_at IS NULL")
    with op.batch_alter_table("effects") as b:
        b.alter_column("updated_at", nullable=False)
        b.alter_column("reconcile_count", server_default=None)

    with op.batch_alter_table("evidence") as b:
        b.add_column(sa.Column("run_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("action_id", sa.Integer(), nullable=True))
        b.add_column(sa.Column("customer_id", sa.String(length=255), nullable=True))
        b.add_column(sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True))
        b.add_column(sa.Column("verified", sa.Integer(), nullable=False, server_default="1"))
    op.execute("UPDATE evidence SET action_id = (SELECT action_id FROM effects WHERE effects.id = evidence.effect_id) WHERE action_id IS NULL")
    op.execute("UPDATE evidence SET run_id = (SELECT run_id FROM actions WHERE actions.id = evidence.action_id) WHERE run_id IS NULL")
    op.execute("UPDATE evidence SET observed_at = verified_at WHERE observed_at IS NULL")
    with op.batch_alter_table("evidence") as b:
        b.alter_column("run_id", nullable=False)
        b.alter_column("action_id", nullable=False)
        b.alter_column("observed_at", nullable=False)
        b.alter_column("verified", server_default=None)
        b.create_foreign_key("fk_evidence_run", "runs", ["run_id"], ["id"])
        b.create_foreign_key("fk_evidence_action", "actions", ["action_id"], ["id"])
        b.create_unique_constraint("uq_effect_evidence", ["effect_id", "evidence_type", "source_ref"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("run_id", sa.Integer(), sa.ForeignKey("runs.id"), nullable=True),
        sa.Column("object_type", sa.String(length=64), nullable=False),
        sa.Column("object_id", sa.String(length=255), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("from_state", sa.String(length=64), nullable=True),
        sa.Column("to_state", sa.String(length=64), nullable=True),
        sa.Column("details", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("audit_events")
    with op.batch_alter_table("evidence") as b:
        b.drop_constraint("uq_effect_evidence", type_="unique")
        b.drop_constraint("fk_evidence_action", type_="foreignkey")
        b.drop_constraint("fk_evidence_run", type_="foreignkey")
        b.drop_column("verified")
        b.drop_column("observed_at")
        b.drop_column("customer_id")
        b.drop_column("action_id")
        b.drop_column("run_id")
    with op.batch_alter_table("effects") as b:
        b.drop_constraint("fk_effects_approval", type_="foreignkey")
        b.drop_column("updated_at")
        b.drop_column("lease_expires_at")
        b.drop_column("first_unknown_at")
        b.drop_column("reconcile_count")
        b.drop_column("approval_id")
    with op.batch_alter_table("approvals") as b:
        b.drop_constraint("fk_approvals_run", type_="foreignkey")
        b.drop_column("consumed_at")
        b.drop_column("claimed_at")
        b.drop_column("created_at")
        b.drop_column("run_id")
    with op.batch_alter_table("actions") as b:
        b.drop_column("updated_at")
        b.drop_column("payload_json")
    with op.batch_alter_table("runs") as b:
        b.drop_column("updated_at")
